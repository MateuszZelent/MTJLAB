"""Read-only Rigol DG1032Z -> MTJ DC and quasi-static waveform calculator.

All physical values in domain models are normalized to SI units with explicit
unit suffixes (`_v`, `_a`, `_ohm`, `_w`, `_hz`, `_s`). The generator output
model delegates directly to `app.safety.rigol_current` (fixed 50 ohm series
source impedance; `HIGHZ` and numeric `LOAD` only alter the displayed voltage
convention). This module never communicates with hardware and never alters
safety limits.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_RESISTANCE,
    QuantityError,
    parse_quantity,
)
from app.safety.rigol_current import (
    _DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V,
    RIGOL_DG1000Z_MIN_VOLTAGE_RESOLUTION_V,
    _open_circuit_voltage,
    rigol_display_voltage_from_open_circuit,
    rigol_voltage_resolution_v,
    validate_rigol_waveform,
)
from app.settings.models import StationSettings

RIGOL_SOURCE_RESISTANCE_OHM: float = 50.0
DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V: float = _DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V
SUPPORTED_WAVEFORMS: tuple[str, ...] = ("DC", "SIN", "SQU", "RAMP", "PULS")


class CalculatorRangeError(ValueError):
    """Requested setting or target lies outside the measured curve domain."""

    def __init__(
        self,
        message: str,
        *,
        axis_name: str,
        min_value_si: float,
        max_value_si: float,
    ) -> None:
        super().__init__(message)
        self.axis_name = axis_name
        self.min_value_si = min_value_si
        self.max_value_si = max_value_si


class AmbiguousBranchError(ValueError):
    """Non-monotonic curve has multiple solutions for the requested value."""

    def __init__(
        self,
        message: str,
        *,
        candidates: tuple[RigolOperatingPoint, ...],
    ) -> None:
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True, slots=True)
class ReferencePoint:
    """One valid measured (I, V) sample on the MTJ curve, in SI units."""

    index: int
    current_a: float
    voltage_v: float
    resistance_ohm: float | None = None
    resistance_valid: bool = True
    keithley_b_a: float | None = None
    moke_vout_v: float | None = None


@dataclass(frozen=True, slots=True)
class ResistanceStats:
    """Summary statistics for valid non-zero-current MTJ resistance points."""

    count: int
    min_ohm: float
    median_ohm: float
    mean_ohm: float
    max_ohm: float


@dataclass(frozen=True, slots=True)
class ReferenceCurvePass:
    """Single directional sweep pass within a reference curve."""

    pass_index: int
    label: str
    points: tuple[ReferencePoint, ...]
    resistance_stats: ResistanceStats | None


@dataclass(frozen=True, slots=True)
class ReferenceIVCurve:
    """Recorded MTJ I-V curve at one (Keithley B, MOKE Box) condition."""

    curve_id: str
    title: str
    source_kind: str  # "hdf5" | "characterization_csv" | "characterization_json" | "sweep_csv"
    source_path: Path | None
    source_sha256: str
    sample_id: str
    sample_name: str
    coordinate_label: str
    mtj_channel: str
    keithley_b_setpoint_a: float | None
    keithley_b_measured_a: float | None
    moke_vout_v: float | None
    moke_hall_v: float | None
    sense_mode: str
    passes: tuple[ReferenceCurvePass, ...]
    excluded_point_count: int = 0
    exclusion_reasons: tuple[str, ...] = ()
    created_at_utc: str = ""

    @property
    def resistance_stats(self) -> ResistanceStats | None:
        if not self.passes:
            return None
        if len(self.passes) == 1:
            return self.passes[0].resistance_stats
        all_points = tuple(pt for p in self.passes for pt in p.points)
        return compute_resistance_stats(all_points)

    @property
    def total_point_count(self) -> int:
        return sum(len(p.points) for p in self.passes)


@dataclass(frozen=True, slots=True)
class ConstantResistanceModel:
    """Linear constant-resistance MTJ model with optional [min, max] uncertainty band."""

    resistance_ohm: float
    minimum_ohm: float | None = None
    maximum_ohm: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.resistance_ohm) or self.resistance_ohm <= 0.0:
            raise ValueError("MTJ resistance must be finite and greater than 0 ohm.")
        if (self.minimum_ohm is None) != (self.maximum_ohm is None):
            raise ValueError("Specify both minimum and maximum resistance bounds, or neither.")
        if self.minimum_ohm is not None and self.maximum_ohm is not None:
            if not (
                math.isfinite(self.minimum_ohm)
                and math.isfinite(self.maximum_ohm)
                and self.minimum_ohm > 0.0
                and self.maximum_ohm > 0.0
            ):
                raise ValueError("Resistance bounds must be finite and greater than 0 ohm.")
            if not (self.minimum_ohm <= self.resistance_ohm <= self.maximum_ohm):
                raise ValueError(
                    "Require minimum resistance <= nominal resistance <= maximum resistance."
                )

    @property
    def has_band(self) -> bool:
        return (
            self.minimum_ohm is not None
            and self.maximum_ohm is not None
            and not math.isclose(self.minimum_ohm, self.maximum_ohm, rel_tol=1e-12, abs_tol=1e-12)
        )


@dataclass(frozen=True, slots=True)
class RigolLoadSpec:
    """Validated Rigol LOAD convention mapped to its open-circuit multiplier."""

    raw_label: str
    is_high_z: bool
    load_ohm: float | None
    open_circuit_scale: float

    @property
    def wire_value(self) -> str | float:
        return "HIGHZ" if self.is_high_z else float(self.load_ohm or 50.0)


@dataclass(frozen=True, slots=True)
class RigolOperatingPoint:
    """Solved DC / instantaneous operating point across the 50-ohm Rigol + MTJ circuit."""

    rigol_setpoint_v: float
    open_circuit_v: float
    mtj_voltage_v: float
    mtj_current_a: float
    mtj_power_w: float
    source_power_w: float
    static_resistance_ohm: float | None
    differential_resistance_ohm: float | None
    voltage_transfer_ratio: float | None
    reflection_gamma: float | None
    exceeds_hardware_oc_limit: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OperatingBand:
    """Min/max envelope across a constant-resistance uncertainty band."""

    rigol_setpoint_min_v: float
    rigol_setpoint_max_v: float
    mtj_voltage_min_v: float
    mtj_voltage_max_v: float
    mtj_current_min_a: float
    mtj_current_max_a: float
    mtj_power_min_w: float
    mtj_power_max_w: float


@dataclass(frozen=True, slots=True)
class DcSolveResult:
    """Complete DC calculation output with optional resistance band and safety advisory."""

    input_key: str
    load: RigolLoadSpec
    point: RigolOperatingPoint
    band: OperatingBand | None
    valid_input_range_si: tuple[float, float] | None
    advisory_safety_message: str | None = None


@dataclass(frozen=True, slots=True)
class WaveformRequest:
    """Quasi-static periodic waveform description in Rigol display coordinates."""

    waveform: str
    high_v: float
    low_v: float
    output_load: str | float
    frequency_hz: float = 1_000.0
    duty_percent: float = 50.0
    ramp_symmetry_percent: float = 50.0
    pulse_width_s: float | None = None
    pulse_leading_s: float = 0.0
    pulse_trailing_s: float = 0.0
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class WaveformEnvelope:
    """Quasi-static single-cycle statistics on the MTJ terminals."""

    mtj_voltage_min_v: float
    mtj_voltage_max_v: float
    mtj_voltage_peak_v: float
    mtj_voltage_vpp_v: float
    mtj_voltage_mean_v: float
    mtj_voltage_rms_v: float
    mtj_current_min_a: float
    mtj_current_max_a: float
    mtj_current_peak_a: float
    mtj_current_mean_a: float
    mtj_current_rms_a: float
    mtj_power_peak_w: float
    mtj_power_avg_w: float


@dataclass(frozen=True, slots=True)
class WaveformSolveResult:
    """Complete quasi-static waveform estimate across one cycle."""

    request: WaveformRequest
    load: RigolLoadSpec
    nominal: WaveformEnvelope
    resistance_range: WaveformEnvelope | None
    low_point: RigolOperatingPoint
    high_point: RigolOperatingPoint
    exceeds_hardware_oc_limit: bool
    warnings: tuple[str, ...] = ()
    advisory_safety_message: str | None = None


@dataclass(frozen=True, slots=True)
class TransferSeries:
    """Plottable transfer curve V_MTJ(V_set) and I_MTJ(V_set) with optional R band."""

    rigol_setpoint_v: tuple[float, ...]
    open_circuit_v: tuple[float, ...]
    mtj_voltage_v: tuple[float, ...]
    mtj_current_a: tuple[float, ...]
    band_low_voltage_v: tuple[float, ...] | None = None
    band_high_voltage_v: tuple[float, ...] | None = None
    band_low_current_a: tuple[float, ...] | None = None
    band_high_current_a: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class RigolVoltageLimitEstimate:
    """Conservative Rigol V_set bounds and symmetric limit derived from MTJ voltage bounds."""

    load: RigolLoadSpec
    requested_mtj_min_v: float
    requested_mtj_max_v: float
    effective_mtj_min_v: float
    effective_mtj_max_v: float
    rigol_min_v: float
    rigol_max_v: float
    rigol_symmetric_limit_v: float
    open_circuit_limit_v: float
    estimated_current_max_a: float
    governing_resistance_ohm: float | None = None
    clamped_to_measured_range: bool = False
    clamped_to_hardware_limit: bool = False
    warnings: tuple[str, ...] = ()


def parse_rigol_load(value: str | float) -> RigolLoadSpec:
    """Parse a Rigol LOAD setting (`HIGHZ` or positive resistance) via canonical rules."""
    if isinstance(value, bool):
        raise TypeError("Rigol LOAD cannot be a boolean value.")
    if isinstance(value, (int, float)):
        load_ohm = float(value)
        if not math.isfinite(load_ohm) or load_ohm <= 0.0:
            raise ValueError("Rigol LOAD resistance must be finite and greater than 0 ohm.")
        scale = _open_circuit_voltage(1.0, load_ohm, RIGOL_SOURCE_RESISTANCE_OHM)
        return RigolLoadSpec(
            raw_label=f"{load_ohm:g} Ω",
            is_high_z=False,
            load_ohm=load_ohm,
            open_circuit_scale=scale,
        )

    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError("Rigol LOAD cannot be empty; enter HIGHZ or a resistance (e.g. 50 ohm).")
    compact = cleaned.upper().replace(" ", "").replace("-", "").replace("_", "")
    if compact in {"HIGHZ", "INF", "INFINITY", "HIZ"}:
        return RigolLoadSpec(
            raw_label="HighZ",
            is_high_z=True,
            load_ohm=None,
            open_circuit_scale=_open_circuit_voltage(1.0, "HIGHZ", RIGOL_SOURCE_RESISTANCE_OHM),
        )

    # Try explicit-unit quantity parsing first; fall back to bare positive ohm string
    # because Rigol SCPI / RigolPage load field accepts bare numeric ohms like "50".
    try:
        load_ohm = parse_quantity(cleaned, DIMENSION_RESISTANCE, require_unit=True).si_value
    except QuantityError as exc:
        normalized_num = cleaned.replace(",", ".")
        try:
            load_ohm = float(normalized_num)
        except ValueError:
            raise ValueError(
                f"Invalid Rigol LOAD {value!r}: expected HIGHZ or a resistance ({exc})."
            ) from exc

    if not math.isfinite(load_ohm) or load_ohm <= 0.0:
        raise ValueError(f"Rigol LOAD resistance must be finite and positive (received {value!r}).")
    scale = _open_circuit_voltage(1.0, load_ohm, RIGOL_SOURCE_RESISTANCE_OHM)
    return RigolLoadSpec(
        raw_label=f"{load_ohm:g} Ω",
        is_high_z=False,
        load_ohm=load_ohm,
        open_circuit_scale=scale,
    )


def compute_resistance_stats(points: Sequence[ReferencePoint]) -> ResistanceStats | None:
    """Compute min/median/mean/max R while excluding near-zero current division artifacts."""
    if not points:
        return None
    finite_currents = [abs(pt.current_a) for pt in points if math.isfinite(pt.current_a)]
    max_abs_i = max(finite_currents, default=0.0)
    if max_abs_i <= 0.0:
        return None
    # Keep resistance accurate for both mA-scale and uA-scale sweeps while excluding
    # the ~0 A offset notch (e.g. -5.3 uA in a 5 mA sweep).
    min_abs_current_a = min(1e-5, 0.01 * max_abs_i)
    if max_abs_i >= 1e-3:
        min_abs_current_a = max(min_abs_current_a, 1e-5)

    values: list[float] = []
    for pt in points:
        if not pt.resistance_valid:
            continue
        if not (math.isfinite(pt.current_a) and math.isfinite(pt.voltage_v)):
            continue
        if abs(pt.current_a) < min_abs_current_a:
            continue
        r = pt.resistance_ohm
        if r is None:
            r = abs(pt.voltage_v / pt.current_a)
        if math.isfinite(r) and r > 0.0:
            values.append(float(r))
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    return ResistanceStats(
        count=len(values),
        min_ohm=float(np.min(arr)),
        median_ohm=float(np.median(arr)),
        mean_ohm=float(np.mean(arr)),
        max_ohm=float(np.max(arr)),
    )


def split_curve_passes(
    points: Sequence[ReferencePoint],
    *,
    setpoints: Sequence[float | None] | None = None,
) -> tuple[ReferenceCurvePass, ...]:
    """Split a recorded sweep into directional passes to preserve hysteresis cleanly."""
    if not points:
        return ()
    if len(points) < 3:
        pts = tuple(points)
        return (
            ReferenceCurvePass(
                pass_index=1,
                label="Pass 1",
                points=pts,
                resistance_stats=compute_resistance_stats(pts),
            ),
        )

    # Choose the driving coordinate to detect sweep direction reversals.
    coords: list[float]
    if setpoints is not None and len(setpoints) == len(points) and all(
        v is not None and math.isfinite(v) for v in setpoints
    ):
        coords = [float(v) for v in setpoints]  # type: ignore[arg-type]
        if max(coords) - min(coords) <= 0.0:
            coords = [pt.current_a for pt in points]
    else:
        coords = [pt.current_a for pt in points]
        if max(coords) - min(coords) <= 0.0:
            coords = [pt.voltage_v for pt in points]

    deltas = [coords[i + 1] - coords[i] for i in range(len(coords) - 1)]
    nonzero_abs = [abs(d) for d in deltas if abs(d) > 0.0]
    if not nonzero_abs:
        pts = tuple(points)
        return (
            ReferenceCurvePass(
                pass_index=1,
                label="Pass 1",
                points=pts,
                resistance_stats=compute_resistance_stats(pts),
            ),
        )

    median_step = float(np.median(nonzero_abs))
    reversal_threshold = 0.2 * median_step

    raw_passes: list[list[ReferencePoint]] = [[points[0]]]
    direction = 0
    for idx in range(1, len(points)):
        delta = coords[idx] - coords[idx - 1]
        if abs(delta) >= reversal_threshold:
            step_dir = 1 if delta > 0.0 else -1
            if direction == 0:
                direction = step_dir
            elif step_dir != direction:
                # Start a new pass sharing the turning point so both passes remain continuous.
                raw_passes.append([points[idx - 1]])
                direction = step_dir
        raw_passes[-1].append(points[idx])

    passes: list[ReferenceCurvePass] = []
    for i, pass_points in enumerate(raw_passes, start=1):
        if len(pass_points) < 2 and len(raw_passes) > 1:
            continue
        pts = tuple(pass_points)
        direction_tag = ""
        if len(pts) >= 2:
            d_curr = pts[-1].current_a - pts[0].current_a
            if d_curr > 0:
                direction_tag = " (↑)"
            elif d_curr < 0:
                direction_tag = " (↓)"
        passes.append(
            ReferenceCurvePass(
                pass_index=len(passes) + 1,
                label=f"Pass {len(passes) + 1}{direction_tag}",
                points=pts,
                resistance_stats=compute_resistance_stats(pts),
            )
        )
    if not passes:
        pts = tuple(points)
        return (
            ReferenceCurvePass(
                pass_index=1,
                label="Pass 1",
                points=pts,
                resistance_stats=compute_resistance_stats(pts),
            ),
        )
    return tuple(passes)


def _build_operating_point(
    *,
    mtj_voltage_v: float,
    mtj_current_a: float,
    load: RigolLoadSpec,
    differential_resistance_ohm: float | None,
    warnings: Sequence[str] = (),
) -> RigolOperatingPoint:
    open_circuit_v = mtj_voltage_v + RIGOL_SOURCE_RESISTANCE_OHM * mtj_current_a
    rigol_setpoint_v = rigol_display_voltage_from_open_circuit(open_circuit_v, load.wire_value)
    mtj_power_w = mtj_voltage_v * mtj_current_a
    source_power_w = RIGOL_SOURCE_RESISTANCE_OHM * mtj_current_a * mtj_current_a
    static_r: float | None = None
    reflection: float | None = None
    if abs(mtj_current_a) > 1e-12:
        candidate = mtj_voltage_v / mtj_current_a
        if math.isfinite(candidate) and candidate > 0.0:
            static_r = candidate
            reflection = (static_r - RIGOL_SOURCE_RESISTANCE_OHM) / (
                static_r + RIGOL_SOURCE_RESISTANCE_OHM
            )
    transfer_ratio: float | None = None
    if abs(rigol_setpoint_v) > 1e-12:
        ratio = mtj_voltage_v / rigol_setpoint_v
        if math.isfinite(ratio):
            transfer_ratio = ratio
    exceeds_hw = abs(open_circuit_v) > DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V + 1e-12
    warn_list = list(warnings)
    if exceeds_hw:
        warn_list.append(
            f"Open-circuit voltage {open_circuit_v:.4g} V exceeds DG1032Z ±{DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V:g} V hardware ceiling."
        )
    return RigolOperatingPoint(
        rigol_setpoint_v=rigol_setpoint_v,
        open_circuit_v=open_circuit_v,
        mtj_voltage_v=mtj_voltage_v,
        mtj_current_a=mtj_current_a,
        mtj_power_w=mtj_power_w,
        source_power_w=source_power_w,
        static_resistance_ohm=static_r,
        differential_resistance_ohm=differential_resistance_ohm,
        voltage_transfer_ratio=transfer_ratio,
        reflection_gamma=reflection,
        exceeds_hardware_oc_limit=exceeds_hw,
        warnings=tuple(warn_list),
    )


def _advisory_station_safety_message(
    *,
    settings: StationSettings | None,
    channel: int,
    waveform: str,
    frequency_hz: float,
    high_v: float,
    low_v: float,
    load: RigolLoadSpec,
) -> str | None:
    if settings is None:
        return None
    channel_settings = settings.rigol.safety.channels.get(str(channel))
    if channel_settings is None:
        return None
    try:
        validate_rigol_waveform(
            channel=channel_settings,
            safety=settings.rigol.safety,
            waveform=waveform,
            frequency=frequency_hz,
            high_level=high_v,
            low_level=low_v,
            output_load=load.wire_value,
        )
    except SafetyViolation as exc:
        return str(exc)
    return None


def _solve_constant_r_point(
    resistance_ohm: float,
    *,
    input_key: str,
    target_si: float,
    load: RigolLoadSpec,
) -> RigolOperatingPoint:
    if input_key == "setting":
        open_circuit_v = target_si * load.open_circuit_scale
        current_a = open_circuit_v / (resistance_ohm + RIGOL_SOURCE_RESISTANCE_OHM)
        voltage_v = current_a * resistance_ohm
    elif input_key == "voltage":
        voltage_v = target_si
        current_a = voltage_v / resistance_ohm
    elif input_key == "current":
        current_a = target_si
        voltage_v = current_a * resistance_ohm
    else:
        raise ValueError(f"Unknown input_key {input_key!r}; expected setting, voltage, or current.")
    return _build_operating_point(
        mtj_voltage_v=voltage_v,
        mtj_current_a=current_a,
        load=load,
        differential_resistance_ohm=resistance_ohm,
    )


def _is_monotonic(values: Sequence[float]) -> bool:
    if len(values) <= 2:
        return True
    non_decreasing = all(values[i + 1] >= values[i] - 1e-15 for i in range(len(values) - 1))
    non_increasing = all(values[i + 1] <= values[i] + 1e-15 for i in range(len(values) - 1))
    return non_decreasing or non_increasing


def _pass_axis_values(
    curve_pass: ReferenceCurvePass,
    load: RigolLoadSpec,
) -> tuple[list[float], list[float], list[float], list[float]]:
    currents = [pt.current_a for pt in curve_pass.points]
    voltages = [pt.voltage_v for pt in curve_pass.points]
    oc_values = [v + RIGOL_SOURCE_RESISTANCE_OHM * i for i, v in zip(currents, voltages, strict=True)]
    set_values = [
        rigol_display_voltage_from_open_circuit(oc, load.wire_value) for oc in oc_values
    ]
    return set_values, oc_values, voltages, currents


def _solve_curve_pass_point(
    curve_pass: ReferenceCurvePass,
    *,
    input_key: str,
    target_si: float,
    load: RigolLoadSpec,
) -> tuple[RigolOperatingPoint, tuple[float, float]]:
    if len(curve_pass.points) < 2:
        raise ValueError("Reference curve pass requires at least 2 valid points.")
    set_values, _oc_values, voltages, currents = _pass_axis_values(curve_pass, load)
    axis_map = {
        "setting": set_values,
        "voltage": voltages,
        "current": currents,
    }
    if input_key not in axis_map:
        raise ValueError(f"Unknown input_key {input_key!r}; expected setting, voltage, or current.")
    axis_vals = axis_map[input_key]
    low_bound = min(axis_vals)
    high_bound = max(axis_vals)
    span = high_bound - low_bound
    tol = max(abs(low_bound), abs(high_bound), span, 1.0) * 1e-12
    if target_si < low_bound - tol or target_si > high_bound + tol:
        raise CalculatorRangeError(
            f"Requested {input_key} ({target_si:.6g} SI) is outside the measured curve range "
            f"[{low_bound:.6g}, {high_bound:.6g}] SI. Extrapolation is disabled.",
            axis_name=input_key,
            min_value_si=low_bound,
            max_value_si=high_bound,
        )
    clamped_target = min(max(target_si, low_bound), high_bound)
    monotonic = _is_monotonic(axis_vals)
    warnings: tuple[str, ...] = (
        ("Reference pass is non-monotonic along this axis; verify switching branches.",)
        if not monotonic
        else ()
    )

    candidates: list[RigolOperatingPoint] = []
    for idx in range(len(axis_vals) - 1):
        a0, a1 = axis_vals[idx], axis_vals[idx + 1]
        seg_low, seg_high = (a0, a1) if a0 <= a1 else (a1, a0)
        seg_tol = max(abs(seg_low), abs(seg_high), 1.0) * 1e-12
        if not (seg_low - seg_tol <= clamped_target <= seg_high + seg_tol):
            continue
        if abs(a1 - a0) <= 1e-15:
            frac = 0.0
        else:
            frac = min(1.0, max(0.0, (clamped_target - a0) / (a1 - a0)))
        v0, v1 = voltages[idx], voltages[idx + 1]
        i0, i1 = currents[idx], currents[idx + 1]
        v_interp = v0 + frac * (v1 - v0)
        i_interp = i0 + frac * (i1 - i0)
        diff_r = (v1 - v0) / (i1 - i0) if abs(i1 - i0) > 1e-15 else None
        if diff_r is not None and not math.isfinite(diff_r):
            diff_r = None
        candidate = _build_operating_point(
            mtj_voltage_v=v_interp,
            mtj_current_a=i_interp,
            load=load,
            differential_resistance_ohm=diff_r,
            warnings=warnings,
        )
        # Deduplicate shared vertices between adjacent segments.
        if any(
            math.isclose(candidate.mtj_voltage_v, existing.mtj_voltage_v, rel_tol=1e-9, abs_tol=1e-12)
            and math.isclose(candidate.mtj_current_a, existing.mtj_current_a, rel_tol=1e-9, abs_tol=1e-15)
            for existing in candidates
        ):
            continue
        candidates.append(candidate)

    if not candidates:
        raise CalculatorRangeError(
            f"No segment on the measured curve brackets {input_key}={target_si:.6g} SI.",
            axis_name=input_key,
            min_value_si=low_bound,
            max_value_si=high_bound,
        )
    if len(candidates) > 1:
        raise AmbiguousBranchError(
            f"Non-monotonic curve has {len(candidates)} branches at {input_key}={target_si:.6g} SI "
            "(for example an MTJ switching step).",
            candidates=tuple(candidates),
        )
    return candidates[0], (low_bound, high_bound)


def solve_dc_point(
    model: ReferenceCurvePass | ConstantResistanceModel,
    *,
    input_key: str,
    target_si: float,
    output_load: str | float,
    settings: StationSettings | None = None,
    channel: int = 1,
) -> DcSolveResult:
    """Solve one DC operating point bidirectionally for a measured pass or fixed resistor."""
    if not math.isfinite(target_si):
        raise ValueError("Input value must be finite.")
    load = parse_rigol_load(output_load)

    band: OperatingBand | None = None
    valid_range: tuple[float, float] | None = None
    if isinstance(model, ConstantResistanceModel):
        point = _solve_constant_r_point(
            model.resistance_ohm, input_key=input_key, target_si=target_si, load=load
        )
        if model.has_band and model.minimum_ohm is not None and model.maximum_ohm is not None:
            p_low = _solve_constant_r_point(
                model.minimum_ohm, input_key=input_key, target_si=target_si, load=load
            )
            p_high = _solve_constant_r_point(
                model.maximum_ohm, input_key=input_key, target_si=target_si, load=load
            )
            band = OperatingBand(
                rigol_setpoint_min_v=min(p_low.rigol_setpoint_v, p_high.rigol_setpoint_v),
                rigol_setpoint_max_v=max(p_low.rigol_setpoint_v, p_high.rigol_setpoint_v),
                mtj_voltage_min_v=min(p_low.mtj_voltage_v, p_high.mtj_voltage_v),
                mtj_voltage_max_v=max(p_low.mtj_voltage_v, p_high.mtj_voltage_v),
                mtj_current_min_a=min(p_low.mtj_current_a, p_high.mtj_current_a),
                mtj_current_max_a=max(p_low.mtj_current_a, p_high.mtj_current_a),
                mtj_power_min_w=min(p_low.mtj_power_w, p_high.mtj_power_w),
                mtj_power_max_w=max(p_low.mtj_power_w, p_high.mtj_power_w),
            )
    elif isinstance(model, ReferenceCurvePass):
        point, valid_range = _solve_curve_pass_point(
            model, input_key=input_key, target_si=target_si, load=load
        )
    else:
        raise TypeError(f"Unsupported load model {type(model).__name__}.")

    advisory = _advisory_station_safety_message(
        settings=settings,
        channel=channel,
        waveform="DC",
        frequency_hz=1_000.0,
        high_v=point.rigol_setpoint_v,
        low_v=point.rigol_setpoint_v,
        load=load,
    )
    return DcSolveResult(
        input_key=input_key,
        load=load,
        point=point,
        band=band,
        valid_input_range_si=valid_range,
        advisory_safety_message=advisory,
    )


def _sample_waveform_quadrature(
    request: WaveformRequest,
    samples: int,
) -> tuple[np.ndarray, np.ndarray, tuple[str, ...]]:
    """Return `(voltages_v, weights, advisory_warnings)` over one period (weights sum to 1).

    Uses exact piecewise-weighted quadrature for SQU, RAMP, and PULS so narrow
    MTJ pulses (e.g. 100 ns at 1 kHz) remain accurate without uniform-grid aliasing.
    For PULS, `pulse_width_s` follows the DG1032Z 50%-50% FWHM convention and
    `pulse_leading_s` / `pulse_trailing_s` follow the 10%-90% transition convention.
    """
    waveform = request.waveform.strip().upper()
    if waveform not in SUPPORTED_WAVEFORMS:
        raise ValueError(
            f"Unsupported waveform {request.waveform!r}; supported: {', '.join(SUPPORTED_WAVEFORMS)}."
        )
    if not (math.isfinite(request.high_v) and math.isfinite(request.low_v)):
        raise ValueError("Waveform High and Low levels must be finite.")
    if waveform == "DC":
        if not math.isclose(request.high_v, request.low_v, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("DC waveform requires High level equal to Low level.")
        return np.asarray([request.high_v], dtype=float), np.asarray([1.0], dtype=float), ()
    if request.high_v < request.low_v:
        raise ValueError("Waveform High level must be greater than or equal to Low level.")
    if not math.isfinite(request.frequency_hz) or request.frequency_hz <= 0.0:
        raise ValueError("Waveform frequency must be finite and positive.")

    vpp = request.high_v - request.low_v
    offset = 0.5 * (request.high_v + request.low_v)
    warnings: list[str] = []

    def _maybe_invert(arr: np.ndarray) -> np.ndarray:
        return (2.0 * offset - arr) if request.inverted else arr

    if waveform == "SIN":
        theta = (np.arange(samples, dtype=float) + 0.5) / float(samples)
        vals = _maybe_invert(offset + 0.5 * vpp * np.sin(2.0 * math.pi * theta))
        weights = np.full(samples, 1.0 / float(samples), dtype=float)
        return vals, weights, ()

    if waveform == "SQU":
        if not math.isfinite(request.duty_percent) or not (0.0 < request.duty_percent < 100.0):
            raise ValueError("Square duty cycle must be strictly between 0% and 100%.")
        duty = request.duty_percent / 100.0
        vals = _maybe_invert(np.asarray([request.high_v, request.low_v], dtype=float))
        weights = np.asarray([duty, 1.0 - duty], dtype=float)
        return vals, weights, ()

    if waveform == "RAMP":
        if not math.isfinite(request.ramp_symmetry_percent) or not (
            0.0 <= request.ramp_symmetry_percent <= 100.0
        ):
            raise ValueError("Ramp symmetry must be between 0% and 100%.")
        sym = request.ramp_symmetry_percent / 100.0
        n_seg = max(64, samples // 2)
        u = (np.arange(n_seg, dtype=float) + 0.5) / float(n_seg)
        v_parts: list[np.ndarray] = []
        w_parts: list[np.ndarray] = []
        if sym > 1e-12:
            v_parts.append(request.low_v + vpp * u)
            w_parts.append(np.full(n_seg, sym / float(n_seg), dtype=float))
        if 1.0 - sym > 1e-12:
            v_parts.append(request.high_v - vpp * u)
            w_parts.append(np.full(n_seg, (1.0 - sym) / float(n_seg), dtype=float))
        vals = _maybe_invert(np.concatenate(v_parts))
        weights = np.concatenate(w_parts)
        return vals, weights / float(np.sum(weights)), ()

    # PULS: DG1032Z 50%-50% FWHM width + 10%-90% leading/trailing edges.
    period_s = 1.0 / request.frequency_hz
    if request.pulse_width_s is not None:
        width_s = float(request.pulse_width_s)
        if not math.isfinite(width_s) or not (0.0 < width_s < period_s):
            raise ValueError(
                f"Pulse width ({width_s:g} s) must lie strictly between 0 and period ({period_s:g} s)."
            )
    else:
        if not math.isfinite(request.duty_percent) or not (0.0 < request.duty_percent < 100.0):
            raise ValueError("Pulse duty cycle must be strictly between 0% and 100%.")
        width_s = period_s * (request.duty_percent / 100.0)

    lead_10_90 = float(request.pulse_leading_s)
    trail_10_90 = float(request.pulse_trailing_s)
    if (
        not (math.isfinite(lead_10_90) and math.isfinite(trail_10_90))
        or lead_10_90 < 0.0
        or trail_10_90 < 0.0
    ):
        raise ValueError("Pulse edge times must be finite and non-negative.")

    # Full 0%-100% linear ramp durations from 10%-90% specification.
    t_rise = lead_10_90 / 0.8
    t_fall = trail_10_90 / 0.8
    if 0.5 * (t_rise + t_fall) > width_s + 1e-15:
        raise ValueError(
            "Pulse leading/trailing edge times are too long for the specified 50%-50% pulse width."
        )
    t_high = max(0.0, width_s - 0.5 * (t_rise + t_fall))
    t_low = period_s - t_high - t_rise - t_fall
    if t_low < -1e-15:
        raise ValueError(
            "Pulse width plus transition edges exceed the waveform period."
        )
    t_low = max(0.0, t_low)

    if width_s < 16e-9:
        warnings.append("Pulse width is below the DG1032Z 16 ns hardware minimum.")
    if width_s >= period_s - 32e-9:
        warnings.append("Pulse width exceeds the DG1032Z (period − 32 ns) timing margin.")
    for name, edge in (("leading", lead_10_90), ("trailing", trail_10_90)):
        if 0.0 < edge < 10e-9:
            warnings.append(f"Pulse {name} edge is below the DG1032Z 10 ns hardware minimum.")
        if edge > 0.625 * width_s + 1e-15:
            warnings.append(f"Pulse {name} edge exceeds DG1032Z 0.625 × pulse width limit.")

    n_edge = max(32, samples // 8)
    u = (np.arange(n_edge, dtype=float) + 0.5) / float(n_edge)
    v_parts = []
    w_parts = []
    if t_rise > 0.0:
        v_parts.append(request.low_v + vpp * u)
        w_parts.append(np.full(n_edge, (t_rise / period_s) / float(n_edge), dtype=float))
    if t_high > 0.0:
        v_parts.append(np.asarray([request.high_v], dtype=float))
        w_parts.append(np.asarray([t_high / period_s], dtype=float))
    if t_fall > 0.0:
        v_parts.append(request.high_v - vpp * u)
        w_parts.append(np.full(n_edge, (t_fall / period_s) / float(n_edge), dtype=float))
    if t_low > 0.0:
        v_parts.append(np.asarray([request.low_v], dtype=float))
        w_parts.append(np.asarray([t_low / period_s], dtype=float))

    vals = _maybe_invert(np.concatenate(v_parts))
    weights = np.concatenate(w_parts)
    weights = weights / float(np.sum(weights))
    return vals, weights, tuple(warnings)


def _envelope_from_samples(
    v_samples: np.ndarray,
    i_samples: np.ndarray,
    weights: np.ndarray,
    *,
    extra_v: Sequence[float] = (),
    extra_i: Sequence[float] = (),
) -> WaveformEnvelope:
    v_min = float(min(float(np.min(v_samples)), *(extra_v or (float(np.min(v_samples)),))))
    v_max = float(max(float(np.max(v_samples)), *(extra_v or (float(np.max(v_samples)),))))
    i_min = float(min(float(np.min(i_samples)), *(extra_i or (float(np.min(i_samples)),))))
    i_max = float(max(float(np.max(i_samples)), *(extra_i or (float(np.max(i_samples)),))))
    p_samples = v_samples * i_samples
    p_extra = [v * i for v, i in zip(extra_v, extra_i, strict=False)]
    p_peak = float(max(float(np.max(np.abs(p_samples))), *(abs(p) for p in p_extra)))
    return WaveformEnvelope(
        mtj_voltage_min_v=v_min,
        mtj_voltage_max_v=v_max,
        mtj_voltage_peak_v=max(abs(v_min), abs(v_max)),
        mtj_voltage_vpp_v=v_max - v_min,
        mtj_voltage_mean_v=float(np.sum(weights * v_samples)),
        mtj_voltage_rms_v=float(np.sqrt(np.sum(weights * v_samples * v_samples))),
        mtj_current_min_a=i_min,
        mtj_current_max_a=i_max,
        mtj_current_peak_a=max(abs(i_min), abs(i_max)),
        mtj_current_mean_a=float(np.sum(weights * i_samples)),
        mtj_current_rms_a=float(np.sqrt(np.sum(weights * i_samples * i_samples))),
        mtj_power_peak_w=p_peak,
        mtj_power_avg_w=float(np.sum(weights * p_samples)),
    )


def solve_waveform(
    model: ReferenceCurvePass | ConstantResistanceModel,
    request: WaveformRequest,
    *,
    settings: StationSettings | None = None,
    channel: int = 1,
    samples: int = 1024,
) -> WaveformSolveResult:
    """Evaluate a periodic Rigol waveform quasi-statically on the MTJ load model."""
    load = parse_rigol_load(request.output_load)
    waveform = request.waveform.strip().upper()
    cycle_set_v, weights, shape_warnings = _sample_waveform_quadrature(request, samples)

    # Validate exact Low and High endpoints first (enforces curve range & monotonicity).
    low_dc = solve_dc_point(
        model, input_key="setting", target_si=request.low_v, output_load=load.wire_value
    )
    high_dc = solve_dc_point(
        model, input_key="setting", target_si=request.high_v, output_load=load.wire_value
    )

    warnings: list[str] = list(
        dict.fromkeys((*low_dc.point.warnings, *high_dc.point.warnings, *shape_warnings))
    )
    if request.inverted:
        warnings.append("Output polarity INV applied (waveform reflected around offset).")
    if waveform != "DC":
        warnings.append(
            "Quasi-static resistive/I-V estimate: ignores RF reactance, parasitic capacitance, and line transients."
        )

    resistance_range_env: WaveformEnvelope | None = None
    if isinstance(model, ConstantResistanceModel):
        oc_cycle = cycle_set_v * load.open_circuit_scale

        def _const_env(r_ohm: float) -> WaveformEnvelope:
            i_arr = oc_cycle / (r_ohm + RIGOL_SOURCE_RESISTANCE_OHM)
            v_arr = i_arr * r_ohm
            low_p = _solve_constant_r_point(
                r_ohm, input_key="setting", target_si=request.low_v, load=load
            )
            high_p = _solve_constant_r_point(
                r_ohm, input_key="setting", target_si=request.high_v, load=load
            )
            return _envelope_from_samples(
                v_arr,
                i_arr,
                weights,
                extra_v=(low_p.mtj_voltage_v, high_p.mtj_voltage_v),
                extra_i=(low_p.mtj_current_a, high_p.mtj_current_a),
            )

        nominal = _const_env(model.resistance_ohm)
        if model.has_band and model.minimum_ohm is not None and model.maximum_ohm is not None:
            env_min_r = _const_env(model.minimum_ohm)
            env_max_r = _const_env(model.maximum_ohm)
            v_min = min(env_min_r.mtj_voltage_min_v, env_max_r.mtj_voltage_min_v)
            v_max = max(env_min_r.mtj_voltage_max_v, env_max_r.mtj_voltage_max_v)
            i_min = min(env_min_r.mtj_current_min_a, env_max_r.mtj_current_min_a)
            i_max = max(env_min_r.mtj_current_max_a, env_max_r.mtj_current_max_a)
            resistance_range_env = WaveformEnvelope(
                mtj_voltage_min_v=v_min,
                mtj_voltage_max_v=v_max,
                mtj_voltage_peak_v=max(abs(v_min), abs(v_max)),
                mtj_voltage_vpp_v=max(env_min_r.mtj_voltage_vpp_v, env_max_r.mtj_voltage_vpp_v),
                mtj_voltage_mean_v=max(
                    env_min_r.mtj_voltage_mean_v, env_max_r.mtj_voltage_mean_v, key=abs
                ),
                mtj_voltage_rms_v=max(env_min_r.mtj_voltage_rms_v, env_max_r.mtj_voltage_rms_v),
                mtj_current_min_a=i_min,
                mtj_current_max_a=i_max,
                mtj_current_peak_a=max(abs(i_min), abs(i_max)),
                mtj_current_mean_a=max(
                    env_min_r.mtj_current_mean_a, env_max_r.mtj_current_mean_a, key=abs
                ),
                mtj_current_rms_a=max(env_min_r.mtj_current_rms_a, env_max_r.mtj_current_rms_a),
                mtj_power_peak_w=max(env_min_r.mtj_power_peak_w, env_max_r.mtj_power_peak_w),
                mtj_power_avg_w=max(env_min_r.mtj_power_avg_w, env_max_r.mtj_power_avg_w),
            )
    else:
        set_vals, _oc_vals, voltages, currents = _pass_axis_values(model, load)
        if _is_monotonic(set_vals):
            arr_s = np.asarray(set_vals, dtype=float)
            arr_v = np.asarray(voltages, dtype=float)
            arr_i = np.asarray(currents, dtype=float)
            if arr_s[0] > arr_s[-1]:
                arr_s = arr_s[::-1]
                arr_v = arr_v[::-1]
                arr_i = arr_i[::-1]
            v_samples = np.interp(cycle_set_v, arr_s, arr_v)
            i_samples = np.interp(cycle_set_v, arr_s, arr_i)
        else:
            solved = [
                _solve_curve_pass_point(
                    model, input_key="setting", target_si=float(sv), load=load
                )[0]
                for sv in cycle_set_v
            ]
            v_samples = np.asarray([pt.mtj_voltage_v for pt in solved], dtype=float)
            i_samples = np.asarray([pt.mtj_current_a for pt in solved], dtype=float)
        nominal = _envelope_from_samples(
            v_samples,
            i_samples,
            weights,
            extra_v=(low_dc.point.mtj_voltage_v, high_dc.point.mtj_voltage_v),
            extra_i=(low_dc.point.mtj_current_a, high_dc.point.mtj_current_a),
        )

    exceeds_hw = low_dc.point.exceeds_hardware_oc_limit or high_dc.point.exceeds_hardware_oc_limit
    advisory = _advisory_station_safety_message(
        settings=settings,
        channel=channel,
        waveform=waveform,
        frequency_hz=request.frequency_hz,
        high_v=request.high_v,
        low_v=request.low_v,
        load=load,
    )
    return WaveformSolveResult(
        request=request,
        load=load,
        nominal=nominal,
        resistance_range=resistance_range_env,
        low_point=low_dc.point,
        high_point=high_dc.point,
        exceeds_hardware_oc_limit=exceeds_hw,
        warnings=tuple(warnings),
        advisory_safety_message=advisory,
    )


def curve_transfer_series(
    model: ReferenceCurvePass | ConstantResistanceModel,
    output_load: str | float,
    *,
    setting_range_v: tuple[float, float] | None = None,
    sample_count: int = 201,
) -> TransferSeries:
    """Build plottable V_MTJ(V_set) and I_MTJ(V_set) series in SI units."""
    load = parse_rigol_load(output_load)
    if isinstance(model, ReferenceCurvePass):
        set_vals, oc_vals, voltages, currents = _pass_axis_values(model, load)
        return TransferSeries(
            rigol_setpoint_v=tuple(set_vals),
            open_circuit_v=tuple(oc_vals),
            mtj_voltage_v=tuple(voltages),
            mtj_current_a=tuple(currents),
        )

    if sample_count < 2:
        raise ValueError("sample_count must be at least 2.")
    low_v, high_v = setting_range_v if setting_range_v is not None else (-1.0, 1.0)
    if not (math.isfinite(low_v) and math.isfinite(high_v)) or low_v >= high_v:
        raise ValueError("setting_range_v must be a finite ascending (low, high) pair.")
    grid = np.linspace(low_v, high_v, sample_count)
    oc = grid * load.open_circuit_scale
    i_nom = oc / (model.resistance_ohm + RIGOL_SOURCE_RESISTANCE_OHM)
    v_nom = i_nom * model.resistance_ohm

    band_low_v = band_high_v = band_low_i = band_high_i = None
    if model.has_band and model.minimum_ohm is not None and model.maximum_ohm is not None:
        i_min_r = oc / (model.minimum_ohm + RIGOL_SOURCE_RESISTANCE_OHM)
        v_min_r = i_min_r * model.minimum_ohm
        i_max_r = oc / (model.maximum_ohm + RIGOL_SOURCE_RESISTANCE_OHM)
        v_max_r = i_max_r * model.maximum_ohm
        band_low_v = tuple(np.minimum(v_min_r, v_max_r).tolist())
        band_high_v = tuple(np.maximum(v_min_r, v_max_r).tolist())
        band_low_i = tuple(np.minimum(i_min_r, i_max_r).tolist())
        band_high_i = tuple(np.maximum(i_min_r, i_max_r).tolist())

    return TransferSeries(
        rigol_setpoint_v=tuple(grid.tolist()),
        open_circuit_v=tuple(oc.tolist()),
        mtj_voltage_v=tuple(v_nom.tolist()),
        mtj_current_a=tuple(i_nom.tolist()),
        band_low_voltage_v=band_low_v,
        band_high_voltage_v=band_high_v,
        band_low_current_a=band_low_i,
        band_high_current_a=band_high_i,
    )


def _floor_rigol_voltage(value_v: float) -> float:
    """Quantize a positive Rigol voltage ceiling downward so rounding never exceeds the limit."""
    if value_v <= 0.0:
        return 0.0
    if value_v < RIGOL_DG1000Z_MIN_VOLTAGE_RESOLUTION_V:
        return value_v
    step = rigol_voltage_resolution_v(value_v)
    steps = math.floor(value_v / step + 1e-9)
    return max(0.0, round(steps * step, 12))


def _solve_conservative_curve_voltage_point(
    curve_pass: ReferenceCurvePass,
    target_mtj_v: float,
    load: RigolLoadSpec,
    warnings: list[str],
) -> RigolOperatingPoint:
    try:
        pt, _ = _solve_curve_pass_point(
            curve_pass, input_key="voltage", target_si=target_mtj_v, load=load
        )
    except AmbiguousBranchError as exc:
        warnings.append(
            "Reference curve is non-monotonic at the MTJ voltage limit; "
            "selected the conservative smallest |V_set| branch."
        )
        return min(exc.candidates, key=lambda cand: abs(cand.rigol_setpoint_v))
    return pt


def compute_rigol_voltage_limit_for_mtj(
    model: ReferenceCurvePass | ConstantResistanceModel,
    *,
    mtj_min_v: float,
    mtj_max_v: float,
    output_load: str | float,
) -> RigolVoltageLimitEstimate:
    """Map an allowed MTJ voltage envelope [mtj_min_v, mtj_max_v] to a safe Rigol V_set ceiling.

    For a resistance uncertainty band [R_min, R_max], V_MTJ / V_oc = R / (R + 50 ohm)
    is strictly increasing in R, so the conservative voltage ceiling solves at R_max
    while the worst-case current estimate evaluates at R_min.
    For a measured Keithley pass, bounds are never extrapolated beyond the recorded
    curve; requests exceeding the measured sweep are clamped to verified bounds.
    """
    if not (math.isfinite(mtj_min_v) and math.isfinite(mtj_max_v)):
        raise ValueError("MTJ min and max voltage bounds must be finite.")
    if mtj_min_v >= mtj_max_v:
        raise ValueError("MTJ minimum voltage must be strictly less than maximum voltage.")
    if mtj_min_v > 0.0 or mtj_max_v < 0.0:
        raise ValueError("MTJ safe voltage window must include 0 V (min <= 0 <= max).")

    load = parse_rigol_load(output_load)
    hw_display_ceiling = DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V / load.open_circuit_scale
    warnings: list[str] = []
    clamped_measured = False
    clamped_hw = False
    governing_r: float | None = None

    if isinstance(model, ConstantResistanceModel):
        r_gov = (
            model.maximum_ohm
            if (model.has_band and model.maximum_ohm is not None)
            else model.resistance_ohm
        )
        r_curr = (
            model.minimum_ohm
            if (model.has_band and model.minimum_ohm is not None)
            else model.resistance_ohm
        )
        governing_r = r_gov
        scale_to_set = ((r_gov + RIGOL_SOURCE_RESISTANCE_OHM) / r_gov) / load.open_circuit_scale
        raw_min_v = mtj_min_v * scale_to_set
        raw_max_v = mtj_max_v * scale_to_set

        rigol_min_v = max(raw_min_v, -hw_display_ceiling)
        rigol_max_v = min(raw_max_v, hw_display_ceiling)
        if rigol_min_v > raw_min_v + 1e-12 or rigol_max_v < raw_max_v - 1e-12:
            clamped_hw = True
            warnings.append(
                f"Requested MTJ voltage exceeds DG1032Z ±{DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V:g} V "
                f"open-circuit ceiling; clamped to ±{hw_display_ceiling:.4g} V ({load.raw_label})."
            )
        eff_min_v = rigol_min_v / scale_to_set
        eff_max_v = rigol_max_v / scale_to_set

        if eff_min_v < 0.0 < eff_max_v:
            raw_sym = min(abs(rigol_min_v), abs(rigol_max_v))
        else:
            raw_sym = max(abs(rigol_min_v), abs(rigol_max_v))
            warnings.append(
                "Rigol combined_voltage_limit is symmetric (±limit); both polarities up to ±limit are allowed."
            )
        rigol_sym_v = _floor_rigol_voltage(min(raw_sym, hw_display_ceiling))
        oc_limit_v = rigol_sym_v * load.open_circuit_scale
        est_i_max = oc_limit_v / (r_curr + RIGOL_SOURCE_RESISTANCE_OHM)
    elif isinstance(model, ReferenceCurvePass):
        if len(model.points) < 2:
            raise ValueError("Reference curve pass requires at least 2 valid points.")
        set_vals, _oc_vals, voltages, currents = _pass_axis_values(model, load)
        meas_v_min = min(voltages)
        meas_v_max = max(voltages)
        eff_min_v = max(mtj_min_v, meas_v_min)
        eff_max_v = min(mtj_max_v, meas_v_max)
        if eff_min_v >= eff_max_v:
            raise CalculatorRangeError(
                "Requested MTJ voltage window does not overlap the measured Keithley curve.",
                axis_name="voltage",
                min_value_si=meas_v_min,
                max_value_si=meas_v_max,
            )
        if mtj_min_v < meas_v_min - 1e-12 or mtj_max_v > meas_v_max + 1e-12:
            clamped_measured = True
            warnings.append(
                f"Requested MTJ voltage exceeds measured curve ({meas_v_min * 1e3:.1f} mV … "
                f"{meas_v_max * 1e3:.1f} mV); clamped to verified measured bounds without extrapolation. "
                "Switch to Fixed R for a resistor-band estimate."
            )

        pt_min = _solve_conservative_curve_voltage_point(model, eff_min_v, load, warnings)
        pt_max = _solve_conservative_curve_voltage_point(model, eff_max_v, load, warnings)
        raw_min_v = min(pt_min.rigol_setpoint_v, pt_max.rigol_setpoint_v)
        raw_max_v = max(pt_min.rigol_setpoint_v, pt_max.rigol_setpoint_v)
        rigol_min_v = max(raw_min_v, -hw_display_ceiling)
        rigol_max_v = min(raw_max_v, hw_display_ceiling)
        if rigol_min_v > raw_min_v + 1e-12 or rigol_max_v < raw_max_v - 1e-12:
            clamped_hw = True
            warnings.append(
                f"Calculated Rigol setting exceeds DG1032Z ±{DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V:g} V "
                f"open-circuit ceiling; clamped to ±{hw_display_ceiling:.4g} V ({load.raw_label})."
            )

        if rigol_min_v < 0.0 < rigol_max_v:
            raw_sym = min(abs(rigol_min_v), abs(rigol_max_v))
        else:
            raw_sym = max(abs(rigol_min_v), abs(rigol_max_v))
            warnings.append(
                "Rigol combined_voltage_limit is symmetric (±limit); both polarities up to ±limit are allowed."
            )
        rigol_sym_v = _floor_rigol_voltage(min(raw_sym, hw_display_ceiling))
        oc_limit_v = rigol_sym_v * load.open_circuit_scale
        in_limit_currents = [
            abs(curr)
            for s_val, curr in zip(set_vals, currents, strict=True)
            if abs(s_val) <= rigol_sym_v + 1e-9
        ]
        in_limit_currents.extend((abs(pt_min.mtj_current_a), abs(pt_max.mtj_current_a)))
        est_i_max = max(in_limit_currents, default=0.0)
    else:
        raise TypeError(f"Unsupported load model {type(model).__name__}.")

    return RigolVoltageLimitEstimate(
        load=load,
        requested_mtj_min_v=mtj_min_v,
        requested_mtj_max_v=mtj_max_v,
        effective_mtj_min_v=eff_min_v,
        effective_mtj_max_v=eff_max_v,
        rigol_min_v=rigol_min_v,
        rigol_max_v=rigol_max_v,
        rigol_symmetric_limit_v=rigol_sym_v,
        open_circuit_limit_v=oc_limit_v,
        estimated_current_max_a=est_i_max,
        governing_resistance_ohm=governing_r,
        clamped_to_measured_range=clamped_measured,
        clamped_to_hardware_limit=clamped_hw,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def export_conversion_csv(
    path: Path,
    model: ReferenceCurvePass | ConstantResistanceModel,
    output_load: str | float,
    *,
    curve: ReferenceIVCurve | None = None,
    setting_range_v: tuple[float, float] | None = None,
    waveform_result: WaveformSolveResult | None = None,
    dc_result: DcSolveResult | None = None,
) -> Path:
    """Atomically write an advisory Rigol-to-MTJ conversion table with full provenance."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    load = parse_rigol_load(output_load)
    series = curve_transfer_series(model, load.wire_value, setting_range_v=setting_range_v)
    has_band = series.band_low_voltage_v is not None

    with temporary.open("w", encoding="utf-8", newline="") as stream:
        stream.write("# MTJLAB Rigol DG1032Z -> MTJ Load Calculator conversion table\n")
        stream.write(
            "# Advisory model only: fixed 50 ohm Rigol series source impedance, direct connection.\n"
        )
        stream.write(
            "# Not a current measurement, hardware compliance, or RF/transient calibration.\n"
        )
        stream.write(
            f"# Rigol LOAD convention: {load.raw_label} (open-circuit scale k = {load.open_circuit_scale:.12g})\n"
        )
        if isinstance(model, ConstantResistanceModel):
            stream.write(f"# Load model: constant resistance R_MTJ = {model.resistance_ohm:.12g} ohm\n")
            if model.has_band:
                stream.write(
                    f"# Resistance band: [{model.minimum_ohm:.12g}, {model.maximum_ohm:.12g}] ohm\n"
                )
        else:
            stream.write(f"# Load model: measured Keithley curve ({model.label}, {len(model.points)} points)\n")
        if curve is not None:
            stream.write(f"# Reference title: {curve.title}\n")
            if curve.source_path is not None:
                stream.write(f"# Source path: {curve.source_path}\n")
            if curve.source_sha256:
                stream.write(f"# Source SHA-256: {curve.source_sha256}\n")
            if curve.sample_id or curve.coordinate_label:
                stream.write(
                    f"# Sample / Coordinate: {curve.sample_name or curve.sample_id} {curve.coordinate_label}\n"
                )
            if curve.keithley_b_setpoint_a is not None:
                stream.write(f"# Keithley B setpoint [A]: {curve.keithley_b_setpoint_a:.12g}\n")
            if curve.moke_vout_v is not None:
                stream.write(f"# MOKE Box VOUT [V]: {curve.moke_vout_v:.12g}\n")
            stream.write(f"# Sense mode: {curve.sense_mode}\n")
            if curve.sense_mode == "4wire":
                stream.write(
                    "# Equivalence status: conditional_external_drops_unknown "
                    "(4-wire measurement excludes lead/contact drops in series with Rigol 50 ohm)\n"
                )
        if dc_result is not None:
            pt = dc_result.point
            stream.write(
                f"# Active DC point: V_set={pt.rigol_setpoint_v:.12g} V, "
                f"V_MTJ={pt.mtj_voltage_v:.12g} V, I_MTJ={pt.mtj_current_a:.12g} A\n"
            )
        if waveform_result is not None:
            env = waveform_result.nominal
            stream.write(
                f"# Active Waveform ({waveform_result.request.waveform}): "
                f"V_MTJ=[{env.mtj_voltage_min_v:.12g}, {env.mtj_voltage_max_v:.12g}] V, "
                f"I_MTJ_peak={env.mtj_current_peak_a:.12g} A, I_MTJ_rms={env.mtj_current_rms_a:.12g} A\n"
            )

        headers = [
            "rigol_setpoint_v",
            "open_circuit_v",
            "mtj_voltage_v",
            "mtj_current_a",
            "mtj_power_w",
            "mtj_static_resistance_ohm",
            "reflection_gamma",
        ]
        if has_band:
            headers.extend(
                [
                    "mtj_voltage_min_v",
                    "mtj_voltage_max_v",
                    "mtj_current_min_a",
                    "mtj_current_max_a",
                ]
            )
        writer = csv.writer(stream)
        writer.writerow(headers)
        for idx in range(len(series.rigol_setpoint_v)):
            v_set = series.rigol_setpoint_v[idx]
            v_oc = series.open_circuit_v[idx]
            v_mtj = series.mtj_voltage_v[idx]
            i_mtj = series.mtj_current_a[idx]
            p_mtj = v_mtj * i_mtj
            r_static = v_mtj / i_mtj if abs(i_mtj) > 1e-12 else math.nan
            gamma = (
                (r_static - RIGOL_SOURCE_RESISTANCE_OHM) / (r_static + RIGOL_SOURCE_RESISTANCE_OHM)
                if math.isfinite(r_static) and r_static > 0.0
                else math.nan
            )
            row = [
                f"{v_set:.12g}",
                f"{v_oc:.12g}",
                f"{v_mtj:.12g}",
                f"{i_mtj:.12g}",
                f"{p_mtj:.12g}",
                f"{r_static:.12g}" if math.isfinite(r_static) else "",
                f"{gamma:.12g}" if math.isfinite(gamma) else "",
            ]
            if (
                has_band
                and series.band_low_voltage_v is not None
                and series.band_high_voltage_v is not None
                and series.band_low_current_a is not None
                and series.band_high_current_a is not None
            ):
                row.extend(
                    [
                        f"{series.band_low_voltage_v[idx]:.12g}",
                        f"{series.band_high_voltage_v[idx]:.12g}",
                        f"{series.band_low_current_a[idx]:.12g}",
                        f"{series.band_high_current_a[idx]:.12g}",
                    ]
                )
            writer.writerow(row)
        stream.flush()

    temporary.replace(target)
    return target
