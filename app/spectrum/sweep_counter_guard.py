"""Constant-memory instrument counter checks across mixed completion evidence."""

from app.domain.spectrum_correction import SpectrumFrameEnvelope, SweepEvidence


class SweepCounterGuard:
    """Host SINGLE tokens are not numeric device counters.

    A host-confirmed sweep does not erase the last instrument counter. A
    counter reset requires a new explicitly identified acquisition segment.
    Call record only after checking the frame's other acceptance conditions.
    """

    __slots__ = ("_segment", "_counter")

    def __init__(self):
        self._segment: str | None = None
        self._counter: int | None = None

    def allows(self, envelope: SpectrumFrameEnvelope) -> bool:
        if not envelope.complete:
            return False
        if envelope.evidence != SweepEvidence.INSTRUMENT_COUNTER:
            return True
        return (envelope.segment_id != self._segment or self._counter is None
                or int(envelope.sweep_id) > self._counter)

    def record(self, envelope: SpectrumFrameEnvelope):
        if not self.allows(envelope):
            raise ValueError("Instrument sweep counter must increase within its segment.")
        if envelope.segment_id != self._segment:
            self._segment, self._counter = envelope.segment_id, None
        if envelope.evidence == SweepEvidence.INSTRUMENT_COUNTER:
            self._counter = int(envelope.sweep_id)
