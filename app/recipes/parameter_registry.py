"""Single source of truth for recipe sweep-axis metadata."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
)


@dataclass(frozen=True, slots=True)
class ParameterDescriptor:
    """Stable contract shared by editing, compilation and persistence."""

    axis_target: str
    device_module: str
    device_name: str
    control_name: str
    ui_group: str
    ui_label: str
    dimension: str
    unit: str
    sweepable: bool = True
    requires_output_cycle: bool = False


@dataclass(frozen=True, slots=True)
class QuickControlDescriptor:
    target: str
    device_module: str
    label: str
    dimension: str
    default_text: str
    atomic_group: str


_DESCRIPTORS: Final[tuple[ParameterDescriptor, ...]] = (
    ParameterDescriptor(
        "keithley.A.level", "keithley", "Keithley 2602A",
        "Keithley A level", "Keithley", "Channel A · source current",
        DIMENSION_CURRENT, "A",
    ),
    ParameterDescriptor(
        "keithley.B.level", "keithley", "Keithley 2602A",
        "Keithley B level", "Keithley", "Channel B · source current",
        DIMENSION_CURRENT, "A",
    ),
    ParameterDescriptor(
        "keithley.A.current", "keithley", "Keithley 2602A",
        "Keithley A current", "Keithley", "Channel A · source current",
        DIMENSION_CURRENT, "A",
    ),
    ParameterDescriptor(
        "keithley.B.current", "keithley", "Keithley 2602A",
        "Keithley B current", "Keithley", "Channel B · source current",
        DIMENSION_CURRENT, "A",
    ),
    ParameterDescriptor(
        "keithley.A.voltage", "keithley", "Keithley 2602A",
        "Keithley A voltage", "Keithley", "Channel A · source voltage",
        DIMENSION_VOLTAGE, "V",
    ),
    ParameterDescriptor(
        "keithley.B.voltage", "keithley", "Keithley 2602A",
        "Keithley B voltage", "Keithley", "Channel B · source voltage",
        DIMENSION_VOLTAGE, "V",
    ),
    ParameterDescriptor(
        "keithley.A.compliance_voltage", "keithley", "Keithley 2602A",
        "Keithley A voltage compliance", "Keithley",
        "Channel A · voltage compliance", DIMENSION_VOLTAGE, "V",
    ),
    ParameterDescriptor(
        "keithley.B.compliance_voltage", "keithley", "Keithley 2602A",
        "Keithley B voltage compliance", "Keithley",
        "Channel B · voltage compliance", DIMENSION_VOLTAGE, "V",
    ),
    ParameterDescriptor(
        "keithley.A.compliance_current", "keithley", "Keithley 2602A",
        "Keithley A current compliance", "Keithley",
        "Channel A · current compliance", DIMENSION_CURRENT, "A",
    ),
    ParameterDescriptor(
        "keithley.B.compliance_current", "keithley", "Keithley 2602A",
        "Keithley B current compliance", "Keithley",
        "Channel B · current compliance", DIMENSION_CURRENT, "A",
    ),
    ParameterDescriptor(
        "keithley.A.settling_time", "keithley", "Keithley 2602A",
        "Keithley A settling time", "Keithley",
        "Channel A · settling time", DIMENSION_TIME, "s",
    ),
    ParameterDescriptor(
        "keithley.B.settling_time", "keithley", "Keithley 2602A",
        "Keithley B settling time", "Keithley",
        "Channel B · settling time", DIMENSION_TIME, "s",
    ),
    *tuple(
        ParameterDescriptor(
            f"rigol.{channel}.{field}",
            "rigol",
            "Rigol DG1032Z",
            f"Rigol CH{channel} {label.lower()}",
            "Rigol",
            f"CH{channel} · {label.lower()}",
            dimension,
            unit,
            requires_output_cycle=False,
        )
        for channel in (1, 2)
        for field, label, dimension, unit in (
            ("frequency", "Frequency", DIMENSION_FREQUENCY, "Hz"),
            ("high_level", "High level", DIMENSION_VOLTAGE, "V"),
            ("low_level", "Low level", DIMENSION_VOLTAGE, "V"),
        )
    ),
    ParameterDescriptor(
        "anritsu.sg.frequency", "anritsu", "Anritsu Signal Generator",
        "Anritsu SG frequency", "Anritsu SG", "Signal generator · frequency",
        DIMENSION_FREQUENCY, "Hz",
    ),
    ParameterDescriptor(
        "anritsu.sg.power", "anritsu", "Anritsu Signal Generator",
        "Anritsu SG power", "Anritsu SG", "Signal generator · power",
        DIMENSION_DBM, "dBm",
    ),
    ParameterDescriptor(
        "anritsu.spectrum.start_frequency", "anritsu",
        "Anritsu Spectrum Analyzer", "Anritsu start frequency",
        "Anritsu Spectrum", "Spectrum · start frequency",
        DIMENSION_FREQUENCY, "Hz",
    ),
    ParameterDescriptor(
        "anritsu.spectrum.stop_frequency", "anritsu",
        "Anritsu Spectrum Analyzer", "Anritsu stop frequency",
        "Anritsu Spectrum", "Spectrum · stop frequency",
        DIMENSION_FREQUENCY, "Hz",
    ),
    ParameterDescriptor(
        "anritsu.spectrum.reference_level", "anritsu",
        "Anritsu Spectrum Analyzer", "Anritsu reference level",
        "Anritsu Spectrum", "Spectrum · reference level",
        DIMENSION_DBM, "dBm",
    ),
)

PARAMETER_DESCRIPTORS: Final = _DESCRIPTORS
PARAMETERS_BY_TARGET: Final = MappingProxyType(
    {descriptor.axis_target: descriptor for descriptor in _DESCRIPTORS}
)
SWEEP_DIMENSIONS: Final = MappingProxyType(
    {
        target: descriptor.dimension
        for target, descriptor in PARAMETERS_BY_TARGET.items()
        if descriptor.sweepable
    }
)


def parameter_descriptor(axis_target: str) -> ParameterDescriptor:
    """Return a registered descriptor or fail closed for an unknown axis."""

    try:
        return PARAMETERS_BY_TARGET[axis_target]
    except KeyError as exc:
        allowed = ", ".join(sorted(PARAMETERS_BY_TARGET))
        raise KeyError(f"Unknown sweep target {axis_target!r}; allowed: {allowed}.") from exc


def legacy_ui_parameter_definitions() -> tuple[dict[str, str], ...]:
    """Return the legacy picker surface without duplicating registry data."""

    hidden = {
        "keithley.A.level",
        "keithley.B.level",
        "keithley.A.compliance_voltage",
        "keithley.B.compliance_voltage",
        "keithley.A.compliance_current",
        "keithley.B.compliance_current",
        "keithley.A.settling_time",
        "keithley.B.settling_time",
    }
    order = {
        target: index
        for index, target in enumerate(
            (
                "keithley.A.current",
                "keithley.A.voltage",
                "keithley.B.current",
                "keithley.B.voltage",
                "rigol.1.frequency",
                "rigol.1.high_level",
                "rigol.1.low_level",
                "rigol.2.frequency",
                "rigol.2.high_level",
                "rigol.2.low_level",
                "anritsu.sg.frequency",
                "anritsu.sg.power",
                "anritsu.spectrum.start_frequency",
                "anritsu.spectrum.stop_frequency",
                "anritsu.spectrum.reference_level",
            )
        )
    }
    descriptors = sorted(
        (
            descriptor
            for descriptor in _DESCRIPTORS
            if descriptor.sweepable and descriptor.axis_target not in hidden
        ),
        key=lambda descriptor: order[descriptor.axis_target],
    )
    return tuple(
        {
            "device": descriptor.ui_group,
            "label": descriptor.ui_label,
            "target": descriptor.axis_target,
            "dimension": descriptor.dimension,
        }
        for descriptor in descriptors
    )


def parameter_definitions_for_module(module_key: str) -> tuple[dict[str, str], ...]:
    """Return the recipe controls exposed by one registered device module."""

    return tuple(
        definition
        for definition in legacy_ui_parameter_definitions()
        if PARAMETERS_BY_TARGET[definition["target"]].device_module == module_key
    )


# Transitional compatibility surface for the existing Qt recipe editor.  It
# belongs to the recipe domain, not to the UI package of any particular device.
SWEEPABLE_PARAMETERS: Final[tuple[dict[str, str], ...]] = legacy_ui_parameter_definitions()


QUICK_CONTROL_DESCRIPTORS: Final[tuple[QuickControlDescriptor, ...]] = (
    *tuple(
        QuickControlDescriptor(
            f"keithley.{channel}.{mode}",
            "keithley",
            f"Keithley {channel} · {mode.title()}",
            DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE,
            "0.000 A" if mode == "current" else "0.000 V",
            f"keithley.{channel}.source",
        )
        for channel in ("A", "B")
        for mode in ("current", "voltage")
    ),
    *tuple(
        QuickControlDescriptor(
            f"rigol.{channel}.{field}",
            "rigol",
            f"Rigol CH{channel} · {label}",
            dimension,
            default,
            f"rigol.{channel}.levels" if field in {"amplitude", "offset"} else f"rigol.{channel}.frequency",
        )
        for channel in (1, 2)
        for field, label, dimension, default in (
            ("frequency", "Frequency", DIMENSION_FREQUENCY, "1.000 kHz"),
            ("high_level", "High level", DIMENSION_VOLTAGE, "1.000 V"),
            ("low_level", "Low level", DIMENSION_VOLTAGE, "-1.000 V"),
            ("amplitude", "Amplitude Vpp", DIMENSION_VOLTAGE, "1.000 V"),
            ("offset", "Offset", DIMENSION_VOLTAGE, "0.000 V"),
        )
    ),
)
QUICK_CONTROLS_BY_TARGET: Final = MappingProxyType(
    {descriptor.target: descriptor for descriptor in QUICK_CONTROL_DESCRIPTORS}
)


def sweep_default(dimension: str) -> tuple[str, str]:
    """Return conservative editor defaults for a registered sweep dimension."""

    return {
        DIMENSION_CURRENT: ("0 A", "1 mA"),
        DIMENSION_VOLTAGE: ("0 V", "10 mV"),
        DIMENSION_FREQUENCY: ("1 kHz", "10 kHz"),
        DIMENSION_DBM: ("-30 dBm", "0 dBm"),
        DIMENSION_MAGNETIC_FIELD: ("0 mT", "1 mT"),
    }[dimension]
