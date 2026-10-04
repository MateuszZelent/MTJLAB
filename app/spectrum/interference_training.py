"""Offline local basis from bounded REF samples; no signal-driven training."""

from dataclasses import dataclass, replace
import json

import numpy as np

from app.domain.spectrum_interference import SpectrumInterferenceCalibration
from .background_profile import BackgroundProfileBuilder
from .interference_model import calibrated_interference_model


@dataclass(frozen=True, slots=True)
class InterferenceTrainingConfig:
    components: int = 2
    maximum_training_frames: int = 64
    working_memory_limit_bytes: int = 64 * 1024 * 1024
    relative_rank_tolerance: float = 1e-6

    def __post_init__(self):
        if type(self.components) is not int or not 1 <= self.components <= 8:
            raise ValueError("Training requires 1 to 8 components.")
        if type(self.maximum_training_frames) is not int or not self.components + 2 <= self.maximum_training_frames <= 256:
            raise ValueError("Training frame cap must exceed rank by two, and be at most 256.")
        if type(self.working_memory_limit_bytes) is not int or self.working_memory_limit_bytes < 1024:
            raise ValueError("Invalid training memory budget.")
        if not np.isfinite(self.relative_rank_tolerance) or not 0 < self.relative_rank_tolerance < 1:
            raise ValueError("Rank tolerance must be finite in (0,1).")


def training_buffer_bytes(size, count, config):
    selected_count = min(count, config.maximum_training_frames)
    return (8 * selected_count * size + 32 * size + 8 * selected_count**2) * 8


def train_interference_basis(context, profile, frame_factory, *, model_id, nuisance_mask,
                             control_mask, protected_mask, control_sigma_w,
                             config=InterferenceTrainingConfig(), provenance=None,
                             signal_control_regions_qualified=False, qualification_evidence="",
                             cancellation_check=None):
    """frame_factory supplies fresh iterators of accepted REF (envelope, W).

    A deterministic, evenly spaced sample trains a local SVD basis. Every
    accepted REF verifies the profile and then calibrates control-fit bounds.
    No F by F covariance is allocated. LAPACK cancellation waits for SVD.
    Empirical coefficient bounds are a calibration envelope, never a CI.
    """
    size, count = context.frequencies_hz.size, profile.sweep_count
    selected_count = min(count, config.maximum_training_frames)
    # Covers matrix copies, reduced SVD outputs and conservative LAPACK scratch;
    # native-library process RSS is not guaranteed by this buffer estimate.
    estimate = training_buffer_bytes(size, count, config)
    if estimate > config.working_memory_limit_bytes:
        raise ValueError("Reference training buffers exceed the working-memory budget.")
    nuisance = np.asarray(nuisance_mask)
    if nuisance.dtype != np.bool_ or nuisance.shape != (size,) or np.count_nonzero(nuisance) <= config.components:
        raise ValueError("Training requires explicit local nuisance bins.")
    if np.all(nuisance):
        raise ValueError("Local nuisance training cannot span the entire frequency axis.")
    edges = np.diff(np.r_[False, nuisance, False].astype(np.int8))
    regions = tuple(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))
    if len(regions) > 32:
        raise ValueError("Local training supports at most 32 nuisance regions.")
    if selected_count < config.components + 2:
        raise ValueError("Too few reference sweeps for the requested basis.")
    if context.context_id != profile.context_id:
        raise ValueError("Training profile context differs from its frequency grid.")
    indices = np.linspace(0, count - 1, selected_count, dtype=int)
    sampled = np.empty((selected_count, size))
    builder = BackgroundProfileBuilder(context, reference_state=profile.reference_state, minimum_sweeps=2,
                                       signal_free_qualified=profile.signal_free_qualified)
    ordinal = 0
    sample_index = 0
    for envelope, watts in frame_factory():
        if cancellation_check:
            cancellation_check()
        builder.add(envelope, watts)
        if sample_index < selected_count and ordinal == indices[sample_index]:
            sampled[sample_index] = watts - profile.mean_w
            sampled[sample_index, ~nuisance] = 0
            sample_index += 1
        ordinal += 1
    if ordinal != count or builder.finish().content_hash != profile.content_hash:
        raise ValueError("Accepted raw references do not reproduce the committed profile.")
    if cancellation_check:
        cancellation_check()
    scale = float(np.max(np.abs(sampled)))
    if not scale > 0:
        raise ValueError("Reference data contain no varying nuisance component.")
    sampled /= scale
    _u, singular, vh = np.linalg.svd(sampled, full_matrices=False)
    if singular[config.components - 1] <= config.relative_rank_tolerance * singular[0]:
        raise ValueError("Reference variations do not identify the requested component count.")
    # RMS-amplitude normalization makes coefficients dimensionless.
    basis = vh[:config.components].T * (singular[:config.components] * scale / np.sqrt(selected_count))
    basis[~nuisance] = 0
    for column in range(config.components):
        pivot = int(np.argmax(np.abs(basis[:, column])))
        if basis[pivot, column] < 0:
            basis[:, column] *= -1
    del sampled, _u, vh
    calibration = SpectrumInterferenceCalibration(
        model_id, context, profile.mean_w, basis, control_mask, protected_mask, control_sigma_w,
        tuple((-1e100, 1e100) for _ in range(config.components)),
        ((profile.profile_id, profile.content_hash),), signal_control_regions_qualified,
        qualification_evidence,
    )
    model = calibrated_interference_model(calibration)
    low, high = np.full(config.components, np.inf), np.full(config.components, -np.inf)
    rms_sum = 0.0
    rms_max = 0.0
    second_count = 0
    second_builder = BackgroundProfileBuilder(context, reference_state=profile.reference_state, minimum_sweeps=2,
                                              signal_free_qualified=profile.signal_free_qualified)
    for envelope, watts in frame_factory():
        if cancellation_check:
            cancellation_check()
        second_builder.add(envelope, watts)
        fit = model.fit(envelope, watts)
        low = np.minimum(low, fit.coefficients)
        high = np.maximum(high, fit.coefficients)
        rms_sum += fit.control_rms_w
        rms_max = max(rms_max, fit.control_rms_w)
        second_count += 1
    if second_count != count or second_builder.finish().content_hash != profile.content_hash or np.any(low >= high):
        raise ValueError("Raw reference history changed or coefficient range is not identified.")
    # Numerical guard only, not extrapolation beyond the observed envelope.
    guard = 64 * np.finfo(float).eps * np.maximum(1, np.maximum(np.abs(low), np.abs(high)))
    bounds = tuple(zip(low - guard, high + guard))
    report = dict(provenance or {})
    report.update(training_algorithm="reference-local-svd-v1", numpy_version=np.__version__,
                  reference_sweeps=count, selected_reference_ordinals=indices.tolist(),
                  reference_started_at_s=profile.started_at_s, reference_completed_at_s=profile.completed_at_s,
                  nuisance_index_ranges=[[int(start), int(end)] for start, end in regions],
                  singular_values_w=(singular[:config.components] * scale).tolist(),
                  control_rms_mean_w=rms_sum / count, control_rms_max_w=rms_max,
                  estimated_buffer_bytes=estimate, relative_rank_tolerance=config.relative_rank_tolerance,
                  maximum_training_frames=config.maximum_training_frames,
                  working_memory_limit_bytes=config.working_memory_limit_bytes,
                  qualification_inferred=False, bounds_are_confidence_interval=False,
                  limitations=["In-sample REF diagnostics, not held-out validation",
                               "Control regions must exclude signal including tails and RBW margin",
                               "No model-parameter covariance or laboratory qualification inferred"])
    return replace(calibration, coefficient_bounds=bounds, training_provenance_json=json.dumps(report, allow_nan=False))
