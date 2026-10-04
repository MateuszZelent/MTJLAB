"""Explicit timing and operator evidence for alternating REF/SIGNAL blocks."""

from dataclasses import dataclass
import math

from .spectrum_correction import SpectrumFrameRole


@dataclass(frozen=True, slots=True)
class InterleavedSpectrumConfig:
    reference_duration_s: float
    signal_duration_s: float
    minimum_reference_sweeps: int = 2
    maximum_reference_blocks: int = 256

    def __post_init__(self):
        for value in (self.reference_duration_s, self.signal_duration_s):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Interleaved block durations require positive finite seconds.")
        if (type(self.minimum_reference_sweeps) is not int
                or not 2 <= self.minimum_reference_sweeps <= 100_000):
            raise ValueError("Interleaved REF requires 2..100000 complete sweeps.")
        if (type(self.maximum_reference_blocks) is not int
                or not 2 <= self.maximum_reference_blocks <= 256):
            raise ValueError("An interleaved archive permits 2..256 reference blocks.")


@dataclass(frozen=True, slots=True)
class SpectrumOperatorStateConfirmation:
    """An operator report, never instrument readback or signal-free qualification."""

    role: SpectrumFrameRole
    description: str
    stable_state_confirmed: bool

    def __post_init__(self):
        if self.role not in (SpectrumFrameRole.REFERENCE, SpectrumFrameRole.SIGNAL) or not isinstance(
                self.role, SpectrumFrameRole):
            raise ValueError("Operator confirmation requires an explicit REF or SIGNAL role.")
        if type(self.description) is not str or not self.description.strip() or len(self.description) > 32768:
            raise ValueError("Operator confirmation requires a bounded state description.")
        if self.stable_state_confirmed is not True:
            raise ValueError("Confirm the stable state before acquiring a measurement block.")


@dataclass(frozen=True, slots=True)
class RecordedInterleavedSignalBlock:
    start_point: int
    stop_point: int
    segment_id: str
    before_profile_id: str
    after_profile_id: str | None
    started_at_s: float
    completed_at_s: float

    def __post_init__(self):
        if (type(self.start_point) is not int or type(self.stop_point) is not int
                or not 0 <= self.start_point <= self.stop_point):
            raise ValueError("Recorded blocks require ordered integer checkpoint boundaries.")
        if not self.segment_id or not self.before_profile_id or self.after_profile_id == "":
            raise ValueError("Recorded blocks require explicit segment and reference identities.")
        if (not math.isfinite(self.started_at_s) or not math.isfinite(self.completed_at_s)
                or self.started_at_s > self.completed_at_s):
            raise ValueError("Recorded blocks require ordered finite acquisition times.")
