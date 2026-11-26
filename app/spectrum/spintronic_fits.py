"""Offline spintronic physics models: resonance fitting, Kittel dispersion, and Gilbert damping.

All calculations operate strictly in SI units (Hz, T, W, W*Hz) on read-only
arrays extracted from stored HDF5 results. Never modifies source files.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from app.domain.quantities import DIMENSION_MAGNETIC_FIELD, parse_quantity
from app.spectrum.resonance_metrics import (
    LinearResonanceFit,
    fit_linear_resonance,
    resonance_values,
)

DEFAULT_GAMMA_GHZ_PER_T = 28.02495


@dataclass(frozen=True, slots=True)
class SpectrumResonanceSummary:
    """Display-ready resonance fit summary backed by linear-power fitting."""

    fit: LinearResonanceFit
    q_factor: float
    r_squared: float
    display_unit: str
    frequencies_hz: np.ndarray
    fitted_display_values: np.ndarray


@dataclass(frozen=True, slots=True)
class KittelDispersionFit:
    """In-plane Kittel FMR dispersion fit f0(H) = (gamma/2pi)*sqrt((|H|+Hk)(|H|+Hk+Meff))."""

    m_eff_t: float
    h_k_t: float
    gamma_ghz_per_t: float
    rmse_hz: float
    r_squared: float
    point_count: int


@dataclass(frozen=True, slots=True)
class GilbertDampingFit:
    """Frequency-swept linear linewidth fit df(f0) = df0 + 2 * alpha_eff * f0."""

    alpha_eff: float
    inhomogeneous_fwhm_hz: float
    rmse_hz: float
    r_squared: float
    point_count: int


def kittel_frequency_hz(
    fields_t: Sequence[float] | np.ndarray,
    *,
    m_eff_t: float,
    h_k_t: float,
    gamma_ghz_per_t: float = DEFAULT_GAMMA_GHZ_PER_T,
) -> np.ndarray:
    """Evaluate the in-plane Kittel dispersion formula in Hz for fields in Tesla."""
    if not all(math.isfinite(v) for v in (m_eff_t, h_k_t, gamma_ghz_per_t)):
        raise ValueError("Kittel parameters must be finite real numbers.")
    if m_eff_t <= 0 or gamma_ghz_per_t <= 0:
        raise ValueError("M_eff and gyromagnetic ratio must be strictly positive.")
    h_abs = np.abs(np.asarray(fields_t, dtype=float))
    if not np.all(np.isfinite(h_abs)):
        raise ValueError("Magnetic field values must be finite.")
    term1 = np.maximum(0.0, h_abs + h_k_t)
    term2 = np.maximum(0.0, h_abs + h_k_t + m_eff_t)
    return (gamma_ghz_per_t * 1e9) * np.sqrt(term1 * term2)


def fit_kittel_dispersion(
    fields_t: Sequence[float] | np.ndarray,
    frequencies_hz: Sequence[float] | np.ndarray,
    *,
    gamma_ghz_per_t: float = DEFAULT_GAMMA_GHZ_PER_T,
) -> KittelDispersionFit:
    """Fit effective magnetization (mu0*M_eff) and anisotropy field (H_k) in Tesla."""
    from scipy.optimize import least_squares

    h = np.asarray(fields_t, dtype=float)
    f = np.asarray(frequencies_hz, dtype=float)
    if h.ndim != 1 or f.ndim != 1 or h.shape != f.shape:
        raise ValueError("Fields and frequencies must be matching 1-D arrays.")
    mask = np.isfinite(h) & np.isfinite(f) & (f > 0)
    h, f = np.abs(h[mask]), f[mask]
    if h.size < 4:
        raise ValueError("Kittel fit requires at least 4 finite resonance points.")

    f_scale = float(np.max(f))
    gamma_hz_per_t = gamma_ghz_per_t * 1e9
    # Initial guess from quadratic f^2 / gamma^2 = (|H|+Hk)^2 + Meff*(|H|+Hk)
    h_span = float(np.max(h) - np.min(h))
    if h_span <= 0:
        raise ValueError("Kittel fit requires variation in magnetic field.")
    y_quad = (f / gamma_hz_per_t) ** 2
    slope = max(0.05, float((np.max(y_quad) - np.min(y_quad)) / h_span))
    m_init = min(3.0, max(0.05, slope))
    hk_init = max(0.0, float(np.min(y_quad) / m_init - np.min(h)))
    hk_init = min(1.0, hk_init)

    def residual(params: np.ndarray) -> np.ndarray:
        m_eff, h_k = float(params[0]), float(params[1])
        pred = kittel_frequency_hz(
            h, m_eff_t=m_eff, h_k_t=h_k, gamma_ghz_per_t=gamma_ghz_per_t
        )
        return (pred - f) / f_scale

    result = least_squares(
        residual,
        x0=[m_init, hk_init],
        bounds=([0.01, -0.5], [5.0, 2.0]),
        max_nfev=300,
    )
    if not result.success:
        raise ValueError("Kittel dispersion fit did not converge.")
    m_eff_t, h_k_t = float(result.x[0]), float(result.x[1])
    pred = kittel_frequency_hz(
        h, m_eff_t=m_eff_t, h_k_t=h_k_t, gamma_ghz_per_t=gamma_ghz_per_t
    )
    err = pred - f
    rmse_hz = float(np.sqrt(np.mean(err**2)))
    ss_tot = float(np.sum((f - np.mean(f)) ** 2))
    r_squared = 1.0 - float(np.sum(err**2)) / ss_tot if ss_tot > 0 else 1.0
    return KittelDispersionFit(
        m_eff_t=m_eff_t,
        h_k_t=h_k_t,
        gamma_ghz_per_t=gamma_ghz_per_t,
        rmse_hz=rmse_hz,
        r_squared=r_squared,
        point_count=int(h.size),
    )


def fit_gilbert_damping(
    frequencies_hz: Sequence[float] | np.ndarray,
    fwhm_hz: Sequence[float] | np.ndarray,
) -> GilbertDampingFit:
    """Fit effective Gilbert damping from frequency-swept linewidth df(f0) = df0 + 2*alpha*f0."""
    f = np.asarray(frequencies_hz, dtype=float)
    df = np.asarray(fwhm_hz, dtype=float)
    if f.ndim != 1 or df.ndim != 1 or f.shape != df.shape:
        raise ValueError("Frequencies and FWHM must be matching 1-D arrays.")
    mask = np.isfinite(f) & np.isfinite(df) & (f > 0) & (df > 0)
    f, df = f[mask], df[mask]
    if f.size < 3:
        raise ValueError("Gilbert damping fit requires at least 3 finite points.")
    if float(np.ptp(f)) <= 0:
        raise ValueError("Gilbert damping fit requires multiple distinct resonance frequencies.")

    slope, intercept = np.polyfit(f, df, 1)
    pred = intercept + slope * f
    err = pred - df
    rmse_hz = float(np.sqrt(np.mean(err**2)))
    ss_tot = float(np.sum((df - np.mean(df)) ** 2))
    r_squared = 1.0 - float(np.sum(err**2)) / ss_tot if ss_tot > 0 else 1.0
    return GilbertDampingFit(
        alpha_eff=float(slope / 2.0),
        inhomogeneous_fwhm_hz=float(intercept),
        rmse_hz=rmse_hz,
        r_squared=r_squared,
        point_count=int(f.size),
    )


def magnetic_field_scale_to_tesla(unit: str) -> float | None:
    """Return scale factor from a magnetic-field unit string to Tesla, or None."""
    cleaned = unit.strip()
    if not cleaned:
        return None
    try:
        return float(parse_quantity(f"1 {cleaned}", DIMENSION_MAGNETIC_FIELD).si_value)
    except ValueError:
        return None


def extract_heatmap_ridge(
    matrix: np.ndarray,
    x_values: np.ndarray,
    y_values: np.ndarray,
    *,
    frequency_axis: int = 1,
    exclude_bands_hz: Sequence[tuple[float, float]] = (),
    min_prominence_ratio: float = 0.25,
) -> tuple[np.ndarray, np.ndarray]:
    """Track dominant peak frequency along each sweep coordinate in a 2-D heatmap.

    Returns ``(sweep_values, peak_frequencies_hz)`` containing finite tracked points.
    """
    data = np.asarray(matrix, dtype=float)
    if data.ndim != 2 or 0 in data.shape:
        return np.asarray((), dtype=float), np.asarray((), dtype=float)
    if frequency_axis == 0:
        data = data.T
        freqs = np.asarray(y_values, dtype=float)
        sweeps = np.asarray(x_values, dtype=float)
    else:
        freqs = np.asarray(x_values, dtype=float)
        sweeps = np.asarray(y_values, dtype=float)
    if data.shape != (sweeps.size, freqs.size):
        return np.asarray((), dtype=float), np.asarray((), dtype=float)

    valid_cols = np.isfinite(freqs)
    for lo, hi in exclude_bands_hz:
        valid_cols &= ~((freqs >= min(lo, hi)) & (freqs <= max(lo, hi)))
    if not np.any(valid_cols):
        return np.asarray((), dtype=float), np.asarray((), dtype=float)

    tracked_sweeps: list[float] = []
    tracked_freqs: list[float] = []
    for row_idx in range(data.shape[0]):
        if not np.isfinite(sweeps[row_idx]):
            continue
        row = data[row_idx]
        usable = valid_cols & np.isfinite(row)
        if int(np.count_nonzero(usable)) < 5:
            continue
        row_vals = row[usable]
        row_freqs = freqs[usable]
        med = float(np.median(row_vals))
        peak_i = int(np.argmax(row_vals))
        peak_val = float(row_vals[peak_i])
        spread = float(np.max(row_vals) - np.min(row_vals))
        if spread <= 0 or (peak_val - med) < spread * min_prominence_ratio:
            continue
        tracked_sweeps.append(float(sweeps[row_idx]))
        tracked_freqs.append(float(row_freqs[peak_i]))
    return np.asarray(tracked_sweeps, dtype=float), np.asarray(tracked_freqs, dtype=float)


def fit_spectrum_resonance(
    frequencies_hz: Sequence[float] | np.ndarray,
    values: Sequence[float] | np.ndarray,
    *,
    unit: str,
    shape: str = "lorentzian",
    window_hz: tuple[float, float] | None = None,
) -> SpectrumResonanceSummary:
    """Fit a single Lorentzian or Gaussian resonance on linear power and map back for display."""
    freqs = np.asarray(frequencies_hz, dtype=float)
    vals = np.asarray(values, dtype=float)
    if freqs.ndim != 1 or vals.ndim != 1 or freqs.shape != vals.shape:
        raise ValueError("Frequency and amplitude arrays must have identical 1-D shapes.")
    mask = np.isfinite(freqs) & np.isfinite(vals)
    if window_hz is not None:
        lo, hi = min(window_hz), max(window_hz)
        mask &= (freqs >= lo) & (freqs <= hi)
    freqs, vals = freqs[mask], vals[mask]
    if freqs.size < 11:
        raise ValueError("Resonance fit requires at least 11 finite frequency points.")

    # Convert logarithmic dBm/dB to strictly positive linear power (W / ratio)
    # so the physical Lorentzian/Gaussian area integral preserves W*Hz semantics.
    if unit == "dBm":
        linear_w = np.power(10.0, (vals - 30.0) / 10.0)
    elif unit == "W":
        linear_w = vals
    elif unit == "dB":
        linear_w = np.power(10.0, vals / 10.0)
    else:
        linear_w = vals

    edge = max(2, linear_w.size // 15)
    baseline_est = float(np.median(np.r_[linear_w[:edge], linear_w[-edge:]]))
    delta = linear_w - baseline_est
    peak_idx = int(np.argmax(np.abs(delta)))
    # Keep initial center strictly inside the open interval (freqs[0], freqs[-1])
    peak_idx = max(1, min(freqs.size - 2, peak_idx))
    center_est = float(freqs[peak_idx])
    peak_height = abs(float(delta[peak_idx]))
    half = peak_height * 0.5
    above = np.flatnonzero(np.abs(delta) >= half)
    span = float(freqs[-1] - freqs[0])
    step = float(np.min(np.diff(freqs)))
    if above.size >= 2:
        fwhm_est = float(freqs[above[-1]] - freqs[above[0]])
    else:
        fwhm_est = span * 0.08
    fwhm_est = max(step * 1.5, min(span * 0.6, fwhm_est))

    fit = fit_linear_resonance(
        freqs,
        linear_w,
        initial_center_hz=center_est,
        initial_fwhm_hz=fwhm_est,
        shape=shape,
    )
    fitted_linear = (
        resonance_values(
            freqs,
            fit.amplitude_w,
            fit.center_hz,
            fit.fwhm_hz,
            shape=shape,
        )
        + fit.baseline_w
    )
    ss_res = float(np.sum((fitted_linear - linear_w) ** 2))
    ss_tot = float(np.sum((linear_w - np.mean(linear_w)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    q_factor = fit.center_hz / fit.fwhm_hz if fit.fwhm_hz > 0 else 0.0

    if unit == "dBm":
        fitted_display = 10.0 * np.log10(np.maximum(fitted_linear, 1e-24)) + 30.0
    elif unit == "dB":
        fitted_display = 10.0 * np.log10(np.maximum(fitted_linear, 1e-24))
    else:
        fitted_display = fitted_linear

    return SpectrumResonanceSummary(
        fit=fit,
        q_factor=float(q_factor),
        r_squared=float(r_squared),
        display_unit=unit,
        frequencies_hz=freqs,
        fitted_display_values=fitted_display,
    )
