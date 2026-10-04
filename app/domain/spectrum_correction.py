"""Device-independent, immutable contracts for quantitative spectrum correction.

Arrays published across threads are backed by immutable bytes, rather than a
writable NumPy owner with its WRITEABLE flag temporarily disabled. Powers are
SI watts; a signed residual is never an absolute logarithmic power.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatVector = NDArray[np.float64]


def immutable_vector(values: ArrayLike, *, name: str, nonnegative: bool = False) -> FloatVector:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1 or array.size < 2 or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite one-dimensional vector of at least two points.")
    if nonnegative and np.any(array < 0):
        raise ValueError(f"{name} must be nonnegative.")
    return np.frombuffer(array.tobytes(), dtype=np.float64)


def positive_time(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite time in seconds.")


class SpectrumFrameRole(StrEnum):
    SIGNAL = "signal"
    REFERENCE = "reference"
    TRANSITION = "transition"
    UNKNOWN = "unknown"


class SweepEvidence(StrEnum):
    UNKNOWN = "unknown"
    QUALIFIED_SINGLE_SWEEP = "qualified_single_sweep"
    INSTRUMENT_COUNTER = "instrument_counter"


class CorrectionQuality(StrEnum):
    RAW_ONLY = "raw_only"
    CALIBRATING = "calibrating"
    READY = "ready"
    UNQUALIFIED = "unqualified"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_ACQUISITION = "invalid_acquisition"
    INVALID_MODEL = "invalid_model"


class TemporalAverageMode(StrEnum):
    BLOCK = "block"
    WINDOW = "window"
    EMA_PREVIEW = "ema_preview"


@dataclass(frozen=True, slots=True, eq=False)
class SpectrumAcquisitionContext:
    """Verified acquisition settings and a shared, immutable frequency grid.

    configuration_fingerprint must identify *actual* detector, bandwidth,
    attenuation, preamp, sweep and averaging settings, not just UI requests.
    Independent sweep qualification is separate from completion evidence.
    """

    frequencies_hz: FloatVector
    configuration_fingerprint: str
    configuration_generation: int = 0
    settings_verified: bool = False
    independent_sweeps_qualified: bool = False
    context_id: str = field(init=False)

    def __post_init__(self) -> None:
        frequencies = immutable_vector(self.frequencies_hz, name="frequencies_hz")
        if np.any(frequencies < 0) or np.any(np.diff(frequencies) <= 0):
            raise ValueError("Spectrum frequency grid must be nonnegative and strictly increasing.")
        if not self.configuration_fingerprint or self.configuration_generation < 0:
            raise ValueError("Acquisition context requires a fingerprint and nonnegative generation.")
        object.__setattr__(self, "frequencies_hz", frequencies)
        digest = hashlib.sha256(frequencies.tobytes())
        digest.update(self.configuration_fingerprint.encode("utf-8"))
        object.__setattr__(self, "context_id", digest.hexdigest())


@dataclass(frozen=True, slots=True)
class SpectrumFrameEnvelope:
    frame_id: int
    segment_id: str
    context_id: str
    configuration_generation: int
    acquired_at_s: float
    role: SpectrumFrameRole = SpectrumFrameRole.SIGNAL
    evidence: SweepEvidence = SweepEvidence.UNKNOWN
    sweep_id: str | None = None
    started_at_s: float | None = None
    received_at_s: float | None = None

    def __post_init__(self) -> None:
        if self.frame_id < 0 or self.configuration_generation < 0:
            raise ValueError("Frame ID and generation must be nonnegative.")
        if not self.segment_id or not self.context_id:
            raise ValueError("Frame requires segment and context IDs.")
        object.__setattr__(self, "role", SpectrumFrameRole(self.role))
        object.__setattr__(self, "evidence", SweepEvidence(self.evidence))
        times = (self.acquired_at_s, self.started_at_s, self.received_at_s)
        if any(value is not None and not math.isfinite(value) for value in times):
            raise ValueError("Frame times must be finite seconds in one clock domain.")
        if self.started_at_s is not None and self.started_at_s > self.acquired_at_s:
            raise ValueError("Sweep start must precede completion.")
        if self.received_at_s is not None and self.received_at_s < self.acquired_at_s:
            raise ValueError("Receipt must not precede acquisition.")
        if self.evidence == SweepEvidence.INSTRUMENT_COUNTER and not self.sweep_id:
            raise ValueError("Instrument-counter evidence requires a sweep ID.")
        if self.evidence == SweepEvidence.INSTRUMENT_COUNTER and (
            not self.sweep_id.isascii() or not self.sweep_id.isdecimal()
        ):
            raise ValueError("Instrument sweep counter must be a nonnegative decimal integer.")

    @property
    def complete(self) -> bool:
        return self.evidence != SweepEvidence.UNKNOWN


@dataclass(frozen=True, slots=True)
class CorrectionConfig:
    average_mode: TemporalAverageMode = TemporalAverageMode.EMA_PREVIEW
    time_constant_s: float = 1.0
    window_frames: int = 32
    maximum_reference_age_s: float | None = None
    maximum_gap_s: float = 5.0
    minimum_reference_sweeps: int = 30
    working_memory_limit_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "average_mode", TemporalAverageMode(self.average_mode))
        positive_time(self.time_constant_s, "time_constant_s")
        positive_time(self.maximum_gap_s, "maximum_gap_s")
        if self.maximum_reference_age_s is not None:
            positive_time(self.maximum_reference_age_s, "maximum_reference_age_s")
        if self.window_frames < 1 or self.minimum_reference_sweeps < 2:
            raise ValueError("Window must contain at least one frame; reference at least two sweeps.")
        if self.working_memory_limit_bytes < 1024:
            raise ValueError("Working-memory limit is too small.")


@dataclass(frozen=True, slots=True, eq=False)
class BackgroundProfile:
    profile_id: str
    context_id: str
    mean_w: FloatVector
    sample_variance_w2: FloatVector
    mean_variance_w2: FloatVector | None
    sweep_count: int
    started_at_s: float
    completed_at_s: float
    reference_state: str
    signal_free_qualified: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.profile_id or not self.context_id:
            raise ValueError("Unsupported profile schema or missing identity.")
        mean = immutable_vector(self.mean_w, name="mean_w", nonnegative=True)
        variance = immutable_vector(
            self.sample_variance_w2, name="sample_variance_w2", nonnegative=True
        )
        mean_variance = self.mean_variance_w2
        if mean_variance is not None:
            mean_variance = immutable_vector(
                mean_variance, name="mean_variance_w2", nonnegative=True
            )
        if variance.shape != mean.shape or (
            mean_variance is not None and mean_variance.shape != mean.shape
        ):
            raise ValueError("Profile vectors must share one frequency grid.")
        if self.sweep_count < 2 or not self.reference_state.strip():
            raise ValueError("Profile requires at least two sweeps and a reference-state description.")
        if not all(math.isfinite(t) for t in (self.started_at_s, self.completed_at_s)):
            raise ValueError("Profile times must be finite.")
        if self.completed_at_s < self.started_at_s:
            raise ValueError("Reference completion must follow its start.")
        object.__setattr__(self, "mean_w", mean)
        object.__setattr__(self, "sample_variance_w2", variance)
        object.__setattr__(self, "mean_variance_w2", mean_variance)

    @property
    def representative_at_s(self) -> float:
        return (self.started_at_s + self.completed_at_s) / 2

    @property
    def content_hash(self) -> str:
        metadata = {
            "context_id": self.context_id,
            "sweep_count": self.sweep_count,
            "started_at_s": self.started_at_s,
            "completed_at_s": self.completed_at_s,
            "reference_state": self.reference_state,
            "signal_free_qualified": self.signal_free_qualified,
            "schema_version": self.schema_version,
            "mean_variance_known": self.mean_variance_w2 is not None,
        }
        digest = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode())
        for values in (self.mean_w, self.sample_variance_w2, self.mean_variance_w2):
            if values is not None:
                digest.update(values.tobytes())
        return digest.hexdigest()


@dataclass(frozen=True, slots=True, eq=False)
class ReferenceEstimate:
    mean_w: FloatVector
    variance_w2: FloatVector | None
    profile_weights: tuple[tuple[str, float], ...]
    age_s: float
    final: bool

    def __post_init__(self) -> None:
        mean = immutable_vector(self.mean_w, name="reference mean_w", nonnegative=True)
        variance = self.variance_w2
        if variance is not None:
            variance = immutable_vector(variance, name="reference variance_w2", nonnegative=True)
            if variance.shape != mean.shape:
                raise ValueError("Reference estimate variance must match its mean.")
        if not math.isfinite(self.age_s) or self.age_s < 0:
            raise ValueError("Reference age must be nonnegative seconds.")
        weights = self.profile_weights
        if not weights or len({key for key, _ in weights}) != len(weights):
            raise ValueError("Reference weights require unique profile IDs.")
        if any(not key or not math.isfinite(w) or w < 0 for key, w in weights):
            raise ValueError("Reference weights must be finite and nonnegative.")
        if not math.isclose(sum(w for _, w in weights), 1.0, abs_tol=1e-12):
            raise ValueError("Reference weights must sum to one.")
        object.__setattr__(self, "mean_w", mean)
        object.__setattr__(self, "variance_w2", variance)


@dataclass(frozen=True, slots=True, eq=False)
class CorrectedSpectrumFrame:
    frame_id: int
    segment_id: str
    context_id: str
    processing_generation: int
    values_w: FloatVector
    standard_uncertainty_w: FloatVector | None
    count: int
    started_at_s: float
    completed_at_s: float
    reference_age_s: float
    profile_weights: tuple[tuple[str, float], ...]
    quality: CorrectionQuality
    average_mode: TemporalAverageMode
    final: bool = False
    algorithm_version: str = "signed-reference-v1"
    interference_model_id: str | None = None
    interference_model_hash: str | None = None
    interference_last_coefficients: tuple[float, ...] = ()
    interference_last_control_rms_w: float | None = None

    def __post_init__(self) -> None:
        values = immutable_vector(self.values_w, name="corrected values_w")
        coefficients = tuple(self.interference_last_coefficients)
        if self.interference_model_id is None:
            if self.interference_model_hash is not None or coefficients or self.interference_last_control_rms_w is not None:
                raise ValueError("Interference diagnostics require a model identity.")
        elif (
            not self.interference_model_id or not self.interference_model_hash
            or len(self.interference_model_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.interference_model_hash)
            or not 1 <= len(coefficients) <= 8 or not all(math.isfinite(value) for value in coefficients)
            or self.interference_last_control_rms_w is None
            or not math.isfinite(self.interference_last_control_rms_w) or self.interference_last_control_rms_w < 0
            or self.algorithm_version != "signed-interference-v1"
            or self.final
            or self.standard_uncertainty_w is not None
        ):
            raise ValueError("Interference result requires complete model diagnostics without unqualified uncertainty.")
        if self.algorithm_version == "signed-interference-v1" and self.interference_model_id is None:
            raise ValueError("Interference algorithm requires its model identity.")
        object.__setattr__(self, "interference_last_coefficients", coefficients)
        uncertainty = self.standard_uncertainty_w
        if uncertainty is not None:
            uncertainty = immutable_vector(
                uncertainty, name="standard_uncertainty_w", nonnegative=True
            )
            if uncertainty.shape != values.shape:
                raise ValueError("Uncertainty must share the corrected frequency grid.")
        if self.count < 1:
            raise ValueError("Corrected snapshot requires contributing signal frames.")
        if self.frame_id < 0 or self.processing_generation < 0:
            raise ValueError("Corrected frame and generation IDs must be nonnegative.")
        if not self.segment_id or not self.context_id or not self.algorithm_version:
            raise ValueError("Corrected result requires source and algorithm identity.")
        times = (self.started_at_s, self.completed_at_s, self.reference_age_s)
        if not all(math.isfinite(t) for t in times):
            raise ValueError("Corrected result times must be finite seconds.")
        if self.completed_at_s < self.started_at_s or self.reference_age_s < 0:
            raise ValueError("Corrected time range or reference age is invalid.")
        if not self.profile_weights or any(
            not key or not math.isfinite(weight) or weight < 0
            for key, weight in self.profile_weights
        ) or not math.isclose(sum(w for _, w in self.profile_weights), 1.0, abs_tol=1e-12):
            raise ValueError("Corrected result requires normalized reference weights.")
        object.__setattr__(self, "values_w", values)
        object.__setattr__(self, "standard_uncertainty_w", uncertainty)
        object.__setattr__(self, "quality", CorrectionQuality(self.quality))
        object.__setattr__(self, "average_mode", TemporalAverageMode(self.average_mode))
