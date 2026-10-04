"""Explicit CPU processing changes; these requests never command hardware."""

from dataclasses import dataclass
from enum import StrEnum

from .spectrum_correction import BackgroundProfile
from .spectrum_interference import SpectrumInterferenceCalibration


class SpectrumDecisionOperation(StrEnum):
    SET_PROFILE = "set_profile"
    BEGIN_REFERENCE = "begin_reference"
    CANCEL_REFERENCE = "cancel_reference"
    RESET_SEGMENT = "reset_segment"
    SET_MODEL = "set_model"


@dataclass(frozen=True, slots=True)
class SpectrumProcessingChange:
    operation: SpectrumDecisionOperation
    profile: BackgroundProfile | None = None
    reference_state: str | None = None
    signal_free_qualified: bool = False
    calibration: SpectrumInterferenceCalibration | None = None

    def __post_init__(self):
        if not isinstance(self.operation, SpectrumDecisionOperation) or type(self.signal_free_qualified) is not bool:
            raise ValueError("Processing changes require a known operation and an explicit qualification boolean.")
        if self.operation == SpectrumDecisionOperation.SET_PROFILE:
            if not isinstance(self.profile, BackgroundProfile):
                raise ValueError("Profile change requires an immutable background profile.")
        elif self.profile is not None:
            raise ValueError("This processing operation cannot carry a background profile.")
        if self.operation == SpectrumDecisionOperation.SET_MODEL:
            if not isinstance(self.calibration, SpectrumInterferenceCalibration):
                raise ValueError("Model change requires an immutable calibration.")
        elif self.calibration is not None:
            raise ValueError("This processing operation cannot carry a model calibration.")
        if self.operation == SpectrumDecisionOperation.BEGIN_REFERENCE:
            if type(self.reference_state) is not str or not self.reference_state.strip() or len(self.reference_state) > 32768:
                raise ValueError("Reference acquisition requires a bounded explicit state description.")
        elif self.reference_state is not None or self.signal_free_qualified:
            raise ValueError("This processing operation cannot declare a signal-free reference state.")
