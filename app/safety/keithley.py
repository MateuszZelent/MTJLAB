"""Validation of source/compliance settings for each Keithley SMU channel."""

from __future__ import annotations

from dataclasses import dataclass
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


def _range_check(name: str, value: float, lower: str, upper: str, dimension: str) -> None:
    minimum = parse_quantity(lower, dimension).si_value
    maximum = parse_quantity(upper, dimension).si_value
    # A sweep endpoint calculated with binary floats can differ from its exact
    # configured bound by a few ulps.  This tolerance only absorbs that
    # representation error; it is relative to the configured range.
    tolerance = max(abs(minimum), abs(maximum), 1.0) * 1e-12
    if value < minimum - tolerance or value > maximum + tolerance:
        raise SafetyViolation(f"{name}={value:.9g} poza zakresem [{minimum:.9g}, {maximum:.9g}] SI.")


def validate_keithley_source(channel: KeithleyChannelSettings, request: KeithleySourceRequest) -> None:
    if request.mode not in channel.allowed_source_modes:
        raise SafetyViolation(f"Tryb źródła {request.mode} nie jest dozwolony dla kanału Keithley.")
    if not channel.enabled:
        raise SafetyViolation("Kanał Keithley jest wyłączony w profilu stanowiska.")
    if request.nplc < 0.001 or request.nplc > 25:
        raise SafetyViolation("NPLC musi mieścić się w zakresie 0.001–25.")
    if request.settle_time_s < 0:
        raise SafetyViolation("Czas ustalania nie może być ujemny.")
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


def validate_keithley_measurement(channel: KeithleyChannelSettings, voltage_v: float, current_a: float) -> None:
    limits = channel.lab_limits
    _range_check("measured current", current_a, limits.measured_current_trip.min, limits.measured_current_trip.max, DIMENSION_CURRENT)
    _range_check("measured voltage", voltage_v, limits.measured_voltage_trip.min, limits.measured_voltage_trip.max, DIMENSION_VOLTAGE)
    max_power = parse_quantity(limits.max_abs_power, DIMENSION_POWER).si_value
    if abs(voltage_v * current_a) > max_power:
        raise SafetyViolation(
            f"Moc DUT {abs(voltage_v * current_a):.9g} W przekracza limit {max_power:.9g} W."
        )
