"""Signed linear-power resonance hypotheses, fitted offline without inferred CI."""

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True, slots=True)
class LinearResonanceFit:
    shape: str
    amplitude_w: float
    center_hz: float
    fwhm_hz: float
    baseline_w: float
    finite_window_area_w_hz: float
    rmse_w: float
    condition_number: float
    optimizer_evaluations: int


def resonance_values(frequencies_hz, amplitude_w, center_hz, fwhm_hz, *, shape="gaussian"):
    if shape not in {"gaussian", "lorentzian"} or not all(
        math.isfinite(value) for value in (amplitude_w, center_hz, fwhm_hz)
    ) or fwhm_hz <= 0:
        raise ValueError("Resonance requires a supported shape and finite SI parameters with positive FWHM.")
    frequencies = np.asarray(frequencies_hz)
    if frequencies.dtype.kind not in "iuf" or not np.all(np.isfinite(frequencies)):
        raise ValueError("Resonance frequency values must be finite real numbers.")
    x = (frequencies.astype(float) - center_hz) / fwhm_hz
    return amplitude_w * (np.exp(-4 * np.log(2) * x**2) if shape == "gaussian" else 1 / (1 + 4 * x**2))


def resonance_area(amplitude_w, center_hz, fwhm_hz, low_hz, high_hz, *, shape="gaussian"):
    resonance_values([low_hz, high_hz], amplitude_w, center_hz, fwhm_hz, shape=shape)
    if not all(math.isfinite(value) for value in (low_hz, high_hz)) or low_hz >= high_hz:
        raise ValueError("Resonance integral requires an ordered finite frequency window.")
    if shape == "gaussian":
        sigma = fwhm_hz / (2 * np.sqrt(2 * np.log(2)))
        return amplitude_w * sigma * np.sqrt(np.pi / 2) * (
            math.erf((high_hz - center_hz) / (np.sqrt(2) * sigma))
            - math.erf((low_hz - center_hz) / (np.sqrt(2) * sigma))
        )
    gamma = fwhm_hz / 2
    return amplitude_w * gamma * (math.atan((high_hz - center_hz) / gamma) - math.atan((low_hz - center_hz) / gamma))


def fit_linear_resonance(frequencies_hz, values_w, *, initial_center_hz, initial_fwhm_hz,
                         shape="gaussian", maximum_evaluations=150):
    """Known single-feature hypothesis with constant baseline; no peak detector.

    Frequency and power are normalized before optimization. A fit is not a
    detection or a confidence interval. Correlated-bin covariance is not
    inferred from optimizer residuals. Integral unit is W*Hz, not watts.
    """
    try:
        from scipy.optimize import least_squares
    except ImportError as exc:
        raise ValueError("Offline resonance fitting needs the optional qualification dependencies.") from exc
    frequencies, values = np.asarray(frequencies_hz), np.asarray(values_w)
    if frequencies.dtype.kind not in "iuf" or values.dtype.kind not in "iuf":
        raise ValueError("Resonance arrays must contain real numeric values.")
    frequencies, values = frequencies.astype(float), values.astype(float)
    if frequencies.ndim != 1 or frequencies.size < 11 or values.shape != frequencies.shape or (
        not np.all(np.isfinite(frequencies)) or not np.all(np.isfinite(values)) or np.any(np.diff(frequencies) <= 0)
    ):
        raise ValueError("Resonance fit needs matching finite arrays and a strictly increasing axis.")
    resonance_values(frequencies, 1, initial_center_hz, initial_fwhm_hz, shape=shape)
    if not frequencies[0] < initial_center_hz < frequencies[-1] or type(maximum_evaluations) is not int or not 1 <= maximum_evaluations <= 10000:
        raise ValueError("Fit requires an initial center inside the grid and a bounded iteration count.")
    span = frequencies[-1] - frequencies[0]
    minimum_width = np.min(np.diff(frequencies)) / 2
    if not minimum_width < initial_fwhm_hz < span:
        raise ValueError("Initial FWHM must lie between half a grid step and the frequency span.")
    scale_w = float(np.max(np.abs(values)))
    if scale_w == 0:
        raise ValueError("A zero trace does not identify resonance parameters.")
    x = (frequencies - initial_center_hz) / initial_fwhm_hz
    y = values / scale_w
    edge_count = max(2, len(y) // 20)
    offset = float(np.median(np.r_[y[:edge_count], y[-edge_count:]]))
    index = int(np.argmax(np.abs(y - offset)))
    amplitude = y[index] - offset

    def residual(parameters):
        a, center, log_width, baseline = parameters
        return resonance_values(x, a, center, np.exp(log_width), shape=shape) + baseline - y

    fit = least_squares(residual, [amplitude, 0, 0, offset],
                        bounds=([-20, x[0], np.log(minimum_width / initial_fwhm_hz), -np.inf],
                                [20, x[-1], np.log(span / initial_fwhm_hz), np.inf]),
                        max_nfev=maximum_evaluations, x_scale=np.ones(4),
                        ftol=1e-10, xtol=1e-10, gtol=1e-10)
    condition = float(np.linalg.cond(fit.jac))
    if not fit.success or not np.isfinite(condition) or condition > 1e10 or np.any(fit.active_mask[:3]):
        raise ValueError("Resonance fit is unidentifiable, bound-limited or did not converge.")
    a, center, log_width, baseline = fit.x
    amplitude_w, center_hz = a * scale_w, initial_center_hz + center * initial_fwhm_hz
    width_hz = np.exp(log_width) * initial_fwhm_hz
    return LinearResonanceFit(shape, float(amplitude_w), float(center_hz), float(width_hz), float(baseline * scale_w),
                              float(resonance_area(amplitude_w, center_hz, width_hz, frequencies[0], frequencies[-1], shape=shape)),
                              float(np.linalg.norm(fit.fun) * scale_w / np.sqrt(len(values))), condition, fit.nfev)
