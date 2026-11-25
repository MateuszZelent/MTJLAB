"""O(F) SI power conversion and streaming statistics, with bounded storage."""

from __future__ import annotations

import math
from collections import deque

import numpy as np
from numpy.typing import ArrayLike

from app.domain.spectrum_correction import FloatVector, TemporalAverageMode, positive_time


def finite_vector(values: ArrayLike, *, size: int | None = None) -> FloatVector:
    vector = np.asarray(values, dtype=np.float64)
    if vector.ndim != 1 or vector.size < 2 or not np.all(np.isfinite(vector)):
        raise ValueError("Spectrum must be a finite one-dimensional vector with at least two points.")
    if size is not None and vector.size != size:
        raise ValueError("Spectrum point count changed.")
    return vector


def dbm_to_w(values_dbm: ArrayLike, *, out: FloatVector | None = None) -> FloatVector:
    values = finite_vector(values_dbm)
    if out is None:
        out = np.empty_like(values)
    elif out.shape != values.shape or out.dtype != np.float64:
        raise ValueError("Conversion buffer must be float64 and match the input.")
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        np.subtract(values, 30.0, out=out)
        np.divide(out, 10.0, out=out)
        np.power(10.0, out, out=out)
    if not np.all(np.isfinite(out)) or np.any(out <= 0):
        raise ValueError("dBm cannot be represented as positive finite float64 watts.")
    return out


class RollingPowerAverage:
    """Equal weight per received spectrum in a trailing frame or time window."""

    MEMORY_LIMIT_BYTES = 256 * 1024 * 1024

    def __init__(self, frames: int, seconds: float | None) -> None:
        if seconds is not None:
            positive_time(seconds, "window seconds")
        elif frames < 1:
            raise ValueError("The rolling frame window must be positive.")
        self.frames = frames
        self.seconds = seconds
        self._rows: deque[tuple[float, FloatVector]] = deque()
        self._sum: FloatVector | None = None
        self._updates = 0

    @property
    def count(self) -> int:
        return len(self._rows)

    @property
    def span_s(self) -> float:
        return self._rows[-1][0] - self._rows[0][0] if self._rows else 0.0

    def clear(self) -> None:
        self._rows.clear()
        self._sum = None
        self._updates = 0

    def add(self, values_dbm: ArrayLike, at_s: float) -> FloatVector:
        if not math.isfinite(at_s) or (self._rows and at_s < self._rows[-1][0]):
            raise ValueError("Rolling average requires monotonic receive times.")
        power = dbm_to_w(values_dbm)
        if self._sum is None:
            self._sum = np.zeros_like(power)
        elif self._sum.shape != power.shape:
            raise ValueError("Rolling average spectrum point count changed.")

        max_frames = self.MEMORY_LIMIT_BYTES // power.nbytes - 1  # Reserve the running sum.
        if self.seconds is None and self.frames > max_frames:
            raise ValueError("The frame window exceeds the 256 MiB Live average buffer. Choose fewer frames.")

        cutoff = at_s - self.seconds if self.seconds is not None else -math.inf
        rebuild_sum = False
        while self._rows and (self._rows[0][0] <= cutoff
                              or (self.seconds is None and self.count >= self.frames)):
            _, dropped = self._rows.popleft()
            self._sum -= dropped
            # Subtracting a much larger outgoing sample can erase the quieter
            # samples that are still present. Rebuild from their stored powers.
            rebuild_sum |= bool(np.any(self._sum < dropped * 1e-6))
        if self.count >= max_frames:
            raise ValueError("The time window exceeds the 256 MiB Live average buffer. Choose a shorter duration.")
        self._rows.append((at_s, power))
        self._updates += 1
        with np.errstate(over="ignore"):
            if rebuild_sum or self._updates % 1024 == 0:
                self._sum.fill(0)
                for _, row in self._rows:
                    self._sum += row
            else:
                self._sum += power
        if not np.all(np.isfinite(self._sum)):
            self.clear()
            raise ValueError("Accumulated spectrum power exceeds the numeric W range.")
        return 10.0 * (np.log10(self._sum) - math.log10(self.count)) + 30.0


class VectorWelford:
    """Unweighted moments; independence is deliberately not inferred here."""

    def __init__(self, size: int) -> None:
        if size < 2:
            raise ValueError("Statistics require at least two frequency points.")
        self.count = 0
        self.mean = np.zeros(size, dtype=np.float64)
        self.m2 = np.zeros(size, dtype=np.float64)
        self._delta = np.empty(size, dtype=np.float64)
        self._work = np.empty(size, dtype=np.float64)

    def add(self, values: ArrayLike) -> None:
        values = finite_vector(values, size=self.mean.size)
        self.count += 1
        np.subtract(values, self.mean, out=self._delta)
        np.divide(self._delta, self.count, out=self._work)
        np.add(self.mean, self._work, out=self.mean)
        np.subtract(values, self.mean, out=self._work)
        np.multiply(self._delta, self._work, out=self._work)
        np.add(self.m2, self._work, out=self.m2)

    def variance(self) -> FloatVector | None:
        if self.count < 2:
            return None
        return np.maximum(self.m2 / (self.count - 1), 0.0)


class TemporalPowerAverage:
    """Signed-power averaging and reference coefficients on identical weights.

    Reference IDs carry scalar weights so their common estimation error does
    not spuriously shrink as each signal frame is accumulated. Switching a
    reference in an unlimited BLOCK closes/reset the block in the processor.
    """

    def __init__(
        self, size: int, mode: TemporalAverageMode, *, tau_s: float, window_frames: int
    ) -> None:
        positive_time(tau_s, "tau_s")
        self.mode = TemporalAverageMode(mode)
        if window_frames < 1:
            raise ValueError("Window frame count must be positive.")
        self.tau_s = tau_s
        self.window_frames = window_frames
        self.moments = VectorWelford(size)
        self.mean = np.zeros(size, dtype=np.float64)
        self.count = 0
        self.first_time_s: float | None = None
        self.last_time_s: float | None = None
        self.reference_weights: dict[str, float] = {}
        self.weight_square_sum = 0.0
        self._ring: deque[tuple[FloatVector, str, float]] = deque()
        self._sum = np.zeros(size, dtype=np.float64)
        self._work = np.empty(size, dtype=np.float64)
        self._updates = 0

    def add(self, residual_w: ArrayLike, at_s: float, profile_id: str) -> None:
        values = finite_vector(residual_w, size=self.mean.size)
        if not math.isfinite(at_s) or not profile_id:
            raise ValueError("Average requires a finite time and profile ID.")
        if self.last_time_s is not None and at_s <= self.last_time_s:
            raise ValueError("Signal acquisition times must be strictly increasing.")
        if self.mode == TemporalAverageMode.BLOCK:
            self.moments.add(values)
            self.mean[:] = self.moments.mean
            previous = self.count
            self.count += 1
            self._update_weights(previous / self.count, 1 / self.count, profile_id)
            self.weight_square_sum = 1 / self.count
        elif self.mode == TemporalAverageMode.EMA_PREVIEW:
            alpha = (
                1.0 if self.last_time_s is None
                else -math.expm1(-(at_s - self.last_time_s) / self.tau_s)
            )
            np.subtract(values, self.mean, out=self._work)
            np.multiply(self._work, alpha, out=self._work)
            np.add(self.mean, self._work, out=self.mean)
            self._update_weights(1 - alpha, alpha, profile_id)
            self.weight_square_sum = (1 - alpha) ** 2 * self.weight_square_sum + alpha**2
            self.count += 1
        else:
            rebuild_sum = False
            if len(self._ring) == self.window_frames:
                old, old_profile, _old_time = self._ring.popleft()
                self._sum -= old
                # Evicting a large or canceling sample from a signed running sum
                # can erase lower-magnitude residuals; rebuild from retained rows.
                rebuild_sum = bool(np.any(np.abs(old) > 0.5 * np.abs(self._sum)))
                self.reference_weights[old_profile] -= 1.0
                if self.reference_weights[old_profile] <= 0:
                    del self.reference_weights[old_profile]
            self._ring.append((values.copy(), profile_id, at_s))
            self.reference_weights[profile_id] = self.reference_weights.get(profile_id, 0) + 1
            self.count = len(self._ring)
            self._updates += 1
            if rebuild_sum or self._updates % 1024 == 0:
                self._sum.fill(0)
                for row, _profile, _at in self._ring:
                    self._sum += row
            else:
                self._sum += values
            np.divide(self._sum, self.count, out=self.mean)
            self.weight_square_sum = 1 / self.count
            self.first_time_s = self._ring[0][2]
        if self.first_time_s is None:
            self.first_time_s = at_s
        self.last_time_s = at_s

    def _update_weights(self, old_weight: float, new_weight: float, profile_id: str) -> None:
        # Do not discard small nonzero weights: that would silently remove a
        # shared-reference uncertainty contribution. Profiles are bounded by
        # resetting the processor's segment on refresh for EMA and BLOCK.
        for key in self.reference_weights:
            self.reference_weights[key] *= old_weight
        self.reference_weights[profile_id] = (
            self.reference_weights.get(profile_id, 0) + new_weight
        )

    def weights(self) -> tuple[tuple[str, float], ...]:
        scale = self.count if self.mode == TemporalAverageMode.WINDOW else 1
        return tuple(sorted((key, value / scale) for key, value in self.reference_weights.items()))

    def sample_mean_variance(self) -> FloatVector | None:
        """IID variance of the mean, only for an unweighted closed block.

        EMA/window CI needs explicit correlation and covariance qualification;
        callers must not treat a preview's scatter as a confidence interval.
        """
        if self.mode != TemporalAverageMode.BLOCK:
            return None
        variance = self.moments.variance()
        return None if variance is None else variance / self.count
