"""Delayed bracketed estimates with common-reference error propagated once."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np
from numpy.typing import ArrayLike

from app.domain.spectrum_correction import (
    BackgroundProfile, CorrectedSpectrumFrame, CorrectionQuality,
    SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole,
    TemporalAverageMode, immutable_vector,
)
from .reference_estimator import bracketed_reference
from .streaming_statistics import VectorWelford, dbm_to_w, finite_vector
from .sweep_counter_guard import SweepCounterGuard


@dataclass(frozen=True, slots=True)
class FinalizedSpectrumBlock:
    result: CorrectedSpectrumFrame
    source_frame_ids: tuple[int, ...]
    raw_mean_w: np.ndarray

    def __post_init__(self):
        ids = tuple(self.source_frame_ids)
        if not self.result.final or self.result.average_mode != TemporalAverageMode.BLOCK:
            raise ValueError("Finalized artifact requires a final measurement block.")
        if len(ids) != self.result.count or not ids or ids[-1] != self.result.frame_id:
            raise ValueError("Finalized sources must match the contributing frame count.")
        if any(first >= second for first, second in zip(ids, ids[1:])):
            raise ValueError("Finalized source frames must be strictly ordered.")
        raw = immutable_vector(self.raw_mean_w, name="raw block mean_w", nonnegative=True)
        if raw.shape != self.result.values_w.shape or np.any(raw <= 0):
            raise ValueError("Raw block mean must be positive and match the result axis.")
        object.__setattr__(self, "raw_mean_w", raw)
        object.__setattr__(self, "source_frame_ids", ids)


def finalize_bracketed_block(
    context: SpectrumAcquisitionContext,
    before: BackgroundProfile,
    after: BackgroundProfile,
    frames: Iterable[tuple[SpectrumFrameEnvelope, ArrayLike]],
    *,
    independent_blocks_qualified=False,
    interpolation_error_variance_w2: ArrayLike | None = None,
    maximum_gap_s=5.0,
    cancellation_check=None,
) -> FinalizedSpectrumBlock:
    """Calculate a new result without mutating provisional data or profiles.

    Each reference coefficient is averaged over the signal block before its
    variance is propagated. Reusing the same uncertain reference N times must
    not divide that reference error by N. An interpolation-error variance is
    a qualified error model for the complete block, in W², not a UI constant.
    Memory is O(F) for vectors plus O(N) integer source IDs; raw frames stream.
    """
    if not math.isfinite(maximum_gap_s) or maximum_gap_s <= 0:
        raise ValueError("Finalization gap limit must be positive seconds.")
    if before.context_id != context.context_id or after.context_id != context.context_id:
        raise ValueError("Finalization references must match the acquisition context.")
    # Validate the bracket even when the supplied signal iterable is empty.
    bracketed_reference(before, after, before.completed_at_s)
    size = context.frequencies_hz.size
    residual_stats, raw_stats = VectorWelford(size), VectorWelford(size)
    weights = np.zeros(2)
    source_ids = []
    first = last = None
    counter_guard = SweepCounterGuard()
    residual = np.empty(size)
    for envelope, dbm in frames:
        if cancellation_check is not None:
            cancellation_check()
        if (
            not envelope.complete or envelope.role != SpectrumFrameRole.SIGNAL
            or envelope.context_id != context.context_id
            or envelope.configuration_generation != context.configuration_generation
        ):
            raise ValueError("Finalization accepts only complete, compatible signal sweeps.")
        if last is not None and (
            envelope.segment_id != last.segment_id or envelope.frame_id <= last.frame_id
            or envelope.acquired_at_s <= last.acquired_at_s
            or envelope.acquired_at_s - last.acquired_at_s > maximum_gap_s
        ):
            raise ValueError("Finalization cannot cross a segment, reorder or acquisition gap.")
        if not counter_guard.allows(envelope):
            raise ValueError("Instrument sweep counter must increase during finalization.")
        estimate = bracketed_reference(before, after, envelope.acquired_at_s)
        linear = dbm_to_w(dbm)
        if linear.shape != (size,):
            raise ValueError("Finalization raw spectrum differs from the acquisition axis.")
        np.subtract(linear, estimate.mean_w, out=residual)
        residual_stats.add(residual)
        raw_stats.add(linear)
        weights += [weight for _, weight in estimate.profile_weights]
        source_ids.append(envelope.frame_id)
        first = envelope if first is None else first
        last = envelope
        counter_guard.record(envelope)
    if first is None or last is None:
        raise ValueError("Finalization requires a contributing signal block.")
    weights /= residual_stats.count
    drift = None
    if interpolation_error_variance_w2 is not None:
        drift = finite_vector(interpolation_error_variance_w2, size=size)
        if np.any(drift < 0):
            raise ValueError("Interpolation-error variance must be nonnegative W².")
    qualified = (
        context.settings_verified and context.independent_sweeps_qualified
        and before.signal_free_qualified and after.signal_free_qualified
        and independent_blocks_qualified and drift is not None
        and before.mean_variance_w2 is not None and after.mean_variance_w2 is not None
    )
    uncertainty = None
    signal_variance = residual_stats.variance()
    if qualified and signal_variance is not None:
        uncertainty = np.sqrt(
            signal_variance / residual_stats.count
            + weights[0] ** 2 * before.mean_variance_w2
            + weights[1] ** 2 * after.mean_variance_w2 + drift
        )
    result = CorrectedSpectrumFrame(
        last.frame_id, last.segment_id, context.context_id, 0, residual_stats.mean,
        uncertainty, residual_stats.count, first.acquired_at_s, last.acquired_at_s,
        last.acquired_at_s - before.completed_at_s,
        ((before.profile_id, float(weights[0])), (after.profile_id, float(weights[1]))),
        CorrectionQuality.READY if qualified and signal_variance is not None else CorrectionQuality.UNQUALIFIED,
        TemporalAverageMode.BLOCK, final=True, algorithm_version="bracketed-reference-v1",
    )
    return FinalizedSpectrumBlock(result, tuple(source_ids), raw_stats.mean)
