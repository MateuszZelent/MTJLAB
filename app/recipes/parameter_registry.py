"""Single source of truth for recipe sweep-axis metadata."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
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
