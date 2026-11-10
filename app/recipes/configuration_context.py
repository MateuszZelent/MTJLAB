"""Read-only, authored recipe context; never a hardware-state prediction.

Values retain their authored spelling and units. Missing initial values stay
absent. An unsupported mutation or multiple possible incoming values produces
``ConfigurationValue(None, node.id, varying=True)`` instead of a default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.recipes.block_registry import ACTION_FIELDS
from app.recipes.models import Recipe, RecipeNode
from app.recipes.parameter_registry import PARAMETERS_BY_TARGET


@dataclass(frozen=True)
class ConfigurationValue:
    value: object | None
    source: str
    varying: bool = False


_State = dict[str, ConfigurationValue]
_KEITHLEY_NAMES = {"mode": "source_mode", "level": "source_level", "settle_time": "settling_time"}
_RIGOL_OUTPUT_NAMES = {"polarity": "output_polarity", "mode": "output_mode"}
_KEITHLEY_PARAMETERS = {
    "source.level": "source_level",
    "source.compliance": "compliance",
    "source.range": "source_range",
    "measurement.nplc": "nplc",
    "measurement.settling_time": "settling_time",
    "measurement.sense_mode": "sense_mode",
    "measurement.voltage_range": "measure_voltage_range",
    "measurement.current_range": "measure_current_range",
}
_RANGE_FLAGS = {
    "source_range": "source_autorange",
    "measure_voltage_range": "measure_voltage_autorange",
    "measure_current_range": "measure_current_autorange",
}
_PASSIVE = {
    "comment",
    "wait",
    "checkpoint",
    "measure_keithley",
    "measure_moke_hall",
    "measure_lakeshore_field",
    "acquire_reference",
    "acquire_spectrum",
    "upload_to_elab",
    "upload_elab",
}


def _unknown(source: str) -> ConfigurationValue:
    return ConfigurationValue(None, source, True)


def _authored(value: object, source: str) -> ConfigurationValue:
    if (
        value is None
        or not isinstance(value, (str, int, float, bool))
        or isinstance(value, str)
        and (not value.strip() or "${" in value)
        or isinstance(value, float)
        and not math.isfinite(value)
    ):
        return _unknown(source)
    return ConfigurationValue(value, source)


def _merge(left: _State, right: _State, source: str) -> _State:
    merged = {}
    for key in left.keys() | right.keys():
        a, b = left.get(key), right.get(key)
        if (
            a is not None
            and b is not None
            and not a.varying
            and not b.varying
            and type(a.value) is type(b.value)
            and a.value == b.value
        ):
            merged[key] = ConfigurationValue(a.value, a.source if a.source == b.source else source)
        else:
            merged[key] = _unknown(source)
    return merged


class _Reached(Exception):
    def __init__(self, state: _State):
        self.state = dict(state)


class _Resolver:
    def __init__(self, node_id: str, device: str, channel: str | int | None):
        self.node_id = node_id
        self.device = device
        self.channel = None if channel is None else str(channel)
        self.path: set[str] = set()

    def locate(self, node: RecipeNode) -> bool:
        if node.id == self.node_id or any(
            self.locate(child) for child in (*node.children, *node.else_children)
        ):
            self.path.add(node.id)
            return True
        return False

    def matches(self, device: str, channel: object = None) -> bool:
        return device == self.device and (
            device not in {"keithley", "rigol", "moke_box"}
            or channel is not None
            and str(channel) == self.channel
        )

    def all_unknown(self, state: _State, source: str) -> _State:
        # Invalidate even when a new/unsupported action has no known field map.
        return {key: _unknown(source) for key in state}

    def dependent_ranges(self, updates: _State, state: _State, source: str) -> None:
        """Invalidate dependent numeric values, preserving authored preferences.

        Keithley measurement ranges and autorange flags are logical preferences.
        Source coupling changes the effective numeric measurement range, shown
        separately by review consumers, without replacing those preferences.
        """
        if self.device == "anritsu":
            for mode_key, value_key in (
                ("rbw_mode", "rbw"),
                ("vbw_mode", "vbw"),
                ("attenuation_mode", "attenuation"),
                ("sweep_time_mode", "sweep_time"),
            ):
                mode = updates.get(mode_key)
                if (
                    mode
                    and (mode.varying or str(mode.value).lower() != "manual")
                    and value_key in state
                ):
                    updates[value_key] = _unknown(source)
            return
        if self.device != "keithley":
            return
        source_auto = updates.get("source_autorange", state.get("source_autorange"))
        if (
            {"source_level", "source_autorange"}.intersection(updates)
            and (source_auto is None or source_auto.varying or source_auto.value is not False)
            and "source_range" in state
            and "source_range" not in updates
        ):
            updates["source_range"] = _unknown(source)

    def parameter(self, parameter: str) -> str | None:
        if self.device == "keithley":
            return _KEITHLEY_PARAMETERS.get(parameter)
        prefix = {"rigol": "carrier.", "anritsu": "spectrum.", "anritsu_sg": "sg."}.get(
            self.device, ""
        )
        leaf = parameter.removeprefix(prefix) if prefix and parameter.startswith(prefix) else ""
        target = {
            "rigol": f"rigol.{self.channel}.{leaf}",
            "anritsu": f"anritsu.spectrum.{leaf}",
            "anritsu_sg": f"anritsu.sg.{leaf}",
        }.get(self.device)
        if target in PARAMETERS_BY_TARGET:
            return leaf
        if self.device == "anritsu":
            if parameter == "spectrum.points":
                return "points"
            if (
                parameter.startswith("advanced.")
                and parameter[9:] in ACTION_FIELDS["configure_anritsu_advanced"]
            ):
                return parameter[9:]
        return None

    def parameter_updates(self, action: dict, node: RecipeNode, state: _State) -> _State:
        key = self.parameter(str(action.get("parameter_id", "")))
        if key is None:
            return self.all_unknown(state, node.id)
        value = (
            _unknown(node.id)
            if action.get("mode") == "sweep"
            else _authored(action.get("value"), node.id)
        )
        if self.device == "rigol" and key in {"amplitude", "offset"}:
            # Both physical levels move. Leave the calculation to the compiler.
            return {name: _unknown(node.id) for name in ("high_level", "low_level")}
        updates = {key: value}
        if key in _RANGE_FLAGS:
            updates[_RANGE_FLAGS[key]] = (
                _unknown(node.id)
                if value.varying
                else ConfigurationValue(str(value.value).strip().upper() == "AUTO", node.id)
            )
        self.dependent_ranges(updates, state, node.id)
        return updates

    def native_matches(self, node: RecipeNode) -> bool:
        configuration = node.data.get("configuration")
        configuration = configuration if isinstance(configuration, dict) else {}
        return self.matches(
            str(node.data.get("device_module", "")),
            configuration.get("channel", node.data.get("channel")),
        )

    def actions(self, node: RecipeNode) -> list[dict]:
        raw = node.data.get("parameter_actions", [])
        return (
            [action for action in raw if isinstance(action, dict)] if isinstance(raw, list) else []
        )

    def effects(self, node: RecipeNode, state: _State) -> _State:
        kind, data = node.type, node.data
        if kind == "sequence":
            if not self.native_matches(node):
                if data.get("device_module") == self.device:
                    configuration = data.get("configuration")
                    configuration = configuration if isinstance(configuration, dict) else {}
                    channel = configuration.get("channel", data.get("channel"))
                    valid = {
                        "keithley": {"A", "B"},
                        "rigol": {"1", "2"},
                        "moke_box": {str(i) for i in range(8)},
                    }.get(self.device, set())
                    if str(channel) not in valid:
                        return self.all_unknown(state, node.id)
                return {}
            if data.get("operation") != "configure_selected_parameters":
                return self.all_unknown(state, node.id)
            raw_actions = data.get("parameter_actions", [])
            if not isinstance(raw_actions, list) or any(
                not isinstance(a, dict) for a in raw_actions
            ):
                return self.all_unknown(state, node.id)
            updates: _State = {}
            for action in self.actions(node):
                if action.get("mode") == "set":
                    updates.update(self.parameter_updates(action, node, {**state, **updates}))
            if (
                self.device == "keithley"
                and any(a.get("mode") in {"set", "sweep"} for a in self.actions(node))
                and data.get("output_policy") != "continue"
            ):
                updates["output_enabled"] = ConfigurationValue(False, node.id)
            policy = data.get("output_policy")
            if policy in {"on", "on_keep", "off"}:
                updates["output_enabled"] = ConfigurationValue(policy != "off", node.id)
            return updates
        if kind == "sweep":
            target = str(data.get("target", ""))
            descriptor = PARAMETERS_BY_TARGET.get(target)
            if descriptor is None:
                return self.all_unknown(state, node.id)
            device, endpoint, leaf = target.split(".", 2)
            device = "anritsu_sg" if device == "anritsu" and endpoint == "sg" else device
            if not self.matches(device, endpoint):
                return {}
            key = {
                "level": "source_level",
                "current": "source_level",
                "voltage": "source_level",
                "compliance_current": "compliance",
                "compliance_voltage": "compliance",
            }.get(leaf, leaf)
            if self.device == "rigol" and key in {"amplitude", "offset"}:
                return {key: _unknown(node.id) for key in ("high_level", "low_level")}
            updates = {key: _unknown(node.id)}
            self.dependent_ranges(updates, state, node.id)
            return updates
        if kind in _PASSIVE or kind in {"repeat", "if"}:
            return {}
        if kind == "final_state":
            if not self.matches(str(data.get("device")), data.get("channel")):
                return {}
            names = {"level": "source_level"} if self.device == "keithley" else {}
            updates = {
                names.get(key, key): _authored(value, node.id)
                for key, value in data.items()
                if key in {"level", "voltage", "frequency", "high_level", "low_level"}
            }
            if data.get("output") == "off":
                updates["output_enabled"] = ConfigurationValue(False, node.id)
            return updates
        device = (
            "anritsu_sg"
            if "anritsu_sg" in kind
            else next(
                (name for name in ("keithley", "rigol", "anritsu", "moke_box") if name in kind),
                str(data.get("device", "")),
            )
        )
        if kind not in ACTION_FIELDS and not device:
            return self.all_unknown(state, node.id)
        if device != self.device:
            return {}
        if not self.matches(device, data.get("channel")):
            # A malformed/dynamic endpoint may address this channel.
            if isinstance(data.get("channel"), (str, int)) and str(data["channel"]) in {
                "A",
                "B",
                "0",
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
                "7",
            }:
                return {}
            return self.all_unknown(state, node.id)
        if kind not in ACTION_FIELDS or kind == "connect":
            return self.all_unknown(state, node.id)
        if kind == "ramp_keithley_to_zero":
            mode = state.get("source_mode")
            zero = {"current": "0 A", "voltage": "0 V"}.get(str(mode.value) if mode else "")
            updates = {
                "source_level": _authored(zero, node.id),
                "output_enabled": ConfigurationValue(False, node.id),
            }
            self.dependent_ranges(updates, state, node.id)
            return updates
        if kind.startswith(("set_", "enable_")) and "output" in kind:
            enabled = True if kind.startswith("enable_") else data.get("enabled")
            return {"output_enabled": _authored(enabled, node.id)}
        names = (
            _KEITHLEY_NAMES
            if device == "keithley"
            else _RIGOL_OUTPUT_NAMES
            if kind == "configure_rigol_output"
            else {}
        )
        updates = {
            names.get(key, key): _authored(value, node.id)
            for key, value in data.items()
            if key in ACTION_FIELDS[kind]
            and key
            not in {"channel", "trace", "mode" if kind.startswith("update_keithley") else "channel"}
        }
        if kind == "configure_keithley":
            # A new source dimension makes omitted dimension-dependent values unknown.
            old_mode, mode = state.get("source_mode"), updates.get("source_mode")
            if mode and (old_mode is None or old_mode.varying or old_mode.value != mode.value):
                for key in ("source_level", "compliance", "source_range", "source_autorange"):
                    if key in state and key not in updates:
                        updates[key] = _unknown(node.id)
            if "settle_time" in data:
                updates["settling_time"] = _authored(data["settle_time"], node.id)
            if "source_range" in data:
                # Literal range writes also select source_autorange in the
                # compiler. An omitted flag comes from station settings even
                # with a preceding request; the merge cannot preserve it.
                # With neither field selected, the merge does preserve both.
                if "source_autorange" not in data:
                    updates["source_autorange"] = _unknown(node.id)
                policy = updates["source_autorange"]
                if (
                    policy.varying
                    or policy.value is not False
                    or data.get("mode") == "measure_only"
                ):
                    updates["source_range"] = _unknown(node.id)
            updates["output_enabled"] = ConfigurationValue(False, node.id)
        elif kind == "configure_rigol":
            updates["output_enabled"] = ConfigurationValue(False, node.id)
        self.dependent_ranges(updates, state, node.id)
        return updates

    def children(
        self, nodes: tuple[RecipeNode, ...], state: _State, *, seek: bool, active: bool = True
    ) -> tuple[_State, set[str]]:
        writes: set[str] = set()
        for child in nodes:
            state, touched = self.walk(child, state, seek=seek, active=active)
            writes.update(touched)
        return state, writes

    def walk(
        self, node: RecipeNode, state: _State, *, seek: bool, active: bool = True
    ) -> tuple[_State, set[str]]:
        if seek and node.id == self.node_id:
            raise _Reached(state)
        if not active or node.data.get("disabled") is True:
            if seek and node.id in self.path:
                self.children((*node.children, *node.else_children), state, seek=True, active=False)
            return state, set()
        if node.type == "if":
            if seek and node.id in self.path:
                branch = (
                    node.children
                    if any(c.id in self.path for c in node.children)
                    else node.else_children
                )
                return self.children(branch, state, seek=True)
            condition = node.data.get("condition")
            if type(condition) is bool:
                return self.children(
                    node.children if condition else node.else_children, state, seek=seek
                )
            left, a = self.children(node.children, dict(state), seek=False)
            right, b = self.children(node.else_children, dict(state), seek=False)
            return _merge(left, right, node.id), a | b

        updates = self.effects(node, state)
        state.update(updates)
        writes = set(updates)
        sweeps = (
            [a for a in self.actions(node) if a.get("mode") == "sweep"]
            if node.type == "sequence"
            else []
        )
        loop = (
            node.type == "sweep"
            or bool(sweeps)
            or (
                node.type == "repeat"
                and (type(node.data.get("count")) is not int or node.data["count"] != 1)
            )
        )
        if loop:
            # Collect all body writes, including those following the requested node.
            # They may already have executed on a previous iteration. No expansion.
            _, touched = self.children(node.children, dict(state), seek=False)
            for key in touched:
                state[key] = _unknown(node.id)
            writes.update(touched)
        if self.native_matches(node):
            for action in sweeps:
                patch = self.parameter_updates(action, node, state)
                state.update(patch)
                writes.update(patch)
        state, touched = self.children(node.children, state, seek=seek)
        writes.update(touched)
        if loop:
            for key in writes:
                state[key] = _unknown(node.id)
        if node.type == "sequence" and self.native_matches(node):
            policy = node.data.get("output_policy")
            if policy == "on":
                state["output_enabled"] = ConfigurationValue(False, node.id)
                writes.add("output_enabled")
        return state, writes


def configuration_before(
    recipe: Recipe,
    node_id: str,
    device: str,
    channel: str | int | None = None,
) -> dict[str, ConfigurationValue]:
    """Return snapshot-named authored values incoming to a recipe node.

    Ancestor operations precede their children; finally targets follow the root
    and earlier finally nodes. Loops conservatively widen touched fields, while
    equal branch values remain known. Disabled subtrees contribute no writes.
    The target and later unrelated nodes are never validated or compiled, so an
    incomplete recipe remains editable. An unknown node ID raises ``KeyError``.

    This is offline authoring context, not safety validation or readback. Values
    are raw authored scalars; dependent/unsupported/dynamic values are unknown.
    Keithley measurement-range preferences and autorange policies remain logical
    settings; effective source-coupled numeric ranges are derived separately.
    ``output_enabled`` additionally describes explicit output transitions.
    """
    resolver = _Resolver(node_id, device, channel)
    nodes = (recipe.root, *recipe.finally_nodes)
    if not any(resolver.locate(node) for node in nodes):
        raise KeyError(node_id)
    try:
        resolver.children(nodes, {}, seek=True)
    except _Reached as reached:
        return reached.state
    raise KeyError(node_id)
