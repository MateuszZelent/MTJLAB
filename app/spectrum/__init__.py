"""Spectrum processing helpers that never mutate the acquired raw trace."""

from .processing import (
    LinearPowerAverager,
    apply_reference_operation,
    average_dbm_traces,
    frequency_grids_match,
    peak_preserving_indices,
)
from .analysis import (
    SpectrumAnalysisParameters,
    SpectrumCleanupResult,
    SpectrumPeak,
    bilateral_denoise_dbm,
    clean_spectrum_dbm,
    clean_spectrum_values,
    detect_spectrum_peaks,
    detect_stationary_interference,
    robust_noise_sigma_db,
    rolling_noise_floor_dbm,
    suppress_stationary_lines_dbm,
)
from .display_model import SpectrumDisplayState, SpectrumDisplayTrace, build_display_state

__all__ = [
    "LinearPowerAverager",
    "apply_reference_operation",
    "average_dbm_traces",
    "frequency_grids_match",
    "peak_preserving_indices",
    "SpectrumAnalysisParameters",
    "SpectrumCleanupResult",
    "SpectrumPeak",
    "bilateral_denoise_dbm",
    "clean_spectrum_dbm",
    "clean_spectrum_values",
    "detect_spectrum_peaks",
    "detect_stationary_interference",
    "robust_noise_sigma_db",
    "rolling_noise_floor_dbm",
    "suppress_stationary_lines_dbm",
    "SpectrumDisplayState",
    "SpectrumDisplayTrace",
    "build_display_state",
]
