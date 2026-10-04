"""Bounded acquisition schedule; no timers, arrays, hardware or state inference."""

from enum import StrEnum

from app.domain.spectrum_correction import SpectrumFrameEnvelope, SpectrumFrameRole
from app.domain.spectrum_interleaved import InterleavedSpectrumConfig, SpectrumOperatorStateConfirmation


class InterleavedPhase(StrEnum):
    WAIT_REFERENCE = "wait_reference"
    REFERENCE = "reference"
    FINISH_REFERENCE = "finish_reference"
    WAIT_SIGNAL = "wait_signal"
    SIGNAL = "signal"
    COMPLETE = "complete"


class InterleavedSpectrumAcquisition:
    def __init__(self, config: InterleavedSpectrumConfig):
        if not isinstance(config, InterleavedSpectrumConfig):
            raise ValueError("Interleaved acquisition requires a typed configuration.")
        self.config = config
        self.phase = InterleavedPhase.WAIT_REFERENCE
        self.reference_blocks = 0
        self.block_index = 0
        self.count = 0
        self.elapsed_s = 0.0
        self._first_time_s = None
        self._last = None

    @property
    def waiting_role(self):
        return {InterleavedPhase.WAIT_REFERENCE: SpectrumFrameRole.REFERENCE,
                InterleavedPhase.WAIT_SIGNAL: SpectrumFrameRole.SIGNAL}.get(self.phase)

    def confirm_state(self, confirmation: SpectrumOperatorStateConfirmation):
        if not isinstance(confirmation, SpectrumOperatorStateConfirmation) or confirmation.role != self.waiting_role:
            raise ValueError("Confirm the required state only after the previous block has committed.")
        self.phase = (InterleavedPhase.REFERENCE if confirmation.role == SpectrumFrameRole.REFERENCE
                      else InterleavedPhase.SIGNAL)
        self.block_index += 1
        self.count = 0
        self.elapsed_s = 0.0
        self._first_time_s = None

    def record_committed(self, envelope: SpectrumFrameEnvelope) -> bool:
        """Advance only after raw storage and processor acceptance succeeded."""
        if self.phase not in (InterleavedPhase.REFERENCE, InterleavedPhase.SIGNAL) or (
                not isinstance(envelope, SpectrumFrameEnvelope) or not envelope.complete
                or envelope.role.value != self.phase.value):
            raise ValueError("Only a complete committed sweep in the active block advances the schedule.")
        if self._last is not None and (envelope.frame_id <= self._last.frame_id
                or envelope.acquired_at_s <= self._last.acquired_at_s):
            raise ValueError("Interleaved sweep identities and acquisition times must increase.")
        if self._last is not None and (envelope.context_id != self._last.context_id
                or envelope.configuration_generation != self._last.configuration_generation
                or (self.count and envelope.segment_id != self._last.segment_id)):
            raise ValueError("Interleaved context must remain fixed; segment changes require a block boundary.")
        if self._first_time_s is None:
            self._first_time_s = envelope.started_at_s if envelope.started_at_s is not None else envelope.acquired_at_s
        self._last = envelope
        self.count += 1
        self.elapsed_s = envelope.acquired_at_s - self._first_time_s
        reference = self.phase == InterleavedPhase.REFERENCE
        duration = self.config.reference_duration_s if reference else self.config.signal_duration_s
        minimum = self.config.minimum_reference_sweeps if reference else 1
        done = self.count >= minimum and self.elapsed_s >= duration
        if done:
            self.phase = InterleavedPhase.FINISH_REFERENCE if reference else InterleavedPhase.WAIT_REFERENCE
        return done

    def reference_committed(self):
        if self.phase != InterleavedPhase.FINISH_REFERENCE:
            raise ValueError("Complete the REF block before publishing its background profile.")
        self.reference_blocks += 1
        self.phase = (InterleavedPhase.COMPLETE if self.reference_blocks >= self.config.maximum_reference_blocks
                      else InterleavedPhase.WAIT_SIGNAL)

    def reference_expired(self):
        if self.phase != InterleavedPhase.SIGNAL:
            raise ValueError("Only an active SIGNAL block can request a fresh reference.")
        self.phase = InterleavedPhase.WAIT_REFERENCE
