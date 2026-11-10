"""Pure, non-executing overview of every authored sweep/device setting.

``card_value`` always comes from the current device tab. ``sweep_value`` is an
authored request, an explicit execution default, a compact axis description,
or the incoming preserved value.
Saved module configurations are drafts: only selected set/sweep actions write.
Actions are lowercase: set, sweep, preserve, derived, acquire, output, final, timing.
This is an authoring review, never a compiled plan or hardware-state prediction.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass

from app.domain.errors import ConfigurationError
from app.domain.quantities import QuantityError, parse_quantity
from app.recipes.block_registry import ACTION_FIELDS, block_for_node
from app.recipes.configuration_context import configuration_before
from app.recipes.models import Recipe, RecipeNode
from app.recipes.parameter_registry import PARAMETERS_BY_TARGET
from app.recipes.sweep_points import estimate_sweep_point_count


@dataclass(frozen=True)
class SweepParameterEntry:
    device: str
    channel: str | int | None
    parameter: str
    label: str
    card_value: object | None
    sweep_value: object | None
    action: str
    source_id: str
    source_label: str
    enabled: bool = True
    detail: str = ""


_KEITHLEY_ALIASES = {
    "mode": "source_mode",
    "level": "source_level",
    "current": "source_level",
    "voltage": "source_level",
    "settle_time": "settling_time",
    "source.mode": "source_mode",
    "source.level": "source_level",
    "source.compliance": "compliance",
    "source.range": "source_range",
    "compliance_voltage": "compliance",
    "compliance_current": "compliance",
    "measurement.nplc": "nplc",
    "measurement.settling_time": "settling_time",
    "measurement.sense_mode": "sense_mode",
    "measurement.voltage_range": "measure_voltage_range",
    "measurement.current_range": "measure_current_range",
}
_STRUCTURAL = {
    "channel",
    "device",
    "device_module",
    "label",
    "description",
    "disabled",
    "text",
    "block_type",
    "operation",
    "configuration",
    "parameter_actions",
    "binding",
    "target",
    "segments",
    "managed_acquisition_id",
    "post_configuration_operation",
}
_NATIVE_METADATA = _STRUCTURAL | {"source_mode", "output_policy"}
_TIME_FIELDS = {
    "duration",
    "deadline",
    "settling_time",
    "inter_sweep_delay",
    "minimum_duration",
    "sweep_time",
    "pulse_width",
    "pulse_leading",
    "pulse_trailing",
    "sync_delay",
}
_LABELS = {
    "source_mode": "Source mode",
    "source_level": "Source level",
    "source_range": "Source range",
    "source_autorange": "Source autorange",
    "output_enabled": "Output enabled",
    "nplc": "Measurement NPLC",
    "rbw": "Resolution bandwidth (RBW)",
    "vbw": "Video bandwidth (VBW)",
    "output_policy": "Output policy",
    "acquisition": "Acquisition",
    "output": "Final output policy",
    "effective_measure_current_range": "Effective current measurement range",
    "effective_measure_voltage_range": "Effective voltage measurement range",
}
_PRESERVED_SWEEP = "Preserved sweep axis:"


def _fixed_source_measurement_range(values: Mapping, dimension: str) -> object | None:
    """Source-coupled range intent, requiring explicit fixed policy and dimension."""
    if values.get("source_mode") != dimension or values.get("source_autorange") is not False:
        return None
    value = values.get("source_range")
    try:
        quantity = parse_quantity(value, dimension)
    except (QuantityError, TypeError, ValueError):
        return None
    return value if quantity.si_value > 0 else None


def _canonical(device: str, key: str) -> str:
    """Use snapshot keys, retaining unrecognized future keys verbatim."""
    if key.startswith(device + "."):
        key = key[len(device) + 1 :]
    if device == "keithley":
        return _KEITHLEY_ALIASES.get(key, key)
    prefixes = {
        "rigol": ("carrier.",),
        "anritsu": ("spectrum.", "advanced."),
        "anritsu_sg": ("anritsu.sg.", "sg."),
    }.get(device, ())
    for prefix in prefixes:
        if key.startswith(prefix):
            key = key[len(prefix) :]
            break
    if device == "rigol":
        key = {"polarity": "output_polarity", "mode": "output_mode"}.get(key, key)
    return key


def _flatten(values: Mapping, prefix: str = "") -> dict[str, object]:
    result = {}
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value:
            result.update(_flatten(value, name))
        else:
            result[name] = deepcopy(value)
    return result


def _snapshot(device: str, values: Mapping) -> dict[str, object]:
    result = {}
    # Prefer an explicit snapshot key when both it and an alias are present.
    flat = _flatten(values)
    for key, value in flat.items():
        canonical = _canonical(device, key)
        if canonical not in result or canonical == key:
            result[canonical] = value
    return result


def _endpoint(target: str) -> tuple[str, str | int | None, str]:
    descriptor = PARAMETERS_BY_TARGET.get(target)
    parts = target.split(".", 2)
    device = descriptor.device_module if descriptor else parts[0]
    endpoint = parts[1] if len(parts) > 1 else ""
    key = parts[2] if len(parts) > 2 else target
    if device == "anritsu" and endpoint == "sg":
        device = "anritsu_sg"
    channel: str | int | None = None
    if device == "keithley":
        channel = endpoint
    elif device == "rigol":
        channel = int(endpoint) if endpoint.isdigit() else endpoint
    elif device == "moke_box":
        raw_channel = endpoint.removeprefix("vout")
        channel = int(raw_channel) if raw_channel.isdigit() else raw_channel
    return device, channel, _canonical(device, key)


def _device(node: RecipeNode) -> tuple[str, str | int | None]:
    data, kind = node.data, node.type
    if kind == "sweep":
        return _endpoint(str(data.get("target", "unknown")))[:2]
    try:
        module = block_for_node(kind, data).device_module
    except (ConfigurationError, ValueError, KeyError):
        module = None
    module = module or data.get("device_module") or data.get("device")
    if not module:
        module = next(
            (
                name
                for name in ("anritsu_sg", "keithley", "rigol", "anritsu", "lakeshore")
                if name in kind
            ),
            None,
        )
    if not module and "moke" in kind:
        module = "moke_box"
    if kind in {"acquire_spectrum", "acquire_reference"}:
        module = "anritsu"
    configuration = data.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    channel = data.get("channel", configuration.get("channel"))
    if module == "rigol" and isinstance(channel, str) and channel.isdigit():
        channel = int(channel)
    return str(module or "sequence"), channel


def _dimension(device: str, channel: object, key: str, mode: object = None) -> str | None:
    if device == "keithley":
        if key in {"source_level", "source_range"}:
            return mode if mode in {"current", "voltage"} else None
        if key == "compliance":
            return {"current": "voltage", "voltage": "current"}.get(str(mode))
        if key == "measure_current_range":
            return "current"
        if key == "measure_voltage_range":
            return "voltage"
    target = {
        "keithley": f"keithley.{channel}.{key}",
        "rigol": f"rigol.{channel}.{key}",
        "anritsu": f"anritsu.spectrum.{key}",
        "anritsu_sg": f"anritsu.sg.{key}",
        "moke_box": f"moke_box.vout{channel}.{key}",
    }.get(device)
    descriptor = PARAMETERS_BY_TARGET.get(target)
    if descriptor:
        return descriptor.dimension
    leaf = key.rsplit(".", 1)[-1]
    if leaf in _TIME_FIELDS:
        return "time"
    if device == "anritsu" and leaf in {"rbw", "vbw"}:
        return "frequency"
    if device == "anritsu" and leaf == "attenuation":
        # Authored attenuation uses a plain integer dB count, unlike quantities.
        return None
    if device == "rigol" and leaf in {"amplitude", "offset", "high_level", "low_level"}:
        return "voltage"
    if device == "moke_box" and leaf in {"voltage", "minimum_voltage", "maximum_voltage"}:
        return "voltage"
    return None


def _invalid_value(value: object, dimension: str | None, key: str = "") -> str:
    if value is None:
        return "Value unavailable"
    if isinstance(value, float) and not math.isfinite(value):
        return "Invalid value: must be finite"
    if isinstance(value, str) and "${" in value:
        return "Value depends on execution context"
    if key in {"average_count", "acquisition_average_count", "points", "count"} and (
        type(value) is not int or value < 1
    ):
        return f"Invalid {key}: requires a positive integer"
    if (
        key
        in {
            "store_raw",
            "store_processed",
            "checkpoint",
            "sync_enabled",
            "preamplifier_enabled",
            "output_enabled",
            "source_autorange",
            "measure_voltage_autorange",
            "measure_current_autorange",
        }
        and type(value) is not bool
    ):
        return f"Invalid {key}: requires a boolean"
    if key in {"nplc", "phase_deg", "square_duty_percent", "ramp_symmetry_percent"}:
        try:
            numeric = parse_quantity(value, "ratio", require_unit=False).si_value
            if key == "nplc" and numeric <= 0:
                return "Invalid nplc: requires a positive number"
        except (QuantityError, TypeError, ValueError) as exc:
            return f"Invalid {key}: {exc}"
    auto_range = (
        key in {"source_range", "measure_voltage_range", "measure_current_range"}
        and value == "AUTO"
    )
    if dimension and not auto_range:
        try:
            parse_quantity(value, dimension)
        except (QuantityError, TypeError, ValueError) as exc:
            return f"Invalid {dimension} value {value!r}: {exc}"
    return ""


def entry_comparison(entry: SweepParameterEntry) -> str:
    """Return same/different/unavailable, with strict finite SI comparisons.

    Disabled requests and missing values never receive an affirmative equality.
    A variable axis is different even if its first value matches the device tab.
    """
    if not entry.enabled:
        return "unavailable"
    if entry.action.lower() == "sweep":
        return "different"
    if entry.action.lower() == "preserve" and _PRESERVED_SWEEP in entry.detail:
        return "different"
    left, right = entry.card_value, entry.sweep_value
    if left is None or right is None or "Invalid " in entry.detail:
        return "unavailable"
    if isinstance(left, bool) or isinstance(right, bool):
        return "same" if type(left) is type(right) and left == right else "different"
    parsed = []
    for value in (left, right):
        try:
            parsed.append(parse_quantity(value))
        except (QuantityError, TypeError, ValueError):
            parsed.append(None)
    if any(q is not None for q in parsed):
        a, b = parsed
        if a is None or b is None:
            return "unavailable"
        return (
            "same"
            if a.dimension == b.dimension
            and math.isclose(a.si_value, b.si_value, rel_tol=1e-12, abs_tol=0.0)
            else "different"
        )
    # Counts and unitted numeric editors (NPLC, phase, duty cycle) are numeric.
    try:
        a = parse_quantity(left, require_unit=False)
        b = parse_quantity(right, require_unit=False)
    except (QuantityError, TypeError, ValueError):
        if any(isinstance(v, float) and not math.isfinite(v) for v in (left, right)):
            return "unavailable"
        if isinstance(left, str) and "${" in left or isinstance(right, str) and "${" in right:
            return "unavailable"
        return "same" if type(left) is type(right) and left == right else "different"
    if isinstance(left, int) and isinstance(right, int):
        return "same" if left == right else "different"
    return (
        "same" if math.isclose(a.si_value, b.si_value, rel_tol=1e-12, abs_tol=0.0) else "different"
    )


def _axis(data: dict, dimension: str | None) -> tuple[str, str]:
    segments = data.get("segments")
    if segments is None:
        segments = [
            {
                key: data[key]
                for key in ("start", "stop", "points", "step", "spacing")
                if key in data
            }
        ]
    descriptions = []
    if isinstance(segments, (list, tuple)):
        for segment in segments:
            if not isinstance(segment, dict):
                descriptions.append(repr(segment))
            elif "value" in segment:
                descriptions.append(str(segment["value"]))
            else:
                text = f"{segment.get('start', '?')} .. {segment.get('stop', '?')}"
                text += f"; {segment.get('spacing', 'linear')}"
                if "step" in segment:
                    text += f"; step {segment['step']}"
                if "points" in segment:
                    text += f"; {segment['points']} points"
                descriptions.append(text)
    else:
        descriptions.append(repr(segments))
    summary = " | ".join(descriptions)
    try:
        if dimension is None:
            raise ValueError("Unknown sweep target/dimension")
        if not isinstance(segments, (list, tuple)):
            raise TypeError("segments must be a list")
        count = estimate_sweep_point_count(segments, dimension)
        return summary, f"Variable sweep: {summary}; {count} points (axis total)"
    except (ConfigurationError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return summary, f"Invalid sweep: {summary}; {exc}"


class _Overview:
    def __init__(self, recipe: Recipe, provider: Callable):
        self.recipe = recipe
        self.provider = provider
        self.cards: dict[tuple, tuple[dict, str]] = {}
        self.entries: list[SweepParameterEntry] = []
        self.nodes: dict[str, RecipeNode] = {}
        pending = [recipe.root, *recipe.finally_nodes]
        while pending:
            node = pending.pop()
            self.nodes[node.id] = node
            pending.extend((*node.children, *node.else_children))

    def inherited_axis(
        self, source: str, device: str, channel: str | int | None, key: str
    ) -> tuple[str, str] | None:
        """Describe only an exact axis origin, never general loop/branch widening."""
        node = self.nodes.get(source)
        if node is None:
            return None
        axis = None
        dimension = None
        if node.type == "sweep":
            target = str(node.data.get("target", ""))
            descriptor = PARAMETERS_BY_TARGET.get(target)
            if descriptor and _endpoint(target) == (device, channel, key):
                axis, dimension = node.data, descriptor.dimension
        elif (
            node.type == "sequence"
            and node.data.get("operation") == "configure_selected_parameters"
        ):
            if _device(node) != (device, channel):
                return None
            configuration = node.data.get("configuration")
            configuration = configuration if isinstance(configuration, dict) else {}
            mode = configuration.get("source_mode", node.data.get("source_mode"))
            actions = node.data.get("parameter_actions", [])
            if isinstance(actions, (list, tuple)):
                for action in actions:
                    if (
                        isinstance(action, dict)
                        and action.get("mode") == "sweep"
                        and _canonical(device, str(action.get("parameter_id", ""))) == key
                    ):
                        axis, dimension = action, _dimension(device, channel, key, mode)
                        break
        if axis is None:
            return None
        summary, detail = _axis(axis, dimension)
        if detail.startswith("Invalid sweep:"):
            return None
        return f"Sweep {summary}", f"{_PRESERVED_SWEEP} {source}; {detail}"

    def card(self, device: str, channel: str | int | None) -> tuple[dict, str]:
        key = device, channel
        if key not in self.cards:
            try:
                values = self.provider(device, channel)
                if not isinstance(values, Mapping):
                    raise TypeError("snapshot is not a mapping")
                self.cards[key] = _snapshot(device, values), ""
            except Exception as exc:  # noqa: BLE001 - external read-only tab provider
                # A closed or incomplete device page must not prevent review.
                self.cards[key] = {}, f"Device tab unavailable: {exc}"
        return self.cards[key]

    def visit(self, node: RecipeNode, active: bool = True, detail: str = "", final: bool = False):
        active = active and node.data.get("disabled") is not True
        device, channel = _device(node)
        card, card_error = self.card(device, channel) if device != "sequence" else ({}, "")
        incoming = (
            configuration_before(self.recipe, node.id, device, channel)
            if device != "sequence"
            else {}
        )
        data = node.data
        configuration = data.get("configuration")
        draft = _snapshot(device, configuration) if isinstance(configuration, Mapping) else {}
        mode = data.get("mode", data.get("source_mode", draft.get("source_mode")))
        if mode is None:
            previous = incoming.get("source_mode")
            mode = previous.value if previous and not previous.varying else card.get("source_mode")
        base_detail = [detail, card_error]
        if not active:
            base_detail.append("Disabled: this node/branch does not execute")
        if node.type == "final_state":
            base_detail.append(
                "Success-only final state; authored intent, not confirmed hardware state"
            )
        elif final:
            base_detail.append(
                "Finally cleanup; success-only final states may supersede this on success"
            )
        written: set[str] = set()
        planned_values: dict[str, object] = {}
        card_overrides: dict[str, object] = {}

        def add(key: str, value: object, action: str, extra: str = "", *, validate: bool = True):
            key = _canonical(device, key)
            parts = [*base_detail, extra]
            card_key = key
            if key == "average_count" and action == "acquire":
                if node.type == "acquire_reference":
                    card_key = "reference_average_count"
                    parts.append(
                        "Current device tab: reference_average_count (reference averaging)"
                    )
                elif node.type == "acquire_spectrum":
                    parts.append("Current device tab: average_count (spectrum averaging)")
            previous = incoming.get(key)
            if previous:
                context = (
                    "varies with sweep axis"
                    if _PRESERVED_SWEEP in extra
                    else "varies/unknown"
                    if previous.varying
                    else str(previous.value)
                )
                parts.append(f"Earlier recipe: {previous.source}: {context}")
            if validate and action != "sweep":
                parts.append(_invalid_value(value, _dimension(device, channel, key, mode), key))
            self.entries.append(
                SweepParameterEntry(
                    device,
                    channel,
                    key,
                    _LABELS.get(key, key.replace("_", " ").replace(".", " / ").capitalize()),
                    deepcopy(card_overrides.get(key, card.get(card_key))),
                    deepcopy(value),
                    action,
                    node.id,
                    f"{data.get('label') or node.type.replace('_', ' ').capitalize()} ({node.id})",
                    active,
                    "; ".join(part for part in parts if part),
                )
            )
            written.add(key)
            planned_values[key] = value

        def output(value: object, extra: str = ""):
            add("output_enabled", value, "final" if final else "output", extra)

        native = node.type == "sequence" and device != "sequence"
        if node.type == "sweep":
            target = str(data.get("target", ""))
            descriptor = PARAMETERS_BY_TARGET.get(target)
            _, _, key = _endpoint(target)
            summary, axis_detail = _axis(data, descriptor.dimension if descriptor else None)
            add(key, summary, "sweep", axis_detail)
        elif native and data.get("operation") == "configure_selected_parameters":
            actions = data.get("parameter_actions", [])
            if isinstance(actions, (list, tuple)):
                for action in actions:
                    if not isinstance(action, dict) or action.get("mode") not in {"set", "sweep"}:
                        continue
                    key = _canonical(device, str(action.get("parameter_id", "unknown_parameter")))
                    if action["mode"] == "sweep":
                        summary, axis_detail = _axis(action, _dimension(device, channel, key, mode))
                        add(key, summary, "sweep", axis_detail)
                    else:
                        add(key, action.get("value"), "set")
                        if key in {
                            "source_range",
                            "measure_voltage_range",
                            "measure_current_range",
                        }:
                            flag = {
                                "source_range": "source_autorange",
                                "measure_voltage_range": "measure_voltage_autorange",
                                "measure_current_range": "measure_current_autorange",
                            }[key]
                            add(
                                flag,
                                str(action.get("value")).upper() == "AUTO",
                                "set",
                                "Range action selects autorange",
                            )
            else:
                add(
                    "parameter_actions",
                    actions,
                    "set",
                    "Invalid parameter_actions: expected a list",
                    validate=False,
                )
            if "output_policy" in data:
                add(
                    "output_policy", data["output_policy"], "output", "Policy for this device block"
                )
                policy = data["output_policy"]
                if policy in {"on", "on_keep", "continue"}:
                    output(
                        True,
                        "Requires confirmed output ON"
                        if policy == "continue"
                        else "Enable for device block",
                    )
                elif policy == "off":
                    output(False, "Explicit output OFF policy")
                elif policy == "unchanged" and device == "keithley" and written - {"output_policy"}:
                    output(False, "Selected configuration executes with output OFF")
        elif node.type == "final_state":
            for key, value in data.items():
                if key in {"device", "channel", "disabled", "label", "description", "block_type"}:
                    continue
                if key == "output" and value == "off":
                    output(False)
                else:
                    add(key, value, "final")
        else:
            fields = ACTION_FIELDS.get(node.type, frozenset())
            for key, value in data.items():
                if key in _STRUCTURAL:
                    continue
                if key in {"comment"}:
                    continue
                if "output" in node.type and key == "enabled":
                    output(value)
                    continue
                action = (
                    "final"
                    if final
                    else "timing"
                    if device == "sequence"
                    else "acquire"
                    if node.type.startswith(("acquire_", "measure_"))
                    else "set"
                )
                for name, item in _flatten({key: value}).items():
                    add(name, item, action, "" if key in fields else "Additional authored field")
            if node.type.startswith("enable_") and "output" in node.type:
                output(True)
            if node.type == "ramp_keithley_to_zero":
                zero = {"current": "0 A", "voltage": "0 V"}.get(str(mode))
                add(
                    "source_level",
                    zero,
                    "final" if final else "set",
                    "Ramp to zero; source dimension unknown" if zero is None else "Ramp to zero",
                )
                output(False, "Ramp completes with output OFF")
            if node.type == "stop_moke_voltage":
                add("voltage", "0 V", "final" if final else "set", "Stop/zero requested VOUT")
            if node.type in {"configure_keithley", "configure_rigol"}:
                output(False, "Configure with output OFF")

        if native:
            # Acquisition flags are authored behavior even when no parameter is selected.
            for key, value in data.items():
                if key in _NATIVE_METADATA or _canonical(device, key) in written:
                    continue
                for name, item in _flatten({key: value}).items():
                    add(name, item, "acquire" if key.startswith("acquisition_") else "set")
            if data.get("post_configuration_operation") not in {None, "configure"}:
                add("post_configuration_operation", data["post_configuration_operation"], "acquire")

        if node.type in {"acquire_spectrum", "acquire_reference"}:
            # Keep aligned with RecipeCompiler._compile_action's acquisition
            # defaults, without importing the compiler or opening reference files.
            defaults: dict[str, object] = {
                "trace": "TRAC1",
                "average_count": 1,
                "inter_sweep_delay": "0 s",
            }
            default_details = {}
            if node.type == "acquire_reference":
                defaults["minimum_duration"] = "0 s"
                if data.get("source_file"):
                    defaults.update(file_kind="reference", average_count=None)
                    default_details["average_count"] = (
                        "Execution default unresolved: averaging count comes from the imported file during compilation"
                    )
                else:
                    defaults["purpose"] = "reference"
            else:
                operation = str(data.get("reference_operation", "none")).strip().lower()
                filters = ()
                processing_error = ""
                if data.get("processing") is not None:
                    # Only inspect whether filters are selected. The processing
                    # parser pulls in hardware adapters through spectrum types;
                    # full policy validation remains the compiler's job.
                    processing = data["processing"]
                    if not isinstance(processing, dict):
                        processing_error = "Invalid processing: expected a mapping"
                    else:
                        filters = processing.get("filters", [])
                        if not isinstance(filters, (list, tuple)) or any(
                            not isinstance(item, str) for item in filters
                        ):
                            processing_error = "Invalid processing: filters must be a list of names"
                defaults.update(
                    reference_operation="none",
                    store_raw=True,
                    store_processed=None
                    if processing_error
                    else operation != "none" or bool(filters),
                )
                if not processing_error:
                    defaults["processing.filters"] = list(filters)
                else:
                    default_details["store_processed"] = (
                        f"Execution default unresolved; {processing_error}"
                    )
            for key, value in defaults.items():
                if key not in written:
                    add(
                        key,
                        value,
                        "acquire",
                        default_details.get(
                            key, f"Execution default: omitted {key} uses {value!r}"
                        ),
                    )

        if node.type.startswith(("acquire_", "measure_")):
            add("acquisition", node.type, "acquire", "Read/acquire at this step", validate=False)

        if device != "sequence":
            for key in dict.fromkeys((*card, *incoming, *draft)):
                if key == "channel" or key in written or key == "output_policy" and key not in data:
                    continue
                previous = incoming.get(key)
                value = previous.value if previous else card.get(key)
                extra = "Preserved; no write selected at this step"
                inherited_axis = None
                if previous and previous.varying:
                    inherited_axis = self.inherited_axis(previous.source, device, channel, key)
                    if inherited_axis is not None:
                        value, axis_detail = inherited_axis
                        extra += f"; {axis_detail}"
                    else:
                        value = None
                        extra += "; execution-dependent incoming value varies or is unknown"
                if key in draft:
                    extra += f"; saved draft (not selected): {draft[key]}"
                add(key, value, "preserve", extra, validate=inherited_axis is None)

        if device == "keithley":
            source_values = {
                "source_mode": planned_values.get("source_mode", mode),
                "source_range": planned_values.get("source_range"),
                "source_autorange": planned_values.get("source_autorange"),
            }
            if (
                node.type == "configure_keithley"
                and "source_range" in data
                and "source_autorange" not in data
            ):
                # This literal's omitted policy is filled from station settings
                # by the compiler, rather than inherited from the current tab.
                source_values["source_autorange"] = None
            source_mode = source_values["source_mode"]
            dimensions = (
                (source_mode,)
                if source_mode in {"current", "voltage"}
                else (() if source_mode == "measure_only" else ("current", "voltage"))
            )
            for dimension in dimensions:
                key = f"effective_measure_{dimension}_range"
                planned_range = _fixed_source_measurement_range(source_values, dimension)
                card_range = _fixed_source_measurement_range(card, dimension)
                card_overrides[key] = card_range
                extra = (
                    f"Derived: measured {dimension} range follows fixed source_range; "
                    f"planned source_mode={source_mode!r}, source_autorange={source_values['source_autorange']!r}, "
                    f"source_range={source_values['source_range']!r}; "
                    f"current device tab source_mode={card.get('source_mode')!r}, "
                    f"source_autorange={card.get('source_autorange')!r}, source_range={card.get('source_range')!r}"
                )
                if planned_range is None:
                    extra += "; execution-dependent planned effective range: AUTO or unknown mode/policy/range"
                if card_range is None:
                    extra += "; execution-dependent device-tab effective range: AUTO or mismatched/unknown mode/policy/range"
                add(key, planned_range, "derived", extra, validate=False)

        # A block policy 'on' switches off after its body. Keep both transitions.
        for child in node.children:
            child_active, branch_detail = active, detail
            if node.type == "if":
                condition = data.get("condition")
                child_active = active and condition is not False
                branch_detail = "; ".join(filter(None, (detail, "Conditional: then branch")))
            self.visit(child, child_active, branch_detail, final)
        for child in node.else_children:
            condition = data.get("condition")
            self.visit(
                child,
                active and condition is not True,
                "; ".join(filter(None, (detail, "Conditional: else branch"))),
                final,
            )
        if native and data.get("output_policy") == "on":
            output(False, "After device block and its children: output OFF")


def build_sweep_parameter_entries(
    recipe: Recipe,
    device_values: Callable[[str, str | int | None], dict],
) -> tuple[SweepParameterEntry, ...]:
    """Collect all node origins without compiling, accessing hardware, or expanding axes.

    The callback is sampled once per participating device/channel and is expected
    to read the current device-tab draft only. Missing tabs remain unavailable.
    Disabled/untaken branches are retained with ``enabled=False``. Conditional
    branches without a static boolean retain their condition in ``detail``.
    """
    overview = _Overview(recipe, device_values)
    overview.visit(recipe.root)
    for node in recipe.finally_nodes:
        overview.visit(node, final=True)
    return tuple(overview.entries)
