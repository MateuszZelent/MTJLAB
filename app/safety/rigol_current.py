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
    QuantityError,
    parse_quantity,
)
from app.safety.precision import quantize_to_step
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

# DG1000Z programming resolutions used at the SCPI boundary.  Frequency is
# specified in the manual as 1 uHz.  Voltage amplitude is specified as
# 0.1 mVpp or four digits, whichever is coarser at the requested magnitude.
RIGOL_DG1000Z_FREQUENCY_RESOLUTION_HZ = 1e-6
RIGOL_DG1000Z_MIN_VOLTAGE_RESOLUTION_V = 1e-4


def rigol_voltage_resolution_v(value_v: float) -> float:
    """Return the conservative DG1000Z voltage step at ``value_v``."""

    if not math.isfinite(value_v):
        raise SafetyViolation("Rigol voltage must be finite before quantisation.")
    magnitude = abs(value_v)
    if magnitude == 0:
        return RIGOL_DG1000Z_MIN_VOLTAGE_RESOLUTION_V
    four_digit_step = 10.0 ** (math.floor(math.log10(magnitude)) - 3)
    return max(RIGOL_DG1000Z_MIN_VOLTAGE_RESOLUTION_V, four_digit_step)


def quantize_rigol_voltage(value_v: float) -> float:
    """Round a DG1000Z voltage to the documented four-digit/0.1 mV step."""

    return quantize_to_step(
        float(value_v),
        rigol_voltage_resolution_v(float(value_v)),
        name="Rigol voltage",
    )


def quantize_rigol_frequency(frequency_hz: float) -> float:
    """Round a DG1000Z frequency to its documented 1 uHz resolution."""

    return quantize_to_step(
        float(frequency_hz),
        RIGOL_DG1000Z_FREQUENCY_RESOLUTION_HZ,
        name="Rigol frequency",
    )


def rigol_hardware_frequency_max_hz(waveform: str | None = None) -> float:
    """Return the immutable DG1032Z ceiling used by UI and safety layers."""

    if waveform is None:
        return max(_DG1032Z_MAX_FREQUENCY_HZ.values())
    return _DG1032Z_MAX_FREQUENCY_HZ.get(
        waveform.strip().upper(),
        max(_DG1032Z_MAX_FREQUENCY_HZ.values()),
    )


def rigol_hardware_frequency_min_hz() -> float:
    """Return the documented minimum frequency resolution for UI bounds."""

    return RIGOL_DG1000Z_FREQUENCY_RESOLUTION_HZ


def rigol_hardware_voltage_bounds_v() -> tuple[float, float]:
    """Return the conservative open-circuit peak voltage envelope."""

    return -_DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V, _DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V


def rigol_hardware_amplitude_bounds_vpp() -> tuple[float, float]:
    """Return the documented open-circuit amplitude envelope."""

    return (
        _DG1032Z_MIN_OPEN_CIRCUIT_VPP,
        max(value for _frequency, value in _DG1032Z_MAX_OPEN_CIRCUIT_VPP_BY_FREQUENCY),
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
    source_resistance: str | float | Quantity = "50 ohm",
) -> RigolCurrentEstimate:
    """Calculate hardware-only worst cases from the documented Thevenin output."""

    high_v = parse_quantity(high_level, DIMENSION_VOLTAGE, require_unit=not isinstance(high_level, (int, float))).si_value
    low_v = parse_quantity(low_level, DIMENSION_VOLTAGE, require_unit=not isinstance(low_level, (int, float))).si_value
    source_ohm = parse_quantity(source_resistance, DIMENSION_RESISTANCE, require_unit=not isinstance(source_resistance, (int, float))).si_value
    numeric = (high_v, low_v, source_ohm)
    if not all(math.isfinite(value) for value in numeric):
        raise SafetyViolation("Rigol voltage and impedance values must be finite.")
    if source_ohm < 50.0 - 1e-9:
        raise SafetyViolation(
            f"Rigol source impedance cannot be below hardware minimum 50 Ω (received {source_ohm:.6g} Ω)."
        )
    open_high = _open_circuit_voltage(high_v, output_load, source_ohm)
    open_low = _open_circuit_voltage(low_v, output_load, source_ohm)
    # Short circuit maximises load current. Matching Rload=Rsource maximises
    # delivered load power. Neither bound assumes an MTJ resistance.
    current_high = open_high / source_ohm
    current_low = open_low / source_ohm
    return RigolCurrentEstimate(
        open_circuit_high_v=open_high,
        open_circuit_low_v=open_low,
        current_high_a=current_high,
        current_low_a=current_low,
        peak_absolute_current_a=max(abs(current_high), abs(current_low)),
        peak_estimated_dut_power_w=max(open_high * open_high, open_low * open_low) / (4 * source_ohm),
        source_resistance_ohm=source_ohm,
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
) -> RigolCurrentEstimate:
    """Validate a complete output configuration before a SCPI write is allowed."""

    waveform_normalized = waveform.strip().upper()
    if waveform_normalized not in channel.allowed_waveforms:
        raise SafetyViolation(f"Waveform {waveform_normalized} is not allowed for this channel.")
    try:
        freq_hz = parse_quantity(frequency, DIMENSION_FREQUENCY, require_unit=not isinstance(frequency, (int, float))).si_value
        high_v = parse_quantity(high_level, DIMENSION_VOLTAGE, require_unit=not isinstance(high_level, (int, float))).si_value
        low_v = parse_quantity(low_level, DIMENSION_VOLTAGE, require_unit=not isinstance(low_level, (int, float))).si_value
    except QuantityError as exc:
        raise SafetyViolation(f"Invalid Rigol waveform quantity: {exc}") from exc
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

    estimate = estimate_rigol_current(
        high_level=high_v,
        low_level=low_v,
        output_load=output_load,
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
