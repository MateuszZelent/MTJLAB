"""Out-of-training REF diagnostics; no inferred CI or scientific approval."""

import json
import math

import numpy as np

from .background_profile import BackgroundProfileBuilder
from .interference_model import calibrated_interference_model
from .streaming_statistics import VectorWelford


def validate_interference_references(calibration, profile, frames, validation_mask, *, reference_context=None,
                                     cancellation_check=None):
    """Fit on calibrated controls and assess distinct, unused frequency bins."""
    provenance = json.loads(calibration.training_provenance_json or "{}")
    try:
        start, end = provenance["reference_started_at_s"], provenance["reference_completed_at_s"]
        if not all(math.isfinite(value) for value in (start, end)) or end < start:
            raise ValueError("Invalid training time range.")
    except KeyError as exc:
        raise ValueError("Calibration lacks a recorded training time range; regenerate it.") from exc
    if profile.context_id != calibration.context.context_id:
        raise ValueError("Validation REF acquisition context differs from calibration.")
    context = reference_context if reference_context is not None else calibration.context
    if context.context_id != profile.context_id:
        raise ValueError("Validation REF profile differs from its acquisition context.")
    if not (profile.completed_at_s < start or profile.started_at_s > end):
        raise ValueError("Validation REF overlaps the training acquisition interval.")
    if any(profile.content_hash == digest for _identity, digest in calibration.source_profiles):
        raise ValueError("Validation REF cannot reuse a training profile.")
    mask = np.asarray(validation_mask)
    controls = calibration.control_mask & ~calibration.protected_mask
    if mask.dtype != np.bool_ or mask.shape != calibration.baseline_w.shape or not mask.any():
        raise ValueError("Validation requires explicit nonempty frequency bins.")
    if np.any(mask & controls):
        raise ValueError("Validation bins must not participate in coefficient fitting.")
    model = calibrated_interference_model(calibration)
    builder = BackgroundProfileBuilder(context, reference_state=profile.reference_state,
                                       minimum_sweeps=2, signal_free_qualified=profile.signal_free_qualified)
    errors = VectorWelford(calibration.baseline_w.size)
    matched_static = VectorWelford(calibration.baseline_w.size)
    rejections = 0
    reasons = {}
    for envelope, watts in frames:
        if cancellation_check:
            cancellation_check()
        builder.add(envelope, watts)  # Enforces role, timing, counter and context, even on rejected fits.
        try:
            fit = model.fit(envelope, watts)
        except (ValueError, np.linalg.LinAlgError) as exc:
            rejections += 1
            key = str(exc)
            reasons[key] = reasons.get(key, 0) + 1
            continue
        errors.add(watts - fit.background_w)
        matched_static.add(watts - calibration.baseline_w)
    if builder.finish().content_hash != profile.content_hash:
        raise ValueError("Validation raw does not reproduce its committed reference profile.")

    def statistics(moments):
        if moments.count == 0:
            return None
        variance = moments.variance()
        rms = np.sqrt(moments.mean**2 + (0 if variance is None else variance * (moments.count - 1) / moments.count))
        if not np.all(np.isfinite(rms)):
            raise ValueError("Validation RMS is not finite.")
        return {"mean_error_w": moments.mean.tolist(), "rms_error_w": rms.tolist(),
                "unused_region_rms_w": float(np.linalg.norm(rms[mask]) / np.sqrt(mask.sum()))}

    return {"validation_algorithm": "disjoint-reference-prediction-v1",
            "model_id": calibration.model_id, "model_hash": calibration.content_hash,
            "validation_profile_id": profile.profile_id, "validation_profile_hash": profile.content_hash,
            "reference_sweeps": builder.statistics.count, "accepted_fits": errors.count,
            "rejected_fits": rejections, "rejection_reasons": reasons,
            "training_interval_s": [start, end],
            "validation_interval_s": [profile.started_at_s, profile.completed_at_s],
            "validation_bin_indices": np.flatnonzero(mask).tolist(),
            "frequencies_hz": calibration.context.frequencies_hz.tolist(),
            "model_error_on_accepted_fits": statistics(errors),
            "static_error_on_same_accepted_fits": statistics(matched_static),
            "condition_number": model.condition_number, "qualification_inferred": False,
            "units": {"frequencies_hz": "Hz", "mean_error_w": "W", "rms_error_w": "W",
                      "unused_region_rms_w": "W", "interval_s": "s (Unix UTC)"},
            "limitations": ["Temporal disjointness does not establish statistical independence",
                            "Error statistics condition on accepted fits; rejected fits remain counted",
                            "REFERENCE diagnostics do not prove preservation of an unknown SIGNAL",
                            "No confidence interval, model-parameter covariance or laboratory approval inferred"]}
