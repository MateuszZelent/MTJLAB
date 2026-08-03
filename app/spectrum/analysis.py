"""Read-only spectrum cleanup, peak measurement, and fitting.

Every function returns derived arrays and leaves the acquired trace untouched.
Frequency values remain in Hz.  The legacy dBm names are retained for API
compatibility, while ``clean_spectrum_values`` and the ``unit`` fields keep
relative/linear display traces explicitly labelled instead of relabelling them
as dBm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


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


@dataclass(frozen=True, slots=True)
class SpectrumCleanupResult:
    values_dbm: tuple[float, ...]
    noise_sigma_db: float
    stationary_interference_indices: tuple[int, ...]
    method: str
    unit: str = "dBm"

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
) -> SpectrumCleanupResult:
    return clean_spectrum_values(values_dbm, unit="dBm", mode=mode, history_dbm=history_dbm)


def clean_spectrum_values(
    values: Sequence[float],
    *,
    unit: str,
    mode: str,
    history_dbm: Sequence[Sequence[float]] = (),
) -> SpectrumCleanupResult:
    """Clean the numeric values of any displayed spectrum unit.

    The algorithms are value-domain operations and do not change units.  The
    legacy ``clean_spectrum_dbm`` entry point remains the dBm-specialized API.
    """

    values = tuple(float(value) for value in values)
    sigma = robust_noise_sigma_db(values)
    mode = mode.lower()
    if mode == "raw":
        return SpectrumCleanupResult(values, sigma, (), "Raw (no processing)", unit)
    interference = detect_stationary_interference(history_dbm)
    if mode == "denoise":
        cleaned = bilateral_denoise_dbm(values)
        method = "Edge-preserving bilateral denoise"
    elif mode == "emi_reject":
        cleaned = suppress_stationary_lines_dbm(values, interference)
        method = "Conservative stationary-line rejection (display only)"
    elif mode == "auto_clean":
        denoised = bilateral_denoise_dbm(values)
        cleaned = suppress_stationary_lines_dbm(denoised, interference)
        method = "Bilateral denoise + stationary-line rejection (display only)"
    else:
        raise ValueError(f"Unsupported spectrum cleanup mode: {mode!r}.")
    return SpectrumCleanupResult(tuple(cleaned), sigma, interference, method, unit)


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


def detect_spectrum_peaks(
    frequencies_hz: Sequence[float],
    values_dbm: Sequence[float],
    *,
    min_snr_db: float = 6.0,
    min_prominence_db: float = 3.0,
    max_peaks: int = 20,
    fit: bool = True,
    unit: str = "dBm",
) -> tuple[SpectrumPeak, ...]:
    frequencies, values = _finite_vectors(frequencies_hz, values_dbm)
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
            abs(peak.index - existing.index) < minimum_distance
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
