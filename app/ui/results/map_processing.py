"""Derived analysis of one frequency × sweep-coordinate plane, never file data.

Common-mode removal is a differential measurement, not a claim that every
stationary spectral feature is noise. No smoothing is done along the sweep axis.
"""

from dataclasses import dataclass
import math
import warnings

import numpy as np


@dataclass(frozen=True, slots=True)
class MapProcessing:
    component: str = "none"
    quantile: float = 0.2
    reference_value: float | None = None
    mask_lines: bool = False
    line_contrast_db: float = 8.0
    line_mad_db: float = 0.5
    line_deviation_db: float = 1.5
    line_stability_fraction: float = 0.95
    line_max_width_hz: float = 6e6
    line_neighbourhood_hz: float = 60e6
    minimum_coverage: float = 0.8
    protected_bands_hz: tuple[tuple[float, float], ...] = ()
    view: str = "result"
    colour_range: str = "full"

    def __post_init__(self):
        if self.component not in {"none", "median_power", "quantile_power", "reference_power", "median_db"}:
            raise ValueError("Unknown map component operation.")
        if self.view not in {"result", "input", "component"} or self.colour_range not in {"full", "robust", "symmetric"}:
            raise ValueError("Unknown map view or colour range.")
        if not math.isfinite(self.quantile) or not 0 < self.quantile < 0.5:
            raise ValueError("Lower quantile must be between 0 and 0.5.")
        if not math.isfinite(self.minimum_coverage) or not 0.5 <= self.minimum_coverage <= 1:
            raise ValueError("Coverage must be between 50% and 100%.")
        if not math.isfinite(self.line_stability_fraction) or not 0.5 <= self.line_stability_fraction <= 1:
            raise ValueError("Stationarity fraction must be between 50% and 100%.")
        for value in (self.line_contrast_db, self.line_mad_db, self.line_deviation_db, self.line_max_width_hz, self.line_neighbourhood_hz):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Line thresholds and frequency widths must be positive and finite.")
        if self.line_neighbourhood_hz <= 2 * self.line_max_width_hz:
            raise ValueError("The line neighbourhood must exceed twice the maximum line width.")
        if self.reference_value is not None and not math.isfinite(self.reference_value):
            raise ValueError("Reference coordinate must be finite.")
        if len(self.protected_bands_hz) > 32 or any(
            not (math.isfinite(low) and math.isfinite(high) and 0 <= low < high)
            for low, high in self.protected_bands_hz
        ):
            raise ValueError("Use at most 32 finite, ordered protected frequency bands.")

    @property
    def active(self):
        return self.component != "none" or self.mask_lines or self.colour_range != "full" or self.view != "result"


@dataclass(frozen=True, slots=True)
class MapAnalysis:
    values: np.ndarray
    unit: str
    component: np.ndarray | None
    component_unit: str
    masked_frequencies_hz: tuple[float, ...]
    notes: tuple[str, ...]
    levels: tuple[float, float]


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("Map analysis cancelled.")


def _column_stat(values, quantile, minimum, cancelled):
    """Bound temporary arrays and cancellation latency for large recorded maps."""
    output = np.full(values.shape[1], np.nan)
    for start in range(0, values.shape[1], 256):
        _cancel(cancelled)
        block = values[:, start:start + 256]
        counts = np.count_nonzero(np.isfinite(block), axis=0)
        if np.all(counts == block.shape[0]):
            statistic = np.quantile(block, quantile, axis=0)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # entirely missing columns
                statistic = np.nanquantile(block, quantile, axis=0)
        statistic[counts < minimum] = np.nan
        output[start:start + 256] = statistic
    return output


def _stationary_lines(values_dbm, frequencies, state, minimum, cancelled):
    centre = _column_stat(values_dbm, 0.5, minimum, cancelled)
    # MAD is used directly in dB; no assumption of Gaussian sweep fluctuations.
    mad = _column_stat(np.abs(values_dbm - centre), 0.5, minimum, cancelled)
    # Physical frequency windows also work on non-uniform analyzer grids. Missing
    # columns are not filled or interpolated into the measured map.
    floor = np.full(centre.shape, np.nan)
    left = np.searchsorted(frequencies, frequencies - state.line_neighbourhood_hz / 2)
    right = np.searchsorted(frequencies, frequencies + state.line_neighbourhood_hz / 2, side="right")
    for index, (lo, hi) in enumerate(zip(left, right, strict=True)):
        if index % 256 == 0:
            _cancel(cancelled)
        neighbours = centre[lo:hi]
        neighbours = neighbours[np.isfinite(neighbours)]
        if neighbours.size >= 5:
            floor[index] = np.median(neighbours)
    elevated = np.isfinite(centre) & ((centre - floor) >= state.line_contrast_db)
    stable = mad <= state.line_mad_db
    finite_count = np.count_nonzero(np.isfinite(values_dbm), axis=0)
    close_count = np.count_nonzero(np.abs(values_dbm - centre) <= state.line_deviation_db, axis=0)
    stability = np.divide(close_count, finite_count, out=np.zeros(centre.shape), where=finite_count > 0)
    stable &= stability >= state.line_stability_fraction
    mask = np.zeros(centre.shape, dtype=bool)
    # Classify the whole elevated lobe's physical width before testing stability.
    # A broad stationary resonance must not become a collection of narrow masks.
    changes = np.diff(np.r_[False, elevated, False].astype(int))
    edges = np.empty(frequencies.size + 1)
    edges[1:-1] = (frequencies[:-1] + frequencies[1:]) / 2
    edges[0] = frequencies[0] - (frequencies[1] - frequencies[0]) / 2
    edges[-1] = frequencies[-1] + (frequencies[-1] - frequencies[-2]) / 2
    for lo, hi in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1), strict=True):
        if edges[hi] - edges[lo] <= state.line_max_width_hz and np.all(stable[lo:hi]):
            mask[lo:hi] = True
    for low, high in state.protected_bands_hz:
        mask[(frequencies >= low) & (frequencies <= high)] = False
    return mask


def analyse_map(values, *, unit, frequencies_hz, coordinate_values, frequency_axis,
                state=MapProcessing(), line_input_dbm=None, cancelled=None):
    """Operate on an already selected slice; transpose support is explicit.

    Subtracting additive components uses W, while median_db deliberately returns
    a relative dB ratio. Missing/under-supported bins stay gaps, never zeros.
    """
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or frequency_axis not in {0, 1}:
        raise ValueError("Map analysis requires Frequency on exactly one map axis.")
    f = np.asarray(frequencies_hz, dtype=float)
    coordinate = np.asarray(coordinate_values, dtype=float)
    rows = matrix if frequency_axis == 1 else matrix.T
    if f.ndim != 1 or coordinate.ndim != 1 or rows.shape != (coordinate.size, f.size):
        raise ValueError("Map axes do not match the recorded matrix.")
    if not np.all(np.isfinite(f)) or f.size < 2 or not np.all(np.diff(f) > 0):
        raise ValueError("Map analysis requires an increasing finite frequency grid.")
    if not np.all(np.isfinite(coordinate)) or coordinate.size == 0 or len(np.unique(coordinate)) != coordinate.size:
        raise ValueError("Sweep coordinates must be finite and distinct within the selected plane.")
    if np.any(np.isinf(rows)):
        rows = np.where(np.isfinite(rows), rows, np.nan)
    _cancel(cancelled)
    notes = []
    finite_rows = int(np.count_nonzero(np.any(np.isfinite(rows), axis=1)))
    minimum = max(3, math.ceil(rows.shape[0] * state.minimum_coverage))
    if (state.component in {"median_power", "quantile_power", "median_db"} or state.mask_lines) and finite_rows < 3:
        raise ValueError("Common-component/line analysis requires at least three readable parameter values.")
    component = None
    component_unit = unit
    output = rows.copy() if state.component == "median_db" or state.mask_lines else rows
    output_unit = unit
    if state.component != "none":
        if state.component == "median_db":
            if unit != "dBm":
                raise ValueError("Relative dB contrast requires absolute raw dBm power.")
            component = _column_stat(rows, 0.5, minimum, cancelled)
            output_unit = "dB"
        else:
            if unit not in {"dBm", "W"}:
                raise ValueError("Additive map subtraction requires power in dBm or W.")
            with np.errstate(over="ignore", invalid="ignore"):
                output = np.power(10., (rows - 30.) / 10.) if unit == "dBm" else rows.copy()
            output[~np.isfinite(output)] = np.nan
            output_unit = component_unit = "W"
            if state.component == "reference_power":
                index = 0 if state.reference_value is None else np.flatnonzero(
                    np.isclose(coordinate, state.reference_value, rtol=1e-9, atol=1e-15))
                if not np.isscalar(index):
                    if len(index) != 1:
                        raise ValueError("The selected reference coordinate is not present in this slice.")
                    index = int(index[0])
                component = output[index].copy()
                notes.append(f"Reference coordinate: {coordinate[index]:.12g} (persisted axis unit).")
            else:
                q = state.quantile if state.component == "quantile_power" else 0.5
                component = _column_stat(output, q, minimum, cancelled)
        output -= component
        notes.append("Differential map: stationary physical signal can also be removed; compare Input and Common component.")
        unsupported = int(np.count_nonzero(~np.isfinite(component)))
        if unsupported:
            notes.append(f"{unsupported} frequency bins lack a readable baseline / required coverage and remain gaps.")
        if state.component == "quantile_power":
            notes.append("Lower quantile is an empirical baseline, not an unbiased noise-power estimate.")
    mask = np.zeros(f.size, dtype=bool)
    if state.mask_lines:
        if line_input_dbm is None and unit != "dBm":
            raise ValueError("Stationary-line classification requires input absolute dBm; disable it for signed residual/ratio maps.")
        line_source = rows
        if line_input_dbm is not None:
            raw = np.asarray(line_input_dbm, dtype=float)
            if raw.shape != matrix.shape:
                raise ValueError("Raw line-classification grid differs from the selected map plane.")
            line_source = raw if frequency_axis == 1 else raw.T
            if np.any(np.isinf(line_source)):
                line_source = np.where(np.isfinite(line_source), line_source, np.nan)
        mask = _stationary_lines(line_source, f, state, minimum, cancelled)
        output[:, mask] = np.nan
        notes.append(f"{np.count_nonzero(mask)} stationary narrow frequency bins classified on recorded raw dBm and masked as gaps; no interpolation.")
    if state.view == "input":
        output, output_unit = rows, unit
    elif state.view == "component":
        if component is None:
            raise ValueError("Select a common-component operation to inspect its baseline.")
        output = np.broadcast_to(component, rows.shape).copy()
        output[~np.isfinite(rows)] = np.nan
        output_unit = component_unit
    _cancel(cancelled)
    finite = output[np.isfinite(output)]
    if not finite.size:
        raise ValueError("No finite map samples remain; relax the filter or inspect Input.")
    low, high = float(np.min(finite)), float(np.max(finite))
    if state.colour_range == "robust":
        low, high = map(float, np.quantile(finite, (0.01, 0.99)))
        notes.append("Colour range: 1–99 percentiles; export retains every value.")
    elif state.colour_range == "symmetric":
        bound = max(abs(low), abs(high))
        low, high = -bound, bound
    return MapAnalysis(output if frequency_axis == 1 else output.T, output_unit, component,
                       component_unit, tuple(f[mask]), tuple(notes), (low, high))
