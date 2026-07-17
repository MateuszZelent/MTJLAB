"""Compile a declarative recipe into a finite, preflight-validated action plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from collections.abc import Iterable
import hashlib
import json
import math
import re
from typing import Any, Final

from app.devices.anritsu.adapter import (
    AdvancedSpectrumConfig,
    SignalGeneratorConfig,
    SpectrumConfig,
)
from app.devices.rigol.adapter import RigolChannelConfig
from app.domain.dut import ExperimentDutLimits
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    Quantity,
    parse_quantity,
)
from app.recipes.models import Recipe, RecipeNode
from app.recipes.sweep_points import generate_sweep_points
from app.safety.keithley import (
    KeithleySafetyEnvelope,
    KeithleySourceRequest,
    validate_keithley_source,
)
from app.safety.anritsu import (
    validate_anritsu_advanced_spectrum,
    validate_anritsu_signal_generator,
    validate_anritsu_spectrum,
    validate_anritsu_trace_name,
)
from app.safety.rigol_current import RigolSafetyEnvelope, validate_rigol_waveform
from app.settings.models import StationSettings


_REFERENCE_RE: Final = re.compile(r"^\$\{([A-Za-z0-9_.-]+)}$")
_SWEEP_DIMENSIONS: Final[dict[str, str]] = {
    "keithley.A.level": DIMENSION_CURRENT,
    "keithley.B.level": DIMENSION_CURRENT,
    "keithley.A.current": DIMENSION_CURRENT,
    "keithley.B.current": DIMENSION_CURRENT,
    "keithley.A.voltage": DIMENSION_VOLTAGE,
    "keithley.B.voltage": DIMENSION_VOLTAGE,
    "rigol.1.high_level": DIMENSION_VOLTAGE,
    "rigol.2.high_level": DIMENSION_VOLTAGE,
    "rigol.1.low_level": DIMENSION_VOLTAGE,
    "rigol.2.low_level": DIMENSION_VOLTAGE,
    "rigol.1.frequency": DIMENSION_FREQUENCY,
    "rigol.2.frequency": DIMENSION_FREQUENCY,
    "anritsu.sg.frequency": DIMENSION_FREQUENCY,
    "anritsu.sg.power": DIMENSION_DBM,
    "anritsu.spectrum.start_frequency": DIMENSION_FREQUENCY,
    "anritsu.spectrum.stop_frequency": DIMENSION_FREQUENCY,
    "anritsu.spectrum.reference_level": DIMENSION_DBM,
}


@dataclass(frozen=True, slots=True)
class PlanAction:
    node_id: str
    kind: str
    payload: dict[str, Any]
    setpoints_si: dict[str, float]
    is_finally: bool = False


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


def required_devices_for_actions(actions: Iterable[PlanAction]) -> frozenset[str]:
    """Return the exact instrument set touched by an immutable execution plan."""

    required: set[str] = set()
    for action in actions:
        if "rigol" in action.kind:
            required.add("rigol")
        if "keithley" in action.kind:
            required.add("keithley")
        if "anritsu" in action.kind or action.kind == "acquire_spectrum":
            required.add("anritsu")
        if action.kind == "verify_connection":
            required.add(str(action.payload["device"]))
    return frozenset(required)


class RecipeCompiler:
    """Reject unsafe values before an adapter can see an execution request."""

    def __init__(self, settings: StationSettings) -> None:
        self._settings = settings
        self._max_actions = int(settings.execution.get("max_expanded_points", 100_000)) * 10
        self._dut_limits = ExperimentDutLimits()

    def compile(self, recipe: Recipe) -> ExecutionPlan:
        self._dut_limits = recipe.dut_limits
        actions: list[PlanAction] = []
        self._visit(recipe.root, {}, actions)
        for node in recipe.finally_nodes:
            self._visit(node, {}, actions, is_finally=True)
        if not actions:
            raise ConfigurationError("The recipe contains no executable actions.")
        if len(actions) > self._max_actions:
            raise SafetyViolation(
                f"The plan expands to {len(actions)} actions; the limit is {self._max_actions}."
            )
        total_points = sum(
            action.kind in {"acquire_spectrum", "checkpoint"} for action in actions
        )
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
                }
                for item in actions
                ],
                "safe_shutdown_actions": safe_shutdown_actions,
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
        # Every recipe ends with a station-wide output-off attempt. A sweep
        # must never leave an unrelated manual source energized merely because
        # it was not referenced by the compiled action list.
        for device in ("keithley", "rigol", "anritsu"):
            action = required_actions[device]
            if action not in result:
                result.append(action)
        result.append("storage.flush_checkpoint")
        return tuple(result)

    @staticmethod
    def _canonicalize(value: Any) -> Any:
        if is_dataclass(value):
            return RecipeCompiler._canonicalize(asdict(value))
        if isinstance(value, dict):
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
        if len(actions) > self._max_actions:
            raise SafetyViolation("The expanded-action limit was exceeded.")
        if node.type == "sequence":
            if node.data.get("operation") == "configure_selected_parameters":
                raise ConfigurationError(
                    f"{node.id}: DeviceNode provider compilation is not implemented yet. "
                    "Execution is blocked so nested acquisitions cannot run once instead "
                    "of once per sweep point."
                )
            for child in node.children:
                self._visit(child, context, actions, is_finally=is_finally)
            return
        if node.type == "sweep":
            target = str(node.data["target"])
            try:
                dimension = _SWEEP_DIMENSIONS[target]
            except KeyError as exc:
                allowed = ", ".join(sorted(_SWEEP_DIMENSIONS))
                raise ConfigurationError(f"Unsupported sweep target {target!r}; allowed: {allowed}.") from exc
            for value in self._node_sweep_values(node, dimension, context):
                nested = dict(context)
                nested[target] = value
                for child in node.children:
                    self._visit(child, nested, actions, is_finally=is_finally)
            return
        if node.type == "repeat":
            for index in range(int(node.data["count"])):
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
                for key in ("start", "stop", "step"):
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
        elif node.type == "configure_keithley":
            payload = self._compile_keithley(data, node.id)
        elif node.type == "configure_anritsu":
            payload = self._compile_anritsu(data)
        elif node.type == "configure_anritsu_advanced":
            payload = self._compile_anritsu_advanced(data, node.id)
        elif node.type == "configure_anritsu_sg":
            payload = self._compile_anritsu_signal_generator(data)
        elif node.type == "measure_keithley":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError(f"{node.id}: measure_keithley requires channel A or B.")
            payload = {"channel": channel}
        elif node.type == "acquire_spectrum":
            if self._settings.anritsu.acquisition.single_sweep_mode != "standard_scpi_opc":
                raise SafetyViolation(
                    "acquire_spectrum requires the qualified Anritsu standard_scpi_opc protocol."
                )
            dut_input = (
                self._dut_limits.anritsu.max_expected_input_dbm
                if self._dut_limits.anritsu is not None
                else None
            )
            payload = {
                "trace": validate_anritsu_trace_name(str(data.get("trace", "TRAC1"))),
                "dut_max_expected_input_dbm": dut_input,
            }
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
            if enabled:
                self._require_complete_dut_limits("rigol", channel)
            payload = {"channel": channel, "enabled": enabled}
        elif node.type == "arm_rigol_output":
            channel = int(data.get("channel", 0))
            if channel not in {1, 2}:
                raise ConfigurationError("arm_rigol_output requires channel 1 or 2.")
            self._assert_output_action_allowed("rigol", True)
            self._require_complete_dut_limits("rigol", channel)
            payload = {"channel": channel}
        elif node.type == "set_keithley_output":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("set_keithley_output requires channel A or B.")
            enabled = self._require_boolean(data, "enabled", node.id)
            self._assert_output_action_allowed("keithley", enabled)
            if enabled:
                self._require_complete_dut_limits("keithley", channel)
            payload = {"channel": channel, "enabled": enabled}
        elif node.type == "ramp_keithley_to_zero":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("ramp_keithley_to_zero requires channel A or B.")
            deadline = self._resolve_quantity(data.get("deadline", "10 s"), DIMENSION_TIME, context).si_value
            if deadline <= 0 or deadline > 120:
                raise SafetyViolation("Keithley ramp deadline must be in the range (0, 120] s.")
            payload = {"channel": channel, "deadline_s": deadline}
        elif node.type == "arm_keithley_output":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("arm_keithley_output requires channel A or B.")
            self._assert_output_action_allowed("keithley", True)
            self._require_complete_dut_limits("keithley", channel)
            payload = {"channel": channel}
        elif node.type == "arm_anritsu_sg_output":
            self._assert_output_action_allowed("anritsu_sg", True)
            self._require_complete_dut_limits("anritsu_sg", "RF")
            payload = {}
        elif node.type == "set_anritsu_sg_output":
            enabled = self._require_boolean(data, "enabled", node.id)
            self._assert_output_action_allowed("anritsu_sg", enabled)
            if enabled:
                self._require_complete_dut_limits("anritsu_sg", "RF")
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
        return PlanAction(node.id, action_kind, payload, setpoints, is_finally=is_finally)

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
        if not enabled:
            return
        if self._settings.outputs_locked:
            raise SafetyViolation("The recipe cannot enable output because the profile is not approved.")
        if device == "rigol":
            permitted = self._settings.rigol.safety.allow_output_enable
        elif device == "keithley":
            permitted = self._settings.keithley.safety.allow_output_enable
        elif device == "anritsu_sg":
            permitted = self._settings.anritsu.safety.signal_generator_output_allowed
        else:
            raise ConfigurationError(f"Unknown output device {device!r}.")
        if not permitted:
            raise SafetyViolation(f"The recipe cannot enable {device}: allow_output_enable=false.")

    def _require_complete_dut_limits(self, device: str, channel: str | int) -> None:
        if device == "anritsu_sg":
            limits = self._dut_limits.anritsu
            complete = bool(
                limits is not None
                and limits.max_signal_generator_output_dbm is not None
            )
        elif device == "keithley":
            limits = self._dut_limits.keithley.get(str(channel))
            complete = bool(
                limits is not None
                and limits.current is not None
                and limits.voltage is not None
                and limits.max_abs_power_w is not None
            )
        elif device == "rigol":
            limits = self._dut_limits.rigol.get(int(channel))
            complete = bool(
                limits is not None
                and limits.minimum_impedance_ohm is not None
                and limits.max_abs_current_a is not None
                and limits.max_abs_power_w is not None
            )
        else:
            raise ConfigurationError(f"Unknown DUT limit device {device!r}.")
        if not complete:
            raise SafetyViolation(
                f"OUTPUT for {device} channel {channel} requires complete recipe.dut_limits "
                "for current, voltage/power or impedance/current/power."
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
        dut = self._dut_limits.anritsu
        if (
            dut is not None
            and dut.max_signal_generator_output_dbm is not None
            and config.power_dbm > dut.max_signal_generator_output_dbm
        ):
            raise SafetyViolation(
                f"Anritsu SG power {config.power_dbm:g} dBm exceeds recipe DUT limit "
                f"{dut.max_signal_generator_output_dbm:g} dBm."
            )
        return {"config": config}

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
        dut = self._dut_limits.rigol.get(channel)
        envelope = (
            RigolSafetyEnvelope(
                minimum_impedance_ohm=dut.minimum_impedance_ohm,
                max_abs_current_a=dut.max_abs_current_a,
                max_abs_power_w=dut.max_abs_power_w,
            )
            if dut is not None
            else None
        )
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
            dut_min_impedance_ohm=self._resolve_quantity(data["dut_min_impedance"], DIMENSION_RESISTANCE, {}).si_value if "dut_min_impedance" in data else None,
            dut_envelope=envelope,
        )
        validate_rigol_waveform(
            channel=settings,
            safety=self._settings.rigol.safety,
            waveform=config.waveform,
            frequency=config.frequency_hz,
            high_level=config.high_level_v,
            low_level=config.low_level_v,
            output_load=config.output_load,
            dut_min_impedance=config.dut_min_impedance_ohm,
            dut_envelope=config.dut_envelope,
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
        dut = self._dut_limits.keithley.get(channel)
        envelope = (
            KeithleySafetyEnvelope(
                current_min_a=dut.current.minimum_si if dut.current is not None else None,
                current_max_a=dut.current.maximum_si if dut.current is not None else None,
                voltage_min_v=dut.voltage.minimum_si if dut.voltage is not None else None,
                voltage_max_v=dut.voltage.maximum_si if dut.voltage is not None else None,
                max_abs_power_w=dut.max_abs_power_w,
            )
            if dut is not None
            else None
        )
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
            dut_envelope=envelope,
        )
        validate_keithley_source(self._settings.keithley.safety.channels[channel], request)
        return {"request": request}

    def _compile_anritsu(self, data: dict[str, Any]) -> dict[str, Any]:
        safety = self._settings.anritsu.safety
        dut_input = (
            self._dut_limits.anritsu.max_expected_input_dbm
            if self._dut_limits.anritsu is not None
            else None
        )
        config = SpectrumConfig(
            start_hz=self._resolve_quantity(data["start_frequency"], DIMENSION_FREQUENCY, {}).si_value,
            stop_hz=self._resolve_quantity(data["stop_frequency"], DIMENSION_FREQUENCY, {}).si_value,
            reference_level_dbm=self._resolve_quantity(data["reference_level"], DIMENSION_DBM, {}).si_value,
            points=int(data["points"]),
            trace=validate_anritsu_trace_name(str(data.get("trace", "TRAC1"))),
            dut_max_expected_input_dbm=dut_input,
        )
        validate_anritsu_spectrum(
            safety,
            start_hz=config.start_hz,
            stop_hz=config.stop_hz,
            reference_level_dbm=config.reference_level_dbm,
            points=config.points,
            dut_max_expected_input_dbm=config.dut_max_expected_input_dbm,
        )
        return {"config": config}
