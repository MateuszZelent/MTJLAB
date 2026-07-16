"""Spectrum processing helpers that never mutate the acquired raw trace."""

from .processing import (
    LinearPowerAverager,
    apply_reference_operation,
    average_dbm_traces,
    frequency_grids_match,
    peak_preserving_indices,
)

__all__ = [
    "LinearPowerAverager",
    "apply_reference_operation",
    "average_dbm_traces",
    "frequency_grids_match",
    "peak_preserving_indices",
]
