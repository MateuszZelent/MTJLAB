"""Common-current-window comparisons using measured I/V, without extrapolation."""

from dataclasses import dataclass, replace
import math

import numpy as np


@dataclass(frozen=True, slots=True)
class FieldFit:
    index: int
    field_current_a: float
    history_segment: int | None
    status: str
    point_indices: tuple[int, ...] = ()
    resistance_ohm: float | None = None
    voltage_offset_v: float | None = None
    resistance_standard_error_ohm: float | None = None
    r_squared: float | None = None
    relative_resistance_percent: float | None = None
    measured_current_min_a: float | None = None
    measured_current_max_a: float | None = None
    reason: str = ""
    bias_coverage: str = "not_determined"
    apparent_ra_ohm_um2: float | None = None
    residuals_v: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldAnalysis:
    current_window_a: tuple[float, float]
    reference_index: int | None
    fits: tuple[FieldFit, ...]
    reference_error: str = ""
    model: str = "V = R_fit * I_measured + V_offset; common current window; OLS v1"


def _fit(curve, window):
    result = FieldFit(curve.index, curve.current_a, curve.history_segment, curve.status)
    if curve.dataset is None or curve.status in {"skipped_field_compliance", "not_started"}:
        return replace(result, reason="no_qualified_field_curve")
    valid = [p for p in curve.dataset.points if p.valid and not p.compliance_active
             and math.isfinite(p.measured_current_a) and math.isfinite(p.measured_voltage_v)]
    if not valid:
        return replace(result, reason="no_valid_points_before_compliance")
    low, high = window
    # Tolerance handles binary representation only, not missing physical data.
    tolerance = 32 * np.finfo(float).eps * max(abs(low), abs(high), np.finfo(float).tiny)
    selected = [p for p in valid if low - tolerance <= p.measured_current_a <= high + tolerance]
    result = replace(result, point_indices=tuple(p.index for p in selected))
    if min(p.measured_current_a for p in valid) > low + tolerance or max(p.measured_current_a for p in valid) < high - tolerance:
        return replace(result, reason="requested_window_not_covered")
    if len(selected) < 3 or len({p.measured_current_a for p in selected}) < 3:
        return replace(result, reason="fewer_than_three_distinct_currents")
    if any(right.index != left.index + 1 for left, right in zip(selected, selected[1:])):
        return replace(result, reason="gap_or_disjoint_branch_in_fit_window")
    demanded_steps = np.diff([p.demanded_si for p in selected])
    if not (np.all(demanded_steps > 0) or np.all(demanded_steps < 0)):
        return replace(result, reason="multiple_sweep_branches_in_fit_window")
    current = np.array([p.measured_current_a for p in selected], dtype=float)
    voltage = np.array([p.measured_voltage_v for p in selected], dtype=float)
    center = float(np.mean(current))
    scale = float(np.max(np.abs(current - center)))
    if not math.isfinite(scale) or scale <= 0:
        return replace(result, reason="ill_conditioned_current_span")
    normalized = (current - center) / scale
    coefficients, _, rank, _ = np.linalg.lstsq(np.column_stack((normalized, np.ones(len(current)))), voltage, rcond=None)
    slope = float(coefficients[0] / scale)
    offset = float(coefficients[1] - slope * center)
    residual = voltage - (coefficients[0] * normalized + coefficients[1])
    rss = float(residual @ residual)
    total = float(np.sum((voltage - np.mean(voltage)) ** 2))
    error = math.sqrt(rss / (len(current) - 2) / float(normalized @ normalized)) / scale
    if rank != 2 or slope <= 0 or not all(math.isfinite(v) for v in (slope, offset, error)):
        return replace(result, reason="nonpositive_or_invalid_fit")
    area = curve.dataset.config.metadata.junction_area_um2
    ra = slope * area if area is not None and math.isfinite(area) and area > 0 else None
    coverage = "bipolar" if np.min(current) < 0 < np.max(current) else (
        "positive_only" if np.min(current) >= 0 else "negative_only")
    return replace(result, resistance_ohm=slope, voltage_offset_v=offset,
                   bias_coverage=coverage, apparent_ra_ohm_um2=ra,
                   residuals_v=tuple(float(value) for value in residual),
                   resistance_standard_error_ohm=error, r_squared=1 - rss / total if total > 0 else None,
                   measured_current_min_a=float(np.min(current)), measured_current_max_a=float(np.max(current)))


def analyze_field_series(series, current_window_a, *, reference_index=None) -> FieldAnalysis:
    """R_fit is a slope estimate in the selected window, not proof of zero-bias R.

    Relative resistance is labelled against an explicit list item, never as TMR.
    Histories and repeated field currents remain separate results.
    """
    low, high = current_window_a
    if not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in (low, high)) or low >= high:
        raise ValueError("Analysis requires a finite, ordered current window in amperes.")
    if reference_index is not None and (type(reference_index) is not int or not 0 <= reference_index < len(series.curves)):
        raise ValueError("Reference must identify an existing field-list item.")
    fits = tuple(_fit(curve, (low, high)) for curve in series.curves)
    reference_error = ""
    if reference_index is not None:
        reference = fits[reference_index].resistance_ohm
        if reference is None:
            reference_error = "Selected reference has no qualified fit in the common window."
        else:
            updated = []
            for fit in fits:
                relative = 100 * (fit.resistance_ohm / reference - 1) if fit.resistance_ohm is not None else None
                if relative is not None and not math.isfinite(relative):
                    relative = None
                updated.append(replace(fit, relative_resistance_percent=relative))
            fits = tuple(updated)
    return FieldAnalysis((float(low), float(high)), reference_index, fits, reference_error)
