"""Bounded offline power-stability diagnostics; never a qualification or TTL."""

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np

from app.domain.spectrum_correction import (
    SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole,
)
from .streaming_statistics import dbm_to_w
from .background_profile import BackgroundProfileBuilder
from .sweep_counter_guard import SweepCounterGuard


def _immutable(values, dtype=np.float64):
    array = np.asarray(values, dtype=dtype)
    return np.frombuffer(array.tobytes(), dtype=dtype).reshape(array.shape)


@dataclass(frozen=True, slots=True)
class ReferenceDiagnosticConfig:
    block_duration_s: float = 1.0
    maximum_blocks: int = 4096
    maximum_bins: int = 16
    maximum_lag: int = 128
    minimum_blocks: int = 16
    relative_cadence_tolerance: float = .1

    def __post_init__(self):
        if not math.isfinite(self.block_duration_s) or self.block_duration_s <= 0:
            raise ValueError("Diagnostic block duration must be positive seconds.")
        for name, lower, upper in (("maximum_blocks", 16, 16384), ("maximum_bins", 1, 32),
                                   ("maximum_lag", 1, 256), ("minimum_blocks", 4, 16384)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"Diagnostic {name} is outside its bounded integer range.")
        if self.minimum_blocks > self.maximum_blocks:
            raise ValueError("Diagnostic minimum exceeds the block limit.")
        if not math.isfinite(self.relative_cadence_tolerance) or not 0 <= self.relative_cadence_tolerance <= .5:
            raise ValueError("Diagnostic cadence tolerance must be in 0..0.5.")


@dataclass(frozen=True, slots=True)
class ReferenceDiagnostics:
    context_id: str
    segment_id: str
    bin_indices: tuple[int, ...]
    frequencies_hz: np.ndarray
    block_indices: np.ndarray
    block_times_s: np.ndarray
    block_counts: np.ndarray
    block_mean_w: np.ndarray
    source_frame_ranges: np.ndarray
    total_sweeps: int
    discarded_tail_sweeps: int
    cadence_valid: bool
    issues: tuple[str, ...]
    allan_tau_s: np.ndarray
    allan_pair_counts: np.ndarray
    allan_variance_w2: np.ndarray | None
    correlation_lag_s: np.ndarray
    autocorrelation: np.ndarray | None
    correlation_valid_bins: np.ndarray
    algorithm_version: str = "reference-power-diagnostics-v1"

    def __post_init__(self):
        integers = {"block_indices", "block_counts", "source_frame_ranges", "allan_pair_counts"}
        for name in ("frequencies_hz", "block_indices", "block_times_s", "block_counts", "block_mean_w",
                     "source_frame_ranges", "allan_tau_s", "allan_pair_counts", "allan_variance_w2",
                     "correlation_lag_s", "autocorrelation", "correlation_valid_bins"):
            value = getattr(self, name)
            if value is not None:
                dtype = np.bool_ if name == "correlation_valid_bins" else (np.int64 if name in integers else np.float64)
                array = np.asarray(value, dtype=dtype)
                if not np.isfinite(array).all():
                    raise ValueError("Reference diagnostics must contain finite arrays.")
                object.__setattr__(self, name, _immutable(array, dtype))
        n, k = len(self.block_indices), len(self.bin_indices)
        expected = {"frequencies_hz": (k,), "block_indices": (n,), "block_times_s": (n,),
                    "block_counts": (n,), "block_mean_w": (n, k), "source_frame_ranges": (n, 2),
                    "allan_tau_s": (len(self.allan_tau_s),),
                    "allan_pair_counts": (len(self.allan_tau_s),),
                    "correlation_lag_s": (len(self.correlation_lag_s),), "correlation_valid_bins": (k,)}
        if any(getattr(self, name).shape != shape for name, shape in expected.items()):
            raise ValueError("Reference diagnostic array shapes disagree.")
        for name, shape in (("allan_variance_w2", (len(self.allan_tau_s), k)),
                            ("autocorrelation", (len(self.correlation_lag_s), k))):
            value = getattr(self, name)
            if (value is None and shape[0] != 0) or (value is not None and value.shape != shape):
                raise ValueError("Reference diagnostic statistic shapes disagree.")
        if n < 1 or k < 1 or np.any(self.block_counts < 1) or self.discarded_tail_sweeps < 1 or (
            int(self.block_counts.sum()) + self.discarded_tail_sweeps != self.total_sweeps
        ):
            raise ValueError("Reference diagnostic sweep counts disagree.")
        if np.any(self.block_mean_w <= 0) or (
            self.allan_variance_w2 is not None and np.any(self.allan_variance_w2 < 0)
        ):
            raise ValueError("Reference diagnostic power and variance signs are invalid.")


def overlapping_allan_variance(block_mean_w, factors, *, cancellation_check=None):
    """Half the mean squared difference of adjacent m-block means (W²).

    All possible starting offsets are used, not independent pair counts.
    This is the overlapping two-sample statistic applied to power samples;
    it is not fractional-frequency stability or a confidence interval.
    See NIST SP1065 for the adjacent-average construction.
    """
    data = np.asarray(block_mean_w, dtype=np.float64)
    if data.ndim != 2 or len(data) < 2 or data.shape[1] < 1 or not np.isfinite(data).all():
        raise ValueError("Allan input requires a finite time-by-bin matrix.")
    factors = tuple(factors)
    if not factors or any(type(m) is not int or m < 1 or 2 * m > len(data) for m in factors):
        raise ValueError("Allan averaging factors must fit two adjacent blocks.")
    # Subtract a common level before accumulation to preserve tiny variations
    # around a much larger power baseline. Do not remove a drift or trend.
    prefix = np.vstack((np.zeros((1, data.shape[1])), np.cumsum(data - data[0], axis=0)))
    values, counts = [], []
    for m in factors:
        if cancellation_check is not None:
            cancellation_check()
        averages = (prefix[m:] - prefix[:-m]) / m
        difference = averages[m:] - averages[:-m]
        variance = .5 * np.mean(difference * difference, axis=0)
        if not np.isfinite(variance).all():
            raise ValueError("Allan power variance overflowed.")
        values.append(variance)
        counts.append(len(difference))
    return np.asarray(values), np.asarray(counts, dtype=np.int64)


def diagnose_reference(
    context: SpectrumAcquisitionContext,
    frames: Iterable[tuple[SpectrumFrameEnvelope, np.ndarray]],
    bin_indices: tuple[int, ...],
    config: ReferenceDiagnosticConfig = ReferenceDiagnosticConfig(),
    *, cancellation_check=None, expected_profile=None,
) -> ReferenceDiagnostics:
    """Read each raw once; retain bounded time-block summaries, not F×history.

    Final partial time block is explicitly discarded. Missing time buckets
    and irregular effective block times suppress Allan/ACF rather than being
    interpolated. Correlation concerns block means, never proves independent
    individual sweeps. Selected bins are diagnostic probes, not filter masks.
    """
    bins = tuple(bin_indices)
    if not bins or len(bins) > config.maximum_bins or len(set(bins)) != len(bins) or any(
        type(index) is not int or not 0 <= index < context.frequencies_hz.size for index in bins
    ):
        raise ValueError("Choose unique, in-range diagnostic bins within the resource limit.")
    builder = None
    if expected_profile is not None:
        if expected_profile.context_id != context.context_id:
            raise ValueError("Diagnostic reference profile differs from its acquisition context.")
        builder = BackgroundProfileBuilder(context, reference_state=expected_profile.reference_state,
            minimum_sweeps=2, signal_free_qualified=expected_profile.signal_free_qualified)
    means, times, counts, buckets, ranges = [], [], [], [], []
    origin = None
    last = first = None
    current_bucket, count, total = 0, 0, 0
    mean = np.zeros(len(bins))
    mean_time = 0.0
    block_first_id = 0
    counter_guard = SweepCounterGuard()

    def close_block():
        if len(means) >= config.maximum_blocks:
            raise ValueError("Reference diagnostic block limit reached; choose a larger block duration.")
        means.append(mean.copy())
        times.append(mean_time)
        counts.append(count)
        buckets.append(current_bucket)
        ranges.append((block_first_id, last.frame_id))

    for envelope, dbm in frames:
        if cancellation_check is not None:
            cancellation_check()
        if not envelope.complete or envelope.role != SpectrumFrameRole.REFERENCE or (
            envelope.context_id != context.context_id
            or envelope.configuration_generation != context.configuration_generation
        ):
            raise ValueError("Reference diagnostics require complete compatible REF sweeps.")
        if last is not None and (
            envelope.segment_id != last.segment_id or envelope.frame_id <= last.frame_id
            or envelope.acquired_at_s <= last.acquired_at_s
        ):
            raise ValueError("Reference diagnostics cannot join segments or reordered/repeated sweeps.")
        if not counter_guard.allows(envelope):
            raise ValueError("Instrument sweep counter must increase during diagnostics.")
        linear = dbm_to_w(dbm)
        if linear.shape != context.frequencies_hz.shape:
            raise ValueError("Diagnostic spectrum axis changed.")
        if builder is not None:
            builder.add(envelope, linear)
        selected = linear[list(bins)]
        if origin is None:
            origin = envelope.acquired_at_s
            first = envelope
            block_first_id = envelope.frame_id
        # Epoch timestamps lose fractional precision. Resolve a boundary within
        # two representable clock steps consistently, without manufacturing gaps
        # for e.g. 100 ms buckets near a modern Unix epoch.
        resolution = max(math.ulp(origin), math.ulp(envelope.acquired_at_s))
        if config.block_duration_s <= 16 * resolution:
            raise ValueError("Diagnostic block duration is too small for the timestamp resolution.")
        bucket = math.floor(((envelope.acquired_at_s - origin) + 2 * resolution)
                            / config.block_duration_s)
        if bucket != current_bucket:
            close_block()
            current_bucket, count = bucket, 0
            mean.fill(0)
            mean_time = 0.0
            block_first_id = envelope.frame_id
        count += 1
        mean += (selected - mean) / count
        mean_time += ((envelope.acquired_at_s - origin) - mean_time) / count
        last = envelope
        counter_guard.record(envelope)
        total += 1
    if first is None or not means:
        raise ValueError("Reference diagnostics require at least one completed time block.")
    if builder is not None and builder.finish().content_hash != expected_profile.content_hash:
        raise ValueError("Reference diagnostic raw does not reproduce its committed profile.")
    data = np.asarray(means)
    relative_times = np.asarray(times)
    issues = []
    regular = bool(np.all(np.diff(buckets) == 1))
    if not regular:
        issues.append("missing_time_blocks")
    if len(times) > 1 and np.any(np.abs(np.diff(relative_times) / config.block_duration_s - 1)
                                 > config.relative_cadence_tolerance):
        regular = False
        issues.append("irregular_effective_cadence")
    enough = len(means) >= config.minimum_blocks
    if not enough:
        issues.append("insufficient_blocks")
    allan = correlation = None
    tau, pairs, lags = np.empty(0), np.empty(0, dtype=np.int64), np.empty(0)
    valid_bins = np.zeros(len(bins), dtype=bool)
    if regular and enough:
        factors = [2 ** exponent for exponent in range(len(means).bit_length()) if 2 ** exponent <= len(means) // 4]
        allan, pairs = overlapping_allan_variance(data, factors, cancellation_check=cancellation_check)
        tau = np.asarray(factors) * config.block_duration_s
        centered = data - data[0]
        centered -= centered.mean(axis=0)
        denominator = np.sum(centered * centered, axis=0)
        valid_bins = (np.ptp(data, axis=0) > 0) & (denominator > 0)
        lag_count = min(config.maximum_lag, len(data) // 4)
        correlation = np.zeros((lag_count + 1, len(bins)))
        for lag in range(lag_count + 1):
            if cancellation_check is not None:
                cancellation_check()
            product = centered if lag == 0 else centered[:-lag]
            other = centered if lag == 0 else centered[lag:]
            correlation[lag, valid_bins] = np.sum(product * other, axis=0)[valid_bins] / denominator[valid_bins]
        lags = np.arange(lag_count + 1) * config.block_duration_s
    return ReferenceDiagnostics(
        context.context_id, first.segment_id, bins, context.frequencies_hz[list(bins)],
        np.asarray(buckets), relative_times + origin, np.asarray(counts), data, np.asarray(ranges),
        total, count, regular, tuple(issues), tau, pairs, allan, lags, correlation, valid_bins,
    )
