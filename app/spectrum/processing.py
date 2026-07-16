"""Numerically explicit spectrum averaging and reference mathematics."""

from __future__ import annotations

import math
from collections.abc import Sequence


def _dbm_to_mw(value_dbm: float) -> float:
    return 10.0 ** (value_dbm / 10.0)


def _mw_to_dbm(value_mw: float) -> float:
    return 10.0 * math.log10(max(value_mw, 1e-300))


def average_dbm_traces(traces: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """Average logarithmic traces correctly by averaging linear mW values."""

    if not traces:
        raise ValueError("At least one spectrum is required for averaging.")
    points = len(traces[0])
    if points < 2 or any(len(trace) != points for trace in traces):
        raise ValueError("All spectra must contain the same number of points.")
    return tuple(
        _mw_to_dbm(sum(_dbm_to_mw(trace[index]) for trace in traces) / len(traces))
        for index in range(points)
    )


def apply_reference_operation(
    signal_dbm: Sequence[float],
    reference_dbm: Sequence[float],
    operation: str,
) -> tuple[tuple[float, ...], str]:
    """Apply point-wise reference math and return values plus an honest unit."""

    if len(signal_dbm) != len(reference_dbm) or len(signal_dbm) < 2:
        raise ValueError("Signal and reference spectra must have identical point counts.")
    operation = operation.lower()
    if operation == "difference_db":
        return tuple(signal - reference for signal, reference in zip(signal_dbm, reference_dbm, strict=True)), "dB"
    signal_mw = tuple(_dbm_to_mw(value) for value in signal_dbm)
    reference_mw = tuple(_dbm_to_mw(value) for value in reference_dbm)
    if operation == "ratio_linear":
        return tuple(signal / reference for signal, reference in zip(signal_mw, reference_mw, strict=True)), "ratio"
    if operation == "add_power":
        return tuple(_mw_to_dbm(signal + reference) for signal, reference in zip(signal_mw, reference_mw, strict=True)), "dBm"
    if operation == "subtract_power":
        return tuple(_mw_to_dbm(max(signal - reference, 1e-30)) for signal, reference in zip(signal_mw, reference_mw, strict=True)), "dBm"
    if operation == "multiply_linear":
        return tuple(signal * reference for signal, reference in zip(signal_mw, reference_mw, strict=True)), "mW²"
    raise ValueError(f"Unsupported reference operation: {operation}.")
