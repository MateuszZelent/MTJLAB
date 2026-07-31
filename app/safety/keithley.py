"""Validation of source/compliance settings for each Keithley SMU channel."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from app.domain.errors import SafetyViolation
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_POWER, DIMENSION_VOLTAGE, parse_quantity
from app.safety.precision import quantize_to_step
from app.settings.models import KeithleyChannelSettings


SourceMode = Literal["current", "voltage", "measure_only"]

# Immutable 2602A range ceilings. These describe instrument range selectors,
# not DUT trip thresholds. The adapter independently validates the documented
# discrete ranges before writing TSP commands.
KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V = 40.0
KEITHLEY_2602A_MAX_CURRENT_RANGE_A = 3.0
KEITHLEY_2602A_VOLTAGE_RANGES = (0.1, 1.0, 6.0, 40.0)
KEITHLEY_2602A_VOLTAGE_SOURCE_RESOLUTIONS = (5e-6, 50e-6, 50e-6, 500e-6)
KEITHLEY_2602A_CURRENT_RANGES = (
    100e-9,
    1e-6,
    10e-6,
    100e-6,
    1e-3,
    10e-3,
    100e-3,
    1.0,
    3.0,
)
KEITHLEY_2602A_CURRENT_SOURCE_RESOLUTIONS = (
    1e-12,
    10e-12,
    100e-12,
    1e-9,
    10e-9,
    100e-9,
    1e-6,
    10e-6,
    10e-6,
)


def keithley_programming_resolution(
    dimension: str,
    value_si: float,
    *,
    requested_range_si: float | None = None,
) -> float:
    """Return the 2602A source-programming step for a selected range."""

    if not math.isfinite(value_si):
        raise SafetyViolation("Keithley value must be finite before quantisation.")
    if requested_range_si is not None and (
        not math.isfinite(requested_range_si) or requested_range_si <= 0
    ):
        raise SafetyViolation("Keithley manual range must be finite and positive.")
    if dimension == DIMENSION_VOLTAGE:
        ranges = KEITHLEY_2602A_VOLTAGE_RANGES
        resolutions = KEITHLEY_2602A_VOLTAGE_SOURCE_RESOLUTIONS
    elif dimension == DIMENSION_CURRENT:
        ranges = KEITHLEY_2602A_CURRENT_RANGES
        resolutions = KEITHLEY_2602A_CURRENT_SOURCE_RESOLUTIONS
    else:
        raise SafetyViolation(f"Unsupported Keithley quantisation dimension {dimension!r}.")

    required = abs(requested_range_si if requested_range_si is not None else value_si)
    for hardware_range, resolution in zip(ranges, resolutions, strict=True):
        if required <= hardware_range or math.isclose(
            required, hardware_range, rel_tol=1e-12, abs_tol=1e-15
        ):
            return resolution
    raise SafetyViolation(
        f"Keithley {dimension} value {value_si:.12g} exceeds the documented 2602A range."
    )


def quantize_keithley_value(
    value_si: float,
    dimension: str,
    *,
    requested_range_si: float | None = None,
) -> float:
    """Round a source level or compliance value to the 2602A range step."""

    return quantize_to_step(
        float(value_si),
        keithley_programming_resolution(
            dimension,
            float(value_si),
            requested_range_si=requested_range_si,
        ),
        name=f"Keithley {dimension}",
    )


@dataclass(frozen=True, slots=True)
class KeithleySourceRequest:
    channel: Literal["A", "B"]
    mode: SourceMode
    level_si: float
    compliance_si: float
    nplc: float = 1.0
    settle_time_s: float = 0.0
    sense_mode: Literal["2wire", "4wire"] = "2wire"
    source_autorange: bool = True
    source_range_si: float | None = None
    measure_voltage_autorange: bool = True
    measure_voltage_range_si: float | None = None
    measure_current_autorange: bool = True
    measure_current_range_si: float | None = None


def _range_check(name: str, value: float, lower: str, upper: str, dimension: str) -> None:
    _require_finite(name, value)
    minimum = parse_quantity(lower, dimension).si_value
    maximum = parse_quantity(upper, dimension).si_value
    # A sweep endpoint calculated with binary floats can differ from its exact
    # configured bound by a few ulps.  This tolerance only absorbs that
    # representation error; it is relative to the configured range.
    tolerance = max(abs(minimum), abs(maximum), 1.0) * 1e-12
    if value < minimum - tolerance or value > maximum + tolerance:
        raise SafetyViolation(f"{name}={value:.9g} is outside [{minimum:.9g}, {maximum:.9g}] SI.")


def validate_keithley_source(channel: KeithleyChannelSettings, request: KeithleySourceRequest) -> None:
    for name, value in (
        ("source level", request.level_si),
        ("compliance", request.compliance_si),
        ("NPLC", request.nplc),
        ("settle time", request.settle_time_s),
    ):
        _require_finite(name, value)
    for name, value in (
        ("source range", request.source_range_si),
        ("measure voltage range", request.measure_voltage_range_si),
        ("measure current range", request.measure_current_range_si),
    ):
        if value is not None:
            _require_finite(name, value)
    if request.mode not in channel.allowed_source_modes:
        raise SafetyViolation(f"Source mode {request.mode} is not allowed for this Keithley channel.")
    if not channel.enabled:
        raise SafetyViolation("The Keithley channel is disabled in the station profile.")
    if request.nplc < 0.001 or request.nplc > 25:
        raise SafetyViolation("NPLC must be in the range 0.001–25.")
    # Series 2600A firmware stores NPLC with 0.001 PLC resolution. Reject
    # values that the instrument would silently round so the UI, persisted
    # configuration and hardware readback always describe the same setting.
    nplc_milli = round(request.nplc * 1000)
    if not math.isclose(request.nplc, nplc_milli / 1000, rel_tol=0.0, abs_tol=1e-12):
        raise SafetyViolation("NPLC must use 0.001 PLC increments.")
    if request.settle_time_s < 0:
        raise SafetyViolation("Settling time cannot be negative.")
    if request.sense_mode not in {"2wire", "4wire"}:
        raise SafetyViolation("Keithley sense mode must be 2wire or 4wire.")
    limits = channel.lab_limits
    if request.mode == "current":
        if limits.source_current.enabled:
            _range_check("source current", request.level_si, limits.source_current.min, limits.source_current.max, DIMENSION_CURRENT)
        if limits.voltage_compliance.enabled:
            _range_check("voltage compliance", request.compliance_si, limits.voltage_compliance.min, limits.voltage_compliance.max, DIMENSION_VOLTAGE)
    elif request.mode == "voltage":
        if limits.source_voltage.enabled:
            _range_check("source voltage", request.level_si, limits.source_voltage.min, limits.source_voltage.max, DIMENSION_VOLTAGE)
        if limits.current_compliance.enabled:
            _range_check("current compliance", request.compliance_si, limits.current_compliance.min, limits.current_compliance.max, DIMENSION_CURRENT)
    else:
        if request.level_si != 0 or request.compliance_si != 0:
            raise SafetyViolation("measure_only mode cannot set a source level or compliance.")
        if not request.source_autorange or request.source_range_si is not None:
            raise SafetyViolation("measure_only mode does not use a source range; set source_autorange=true.")
    if request.mode != "measure_only":
        worst_case_power = abs(request.level_si * request.compliance_si)
        max_power = (
            parse_quantity(limits.max_abs_power, DIMENSION_POWER).si_value
            if limits.max_abs_power_enabled
            else math.inf
        )
        tolerance = max(max_power, 1.0) * 1e-12
        if math.isfinite(max_power) and worst_case_power > max_power + tolerance:
            raise SafetyViolation(
                "Worst-case source × compliance power "
                f"{worst_case_power:.9g} W exceeds the station profile "
                f"{max_power:.9g} W."
            )
    source_required = abs(request.level_si)
    source_range_max = (
        KEITHLEY_2602A_MAX_CURRENT_RANGE_A
        if request.mode == "current"
        else KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V
    )
    _validate_manual_range(
        "source range",
        request.source_autorange,
        request.source_range_si,
        source_required,
        source_range_max,
    )
    voltage_required = abs(request.compliance_si if request.mode == "current" else request.level_si)
    current_required = abs(request.level_si if request.mode == "current" else request.compliance_si)
    _validate_manual_range(
        "measure voltage range",
        request.measure_voltage_autorange,
        request.measure_voltage_range_si,
        voltage_required,
        KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V,
    )
    _validate_manual_range(
        "measure current range",
        request.measure_current_autorange,
        request.measure_current_range_si,
        current_required,
        KEITHLEY_2602A_MAX_CURRENT_RANGE_A,
    )


def _validate_manual_range(
    name: str,
    autorange: bool,
    manual_range_si: float | None,
    required_si: float,
    hardware_max_si: float,
) -> None:
    if autorange:
        if manual_range_si is not None:
            raise SafetyViolation(f"{name}: a manual range requires autorange=false.")
        return
    if manual_range_si is None or manual_range_si <= 0:
        raise SafetyViolation(f"{name}: autorange=false requires a positive range.")
    if manual_range_si < required_si or manual_range_si > hardware_max_si:
        raise SafetyViolation(
            f"{name}={manual_range_si:.9g} does not cover required value {required_si:.9g} "
            f"or exceeds the 2602A hardware maximum {hardware_max_si:.9g} SI."
        )


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise SafetyViolation(f"{name} must be finite.")


def validate_keithley_measurement(
    channel: KeithleyChannelSettings,
    voltage_v: float,
    current_a: float,
) -> None:
    limits = channel.lab_limits
    if limits.measured_current_trip.enabled:
        _range_check("measured current", current_a, limits.measured_current_trip.min, limits.measured_current_trip.max, DIMENSION_CURRENT)
    if limits.measured_voltage_trip.enabled:
        _range_check("measured voltage", voltage_v, limits.measured_voltage_trip.min, limits.measured_voltage_trip.max, DIMENSION_VOLTAGE)
    max_power = (
        parse_quantity(limits.max_abs_power, DIMENSION_POWER).si_value
        if limits.max_abs_power_enabled
        else math.inf
    )
    measured_power = abs(voltage_v * current_a)
    if math.isfinite(max_power) and measured_power > max_power and not math.isclose(
        measured_power, max_power, rel_tol=1e-12, abs_tol=1e-15
    ):
        raise SafetyViolation(
            f"Measured power {measured_power:.9g} W exceeds the {max_power:.9g} W station limit."
        )
