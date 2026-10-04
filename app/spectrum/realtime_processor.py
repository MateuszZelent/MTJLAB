"""Deterministic O(F) signed-power correction; no Qt or instrument control."""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike
from app.domain.spectrum_interference import SpectrumInterferenceCalibration

from app.domain.spectrum_correction import (
    BackgroundProfile,
    CorrectedSpectrumFrame,
    CorrectionConfig,
    CorrectionQuality,
    FloatVector,
    SpectrumAcquisitionContext,
    SpectrumFrameEnvelope,
    SpectrumFrameRole,
    TemporalAverageMode,
    immutable_vector,
)
from .background_profile import BackgroundProfileBuilder
from .streaming_statistics import TemporalPowerAverage, dbm_to_w, finite_vector
from .sweep_counter_guard import SweepCounterGuard
from .interference_model import calibrated_interference_model


class RealtimeSpectrumProcessor:
    """Consume every accepted frame; publish copies only when requested.

    Frames without qualified completeness never change quantitative state.
    Independence, a signal-free reference and drift qualification are separate
    evidence. Preview averaging does not publish an unqualified CI.
    """

    def __init__(self, context: SpectrumAcquisitionContext, config: CorrectionConfig) -> None:
        self.context = context
        self.context_id = context.context_id
        self.config = config
        size = context.frequencies_hz.size
        vector_count = 20 + (
            config.window_frames if config.average_mode == TemporalAverageMode.WINDOW else 0
        )
        if vector_count * size * 8 > config.working_memory_limit_bytes:
            raise ValueError("Configured correction window exceeds the working-memory limit.")
        self._linear = np.empty(size, dtype=np.float64)
        self._residual = np.empty(size, dtype=np.float64)
        self._average = self._new_average()
        self._profile: BackgroundProfile | None = None
        self._drift_rate_w2_s: FloatVector | None = None
        self._builder: BackgroundProfileBuilder | None = None
        self._last: SpectrumFrameEnvelope | None = None
        self._last_signal: SpectrumFrameEnvelope | None = None
        self._counter_guard = SweepCounterGuard()
        self._generation = 0
        self._interference_calibration = None
        self._interference_model = None
        self._last_interference_fit = None
        self.quality = CorrectionQuality.RAW_ONLY
        self.accepted_frames = 0
        self.rejected_frames = 0
        self.reference_age_s: float | None = None

    def _new_average(self) -> TemporalPowerAverage:
        return TemporalPowerAverage(
            self.context.frequencies_hz.size, self.config.average_mode,
            tau_s=self.config.time_constant_s, window_frames=self.config.window_frames,
        )

    def reset_segment(self) -> None:
        self._average = self._new_average()
        self._last_signal = None
        self._last_interference_fit = None
        self._generation += 1

    @property
    def profile(self) -> BackgroundProfile | None:
        return self._profile

    @property
    def calibration_count(self) -> int:
        return 0 if self._builder is None else self._builder.statistics.count

    def set_background_profile(
        self, profile: BackgroundProfile, *, drift_variance_rate_w2_s: ArrayLike | None = None
    ) -> None:
        if profile.context_id != self.context_id or profile.mean_w.shape != self._linear.shape:
            raise ValueError("Background profile does not match the acquisition context.")
        drift = None
        if drift_variance_rate_w2_s is not None:
            drift = immutable_vector(
                drift_variance_rate_w2_s, name="drift_variance_rate_w2_s", nonnegative=True
            )
            if drift.shape != self._linear.shape:
                raise ValueError("Drift-rate vector must share the frequency grid.")
        self._profile = profile
        self._interference_calibration = self._interference_model = None
        self._drift_rate_w2_s = drift
        self._builder = None
        # Refresh closes the temporal segment. This avoids mixing windows
        # trained with different profiles and keeps uncertainty state bounded.
        self.reset_segment()
        self.quality = CorrectionQuality.UNQUALIFIED

    def set_interference_calibration(self, calibration: SpectrumInterferenceCalibration) -> None:
        """Install a qualified control fit and close the previous averaging block."""
        profile = self._profile
        if self._builder is not None or profile is None:
            raise ValueError("Interference correction requires a completed background profile.")
        if calibration.context.context_id != self.context_id or calibration.source_profiles != (
            (profile.profile_id, profile.content_hash),
        ) or not np.array_equal(calibration.baseline_w, profile.mean_w):
            raise ValueError("Interference calibration must match the active reference and context.")
        if not calibration.signal_control_regions_qualified:
            raise ValueError("Live interference correction requires independently qualified signal controls.")
        calibration_bytes = sum(array.nbytes for array in (
            calibration.basis_w, calibration.baseline_w, calibration.control_sigma_w,
            calibration.control_mask, calibration.protected_mask,
        ))
        vector_count = 20 + (self.config.window_frames if self.config.average_mode == TemporalAverageMode.WINDOW else 0)
        base_bytes = calibration_bytes + vector_count * self._linear.size * 8
        # Bound the retained operator before allocating QR. Runtime scratch
        # is covered by the existing conservative twenty-vector reservation.
        rank = calibration.basis_w.shape[1]
        operator_bound = 2 * calibration.basis_w.nbytes + (3 * self._linear.size + rank * rank + rank) * 8
        if base_bytes + operator_bound > self.config.working_memory_limit_bytes:
            raise ValueError("Interference calibration exceeds the working-memory limit.")
        model = calibrated_interference_model(calibration)
        if base_bytes + model.working_memory_bytes > self.config.working_memory_limit_bytes:
            raise ValueError("Interference calibration exceeds the working-memory limit.")
        self._interference_calibration, self._interference_model = calibration, model
        self.reset_segment()
        self.quality = CorrectionQuality.UNQUALIFIED

    def begin_reference(self, reference_state: str, *, signal_free_qualified: bool = False) -> None:
        if self._builder is not None:
            raise ValueError("Reference calibration is already active.")
        self._builder = BackgroundProfileBuilder(
            self.context, reference_state=reference_state,
            minimum_sweeps=self.config.minimum_reference_sweeps,
            signal_free_qualified=signal_free_qualified,
        )
        self.reset_segment()
        self.quality = CorrectionQuality.CALIBRATING

    def finish_reference(self) -> BackgroundProfile:
        if self._builder is None:
            raise ValueError("No reference calibration is active.")
        profile = self._builder.finish()
        self.set_background_profile(profile)
        return profile

    def cancel_reference(self) -> None:
        self._builder = None
        self.reset_segment()
        self.quality = (
            CorrectionQuality.RAW_ONLY if self._profile is None else CorrectionQuality.UNQUALIFIED
        )

    def ingest(self, envelope: SpectrumFrameEnvelope, powers_dbm: ArrayLike) -> bool:
        """Return whether the frame contributed; reject before touching accumulators."""
        finite_vector(powers_dbm, size=self._linear.size)
        if (
            envelope.context_id != self.context_id
            or envelope.configuration_generation != self.context.configuration_generation
        ):
            self.reset_segment()
            self._builder = None
            self.quality = CorrectionQuality.INCOMPATIBLE
            self.rejected_frames += 1
            return False
        if not envelope.complete or (
            self._last is not None and (
                envelope.frame_id <= self._last.frame_id
                or envelope.acquired_at_s <= self._last.acquired_at_s
            )
        ) or not self._counter_guard.allows(envelope):
            self.quality = CorrectionQuality.INVALID_ACQUISITION
            self.rejected_frames += 1
            return False
        dbm_to_w(powers_dbm, out=self._linear)
        if self._last is not None and envelope.segment_id != self._last.segment_id:
            self.reset_segment()
        self._last = envelope
        self._counter_guard.record(envelope)
        if envelope.role in {SpectrumFrameRole.UNKNOWN, SpectrumFrameRole.TRANSITION}:
            self.reset_segment()
            self.quality = CorrectionQuality.RAW_ONLY
            return False
        if envelope.role == SpectrumFrameRole.REFERENCE:
            if self._builder is None:
                self.quality = CorrectionQuality.RAW_ONLY
                return False
            self._builder.add(envelope, self._linear)
            self.accepted_frames += 1
            self.quality = CorrectionQuality.CALIBRATING
            return True
        if self._builder is not None:
            self.rejected_frames += 1
            self.quality = CorrectionQuality.INVALID_ACQUISITION
            return False
        if self._profile is None:
            self.quality = CorrectionQuality.RAW_ONLY
            return False
        if envelope.acquired_at_s < self._profile.completed_at_s:
            self.rejected_frames += 1
            self.quality = CorrectionQuality.INVALID_ACQUISITION
            return False
        age_s = envelope.acquired_at_s - self._profile.completed_at_s
        self.reference_age_s = age_s
        maximum_age = self.config.maximum_reference_age_s
        if maximum_age is not None and age_s > maximum_age:
            self.reset_segment()
            self.quality = CorrectionQuality.STALE
            return False
        if (
            self._average.last_time_s is not None
            and envelope.acquired_at_s - self._average.last_time_s > self.config.maximum_gap_s
        ):
            self.reset_segment()
        if self._interference_model is not None:
            try:
                fit = self._interference_model.fit(envelope, self._linear)
            except (ValueError, np.linalg.LinAlgError):
                self.rejected_frames += 1
                self.quality = CorrectionQuality.INVALID_MODEL
                return False
            background = fit.background_w
            self._last_interference_fit = fit
        else:
            background = self._profile.mean_w
        np.subtract(self._linear, background, out=self._residual)
        self._average.add(self._residual, envelope.acquired_at_s, self._profile.profile_id)
        self._last_signal = envelope
        self.accepted_frames += 1
        self.quality = (
            CorrectionQuality.READY
            if self._interference_model is None and self.context.settings_verified and self._profile.signal_free_qualified
            and maximum_age is not None and self._drift_rate_w2_s is not None
            else CorrectionQuality.UNQUALIFIED
        )
        return True

    def status_at(self, at_s: float) -> CorrectionQuality:
        """Age validation works even when acquisition has stopped delivering frames."""
        if not np.isfinite(at_s):
            raise ValueError("Status time must be finite seconds.")
        if self._profile is None or self._builder is not None:
            return self.quality
        self.reference_age_s = max(0.0, at_s - self._profile.completed_at_s)
        maximum_age = self.config.maximum_reference_age_s
        if maximum_age is not None and self.reference_age_s > maximum_age:
            self.quality = CorrectionQuality.STALE
        return self.quality

    def snapshot(self) -> CorrectedSpectrumFrame | None:
        last, profile = self._last_signal, self._profile
        if last is None or profile is None or not self._average.count:
            return None
        uncertainty = None
        if (
            self.quality == CorrectionQuality.READY
            and self.context.independent_sweeps_qualified
            and profile.mean_variance_w2 is not None
        ):
            signal_variance = self._average.sample_mean_variance()
            if signal_variance is not None:
                assert self._drift_rate_w2_s is not None
                # One held reference: its variance is added once, not divided
                # by the number of signal frames. End-age drift variance is a
                # conservative shared contribution for the whole block.
                age_s = last.acquired_at_s - profile.completed_at_s
                uncertainty = np.sqrt(
                    signal_variance + profile.mean_variance_w2
                    + age_s * self._drift_rate_w2_s
                )
        assert self._average.first_time_s is not None
        return CorrectedSpectrumFrame(
            frame_id=last.frame_id, segment_id=last.segment_id, context_id=self.context_id,
            processing_generation=self._generation,
            values_w=self._average.mean, standard_uncertainty_w=uncertainty,
            count=self._average.count, started_at_s=self._average.first_time_s,
            completed_at_s=last.acquired_at_s,
            reference_age_s=last.acquired_at_s - profile.completed_at_s,
            profile_weights=self._average.weights(), quality=self.quality,
            average_mode=self.config.average_mode,
            algorithm_version="signed-reference-v1" if self._interference_model is None else "signed-interference-v1",
            interference_model_id=(self._interference_calibration.model_id if self._interference_calibration else None),
            interference_model_hash=(self._interference_calibration.content_hash if self._interference_calibration else None),
            interference_last_coefficients=(self._last_interference_fit.coefficients if self._last_interference_fit else ()),
            interference_last_control_rms_w=(self._last_interference_fit.control_rms_w if self._last_interference_fit else None),
        )
