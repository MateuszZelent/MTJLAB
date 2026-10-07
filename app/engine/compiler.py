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
from app.domain.errors import ConfigurationError, ExecutionError, SafetyViolation
from app.domain.immutable import freeze_configuration
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
from app.recipes.models import Recipe, RecipeNode, validate_action_fields
from app.recipes.parameter_registry import SWEEP_DIMENSIONS
from app.recipes.semantic_tree import (
    AxisPointContext, SemanticMeasurementTree, axis_update_matches, normalize_recipe_tree,
)
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
from app.safety.moke_box import MokeVoltagePlan, control_profile_from_settings
from app.storage.moke_calibration_store import MokeCalibrationRepository


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
    completion_only: bool = False
    retained_output: str | None = None
    fault_only: bool = False


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
    elab_upload_config: dict[str, Any] | None = None
    total_control_points: int = 0

    @property
    def retained_outputs(self) -> frozenset[str]:
        return frozenset(action.retained_output for action in self.actions
                         if action.completion_only and action.retained_output is not None)

    def __post_init__(self) -> None:
        # Compilation builds mutable drafts; the executable plan owns a frozen
        # snapshot so later edits cannot alter the accepted values or hash.
        object.__setattr__(self, "actions", tuple(freeze_configuration(action) for action in self.actions))
        object.__setattr__(self, "recipe_dut_limits", freeze_configuration(self.recipe_dut_limits))
        object.__setattr__(self, "elab_upload_config", freeze_configuration(self.elab_upload_config))
        object.__setattr__(self, "required_devices", frozenset(self.required_devices))
        object.__setattr__(self, "safe_shutdown_actions", tuple(self.safe_shutdown_actions))


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
        if action.kind == "measure_moke_hall" or "moke" in action.kind:
            required.add("moke_box")
        if action.kind == "measure_lakeshore_field":
            required.add("lakeshore_gaussmeter")
        if action.kind == "verify_connection":
            required.add(str(action.payload["device"]))
        if action.kind == "assert_output_on":
            device = str(action.payload.get("device", ""))
            required.add("anritsu" if device == "anritsu_sg" else device)
    return frozenset(required)


def action_produces_checkpoint(action: PlanAction) -> bool:
    """One authoritative count for compilation, storage estimates and recovery."""
    return action.kind in {"acquire_spectrum", "checkpoint"} or (
        action.kind in {"measure_moke_hall", "measure_lakeshore_field"}
        and action.payload.get("checkpoint", True) is True
    )


def action_can_energize(action: PlanAction) -> bool:
    """Classify recipe mutations without confusing DAC control with an ON switch."""
    return action.kind == "update_moke_voltage" or (
        action.kind in {"set_rigol_output", "set_keithley_output", "set_anritsu_sg_output"}
        and bool(action.payload.get("enabled"))
    )


def controlled_output_endpoints(actions: Iterable[PlanAction]) -> frozenset[str]:
    """Physical outputs whose confirmed OFF state a resume must establish."""
    endpoints = set()
    for action in actions:
        payload = action.payload
        if "keithley" in action.kind:
            channel = getattr(payload.get("request"), "channel", payload.get("channel"))
            if channel in {"A", "B"}:
                endpoints.add(f"keithley.{channel}")
        elif "rigol" in action.kind:
            channel = getattr(payload.get("config"), "channel", payload.get("channel"))
            if channel in {1, 2}:
                endpoints.add(f"rigol.{channel}")
        elif "anritsu_sg" in action.kind or (
            action.kind == "assert_output_on" and payload.get("device") == "anritsu_sg"
        ):
            endpoints.add("anritsu.sg")
        elif action.kind in {"configure_moke_box", "arm_moke_voltage", "update_moke_voltage", "stop_moke_voltage"}:
            endpoints.add("moke_box.field")
    return frozenset(endpoints)


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
        self._max_points = settings.execution.get("max_expanded_points", 100_000)
        if type(self._max_points) is not int or self._max_points < 1:
            raise ConfigurationError("max_expanded_points must be a positive integer.")
        self._max_actions = settings.execution.get("max_expanded_actions", self._max_points * 10)
        if type(self._max_actions) is not int or self._max_actions < 1:
            raise ConfigurationError("max_expanded_actions must be a positive integer.")
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
        # Actions authored inside a semantic loop inherit the immutable
        # context of the current point.  This lets Runner/storage identify
        # the exact outer/inner axis combination without parsing node IDs.
        self._active_axis_context: AxisPointContext | None = None
        self._recipe_nodes: dict[str, RecipeNode] = {}
        self._planned_action_cursor = 0
        self._planned_context: dict[str, Quantity] = {}
        self._planned_keithley_requests: dict[str, KeithleySourceRequest] = {}
        self._planned_rigol_configs: dict[int, RigolChannelConfig] = {}
        self._planned_anritsu_mode: str | None = None

    def _sync_planned_state(self, actions: list[PlanAction]) -> None:
        """Track physical state across loop exits without rescanning the plan."""
        for action in actions[self._planned_action_cursor:]:
            self._remember_literal_configuration(action, self._planned_context)
            if action.kind == "configure_anritsu" and action.payload["config"].changed_fields is None:
                self._planned_anritsu_mode = "SPECTRUM"
            elif action.kind == "configure_anritsu_sg":
                self._planned_anritsu_mode = "SG"
            if action.kind == "configure_keithley":
                request = action.payload["request"]
                merged = self._merge_keithley_configuration(
                    self._planned_keithley_requests.get(request.channel), request
                )
                action.payload["request"] = replace(merged, changed_fields=request.changed_fields)
                self._planned_keithley_requests[request.channel] = merged
            elif action.kind in {"update_keithley_level", "update_keithley_compliance"}:
                channel = action.payload["channel"]
                previous = self._planned_keithley_requests.get(channel)
                if previous is not None:
                    field = "level_si" if action.kind == "update_keithley_level" else "compliance_si"
                    self._planned_keithley_requests[channel] = replace(previous, **{field: action.payload[field]})
            if action.kind == "configure_rigol":
                config = action.payload["config"]
                self._planned_rigol_configs[config.channel] = config
            elif action.kind in {"update_rigol_frequency", "update_rigol_levels"}:
                channel = action.payload["channel"]
                previous = self._planned_rigol_configs.get(channel)
                if previous is not None:
                    fields = ("frequency_hz",) if action.kind == "update_rigol_frequency" else ("high_level_v", "low_level_v")
                    self._planned_rigol_configs[channel] = replace(previous, **{field: action.payload[field] for field in fields})
        self._planned_action_cursor = len(actions)

    @staticmethod
    def _merge_keithley_configuration(previous, request):
        """Mirror adapter patch semantics instead of promoting defaults to settings."""
        if previous is None or request.changed_fields is None:
            return replace(request, changed_fields=None)
        merged = replace(previous, **{
            key: getattr(request, key) for key in request.changed_fields
        }, changed_fields=None)
        if merged.mode == "measure_only":
            merged = replace(
                merged, level_si=0.0, compliance_si=0.0,
                source_range_si=None, source_autorange=False,
            )
        return merged

    def _check_cancelled(self) -> None:
        if (
            self._cancellation_requested is not None
            and self._cancellation_requested()
        ):
            raise ConfigurationError("Recipe compilation cancelled.")

    def _check_expansion_budget(self, recipe: Recipe) -> None:
        """Bound repeats and checkpoint products before plan allocation."""
        def cost(node: RecipeNode) -> tuple[int, int]:
            self._check_cancelled()
            if node.data.get("disabled") is True:
                return 0, 0
            children = [cost(child) for child in node.children]
            alternative = [cost(child) for child in node.else_children]
            points = sum(item[0] for item in children)
            actions = sum(item[1] for item in children)
            if node.type == "if":
                points = max(points, sum(item[0] for item in alternative))
                actions = max(actions, sum(item[1] for item in alternative))
            multiplier = node.data["count"] if node.type == "repeat" else 1
            binding = self._semantic_axes_by_source.get(node.id)
            if binding is not None:
                multiplier *= len(binding.points)
                actions += 1
            if node.type not in {"sequence", "sweep", "repeat", "if"}:
                actions += 1
                if node.type in {"acquire_spectrum", "checkpoint"} or (
                    node.type in {"measure_moke_hall", "measure_lakeshore_field"}
                    and node.data.get("checkpoint", True) is True
                ):
                    points += 1
            points *= multiplier
            actions *= multiplier
            if points > self._max_points or actions > self._max_actions:
                raise SafetyViolation(f"{node.id}: expanded recipe exceeds its checkpoint/action budget before allocation.")
            return points, actions
        cost(recipe.root)

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

    def _binding_configured(self, binding, actions: list[PlanAction]) -> bool:
        self._sync_planned_state(actions)
        channel = str(binding.endpoint)
        if binding.device_module == "keithley":
            request = self._planned_keithley_requests.get(channel)
            tail = binding.target.rsplit(".", 1)[-1]
            mode = "current" if tail in {"level", "current", "compliance_voltage"} else "voltage"
            return request is not None and (tail == "settling_time" or request.mode == mode)
        if binding.device_module == "rigol":
            return int(channel) in self._planned_rigol_configs
        if binding.device_module == "anritsu":
            return self._planned_anritsu_mode == binding.endpoint.upper()
        if binding.device_module == "moke_box":
            for action in reversed(actions):
                if action.kind == "stop_moke_voltage":
                    return False
                if action.kind == "configure_moke_box":
                    return (not action.payload.get("automatic_sweep_preparation", False)
                            and f"vout{action.payload['profile'].channel}" == channel)
            return False
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
            mode = "current" if target_tail in {"level", "current", "compliance_voltage"} else "voltage"
            provider_node = RecipeNode(
                node.id,
                node.type,
                {**node.data, "channel": binding.endpoint, "source_mode": mode},
                node.children,
                node.else_children,
            )
        physical_context = {**context, **self._planned_context, binding.target: value}
        if binding.device_module == "keithley":
            request = self._planned_keithley_requests.get(binding.endpoint)
            if request is not None and request.source_range_si is not None:
                dimension = DIMENSION_CURRENT if request.mode == "current" else DIMENSION_VOLTAGE
                physical_context[f"keithley.{binding.endpoint}.source_range"] = Quantity(request.source_range_si, dimension)
        compiled = provider.compile_point(provider_node, binding, value, physical_context, self._settings)
        axis_context = AxisPointContext(
            binding.axis_id,
            point_index,
            point_count,
            binding.stage_index_at(point_index),
            value.si_value,
            {key: item.si_value for key, item in context.items()},
            (*active_axis_ids, binding.source_node_id),
        )
        payload = dict(compiled.payload)
        payload.setdefault("requested_si", compiled.requested_si)
        payload.setdefault("applied_si", compiled.applied_si)
        # Runtime confirmation must not infer the target when two active axes
        # happen to request the same numerical value.
        payload.setdefault("target", binding.target)
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
        self._check_cancelled()
        actions: list[PlanAction] = []
        self._reference_assets = {}
        self._recipe_nodes = {}
        self._active_axis_path = ()
        self._active_axis_context = None
        self._planned_action_cursor = 0
        self._planned_context = {}
        self._planned_keithley_requests = {}
        self._planned_rigol_configs = {}
        self._planned_anritsu_mode = None

        def index_recipe_node(node: RecipeNode) -> None:
            from app.recipes.models import validate_action_fields
            validate_action_fields(node.type, node.data, f"block {node.id}")
            self._recipe_nodes[node.id] = node
            for child in (*node.children, *node.else_children):
                index_recipe_node(child)

        index_recipe_node(recipe.root)
        for node in recipe.finally_nodes:
            index_recipe_node(node)
        self._semantic_tree = normalize_recipe_tree(
            recipe, self._device_registry.sweep_providers(), max_points=self._max_points,
            cancellation_requested=self._cancellation_requested,
        )
        self._semantic_axes_by_source = {
            node.source_node_id: node.axis
            for node in self._semantic_tree.by_id.values()
            if node.axis is not None and node.source_node_id is not None
        }
        self._check_expansion_budget(recipe)
        self._visit(recipe.root, {}, actions)
        completion_started = False
        for node in recipe.finally_nodes:
            if completion_started and node.type != "final_state":
                raise ConfigurationError("Put final-state blocks after all shutdown actions in Finally.")
            completion_started = completion_started or node.type == "final_state"
            self._visit(node, {}, actions, is_finally=True)
        final_endpoints = {action.payload["final_endpoint"] for action in actions if action.completion_only}
        for index, action in enumerate(actions):
            if not action.is_finally or action.completion_only:
                continue
            channel = action.payload.get("channel")
            endpoint = (f"keithley.{channel}" if action.kind in {"set_keithley_output", "ramp_keithley_to_zero"}
                        else f"rigol.{channel}" if action.kind == "set_rigol_output"
                        else f"moke_box.vout{channel}" if action.kind == "stop_moke_voltage" else None)
            overridden = endpoint in final_endpoints or (action.kind == "stop_moke_voltage" and channel is None
                           and any(endpoint.startswith("moke_box.") for endpoint in final_endpoints))
            if overridden:
                actions[index] = replace(action, fault_only=True)
        self._prepare_moke_trajectories(actions)
        self._validate_reference_flow(actions)
        self._validate_device_state_flow(actions)
        self._validate_keithley_range_flow(actions)
        if not actions:
            raise ConfigurationError("The recipe contains no executable actions.")
        if len(actions) > self._max_actions:
            raise SafetyViolation(
                f"The plan expands to {len(actions)} actions; the limit is {self._max_actions}."
            )
        total_points = sum(action_produces_checkpoint(action) for action in actions)
        semantic_points = self._semantic_point_count()
        if total_points > self._max_points:
            raise SafetyViolation(
                f"The plan expands to {total_points} points; the limit is {self._max_points}."
            )
        total_spectra = sum(action.kind == "acquire_spectrum" for action in actions)
        required_devices = required_devices_for_actions(actions)
        safe_shutdown_actions = self._safe_shutdown_actions(
            required_devices, anritsu_output="anritsu.sg" in controlled_output_endpoints(actions),
            moke_output=any(action.kind in {"arm_moke_voltage", "update_moke_voltage", "stop_moke_voltage"}
                                             for action in actions))
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
                    "completion_only": item.completion_only,
                    "retained_output": item.retained_output,
                    "fault_only": item.fault_only,
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
        elab_action = next(
            (action for action in actions if action.kind in {"upload_to_elab", "upload_elab"}),
            None,
        )
        elab_upload_config = dict(elab_action.payload) if elab_action is not None else None
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
            elab_upload_config,
            semantic_points,
        )

    @staticmethod
    def _prepare_moke_trajectories(actions: list[PlanAction]) -> None:
        """Bind every expanded voltage point to one immutable arm permission."""
        configured = None
        arm = None
        targets: list[float] = []
        target_actions: list[PlanAction] = []

        def finish():
            if configured is None:
                return
            if not targets or arm is None:
                raise ConfigurationError("MOKE voltage configuration requires arm_moke_voltage and voltage targets.")
            profile = configured.payload["profile"]
            plan = MokeVoltagePlan(profile.fingerprint, profile.channel,
                                   configured.payload["minimum_v"], configured.payload["maximum_v"], tuple(targets))
            plan.validate(profile)
            configured.payload["plan"] = plan
            arm.payload["plan"] = plan
            for target_action in target_actions:
                target_action.payload["applied_si"] = plan.applied_voltage(target_action.payload["voltage_v"])

        for action in actions:
            if action.kind == "configure_moke_box":
                finish()
                configured, arm, targets = action, None, []
                target_actions = []
            elif action.kind == "arm_moke_voltage":
                if configured is None or arm is not None:
                    raise ConfigurationError("MOKE arm requires exactly one preceding voltage configuration.")
                arm = action
            elif action.kind == "update_moke_voltage":
                if configured is None or arm is None:
                    raise ConfigurationError("MOKE voltage update requires configuration followed by explicit arm.")
                if action.payload["channel"] != configured.payload["profile"].channel:
                    raise SafetyViolation("MOKE voltage update channel differs from the configured physical binding.")
                action.payload["ramp_timeout_s"] = configured.payload["profile"].ramp_timeout_s
                targets.append(action.payload["voltage_v"])
                target_actions.append(action)
            elif action.kind == "stop_moke_voltage":
                finish()
                configured, arm, targets = None, None, []
                target_actions = []
        finish()

    def _validate_keithley_range_flow(self, actions: list[PlanAction]) -> None:
        """Check expanded legacy level updates against each channel's fixed range."""
        configured: dict[str, KeithleySourceRequest] = {}
        for action in actions:
            if action.kind == "configure_keithley":
                request = action.payload["request"]
                merged = self._merge_keithley_configuration(configured.get(request.channel), request)
                validate_keithley_source(self._settings.keithley.safety.channels[request.channel], merged)
                action.payload["request"] = replace(merged, changed_fields=request.changed_fields)
                configured[request.channel] = merged
            elif action.kind == "update_keithley_level":
                channel = action.payload["channel"]
                request = configured.get(channel)
                if request is not None:
                    raw = replace(request, level_si=action.payload["level_si"])
                    validate_keithley_source(
                        self._settings.keithley.safety.channels[channel],
                        raw,
                    )
                    applied = self._quantize_keithley_request(raw)
                    validate_keithley_source(self._settings.keithley.safety.channels[channel], applied)
                    action.payload["level_si"] = applied.level_si
                    if "applied_si" in action.payload:
                        action.payload["applied_si"] = applied.level_si
                    configured[channel] = applied
            elif action.kind == "update_keithley_compliance":
                channel = action.payload["channel"]
                request = configured.get(channel)
                if request is not None:
                    raw = replace(request, compliance_si=action.payload["compliance_si"])
                    validate_keithley_source(self._settings.keithley.safety.channels[channel], raw)
                    applied = self._quantize_keithley_request(raw)
                    validate_keithley_source(self._settings.keithley.safety.channels[channel], applied)
                    action.payload["compliance_si"] = applied.compliance_si
                    if "applied_si" in action.payload:
                        action.payload["applied_si"] = applied.compliance_si
                    configured[channel] = applied

    @staticmethod
    def _validate_reference_flow(actions: list[PlanAction]) -> None:
        from app.recipes.spectrum_processing import REFERENCE_UNITS

        reference_available = False
        configurations = {}
        processed_unit = None
        for action in actions:
            if action.kind in {"configure_anritsu", "configure_anritsu_advanced"}:
                config = action.payload["config"]
                if configurations.get(action.kind) != config:
                    reference_available = False
                configurations[action.kind] = config
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
                    "acquire_reference action after the last analyzer settings change."
                )
            if action.kind == "acquire_spectrum" and action.payload.get("store_processed"):
                operation = action.payload.get("reference_operation", "none")
                unit = REFERENCE_UNITS[operation]
                if processed_unit is not None and unit != processed_unit:
                    raise ConfigurationError(
                        f"{action.node_id}: processed spectrum units cannot change within one run "
                        f"({processed_unit} to {unit}); use separate recipes for different physical quantities."
                    )
                processed_unit = unit

    @staticmethod
    def _validate_device_state_flow(actions: list[PlanAction]) -> None:
        """Reject update/energisation sequences that cannot be valid at runtime."""

        configured: set[tuple[str, str]] = set()
        output_enabled: dict[tuple[str, str], bool] = {}
        anritsu_mode: str | None = None
        for action in actions:
            if action.fault_only:
                continue
            kind = action.kind
            payload = action.payload
            if kind == "configure_anritsu" and payload["config"].changed_fields is None:
                if output_enabled.get(("anritsu_sg", "RF"), False):
                    raise ConfigurationError(f"{action.node_id}: turn SG RF OFF explicitly before selecting Spectrum Analyzer mode.")
                anritsu_mode = "spectrum"
                configured.discard(("anritsu_sg", "RF"))
            elif kind == "configure_anritsu_sg":
                anritsu_mode = "sg"
            elif kind in {"acquire_reference", "acquire_spectrum"} and not payload.get("source_file") and anritsu_mode == "sg":
                raise ConfigurationError(f"{action.node_id}: acquisition requires an explicit Spectrum Analyzer configuration after SG mode; it cannot silently switch SG OFF.")
            key: tuple[str, str] | None = None
            if kind == "configure_keithley":
                key = ("keithley", str(payload["request"].channel))
            elif kind == "configure_rigol":
                key = ("rigol", str(payload["config"].channel))
            elif kind == "configure_anritsu_sg":
                key = ("anritsu_sg", "RF")
            if key is not None:
                if kind == "configure_keithley" and payload["request"].mode == "measure_only":
                    # Measurement setup preserves the hardware source registers;
                    # it is not evidence of a validated source for OUTPUT ON.
                    configured.discard(key)
                else:
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

    def _safe_shutdown_actions(self, required_devices: frozenset[str], *, moke_output: bool = False,
                               anritsu_output: bool = False) -> tuple[str, ...]:
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
            if action.startswith("anritsu."):
                if not anritsu_output:
                    # A receiver owns no energy output; normal completion keeps
                    # its front-panel acquisition running. Fault abort is separate.
                    continue
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
            "anritsu": "anritsu.rf_off_and_abort" if anritsu_output else "anritsu.abort_acquisition",
        }
        if moke_output:
            result.append("moke_box.dac_zero_or_unknown")
        # Every execution mode owns only the devices declared by its actions.
        # The separate station E-STOP remains explicitly global.
        shutdown_devices = tuple(
            device for device in ("keithley", "rigol", "anritsu")
            if device in required_devices and (device != "anritsu" or anritsu_output)
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

    def _visit_final_state(self, node, context, actions) -> None:
        """Compile success-only endpoints through existing validated operations."""
        self._sync_planned_state(actions)
        data = dict(node.data)
        device, channel, output = data.get("device"), data.get("channel"), data.get("output")
        fields = {"moke_box": {"voltage"}, "keithley": {"level"},
                  "rigol": {"frequency", "high_level", "low_level"}}
        if not isinstance(device, str) or device not in fields or not isinstance(output, str) or output not in {"off", "hold"}:
            raise ConfigurationError("Final state requires MOKE/Keithley/Rigol and output off or hold.")
        extra = set(data) - fields[device] - {"device", "channel", "output", "label", "description", "disabled"}
        if extra:
            raise ConfigurationError(f"{node.id}: fields do not belong to {device}: {sorted(extra)}.")
        if (device == "keithley" and (not isinstance(channel, str) or channel not in {"A", "B"})) or (
            device != "keithley" and (type(channel) is not int or channel not in (range(8) if device == "moke_box" else (1, 2)))
        ):
            raise ConfigurationError("Final state requires a valid explicit output channel.")
        endpoint = f"{device}.{'vout' if device == 'moke_box' else ''}{channel}"
        if any(action.source_node_id != node.id and action.completion_only
               and action.payload.get("final_endpoint") == endpoint for action in actions):
            raise ConfigurationError(f"Duplicate final state for {endpoint}.")
        operations = []
        if device == "moke_box":
            if not any(action.kind == "configure_moke_box" and action.payload["profile"].channel == channel
                       and not action.completion_only for action in actions):
                raise ConfigurationError("MOKE final state requires this VOUT in the main measurement plan.")
            if output == "off":
                if "voltage" in data and parse_quantity(data["voltage"], DIMENSION_VOLTAGE).si_value != 0:
                    raise ConfigurationError("MOKE off uses the qualified zero target; choose hold for nonzero voltage.")
                operations.append(("stop_moke_voltage", {"channel": channel}))
            else:
                operations.append(("set_moke_voltage", {"channel": channel, "voltage": data.get("voltage")}))
        elif device == "keithley":
            request = self._planned_keithley_requests.get(channel)
            if request is None or request.mode == "measure_only":
                raise ConfigurationError("Keithley final state requires a source configuration in the main plan.")
            if "level" in data:
                operations.append(("update_keithley_level", {"channel": channel, "mode": request.mode, "level": data["level"]}))
            if output == "off":
                operations.append(("set_keithley_output", {"channel": channel, "enabled": False}))
        else:
            if channel not in self._planned_rigol_configs:
                raise ConfigurationError("Rigol final state requires a carrier configuration in the main plan.")
            if "frequency" in data:
                operations.append(("update_rigol_frequency", {"channel": channel, "frequency": data["frequency"]}))
            if {"high_level", "low_level"}.intersection(data):
                if not {"high_level", "low_level"}.issubset(data):
                    raise ConfigurationError("Rigol final voltage requires both high_level and low_level.")
                operations.append(("update_rigol_levels", {key: data[key] for key in ("channel", "high_level", "low_level")}))
            if output == "off":
                operations.append(("set_rigol_output", {"channel": channel, "enabled": False}))
        start = len(actions)
        for index, (kind, values) in enumerate(operations):
            self._visit(RecipeNode(f"{node.id}.{index}", kind, values), context, actions)
        if output == "hold" and device != "moke_box":
            self._append_output_continuity_assertion(actions, node_id=node.id, device=device,
                                                    channel=str(channel), context=context)
        for index in range(start, len(actions)):
            action = actions[index]
            actions[index] = replace(action, is_finally=True, completion_only=True,
                semantic_id=node.id, source_node_id=node.id,
                payload={**action.payload, "final_endpoint": endpoint},
                retained_output=endpoint if output == "hold" and index == len(actions) - 1 else None)

    def _visit(
        self,
        node: RecipeNode,
        context: dict[str, Quantity],
        actions: list[PlanAction],
        *,
        is_finally: bool = False,
    ) -> None:
        self._check_cancelled()
        validate_action_fields(node.type, node.data, node.id)
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
        if node.type == "final_state":
            if not is_finally:
                raise ConfigurationError("Final state belongs in the Finally branch.")
            self._visit_final_state(node, context, actions)
            return
        if node.type == "sequence":
            device_module = node.data.get("device_module")
            if device_module == "moke_box" and "channel" in node.data:
                declared_channel = node.data["channel"]
                if type(declared_channel) is not int or declared_channel not in range(8):
                    raise ConfigurationError(f"{node.id}: MOKE device channel must be 0..7.")
                for child in node.children:
                    child_channel = (child.data.get("channel") if child.type in {"set_moke_voltage", "configure_moke_box", "update_moke_voltage"}
                        else int(str(child.data["target"]).split(".")[1].removeprefix("vout"))
                        if child.type == "sweep" and str(child.data.get("target", "")).startswith("moke_box.vout") else None)
                    if child_channel is not None and child_channel != declared_channel:
                        raise ConfigurationError(f"{node.id}: MOKE device channel differs from its operation channel.")
            if device_module == "moke_box" and (
                node.data.get("configuration_required")
                or not any(child.type in {"configure_moke_box", "set_moke_voltage"}
                    or (child.type == "sweep" and str(child.data.get("target", "")).startswith("moke_box."))
                    for child in node.children)
            ):
                raise ConfigurationError(f"{node.id}: MOKE Box configuration is incomplete.")
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
        if node.type == "set_moke_voltage":
            if is_finally:
                raise SafetyViolation("Use MOKE return-to-zero cleanup in finally.")
            channel = node.data.get("channel")
            simulation = bool((self._settings.moke_box.endpoint or "").startswith("SIM::MOKE"))
            profile = control_profile_from_settings(self._settings, simulation=simulation, channel=channel)
            voltage = parse_quantity(node.data.get("voltage"), DIMENSION_VOLTAGE).si_value
            MokeVoltagePlan(profile.fingerprint, channel, profile.minimum_v, profile.maximum_v, (voltage,)).validate(profile)
            operations = [
                ("prepare", "configure_moke_box", {"channel": channel,
                    "minimum_voltage": Quantity(profile.minimum_v, DIMENSION_VOLTAGE),
                    "maximum_voltage": Quantity(profile.maximum_v, DIMENSION_VOLTAGE)}),
                ("arm", "arm_moke_voltage", {}),
                ("apply", "update_moke_voltage", {"channel": channel, "voltage": node.data["voltage"]}),
            ]
            for suffix, kind, data in operations:
                action = self._compile_action(RecipeNode(f"{node.id}.{suffix}", kind, data), context, is_finally=False)
                if kind == "configure_moke_box":
                    action.payload["automatic_sweep_preparation"] = True
                actions.append(replace(action, source_node_id=node.id, semantic_id=node.id))
            return
        if node.type == "sweep":
            target = str(node.data["target"])
            try:
                dimension = SWEEP_DIMENSIONS[target]
            except KeyError as exc:
                allowed = ", ".join(sorted(SWEEP_DIMENSIONS))
                raise ConfigurationError(f"Unsupported sweep target {target!r}; allowed: {allowed}.") from exc
            binding = self._semantic_axes_by_source.get(node.id)
            configured = binding is not None and self._binding_configured(binding, actions)
            if binding is not None and binding.device_module == "moke_box" and not configured:
                if is_finally:
                    raise SafetyViolation("MOKE voltage sweeps cannot be cleanup actions.")
                values = tuple(self._node_sweep_values(node, dimension, context))
                if not values:
                    raise ConfigurationError(f"{node.id}: MOKE sweep has no voltage points.")
                channel = int(binding.endpoint.removeprefix("vout"))
                simulation = bool((self._settings.moke_box.endpoint or "").startswith("SIM::MOKE"))
                profile = control_profile_from_settings(self._settings, simulation=simulation, channel=channel)
                minimum = min(value.si_value for value in values)
                maximum = max(value.si_value for value in values)
                if minimum == maximum:
                    minimum, maximum = profile.minimum_v, profile.maximum_v
                preparation = self._compile_action(RecipeNode(
                    f"{node.id}.prepare", "configure_moke_box", {
                        "channel": channel,
                        "minimum_voltage": Quantity(minimum, DIMENSION_VOLTAGE),
                        "maximum_voltage": Quantity(maximum, DIMENSION_VOLTAGE),
                    }), context, is_finally=False)
                preparation.payload["automatic_sweep_preparation"] = True
                actions.append(replace(preparation, source_node_id=node.id, semantic_id=binding.axis_id))
                arming = self._compile_action(
                    RecipeNode(f"{node.id}.arm", "arm_moke_voltage", {}), context, is_finally=False)
                actions.append(replace(arming, source_node_id=node.id, semantic_id=binding.axis_id))
                configured = True
            authored_configuration = False
            if binding is not None:
                reference = "${" + binding.target + "}"
                configuration_kind = {
                    "keithley": "configure_keithley", "rigol": "configure_rigol",
                    "anritsu": "configure_anritsu_sg" if binding.endpoint == "SG" else "configure_anritsu",
                }.get(binding.device_module)
                field_name = {
                    "level": "level", "current": "level", "voltage": "level", "compliance_current": "compliance",
                    "compliance_voltage": "compliance", "power": "power",
                }.get(binding.target.rsplit(".", 1)[-1], binding.target.rsplit(".", 1)[-1])
                authored_configuration = any(
                    child.type == configuration_kind and child.data.get(field_name) == reference
                    and (binding.device_module == "anritsu" or str(child.data.get("channel")) == binding.endpoint)
                    for child in node.children
                )
                if not configured and not authored_configuration and not binding.target.endswith("settling_time"):
                    raise ConfigurationError(
                        f"{node.id}: physical sweep {target!r} requires explicit device configuration before its points."
                    )
            for point_index, value in enumerate(self._node_sweep_values(node, dimension, context)):
                self._check_cancelled()
                nested = dict(context)
                nested[target] = value
                binding = self._semantic_axes_by_source.get(node.id)
                generated_kind: str | None = None
                if (
                    binding is not None
                    and not is_finally
                    and not authored_configuration
                    and (configured or binding.target.endswith("settling_time"))
                ):
                    self._sync_planned_state(actions)
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
                previous_context = self._active_axis_context
                if binding is not None and generated_kind:
                    self._active_axis_path = (*previous_path, binding.source_node_id)
                    self._active_axis_context = point_action.axis_context
                try:
                    for child in node.children:
                        if generated_kind and axis_update_matches(child, binding, generated_kind):
                            continue
                        self._visit(child, nested, actions, is_finally=is_finally)
                finally:
                    self._active_axis_path = previous_path
                    self._active_axis_context = previous_context
            return
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
        self._sync_planned_state(actions)
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
            "source_autorange": configuration.get("source_autorange", False),
            "source_range": configuration.get("source_range"),
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
        self._sync_planned_state(actions)
        baseline = self._planned_keithley_requests.get(channel)
        if baseline is None or baseline.mode != mode:
            raise ConfigurationError(
                f"{node.id}: selected Keithley parameters require an explicit "
                f"configure_keithley baseline for channel {channel} in {mode} mode."
            )
        level_dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        compliance_dimension = DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
        configure_data.update({
            "level": Quantity(baseline.level_si, level_dimension),
            "compliance": Quantity(baseline.compliance_si, compliance_dimension),
            "nplc": baseline.nplc,
            "settle_time": Quantity(baseline.settle_time_s, DIMENSION_TIME),
            "sense_mode": baseline.sense_mode,
            "source_autorange": baseline.source_autorange,
            "source_range": None if baseline.source_range_si is None else Quantity(baseline.source_range_si, level_dimension),
            "measure_voltage_autorange": baseline.measure_voltage_autorange,
            "measure_voltage_range": None if baseline.measure_voltage_range_si is None else Quantity(baseline.measure_voltage_range_si, DIMENSION_VOLTAGE),
            "measure_current_autorange": baseline.measure_current_autorange,
            "measure_current_range": None if baseline.measure_current_range_si is None else Quantity(baseline.measure_current_range_si, DIMENSION_CURRENT),
        })
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
        field_map = {
            "source.level": ("level_si",),
            "source.compliance": ("compliance_si",),
            "measurement.nplc": ("nplc",),
            "measurement.settling_time": ("settle_time_s",),
            "measurement.sense_mode": ("sense_mode",),
            "source.range": ("source_autorange", "source_range_si"),
            "measurement.voltage_range": ("measure_voltage_autorange", "measure_voltage_range_si"),
            "measurement.current_range": ("measure_current_autorange", "measure_current_range_si"),
        }
        selected_fields = tuple(dict.fromkeys(
            field for action in parameter_actions
            for field in field_map[str(action["parameter_id"])]
        ))
        configure_action.payload["request"] = replace(
            configure_action.payload["request"], changed_fields=selected_fields,
        )
        legacy_binding = self._semantic_axes_by_source.get(node.id)
        if sweep_values:
            configure_action = self._semanticize_legacy_action(
                configure_action,
                legacy_binding,
                self._legacy_axis_context(node, sweep_values[0], 0),
                sweep_values[0],
                node.id,
            )
        configured_request = configure_action.payload["request"]
        applied_configured_request = self._quantize_keithley_request(
            baseline if output_policy == "continue" else configured_request
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
            if selected_fields:
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
            previous_context = self._active_axis_context
            point_context = self._legacy_axis_context(node, value, point_index)
            if point_context is not None:
                self._active_axis_context = point_context
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
                    point_config = {"channel": channel, "mode": mode, "compliance": value}
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
                    update_action = self._semanticize_legacy_action(
                        update_action,
                        legacy_binding,
                        point_context,
                        value,
                        node.id,
                    )
                    applied_signature = self._keithley_sweep_signature(
                        configured_request, sweep_parameter, value
                    )
                    if (
                        (point_index == 0 and output_policy == "continue")
                        or (point_index > 0 and applied_signature != previous_applied_signature)
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
            if settle_value.si_value > 0 or sweep_parameter == "measurement.settling_time":
                wait_action = self._compile_action(
                        RecipeNode(
                            f"{node.id}.settle",
                            "wait",
                            {"duration": settle_value},
                        ),
                        nested,
                        is_finally=False,
                    )
                if sweep_parameter == "measurement.settling_time":
                    wait_action = self._semanticize_legacy_action(
                        wait_action, legacy_binding, point_context, value, node.id,
                    )
                actions.append(wait_action)
            for child in node.children:
                self._visit(child, nested, actions, is_finally=False)
            self._active_axis_context = previous_context

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
        self._sync_planned_state(actions)
        baseline = self._planned_rigol_configs.get(channel)
        if baseline is None:
            raise ConfigurationError(f"{node.id}: selected Rigol parameters require an explicit configure_rigol baseline for CH{channel}.")
        configuration = {
            **configuration,
            "waveform": baseline.waveform,
            "frequency": Quantity(baseline.frequency_hz, DIMENSION_FREQUENCY),
            "high_level": Quantity(baseline.high_level_v, DIMENSION_VOLTAGE),
            "low_level": Quantity(baseline.low_level_v, DIMENSION_VOLTAGE),
            "output_load": baseline.output_load, "phase_deg": baseline.phase_deg,
            "square_duty_percent": baseline.square_duty_percent,
            "ramp_symmetry_percent": baseline.ramp_symmetry_percent,
            "pulse_width": None if baseline.pulse_width_s is None else Quantity(baseline.pulse_width_s, DIMENSION_TIME),
            "pulse_leading": None if baseline.pulse_leading_s is None else Quantity(baseline.pulse_leading_s, DIMENSION_TIME),
            "pulse_trailing": None if baseline.pulse_trailing_s is None else Quantity(baseline.pulse_trailing_s, DIMENSION_TIME),
        }
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
        legacy_binding = self._semantic_axes_by_source.get(node.id)
        if output_policy == "continue":
            applied_config = self._quantize_rigol_config(baseline)
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
                },
            )
        # Continuity preserves OUTPUT, not the previous selected setpoints.
        # Apply every authored Set and the first ROI point after checking the
        # incoming carrier; otherwise constant Set rows silently disappear.
        selected = {str(action["parameter_id"]) for action in parameter_actions}
        config = configure_action.payload["config"]
        if "carrier.frequency" in selected:
            update = replace(configure_action, kind="update_rigol_frequency", payload={"channel": channel, "frequency_hz": config.frequency_hz})
            if sweep_values and sweep_parameter == "carrier.frequency":
                update = self._semanticize_legacy_action(
                    update, legacy_binding, self._legacy_axis_context(node, sweep_values[0], 0),
                    sweep_values[0], node.id,
                )
            actions.append(update)
            self._remember_literal_configuration(update, context)
        if selected & {"carrier.high_level", "carrier.low_level", "carrier.amplitude", "carrier.offset"}:
            update = replace(configure_action, kind="update_rigol_levels", payload={"channel": channel, "high_level_v": config.high_level_v, "low_level_v": config.low_level_v})
            if sweep_values and sweep_parameter != "carrier.frequency":
                update = self._semanticize_legacy_action(
                    update, legacy_binding, self._legacy_axis_context(node, sweep_values[0], 0),
                    sweep_values[0], node.id,
                )
            actions.append(update)
            self._remember_literal_configuration(update, context)

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
            previous_context = self._active_axis_context
            point_context = self._legacy_axis_context(node, value, point_index)
            if point_context is not None:
                self._active_axis_context = point_context
            nested = dict(context)
            if value is not None and axis_target is not None:
                self._sync_planned_state(actions)
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
                    current_config = self._planned_rigol_configs[channel]
                    point_config["high_level"] = Quantity(current_config.high_level_v, DIMENSION_VOLTAGE)
                    point_config["low_level"] = Quantity(current_config.low_level_v, DIMENSION_VOLTAGE)
                    if sweep_parameter == "carrier.high_level":
                        point_config["high_level"] = value
                    elif sweep_parameter == "carrier.low_level":
                        point_config["low_level"] = value
                    elif sweep_parameter == "carrier.amplitude":
                        current_offset = (
                            current_config.high_level_v
                            + current_config.low_level_v
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
                            current_config.high_level_v
                            - current_config.low_level_v
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
                        {name: point_config[name] for name in ("channel", "high_level", "low_level")},
                    )
                update_action = self._compile_action(
                    update_node, nested, is_finally=False
                )
                update_action = self._semanticize_legacy_action(
                    update_action,
                    legacy_binding,
                    point_context,
                    value,
                    node.id,
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
            self._active_axis_context = previous_context

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
        advanced_data: dict[str, Any] = {}
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
            "advanced.vbw_filter_mode": "vbw_filter_mode",
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

        legacy_binding = self._semantic_axes_by_source.get(node.id)
        for point_index, value in enumerate(sweep_values or (None,)):
            self._check_cancelled()
            previous_context = self._active_axis_context
            point_context = self._legacy_axis_context(node, value, point_index)
            if point_context is not None:
                self._active_axis_context = point_context
            nested = dict(context)
            point_base = dict(base_data)
            if value is not None and sweep_parameter is not None and axis_target is not None:
                key, _dimension, _target = base_parameters[sweep_parameter]
                point_base[key] = value
                nested[axis_target] = value
            self._sync_planned_state(actions)
            selected_base = {
                base_parameters[str(action["parameter_id"])][0]
                for action in parameter_actions
                if str(action["parameter_id"]) in base_parameters
            }
            if selected_base:
                for parameter, (key, _dimension, _target) in base_parameters.items():
                    current = self._planned_context.get(f"anritsu.{parameter}")
                    if current is None:
                        raise ConfigurationError(f"{node.id}: selected spectrum fields require an explicit configure_anritsu baseline.")
                    if key not in selected_base:
                        point_base[key] = int(current.si_value) if key == "points" else current
            if selected_base:
                configure_action = self._compile_action(
                    RecipeNode(
                        f"{node.id}.configure-spectrum",
                        "configure_anritsu",
                        point_base,
                    ),
                    nested,
                    is_finally=False,
                )
                configure_action = self._semanticize_legacy_action(
                    configure_action,
                    legacy_binding,
                    point_context,
                    value,
                    node.id,
                )
                config_fields = {
                    "start_frequency": "start_hz", "stop_frequency": "stop_hz",
                    "reference_level": "reference_level_dbm", "points": "points",
                }
                configure_action.payload["config"] = replace(
                    configure_action.payload["config"],
                    changed_fields=tuple(config_fields[key] for key in sorted(selected_base)),
                )
                if selected_base:
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
            self._active_axis_context = previous_context

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
        self._sync_planned_state(actions)
        baseline_frequency = self._planned_context.get("anritsu.sg.frequency")
        baseline_power = self._planned_context.get("anritsu.sg.power")
        if baseline_frequency is None or baseline_power is None:
            raise ConfigurationError(f"{node.id}: selected SG parameters require an explicit configure_anritsu_sg baseline.")
        point_data.update(frequency=baseline_frequency, power=baseline_power)
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
        legacy_binding = self._semantic_axes_by_source.get(node.id)
        first_configure = self._compile_action(
            RecipeNode(
                f"{node.id}.configure-sg",
                "update_anritsu_sg",
                first_data,
            ),
            first_context,
            is_finally=False,
        )
        if sweep_values:
            first_configure = self._semanticize_legacy_action(
                first_configure,
                legacy_binding,
                self._legacy_axis_context(node, sweep_values[0], 0),
                sweep_values[0],
                node.id,
            )
        first_config = first_configure.payload["config"]
        selected_fields = tuple(dict.fromkeys(
            "frequency_hz" if str(action["parameter_id"]) == "sg.frequency" else "power_dbm"
            for action in parameter_actions
        ))
        first_config = replace(first_config, changed_fields=selected_fields)
        first_configure.payload["config"] = first_config
        runtime_context = dict(context)
        if output_policy == "continue":
            self._append_output_continuity_assertion(
                actions,
                node_id=node.id,
                device="anritsu_sg",
                channel="RF",
                context=context,
                expected_state={
                    "frequency_hz": baseline_frequency.si_value,
                    "power_dbm": baseline_power.si_value,
                },
            )
        if selected_fields:
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
            previous_context = self._active_axis_context
            point_context = self._legacy_axis_context(node, value, point_index)
            if point_context is not None:
                self._active_axis_context = point_context
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
                    update_action = self._compile_action(
                        RecipeNode(
                            f"{node.id}.update-sg",
                            "update_anritsu_sg",
                            current,
                        ),
                        nested,
                        is_finally=False,
                    )
                    update_action.payload["config"] = replace(
                        update_action.payload["config"],
                        changed_fields=("frequency_hz" if sweep_parameter == "sg.frequency" else "power_dbm",),
                    )
                    actions.append(
                        self._semanticize_legacy_action(
                            update_action,
                            legacy_binding,
                            point_context,
                            value,
                            node.id,
                        )
                    )
            for child in node.children:
                self._visit(child, nested, actions, is_finally=False)
            self._active_axis_context = previous_context

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
        if action.kind == "update_keithley_level":
            mode = action.payload["mode"]
            dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
            context[f"keithley.{action.payload['channel']}.{mode}"] = Quantity(action.payload["level_si"], dimension)
            return
        if action.kind == "update_rigol_frequency":
            context[f"rigol.{action.payload['channel']}.frequency"] = Quantity(action.payload["frequency_hz"], DIMENSION_FREQUENCY)
            return
        if action.kind == "update_rigol_levels":
            prefix = f"rigol.{action.payload['channel']}"
            context[f"{prefix}.high_level"] = Quantity(action.payload["high_level_v"], DIMENSION_VOLTAGE)
            context[f"{prefix}.low_level"] = Quantity(action.payload["low_level_v"], DIMENSION_VOLTAGE)
            return
        if action.kind == "configure_rigol":
            config = action.payload["config"]
            prefix = f"rigol.{config.channel}"
            context[f"{prefix}.frequency"] = Quantity(config.frequency_hz, DIMENSION_FREQUENCY)
            context[f"{prefix}.high_level"] = Quantity(config.high_level_v, DIMENSION_VOLTAGE)
            context[f"{prefix}.low_level"] = Quantity(config.low_level_v, DIMENSION_VOLTAGE)
            return
        if action.kind in {"configure_anritsu_sg", "update_anritsu_sg"}:
            config = action.payload["config"]
            context["anritsu.sg.frequency"] = Quantity(config.frequency_hz, DIMENSION_FREQUENCY)
            context["anritsu.sg.power"] = Quantity(config.power_dbm, DIMENSION_DBM)
        if action.kind == "configure_anritsu":
            config = action.payload["config"]
            context["anritsu.spectrum.start_frequency"] = Quantity(config.start_hz, DIMENSION_FREQUENCY)
            context["anritsu.spectrum.stop_frequency"] = Quantity(config.stop_hz, DIMENSION_FREQUENCY)
            context["anritsu.spectrum.reference_level"] = Quantity(config.reference_level_dbm, DIMENSION_DBM)
            context["anritsu.spectrum.points"] = Quantity(float(config.points), "ratio")

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
            condition = node.data["condition"]
            if not isinstance(condition, bool):
                raise ConfigurationError(f"{node.id}: if.condition must be true or false.")
            if any(key in node.data for key in ("left", "operator", "right")):
                raise ConfigurationError(f"{node.id}: use either condition or left/operator/right.")
            return condition
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

    def _legacy_axis_context(
        self,
        node: RecipeNode,
        value: Quantity | None,
        point_index: int,
    ) -> AxisPointContext | None:
        """Build the same point context for a provider-owned legacy sweep.

        Device-module sweeps predate the explicit ``sweep`` node and expand
        their points inside the provider visitor.  Without this bridge,
        authored children (WAIT/acquire) had no axis context and the semantic
        tree could not show which ROI value was active.
        """

        if value is None:
            return None
        binding = self._semantic_axes_by_source.get(node.id)
        if binding is None:
            return None
        stage_index = binding.stage_index_at(point_index)
        active = (
            dict(self._active_axis_context.active_setpoints_si)
            if self._active_axis_context is not None
            else {}
        )
        target = str(getattr(binding, "target", ""))
        if target:
            active[target] = value.si_value
        loop_path = (
            (*self._active_axis_context.loop_path, node.id)
            if self._active_axis_context is not None
            else (*self._active_axis_path, node.id)
        )
        return AxisPointContext(
            str(getattr(binding, "axis_id", f"{node.id}.axis")),
            point_index,
            len(getattr(binding, "points", ())) or 1,
            stage_index,
            value.si_value,
            active,
            loop_path,
        )

    @staticmethod
    def _semanticize_legacy_action(
        action: PlanAction,
        binding: object | None,
        context: AxisPointContext | None,
        value: Quantity | None,
        source_node_id: str,
    ) -> PlanAction:
        """Attach the shared ROI operation identity to a legacy action."""

        if binding is None or context is None or value is None:
            return action
        target = str(getattr(binding, "target", ""))
        if not target:
            return action
        payload = dict(action.payload)
        payload.setdefault("requested_si", value.si_value)
        if "applied_si" not in payload:
            if action.kind == "update_keithley_level":
                payload["applied_si"] = payload.get("level_si", value.si_value)
            elif action.kind == "update_keithley_compliance":
                payload["applied_si"] = payload.get("compliance_si", value.si_value)
            elif action.kind == "update_rigol_frequency":
                payload["applied_si"] = payload.get("frequency_hz", value.si_value)
            elif action.kind == "update_rigol_levels":
                high, low = payload["high_level_v"], payload["low_level_v"]
                parameter = target.rsplit(".", 1)[-1]
                payload["applied_si"] = {
                    "high_level": high, "low_level": low,
                    "amplitude": high - low, "offset": (high + low) / 2,
                }[parameter]
            else:
                payload["applied_si"] = value.si_value
        payload.setdefault("target", target)
        return replace(
            action,
            payload=payload,
            semantic_id=f"{getattr(binding, 'axis_id', source_node_id)}.set-roi-value",
            source_node_id=source_node_id,
            axis_context=context,
        )

    def _compile_action(
        self, node: RecipeNode, context: dict[str, Quantity], *, is_finally: bool
    ) -> PlanAction:
        validate_action_fields(node.type, node.data, node.id)
        data = {
            key: self._resolve_value(value, context) for key, value in node.data.items()
            if key not in {"description", "disabled"} and (key != "label" or node.type == "checkpoint")
        }
        setpoints = self._context_as_si(context)
        action_kind = node.type
        if node.type == "configure_rigol":
            payload = self._compile_rigol(data)
            names = {"waveform": "waveform", "frequency": "frequency_hz", "high_level": "high_level_v", "low_level": "low_level_v",
                     "output_load": "output_load", "phase_deg": "phase_deg", "square_duty_percent": "square_duty_percent",
                     "ramp_symmetry_percent": "ramp_symmetry_percent", "pulse_width": "pulse_width_s",
                     "pulse_leading": "pulse_leading_s", "pulse_trailing": "pulse_trailing_s"}
            payload["config"] = replace(payload["config"], changed_fields=tuple(value for key, value in names.items() if key in data))
        elif node.type == "configure_moke_box":
            unknown = set(data) - {"channel", "minimum_voltage", "maximum_voltage", "calibration_id"}
            if unknown:
                raise ConfigurationError(f"{node.id}: unsupported MOKE voltage configuration fields: {sorted(unknown)}.")
            simulation = bool((self._settings.moke_box.endpoint or "").startswith("SIM::MOKE"))
            channel = data.get("channel")
            profile = control_profile_from_settings(self._settings, simulation=simulation, channel=channel)
            if type(channel) is not int or channel != profile.channel:
                raise SafetyViolation(f"{node.id}: MOKE configuration requires the qualified VOUT channel.")
            minimum = parse_quantity(data.get("minimum_voltage"), DIMENSION_VOLTAGE).si_value
            maximum = parse_quantity(data.get("maximum_voltage"), DIMENSION_VOLTAGE).si_value
            MokeVoltagePlan(profile.fingerprint, channel, minimum, maximum, (minimum,)).validate(profile)
            repository = MokeCalibrationRepository(self._settings.moke_box.calibration_directory)
            selected = data.get("calibration_id", self._settings.moke_box.active_calibration_id)
            calibration = repository.load(selected) if selected else repository.active(
                profile_fingerprint=profile.fingerprint, simulation=profile.simulation)
            if calibration is not None and (
                calibration.context.profile_fingerprint != profile.fingerprint
                or calibration.context.simulation != profile.simulation
            ):
                raise ConfigurationError("MOKE calibration snapshot does not match the configured output profile.")
            payload = {"profile": profile, "minimum_v": minimum, "maximum_v": maximum, "calibration": calibration}
        elif node.type == "arm_moke_voltage":
            if data or is_finally:
                raise ConfigurationError("arm_moke_voltage has no mutable parameters and cannot be a finally action.")
            payload = {}
        elif node.type == "update_moke_voltage":
            if is_finally:
                raise ConfigurationError("Use stop_moke_voltage in finally; normal voltage updates cannot be cleanup actions.")
            channel = data.get("channel")
            if type(channel) is not int or channel not in range(8):
                raise ConfigurationError("MOKE voltage update requires integer channel 0..7.")
            value = parse_quantity(data.get("voltage"), DIMENSION_VOLTAGE).si_value
            payload = {"channel": channel, "voltage_v": value}
        elif node.type == "stop_moke_voltage":
            simulation = bool((self._settings.moke_box.endpoint or "").startswith("SIM::MOKE"))
            channel = data.get("channel")
            if channel is not None and (type(channel) is not int or channel not in range(8)):
                raise ConfigurationError("MOKE stop requires channel 0..7.")
            profile = control_profile_from_settings(self._settings, simulation=simulation, channel=channel)
            payload = {"ramp_timeout_s": profile.ramp_timeout_s}
            if channel is not None:
                payload["channel"] = channel
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
            field_map = {
                "mode": "mode", "level": "level_si", "compliance": "compliance_si",
                "nplc": "nplc", "settle_time": "settle_time_s", "settling_time": "settle_time_s",
                "sense_mode": "sense_mode", "source_autorange": "source_autorange",
                "source_range": "source_range_si", "measure_voltage_autorange": "measure_voltage_autorange",
                "measure_voltage_range": "measure_voltage_range_si", "measure_current_autorange": "measure_current_autorange",
                "measure_current_range": "measure_current_range_si",
            }
            selected = list(dict.fromkeys(field_map[key] for key in data if key in field_map))
            if "source_range_si" in selected and "source_autorange" not in selected:
                selected.append("source_autorange")
            payload["request"] = replace(payload["request"], changed_fields=tuple(selected))
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
            channel, mode = data.get("channel"), data.get("mode")
            request = self._planned_keithley_requests.get(channel)
            if request is None or request.mode != mode or mode == "measure_only":
                raise ConfigurationError(
                    f"{node.id}: compliance update requires a matching explicit current/voltage baseline."
                )
            dimension = DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
            request = replace(request, compliance_si=self._resolve_quantity(data["compliance"], dimension, {}).si_value)
            validate_keithley_source(self._settings.keithley.safety.channels[channel], request)
            payload = {
                "channel": request.channel,
                "mode": request.mode,
                "compliance_si": request.compliance_si,
            }
        elif node.type == "update_rigol_frequency":
            payload = self._compile_rigol_frequency_update(data, node.id)
        elif node.type == "update_rigol_levels":
            channel = data["channel"]
            baseline = self._planned_rigol_configs.get(channel)
            if baseline is None:
                raise ConfigurationError(f"{node.id}: Rigol level update requires an explicit carrier baseline.")
            config = replace(baseline,
                high_level_v=self._resolve_quantity(data["high_level"], DIMENSION_VOLTAGE, {}).si_value,
                low_level_v=self._resolve_quantity(data["low_level"], DIMENSION_VOLTAGE, {}).si_value)
            validate_rigol_waveform(channel=self._settings.rigol.safety.channels[str(channel)], safety=self._settings.rigol.safety,
                waveform=config.waveform, frequency=config.frequency_hz, high_level=config.high_level_v,
                low_level=config.low_level_v, output_load=config.output_load)
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
            if node.type == "acquire_spectrum" and ("source_file" in data or "file_kind" in data):
                raise ConfigurationError(f"{node.id}: load reference files in an acquire_reference step.")
            if node.type == "acquire_reference":
                if "processing" in data:
                    raise ConfigurationError(f"{node.id}: reference sources must remain unfiltered; apply filters to acquire_spectrum.")
                if ("source_file" in data or "file_kind" in data) and (
                    not isinstance(data.get("source_file"), str) or not data["source_file"].strip()
                ):
                    raise ConfigurationError(f"{node.id}: choose a reference/background source_file.")
            if self._settings.anritsu.acquisition.single_sweep_mode != "standard_scpi_opc":
                raise SafetyViolation(
                    f"{node.type} requires the qualified Anritsu standard_scpi_opc protocol."
                )
            payload = {
                "trace": validate_anritsu_trace_name(str(data.get("trace", "TRAC1"))),
            }
            average_count = data.get("average_count", 1)
            if type(average_count) is not int:
                raise ConfigurationError(
                    f"{node.id}: average_count must be an integer."
                )
            if not 1 <= average_count <= 9999:
                raise SafetyViolation(
                    f"{node.id}: average_count must be in the range 1..9999."
                )
            payload["average_count"] = average_count
            if node.type == "acquire_reference":
                duration = self._resolve_quantity(data.get("minimum_duration", "0 s"), DIMENSION_TIME, {}).si_value
                if not 0 <= duration <= 3600:
                    raise ConfigurationError(f"{node.id}: minimum_duration must be in 0..3600 s.")
                purpose = data.get("purpose", "reference")
                if not isinstance(purpose, str) or purpose not in {"reference", "background"}:
                    raise ConfigurationError(f"{node.id}: purpose must be reference or background.")
                if duration and data.get("source_file"):
                    raise ConfigurationError(f"{node.id}: minimum_duration cannot be used with an imported file.")
                if duration:
                    payload["minimum_duration_s"] = duration
                if "purpose" in data:
                    payload["purpose"] = purpose
            delay = self._resolve_quantity(data.get("inter_sweep_delay", "0 s"), DIMENSION_TIME, {}).si_value
            if not 0 <= delay <= 3600:
                raise SafetyViolation(f"{node.id}: inter_sweep_delay must be in 0..3600 s.")
            payload["inter_sweep_delay_s"] = delay
            if node.type == "acquire_reference" and data.get("source_file"):
                from pathlib import Path

                from app.storage.recipe_reference import file_sha256, load_recipe_reference

                path = Path(str(data["source_file"])).expanduser().resolve()
                kind = str(data.get("file_kind", "reference"))
                try:
                    key = (str(path), kind)
                    if key not in self._reference_assets:
                        digest = file_sha256(path)
                        _trace, count, _evidence = load_recipe_reference(path, kind)
                        if file_sha256(path) != digest:
                            raise ValueError("Reference file changed during preflight.")
                        self._reference_assets[key] = (digest, count)
                    digest, count = self._reference_assets[key]
                except (OSError, ValueError, ExecutionError) as exc:
                    raise ConfigurationError(f"{node.id}: cannot load reference: {exc}") from exc
                payload.update(source_file=str(path), source_sha256=digest, file_kind=kind, average_count=count)
            if node.type == "acquire_spectrum":
                from app.recipes.spectrum_processing import (
                    REFERENCE_OPERATIONS,
                    parse_processing,
                    processing_mapping,
                )

                operation = str(data.get("reference_operation", "none")).strip().lower()
                if operation not in {value for _label, value in REFERENCE_OPERATIONS}:
                    raise ConfigurationError(
                        f"{node.id}: unsupported reference_operation {operation!r}."
                    )
                try:
                    filters, parameters = parse_processing(data.get("processing"))
                except ValueError as exc:
                    raise ConfigurationError(f"{node.id}: {exc}") from exc
                if "emi_reject" in filters and (average_count < parameters.emi_min_frames
                        or operation in {"ratio_linear", "multiply_linear", "subtract_power_signed"}):
                    raise ConfigurationError(f"{node.id}: EMI requires dB/dBm and at least {parameters.emi_min_frames} raw sweeps per point.")
                if filters:
                    payload["processing"] = processing_mapping(filters, parameters)
                store_raw = self._optional_boolean(data, "store_raw", True, node.id)
                store_processed = self._optional_boolean(
                    data, "store_processed", operation != "none" or bool(filters), node.id
                )
                if not store_raw:
                    raise ConfigurationError(
                        f"{node.id}: RAW spectrum storage is required for scientific "
                        "provenance and processed-spectrum grid identity."
                    )
                if filters and not store_processed:
                    raise ConfigurationError(f"{node.id}: selected filters require processed storage alongside RAW.")
                if store_processed and operation == "none" and not filters:
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
        elif node.type in {"upload_to_elab", "upload_elab"}:
            action_kind = "upload_to_elab"
            payload = {
                "template_id": data.get("template_id"),
                "template_name": str(data.get("template_name", "") or "").strip(),
                "title_pattern": str(data.get("title_pattern", "") or "").strip(),
                "tags": list(data.get("tags") or []),
                "attach_hdf5": bool(data.get("attach_hdf5", True)),
                "attach_csv": bool(data.get("attach_csv", True)),
            }
        else:
            raise ConfigurationError(f"{node.id}: unsupported action type {node.type!r}.")
        if is_finally:
            safe_finally_actions = {
                "stop_moke_voltage",
                "ramp_keithley_to_zero",
                "set_rigol_output",
                "set_keithley_output",
                "set_anritsu_sg_output",
            }
            if node.type not in safe_finally_actions:
                raise SafetyViolation("The finally section may contain only an approved ramp to zero or output-off action.")
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
            axis_context=self._active_axis_context,
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
        for value_name, mode_name in (("rbw", "rbw_mode"), ("vbw", "vbw_mode"),
                                      ("attenuation", "attenuation_mode"), ("sweep_time", "sweep_time_mode")):
            if value_name in data and str(data.get(mode_name, "")).strip().lower() != "manual":
                raise ConfigurationError(f"{node_id}: {value_name} requires explicit {mode_name}=manual; the value cannot be ignored.")
        filter_mode = data.get("vbw_filter_mode")
        if filter_mode not in {None, "VID", "POW"}:
            raise ConfigurationError(f"{node_id}: vbw_filter_mode must be VID or POW.")
        rbw_mode = str(data["rbw_mode"]).strip().lower() if "rbw_mode" in data else None
        vbw_mode = str(data["vbw_mode"]).strip().lower() if "vbw_mode" in data else None
        attenuation_mode = str(data["attenuation_mode"]).strip().lower() if "attenuation_mode" in data else None
        sweep_time_mode = str(data["sweep_time_mode"]).strip().lower() if "sweep_time_mode" in data else None
        for name, mode, allowed in (
            ("rbw_mode", rbw_mode, {"auto", "manual"}),
            ("vbw_mode", vbw_mode, {"auto", "manual", "off"}),
            ("attenuation_mode", attenuation_mode, {"auto", "manual"}),
            ("sweep_time_mode", sweep_time_mode, {"auto", "manual"}),
        ):
            if mode is not None and mode not in allowed:
                raise ConfigurationError(
                    f"{node_id}.{name} must be one of: {', '.join(sorted(allowed))}."
                )
        config = AdvancedSpectrumConfig(
            vbw_filter_mode=filter_mode,
            rbw_auto=rbw_mode == "auto" if rbw_mode is not None else None,
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
            detector=str(data["detector"]).strip().upper() if "detector" in data else None,
            attenuation_auto=attenuation_mode == "auto" if attenuation_mode is not None else None,
            attenuation_db=(
                self._resolve_quantity(data.get("attenuation"), DIMENSION_DB, {}).si_value
                if attenuation_mode == "manual"
                else None
            ),
            preamplifier_enabled=self._optional_boolean(
                data, "preamplifier_enabled", False, node_id
            ) if "preamplifier_enabled" in data else None,
            sweep_time_auto=sweep_time_mode == "auto" if sweep_time_mode is not None else None,
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
        configured_auto = self._settings.keithley.safety.channels[channel].defaults.get("source_autorange", False)
        if not isinstance(configured_auto, bool):
            raise ConfigurationError("Settings source_autorange must be a boolean.")
        required_auto = configured_auto if mode != "measure_only" else False
        recipe_auto = self._optional_boolean(data, "source_autorange", required_auto, node_id)
        if recipe_auto != required_auto:
            raise SafetyViolation(
                f"{node_id}: recipe source_autorange={recipe_auto} conflicts with "
                f"{'measure_only' if mode == 'measure_only' else 'Settings'} "
                f"source_autorange={required_auto}. Update the recipe or change Settings explicitly."
            )
        source_auto = recipe_auto
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
            settle_time_s=self._resolve_quantity(
                data.get("settle_time") or data.get("settling_time") or "0 s",
                DIMENSION_TIME,
                {},
            ).si_value,
            sense_mode=str(data.get("sense_mode", "2wire")),  # type: ignore[arg-type]
            source_autorange=source_auto,
            source_range_si=None if source_auto or mode == "measure_only" else self._optional_quantity(data, "source_range", dimension),
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
        if data.get("vbw_filter_mode") not in {None, "VID", "POW"}:
            raise ConfigurationError("Anritsu vbw_filter_mode must be VID or POW.")
        if type(data.get("points")) is not int:
            raise ConfigurationError("Anritsu points must be an integer without rounding.")
        config = SpectrumConfig(
            start_hz=self._resolve_quantity(data["start_frequency"], DIMENSION_FREQUENCY, {}).si_value,
            stop_hz=self._resolve_quantity(data["stop_frequency"], DIMENSION_FREQUENCY, {}).si_value,
            reference_level_dbm=self._resolve_quantity(data["reference_level"], DIMENSION_DBM, {}).si_value,
            points=data["points"],
            trace=validate_anritsu_trace_name(str(data.get("trace", "TRAC1"))),
            prepare_current_buffer=False,
            vbw_mode=data.get("vbw_filter_mode"),
        )
        validate_anritsu_spectrum(
            safety,
            start_hz=config.start_hz,
            stop_hz=config.stop_hz,
            reference_level_dbm=config.reference_level_dbm,
            points=config.points,
        )
        return {"config": config}

