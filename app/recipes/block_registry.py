"""Authoritative recipe block identities and field contracts, independent of UI."""
from __future__ import annotations
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from app.domain.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class BlockDefinition:
    block_type: str
    node_type: str
    allowed_fields: frozenset[str]
    device_module: str | None = None

# Authoritative structural contract shared by the parser and visual builder.
# Every other node type is an atomic action and cannot own executable children.
CONTAINER_NODE_TYPES: Final[frozenset[str]] = frozenset(
    {"sequence", "sweep", "repeat", "if"}
)

# Authored operation fields are a public recipe contract. Unknown fields must
# never silently turn a requested hardware setting or wait into a default.
_COMMON_FIELDS = frozenset({"disabled", "label", "description"})
_KEITHLEY_FIELDS = frozenset({
    "channel", "mode", "level", "compliance", "nplc", "settle_time", "settling_time",
    "sense_mode", "source_autorange", "source_range", "measure_voltage_autorange",
    "measure_voltage_range", "measure_current_autorange", "measure_current_range",
})
_RIGOL_FIELDS = frozenset({
    "channel", "waveform", "frequency", "high_level", "low_level", "output_load", "phase_deg",
    "square_duty_percent", "ramp_symmetry_percent", "pulse_width", "pulse_leading", "pulse_trailing",
    "dut_min_impedance",
})
_ANRITSU_FIELDS = frozenset({"start_frequency", "stop_frequency", "reference_level", "points", "trace", "vbw_filter_mode"})
_ADVANCED_FIELDS = frozenset({
    "rbw_mode", "rbw", "vbw_mode", "vbw", "detector", "attenuation_mode",
    "attenuation", "preamplifier_enabled", "sweep_time_mode", "sweep_time", "vbw_filter_mode",
})
ACTION_FIELDS: Final = {
    "sequence": frozenset({"device_module", "operation", "configuration", "parameter_actions",
        "channel", "source_mode", "output_policy", "configuration_required", "trace", "post_configuration_operation",
        "text", "roi_required", "acquire_single", "managed_acquisition_id",
        "acquisition_average_count", "acquisition_reference_operation"}),
    "sweep": frozenset({"target", "binding", "start", "stop", "points", "spacing", "segments"}),
    "repeat": frozenset({"count"}),
    "if": frozenset({"condition", "left", "operator", "right"}),
    "configure_keithley": _KEITHLEY_FIELDS,
    "configure_rigol": _RIGOL_FIELDS,
    "configure_rigol_output": frozenset({"channel", "output_load", "polarity", "mode",
        "gate_polarity", "sync_enabled", "sync_polarity", "sync_delay"}),
    "configure_anritsu": _ANRITSU_FIELDS,
    "configure_anritsu_advanced": _ADVANCED_FIELDS,
    "configure_anritsu_sg": frozenset({"frequency", "power"}),
    "configure_moke_box": frozenset({"channel", "minimum_voltage", "maximum_voltage", "calibration_id"}),
    "arm_moke_voltage": frozenset(),
    "stop_moke_voltage": frozenset({"channel"}),
    "final_state": frozenset({"device", "channel", "output", "voltage", "level", "frequency", "high_level", "low_level"}),
    "update_moke_voltage": frozenset({"channel", "voltage"}),
    "set_moke_voltage": frozenset({"channel", "voltage"}),
    "update_keithley_level": frozenset({"channel", "mode", "level"}),
    "update_keithley_compliance": frozenset({"channel", "mode", "compliance"}),
    "update_rigol_frequency": frozenset({"channel", "frequency"}),
    "update_rigol_levels": frozenset({"channel", "high_level", "low_level"}),
    "update_anritsu_sg": frozenset({"frequency", "power"}),
    "set_rigol_output": frozenset({"channel", "enabled"}),
    "enable_rigol_output": frozenset({"channel"}),
    "set_keithley_output": frozenset({"channel", "enabled"}),
    "ramp_keithley_to_zero": frozenset({"channel", "deadline"}),
    "set_anritsu_sg_output": frozenset({"enabled"}),
    "enable_anritsu_sg_output": frozenset(),
    "measure_keithley": frozenset({"channel"}),
    "measure_moke_hall": frozenset({"checkpoint"}),
    "measure_lakeshore_field": frozenset({"checkpoint"}),
    "wait": frozenset({"duration"}),
    "checkpoint": frozenset(),
    "connect": frozenset({"device"}),
    "comment": frozenset({"text", "comment"}),
    "acquire_reference": frozenset({"trace", "average_count", "inter_sweep_delay", "source_file", "file_kind", "minimum_duration", "purpose"}),
    "acquire_spectrum": frozenset({"trace", "average_count", "inter_sweep_delay", "reference_operation", "processing", "store_raw", "store_processed"}),
    **{kind: frozenset({"template_id", "template_name", "title_pattern", "tags", "attach_hdf5", "attach_csv"})
       for kind in ("upload_to_elab", "upload_elab")},
}

ACTION_FIELDS = MappingProxyType(ACTION_FIELDS)
ACTION_TYPES: Final = frozenset(ACTION_FIELDS)

# These identities are permanent, unlike a block's position in the tree.
DEVICE_MODULES = frozenset({"moke_box", "keithley", "rigol", "anritsu", "anritsu_sg"})
BLOCKS = MappingProxyType({
    **{f"recipe.{kind}": BlockDefinition(f"recipe.{kind}", kind, fields | _COMMON_FIELDS)
       for kind, fields in ACTION_FIELDS.items()},
    **{f"device.{module}": BlockDefinition(f"device.{module}", "sequence",
        ACTION_FIELDS["sequence"] | _COMMON_FIELDS, module) for module in DEVICE_MODULES},
})


def block_for_node(kind, data, declared=None):
    if kind == "sequence" and "device_module" in data and (
        not isinstance(data["device_module"], str) or data["device_module"] not in DEVICE_MODULES
    ):
        raise ConfigurationError(f"Unknown recipe device module {data['device_module']!r}.")
    identity = (f"device.{data['device_module']}" if kind == "sequence" and data.get("device_module")
                else f"recipe.{kind}")
    if identity not in BLOCKS:
        raise ConfigurationError(f"Unknown recipe block {identity!r}.")
    if declared is not None and (not isinstance(declared, str) or declared != identity):
        raise ConfigurationError(f"block_type {declared!r} does not match {identity!r} for node type {kind!r}.")
    return BLOCKS[identity]


def library_block_type(route):
    if route is None:
        return "recipe.repeat"
    category, _, kind = route.partition(":")
    if category == "device":
        identity = f"device.{kind}"
    elif category in {"flow", "integration"}:
        identity = "device.moke_box" if kind == "set_moke_voltage" else f"recipe.{kind}"
    elif category == "output":
        identity = "recipe.set_keithley_output" if kind.startswith("keithley_") else (
            "recipe.enable_rigol_output" if kind.startswith("rigol_") else "recipe.enable_anritsu_sg_output")
    elif category == "safety":
        identity = ("recipe.stop_moke_voltage" if kind == "moke_zero" else
            "recipe.set_keithley_output" if kind.endswith("_off") else
            "recipe.ramp_keithley_to_zero" if kind.startswith("keithley_") else
            "recipe.set_rigol_output" if kind.startswith("rigol_") else
            "recipe.set_anritsu_sg_output" if kind == "anritsu_sg" else "")
    else:
        identity = ""
    if identity not in BLOCKS:
        raise ConfigurationError(f"Unregistered library route {route!r}.")
    return identity


def annotate_recipe_blocks(raw):
    """Add explicit identities without changing operation data or ordering."""
    def visit(node):
        node["type"] = node["type"].strip().lower()
        definition = block_for_node(node["type"], node, node.get("block_type"))
        # Replaced CommentedMaps may retain old key-order bookkeeping after
        # clear/update. Assignment is safe for both loaded and rebuilt maps.
        node["block_type"] = definition.block_type
        for branch in ("children", "else"):
            for child in node.get(branch) or []:
                visit(child)
    visit(raw["root"])
    for node in raw.get("finally") or []:
        visit(node)

