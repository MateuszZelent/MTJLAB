"""Compile a declarative recipe into a finite, preflight-validated action plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import re
from typing import Any, Final

from app.devices.anritsu.adapter import SpectrumConfig
from app.devices.rigol.adapter import RigolChannelConfig
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    Quantity,
    parse_quantity,
)
from app.recipes.models import Recipe, RecipeNode
from app.safety.keithley import KeithleySourceRequest, validate_keithley_source
from app.safety.anritsu import validate_anritsu_spectrum, validate_anritsu_trace_name
from app.safety.rigol_current import validate_rigol_waveform
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


class RecipeCompiler:
    """Reject unsafe values before an adapter can see an execution request."""

    def __init__(self, settings: StationSettings) -> None:
        self._settings = settings
        self._max_actions = int(settings.execution.get("max_expanded_points", 100_000)) * 10

    def compile(self, recipe: Recipe) -> ExecutionPlan:
        actions: list[PlanAction] = []
        self._visit(recipe.root, {}, actions)
        for node in recipe.finally_nodes:
            self._visit(node, {}, actions, is_finally=True)
        if not actions:
            raise ConfigurationError("Receptura nie zawiera żadnej wykonywalnej akcji.")
        if len(actions) > self._max_actions:
            raise SafetyViolation(
                f"Plan rozwija się do {len(actions)} akcji, limit wynosi {self._max_actions}."
            )
        total_points = sum(action.kind == "acquire_spectrum" for action in actions)
        canonical = json.dumps(
            [
                {
                    "node_id": item.node_id,
                    "kind": item.kind,
                    "payload": self._canonicalize(item.payload),
                    "setpoints": item.setpoints_si,
                    "is_finally": item.is_finally,
                }
                for item in actions
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return ExecutionPlan(recipe.name, tuple(actions), total_points, digest, recipe.source_text)

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
            raise SafetyViolation("Limit rozwiniętych akcji został przekroczony.")
        if node.type == "sequence":
            for child in node.children:
                self._visit(child, context, actions, is_finally=is_finally)
            return
        if node.type == "sweep":
            target = str(node.data["target"])
            try:
                dimension = _SWEEP_DIMENSIONS[target]
            except KeyError as exc:
                allowed = ", ".join(sorted(_SWEEP_DIMENSIONS))
                raise ConfigurationError(f"Nieobsługiwany target sweep {target!r}; dozwolone: {allowed}.") from exc
            start = self._resolve_quantity(node.data["start"], dimension, context)
            stop = self._resolve_quantity(node.data["stop"], dimension, context)
            points = int(node.data["points"])
            spacing = str(node.data.get("spacing", "linear"))
            for value in self._sweep_values(start, stop, points, spacing):
                nested = dict(context)
                nested[target] = value
                for child in node.children:
                    self._visit(child, nested, actions, is_finally=is_finally)
            return
        if node.type == "comment":
            return
        action = self._compile_action(node, context, is_finally=is_finally)
        actions.append(action)

    @staticmethod
    def _sweep_values(start: Quantity, stop: Quantity, points: int, spacing: str) -> tuple[Quantity, ...]:
        if spacing == "linear":
            step = (stop.si_value - start.si_value) / (points - 1)
            return tuple(Quantity(start.si_value + index * step, start.dimension) for index in range(points))
        if start.si_value <= 0 or stop.si_value <= 0:
            raise ConfigurationError("Sweep logarytmiczny wymaga dodatniego start i stop.")
        ratio = (stop.si_value / start.si_value) ** (1 / (points - 1))
        return tuple(Quantity(start.si_value * ratio**index, start.dimension) for index in range(points))

    def _resolve_value(self, value: Any, context: dict[str, Quantity]) -> Any:
        if isinstance(value, str):
            match = _REFERENCE_RE.match(value)
            if match:
                try:
                    return context[match.group(1)]
                except KeyError as exc:
                    raise ConfigurationError(f"Brak wartości sweep dla {value}.") from exc
        return value

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
        if node.type == "configure_rigol":
            payload = self._compile_rigol(data)
        elif node.type == "configure_keithley":
            payload = self._compile_keithley(data, node.id)
        elif node.type == "configure_anritsu":
            payload = self._compile_anritsu(data)
        elif node.type == "measure_keithley":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError(f"{node.id}: measure_keithley wymaga channel A lub B.")
            payload = {"channel": channel}
        elif node.type == "acquire_spectrum":
            if self._settings.anritsu.acquisition.single_sweep_mode != "standard_scpi_opc":
                raise SafetyViolation(
                    "acquire_spectrum wymaga zatwierdzonego protokołu Anritsu standard_scpi_opc."
                )
            payload = {"trace": validate_anritsu_trace_name(str(data.get("trace", "TRAC1")))}
        elif node.type == "wait":
            duration = self._resolve_quantity(data.get("duration"), DIMENSION_TIME, context).si_value
            if duration < 0 or duration > 3600:
                raise SafetyViolation("Czas wait musi być w zakresie 0–3600 s.")
            payload = {"duration_s": duration}
        elif node.type == "set_rigol_output":
            channel = int(data.get("channel", 0))
            if channel not in {1, 2}:
                raise ConfigurationError("set_rigol_output wymaga channel 1 albo 2.")
            enabled = self._require_boolean(data, "enabled", node.id)
            self._assert_output_action_allowed("rigol", enabled)
            payload = {"channel": channel, "enabled": enabled}
        elif node.type == "arm_rigol_output":
            channel = int(data.get("channel", 0))
            if channel not in {1, 2}:
                raise ConfigurationError("arm_rigol_output wymaga channel 1 albo 2.")
            self._assert_output_action_allowed("rigol", True)
            payload = {"channel": channel}
        elif node.type == "set_keithley_output":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("set_keithley_output wymaga channel A albo B.")
            enabled = self._require_boolean(data, "enabled", node.id)
            self._assert_output_action_allowed("keithley", enabled)
            payload = {"channel": channel, "enabled": enabled}
        elif node.type == "ramp_keithley_to_zero":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("ramp_keithley_to_zero wymaga channel A albo B.")
            deadline = self._resolve_quantity(data.get("deadline", "10 s"), DIMENSION_TIME, context).si_value
            if deadline <= 0 or deadline > 120:
                raise SafetyViolation("Deadline rampy Keithley musi być w zakresie (0, 120] s.")
            payload = {"channel": channel, "deadline_s": deadline}
        elif node.type == "arm_keithley_output":
            channel = str(data.get("channel", ""))
            if channel not in {"A", "B"}:
                raise ConfigurationError("arm_keithley_output wymaga channel A albo B.")
            self._assert_output_action_allowed("keithley", True)
            payload = {"channel": channel}
        else:
            raise ConfigurationError(f"{node.id}: nieobsługiwany typ akcji {node.type!r}.")
        if is_finally:
            safe_finally_actions = {"ramp_keithley_to_zero", "set_rigol_output", "set_keithley_output"}
            if node.type not in safe_finally_actions:
                raise SafetyViolation("Sekcja finally może zawierać tylko rampę Keithley albo wyłączenie wyjścia.")
            if node.type in {"set_rigol_output", "set_keithley_output"} and payload["enabled"]:
                raise SafetyViolation("Sekcja finally nie może włączać wyjść.")
        return PlanAction(node.id, node.type, payload, setpoints, is_finally=is_finally)

    @staticmethod
    def _require_boolean(data: dict[str, Any], key: str, node_id: str) -> bool:
        value = data.get(key)
        if not isinstance(value, bool):
            raise ConfigurationError(f"{node_id}: {key} musi być wartością true albo false, nie tekstem.")
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
            raise SafetyViolation("Receptura nie może włączyć wyjścia: profil nie jest zatwierdzony.")
        permitted = self._settings.rigol.safety.allow_output_enable if device == "rigol" else self._settings.keithley.safety.allow_output_enable
        if not permitted:
            raise SafetyViolation(f"Receptura nie może włączyć {device}: allow_output_enable=false.")

    def _compile_rigol(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            channel = int(data["channel"])
            settings = self._settings.rigol.safety.channels[str(channel)]
        except (KeyError, ValueError) as exc:
            raise ConfigurationError("configure_rigol wymaga poprawnego kanału 1 lub 2.") from exc
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
        )
        return {"config": config}

    def _compile_keithley(self, data: dict[str, Any], node_id: str) -> dict[str, Any]:
        channel = str(data.get("channel", ""))
        mode = str(data.get("mode", ""))
        if channel not in {"A", "B"} or mode not in {"current", "voltage", "measure_only"}:
            raise ConfigurationError("configure_keithley wymaga channel A/B i mode current/voltage/measure_only.")
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
