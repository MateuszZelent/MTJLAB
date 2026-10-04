"""Read-only spectrum cleanup, peak measurement, and fitting.

Every function returns derived arrays and leaves the acquired trace untouched.
Frequency values remain in Hz.  The legacy dBm names are retained for API
compatibility, while ``clean_spectrum_values`` and the ``unit`` fields keep
relative/linear display traces explicitly labelled instead of relabelling them
as dBm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Sequence

import numpy as np

from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from .processing import frequency_grids_match
from .streaming_statistics import dbm_to_w


@dataclass(frozen=True, slots=True)
class SpectrumAnalysisParameters:
    denoise_window: int = 9
    emi_threshold_db: float = 10.0
    emi_max_std_db: float = 0.75
    emi_min_frames: int = 5
    peak_min_snr_db: float = 6.0
    peak_min_prominence_db: float = 3.0
    peak_max_count: int = 20
    peak_fit_models: bool = True
    narrow_max_width_hz: float = 6e6
    narrow_threshold_sigma: float = 6.0
    narrow_protected_regions_hz: tuple[tuple[float, float], ...] = ()
    peak_min_width_hz: float = 0.0
    peak_max_width_hz: float = 0.0
    peak_min_distance_hz: float = 0.0
    peak_min_snr_sigma: float = 6.0
    peak_min_prominence_sigma: float = 3.0
    peak_polarity: str = "positive"
    temporal_average_frames: int = 1
    temporal_max_gap_s: float = 30.0
    peak_measure_filtered: bool = False

    def __post_init__(self) -> None:
        if type(self.temporal_average_frames) is not int or not 1 <= self.temporal_average_frames <= 64:
            raise ValueError("Power averaging requires 1 to 64 received frames.")
        if not math.isfinite(self.temporal_max_gap_s) or self.temporal_max_gap_s <= 0:
            raise ValueError("Averaging reset gap must be a positive finite time.")
        if len(self.narrow_protected_regions_hz) > 32 or any(
            not (math.isfinite(low) and math.isfinite(high) and 0 <= low < high)
            for low, high in self.narrow_protected_regions_hz
        ):
            raise ValueError("Provide at most 32 ordered, finite protected frequency bands.")
        widths = (self.peak_min_width_hz, self.peak_max_width_hz, self.peak_min_distance_hz)
        if any(not math.isfinite(value) or value < 0 for value in widths):
            raise ValueError("Peak widths and separation must be finite nonnegative frequencies.")
        if self.peak_max_width_hz and self.peak_max_width_hz < self.peak_min_width_hz:
            raise ValueError("Maximum peak width must be at least the minimum width.")
        if self.peak_polarity not in {"positive", "negative", "both"}:
            raise ValueError("Unsupported peak polarity.")
        if any(not math.isfinite(value) or value <= 0
               for value in (self.peak_min_snr_sigma, self.peak_min_prominence_sigma)):
            raise ValueError("Linear peak thresholds must be positive finite noise multiples.")


@dataclass(frozen=True, slots=True)
class SpectrumPeak:
    index: int
    frequency_hz: float
    amplitude_dbm: float
    noise_floor_dbm: float
    snr_db: float
    prominence_db: float
    left_half_power_hz: float | None
    right_half_power_hz: float | None
    fwhm_hz: float | None
    q_factor: float | None
    fit_model: str
    fit_center_hz: float | None
    fit_fwhm_hz: float | None
    fit_rmse_db: float | None
    amplitude_unit: str = "dBm"
    contrast_unit: str = "dB"


@dataclass(frozen=True, slots=True)
class SpectrumCleanupResult:
    values_dbm: tuple[float, ...]
    noise_sigma_db: float
    stationary_interference_indices: tuple[int, ...]
    method: str
    unit: str = "dBm"
    modified_bin_indices: tuple[int, ...] = ()
    removed_peak_indices: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()
    input_values: tuple[float, ...] | None = None
    input_provenance: tuple[str, ...] = ()
    applied_modes: tuple[str, ...] = ()

    @property
    def values(self) -> tuple[float, ...]:
        """Unit-neutral alias used by the display/analysis pipeline."""

        return self.values_dbm


def _finite_vectors(
    frequencies_hz: Sequence[float], values_dbm: Sequence[float]
) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.asarray(frequencies_hz, dtype=float)
    values = np.asarray(values_dbm, dtype=float)
    if frequencies.ndim != 1 or values.ndim != 1 or frequencies.size != values.size:
        raise ValueError("Frequency and amplitude arrays must be equally-sized one-dimensional vectors.")
    if frequencies.size < 5:
        raise ValueError("Spectrum analysis requires at least five points.")
    if not np.all(np.isfinite(frequencies)) or not np.all(np.isfinite(values)):
        raise ValueError("Spectrum analysis requires finite frequency and amplitude values.")
    differences = np.diff(frequencies)
    if not (np.all(differences > 0) or np.all(differences < 0)):
        raise ValueError("Spectrum frequencies must be strictly monotonic.")
    if differences[0] < 0:
        frequencies = frequencies[::-1].copy()
        values = values[::-1].copy()
    return frequencies, values


def robust_noise_sigma_db(values_dbm: Sequence[float]) -> float:
    """Estimate point noise with a MAD of adjacent-bin differences."""

    values = np.asarray(values_dbm, dtype=float)
    if values.ndim != 1 or values.size < 3 or not np.all(np.isfinite(values)):
        raise ValueError("Noise estimation requires at least three finite dBm values.")
    differences = np.diff(values)
    median = float(np.median(differences))
    mad = float(np.median(np.abs(differences - median)))
    # Adjacent differences contain two independent noise contributions.
    return max(1.4826 * mad / math.sqrt(2.0), 1e-6)


def _odd_window(point_count: int, preferred: int) -> int:
    window = max(3, min(preferred, point_count if point_count % 2 else point_count - 1))
    return window if window % 2 else window - 1


def _linear_noise_sigma(values: Sequence[float]) -> float:
    """Noise in source units, without logarithmic-unit numerical floors."""
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or array.size < 3 or not np.all(np.isfinite(array)):
        raise ValueError("Noise estimation requires at least three finite values.")
    scale = max(float(np.max(np.abs(array))), np.finfo(float).tiny)
    normalized = array / scale
    differences = np.diff(normalized)
    mad = float(np.median(np.abs(differences - np.median(differences))))
    return max(1.4826 * mad / math.sqrt(2.0), 64 * np.finfo(float).eps) * scale


def bilateral_denoise_linear(values: Sequence[float], *, window: int = 9) -> tuple[float, ...]:
    """Scale-invariant bilateral smoothing of signed linear power."""
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or array.size < 5 or not np.all(np.isfinite(array)):
        raise ValueError("Denoising requires at least five finite linear values.")
    scale = max(float(np.max(np.abs(array))), np.finfo(float).tiny)
    normalized = array / scale
    width = _odd_window(array.size, int(window))
    radius = width // 2
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(normalized, radius, mode="edge"), width)
    spatial = np.arange(-radius, radius + 1, dtype=float)
    spatial_weights = np.exp(-0.5 * (spatial / max(radius / 1.8, 1.0)) ** 2)
    sigma = max(2.5 * _linear_noise_sigma(normalized), 64 * np.finfo(float).eps)
    weights = np.exp(-0.5 * ((frames - normalized[:, None]) / sigma) ** 2) * spatial_weights
    filtered = np.sum(weights * frames, axis=1) / np.sum(weights, axis=1)
    return tuple(float(value) for value in filtered * scale)


def rolling_noise_floor_dbm(values_dbm: Sequence[float], *, window: int = 51) -> np.ndarray:
    values = np.asarray(values_dbm, dtype=float)
    if values.ndim != 1 or values.size < 5 or not np.all(np.isfinite(values)):
        raise ValueError("Noise-floor estimation requires at least five finite dBm values.")
    width = _odd_window(values.size, window)
    radius = width // 2
    padded = np.pad(values, radius, mode="edge")
    frames = np.lib.stride_tricks.sliding_window_view(padded, width)
    return np.percentile(frames, 30.0, axis=1)


def bilateral_denoise_dbm(
    values_dbm: Sequence[float], *, window: int = 9
) -> tuple[float, ...]:
    """Edge-preserving smoothing in dB; strong narrow peaks retain their height."""

    values = np.asarray(values_dbm, dtype=float)
    if values.ndim != 1 or values.size < 5 or not np.all(np.isfinite(values)):
        raise ValueError("Denoising requires at least five finite dBm values.")
    width = _odd_window(values.size, int(window))
    radius = width // 2
    padded = np.pad(values, radius, mode="edge")
    frames = np.lib.stride_tricks.sliding_window_view(padded, width)
    spatial_positions = np.arange(-radius, radius + 1, dtype=float)
    spatial_sigma = max(radius / 1.8, 1.0)
    spatial_weights = np.exp(-0.5 * (spatial_positions / spatial_sigma) ** 2)
    noise_sigma = robust_noise_sigma_db(values)
    range_sigma = max(2.5 * noise_sigma, 0.35)
    range_weights = np.exp(-0.5 * ((frames - values[:, None]) / range_sigma) ** 2)
    weights = range_weights * spatial_weights[None, :]
    filtered = np.sum(weights * frames, axis=1) / np.maximum(np.sum(weights, axis=1), 1e-15)
    return tuple(float(value) for value in filtered)


def _gaussian_detection_trace(values_dbm: np.ndarray) -> np.ndarray:
    """Suppress bin-to-bin maxima for detection without becoming display data."""

    radius = min(6, max(2, values_dbm.size // 500))
    positions = np.arange(-radius, radius + 1, dtype=float)
    sigma = max(radius / 2.0, 1.0)
    kernel = np.exp(-0.5 * (positions / sigma) ** 2)
    kernel /= np.sum(kernel)
    padded = np.pad(values_dbm, radius, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def detect_stationary_interference(
    history_dbm: Sequence[Sequence[float]],
    *,
    min_frames: int = 5,
    threshold_db: float = 10.0,
    max_std_db: float = 0.75,
) -> tuple[int, ...]:
    """Return conservative stationary-line candidates from temporal history.

    A stable desired carrier is mathematically indistinguishable from EMI here;
    callers must label these bins as candidates and preserve Raw data.
    """

    history = np.asarray(history_dbm, dtype=float)
    if history.ndim != 2 or history.shape[0] < min_frames or history.shape[1] < 5:
        return ()
    if not np.all(np.isfinite(history)):
        raise ValueError("EMI candidate detection requires finite history values.")
    median_trace = np.median(history, axis=0)
    temporal_std = np.std(history, axis=0)
    local_floor = rolling_noise_floor_dbm(median_trace, window=51)
    elevated = median_trace - local_floor >= float(threshold_db)
    stable = temporal_std <= float(max_std_db)
    local_maximum = np.r_[False, (median_trace[1:-1] >= median_trace[:-2]) & (median_trace[1:-1] >= median_trace[2:]), False]
    centers = np.flatnonzero(elevated & stable & local_maximum)
    flagged: set[int] = set()
    for center in centers:
        flagged.update(range(max(0, center - 1), min(history.shape[1], center + 2)))
    return tuple(sorted(flagged))


def suppress_stationary_lines_dbm(
    values_dbm: Sequence[float], indices: Sequence[int]
) -> tuple[float, ...]:
    values = np.asarray(values_dbm, dtype=float).copy()
    if values.ndim != 1 or not np.all(np.isfinite(values)):
        raise ValueError("Stationary-line suppression requires finite dBm values.")
    mask = np.zeros(values.size, dtype=bool)
    valid = [int(index) for index in indices if 0 <= int(index) < values.size]
    mask[valid] = True
    anchors = np.flatnonzero(~mask)
    if anchors.size < 2:
        return tuple(float(value) for value in values)
    values[mask] = np.interp(np.flatnonzero(mask), anchors, values[anchors])
    return tuple(float(value) for value in values)


def clean_spectrum_dbm(
    values_dbm: Sequence[float],
    *,
    mode: str,
    history_dbm: Sequence[Sequence[float]] = (),
    parameters: SpectrumAnalysisParameters | None = None,
    frequencies_hz: Sequence[float] | None = None,
) -> SpectrumCleanupResult:
    return clean_spectrum_values(
        values_dbm,
        unit="dBm",
        mode=mode,
        history_dbm=history_dbm,
        parameters=parameters,
        frequencies_hz=frequencies_hz,
    )


def clean_spectrum_values(
    values: Sequence[float],
    *,
    unit: str,
    mode: str,
    history_dbm: Sequence[Sequence[float]] = (),
    parameters: SpectrumAnalysisParameters | None = None,
    frequencies_hz: Sequence[float] | None = None,
) -> SpectrumCleanupResult:
    """Clean the numeric values of any displayed spectrum unit.

    The algorithms are value-domain operations and do not change units.  The
    legacy ``clean_spectrum_dbm`` entry point remains the dBm-specialized API.
    """

    params = parameters or SpectrumAnalysisParameters()
    values = tuple(float(value) for value in values)
    if mode.lower() == "narrow_reject":
        from .narrow_spikes import filter_narrow_spikes

        if frequencies_hz is None:
            raise ValueError("Narrow-peak filtering needs a frequency axis in Hz.")
        result = filter_narrow_spikes(frequencies_hz, values,
            maximum_width_hz=params.narrow_max_width_hz,
            threshold_sigma=params.narrow_threshold_sigma,
            protected_regions_hz=params.narrow_protected_regions_hz)
        return SpectrumCleanupResult(result.values, result.noise_scale, (),
            "Narrow-peak rejection (display only)", unit, result.modified_indices,
            result.peak_indices, result.notes)
    linear = unit not in {"dBm", "dB"}
    sigma = _linear_noise_sigma(values) if linear else robust_noise_sigma_db(values)
    mode = mode.lower()
    if mode == "raw":
        return SpectrumCleanupResult(values, sigma, (), "Raw (no processing)", unit)
    interference = (
        detect_stationary_interference(
            history_dbm,
            min_frames=params.emi_min_frames,
            threshold_db=params.emi_threshold_db,
            max_std_db=params.emi_max_std_db,
        )
        if mode in {"emi_reject", "auto_clean"}
        else ()
    )
    if mode == "denoise":
        cleaned = (bilateral_denoise_linear(values, window=params.denoise_window)
                   if linear else bilateral_denoise_dbm(values, window=params.denoise_window))
        method = "Edge-preserving bilateral denoise"
    elif mode == "emi_reject":
        cleaned = suppress_stationary_lines_dbm(values, interference)
        method = "Conservative stationary-line rejection (display only)"
    elif mode == "auto_clean":
        denoised = bilateral_denoise_dbm(values, window=params.denoise_window)
        cleaned = suppress_stationary_lines_dbm(denoised, interference)
        method = "Bilateral denoise + stationary-line rejection (display only)"
    else:
        raise ValueError(f"Unsupported spectrum cleanup mode: {mode!r}.")
    notes: tuple[str, ...] = ()
    if mode in {"emi_reject", "auto_clean"}:
        if len(history_dbm) < params.emi_min_frames:
            notes = (
                f"Stationary-line rejection needs history: {len(history_dbm)}/{params.emi_min_frames} frames.",
            )
        elif not interference:
            notes = ("No stationary-line candidates found in the current history.",)
    return SpectrumCleanupResult(tuple(cleaned), sigma, interference, method, unit, notes=notes)


SPECTRUM_FILTER_ORDER = ("background", "narrow_reject", "emi_reject", "denoise")
SPECTRUM_FILTER_LABELS = {
    "background": "Background subtraction",
    "narrow_reject": "Narrow-peak rejection",
    "emi_reject": "Stationary-line rejection",
    "denoise": "Edge-preserving denoise",
}


def clean_spectrum_pipeline(
    values: Sequence[float],
    *,
    unit: str,
    modes: Sequence[str],
    history: Sequence[Sequence[float]] = (),
    parameters: SpectrumAnalysisParameters | None = None,
    frequencies_hz: Sequence[float] | None = None,
    background_profile: BackgroundProfile | None = None,
    background_context: SpectrumAcquisitionContext | None = None,
    background_model=None,
) -> SpectrumCleanupResult:
    """Compose display filters in a stable order, preserving units and raw input.

    Reject narrow extrema before smoothing can widen them. Classify stationary
    lines on the source history, then denoise the resulting trace. A filter
    unavailable for this grid is reported explicitly; subsequent filters still
    receive the last valid intermediate result. Invalid source data is rejected
    before any stage runs.
    """
    selected = set(modes)
    unknown = selected.difference(SPECTRUM_FILTER_ORDER)
    if unknown:
        raise ValueError(f"Unsupported spectrum filters: {sorted(unknown)!r}.")
    original = tuple(float(value) for value in values)
    methods: list[str] = []
    applied_modes: list[str] = []
    if "background" in selected:
        if unit != "dBm":
            raise ValueError("Background subtraction requires an absolute dBm input; the residual is signed W.")
        if background_profile is None or background_context is None:
            raise ValueError("Record or load a background profile before enabling Background.")
        if background_profile.context_id != background_context.context_id:
            raise ValueError("Background profile does not match its acquisition context.")
        if not background_context.settings_verified:
            raise ValueError("Background analyzer settings have not been verified.")
        if frequencies_hz is None or not frequency_grids_match(frequencies_hz, background_context.frequencies_hz):
            raise ValueError("Background frequency grid differs from the current spectrum.")
        if len(original) != len(background_profile.mean_w):
            raise ValueError("Background point count differs from the current spectrum.")
        if background_model is not None and (
            background_model.context_id != background_context.context_id
            or not np.array_equal(background_model.baseline_w, background_profile.mean_w)
        ):
            raise ValueError("Background model does not match the active background context and baseline.")
        power = dbm_to_w(original)
        background = background_profile.mean_w if background_model is None else background_model.fit_preview(power).background_w
        original = tuple(power - background)
        unit = "W"
        methods.append(f"Background subtraction [{background_profile.profile_id}] (signed W, display only)")
        applied_modes.append("background")
    result = clean_spectrum_values(original, unit=unit, mode="raw", parameters=parameters)
    notes: list[str] = []
    protected = np.zeros(len(original), dtype=bool)
    regions = (parameters or SpectrumAnalysisParameters()).narrow_protected_regions_hz
    if regions:
        if frequencies_hz is None or len(frequencies_hz) != len(original):
            raise ValueError("Signal protection requires the matching frequency axis.")
        axis = np.asarray(frequencies_hz)
        for low, high in regions:
            protected |= (axis >= low) & (axis <= high)
        notes.append(f"Signal protection: {int(protected.sum())} bins unchanged by display filters.")
    if "background" in selected and not background_profile.signal_free_qualified:
        notes.append("Signal absence and uncertainty remain unqualified; display preview only.")
    interference: set[int] = set()
    removed: set[int] = set()
    for mode in SPECTRUM_FILTER_ORDER:
        if mode not in selected or mode == "background":
            continue
        if mode == "emi_reject" and unit not in {"dBm", "dB"}:
            notes.append(f"Stationary-line rejection requires dB or dBm; source unit is {unit}.")
            continue
        try:
            stage = clean_spectrum_values(
                result.values, unit=unit, mode=mode, history_dbm=history,
                parameters=parameters, frequencies_hz=frequencies_hz,
            )
        except ValueError as exc:
            notes.append(f"{SPECTRUM_FILTER_LABELS[mode]} unavailable: {exc}")
            continue
        if np.any(protected):
            restored = np.asarray(stage.values).copy()
            restored[protected] = np.asarray(original)[protected]
            stage = replace(stage, values_dbm=tuple(restored),
                stationary_interference_indices=tuple(i for i in stage.stationary_interference_indices if not protected[i]),
                removed_peak_indices=tuple(i for i in stage.removed_peak_indices if not protected[i]))
        result = stage
        applied_modes.append(mode)
        methods.append(result.method)
        notes.extend(result.notes)
        interference.update(result.stationary_interference_indices)
        removed.update(result.removed_peak_indices)
    if any(mode in selected for mode in ("narrow_reject", "emi_reject")):
        notes.append("Outlier thresholds are heuristic scales, not detection confidence or measurement uncertainty.")
    if "narrow_reject" in selected and not regions:
        notes.append("No protected signal bands: real narrow resonances can be removed.")
    modified = tuple(
        index
        for index, (before, after) in enumerate(zip(original, result.values, strict=True))
        if before != after
    )
    return SpectrumCleanupResult(
        result.values, (_linear_noise_sigma(original) if unit not in {"dBm", "dB"} else robust_noise_sigma_db(original)), tuple(sorted(interference)),
        " → ".join(methods) if methods else result.method, unit,
        modified, tuple(sorted(removed)), tuple(notes), original,
        ("background", background_profile.profile_id, background_profile.content_hash, background_profile.context_id)
        if "background" in selected else (), tuple(applied_modes),
    )


def _crossing_frequency(
    frequencies: np.ndarray,
    values: np.ndarray,
    start: int,
    direction: int,
    level_dbm: float,
) -> float | None:
    index = start
    while 0 <= index + direction < values.size:
        following = index + direction
        if (values[index] - level_dbm) * (values[following] - level_dbm) <= 0:
            y0, y1 = values[index], values[following]
            if math.isclose(float(y0), float(y1), abs_tol=1e-15):
                return float(frequencies[following])
            fraction = float((level_dbm - y0) / (y1 - y0))
            return float(frequencies[index] + fraction * (frequencies[following] - frequencies[index]))
        index = following
    return None


def _quadratic_center(frequencies: np.ndarray, values: np.ndarray, index: int) -> tuple[float, float]:
    if index <= 0 or index >= values.size - 1:
        return float(frequencies[index]), float(values[index])
    x = frequencies[index - 1 : index + 2]
    y = values[index - 1 : index + 2]
    shifted = x - x[1]
    coefficients = np.polyfit(shifted, y, 2)
    if coefficients[0] >= 0 or math.isclose(float(coefficients[0]), 0.0, abs_tol=1e-30):
        return float(frequencies[index]), float(values[index])
    offset = float(-coefficients[1] / (2.0 * coefficients[0]))
    if abs(offset) > abs(float(x[2] - x[0])):
        return float(frequencies[index]), float(values[index])
    amplitude = float(np.polyval(coefficients, offset))
    return float(x[1] + offset), amplitude


def _fit_peak_shape(
    frequencies: np.ndarray,
    values_dbm: np.ndarray,
    index: int,
    measured_fwhm_hz: float | None,
) -> tuple[str, float | None, float | None, float | None]:
    spacing = float(np.median(np.diff(frequencies)))
    half_points = min(50, max(5, int(round((measured_fwhm_hz or spacing * 4) / spacing * 2))))
    start, stop = max(0, index - half_points), min(values_dbm.size, index + half_points + 1)
    x = frequencies[start:stop]
    y_mw = 10.0 ** (values_dbm[start:stop] / 10.0)
    if x.size < 7:
        return "none", None, None, None
    initial_width = max(measured_fwhm_hz or spacing * 3.0, spacing * 1.25)
    minimum_width = max(spacing, initial_width / 4.0)
    maximum_width = max(
        minimum_width,
        min(max(float(x[-1] - x[0]), spacing), initial_width * 4.0),
    )
    widths = np.geomspace(minimum_width, maximum_width, 20)
    center_span = max(
        spacing,
        min(initial_width / 2.0, float(x[-1] - x[0]) / 4.0),
    )
    centers = frequencies[index] + np.linspace(-center_span, center_span, 21)
    best: tuple[float, str, float, float] | None = None
    for model in ("Gaussian", "Lorentzian"):
        for center in centers:
            for width in widths:
                normalized = (x - center) / width
                shape = (
                    np.exp(-4.0 * math.log(2.0) * normalized**2)
                    if model == "Gaussian"
                    else 1.0 / (1.0 + 4.0 * normalized**2)
                )
                design = np.column_stack((np.ones(shape.size), shape))
                baseline, amplitude = np.linalg.lstsq(design, y_mw, rcond=None)[0]
                if baseline < 0 or amplitude <= 0:
                    continue
                predicted_mw = np.maximum(baseline + amplitude * shape, 1e-300)
                predicted_dbm = 10.0 * np.log10(predicted_mw)
                measured_dbm = 10.0 * np.log10(np.maximum(y_mw, 1e-300))
                rmse = float(np.sqrt(np.mean((predicted_dbm - measured_dbm) ** 2)))
                if best is None or rmse < best[0]:
                    best = (rmse, model, float(center), float(width))
    if best is None:
        return "none", None, None, None
    return best[1], best[2], best[3], best[0]


def _width_allowed(width_hz: float | None, parameters: SpectrumAnalysisParameters) -> bool:
    if not parameters.peak_min_width_hz and not parameters.peak_max_width_hz:
        return True
    return width_hz is not None and width_hz >= parameters.peak_min_width_hz and (
        not parameters.peak_max_width_hz or width_hz <= parameters.peak_max_width_hz)


def _detect_linear_peaks(
    frequencies: np.ndarray, values: np.ndarray, parameters: SpectrumAnalysisParameters,
    unit: str, descending: bool,
) -> tuple[SpectrumPeak, ...]:
    """Signed linear extrema: thresholds in noise multiples, geometry in Hz."""
    from scipy.signal import find_peaks, peak_widths

    detection = _gaussian_detection_trace(np.asarray(bilateral_denoise_linear(values, window=11)))
    sigma = max(_linear_noise_sigma(values), np.finfo(float).tiny)
    candidates: list[SpectrumPeak] = []
    signs = (1, -1) if parameters.peak_polarity == "both" else (
        -1 if parameters.peak_polarity == "negative" else 1,)
    bins = np.arange(values.size)
    for sign in signs:
        oriented = sign * detection
        indices, properties = find_peaks(oriented, prominence=parameters.peak_min_prominence_sigma * sigma)
        if not indices.size:
            continue
        widths = peak_widths(oriented, indices, rel_height=.5,
                            prominence_data=(properties["prominences"], properties["left_bases"], properties["right_bases"]))
        for row, index in enumerate(indices):
            prominence = float(properties["prominences"][row])
            baseline = float(detection[index] - sign * prominence)
            snr = float(sign * (values[index] - baseline) / sigma)
            if snr < parameters.peak_min_snr_sigma:
                continue
            left = float(np.interp(widths[2][row], bins, frequencies))
            right = float(np.interp(widths[3][row], bins, frequencies))
            width_hz = right - left
            if not _width_allowed(width_hz, parameters):
                continue
            center, _ = _quadratic_center(frequencies, oriented, int(index))
            candidates.append(SpectrumPeak(
                index=int(values.size - 1 - index if descending else index), frequency_hz=float(center),
                amplitude_dbm=float(values[index]), noise_floor_dbm=baseline,
                snr_db=snr, prominence_db=prominence / sigma,
                left_half_power_hz=left, right_half_power_hz=right, fwhm_hz=width_hz,
                q_factor=float(center / width_hz) if width_hz > 0 else None,
                fit_model="not fitted", fit_center_hz=None, fit_fwhm_hz=None, fit_rmse_db=None,
                amplitude_unit=unit, contrast_unit="σ",
            ))
    candidates.sort(key=lambda peak: peak.snr_db, reverse=True)
    accepted: list[SpectrumPeak] = []
    automatic_distance = float(np.median(np.diff(frequencies))) * max(1, values.size // 500)
    minimum_distance = parameters.peak_min_distance_hz or automatic_distance
    for peak in candidates:
        if any(abs(peak.frequency_hz - other.frequency_hz) < minimum_distance for other in accepted):
            continue
        accepted.append(peak)
        if len(accepted) >= max(1, parameters.peak_max_count):
            break
    return tuple(accepted)


def detect_spectrum_peaks(
    frequencies_hz: Sequence[float],
    values_dbm: Sequence[float],
    *,
    min_snr_db: float = 6.0,
    min_prominence_db: float = 3.0,
    max_peaks: int = 20,
    fit: bool = True,
    unit: str = "dBm",
    parameters: SpectrumAnalysisParameters | None = None,
) -> tuple[SpectrumPeak, ...]:
    if parameters is not None:
        min_snr_db = parameters.peak_min_snr_db
        min_prominence_db = parameters.peak_min_prominence_db
        max_peaks = parameters.peak_max_count
        fit = parameters.peak_fit_models
    frequencies, values = _finite_vectors(frequencies_hz, values_dbm)
    if unit not in {"dBm", "dB"}:
        return _detect_linear_peaks(frequencies, values, parameters or SpectrumAnalysisParameters(
            peak_max_count=max_peaks), unit, frequencies_hz[0] > frequencies_hz[-1])
    detection_values = _gaussian_detection_trace(
        np.asarray(bilateral_denoise_dbm(values, window=11), dtype=float)
    )
    floor_window = _odd_window(values.size, max(51, values.size // 5))
    floor = rolling_noise_floor_dbm(detection_values, window=floor_window)
    local_maximum = np.r_[
        False,
        (detection_values[1:-1] > detection_values[:-2])
        & (detection_values[1:-1] >= detection_values[2:]),
        False,
    ]
    candidates = np.flatnonzero(
        local_maximum
        & ((detection_values - floor) >= float(min_snr_db))
    )
    neighborhood = max(3, min(50, values.size // 100))
    measured: list[SpectrumPeak] = []
    for index in candidates:
        left_slice = detection_values[max(0, index - neighborhood) : index + 1]
        right_slice = detection_values[
            index : min(values.size, index + neighborhood + 1)
        ]
        local_prominence = float(
            detection_values[index]
            - max(float(np.min(left_slice)), float(np.min(right_slice)))
        )
        prominence = max(
            local_prominence,
            float(detection_values[index] - floor[index]),
        )
        if prominence < min_prominence_db:
            continue
        center_hz, _smoothed_amplitude = _quadratic_center(
            frequencies, detection_values, int(index)
        )
        amplitude_dbm = float(values[index])
        half_power = float(
            detection_values[index]
            - (10.0 * math.log10(2.0) if unit in {"dBm", "dB"} else detection_values[index] / 2.0)
        )
        left_hz = _crossing_frequency(
            frequencies, detection_values, int(index), -1, half_power
        )
        right_hz = _crossing_frequency(
            frequencies, detection_values, int(index), 1, half_power
        )
        fwhm_hz = (
            float(right_hz - left_hz)
            if left_hz is not None and right_hz is not None and right_hz > left_hz
            else None
        )
        if parameters is not None and not _width_allowed(fwhm_hz, parameters):
            continue
        fit_model, fit_center, fit_width, fit_rmse = (
            _fit_peak_shape(frequencies, values, int(index), fwhm_hz)
            if fit and unit == "dBm"
            else ("not fitted", None, None, None)
        )
        effective_center = fit_center if fit_center is not None else center_hz
        effective_width = fit_width if fit_width is not None else fwhm_hz
        measured.append(
            SpectrumPeak(
                index=int(index),
                frequency_hz=float(effective_center),
                amplitude_dbm=float(amplitude_dbm),
                noise_floor_dbm=float(floor[index]),
                snr_db=float(amplitude_dbm - floor[index]),
                prominence_db=prominence,
                left_half_power_hz=left_hz,
                right_half_power_hz=right_hz,
                fwhm_hz=fwhm_hz,
                q_factor=(float(effective_center / effective_width) if effective_width and effective_width > 0 else None),
                fit_model=fit_model,
                fit_center_hz=fit_center,
                fit_fwhm_hz=fit_width,
                fit_rmse_db=fit_rmse,
                amplitude_unit=unit,
            )
        )
    measured.sort(
        key=lambda peak: (
            peak.snr_db,
            peak.prominence_db,
            -(peak.fit_rmse_db if peak.fit_rmse_db is not None else math.inf),
        ),
        reverse=True,
    )
    accepted: list[SpectrumPeak] = []
    minimum_distance = max(1, values.size // 500)
    for peak in measured:
        if any(
            (parameters is not None and parameters.peak_min_distance_hz > 0
             and abs(peak.frequency_hz - existing.frequency_hz) < parameters.peak_min_distance_hz)
            or ((parameters is None or parameters.peak_min_distance_hz == 0)
                and abs(peak.index - existing.index) < minimum_distance)
            or abs(peak.frequency_hz - existing.frequency_hz)
            < 0.5
            * max(
                peak.fit_fwhm_hz or peak.fwhm_hz or 0.0,
                existing.fit_fwhm_hz or existing.fwhm_hz or 0.0,
            )
            for existing in accepted
        ):
            continue
        accepted.append(peak)
        if len(accepted) >= max(1, int(max_peaks)):
            break
    return tuple(accepted)
