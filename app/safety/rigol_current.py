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


# DG1032Z documented basic-waveform ceilings.  These are hardware limits,
# independent of (and more authoritative than) an editable laboratory profile.
_DG1032Z_MAX_FREQUENCY_HZ = {
    "SIN": 30e6,
    "SQU": 25e6,
    "RAMP": 500e3,
    "PULS": 15e6,
    "USER": 10e6,
}

# Output-characteristic limits from the DG1000Z data sheet.  Values are
# expressed at the generator's physical, open-circuit output.  The manual
# specifies half of these values when the front-panel load is set to 50 ohm;
# the instrument only changes the displayed/programmed voltage, not its fixed
# 50-ohm series source impedance.
_DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V = 10.0
_DG1032Z_MIN_OPEN_CIRCUIT_VPP = 2e-3
_DG1032Z_MAX_OPEN_CIRCUIT_VPP_BY_FREQUENCY = (
    (10e6, 20.0),
    (30e6, 10.0),
)


def rigol_hardware_frequency_max_hz(waveform: str | None = None) -> float:
    """Return the immutable DG1032Z ceiling used by UI and safety layers."""

    if waveform is None:
        return max(_DG1032Z_MAX_FREQUENCY_HZ.values())
    return _DG1032Z_MAX_FREQUENCY_HZ.get(
        waveform.strip().upper(),
        max(_DG1032Z_MAX_FREQUENCY_HZ.values()),
    )


def _rigol_hardware_open_circuit_vpp_max(frequency_hz: float) -> float:
    for upper_frequency_hz, maximum_vpp in _DG1032Z_MAX_OPEN_CIRCUIT_VPP_BY_FREQUENCY:
        if frequency_hz <= upper_frequency_hz:
            return maximum_vpp
    # DG1032Z cannot produce a carrier above 30 MHz.  Returning the final
    # documented tier keeps this helper conservative if called before the
    # waveform-specific frequency guard.
    return _DG1032Z_MAX_OPEN_CIRCUIT_VPP_BY_FREQUENCY[-1][1]


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


@dataclass(frozen=True, slots=True)
class RigolSafetyEnvelope:
    minimum_impedance_ohm: float | None = None
    max_abs_current_a: float | None = None
    max_abs_power_w: float | None = None


def _open_circuit_voltage(displayed_v: float, output_load: str | float, source_ohm: float) -> float:
    if isinstance(output_load, str) and output_load.strip().upper() in {"HIGHZ", "INF", "INFINITY"}:
        return displayed_v
    try:
        load_ohm = float(output_load)
    except (TypeError, ValueError) as exc:
        raise SafetyViolation(
            "Rigol output load must be a resistance value or HIGHZ."
        ) from exc
    if not math.isfinite(load_ohm) or load_ohm <= 0:
        raise SafetyViolation("Rigol output load must be finite and positive or HIGHZ.")
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
    numeric = (high_v, low_v, source_ohm, dut_ohm)
    if not all(math.isfinite(value) for value in numeric):
        raise SafetyViolation("Rigol voltage and impedance values must be finite.")
    if source_ohm <= 0 or dut_ohm <= 0:
        raise SafetyViolation("Source and DUT impedances must be positive.")
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
            f"{name}={value:.9g} is outside the configured [{lower:.9g}, {upper:.9g}] SI range."
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
    dut_envelope: RigolSafetyEnvelope | None = None,
) -> RigolCurrentEstimate:
    """Validate a complete output configuration before a SCPI write is allowed."""

    waveform_normalized = waveform.strip().upper()
    if waveform_normalized not in channel.allowed_waveforms:
        raise SafetyViolation(f"Waveform {waveform_normalized} is not allowed for this channel.")
    freq_hz = parse_quantity(frequency, DIMENSION_FREQUENCY, require_unit=not isinstance(frequency, (int, float))).si_value
    high_v = parse_quantity(high_level, DIMENSION_VOLTAGE, require_unit=not isinstance(high_level, (int, float))).si_value
    low_v = parse_quantity(low_level, DIMENSION_VOLTAGE, require_unit=not isinstance(low_level, (int, float))).si_value
    if not all(math.isfinite(value) for value in (freq_hz, high_v, low_v)):
        raise SafetyViolation("Rigol frequency and voltage levels must be finite.")
    if waveform_normalized not in {"DC", "NOIS"} and freq_hz <= 0:
        raise SafetyViolation("Rigol waveform frequency must be positive.")
    if waveform_normalized == "DC" and not math.isclose(
        high_v, low_v, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise SafetyViolation(
            "DC mode requires one DC level; the internal configuration supplied two different voltages."
        )
    if waveform_normalized != "DC" and high_v <= low_v:
        raise SafetyViolation("HighL must be greater than LowL for non-DC waveforms.")
    hardware_max_hz = _DG1032Z_MAX_FREQUENCY_HZ.get(waveform_normalized)
    if hardware_max_hz is not None and freq_hz > hardware_max_hz:
        raise SafetyViolation(
            f"{waveform_normalized} frequency {freq_hz:.9g} Hz exceeds the "
            f"documented DG1032Z hardware limit {hardware_max_hz:.9g} Hz."
        )

    limits = channel.lab_limits
    if waveform_normalized not in {"DC", "NOIS"} and limits.frequency.enabled:
        _enforce_range("frequency", freq_hz, limits.frequency.min, limits.frequency.max, DIMENSION_FREQUENCY)
    if waveform_normalized != "DC" and limits.high_level.enabled:
        _enforce_range("high_level", high_v, limits.high_level.min, limits.high_level.max, DIMENSION_VOLTAGE)
    if waveform_normalized != "DC" and limits.low_level.enabled:
        _enforce_range("low_level", low_v, limits.low_level.min, limits.low_level.max, DIMENSION_VOLTAGE)
    if waveform_normalized != "DC" and limits.amplitude_vpp.enabled:
        _enforce_range("amplitude_vpp", high_v - low_v, limits.amplitude_vpp.min, limits.amplitude_vpp.max, DIMENSION_VOLTAGE)
    if limits.offset.enabled:
        _enforce_range("offset", (high_v + low_v) / 2.0, limits.offset.min, limits.offset.max, DIMENSION_VOLTAGE)

    if dut_min_impedance is None:
        if dut_envelope is not None and dut_envelope.minimum_impedance_ohm is not None:
            dut_min_impedance = dut_envelope.minimum_impedance_ohm
        elif safety.require_declared_dut_impedance:
            raise SafetyViolation("Declare the minimum DUT impedance before enabling Rigol output.")
        else:
            dut_min_impedance = limits.declared_dut_impedance.min
    if dut_envelope is not None:
        for name, value in (
            ("minimum impedance", dut_envelope.minimum_impedance_ohm),
            ("maximum current", dut_envelope.max_abs_current_a),
            ("maximum power", dut_envelope.max_abs_power_w),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise SafetyViolation(f"Rigol DUT {name} must be finite and positive.")
        declared_impedance = parse_quantity(
            dut_min_impedance,
            DIMENSION_RESISTANCE,
            require_unit=not isinstance(dut_min_impedance, (int, float)),
        ).si_value
        if (
            dut_envelope.minimum_impedance_ohm is not None
            and declared_impedance > dut_envelope.minimum_impedance_ohm
        ):
            raise SafetyViolation(
                "Declared Rigol DUT impedance is less conservative than the recipe DUT minimum."
            )
    estimate = estimate_rigol_current(
        high_level=high_v,
        low_level=low_v,
        output_load=output_load,
        dut_min_impedance=dut_min_impedance,
        source_resistance=safety.fixed_source_resistance,
    )
    open_circuit_peak_v = max(
        abs(estimate.open_circuit_high_v),
        abs(estimate.open_circuit_low_v),
    )
    if open_circuit_peak_v > _DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V:
        raise SafetyViolation(
            "Rigol requested open-circuit level "
            f"{open_circuit_peak_v:.9g} V exceeds the immutable DG1032Z "
            f"hardware limit {_DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V:.9g} V peak."
        )
    if waveform_normalized != "DC":
        open_circuit_vpp = (
            estimate.open_circuit_high_v - estimate.open_circuit_low_v
        )
        hardware_vpp_max = _rigol_hardware_open_circuit_vpp_max(
            freq_hz if waveform_normalized != "NOIS" else 0.0
        )
        if open_circuit_vpp < _DG1032Z_MIN_OPEN_CIRCUIT_VPP:
            raise SafetyViolation(
                "Rigol requested open-circuit amplitude "
                f"{open_circuit_vpp:.9g} Vpp is below the immutable DG1032Z "
                f"minimum {_DG1032Z_MIN_OPEN_CIRCUIT_VPP:.9g} Vpp."
            )
        if open_circuit_vpp > hardware_vpp_max:
            raise SafetyViolation(
                "Rigol requested open-circuit amplitude "
                f"{open_circuit_vpp:.9g} Vpp exceeds the immutable DG1032Z "
                f"limit {hardware_vpp_max:.9g} Vpp at {freq_hz:.9g} Hz."
            )
    current_limit = (
        parse_quantity(limits.estimated_load_current.max_abs or limits.estimated_load_current.max, DIMENSION_CURRENT).si_value
        if limits.estimated_load_current.enabled
        else math.inf
    )
    if dut_envelope is not None and dut_envelope.max_abs_current_a is not None:
        current_limit = min(current_limit, dut_envelope.max_abs_current_a)
    if estimate.peak_absolute_current_a > current_limit:
        raise SafetyViolation(
            "Estimated Rigol load current "
            f"{estimate.peak_absolute_current_a:.9g} A exceeds the {current_limit:.9g} A limit."
        )
    power_limit = (
        parse_quantity(
            limits.estimated_load_power.max_abs or limits.estimated_load_power.max,
            DIMENSION_POWER,
        ).si_value
        if limits.estimated_load_power.enabled
        else math.inf
    )
    if dut_envelope is not None and dut_envelope.max_abs_power_w is not None:
        power_limit = min(power_limit, dut_envelope.max_abs_power_w)
    if estimate.peak_estimated_dut_power_w > power_limit:
        raise SafetyViolation(
            "Estimated Rigol DUT power "
            f"{estimate.peak_estimated_dut_power_w:.9g} W exceeds the {power_limit:.9g} W limit."
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
    """Validate all sweep values against the configured channel limits."""

    numeric = (start_hz, stop_hz, duration_s, start_hold_s, stop_hold_s, return_time_s)
    if not all(math.isfinite(value) for value in numeric):
        raise SafetyViolation("Sweep values must be finite numbers.")
    if start_hz <= 0 or stop_hz <= 0 or start_hz == stop_hz:
        raise SafetyViolation("Sweep requires positive and different start/stop frequencies.")
    if not 1e-3 <= duration_s <= 500:
        raise SafetyViolation(
            "Rigol sweep duration must be within the hardware range 1 ms..500 s."
        )
    hold_times = (start_hold_s, stop_hold_s, return_time_s)
    if min(hold_times) < 0 or max(hold_times) > 500:
        raise SafetyViolation(
            "Rigol sweep hold and return times must be within 0..500 s."
        )
    if isinstance(steps, bool) or not isinstance(steps, int):
        raise SafetyViolation("Sweep step count must be an integer.")
    if not 2 <= steps <= 1024:
        raise SafetyViolation(
            "Rigol sweep step count must be within the hardware range 2..1024."
        )

    limits = channel.lab_limits
    if limits.frequency.enabled:
        _enforce_range("sweep_start", start_hz, limits.frequency.min, limits.frequency.max, DIMENSION_FREQUENCY)
        _enforce_range("sweep_stop", stop_hz, limits.frequency.min, limits.frequency.max, DIMENSION_FREQUENCY)
    if limits.sweep_duration.enabled:
        _enforce_range("sweep_duration", duration_s, limits.sweep_duration.min, limits.sweep_duration.max, "time")
    if limits.sweep_steps.enabled and not limits.sweep_steps.min <= steps <= limits.sweep_steps.max:
        raise SafetyViolation(
            f"sweep_steps={steps} outside configured range [{limits.sweep_steps.min}, {limits.sweep_steps.max}]."
        )
