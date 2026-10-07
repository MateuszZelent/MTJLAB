"""Visible initial configuration authored from an operator's stored snapshot."""

from app.domain.errors import ConfigurationError
from app.recipes.models import RecipeNode, validate_action_fields


def explicit_baseline_mapping(node: RecipeNode, *, node_id: str) -> dict[str, object]:
    module = node.data.get("device_module")
    snapshot = node.data.get("configuration")
    if node.data.get("operation") != "configure_selected_parameters" or not isinstance(snapshot, dict):
        raise ConfigurationError("Choose a configured device node with stored settings first.")
    mapping = {
        "keithley": ("configure_keithley", {
            "mode": "mode", "level": "level", "settle_time": "settle_time",
            "channel": "channel", "source_mode": "mode", "source_level": "level", "compliance": "compliance",
            "nplc": "nplc", "settling_time": "settle_time", "sense_mode": "sense_mode",
            "source_autorange": "source_autorange", "source_range": "source_range",
            "measure_voltage_autorange": "measure_voltage_autorange", "measure_voltage_range": "measure_voltage_range",
            "measure_current_autorange": "measure_current_autorange", "measure_current_range": "measure_current_range",
        }),
        "rigol": ("configure_rigol", {name: name for name in (
            "channel", "waveform", "frequency", "high_level", "low_level", "output_load", "phase_deg",
            "square_duty_percent", "ramp_symmetry_percent", "pulse_width", "pulse_leading", "pulse_trailing",
        )}),
        "anritsu": ("configure_anritsu", {name: name for name in ("start_frequency", "stop_frequency", "reference_level", "points", "trace")}),
        "anritsu_sg": ("configure_anritsu_sg", {"frequency": "frequency", "power": "power"}),
    }.get(module)
    if mapping is None:
        raise ConfigurationError("This device requires its explicit preparation workflow.")
    kind, fields = mapping
    values = {}
    for source, target in fields.items():
        if source not in snapshot or snapshot[source] is None:
            continue
        value = snapshot[source]
        if target in values and values[target] != value:
            raise ConfigurationError(f"Stored configuration has conflicting aliases for {target}; review it before adding initial settings.")
        values[target] = value
    if "nplc" in values:
        if isinstance(values["nplc"], bool):
            raise ConfigurationError("NPLC requires a numeric value.")
        values["nplc"] = float(str(values["nplc"]).replace(",", "."))
    validate_action_fields(kind, values, node_id)
    return {"id": node_id, "type": kind, "label": f"Initial {module} settings — review before running", **values}
