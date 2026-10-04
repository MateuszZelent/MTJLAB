"""Private, versioned HDF5 representation of SI correction profiles/results."""

from __future__ import annotations

from dataclasses import asdict
import json
import re

from app.domain.errors import ExecutionError
from app.domain.spectrum_correction import (
    BackgroundProfile,
    CorrectedSpectrumFrame,
    SpectrumAcquisitionContext,
    SpectrumFrameEnvelope,
)

SCHEMA = "spectrum-correction-v1"


def safe_record_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise ExecutionError("Spectrum processing record ID is not a safe HDF5 key.")
    return value


def read_selected_profile(root, profile_id=None):
    """Select a committed profile explicitly; never choose by HDF5 order."""
    if root is None or len(root) == 0:
        raise ExecutionError("Archive contains no committed background profile.")
    if profile_id is None:
        if len(root) != 1:
            raise ExecutionError("Choose an explicit profile ID: archive contains multiple background profiles.")
        group = next(iter(root.values()))
    else:
        if type(profile_id) is not str:
            raise ExecutionError("Background profile ID must be an explicit string.")
        key = safe_record_id(profile_id)
        if key not in root:
            raise ExecutionError(f"Selected background profile is missing: {key}.")
        group = root[key]
    context, profile = read_profile(group)
    if profile_id is not None and profile.profile_id != profile_id:
        raise ExecutionError("Selected background profile key differs from its stored identity.")
    return context, profile


def _array(group, name, values, unit):
    dataset = group.create_dataset(name, data=values, dtype="f8")
    dataset.attrs["unit"] = unit


def _read_array(group, name, unit):
    dataset = group[name]
    if str(dataset.attrs.get("unit", "")) != unit:
        raise ExecutionError(f"Processing dataset {name} does not carry unit {unit}.")
    return dataset[:]


def write_profile(group, context: SpectrumAcquisitionContext, profile: BackgroundProfile) -> None:
    if profile.context_id != context.context_id:
        raise ExecutionError("Profile acquisition identity does not match its frequency axis.")
    if profile.mean_w.shape != context.frequencies_hz.shape:
        raise ExecutionError("Profile and frequency grid have mismatched lengths.")
    group.attrs["schema"] = SCHEMA
    group.attrs["profile_id"] = profile.profile_id
    group.attrs["content_hash"] = profile.content_hash
    metadata = {
        "configuration_fingerprint": context.configuration_fingerprint,
        "configuration_generation": context.configuration_generation,
        "settings_verified": context.settings_verified,
        "independent_sweeps_qualified": context.independent_sweeps_qualified,
        "context_id": profile.context_id,
        "sweep_count": profile.sweep_count,
        "started_at_s": profile.started_at_s,
        "completed_at_s": profile.completed_at_s,
        "reference_state": profile.reference_state,
        "signal_free_qualified": profile.signal_free_qualified,
    }
    group.attrs["metadata_json"] = json.dumps(metadata, sort_keys=True, allow_nan=False)
    _array(group, "frequency_hz", context.frequencies_hz, "Hz")
    _array(group, "mean_w", profile.mean_w, "W")
    _array(group, "sample_variance_w2", profile.sample_variance_w2, "W^2")
    if profile.mean_variance_w2 is not None:
        _array(group, "mean_variance_w2", profile.mean_variance_w2, "W^2")


def read_profile(group) -> tuple[SpectrumAcquisitionContext, BackgroundProfile]:
    if group.attrs.get("schema") != SCHEMA or not bool(group.attrs.get("complete", False)):
        raise ExecutionError("Unknown or uncommitted spectrum profile schema.")
    try:
        metadata = json.loads(group.attrs["metadata_json"])
        context = SpectrumAcquisitionContext(
            _read_array(group, "frequency_hz", "Hz"),
            metadata["configuration_fingerprint"],
            configuration_generation=metadata["configuration_generation"],
            settings_verified=metadata["settings_verified"],
            independent_sweeps_qualified=metadata["independent_sweeps_qualified"],
        )
        profile = BackgroundProfile(
            profile_id=str(group.attrs["profile_id"]), context_id=metadata["context_id"],
            mean_w=_read_array(group, "mean_w", "W"),
            sample_variance_w2=_read_array(group, "sample_variance_w2", "W^2"),
            mean_variance_w2=(
                _read_array(group, "mean_variance_w2", "W^2")
                if "mean_variance_w2" in group else None
            ),
            sweep_count=metadata["sweep_count"], started_at_s=metadata["started_at_s"],
            completed_at_s=metadata["completed_at_s"],
            reference_state=metadata["reference_state"],
            signal_free_qualified=metadata["signal_free_qualified"],
        )
        if context.context_id != profile.context_id:
            raise ExecutionError("Stored profile frequency/configuration identity is corrupted.")
        if profile.content_hash != group.attrs["content_hash"]:
            raise ExecutionError("Stored spectrum profile checksum is corrupted.")
        return context, profile
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Malformed spectrum background profile: {exc}") from exc


def write_envelope(group, envelope: SpectrumFrameEnvelope) -> None:
    group.attrs["acquisition_envelope_json"] = json.dumps(
        asdict(envelope), sort_keys=True, allow_nan=False,
    )


def read_envelope(group) -> SpectrumFrameEnvelope | None:
    if "acquisition_envelope_json" not in group.attrs:
        return None
    try:
        return SpectrumFrameEnvelope(**json.loads(group.attrs["acquisition_envelope_json"]))
    except (TypeError, ValueError) as exc:
        raise ExecutionError(f"Malformed spectrum acquisition envelope: {exc}") from exc


def write_corrected(group, result: CorrectedSpectrumFrame) -> None:
    metadata = {
        field: getattr(result, field) for field in (
            "frame_id", "segment_id", "context_id", "processing_generation", "count",
            "started_at_s", "completed_at_s", "reference_age_s", "profile_weights", "quality",
            "average_mode", "final", "algorithm_version",
            "interference_model_id", "interference_model_hash", "interference_last_coefficients",
            "interference_last_control_rms_w",
        )
    }
    group.attrs["schema"] = SCHEMA
    group.attrs["metadata_json"] = json.dumps(metadata, sort_keys=True, allow_nan=False)
    _array(group, "values_w", result.values_w, "W")
    if result.standard_uncertainty_w is not None:
        _array(group, "standard_uncertainty_w", result.standard_uncertainty_w, "W")


def read_corrected(group) -> CorrectedSpectrumFrame:
    if group.attrs.get("schema") != SCHEMA:
        raise ExecutionError("Unknown spectrum correction result schema.")
    try:
        metadata = json.loads(group.attrs["metadata_json"])
        metadata["profile_weights"] = tuple(tuple(row) for row in metadata["profile_weights"])
        return CorrectedSpectrumFrame(
            **metadata, values_w=_read_array(group, "values_w", "W"),
            standard_uncertainty_w=(
                _read_array(group, "standard_uncertainty_w", "W")
                if "standard_uncertainty_w" in group else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Malformed corrected spectrum result: {exc}") from exc
