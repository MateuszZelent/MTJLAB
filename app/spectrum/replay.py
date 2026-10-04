"""Read-only, bounded replay of quantitative raw checkpoints through the live core."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterator

import h5py
import numpy as np

from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.spectrum_correction import (
    CorrectedSpectrumFrame, CorrectionConfig, SpectrumFrameEnvelope, SpectrumFrameRole,
)
from app.storage.spectrum_correction_codec import read_corrected, read_envelope, read_profile
from app.storage.spectrum_interference_codec import read_interference_calibration
from app.storage.spectrum_decision_store import ROOT, SCHEMA, iter_decisions, read_decision_context
from .realtime_processor import RealtimeSpectrumProcessor


@dataclass(frozen=True, slots=True)
class ReplayedSpectrumFrame:
    point_index: int
    envelope: SpectrumFrameEnvelope
    contributed: bool
    result: CorrectedSpectrumFrame | None


def replay_quantitative_session(path: str | Path, *, verify_stored=True,
                                cancellation_check=None) -> Iterator[ReplayedSpectrumFrame]:
    """Replay archived raw, never processed data; do not write to the source file.

    One HDF5 handle stays open and one frame is yielded at a time. Only the
    New sessions use explicit decisions at checkpoint boundaries. Older
    single-profile sessions retain their original reconstruction rules.
    """
    with h5py.File(path, "r") as file:
        attrs = file["run"].attrs
        if attrs.get("spectrum_correction_schema") != "spectrum-correction-v1":
            raise ExecutionError("Archive does not contain a supported quantitative session.")
        try:
            decision_schema = attrs.get("spectrum_processing_decision_schema")
            if decision_schema is not None:
                if decision_schema != SCHEMA:
                    raise ExecutionError("Unsupported processing decision history schema.")
                yield from _replay_decision_session(file, verify_stored=verify_stored,
                                                   cancellation_check=cancellation_check)
                return
            if ROOT in file:
                raise ExecutionError("Processing decision history is missing its session schema marker.")
            config = CorrectionConfig(**json.loads(attrs["spectrum_correction_config_json"]))
            root = file["spectrum_processing_v1/profiles"]
            if len(root) != 1:
                raise ExecutionError("Manual-session replay requires exactly one committed background profile.")
            context, profile = read_profile(next(iter(root.values())))
            processor = RealtimeSpectrumProcessor(context, config)
            initial_id = str(attrs["spectrum_correction_initial_profile_id"])
            reference_state = str(attrs["spectrum_correction_initial_reference_state"])
            calibrating = bool(reference_state)
            if initial_id:
                if initial_id != profile.profile_id:
                    raise ExecutionError("Initial reference profile is missing from the archive.")
                processor.set_background_profile(profile)
            if calibrating:
                processor.begin_reference(reference_state, signal_free_qualified=bool(
                    attrs["spectrum_correction_initial_signal_free_qualified"],
                ))
            model_id = str(attrs.get("spectrum_correction_initial_interference_model_id", ""))
            if model_id:
                calibration = read_interference_calibration(file[f"spectrum_processing_v1/interference_models/{model_id}"])
                processor.set_interference_calibration(calibration)
            points = file["points"]
            for index in range(len(points)):
                _check_cancellation(cancellation_check)
                if str(index) not in points or not points[str(index)].attrs.get("complete", False):
                    raise ExecutionError("Replay encountered an uncommitted or discontinuous checkpoint.")
                raw = file[f"spectra/{index}"]
                envelope = read_envelope(raw)
                if envelope is None:
                    raise ExecutionError("Quantitative replay requires acquisition evidence for every raw frame.")
                if not np.array_equal(raw["frequency_hz"][:], context.frequencies_hz):
                    raise ExecutionError("Replay raw frequency axis differs from its profile.")
                if calibrating and envelope.role == SpectrumFrameRole.SIGNAL:
                    reconstructed = processor.finish_reference()
                    if reconstructed.content_hash != profile.content_hash:
                        raise ExecutionError("Raw calibration does not reproduce the committed profile.")
                    calibrating = False
                contributed = processor.ingest(envelope, raw["power_dbm"][:])
                result = processor.snapshot()
                if result is not None and (not contributed or result.frame_id != envelope.frame_id):
                    result = None
                if verify_stored:
                    stored = read_corrected(raw["correction_v1"]) if "correction_v1" in raw else None
                    _verify_result(stored, result)
                    recorded = json.loads(points[str(index)]["metadata_json"].asstr()[()])
                    if recorded.get("quantitative_accepted") is not contributed:
                        raise ExecutionError("Replay contribution decision differs from the checkpoint.")
                yield ReplayedSpectrumFrame(index, envelope, contributed, result)
            if calibrating:
                reconstructed = processor.finish_reference()
                if reconstructed.content_hash != profile.content_hash:
                    raise ExecutionError("Raw calibration does not reproduce the committed profile.")
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError(f"Malformed quantitative replay archive: {exc}") from exc


def _require_parameters(parameters, keys):
    if type(parameters) is not dict or set(parameters) != set(keys):
        raise ExecutionError("Processing decision parameters differ from their operation contract.")


def _selected_profile(file, context, parameters):
    _require_parameters(parameters, ("profile_id", "profile_hash"))
    identity = parameters["profile_id"]
    if type(identity) is not str or not identity or "/" in identity or identity in {".", ".."}:
        raise ExecutionError("Processing decision requires an explicit stored profile identity.")
    stored_context, profile = read_profile(file[f"spectrum_processing_v1/profiles/{identity}"])
    if (stored_context.context_id != context.context_id or profile.profile_id != identity
            or profile.content_hash != parameters["profile_hash"]):
        raise ExecutionError("Processing decision profile provenance differs from the stored profile.")
    return profile


def _set_model(file, processor, parameters):
    _require_parameters(parameters, ("model_id", "model_hash"))
    identity = parameters["model_id"]
    if type(identity) is not str or not identity or "/" in identity or identity in {".", ".."}:
        raise ExecutionError("Processing decision requires an explicit stored model identity.")
    model = read_interference_calibration(file[f"spectrum_processing_v1/interference_models/{identity}"])
    if model.model_id != identity or model.content_hash != parameters["model_hash"]:
        raise ExecutionError("Processing decision model provenance differs from the stored model.")
    processor.set_interference_calibration(model)


def _begin_reference(processor, parameters):
    _require_parameters(parameters, ("reference_state", "signal_free_qualified"))
    state = parameters["reference_state"]
    if (type(state) is not str or not state.strip() or len(state) > 32768
            or type(parameters["signal_free_qualified"]) is not bool):
        raise ExecutionError("Processing decision requires explicit reference-state evidence.")
    processor.begin_reference(state, signal_free_qualified=parameters["signal_free_qualified"])


def _apply_decision(file, processor, record):
    operation, parameters = record["operation"], record["parameters"]
    if operation == "initialize":
        _require_parameters(parameters, ("profile_id", "profile_hash", "reference_state",
                                         "signal_free_qualified", "model_id", "model_hash"))
        if type(parameters["signal_free_qualified"]) is not bool:
            raise ExecutionError("Initialization qualification must be an explicit boolean.")
        if parameters["profile_id"] is not None:
            processor.set_background_profile(_selected_profile(file, processor.context, {
                key: parameters[key] for key in ("profile_id", "profile_hash")}))
        elif parameters["profile_hash"] is not None:
            raise ExecutionError("Initialization has a profile hash without a profile identity.")
        if parameters["reference_state"] is not None:
            _begin_reference(processor, {key: parameters[key] for key in
                                         ("reference_state", "signal_free_qualified")})
        if parameters["model_id"] is not None:
            _set_model(file, processor, {key: parameters[key] for key in ("model_id", "model_hash")})
        elif parameters["model_hash"] is not None:
            raise ExecutionError("Initialization has a model hash without a model identity.")
    elif operation == "set_profile":
        processor.set_background_profile(_selected_profile(file, processor.context, parameters))
    elif operation == "finish_reference":
        stored = _selected_profile(file, processor.context, parameters)
        actual = processor.finish_reference()
        if actual.profile_id != stored.profile_id or actual.content_hash != stored.content_hash:
            raise ExecutionError("Raw calibration does not reproduce the selected committed profile.")
    elif operation == "begin_reference":
        _begin_reference(processor, parameters)
    elif operation in {"reset_segment", "cancel_reference"}:
        _require_parameters(parameters, ())
        if operation == "reset_segment":
            processor.reset_segment()
        else:
            processor.cancel_reference()
    elif operation == "set_model":
        _set_model(file, processor, parameters)
    elif operation == "status_at":
        _require_parameters(parameters, ("at_s", "quality"))
        at_s = parameters["at_s"]
        if type(at_s) not in (float, int) or not np.isfinite(at_s) or at_s <= 0:
            raise ExecutionError("Processing decision status requires a finite positive timestamp.")
        if processor.status_at(at_s).value != parameters["quality"]:
            raise ExecutionError("Replay status decision differs from its recorded quality.")
    else:
        raise ExecutionError("Unknown processing decision operation.")


def _check_cancellation(check):
    if check is not None and check():
        raise ProcessingCancelled("Quantitative replay canceled at a checkpoint boundary.")


def _replay_decision_session(file, *, verify_stored, cancellation_check):
    context, config = read_decision_context(file)
    if asdict(config) != json.loads(file["run"].attrs["spectrum_correction_config_json"]):
        raise ExecutionError("Processing decision configuration differs from its session.")
    processor = RealtimeSpectrumProcessor(context, config)
    decisions = iter_decisions(file)
    pending = next(decisions, None)
    points = file["points"]
    # Include the trailing boundary: finishing a reference or resetting after
    # the last raw checkpoint must be verified even without a following frame.
    for index in range(len(points) + 1):
        _check_cancellation(cancellation_check)
        while pending is not None and pending["before_point_index"] == index:
            _check_cancellation(cancellation_check)
            _apply_decision(file, processor, pending)
            pending = next(decisions, None)
        if index == len(points):
            break
        if str(index) not in points or not points[str(index)].attrs.get("complete", False):
            raise ExecutionError("Replay encountered an uncommitted or discontinuous checkpoint.")
        raw = file[f"spectra/{index}"]
        envelope = read_envelope(raw)
        if envelope is None:
            raise ExecutionError("Quantitative replay requires acquisition evidence for every raw frame.")
        if not np.array_equal(raw["frequency_hz"][:], context.frequencies_hz):
            raise ExecutionError("Replay raw frequency axis differs from its context.")
        contributed = processor.ingest(envelope, raw["power_dbm"][:])
        result = processor.snapshot()
        if result is not None and (not contributed or result.frame_id != envelope.frame_id):
            result = None
        if verify_stored:
            stored = read_corrected(raw["correction_v1"]) if "correction_v1" in raw else None
            _verify_result(stored, result)
            recorded = json.loads(points[str(index)]["metadata_json"].asstr()[()])
            if recorded.get("quantitative_accepted") is not contributed:
                raise ExecutionError("Replay contribution decision differs from the checkpoint.")
        yield ReplayedSpectrumFrame(index, envelope, contributed, result)
    if pending is not None:
        raise ExecutionError("Processing history contains an unreachable checkpoint boundary.")


def _verify_result(stored, actual):
    if (stored is None) != (actual is None):
        raise ExecutionError("Replay result availability differs from the checkpoint.")
    if stored is None:
        return
    fields = ("frame_id", "segment_id", "context_id", "processing_generation", "count",
              "started_at_s", "completed_at_s", "reference_age_s", "profile_weights",
              "quality", "average_mode", "final", "algorithm_version")
    fields += ("interference_model_id", "interference_model_hash", "interference_last_coefficients",
               "interference_last_control_rms_w")
    if any(getattr(stored, field) != getattr(actual, field) for field in fields):
        raise ExecutionError("Replay result provenance differs from the checkpoint.")
    if not np.allclose(stored.values_w, actual.values_w, rtol=1e-12, atol=0):
        raise ExecutionError("Replay signed power differs from the checkpoint.")
    first, second = stored.standard_uncertainty_w, actual.standard_uncertainty_w
    if (first is None) != (second is None) or (
        first is not None and not np.allclose(first, second, rtol=1e-12, atol=0)
    ):
        raise ExecutionError("Replay uncertainty differs from the checkpoint.")
