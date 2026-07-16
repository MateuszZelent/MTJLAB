"""Conservative output-current estimate for the voltage-source Rigol DG1032Z."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_FREQUENCY,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_VOLTAGE,
    Quantity,
    parse_quantity,
)
from app.settings.models import RigolChannelSettings, RigolSafety


@dataclass(frozen=True, slots=True)
class RigolCurrentEstimate:
    """Worst-case DC-equivalent output-current estimate, not a measured value."""

    open_circuit_high_v: float
    open_circuit_low_v: float
    current_high_a: float
    current_low_a: float
    peak_absolute_current_a: float
    peak_estimated_dut_power_w: float
    source_resistance_ohm: float
    dut_min_resistance_ohm: float


def _open_circuit_voltage(displayed_v: float, output_load: str | float, source_ohm: float) -> float:
    if isinstance(output_load, str) and output_load.strip().upper() in {"HIGHZ", "INF", "INFINITY"}:
        return displayed_v
    load_ohm = float(output_load)
    if load_ohm <= 0:
        raise SafetyViolation("Ustawienie obciążenia Rigola musi być dodatnie albo HIGHZ.")
    return displayed_v * (source_ohm + load_ohm) / load_ohm


def estimate_rigol_current(
    *,
    high_level: str | float | Quantity,
    low_level: str | float | Quantity,
    output_load: str | float,
    dut_min_impedance: str | float | Quantity,
    source_resistance: str | float | Quantity = "50 ohm",
) -> RigolCurrentEstimate:
    """Estimate current using the documented 50-ohm Thevenin output model."""

    high_v = parse_quantity(high_level, DIMENSION_VOLTAGE, require_unit=not isinstance(high_level, (int, float))).si_value
    low_v = parse_quantity(low_level, DIMENSION_VOLTAGE, require_unit=not isinstance(low_level, (int, float))).si_value
    source_ohm = parse_quantity(source_resistance, DIMENSION_RESISTANCE, require_unit=not isinstance(source_resistance, (int, float))).si_value
    dut_ohm = parse_quantity(dut_min_impedance, DIMENSION_RESISTANCE, require_unit=not isinstance(dut_min_impedance, (int, float))).si_value
    if source_ohm <= 0 or dut_ohm <= 0:
        raise SafetyViolation("Impedancje źródła i DUT muszą być dodatnie.")
    open_high = _open_circuit_voltage(high_v, output_load, source_ohm)
    open_low = _open_circuit_voltage(low_v, output_load, source_ohm)
    current_high = open_high / (source_ohm + dut_ohm)
    current_low = open_low / (source_ohm + dut_ohm)
    return RigolCurrentEstimate(
        open_circuit_high_v=open_high,
        open_circuit_low_v=open_low,
        current_high_a=current_high,
        current_low_a=current_low,
        peak_absolute_current_a=max(abs(current_high), abs(current_low)),
        peak_estimated_dut_power_w=max(current_high * current_high, current_low * current_low) * dut_ohm,
        source_resistance_ohm=source_ohm,
        dut_min_resistance_ohm=dut_ohm,
    )


def _enforce_range(name: str, value: float, minimum: str, maximum: str, dimension: str) -> None:
    lower = parse_quantity(minimum, dimension).si_value
    upper = parse_quantity(maximum, dimension).si_value
    tolerance = max(abs(lower), abs(upper), 1.0) * 1e-12
    if value < lower - tolerance or value > upper + tolerance:
        raise SafetyViolation(
            f"{name}={value:.9g} poza zatwierdzonym zakresem [{lower:.9g}, {upper:.9g}] SI."
        )


def validate_rigol_waveform(
    *,
    channel: RigolChannelSettings,
    safety: RigolSafety,
    waveform: str,
    frequency: str | float | Quantity,
    high_level: str | float | Quantity,
    low_level: str | float | Quantity,
    output_load: str | float,
    dut_min_impedance: str | float | Quantity | None,
) -> RigolCurrentEstimate:
    """Validate a complete output configuration before a SCPI write is allowed."""

    waveform_normalized = waveform.strip().upper()
    if waveform_normalized not in channel.allowed_waveforms:
        raise SafetyViolation(f"Przebieg {waveform_normalized} nie jest dozwolony dla kanału.")
    freq_hz = parse_quantity(frequency, DIMENSION_FREQUENCY, require_unit=not isinstance(frequency, (int, float))).si_value
    high_v = parse_quantity(high_level, DIMENSION_VOLTAGE, require_unit=not isinstance(high_level, (int, float))).si_value
    low_v = parse_quantity(low_level, DIMENSION_VOLTAGE, require_unit=not isinstance(low_level, (int, float))).si_value
    if waveform_normalized != "DC" and high_v <= low_v:
        raise SafetyViolation("HighL musi być większy od LowL dla przebiegów zmiennych.")

    limits = channel.lab_limits
    if waveform_normalized not in {"DC", "NOIS"}:
        _enforce_range("frequency", freq_hz, limits.frequency.min, limits.frequency.max, DIMENSION_FREQUENCY)
    _enforce_range("high_level", high_v, limits.high_level.min, limits.high_level.max, DIMENSION_VOLTAGE)
    _enforce_range("low_level", low_v, limits.low_level.min, limits.low_level.max, DIMENSION_VOLTAGE)
    if waveform_normalized != "DC":
        _enforce_range("amplitude_vpp", high_v - low_v, limits.amplitude_vpp.min, limits.amplitude_vpp.max, DIMENSION_VOLTAGE)
    _enforce_range("offset", (high_v + low_v) / 2.0, limits.offset.min, limits.offset.max, DIMENSION_VOLTAGE)

    if dut_min_impedance is None:
        if safety.require_declared_dut_impedance:
            raise SafetyViolation("Przed włączeniem Rigola trzeba zadeklarować minimalną impedancję DUT.")
        dut_min_impedance = limits.declared_dut_impedance.min
    estimate = estimate_rigol_current(
        high_level=high_v,
        low_level=low_v,
        output_load=output_load,
        dut_min_impedance=dut_min_impedance,
        source_resistance=safety.fixed_source_resistance,
    )
    current_limit = parse_quantity(limits.estimated_load_current.max_abs or limits.estimated_load_current.max, DIMENSION_CURRENT).si_value
    if estimate.peak_absolute_current_a > current_limit:
        raise SafetyViolation(
            "Szacowany prąd obciążenia Rigola "
            f"{estimate.peak_absolute_current_a:.9g} A przekracza limit {current_limit:.9g} A."
        )
    power_limit = parse_quantity(
        limits.estimated_load_power.max_abs or limits.estimated_load_power.max, DIMENSION_POWER
    ).si_value
    if estimate.peak_estimated_dut_power_w > power_limit:
        raise SafetyViolation(
            "Szacowana moc na DUT dla Rigola "
            f"{estimate.peak_estimated_dut_power_w:.9g} W przekracza limit {power_limit:.9g} W."
        )
    return estimate


def validate_rigol_frequency_sweep(
    *,
    channel: RigolChannelSettings,
    start_hz: float,
    stop_hz: float,
    duration_s: float,
    steps: int,
    start_hold_s: float = 0.0,
    stop_hold_s: float = 0.0,
    return_time_s: float = 0.0,
) -> None:
    """Validate all sweep values against the approved channel profile."""

    numeric = (start_hz, stop_hz, duration_s, start_hold_s, stop_hold_s, return_time_s)
    if not all(math.isfinite(value) for value in numeric):
        raise SafetyViolation("Sweep values must be finite numbers.")
    if start_hz <= 0 or stop_hz <= 0 or start_hz == stop_hz:
        raise SafetyViolation("Sweep requires positive and different start/stop frequencies.")
    if duration_s <= 0 or min(start_hold_s, stop_hold_s, return_time_s) < 0:
        raise SafetyViolation("Sweep duration must be positive; hold and return times cannot be negative.")
    if isinstance(steps, bool) or not isinstance(steps, int):
        raise SafetyViolation("Sweep step count must be an integer.")

    limits = channel.lab_limits
    _enforce_range("sweep_start", start_hz, limits.frequency.min, limits.frequency.max, DIMENSION_FREQUENCY)
    _enforce_range("sweep_stop", stop_hz, limits.frequency.min, limits.frequency.max, DIMENSION_FREQUENCY)
    _enforce_range("sweep_duration", duration_s, limits.sweep_duration.min, limits.sweep_duration.max, "time")
    if not limits.sweep_steps.min <= steps <= limits.sweep_steps.max:
        raise SafetyViolation(
            f"sweep_steps={steps} outside approved range [{limits.sweep_steps.min}, {limits.sweep_steps.max}]."
        )
