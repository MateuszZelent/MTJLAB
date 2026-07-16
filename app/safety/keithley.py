"""Validation of source/compliance settings for each Keithley SMU channel."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from app.domain.errors import SafetyViolation
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_POWER, DIMENSION_VOLTAGE, parse_quantity
from app.settings.models import KeithleyChannelSettings


SourceMode = Literal["current", "voltage", "measure_only"]


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
        raise SafetyViolation(f"{name}={value:.9g} poza zakresem [{minimum:.9g}, {maximum:.9g}] SI.")


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
        raise SafetyViolation(f"Tryb źródła {request.mode} nie jest dozwolony dla kanału Keithley.")
    if not channel.enabled:
        raise SafetyViolation("Kanał Keithley jest wyłączony w profilu stanowiska.")
    if request.nplc < 0.001 or request.nplc > 25:
        raise SafetyViolation("NPLC musi mieścić się w zakresie 0.001–25.")
    if request.settle_time_s < 0:
        raise SafetyViolation("Czas ustalania nie może być ujemny.")
    if request.sense_mode not in {"2wire", "4wire"}:
        raise SafetyViolation("Sense mode Keithley musi być 2wire albo 4wire.")
    limits = channel.lab_limits
    if request.mode == "current":
        _range_check("source current", request.level_si, limits.source_current.min, limits.source_current.max, DIMENSION_CURRENT)
        _range_check("voltage compliance", request.compliance_si, limits.voltage_compliance.min, limits.voltage_compliance.max, DIMENSION_VOLTAGE)
    elif request.mode == "voltage":
        _range_check("source voltage", request.level_si, limits.source_voltage.min, limits.source_voltage.max, DIMENSION_VOLTAGE)
        _range_check("current compliance", request.compliance_si, limits.current_compliance.min, limits.current_compliance.max, DIMENSION_CURRENT)
    else:
        if request.level_si != 0 or request.compliance_si != 0:
            raise SafetyViolation("Tryb measure_only nie może wymuszać poziomu ani compliance.")
        if not request.source_autorange or request.source_range_si is not None:
            raise SafetyViolation("Tryb measure_only nie obsługuje zakresu źródła; ustaw source_autorange=true.")
    if request.mode != "measure_only":
        worst_case_power = abs(request.level_si * request.compliance_si)
        max_power = parse_quantity(limits.max_abs_power, DIMENSION_POWER).si_value
        tolerance = max(max_power, 1.0) * 1e-12
        if worst_case_power > max_power + tolerance:
            raise SafetyViolation(
                "Najgorsza możliwa moc source × compliance "
                f"{worst_case_power:.9g} W przekracza limit DUT {max_power:.9g} W."
            )
    source_dimension = DIMENSION_CURRENT if request.mode == "current" else DIMENSION_VOLTAGE
    source_limits = limits.source_current if request.mode == "current" else limits.source_voltage
    source_required = abs(request.level_si)
    _validate_manual_range(
        "source range",
        request.source_autorange,
        request.source_range_si,
        source_required,
        source_limits.min,
        source_limits.max,
        source_dimension,
    )
    voltage_required = abs(request.compliance_si if request.mode == "current" else request.level_si)
    current_required = abs(request.level_si if request.mode == "current" else request.compliance_si)
    _validate_manual_range(
        "measure voltage range",
        request.measure_voltage_autorange,
        request.measure_voltage_range_si,
        voltage_required,
        limits.measured_voltage_trip.min,
        limits.measured_voltage_trip.max,
        DIMENSION_VOLTAGE,
    )
    _validate_manual_range(
        "measure current range",
        request.measure_current_autorange,
        request.measure_current_range_si,
        current_required,
        limits.measured_current_trip.min,
        limits.measured_current_trip.max,
        DIMENSION_CURRENT,
    )


def _validate_manual_range(
    name: str,
    autorange: bool,
    manual_range_si: float | None,
    required_si: float,
    lower: str,
    upper: str,
    dimension: str,
) -> None:
    if autorange:
        if manual_range_si is not None:
            raise SafetyViolation(f"{name}: ręczny zakres wymaga autorange=false.")
        return
    if manual_range_si is None or manual_range_si <= 0:
        raise SafetyViolation(f"{name}: autorange=false wymaga dodatniego zakresu.")
    maximum = max(
        abs(parse_quantity(lower, dimension).si_value),
        abs(parse_quantity(upper, dimension).si_value),
    )
    if manual_range_si < required_si or manual_range_si > maximum:
        raise SafetyViolation(
            f"{name}={manual_range_si:.9g} nie obejmuje wymaganej wartości {required_si:.9g} "
            f"albo przekracza limit {maximum:.9g} SI."
        )


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise SafetyViolation(f"{name} musi być skończoną liczbą.")


def validate_keithley_measurement(channel: KeithleyChannelSettings, voltage_v: float, current_a: float) -> None:
    limits = channel.lab_limits
    _range_check("measured current", current_a, limits.measured_current_trip.min, limits.measured_current_trip.max, DIMENSION_CURRENT)
    _range_check("measured voltage", voltage_v, limits.measured_voltage_trip.min, limits.measured_voltage_trip.max, DIMENSION_VOLTAGE)
    max_power = parse_quantity(limits.max_abs_power, DIMENSION_POWER).si_value
    if abs(voltage_v * current_a) > max_power:
        raise SafetyViolation(
            f"Moc DUT {abs(voltage_v * current_a):.9g} W przekracza limit {max_power:.9g} W."
        )
