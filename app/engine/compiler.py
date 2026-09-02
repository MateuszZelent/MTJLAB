"""Compile a declarative recipe into a finite, preflight-validated action plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
from collections.abc import Iterable, Mapping
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING, Any, Callable, Final

from app.devices.anritsu_ms2830a.adapter import (
    AdvancedSpectrumConfig,
    SignalGeneratorConfig,
    SpectrumConfig,
)
from app.devices.rigol_dg1000z.adapter import RigolChannelConfig, RigolOutputConfig
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    Quantity,
    parse_quantity,
)
from app.recipes.models import Recipe, RecipeNode
from app.recipes.parameter_registry import SWEEP_DIMENSIONS
from app.recipes.semantic_tree import AxisPointContext, SemanticMeasurementTree, normalize_recipe_tree
from app.recipes.sweep_points import generate_sweep_points
if TYPE_CHECKING:
    from app.contracts import DeviceModuleRegistry
from app.safety.keithley import (
    KeithleySourceRequest,
    quantize_keithley_value,
    validate_keithley_source,
)
from app.safety.anritsu import (
    validate_anritsu_advanced_spectrum,
    validate_anritsu_signal_generator,
    validate_anritsu_spectrum,
    validate_anritsu_trace_name,
)
from app.safety.rigol_current import (
    quantize_rigol_frequency,
    quantize_rigol_voltage,
    validate_rigol_waveform,
)
from app.settings.models import StationSettings


_REFERENCE_RE: Final = re.compile(r"^\$\{([A-Za-z0-9_.-]+)}$")


@dataclass(frozen=True, slots=True)
class PlanAction:
    node_id: str
    kind: str
    payload: dict[str, Any]
    setpoints_si: dict[str, float]
    is_finally: bool = False
    semantic_id: str | None = None
    source_node_id: str | None = None
    axis_context: AxisPointContext | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    recipe_name: str
    actions: tuple[PlanAction, ...]
    total_points: int
    sha256: str
    recipe_source: str
    required_devices: frozenset[str] = frozenset()
    total_spectra: int = 0
    safe_shutdown_actions: tuple[str, ...] = ()
    recipe_dut_limits: dict[str, Any] = field(default_factory=dict)


def required_devices_for_actions(actions: Iterable[PlanAction]) -> frozenset[str]:
    """Return the exact instrument set touched by an immutable execution plan."""

    required: set[str] = set()
    for action in actions:
        if "rigol" in action.kind:
            required.add("rigol")
        if "keithley" in action.kind:
            required.add("keithley")
        if "anritsu" in action.kind or action.kind in {"acquire_reference", "acquire_spectrum"}:
            required.add("anritsu")
        if action.kind == "measure_moke_hall":
            required.add("moke_box")
        if action.kind == "measure_lakeshore_field":
            required.add("lakeshore_gaussmeter")
        if action.kind == "verify_connection":
            required.add(str(action.payload["device"]))
        if action.kind == "assert_output_on":
            device = str(action.payload.get("device", ""))
            required.add("anritsu" if device == "anritsu_sg" else device)
    return frozenset(required)


class RecipeCompiler:
    """Reject unsafe values before an adapter can see an execution request."""

    def __init__(
        self,
        settings: StationSettings,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        outputs_forced_off: bool = False,
        device_registry: DeviceModuleRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._max_actions = int(settings.execution.get("max_expanded_points", 100_000)) * 10
        self._cancellation_requested = cancellation_requested
        self._outputs_forced_off = bool(outputs_forced_off)
        if device_registry is None:
            # Lazy import avoids the composition registry importing device UI
            # modules while this compiler is still being initialized.
            from app.devices.registry import built_in_device_registry

            device_registry = built_in_device_registry()
        self._device_registry = device_registry
        self._semantic_tree: SemanticMeasurementTree | None = None
        self._semantic_axes_by_source: dict[str, object] = {}
        self._active_axis_path: tuple[str, ...] = ()
        self._recipe_nodes: dict[str, RecipeNode] = {}

    def _check_cancelled(self) -> None:
        if (
            self._cancellation_requested is not None
            and self._cancellation_requested()
        ):
            raise ConfigurationError("Recipe compilation cancelled.")

    def _semantic_point_count(self) -> int:
        """Count Cartesian leaf points without changing checkpoint semantics."""

        tree = self._semantic_tree
        if tree is None:
            return 0
        total = 0

        def visit(node) -> bool:
            nonlocal total
            has_axis_descendant = False
            for child in node.children:
                child_contains_axis = visit(child)
                has_axis_descendant = has_axis_descendant or child_contains_axis
            if node.kind.value == "sweep_axis" and node.axis is not None and not has_axis_descendant:
                total += len(tree.point_contexts.get(node.source_node_id or "", ()))
            return node.kind.value == "sweep_axis" or has_axis_descendant

        for root in tree.roots:
            visit(root)
        return total

    @staticmethod
    def _axis_update_matches(child: RecipeNode, compiled_kind: str) -> bool:
        """Identify an authored technical update represented by Set ROI value."""

        return child.type == compiled_kind

    @staticmethod
    def _binding_configured(binding, actions: list[PlanAction]) -> bool:
        channel = str(binding.endpoint)
        if binding.device_module == "keithley":
            return any(
                action.kind == "configure_keithley"
                and str(action.payload.get("request").channel) == channel
                for action in actions
            )
        if binding.device_module == "rigol":
            return any(
                action.kind == "configure_rigol"
                and str(action.payload.get("config").channel) == channel
                for action in actions
            )
        if binding.device_module == "anritsu":
            return any(
                action.kind in {"configure_anritsu", "configure_anritsu_sg"}
                for action in actions
            )
        return False

    def _semantic_axis_action(
        self,
        node: RecipeNode,
        binding,
        value: Quantity,
        context: dict[str, Quantity],
        point_index: int,
        point_count: int,
        active_axis_ids: tuple[str, ...],
    ) -> PlanAction:
        provider = self._device_registry.sweep_providers().get(binding.device_module)
        if provider is None:
            raise ConfigurationError(
                f"{node.id}: no registered sweep provider for {binding.device_module!r}."
            )
        owner = self._recipe_nodes.get(binding.owner_node_id, node)
        provider_node = owner
        if binding.owner_node_id == node.id and not owner.data.get("channel"):
            target_tail = binding.target.rsplit(".", 1)[-1]
            mode = "current" if target_tail in {"current", "compliance_voltage"} else "voltage"
            provider_node = RecipeNode(
                node.id,
                node.type,
                {**node.data, "channel": binding.endpoint, "source_mode": mode},
                node.children,
                node.else_children,
            )
        compiled = provider.compile_point(provider_node, binding, value, context, self._settings)
        axis_context = AxisPointContext(
            binding.axis_id,
            point_index,
            point_count,
            next(
                stage.stage_index
                for stage in binding.stages
                if any(point.si_value == value.si_value for point in stage.points)
            ),
            value.si_value,
            {key: item.si_value for key, item in context.items()},
            (*active_axis_ids, binding.source_node_id),
        )
        payload = dict(compiled.payload)
        payload.setdefault("requested_si", compiled.requested_si)
        payload.setdefault("applied_si", compiled.applied_si)
        return PlanAction(
            f"{binding.axis_id}.point.{point_index}",
            compiled.action_kind,
            payload,
            {key: item.si_value for key, item in context.items()},
            semantic_id=f"{binding.axis_id}.set-roi-value",
            source_node_id=node.id,
            axis_context=axis_context,
        )

    def compile(self, recipe: Recipe) -> ExecutionPlan:
        actions: list[PlanAction] = []
        self._recipe_nodes = {}

        def index_recipe_node(node: RecipeNode) -> None:
            self._recipe_nodes[node.id] = node
            for child in (*node.children, *node.else_children):
                index_recipe_node(child)

        index_recipe_node(recipe.root)
        for node in recipe.finally_nodes:
            index_recipe_node(node)
        try:
            self._semantic_tree = normalize_recipe_tree(
                recipe, self._device_registry.sweep_providers()
            )
            self._semantic_axes_by_source = {
                node.source_node_id: node.axis
                for node in self._semantic_tree.by_id.values()
                if node.axis is not None and node.source_node_id is not None
            }
        except Exception:
            # Keep the established compiler diagnostics for recipes that do not
            # use sweep axes; axis-bearing documents fail at the same typed
            # normalization boundary with the precise ConfigurationError.
            self._semantic_tree = None
            self._semantic_axes_by_source = {}
        self._visit(recipe.root, {}, actions)
        for node in recipe.finally_nodes:
            self._visit(node, {}, actions, is_finally=True)
        self._validate_reference_flow(actions)
        self._validate_device_state_flow(actions)
        if not actions:
            raise ConfigurationError("The recipe contains no executable actions.")
        if len(actions) > self._max_actions:
            raise SafetyViolation(
                f"The plan expands to {len(actions)} actions; the limit is {self._max_actions}."
            )
        total_points = sum(
            action.kind in {"acquire_spectrum", "checkpoint"}
            or (
                action.kind in {"measure_moke_hall", "measure_lakeshore_field"}
                and bool(action.payload.get("checkpoint", True))
            )
            for action in actions
        )
        semantic_points = self._semantic_point_count()
        if semantic_points:
            total_points = max(total_points, semantic_points)
        total_spectra = sum(action.kind == "acquire_spectrum" for action in actions)
        required_devices = required_devices_for_actions(actions)
        safe_shutdown_actions = self._safe_shutdown_actions(required_devices)
        canonical = json.dumps(
            {
                "actions": [
                {
                    "node_id": item.node_id,
                    "kind": item.kind,
                    "payload": self._canonicalize(item.payload),
                    "setpoints": item.setpoints_si,
                    "is_finally": item.is_finally,
                    "semantic_id": item.semantic_id,
                    "source_node_id": item.source_node_id,
                    "axis_context": self._canonicalize(item.axis_context),
                }
                for item in actions
                ],
                "safe_shutdown_actions": safe_shutdown_actions,
                "recipe_dut_limits": self._canonicalize(recipe.dut_limits),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return ExecutionPlan(
            recipe.name,
            tuple(actions),
            total_points,
            digest,
            recipe.source_text,
            required_devices,
            total_spectra,
            safe_shutdown_actions,
            dict(recipe.dut_limits),
        )

    @staticmethod
    def _validate_reference_flow(actions: list[PlanAction]) -> None:
        reference_available = False
        for action in actions:
            if action.kind == "acquire_reference":
                reference_available = True
                continue
            if (
                action.kind == "acquire_spectrum"
                and action.payload.get("reference_operation", "none") != "none"
                and not reference_available
            ):
                raise ConfigurationError(
                    f"{action.node_id}: reference processing requires an earlier "
                    "acquire_reference action."
                )

    @staticmethod
    def _validate_device_state_flow(actions: list[PlanAction]) -> None:
        """Reject update/energisation sequences that cannot be valid at runtime."""

        configured: set[tuple[str, str]] = set()
        output_enabled: dict[tuple[str, str], bool] = {}
        for action in actions:
            if action.is_finally:
                continue
            kind = action.kind
            payload = action.payload
            key: tuple[str, str] | None = None
            if kind == "configure_keithley":
                key = ("keithley", str(payload["request"].channel))
            elif kind == "configure_rigol":
                key = ("rigol", str(payload["config"].channel))
            elif kind == "configure_anritsu_sg":
                key = ("anritsu_sg", "RF")
            if key is not None:
                configured.add(key)
                # Every full device configuration is specified to force and
                # confirm OUTPUT OFF before applying setpoints.
                output_enabled[key] = False
                continue

            if kind == "assert_output_on":
                assertion_key = (
                    str(payload.get("device", "")),
                    str(payload.get("channel", "")),
                )
                if assertion_key not in configured:
                    raise ConfigurationError(
                        f"{action.node_id}: continuous OUTPUT requires an earlier "
                        f"configuration for {assertion_key[0]} channel "
                        f"{assertion_key[1]}."
                    )
                if not output_enabled.get(assertion_key, False):
                    raise ConfigurationError(
                        f"{action.node_id}: continuous OUTPUT requires a previously "
                        "confirmed OUTPUT ON transition in this recipe."
                    )
                continue

            update_device: str | None = None
            update_channel: str | None = None
            if kind in {"update_keithley_level", "update_keithley_compliance"}:
                update_device = "keithley"
                update_channel = str(payload["channel"])
            elif kind in {"update_rigol_frequency", "update_rigol_levels"}:
                update_device = "rigol"
                update_channel = str(payload["channel"])
            elif kind == "update_anritsu_sg":
                update_device = "anritsu_sg"
                update_channel = "RF"
            if update_device is not None and update_channel is not None:
                update_key = (update_device, update_channel)
                if update_key not in configured:
                    raise ConfigurationError(
                        f"{action.node_id}: {kind} requires an earlier configuration "
                        f"for {update_device} channel {update_channel}."
                    )
                continue

            output_device: str | None = None
            output_channel: str | None = None
            if kind == "set_keithley_output":
                output_device, output_channel = "keithley", str(payload["channel"])
            elif kind == "set_rigol_output":
                output_device, output_channel = "rigol", str(payload["channel"])
            elif kind == "set_anritsu_sg_output":
                output_device, output_channel = "anritsu_sg", "RF"
            if output_device is None or output_channel is None:
                continue
            output_key = (output_device, output_channel)
            enabled = bool(payload["enabled"])
            if enabled:
                if output_key not in configured:
                    raise ConfigurationError(
                        f"{action.node_id}: {kind} requires an earlier configuration "
                        f"for {output_device} channel {output_channel}."
                    )
            output_enabled[output_key] = enabled

    @staticmethod
    def _append_output_continuity_assertion(
        actions: list[PlanAction],
        *,
        node_id: str,
        device: str,
        channel: str,
        context: dict[str, Quantity],
        expected_state: dict[str, Any] | None = None,
    ) -> None:
        """Make plan-owned OUTPUT continuity explicit and preflight-verifiable."""

        actions.append(
            PlanAction(
                f"{node_id}.assert-output-on",
                "assert_output_on",
                {
                    "device": device,
                    "channel": channel,
                    "expected_state": dict(expected_state or {}),
                },
                {name: quantity.si_value for name, quantity in context.items()},
            )
        )

    def _safe_shutdown_actions(self, required_devices: frozenset[str]) -> tuple[str, ...]:
        allowed = {
            "keithley.outputs_off": "keithley",
            "rigol.outputs_off": "rigol",
            "anritsu.abort_acquisition": "anritsu",
            "anritsu.rf_off_and_abort": "anritsu",
            "storage.flush_checkpoint": "storage",
        }
        configured = self._settings.execution.get("emergency_stop_order", ())
        if not isinstance(configured, (tuple, list)):
            raise ConfigurationError("execution.emergency_stop_order must be a list.")
        result: list[str] = []
        for value in configured:
            action = str(value)
            if action not in allowed:
                raise ConfigurationError(f"Unsupported emergency-stop action {action!r}.")
            if action == "anritsu.abort_acquisition":
                action = "anritsu.rf_off_and_abort"
            device = allowed[action]
            if device == "storage":
                continue
            if device in required_devices:
                if action not in result:
                    result.append(action)
        required_actions = {
            "keithley": "keithley.outputs_off",
            "rigol": "rigol.outputs_off",
            "anritsu": "anritsu.rf_off_and_abort",
        }
        # A normal measurement owns the complete station and therefore keeps
        # the station-wide shutdown invariant.  A dry run never enables an
        # output; it is intentionally scoped to the devices referenced by the
        # plan so an Anritsu-only simulation does not require unrelated source
        # sessions just to prove a state it is forbidden to energise.
        shutdown_devices = (
            tuple(device for device in ("keithley", "rigol", "anritsu") if device in required_devices)
            if self._outputs_forced_off
            else ("keithley", "rigol", "anritsu")
        )
        for device in shutdown_devices:
            action = required_actions[device]
            if action not in result:
                result.append(action)
        result.append("storage.flush_checkpoint")
        return tuple(result)

    @staticmethod
    def _action_value(action: dict[str, Any], node_id: str, parameter_id: str) -> Any:
        if "value" not in action:
            raise ConfigurationError(
                f"{node_id}: parameter action {parameter_id!r} requires a value."
            )
        return action["value"]

    def _apply_keithley_set_actions(
        self,
        configure_data: dict[str, Any],
        parameter_actions: list[dict[str, Any]],
        node_id: str,
    ) -> None:
        """Make explicit ``Set`` rows authoritative over the snapshot.

        The UI writes both a complete snapshot and the selected parameter rows.
        Applying the rows here keeps hand-authored recipes deterministic too;
        otherwise the visible action could be silently ignored by the compiler.
        Sweep rows are applied later because their first point is the initial
        configuration value.
        """

        target_for_parameter = {
            "source.level": "level",
            "source.compliance": "compliance",
            "measurement.nplc": "nplc",
            "measurement.settling_time": "settle_time",
            "measurement.sense_mode": "sense_mode",
            "source.range": "source_range",
            "measurement.voltage_range": "measure_voltage_range",
            "measurement.current_range": "measure_current_range",
        }
        for action in parameter_actions:
            if str(action.get("mode", "")) != "set":
                continue
            parameter_id = str(action.get("parameter_id", ""))
            target = target_for_parameter.get(parameter_id)
            if target is None:
                raise ConfigurationError(
                    f"{node_id}: unsupported Keithley parameter action {parameter_id!r}."
                )
            configure_data[target] = self._action_value(action, node_id, parameter_id)

    def _apply_rigol_set_actions(
        self,
        config_data: dict[str, Any],
        parameter_actions: list[dict[str, Any]],
        context: dict[str, Quantity],
        node_id: str,
    ) -> None:
        """Apply explicit Rigol ``Set`` rows to the canonical HighL/LowL pair."""

        for action in parameter_actions:
            if str(action.get("mode", "")) != "set":
                continue
            parameter_id = str(action.get("parameter_id", ""))
            value = self._action_value(action, node_id, parameter_id)
            if parameter_id == "carrier.frequency":
                config_data["frequency"] = value
            elif parameter_id == "carrier.high_level":
                config_data["high_level"] = value
            elif parameter_id == "carrier.low_level":
                config_data["low_level"] = value
            elif parameter_id in {"carrier.amplitude", "carrier.offset"}:
                high = self._resolve_quantity(
                    config_data["high_level"], DIMENSION_VOLTAGE, context
                ).si_value
                low = self._resolve_quantity(
                    config_data["low_level"], DIMENSION_VOLTAGE, context
                ).si_value
                amplitude = high - low
                offset = (high + low) / 2.0
                selected = self._resolve_quantity(value, DIMENSION_VOLTAGE, context).si_value
                if parameter_id == "carrier.amplitude":
                    amplitude = selected
                else:
                    offset = selected
                config_data["high_level"] = Quantity(
                    offset + amplitude / 2.0, DIMENSION_VOLTAGE
                )
                config_data["low_level"] = Quantity(
                    offset - amplitude / 2.0, DIMENSION_VOLTAGE
                )

    def _quantize_keithley_request(
        self, request: KeithleySourceRequest
    ) -> KeithleySourceRequest:
        """Return the exact setpoint representation used at the TSP boundary."""

        if request.mode == "measure_only":
            return request
        level_dimension = (
            DIMENSION_CURRENT if request.mode == "current" else DIMENSION_VOLTAGE
        )
        compliance_dimension = (
            DIMENSION_VOLTAGE if request.mode == "current" else DIMENSION_CURRENT
        )
        source_range = (
            request.source_range_si if not request.source_autorange else None
        )
        return replace(
            request,
            level_si=quantize_keithley_value(
                request.level_si,
                level_dimension,
                requested_range_si=source_range,
            ),
            compliance_si=quantize_keithley_value(
                request.compliance_si,
                compliance_dimension,
            ),
        )

    @staticmethod
    def _quantize_rigol_config(config: RigolChannelConfig) -> RigolChannelConfig:
        """Return the exact frequency/voltage representation sent by the adapter."""

        return replace(
            config,
            frequency_hz=quantize_rigol_frequency(config.frequency_hz),
            high_level_v=quantize_rigol_voltage(config.high_level_v),
            low_level_v=quantize_rigol_voltage(config.low_level_v),
        )

    def _rigol_sweep_signature(
        self, config: RigolChannelConfig, parameter: str | None
    ) -> float | tuple[float, float] | None:
        if parameter == "carrier.frequency":
            return quantize_rigol_frequency(config.frequency_hz)
        if parameter is None:
            return None
        applied = self._quantize_rigol_config(config)
        return applied.high_level_v, applied.low_level_v

    def _keithley_sweep_request(
        self,
        request: KeithleySourceRequest,
        parameter: str,
        value: Quantity,
    ) -> KeithleySourceRequest:
        candidate = request
        if parameter == "source.level":
            candidate = replace(candidate, level_si=value.si_value)
        elif parameter == "source.compliance":
            candidate = replace(candidate, compliance_si=value.si_value)
        elif parameter == "measurement.settling_time":
            candidate = replace(candidate, settle_time_s=value.si_value)
        else:
            raise ConfigurationError(
                f"Unsupported Keithley sweep parameter {parameter!r}."
            )
        channel_settings = self._settings.keithley.safety.channels[candidate.channel]
        validate_keithley_source(channel_settings, candidate)
        applied = self._quantize_keithley_request(candidate)
        validate_keithley_source(channel_settings, applied)
        return applied

    def _keithley_sweep_signature(
        self,
        request: KeithleySourceRequest,
        parameter: str,
        value: Quantity,
    ) -> float:
        applied = self._keithley_sweep_request(request, parameter, value)
        if parameter == "source.level":
            return applied.level_si
        if parameter == "source.compliance":
            return applied.compliance_si
        return applied.settle_time_s

    @staticmethod
    def _canonicalize(value: Any) -> Any:
        if is_dataclass(value):
            return RecipeCompiler._canonicalize(asdict(value))
        if isinstance(value, Mapping):
            return {str(key): RecipeCompiler._canonicalize(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [RecipeCompiler._canonicalize(item) for item in value]
        return value

    def _visit(
        self,
        node: RecipeNode,
        context: dict[str, Quantity],
        actions: list[PlanAction],
        *,
        is_finally: bool = False,
    ) -> None:
        self._check_cancelled()
        disabled = node.data.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ConfigurationError(
                f"{node.id}: disabled must be boolean true or false."
            )
        if disabled:
            if is_finally:
                raise SafetyViolation(
                    f"{node.id}: finally safety actions cannot be disabled."
                )
            return
        if len(actions) > self._max_actions:
            raise SafetyViolation("The expanded-action limit was exceeded.")
        if node.type == "sequence":
            device_module = node.data.get("device_module")
            if (
                device_module in {"keithley", "rigol", "anritsu", "anritsu_sg"}
                and node.data.get("operation") != "configure_selected_parameters"
            ):
                raise ConfigurationError(
                    f"{node.id}: device configuration is incomplete. "
                    "The node contains no configuration snapshot."
                )
            if node.data.get("operation") == "configure_selected_parameters":
                if node.data.get("device_module") == "keithley":
                    self._visit_keithley_device_node(
                        node, context, actions, is_finally=is_finally
                    )
                    return
                if node.data.get("device_module") == "rigol":
                    self._visit_rigol_device_node(
                        node, context, actions, is_finally=is_finally
                    )
                    return
                if node.data.get("device_module") == "anritsu":
                    self._visit_anritsu_device_node(
                        node, context, actions, is_finally=is_finally
                    )
                    return
                if node.data.get("device_module") == "anritsu_sg":
                    self._visit_anritsu_sg_device_node(
                        node, context, actions, is_finally=is_finally
                    )
                    return
                raise ConfigurationError(
                    f"{node.id}: DeviceNode provider compilation is not implemented "
                    f"for {node.data.get('device_module', 'unknown')!r}."
                )
            for child in node.children:
                self._visit(child, context, actions, is_finally=is_finally)
            return
        if node.type in {"enable_rigol_output", "enable_anritsu_sg_output"}:
            if is_finally:
                raise SafetyViolation("An OUTPUT ON action is not allowed in finally.")
            is_rigol = node.type == "enable_rigol_output"
            channel: int | None = None
            if is_rigol:
                try:
                    channel = int(node.data.get("channel", 0))
                except (TypeError, ValueError) as exc:
                    raise ConfigurationError(
                        f"{node.id}: Rigol OUTPUT ON requires channel 1 or 2."
                    ) from exc
                if channel not in {1, 2}:
                    raise ConfigurationError(
                        f"{node.id}: Rigol OUTPUT ON requires channel 1 or 2."
                    )
            # Authoring and execution both expose one deliberate OUTPUT ON
            # transition. Configuration, limits and readback remain separate
            # fail-closed gates; there is no intermediate output state.
            device = "rigol" if is_rigol else "anritsu-sg"
            output_kind = "set_rigol_output" if is_rigol else "set_anritsu_sg_output"
            data: dict[str, Any] = {"enabled": True}
            if channel is not None:
                data["channel"] = channel
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.{device}.output-on", output_kind, data
                    ),
                    context,
                    is_finally=False,
                )
            )
            return
        if node.type == "sweep":
            target = str(node.data["target"])
            try:
                dimension = SWEEP_DIMENSIONS[target]
            except KeyError as exc:
                allowed = ", ".join(sorted(SWEEP_DIMENSIONS))
                raise ConfigurationError(f"Unsupported sweep target {target!r}; allowed: {allowed}.") from exc
            for value in self._node_sweep_values(node, dimension, context):
                self._check_cancelled()
                nested = dict(context)
                nested[target] = value
                binding = self._semantic_axes_by_source.get(node.id)
                generated_kind: str | None = None
                if (
                    binding is not None
                    and not is_finally
                    and self._binding_configured(binding, actions)
                ):
                    point_index = next(
                        (
                            index
                            for index, point in enumerate(binding.points)
                            if math.isclose(point.si_value, value.si_value, rel_tol=0.0, abs_tol=1e-18)
                        ),
                        0,
                    )
                    point_action = self._semantic_axis_action(
                        node,
                        binding,
                        value,
                        nested,
                        point_index,
                        len(binding.points),
                        self._active_axis_path,
                    )
                    actions.append(point_action)
                    generated_kind = point_action.kind
                previous_path = self._active_axis_path
                if binding is not None:
                    self._active_axis_path = (*previous_path, binding.source_node_id)
                try:
                    for child in node.children:
                        if generated_kind and self._axis_update_matches(child, generated_kind):
                            continue
                        self._visit(child, nested, actions, is_finally=is_finally)
                finally:
                    self._active_axis_path = previous_path
            return
        if node.type == "configure_moke_box":
            raise ConfigurationError(
                f"{node.id}: MOKE field sweeps require a hardware-qualified field "
                "transfer function. The reconstructed protocol only confirms raw VOUT."
            )
        if node.type == "repeat":
            for index in range(int(node.data["count"])):
                self._check_cancelled()
                nested = dict(context)
                nested[f"repeat.{node.id}.index"] = Quantity(float(index), "dimensionless")
                for child in node.children:
                    self._visit(child, nested, actions, is_finally=is_finally)
            return
        if node.type == "if":
            selected = node.children if self._evaluate_condition(node, context) else node.else_children
            for child in selected:
                self._visit(child, context, actions, is_finally=is_finally)
            return
        if node.type == "comment":
            return
        action = self._compile_action(node, context, is_finally=is_finally)
        actions.append(action)
        if not is_finally:
            self._remember_literal_configuration(action, context)

    def _visit_keithley_device_node(
        self,
        node: RecipeNode,
        context: dict[str, Quantity],
        actions: list[PlanAction],
        *,
        is_finally: bool,
    ) -> None:
        """Compile one deterministic Keithley module and its optional source axis."""

        if is_finally:
            raise SafetyViolation("A Keithley device module is not allowed in finally.")
        configuration = node.data.get("configuration")
        if not isinstance(configuration, dict):
            raise ConfigurationError(
                f"{node.id}: Keithley provider requires a complete configuration snapshot. "
                "Open the node editor and apply the configuration again."
            )
        channel = str(configuration.get("channel", node.data.get("channel", "")))
        mode = str(configuration.get("source_mode", node.data.get("source_mode", "")))
        if channel not in {"A", "B"} or mode not in {
            "current",
            "voltage",
            "measure_only",
        }:
            raise ConfigurationError(
                f"{node.id}: invalid Keithley channel or source mode in snapshot."
            )
        raw_actions = node.data.get("parameter_actions", [])
        if not isinstance(raw_actions, list) or any(
            not isinstance(action, dict) for action in raw_actions
        ):
            raise ConfigurationError(f"{node.id}: parameter_actions must be a list.")
        parameter_actions = [dict(action) for action in raw_actions]
        allowed_parameters = {
            "source.level",
            "source.compliance",
            "measurement.nplc",
            "measurement.settling_time",
            "measurement.sense_mode",
            "source.range",
            "measurement.voltage_range",
            "measurement.current_range",
        }
        for action in parameter_actions:
            parameter_id = str(action.get("parameter_id", ""))
            action_mode = str(action.get("mode", ""))
            if parameter_id not in allowed_parameters or action_mode not in {"set", "sweep"}:
                raise ConfigurationError(
                    f"{node.id}: unsupported Keithley parameter action "
                    f"{parameter_id!r}/{action_mode!r}."
                )
            if mode == "measure_only" and parameter_id in {
                "source.level",
                "source.compliance",
            }:
                raise ConfigurationError(
                    f"{node.id}: measure_only cannot select {parameter_id}."
                )
        sweep_actions = [
            action for action in parameter_actions if action.get("mode") == "sweep"
        ]
        if len(sweep_actions) > 1:
            raise ConfigurationError(
                f"{node.id}: a Keithley module supports exactly one local sweep axis."
            )
        configure_data: dict[str, Any] = {
            "channel": channel,
            "mode": mode,
            "level": configuration.get("source_level"),
            "compliance": configuration.get("compliance"),
            "nplc": configuration.get("nplc", 1.0),
            "settle_time": configuration.get("settling_time", "0 s"),
            "sense_mode": configuration.get("sense_mode", "2wire"),
            "source_autorange": configuration.get("source_autorange", True),
            "source_range": configuration.get("source_range", "AUTO"),
            "measure_voltage_autorange": configuration.get(
                "measure_voltage_autorange", True
            ),
            "measure_voltage_range": configuration.get(
                "measure_voltage_range", "AUTO"
            ),
            "measure_current_autorange": configuration.get(
                "measure_current_autorange", True
            ),
            "measure_current_range": configuration.get(
                "measure_current_range", "AUTO"
            ),
        }
        self._apply_keithley_set_actions(configure_data, parameter_actions, node.id)
        sweep_values: tuple[Quantity, ...] = ()
        axis_target = f"keithley.{channel}.{mode}"
        sweep_parameter = (
            str(sweep_actions[0].get("parameter_id")) if sweep_actions else None
        )
        if sweep_actions:
            segments = sweep_actions[0].get("segments")
            if not isinstance(segments, list) or not segments:
                raise ConfigurationError(
                    f"{node.id}: {sweep_parameter} sweep requires a non-empty ROI."
                )
            if sweep_parameter == "source.level":
                if mode == "measure_only":
                    raise ConfigurationError(
                        f"{node.id}: measure_only cannot sweep source.level."
                    )
                dimension = (
                    DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
                )
                axis_target = f"keithley.{channel}.{mode}"
                configure_key = "level"
            elif sweep_parameter == "source.compliance":
                if mode == "measure_only":
                    raise ConfigurationError(
                        f"{node.id}: measure_only cannot sweep source.compliance."
                    )
                dimension = (
                    DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
                )
                compliance_kind = (
                    "compliance_voltage"
                    if mode == "current"
                    else "compliance_current"
                )
                axis_target = f"keithley.{channel}.{compliance_kind}"
                configure_key = "compliance"
            elif sweep_parameter == "measurement.settling_time":
                dimension = DIMENSION_TIME
                axis_target = f"keithley.{channel}.settling_time"
                configure_key = "settle_time"
            else:
                raise ConfigurationError(
                    f"{node.id}: {sweep_parameter!r} is fixed-only."
                )
            sweep_values = generate_sweep_points(segments, dimension)
            configure_data[configure_key] = sweep_values[0]

        output_policy = str(node.data.get("output_policy", "unchanged"))
        if output_policy not in {
            "unchanged",
            "on",
            "off",
            "on_keep",
            "continue",
        }:
            raise ConfigurationError(f"{node.id}: invalid Keithley output policy.")
        if output_policy == "continue":
            unsupported_live_actions = [
                str(action.get("parameter_id", ""))
                for action in parameter_actions
                if action.get("mode") == "set"
                or str(action.get("parameter_id", ""))
                not in {
                    "source.level",
                    "source.compliance",
                    "measurement.settling_time",
                }
            ]
            if unsupported_live_actions:
                raise ConfigurationError(
                    f"{node.id}: Continue confirmed OUTPUT ON accepts only a local "
                    "source-level, compliance, or settling-time sweep; fixed/full "
                    "configuration rows require OUTPUT OFF."
                )

        configure_node = RecipeNode(
            f"{node.id}.configure", "configure_keithley", configure_data
        )
        configure_context = dict(context)
        if sweep_values:
            configure_context[axis_target] = sweep_values[0]
        configure_action = self._compile_action(
            configure_node, configure_context, is_finally=False
        )
        configured_request = configure_action.payload["request"]
        applied_configured_request = self._quantize_keithley_request(
            configured_request
        )
        if output_policy == "continue":
            self._append_output_continuity_assertion(
                actions,
                node_id=node.id,
                device="keithley",
                channel=channel,
                context=context,
                expected_state={
                    "mode": applied_configured_request.mode,
                    "source_level_si": applied_configured_request.level_si,
                    "compliance_si": applied_configured_request.compliance_si,
                    "nplc": applied_configured_request.nplc,
                    "settle_time_s": applied_configured_request.settle_time_s,
                    "sense_mode": applied_configured_request.sense_mode,
                    "source_autorange": applied_configured_request.source_autorange,
                    "source_range_si": applied_configured_request.source_range_si,
                    "measure_voltage_autorange": applied_configured_request.measure_voltage_autorange,
                    "measure_voltage_range_si": applied_configured_request.measure_voltage_range_si,
                    "measure_current_autorange": applied_configured_request.measure_current_autorange,
                    "measure_current_range_si": applied_configured_request.measure_current_range_si,
                },
            )
        else:
            actions.append(configure_action)
            self._remember_literal_configuration(configure_action, context)
        if output_policy in {"on", "on_keep"}:
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-on",
                        "set_keithley_output",
                        {"channel": channel, "enabled": True},
                    ),
                    context,
                    is_finally=False,
                )
            )
        elif output_policy == "off":
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-off",
                        "set_keithley_output",
                        {"channel": channel, "enabled": False},
                    ),
                    context,
                    is_finally=False,
                )
            )

        previous_applied_signature: float | None = (
            self._keithley_sweep_signature(
                configured_request, sweep_parameter, sweep_values[0]
            )
            if sweep_values and sweep_parameter is not None
            else None
        )
        for point_index, value in enumerate(sweep_values or (None,)):
            self._check_cancelled()
            nested = dict(context)
            if value is not None:
                nested[axis_target] = value
                if output_policy == "continue" and point_index > 0:
                    self._append_output_continuity_assertion(
                        actions,
                        node_id=f"{node.id}.point",
                        device="keithley",
                        channel=channel,
                        context=nested,
                    )
                update_action: PlanAction | None = None
                if sweep_parameter == "source.level":
                    update_action = self._compile_action(
                        RecipeNode(
                            f"{node.id}.update-level",
                            "update_keithley_level",
                            {
                                "channel": channel,
                                "mode": mode,
                                "level": value,
                            },
                        ),
                        nested,
                        is_finally=False,
                    )
                elif sweep_parameter == "source.compliance":
                    point_config = dict(configure_data)
                    point_config["compliance"] = value
                    update_action = self._compile_action(
                        RecipeNode(
                            f"{node.id}.update-compliance",
                            "update_keithley_compliance",
                            point_config,
                        ),
                        nested,
                        is_finally=False,
                    )
                if update_action is not None:
                    applied_signature = self._keithley_sweep_signature(
                        configured_request, sweep_parameter, value
                    )
                    if (
                        point_index > 0
                        and applied_signature != previous_applied_signature
                    ):
                        actions.append(update_action)
                    previous_applied_signature = applied_signature
            settle_value = (
                value
                if sweep_parameter == "measurement.settling_time"
                and value is not None
                else self._resolve_quantity(
                    configure_data["settle_time"], DIMENSION_TIME, {}
                )
            )
            if settle_value.si_value > 0:
                actions.append(
                    self._compile_action(
                        RecipeNode(
                            f"{node.id}.settle",
                            "wait",
                            {"duration": settle_value},
                        ),
                        nested,
                        is_finally=False,
                    )
                )
            for child in node.children:
                self._visit(child, nested, actions, is_finally=False)

        if output_policy == "on":
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-off",
                        "set_keithley_output",
                        {"channel": channel, "enabled": False},
                    ),
                    context,
                    is_finally=False,
                )
            )

    def _visit_rigol_device_node(
        self,
        node: RecipeNode,
        context: dict[str, Quantity],
        actions: list[PlanAction],
        *,
        is_finally: bool,
    ) -> None:
        """Compile one deterministic Rigol carrier and one optional local axis."""

        if is_finally:
            raise SafetyViolation("A Rigol device module is not allowed in finally.")
        configuration = node.data.get("configuration")
        if not isinstance(configuration, dict):
            raise ConfigurationError(
                f"{node.id}: Rigol provider requires a complete configuration snapshot. "
                "Open the node editor and apply the configuration again."
            )
        try:
            channel = int(configuration.get("channel", node.data.get("channel")))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"{node.id}: invalid Rigol channel in snapshot."
            ) from exc
        waveform = str(configuration.get("waveform", "")).upper()
        config_data: dict[str, Any] = {
            "channel": channel,
            "waveform": waveform,
            "frequency": configuration.get("frequency"),
            "high_level": configuration.get("high_level"),
            "low_level": configuration.get("low_level"),
            "output_load": configuration.get("output_load", "HIGHZ"),
            "phase_deg": configuration.get("phase_deg", 0),
        }
        if waveform == "SQU":
            config_data["square_duty_percent"] = configuration.get(
                "square_duty_percent", 50
            )
        elif waveform == "RAMP":
            config_data["ramp_symmetry_percent"] = configuration.get(
                "ramp_symmetry_percent", 50
            )
        elif waveform == "PULS":
            config_data.update(
                {
                    "pulse_width": configuration.get("pulse_width", "100 us"),
                    "pulse_leading": configuration.get(
                        "pulse_leading", "10 ns"
                    ),
                    "pulse_trailing": configuration.get(
                        "pulse_trailing", "10 ns"
                    ),
                }
            )

        raw_actions = node.data.get("parameter_actions", [])
        if not isinstance(raw_actions, list) or any(
            not isinstance(action, dict) for action in raw_actions
        ):
            raise ConfigurationError(f"{node.id}: parameter_actions must be a list.")
        parameter_actions = [dict(action) for action in raw_actions]
        allowed = {
            "carrier.frequency": (
                "frequency",
                DIMENSION_FREQUENCY,
                f"rigol.{channel}.frequency",
            ),
            "carrier.high_level": (
                "high_level",
                DIMENSION_VOLTAGE,
                f"rigol.{channel}.high_level",
            ),
            "carrier.low_level": (
                "low_level",
                DIMENSION_VOLTAGE,
                f"rigol.{channel}.low_level",
            ),
            "carrier.amplitude": (
                "amplitude",
                DIMENSION_VOLTAGE,
                f"rigol.{channel}.amplitude",
            ),
            "carrier.offset": (
                "offset",
                DIMENSION_VOLTAGE,
                f"rigol.{channel}.offset",
            ),
        }
        for action in parameter_actions:
            parameter_id = str(action.get("parameter_id", ""))
            action_mode = str(action.get("mode", ""))
            if parameter_id not in allowed or action_mode not in {"set", "sweep"}:
                raise ConfigurationError(
                    f"{node.id}: unsupported Rigol parameter action "
                    f"{parameter_id!r}/{action_mode!r}."
                )
        self._apply_rigol_set_actions(config_data, parameter_actions, context, node.id)
        sweep_actions = [
            action for action in parameter_actions if action.get("mode") == "sweep"
        ]
        if len(sweep_actions) > 1:
            raise ConfigurationError(
                f"{node.id}: a Rigol module supports one local sweep axis."
            )
        sweep_values: tuple[Quantity, ...] = ()
        sweep_parameter: str | None = None
        axis_target: str | None = None
        if sweep_actions:
            sweep_parameter = str(sweep_actions[0]["parameter_id"])
            config_key, dimension, axis_target = allowed[sweep_parameter]
            if sweep_parameter == "carrier.frequency" and waveform in {"DC", "NOIS"}:
                raise ConfigurationError(
                    f"{node.id}: {waveform} has no carrier-frequency axis."
                )
            segments = sweep_actions[0].get("segments")
            if not isinstance(segments, list) or not segments:
                raise ConfigurationError(
                    f"{node.id}: {sweep_parameter} requires a non-empty ROI."
                )
            sweep_values = generate_sweep_points(segments, dimension)
            if sweep_parameter in {"carrier.amplitude", "carrier.offset"}:
                base_high = self._resolve_quantity(
                    config_data["high_level"], DIMENSION_VOLTAGE, context
                ).si_value
                base_low = self._resolve_quantity(
                    config_data["low_level"], DIMENSION_VOLTAGE, context
                ).si_value
                base_amplitude = base_high - base_low
                base_offset = (base_high + base_low) / 2.0
                initial = sweep_values[0].si_value
                amplitude = initial if sweep_parameter == "carrier.amplitude" else base_amplitude
                offset = initial if sweep_parameter == "carrier.offset" else base_offset
                config_data["high_level"] = Quantity(
                    offset + amplitude / 2.0, DIMENSION_VOLTAGE
                )
                config_data["low_level"] = Quantity(
                    offset - amplitude / 2.0, DIMENSION_VOLTAGE
                )
            else:
                config_data[config_key] = sweep_values[0]

        output_policy = str(node.data.get("output_policy", "unchanged"))
        if output_policy not in {
            "unchanged",
            "on",
            "off",
            "on_keep",
            "continue",
        }:
            raise ConfigurationError(f"{node.id}: invalid Rigol output policy.")

        configure_context = dict(context)
        if sweep_values and axis_target is not None:
            configure_context[axis_target] = sweep_values[0]
        configure_action = self._compile_action(
            RecipeNode(
                f"{node.id}.configure", "configure_rigol", config_data
            ),
            configure_context,
            is_finally=False,
        )
        output_path_action = self._compile_action(
            RecipeNode(
                f"{node.id}.configure-output-path",
                "configure_rigol_output",
                {
                    "channel": channel,
                    "output_load": configuration.get("output_load", "HIGHZ"),
                    "polarity": configuration.get("output_polarity", "NORM"),
                    "mode": configuration.get("output_mode", "NORM"),
                    "gate_polarity": configuration.get("gate_polarity", "NORM"),
                    "sync_enabled": configuration.get("sync_enabled", False),
                    "sync_polarity": configuration.get("sync_polarity", "NORM"),
                    "sync_delay": configuration.get("sync_delay", "0 s"),
                },
            ),
            configure_context,
            is_finally=False,
        )
        if output_policy == "continue":
            config = configure_action.payload["config"]
            applied_config = self._quantize_rigol_config(config)
            output_config = output_path_action.payload["config"]
            self._append_output_continuity_assertion(
                actions,
                node_id=node.id,
                device="rigol",
                channel=str(channel),
                context=context,
                expected_state={
                    "frequency_hz": applied_config.frequency_hz,
                    "high_level_v": applied_config.high_level_v,
                    "low_level_v": applied_config.low_level_v,
                    "output_load": applied_config.output_load,
                    "waveform": applied_config.waveform,
                    "phase_deg": applied_config.phase_deg,
                    "square_duty_percent": applied_config.square_duty_percent,
                    "ramp_symmetry_percent": applied_config.ramp_symmetry_percent,
                    "pulse_width_s": applied_config.pulse_width_s,
                    "pulse_leading_s": applied_config.pulse_leading_s,
                    "pulse_trailing_s": applied_config.pulse_trailing_s,
                    "output_path": {
                        "polarity": output_config.polarity,
                        "mode": output_config.mode,
                        "gate_polarity": output_config.gate_polarity,
                        "sync_enabled": output_config.sync_enabled,
                        "sync_polarity": output_config.sync_polarity,
                        "sync_delay_s": output_config.sync_delay_s,
                    },
                },
            )
        else:
            actions.append(configure_action)
            self._remember_literal_configuration(configure_action, context)
            actions.append(output_path_action)

        if output_policy in {"on", "on_keep"}:
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-on",
                        "set_rigol_output",
                        {"channel": channel, "enabled": True},
                    ),
                    context,
                    is_finally=False,
                )
            )
        elif output_policy == "off":
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-off",
                        "set_rigol_output",
                        {"channel": channel, "enabled": False},
                    ),
                    context,
                    is_finally=False,
                )
            )

        previous_rigol_signature = self._rigol_sweep_signature(
            configure_action.payload["config"], sweep_parameter
        )
        for point_index, value in enumerate(sweep_values or (None,)):
            self._check_cancelled()
            nested = dict(context)
            if value is not None and axis_target is not None:
                nested[axis_target] = value
                if output_policy == "continue" and point_index > 0:
                    self._append_output_continuity_assertion(
                        actions,
                        node_id=f"{node.id}.point",
                        device="rigol",
                        channel=str(channel),
                        context=nested,
                    )
                if sweep_parameter == "carrier.frequency":
                    update_node = RecipeNode(
                        f"{node.id}.update-frequency",
                        "update_rigol_frequency",
                        {"channel": channel, "frequency": value},
                    )
                else:
                    point_config = dict(config_data)
                    if sweep_parameter == "carrier.high_level":
                        point_config["high_level"] = value
                    elif sweep_parameter == "carrier.low_level":
                        point_config["low_level"] = value
                    elif sweep_parameter == "carrier.amplitude":
                        current_offset = (
                            float(configure_action.payload["config"].high_level_v)
                            + float(configure_action.payload["config"].low_level_v)
                        ) / 2.0
                        point_config["high_level"] = Quantity(
                            current_offset + value.si_value / 2.0,
                            DIMENSION_VOLTAGE,
                        )
                        point_config["low_level"] = Quantity(
                            current_offset - value.si_value / 2.0,
                            DIMENSION_VOLTAGE,
                        )
                    elif sweep_parameter == "carrier.offset":
                        current_amplitude = (
                            float(configure_action.payload["config"].high_level_v)
                            - float(configure_action.payload["config"].low_level_v)
                        )
                        point_config["high_level"] = Quantity(
                            value.si_value + current_amplitude / 2.0,
                            DIMENSION_VOLTAGE,
                        )
                        point_config["low_level"] = Quantity(
                            value.si_value - current_amplitude / 2.0,
                            DIMENSION_VOLTAGE,
                        )
                    update_node = RecipeNode(
                        f"{node.id}.update-levels",
                        "update_rigol_levels",
                        point_config,
                    )
                update_action = self._compile_action(
                    update_node, nested, is_finally=False
                )
                if sweep_parameter == "carrier.frequency":
                    applied_signature = quantize_rigol_frequency(
                        update_action.payload["frequency_hz"]
                    )
                else:
                    candidate_config = replace(
                        configure_action.payload["config"],
                        high_level_v=update_action.payload["high_level_v"],
                        low_level_v=update_action.payload["low_level_v"],
                    )
                    applied_signature = self._rigol_sweep_signature(
                        candidate_config, sweep_parameter
                    )
                if point_index > 0 and applied_signature != previous_rigol_signature:
                    actions.append(update_action)
                previous_rigol_signature = applied_signature
            for child in node.children:
                self._visit(child, nested, actions, is_finally=False)

        if output_policy == "on":
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-off",
                        "set_rigol_output",
                        {"channel": channel, "enabled": False},
                    ),
                    context,
                    is_finally=False,
                )
            )

    def _visit_anritsu_device_node(
        self,
        node: RecipeNode,
        context: dict[str, Quantity],
        actions: list[PlanAction],
        *,
        is_finally: bool,
    ) -> None:
        """Compile a spectrum snapshot and one optional analyser sweep axis."""

        if is_finally:
            raise SafetyViolation("An Anritsu device module is not allowed in finally.")
        configuration = node.data.get("configuration")
        if not isinstance(configuration, dict):
            raise ConfigurationError(
                f"{node.id}: Anritsu provider requires a complete configuration "
                "snapshot. Open the node editor and apply the configuration again."
            )
        base_data: dict[str, Any] = {
            "start_frequency": configuration.get("start_frequency"),
            "stop_frequency": configuration.get("stop_frequency"),
            "reference_level": configuration.get("reference_level"),
            "points": configuration.get("points"),
            "trace": node.data.get("trace", "TRAC1"),
        }
        if any(value is None for value in base_data.values()):
            raise ConfigurationError(
                f"{node.id}: incomplete Anritsu spectrum snapshot."
            )
        advanced_data: dict[str, Any] = {
            "rbw_mode": "auto",
            "vbw_mode": "auto",
            "detector": "NORM",
            "attenuation_mode": "auto",
            "preamplifier_enabled": False,
            "sweep_time_mode": "auto",
        }
        base_parameters = {
            "spectrum.start_frequency": (
                "start_frequency",
                DIMENSION_FREQUENCY,
                "anritsu.spectrum.start_frequency",
            ),
            "spectrum.stop_frequency": (
                "stop_frequency",
                DIMENSION_FREQUENCY,
                "anritsu.spectrum.stop_frequency",
            ),
            "spectrum.reference_level": (
                "reference_level",
                DIMENSION_DBM,
                "anritsu.spectrum.reference_level",
            ),
            "spectrum.points": ("points", None, None),
        }
        advanced_parameters = {
            "advanced.rbw_mode": "rbw_mode",
            "advanced.rbw": "rbw",
            "advanced.vbw_mode": "vbw_mode",
            "advanced.vbw": "vbw",
            "advanced.detector": "detector",
            "advanced.attenuation_mode": "attenuation_mode",
            "advanced.attenuation": "attenuation",
            "advanced.preamplifier_enabled": "preamplifier_enabled",
            "advanced.sweep_time_mode": "sweep_time_mode",
            "advanced.sweep_time": "sweep_time",
        }
        raw_actions = node.data.get("parameter_actions", [])
        if not isinstance(raw_actions, list) or any(
            not isinstance(action, dict) for action in raw_actions
        ):
            raise ConfigurationError(f"{node.id}: parameter_actions must be a list.")
        parameter_actions = [dict(action) for action in raw_actions]
        action_by_parameter = {
            str(action.get("parameter_id", "")): action
            for action in parameter_actions
        }
        for mode_parameter, value_parameter in (
            ("advanced.rbw_mode", "advanced.rbw"),
            ("advanced.vbw_mode", "advanced.vbw"),
            ("advanced.attenuation_mode", "advanced.attenuation"),
            ("advanced.sweep_time_mode", "advanced.sweep_time"),
        ):
            mode_action = action_by_parameter.get(mode_parameter)
            value_action = action_by_parameter.get(value_parameter)
            manual = (
                mode_action is not None
                and str(mode_action.get("mode")) == "set"
                and str(mode_action.get("value", "")).lower() == "manual"
            )
            if manual != (value_action is not None):
                raise ConfigurationError(
                    f"{node.id}: {mode_parameter}='manual' and {value_parameter} "
                    "must be selected together."
                )
        sweep_actions = [
            action for action in parameter_actions if action.get("mode") == "sweep"
        ]
        if len(sweep_actions) > 1:
            raise ConfigurationError(
                f"{node.id}: an Anritsu module supports one local sweep axis."
            )
        sweep_values: tuple[Quantity, ...] = ()
        sweep_parameter: str | None = None
        axis_target: str | None = None
        for action in parameter_actions:
            parameter_id = str(action.get("parameter_id", ""))
            mode = str(action.get("mode", ""))
            if mode not in {"set", "sweep"}:
                raise ConfigurationError(
                    f"{node.id}: unsupported Anritsu parameter mode {mode!r}."
                )
            if parameter_id in base_parameters:
                key, dimension, target = base_parameters[parameter_id]
                if mode == "sweep":
                    if dimension is None or target is None:
                        raise ConfigurationError(
                            f"{node.id}: {parameter_id!r} is fixed-only."
                        )
                    segments = action.get("segments")
                    if not isinstance(segments, list) or not segments:
                        raise ConfigurationError(
                            f"{node.id}: {parameter_id} requires a non-empty ROI."
                        )
                    sweep_values = generate_sweep_points(segments, dimension)
                    sweep_parameter = parameter_id
                    axis_target = target
                    base_data[key] = sweep_values[0]
                else:
                    base_data[key] = action.get("value")
                continue
            if parameter_id in advanced_parameters and mode == "set":
                advanced_data[advanced_parameters[parameter_id]] = action.get("value")
                continue
            raise ConfigurationError(
                f"{node.id}: unsupported Anritsu parameter action "
                f"{parameter_id!r}/{mode!r}."
            )

        for value in sweep_values or (None,):
            self._check_cancelled()
            nested = dict(context)
            point_base = dict(base_data)
            if value is not None and sweep_parameter is not None and axis_target is not None:
                key, _dimension, _target = base_parameters[sweep_parameter]
                point_base[key] = value
                nested[axis_target] = value
            configure_action = self._compile_action(
                RecipeNode(
                    f"{node.id}.configure-spectrum",
                    "configure_anritsu",
                    point_base,
                ),
                nested,
                is_finally=False,
            )
            actions.append(configure_action)
            self._remember_literal_configuration(configure_action, nested)
            if any(
                parameter_id in advanced_parameters
                for parameter_id in (
                    str(action.get("parameter_id", ""))
                    for action in parameter_actions
                )
            ):
                actions.append(
                    self._compile_action(
                        RecipeNode(
                            f"{node.id}.configure-advanced",
                            "configure_anritsu_advanced",
                            advanced_data,
                        ),
                        nested,
                        is_finally=False,
                    )
                )
            for child in node.children:
                self._visit(child, nested, actions, is_finally=False)

    def _visit_anritsu_sg_device_node(
        self,
        node: RecipeNode,
        context: dict[str, Quantity],
        actions: list[PlanAction],
        *,
        is_finally: bool,
    ) -> None:
        """Compile a complete Anritsu SG snapshot and one optional local axis.

        Full configuration always happens with RF disabled. The node's output
        policy may then enable RF for the block, keep plan-confirmed RF enabled,
        or continue an already confirmed live sweep without adopting external
        instrument state.
        """

        if is_finally:
            raise SafetyViolation(
                "An Anritsu signal-generator module is not allowed in finally."
            )
        configuration = node.data.get("configuration")
        if not isinstance(configuration, dict):
            raise ConfigurationError(
                f"{node.id}: Anritsu SG provider requires a complete configuration "
                "snapshot. Open the node editor and apply the configuration again."
            )
        point_data: dict[str, Any] = {
            "frequency": configuration.get("frequency"),
            "power": configuration.get("power"),
        }
        if any(value is None for value in point_data.values()):
            raise ConfigurationError(
                f"{node.id}: incomplete Anritsu signal-generator snapshot."
            )
        definitions = {
            "sg.frequency": (
                "frequency",
                DIMENSION_FREQUENCY,
                "anritsu.sg.frequency",
            ),
            "sg.power": ("power", DIMENSION_DBM, "anritsu.sg.power"),
        }
        raw_actions = node.data.get("parameter_actions", [])
        if not isinstance(raw_actions, list) or any(
            not isinstance(action, dict) for action in raw_actions
        ):
            raise ConfigurationError(f"{node.id}: parameter_actions must be a list.")
        parameter_actions = [dict(action) for action in raw_actions]
        sweeps = [
            action for action in parameter_actions if action.get("mode") == "sweep"
        ]
        if len(sweeps) > 1:
            raise ConfigurationError(
                f"{node.id}: an Anritsu SG module supports one local sweep axis."
            )
        sweep_values: tuple[Quantity, ...] = ()
        sweep_parameter: str | None = None
        axis_target: str | None = None
        for action in parameter_actions:
            parameter_id = str(action.get("parameter_id", ""))
            mode = str(action.get("mode", ""))
            if parameter_id not in definitions or mode not in {"set", "sweep"}:
                raise ConfigurationError(
                    f"{node.id}: unsupported Anritsu SG parameter action "
                    f"{parameter_id!r}/{mode!r}."
                )
            key, dimension, target = definitions[parameter_id]
            if mode == "set":
                point_data[key] = action.get("value")
                continue
            segments = action.get("segments")
            if not isinstance(segments, list) or not segments:
                raise ConfigurationError(
                    f"{node.id}: {parameter_id} requires a non-empty ROI."
                )
            sweep_values = generate_sweep_points(segments, dimension)
            sweep_parameter = parameter_id
            axis_target = target

        output_policy = str(node.data.get("output_policy", "unchanged"))
        if output_policy not in {
            "unchanged",
            "on",
            "off",
            "on_keep",
            "continue",
        }:
            raise ConfigurationError(f"{node.id}: invalid Anritsu SG output policy.")

        first_data = dict(point_data)
        first_context = dict(context)
        if sweep_values and sweep_parameter is not None and axis_target:
            first_key, _dimension, _target = definitions[sweep_parameter]
            first_data[first_key] = sweep_values[0]
            first_context[axis_target] = sweep_values[0]
        first_configure = self._compile_action(
            RecipeNode(
                f"{node.id}.configure-sg",
                "configure_anritsu_sg",
                first_data,
            ),
            first_context,
            is_finally=False,
        )
        first_config = first_configure.payload["config"]
        runtime_context = dict(context)
        if output_policy == "continue":
            self._append_output_continuity_assertion(
                actions,
                node_id=node.id,
                device="anritsu_sg",
                channel="RF",
                context=context,
                expected_state={
                    "frequency_hz": first_config.frequency_hz,
                    "power_dbm": first_config.power_dbm,
                },
            )
        else:
            actions.append(first_configure)
            self._remember_literal_configuration(first_configure, runtime_context)

        if output_policy in {"on", "on_keep"}:
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-on",
                        "set_anritsu_sg_output",
                        {"enabled": True},
                    ),
                    context,
                    is_finally=False,
                )
            )
        elif output_policy == "off":
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-off",
                        "set_anritsu_sg_output",
                        {"enabled": False},
                    ),
                    context,
                    is_finally=False,
                )
            )

        for point_index, value in enumerate(sweep_values or (None,)):
            self._check_cancelled()
            nested = dict(runtime_context)
            current = dict(point_data)
            if value is not None and sweep_parameter is not None and axis_target:
                key, _dimension, _target = definitions[sweep_parameter]
                current[key] = value
                nested[axis_target] = value
                if output_policy == "continue" and point_index > 0:
                    self._append_output_continuity_assertion(
                        actions,
                        node_id=f"{node.id}.point",
                        device="anritsu_sg",
                        channel="RF",
                        context=nested,
                    )
                if point_index > 0:
                    actions.append(
                        self._compile_action(
                            RecipeNode(
                                f"{node.id}.update-sg",
                                "update_anritsu_sg",
                                current,
                            ),
                            nested,
                            is_finally=False,
                        )
                    )
            for child in node.children:
                self._visit(child, nested, actions, is_finally=False)

        if output_policy == "on":
            actions.append(
                self._compile_action(
                    RecipeNode(
                        f"{node.id}.output-off",
                        "set_anritsu_sg_output",
                        {"enabled": False},
                    ),
                    context,
                    is_finally=False,
                )
            )

    @staticmethod
    def _remember_literal_configuration(
        action: PlanAction, context: dict[str, Quantity],
    ) -> None:
        """Carry literal device state into following checkpoint provenance.

        A fixed configuration is an operation, not a one-point sweep.  It must
        therefore not add an axis, while every later checkpoint still needs to
        state the fixed setpoint that was in force when its spectrum was made.
        """

        if action.kind == "configure_keithley":
            request = action.payload["request"]
            if request.mode in {"current", "voltage"}:
                dimension = (
                    DIMENSION_CURRENT if request.mode == "current" else DIMENSION_VOLTAGE
                )
                context[f"keithley.{request.channel}.{request.mode}"] = Quantity(
                    request.level_si, dimension
                )
            return
        if action.kind == "configure_rigol":
            config = action.payload["config"]
            prefix = f"rigol.{config.channel}"
            context[f"{prefix}.frequency"] = Quantity(config.frequency_hz, DIMENSION_FREQUENCY)
            context[f"{prefix}.high_level"] = Quantity(config.high_level_v, DIMENSION_VOLTAGE)
            context[f"{prefix}.low_level"] = Quantity(config.low_level_v, DIMENSION_VOLTAGE)
            return
        if action.kind == "configure_anritsu_sg":
            config = action.payload["config"]
            context["anritsu.sg.frequency"] = Quantity(config.frequency_hz, DIMENSION_FREQUENCY)
            context["anritsu.sg.power"] = Quantity(config.power_dbm, DIMENSION_DBM)

    @staticmethod
    def _sweep_values(start: Quantity, stop: Quantity, points: int, spacing: str) -> tuple[Quantity, ...]:
        if spacing == "linear":
            step = (stop.si_value - start.si_value) / (points - 1)
            return tuple(Quantity(start.si_value + index * step, start.dimension) for index in range(points))
        if start.si_value <= 0 or stop.si_value <= 0:
            raise ConfigurationError("A logarithmic sweep requires positive start and stop values.")
        ratio = (stop.si_value / start.si_value) ** (1 / (points - 1))
        return tuple(Quantity(start.si_value * ratio**index, start.dimension) for index in range(points))

    def _node_sweep_values(
        self,
        node: RecipeNode,
        dimension: str,
        context: dict[str, Quantity],
    ) -> tuple[Quantity, ...]:
        """Resolve legacy sweep fields or generator-produced segments."""

        segments = node.data.get("segments")
        if isinstance(segments, list):
            resolved: list[dict[str, Any]] = []
            for raw in segments:
                if not isinstance(raw, dict):
                    raise ConfigurationError(f"{node.id}: sweep segment must be a mapping.")
                segment = dict(raw)
                for key in ("start", "stop", "step", "value"):
                    if key in segment:
                        value = self._resolve_value(segment[key], context)
                        if isinstance(value, Quantity):
                            segment[key] = value
                resolved.append(segment)
            return generate_sweep_points(resolved, dimension)
        start = self._resolve_quantity(node.data["start"], dimension, context)
        stop = self._resolve_quantity(node.data["stop"], dimension, context)
        return self._sweep_values(
            start,
            stop,
            int(node.data["points"]),
            str(node.data.get("spacing", "linear")),
        )

    def _resolve_value(self, value: Any, context: dict[str, Quantity]) -> Any:
        if isinstance(value, str):
            match = _REFERENCE_RE.match(value)
            if match:
                try:
                    return context[match.group(1)]
                except KeyError as exc:
                    raise ConfigurationError(f"No sweep value is available for {value}.") from exc
        return value

    def _evaluate_condition(self, node: RecipeNode, context: dict[str, Quantity]) -> bool:
        if "condition" in node.data:
            return bool(node.data["condition"])
        left_raw = node.data["left"]
        match = _REFERENCE_RE.match(left_raw) if isinstance(left_raw, str) else None
        if match is None:
            raise ConfigurationError(f"{node.id}: if.left must be a sweep/repeat reference.")
        try:
            left = context[match.group(1)]
        except KeyError as exc:
            raise ConfigurationError(f"{node.id}: unresolved if reference {left_raw!r}.") from exc
        right_value = self._resolve_value(node.data["right"], context)
        right = parse_quantity(
            right_value,
            left.dimension,
            require_unit=left.dimension != "dimensionless",
        )
        operator = str(node.data["operator"])
        if operator == "<":
            return left.si_value < right.si_value
        if operator == "<=":
            return left.si_value <= right.si_value
        if operator == "==":
            return math.isclose(left.si_value, right.si_value, rel_tol=1e-12, abs_tol=0.0)
        if operator == "!=":
            return not math.isclose(left.si_value, right.si_value, rel_tol=1e-12, abs_tol=0.0)
        if operator == ">=":
            return left.si_value >= right.si_value
        if operator == ">":
            return left.si_value > right.si_value
        raise ConfigurationError(f"{node.id}: unsupported if operator {operator!r}.")

    def _resolve_quantity(self, value: Any, dimension: str, context: dict[str, Quantity]) -> Quantity:
        resolved = self._resolve_value(value, context)
        return parse_quantity(resolved, dimension, require_unit=not isinstance(resolved, Quantity))

    @staticmethod
    def _context_as_si(context: dict[str, Quantity]) -> dict[str, float]:
        return {name: value.si_value for name, value in context.items()}

    def _compile_action(
        self, node: RecipeNode, context: dict[str, Quantity], *, is_finally: bool
    ) -> PlanAction:
        data = {key: self._resolve_value(value, context) for key, value in node.data.items()}
        setpoints = self._context_as_si(context)
        action_kind = node.type
        if node.type == "configure_rigol":
            payload = self._compile_rigol(data)
        elif node.type == "configure_rigol_output":
            channel = int(data.get("channel", 0))
            if channel not in {1, 2}:
                raise ConfigurationError(
                    f"{node.id}: Rigol output path requires channel 1 or 2."
                )
            config = RigolOutputConfig(
                    channel=channel,
                    output_load=data.get("output_load", "HIGHZ"),
                    polarity=str(data.get("polarity", "NORM")).upper(),  # type: ignore[arg-type]
                    mode=str(data.get("mode", "NORM")).upper(),  # type: ignore[arg-type]
                    gate_polarity=str(
                        data.get("gate_polarity", "NORM")
                    ).upper(),  # type: ignore[arg-type]
                    sync_enabled=self._optional_boolean(
                        data, "sync_enabled", False, node.id
                    ),
                    sync_polarity=str(
                        data.get("sync_polarity", "NORM")
                    ).upper(),  # type: ignore[arg-type]
                    sync_delay_s=self._resolve_quantity(
                        data.get("sync_delay", "0 s"),
                        DIMENSION_TIME,
                        context,
                    ).si_value,
                )
            if config.polarity not in {"NORM", "INV"}:
                raise ConfigurationError(
                    f"{node.id}: Rigol output polarity must be NORM or INV."
                )
            if config.mode not in {"NORM", "GAT"}:
                raise ConfigurationError(
                    f"{node.id}: Rigol output mode must be NORM or GAT."
                )
            if config.gate_polarity not in {"NORM", "INV"}:
                raise ConfigurationError(
                    f"{node.id}: Rigol gate polarity must be NORM or INV."
                )
            if config.sync_polarity not in {"NORM", "INV"}:
                raise ConfigurationError(
                    f"{node.id}: Rigol SYNC polarity must be NORM or INV."
                )
            if not 0 <= config.sync_delay_s <= 10:
                raise SafetyViolation(
                    f"{node.id}: Rigol SYNC delay must be in the range 0..10 s."
                )
            payload = {"config": config}
        elif node.type == "configure_keithley":
            payload = self._compile_keithley(data, node.id)
        elif node.type == "configure_anritsu":
            payload = self._compile_anritsu(data)
        elif node.type == "configure_anritsu_advanced":
            payload = self._compile_anritsu_advanced(data, node.id)
        elif node.type == "configure_anritsu_sg":
            payload = self._compile_anritsu_signal_generator(data)
        elif node.type == "update_anritsu_sg":
            payload = self._compile_anritsu_signal_generator(data)
        elif node.type == "update_keithley_level":
            payload = self._compile_keithley_level_update(data, node.id)
        elif node.type == "update_keithley_compliance":
            request = self._compile_keithley(data, node.id)["request"]
            if request.mode == "measure_only":
                raise ConfigurationError(
                    f"{node.id}: measure_only has no source compliance."
                )
            payload = {
                "channel": request.channel,
                "mode": request.mode,
                "compliance_si": request.compliance_si,
            }
        elif node.type == "update_rigol_frequency":
            payload = self._compile_rigol_frequency_update(data, node.id)
        elif node.type == "update_rigol_levels":
            config = self._compile_rigol(data)["config"]
            payload = {
                "channel": config.channel,
                "high_level_v": config.high_level_v,
                "low_level_v": config.low_level_v,
            }
        elif node.type == "measure_keithley":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError(f"{node.id}: measure_keithley requires channel A or B.")
            payload = {"channel": channel}
        elif node.type == "measure_moke_hall":
            profile = self._settings.moke_box
            if not profile.enabled or not profile.protocol_qualified or not profile.endpoint:
                raise ConfigurationError(
                    f"{node.id}: MOKE Hall measurement requires an enabled, protocol-qualified TCP endpoint."
                )
            payload = {"checkpoint": bool(data.get("checkpoint", True))}
        elif node.type == "measure_lakeshore_field":
            profile = self._settings.lakeshore_gaussmeter
            if not profile.enabled or not profile.resource:
                raise ConfigurationError(
                    f"{node.id}: Lake Shore field measurement requires enabled=true and a VISA resource."
                )
            payload = {"checkpoint": bool(data.get("checkpoint", True))}
        elif node.type in {"acquire_reference", "acquire_spectrum"}:
            if self._settings.anritsu.acquisition.single_sweep_mode != "standard_scpi_opc":
                raise SafetyViolation(
                    f"{node.type} requires the qualified Anritsu standard_scpi_opc protocol."
                )
            payload = {
                "trace": validate_anritsu_trace_name(str(data.get("trace", "TRAC1"))),
            }
            try:
                average_count = int(data.get("average_count", 1))
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"{node.id}: average_count must be an integer."
                ) from exc
            if not 1 <= average_count <= 9999:
                raise SafetyViolation(
                    f"{node.id}: average_count must be in the range 1..9999."
                )
            payload["average_count"] = average_count
            if node.type == "acquire_spectrum":
                operation = str(data.get("reference_operation", "none")).strip().lower()
                if operation not in {
                    "none",
                    "difference_db",
                    "ratio_linear",
                    "add_power",
                    "subtract_power",
                    "multiply_linear",
                }:
                    raise ConfigurationError(
                        f"{node.id}: unsupported reference_operation {operation!r}."
                    )
                store_raw = self._optional_boolean(data, "store_raw", True, node.id)
                store_processed = self._optional_boolean(
                    data, "store_processed", operation != "none", node.id
                )
                if not store_raw:
                    raise ConfigurationError(
                        f"{node.id}: RAW spectrum storage is required for scientific "
                        "provenance and processed-spectrum grid identity."
                    )
                if store_processed and operation == "none":
                    raise ConfigurationError(
                        f"{node.id}: store_processed requires a reference_operation."
                    )
                payload.update(
                    {
                        "reference_operation": operation,
                        "store_raw": store_raw,
                        "store_processed": store_processed,
                    }
                )
        elif node.type == "checkpoint":
            payload = {"label": str(data.get("label", node.id))}
        elif node.type == "connect":
            payload = {"device": str(data["device"])}
            action_kind = "verify_connection"
        elif node.type == "wait":
            duration = self._resolve_quantity(data.get("duration"), DIMENSION_TIME, context).si_value
            if duration < 0 or duration > 3600:
                raise SafetyViolation("Wait duration must be in the range 0–3600 s.")
            payload = {"duration_s": duration}
        elif node.type == "set_rigol_output":
            channel = int(data.get("channel", 0))
            if channel not in {1, 2}:
                raise ConfigurationError("set_rigol_output requires channel 1 or 2.")
            enabled = self._require_boolean(data, "enabled", node.id)
            self._assert_output_action_allowed("rigol", enabled)
            payload = {"channel": channel, "enabled": enabled}
        elif node.type == "set_keithley_output":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("set_keithley_output requires channel A or B.")
            enabled = self._require_boolean(data, "enabled", node.id)
            self._assert_output_action_allowed("keithley", enabled)
            payload = {"channel": channel, "enabled": enabled}
        elif node.type == "ramp_keithley_to_zero":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("ramp_keithley_to_zero requires channel A or B.")
            deadline = self._resolve_quantity(data.get("deadline", "10 s"), DIMENSION_TIME, context).si_value
            if deadline <= 0 or deadline > 120:
                raise SafetyViolation("Keithley ramp deadline must be in the range (0, 120] s.")
            payload = {"channel": channel, "deadline_s": deadline}
        elif node.type == "set_anritsu_sg_output":
            enabled = self._require_boolean(data, "enabled", node.id)
            self._assert_output_action_allowed("anritsu_sg", enabled)
            payload = {"enabled": enabled}
        else:
            raise ConfigurationError(f"{node.id}: unsupported action type {node.type!r}.")
        if is_finally:
            safe_finally_actions = {
                "ramp_keithley_to_zero",
                "set_rigol_output",
                "set_keithley_output",
                "set_anritsu_sg_output",
            }
            if node.type not in safe_finally_actions:
                raise SafetyViolation("The finally section may contain only a Keithley ramp or output-off action.")
            if node.type in {
                "set_rigol_output",
                "set_keithley_output",
                "set_anritsu_sg_output",
            } and payload["enabled"]:
                raise SafetyViolation("The finally section cannot enable outputs.")
        semantic_id = (
            node.id
            if self._semantic_tree is not None and node.id in self._semantic_tree.by_id
            else None
        )
        return PlanAction(
            node.id,
            action_kind,
            payload,
            setpoints,
            is_finally=is_finally,
            semantic_id=semantic_id,
            source_node_id=node.id if semantic_id else None,
        )

    @staticmethod
    def _require_boolean(data: dict[str, Any], key: str, node_id: str) -> bool:
        value = data.get(key)
        if not isinstance(value, bool):
            raise ConfigurationError(f"{node_id}: {key} must be boolean true or false, not text.")
        return value

    @staticmethod
    def _optional_boolean(data: dict[str, Any], key: str, default: bool, node_id: str) -> bool:
        if key not in data:
            return default
        return RecipeCompiler._require_boolean(data, key, node_id)

    @staticmethod
    def _optional_quantity(data: dict[str, Any], key: str, dimension: str) -> float | None:
        value = data.get(key)
        if value is None or (isinstance(value, str) and value.strip().upper() == "AUTO"):
            return None
        return parse_quantity(value, dimension).si_value

    def _assert_output_action_allowed(self, device: str, enabled: bool) -> None:
        if not enabled or self._outputs_forced_off:
            return
        if device == "rigol":
            permitted = self._settings.rigol.safety.allow_output_enable
        elif device == "keithley":
            permitted = self._settings.keithley.safety.allow_output_enable
        elif device == "anritsu_sg":
            permitted = self._settings.anritsu.safety.signal_generator_output_allowed
        else:
            raise ConfigurationError(f"Unknown output device {device!r}.")
        if not permitted:
            raise SafetyViolation(
                f"The recipe cannot enable {device}: its output permission is disabled."
            )

    def _compile_anritsu_signal_generator(self, data: dict[str, Any]) -> dict[str, Any]:
        config = SignalGeneratorConfig(
            frequency_hz=self._resolve_quantity(
                data["frequency"], DIMENSION_FREQUENCY, {}
            ).si_value,
            power_dbm=self._resolve_quantity(data["power"], DIMENSION_DBM, {}).si_value,
        )
        validate_anritsu_signal_generator(
            self._settings.anritsu,
            frequency_hz=config.frequency_hz,
            power_dbm=config.power_dbm,
        )
        return {"config": config}

    def _compile_keithley_level_update(
        self, data: dict[str, Any], node_id: str
    ) -> dict[str, Any]:
        channel = str(data.get("channel", ""))
        mode = str(data.get("mode", "")).strip().lower()
        if channel not in {"A", "B"} or mode not in {"current", "voltage"}:
            raise ConfigurationError(
                f"{node_id}: update_keithley_level requires channel A/B and "
                "mode current/voltage."
            )
        dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        level = self._resolve_quantity(data.get("level"), dimension, {}).si_value
        channel_settings = self._settings.keithley.safety.channels[channel]
        profile_range = (
            channel_settings.lab_limits.source_current
            if mode == "current"
            else channel_settings.lab_limits.source_voltage
        )
        if profile_range.enabled:
            minimum = parse_quantity(profile_range.min, dimension).si_value
            maximum = parse_quantity(profile_range.max, dimension).si_value
            if not minimum <= level <= maximum:
                raise SafetyViolation(
                    f"{node_id}: Keithley {channel} {mode} level {level:g} SI is outside "
                    f"the station range [{minimum:g}, {maximum:g}]."
                )
        return {"channel": channel, "mode": mode, "level_si": level}

    def _compile_rigol_frequency_update(
        self, data: dict[str, Any], node_id: str
    ) -> dict[str, Any]:
        try:
            channel = int(data.get("channel", 0))
            channel_settings = self._settings.rigol.safety.channels[str(channel)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"{node_id}: update_rigol_frequency requires channel 1 or 2."
            ) from exc
        if channel not in {1, 2} or not channel_settings.enabled:
            raise SafetyViolation(f"{node_id}: Rigol CH{channel} is disabled.")
        frequency_hz = self._resolve_quantity(
            data.get("frequency"), DIMENSION_FREQUENCY, {}
        ).si_value
        limits = channel_settings.lab_limits.frequency
        if limits.enabled:
            minimum = parse_quantity(limits.min, DIMENSION_FREQUENCY).si_value
            maximum = parse_quantity(limits.max, DIMENSION_FREQUENCY).si_value
            if not minimum <= frequency_hz <= maximum:
                raise SafetyViolation(
                    f"{node_id}: Rigol CH{channel} frequency {frequency_hz:g} Hz is "
                    f"outside the station range [{minimum:g}, {maximum:g}] Hz."
                )
        return {"channel": channel, "frequency_hz": frequency_hz}

    def _compile_anritsu_advanced(
        self, data: dict[str, Any], node_id: str
    ) -> dict[str, Any]:
        rbw_mode = str(data.get("rbw_mode", "auto")).strip().lower()
        vbw_mode = str(data.get("vbw_mode", "auto")).strip().lower()
        attenuation_mode = str(data.get("attenuation_mode", "auto")).strip().lower()
        sweep_time_mode = str(data.get("sweep_time_mode", "auto")).strip().lower()
        for name, mode, allowed in (
            ("rbw_mode", rbw_mode, {"auto", "manual"}),
            ("vbw_mode", vbw_mode, {"auto", "manual", "off"}),
            ("attenuation_mode", attenuation_mode, {"auto", "manual"}),
            ("sweep_time_mode", sweep_time_mode, {"auto", "manual"}),
        ):
            if mode not in allowed:
                raise ConfigurationError(
                    f"{node_id}.{name} must be one of: {', '.join(sorted(allowed))}."
                )
        config = AdvancedSpectrumConfig(
            rbw_auto=rbw_mode == "auto",
            rbw_hz=(
                self._resolve_quantity(data.get("rbw"), DIMENSION_FREQUENCY, {}).si_value
                if rbw_mode == "manual"
                else None
            ),
            vbw_mode=vbw_mode,
            vbw_hz=(
                self._resolve_quantity(data.get("vbw"), DIMENSION_FREQUENCY, {}).si_value
                if vbw_mode == "manual"
                else None
            ),
            detector=str(data.get("detector", "NORM")).strip().upper(),
            attenuation_auto=attenuation_mode == "auto",
            attenuation_db=(
                self._resolve_quantity(data.get("attenuation"), DIMENSION_DB, {}).si_value
                if attenuation_mode == "manual"
                else None
            ),
            preamplifier_enabled=self._optional_boolean(
                data, "preamplifier_enabled", False, node_id
            ),
            sweep_time_auto=sweep_time_mode == "auto",
            sweep_time_s=(
                self._resolve_quantity(data.get("sweep_time"), DIMENSION_TIME, {}).si_value
                if sweep_time_mode == "manual"
                else None
            ),
        )
        validate_anritsu_advanced_spectrum(
            self._settings.anritsu,
            rbw_auto=config.rbw_auto,
            rbw_hz=config.rbw_hz,
            vbw_mode=config.vbw_mode,
            vbw_hz=config.vbw_hz,
            detector=config.detector,
            attenuation_auto=config.attenuation_auto,
            attenuation_db=config.attenuation_db,
            preamplifier_enabled=config.preamplifier_enabled,
            sweep_time_auto=config.sweep_time_auto,
            sweep_time_s=config.sweep_time_s,
            hardware_options=self._settings.anritsu.identity.required_options,
        )
        return {"config": config}

    def _compile_rigol(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            channel = int(data["channel"])
            settings = self._settings.rigol.safety.channels[str(channel)]
        except (KeyError, ValueError) as exc:
            raise ConfigurationError("configure_rigol requires valid channel 1 or 2.") from exc
        config = RigolChannelConfig(
            channel=channel,
            waveform=str(data["waveform"]).upper(),
            frequency_hz=self._resolve_quantity(data["frequency"], DIMENSION_FREQUENCY, {}).si_value,
            high_level_v=self._resolve_quantity(data["high_level"], DIMENSION_VOLTAGE, {}).si_value,
            low_level_v=self._resolve_quantity(data["low_level"], DIMENSION_VOLTAGE, {}).si_value,
            output_load=str(data.get("output_load", "HIGHZ")),
            phase_deg=float(data.get("phase_deg", 0.0)),
            square_duty_percent=float(data["square_duty_percent"]) if "square_duty_percent" in data else None,
            ramp_symmetry_percent=float(data["ramp_symmetry_percent"]) if "ramp_symmetry_percent" in data else None,
            pulse_width_s=self._resolve_quantity(data["pulse_width"], DIMENSION_TIME, {}) .si_value if "pulse_width" in data else None,
            pulse_leading_s=self._resolve_quantity(data["pulse_leading"], DIMENSION_TIME, {}).si_value if "pulse_leading" in data else None,
            pulse_trailing_s=self._resolve_quantity(data["pulse_trailing"], DIMENSION_TIME, {}).si_value if "pulse_trailing" in data else None,
        )
        validate_rigol_waveform(
            channel=settings,
            safety=self._settings.rigol.safety,
            waveform=config.waveform,
            frequency=config.frequency_hz,
            high_level=config.high_level_v,
            low_level=config.low_level_v,
            output_load=config.output_load,
        )
        applied = self._quantize_rigol_config(config)
        validate_rigol_waveform(
            channel=settings,
            safety=self._settings.rigol.safety,
            waveform=applied.waveform,
            frequency=applied.frequency_hz,
            high_level=applied.high_level_v,
            low_level=applied.low_level_v,
            output_load=applied.output_load,
        )
        return {"config": config}

    def _compile_keithley(self, data: dict[str, Any], node_id: str) -> dict[str, Any]:
        channel = str(data.get("channel", ""))
        mode = str(data.get("mode", ""))
        if channel not in {"A", "B"} or mode not in {"current", "voltage", "measure_only"}:
            raise ConfigurationError("configure_keithley requires channel A/B and mode current/voltage/measure_only.")
        dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        level = 0.0 if mode == "measure_only" else self._resolve_quantity(data.get("level"), dimension, {}).si_value
        compliance_dimension = DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
        compliance = 0.0 if mode == "measure_only" else self._resolve_quantity(data.get("compliance"), compliance_dimension, {}).si_value
        request = KeithleySourceRequest(
            channel=channel,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            level_si=level,
            compliance_si=compliance,
            nplc=float(data.get("nplc", 1.0)),
            settle_time_s=self._resolve_quantity(data.get("settle_time", "0 s"), DIMENSION_TIME, {}).si_value,
            sense_mode=str(data.get("sense_mode", "2wire")),  # type: ignore[arg-type]
            source_autorange=self._optional_boolean(data, "source_autorange", True, node_id),
            source_range_si=self._optional_quantity(data, "source_range", dimension),
            measure_voltage_autorange=self._optional_boolean(data, "measure_voltage_autorange", True, node_id),
            measure_voltage_range_si=self._optional_quantity(data, "measure_voltage_range", DIMENSION_VOLTAGE),
            measure_current_autorange=self._optional_boolean(data, "measure_current_autorange", True, node_id),
            measure_current_range_si=self._optional_quantity(data, "measure_current_range", DIMENSION_CURRENT),
        )
        validate_keithley_source(self._settings.keithley.safety.channels[channel], request)
        applied = self._quantize_keithley_request(request)
        validate_keithley_source(
            self._settings.keithley.safety.channels[channel], applied
        )
        return {"request": request}

    def _compile_anritsu(self, data: dict[str, Any]) -> dict[str, Any]:
        safety = self._settings.anritsu.safety
        config = SpectrumConfig(
            start_hz=self._resolve_quantity(data["start_frequency"], DIMENSION_FREQUENCY, {}).si_value,
            stop_hz=self._resolve_quantity(data["stop_frequency"], DIMENSION_FREQUENCY, {}).si_value,
            reference_level_dbm=self._resolve_quantity(data["reference_level"], DIMENSION_DBM, {}).si_value,
            points=int(data["points"]),
            trace=validate_anritsu_trace_name(str(data.get("trace", "TRAC1"))),
        )
        validate_anritsu_spectrum(
            safety,
            start_hz=config.start_hz,
            stop_hz=config.stop_hz,
            reference_level_dbm=config.reference_level_dbm,
            points=config.points,
        )
        return {"config": config}

