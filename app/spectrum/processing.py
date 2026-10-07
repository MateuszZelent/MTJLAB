"""Numerically explicit spectrum averaging and reference mathematics."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


class LinearPowerAverager:
    """Streaming dBm averager retaining one accumulator instead of all traces."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._sum_mw: np.ndarray | None = None
        self.count = 0

    def add(self, trace_dbm: Sequence[float]) -> int:
        arr = np.asarray(trace_dbm, dtype=float)
        if arr.ndim != 1:
            raise ValueError("A spectrum must be a one-dimensional sequence.")
        if arr.size < 2:
            raise ValueError("A spectrum must contain at least two points.")
        if not np.all(np.isfinite(arr)):
            raise ValueError("A spectrum contains NaN or infinity.")
        if self._sum_mw is not None and arr.shape != self._sum_mw.shape:
            raise ValueError("All spectra must contain the same number of points.")
        with np.errstate(over="ignore", under="ignore"):
            linear = 10.0 ** (arr / 10.0)
        if not np.all(np.isfinite(linear) & (linear > 0)):
            raise ValueError("Spectrum power cannot be represented as finite positive mW.")
        # Commit only after validation: a failed addition must leave both the
        # accumulated power and the sample count unchanged.
        with np.errstate(over="ignore"):
            candidate = linear if self._sum_mw is None else self._sum_mw + linear
        if not np.all(np.isfinite(candidate)):
            raise ValueError("Accumulated spectrum power exceeds the numeric mW range.")
        self._sum_mw = candidate
        self.count += 1
        return self.count

    def result(self) -> tuple[float, ...]:
        if self.count == 0 or self._sum_mw is None:
            raise ValueError("At least one spectrum is required for averaging.")
        # Subtract the logarithm of the count to avoid underflow when dividing
        # representable subnormal powers; do not silently clamp the result.
        dbm = 10.0 * (np.log10(self._sum_mw) - math.log10(self.count))
        return tuple(dbm.tolist())


def _dbm_to_mw(value_dbm: float) -> float:
    return 10.0 ** (value_dbm / 10.0)


def _mw_to_dbm(value_mw: float) -> float:
    return 10.0 * math.log10(max(value_mw, 1e-300))


def average_dbm_traces(traces: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """Average logarithmic traces correctly by averaging linear mW values."""

    if not traces:
        raise ValueError("At least one spectrum is required for averaging.")
    averager = LinearPowerAverager()
    for trace in traces:
        averager.add(trace)
    return averager.result()


def apply_reference_operation(
    signal_dbm: Sequence[float],
    reference_dbm: Sequence[float],
    operation: str,
) -> tuple[tuple[float, ...], str]:
    """Apply point-wise reference math and return values plus an honest unit."""

    sig = np.asarray(signal_dbm, dtype=float)
    ref = np.asarray(reference_dbm, dtype=float)
    if sig.ndim != 1 or ref.ndim != 1:
        raise ValueError("Signal and reference spectra must be one-dimensional.")
    if sig.shape != ref.shape or sig.size < 2:
        raise ValueError("Signal and reference spectra must have identical point counts.")
    if not (np.all(np.isfinite(sig)) and np.all(np.isfinite(ref))):
        raise ValueError("Signal and reference spectra must contain finite values.")
    operation = operation.lower()
    # Work in log power until a linear result is required. Converting both
    # inputs first can overflow even when their ratio/product is representable.
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        if operation == "difference_db":
            res, unit = sig - ref, "dB"
        elif operation == "ratio_linear":
            res, unit = np.power(10.0, sig / 10.0 - ref / 10.0), "ratio"
        elif operation == "multiply_linear":
            res, unit = np.power(10.0, sig / 10.0 + ref / 10.0), "mW²"
        elif operation == "add_power":
            high = np.maximum(sig, ref)
            ratio = np.power(10.0, -np.abs(sig / 10.0 - ref / 10.0))
            res, unit = high + (10.0 / math.log(10.0)) * np.log1p(ratio), "dBm"
        elif operation in {"subtract_power", "subtract_power_signed"}:
            high = np.maximum(sig, ref)
            # expm1 preserves small residuals between nearly equal inputs.
            fraction = -np.expm1(-np.abs(sig - ref) * (math.log(10.0) / 10.0))
            residual_dbm = high + 10.0 * np.log10(fraction)
            if operation == "subtract_power":
                res, unit = np.where(sig > ref, residual_dbm, np.nan), "dBm"
            else:
                # dBm -> W: subtract 30 dB before exponentiation.
                res = np.sign(sig - ref) * np.power(10.0, (residual_dbm - 30.0) / 10.0)
                res = np.where(sig == ref, 0.0, res)
                unit = "W"
        else:
            raise ValueError(f"Unsupported reference operation: {operation}.")
    valid = np.isfinite(res)
    if operation == "subtract_power":
        valid |= (sig <= ref) & np.isnan(res)
    elif operation in {"ratio_linear", "multiply_linear"}:
        valid &= res > 0
    elif operation == "subtract_power_signed":
        valid &= (res != 0) | (sig == ref)
    if not np.all(valid):
        raise ValueError(f"Reference operation {operation} exceeds the numeric range of {unit}.")
    return tuple(res.tolist()), unit


def frequency_grids_match(left: Sequence[float], right: Sequence[float], *, relative: float = 1e-9) -> bool:
    if len(left) != len(right) or len(left) < 2:
        return False
    return all(
        math.isclose(a, b, rel_tol=relative, abs_tol=max(abs(a), abs(b), 1.0) * 1e-12)
        for a, b in zip(left, right, strict=True)
    )


def peak_preserving_indices(values: Sequence[float], max_points: int) -> tuple[int, ...]:
    """Return ordered indices that retain extrema instead of every Nth sample."""

    count = len(values)
    if max_points <= 0 or count <= max_points:
        return tuple(range(count))
    if max_points < 3:
        return (0, count - 1)[:max_points]
    interior = count - 2
    bucket_count = max(1, (max_points - 2) // 2)
    selected = {0, count - 1}
    for bucket in range(bucket_count):
        start = 1 + interior * bucket // bucket_count
        stop = 1 + interior * (bucket + 1) // bucket_count
        candidates = [index for index in range(start, stop) if math.isfinite(values[index])]
        if not candidates:
            continue
        selected.add(min(candidates, key=values.__getitem__))
        selected.add(max(candidates, key=values.__getitem__))
    ordered = sorted(selected)
    if len(ordered) > max_points:
        # Preserve endpoints and the globally strongest extrema when an odd
        # point budget leaves one slot fewer than a complete min/max pair.
        interior_indices = ordered[1:-1]
        center = sum(values[index] for index in interior_indices if math.isfinite(values[index])) / max(
            1, sum(math.isfinite(values[index]) for index in interior_indices)
        )
        interior_indices.sort(key=lambda index: abs(values[index] - center), reverse=True)
        ordered = sorted([0, *interior_indices[: max_points - 2], count - 1])
    return tuple(ordered)
