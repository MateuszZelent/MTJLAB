"""Numerically explicit spectrum averaging and reference mathematics."""

from __future__ import annotations

import math
from collections.abc import Sequence


class LinearPowerAverager:
    """Streaming dBm averager retaining one accumulator instead of all traces."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._sum_mw: list[float] = []
        self.count = 0

    def add(self, trace_dbm: Sequence[float]) -> int:
        if len(trace_dbm) < 2:
            raise ValueError("A spectrum must contain at least two points.")
        if not all(math.isfinite(value) for value in trace_dbm):
            raise ValueError("A spectrum contains NaN or infinity.")
        linear = [_dbm_to_mw(value) for value in trace_dbm]
        if not self._sum_mw:
            self._sum_mw = linear
        elif len(linear) != len(self._sum_mw):
            raise ValueError("All spectra must contain the same number of points.")
        else:
            for index, value in enumerate(linear):
                self._sum_mw[index] += value
        self.count += 1
        return self.count

    def result(self) -> tuple[float, ...]:
        if self.count == 0:
            raise ValueError("At least one spectrum is required for averaging.")
        return tuple(_mw_to_dbm(value / self.count) for value in self._sum_mw)


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

    if len(signal_dbm) != len(reference_dbm) or len(signal_dbm) < 2:
        raise ValueError("Signal and reference spectra must have identical point counts.")
    if not all(math.isfinite(value) for value in (*signal_dbm, *reference_dbm)):
        raise ValueError("Signal and reference spectra must contain finite values.")
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
        return tuple(
            _mw_to_dbm(signal - reference) if signal > reference else math.nan
            for signal, reference in zip(signal_mw, reference_mw, strict=True)
        ), "dBm"
    if operation == "multiply_linear":
        return tuple(signal * reference for signal, reference in zip(signal_mw, reference_mw, strict=True)), "mW²"
    raise ValueError(f"Unsupported reference operation: {operation}.")


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
