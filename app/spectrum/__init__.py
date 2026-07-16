"""Spectrum processing helpers that never mutate the acquired raw trace."""

from .processing import average_dbm_traces, apply_reference_operation

__all__ = ["average_dbm_traces", "apply_reference_operation"]
