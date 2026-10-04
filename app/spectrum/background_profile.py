"""Build a background exclusively from identified, completed reference sweeps."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from numpy.typing import ArrayLike

from app.domain.spectrum_correction import (
    BackgroundProfile,
    SpectrumAcquisitionContext,
    SpectrumFrameEnvelope,
    SpectrumFrameRole,
)
from .streaming_statistics import VectorWelford, finite_vector
from .sweep_counter_guard import SweepCounterGuard


class BackgroundProfileBuilder:
    def __init__(
        self,
        context: SpectrumAcquisitionContext,
        *,
        reference_state: str,
        minimum_sweeps: int = 30,
        signal_free_qualified: bool = False,
    ) -> None:
        if not reference_state.strip() or minimum_sweeps < 2:
            raise ValueError("Calibration requires a reference description and at least two sweeps.")
        self.context = context
        self.context_id = context.context_id
        self.reference_state = reference_state
        self.minimum_sweeps = minimum_sweeps
        self.signal_free_qualified = signal_free_qualified
        self.statistics = VectorWelford(context.frequencies_hz.size)
        self.first: SpectrumFrameEnvelope | None = None
        self.last: SpectrumFrameEnvelope | None = None
        self._counter_guard = SweepCounterGuard()

    def add(self, envelope: SpectrumFrameEnvelope, powers_w: ArrayLike) -> None:
        if envelope.role != SpectrumFrameRole.REFERENCE or not envelope.complete:
            raise ValueError("Only completed reference sweeps can train the background.")
        if (
            envelope.context_id != self.context_id
            or envelope.configuration_generation != self.context.configuration_generation
        ):
            raise ValueError("Reference acquisition context changed during calibration.")
        if self.last is not None and (
            envelope.frame_id <= self.last.frame_id
            or envelope.acquired_at_s <= self.last.acquired_at_s
            or envelope.segment_id != self.last.segment_id
        ):
            raise ValueError("Reference frames must be ordered within one segment.")
        if not self._counter_guard.allows(envelope):
            raise ValueError("Instrument sweep counter must increase within a calibration.")
        values = finite_vector(powers_w, size=self.context.frequencies_hz.size)
        if np.any(values <= 0):
            raise ValueError("Acquired reference power must be positive watts.")
        self.statistics.add(values)
        self._counter_guard.record(envelope)
        if self.first is None:
            self.first = envelope
        self.last = envelope

    def finish(self) -> BackgroundProfile:
        if (
            self.statistics.count < self.minimum_sweeps
            or self.first is None or self.last is None
        ):
            raise ValueError("Reference calibration has insufficient completed sweeps.")
        variance = self.statistics.variance()
        assert variance is not None
        # A protocol completion flag does not establish independence. This
        # estimate is available only when the acquisition context has separate
        # experimental evidence for independent sweeps.
        mean_variance = (
            variance / self.statistics.count
            if self.context.independent_sweeps_qualified else None
        )
        profile = BackgroundProfile(
            profile_id="pending",
            context_id=self.context_id,
            mean_w=self.statistics.mean,
            sample_variance_w2=variance,
            mean_variance_w2=mean_variance,
            sweep_count=self.statistics.count,
            started_at_s=(
                self.first.started_at_s
                if self.first.started_at_s is not None else self.first.acquired_at_s
            ),
            completed_at_s=self.last.acquired_at_s,
            reference_state=self.reference_state,
            signal_free_qualified=self.signal_free_qualified,
        )
        return replace(profile, profile_id=profile.content_hash)
