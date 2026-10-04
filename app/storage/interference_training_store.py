"""Read closed REF raw and publish a new, self-contained calibration artifact."""

from datetime import datetime, timezone
import json
from pathlib import Path

import h5py
import numpy as np

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.models import MeasurementPoint
from app.domain.quantities import DIMENSION_POWER, parse_quantity
from app.spectrum.frequency_regions import frequency_region_mask
from app.spectrum.interference_training import InterferenceTrainingConfig, train_interference_basis, training_buffer_bytes
from app.spectrum.streaming_statistics import dbm_to_w
from .finalized_spectrum_store import file_sha256
from .hdf5_writer import Hdf5RunWriter
from .spectrum_correction_codec import read_envelope, read_profile


def train_interference_archive(source, destination, *, model_id, nuisance_mask,
                               control_mask, protected_mask, control_sigma_w,
                               config=InterferenceTrainingConfig(),
                               signal_control_regions_qualified=False, qualification_evidence="",
                               cancellation_check=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise ExecutionError("Training requires a new destination separate from its raw source.")
    original_hash = file_sha256(source, cancellation_check=cancellation_check)
    with h5py.File(source, "r") as file:
        if file["run"].attrs.get("status") != "completed" or len(file["_pending"]):
            raise ExecutionError("Training requires a completed raw reference archive.")
        root = file.get("spectrum_processing_v1/profiles")
        if root is None or len(root) != 1:
            raise ExecutionError("Training requires exactly one committed reference profile.")
        group = next(iter(root.values()))
        profile_count = json.loads(group.attrs["metadata_json"])["sweep_count"]
        if training_buffer_bytes(group["frequency_hz"].shape[0], profile_count, config) > config.working_memory_limit_bytes:
            raise ExecutionError("Training archive buffers exceed the working-memory budget.")
        context, profile = read_profile(group)
        simulation_metadata = json.loads(file["run/simulation_json"].asstr()[()])
        simulation_metadata["derived_operation"] = "reference_local_svd_training"
        device_idn = json.loads(file["run/device_idn_json"].asstr()[()])
        settings_source = file["run/settings_yaml"].asstr()[()]

        def frames():
            for index in range(len(file["points"])):
                if cancellation_check:
                    cancellation_check()
                point = file.get(f"points/{index}")
                if point is None or not point.attrs.get("complete", False):
                    raise ExecutionError("Training source contains an uncommitted raw checkpoint.")
                raw = file[f"spectra/{index}"]
                envelope = read_envelope(raw)
                if envelope is None or envelope.role.value != "reference":
                    raise ExecutionError("Training requires raw REFERENCE evidence; SIGNAL cannot train this basis.")
                if not np.array_equal(raw["frequency_hz"][:], context.frequencies_hz):
                    raise ExecutionError("Training source frequency grid changed.")
                accepted = json.loads(point["metadata_json"].asstr()[()]).get("quantitative_accepted")
                if type(accepted) is not bool:
                    raise ExecutionError("Training source lacks an explicit contribution decision.")
                if accepted:
                    yield envelope, dbm_to_w(raw["power_dbm"][:])

        calibration = train_interference_basis(
            context, profile, frames, model_id=model_id, nuisance_mask=nuisance_mask,
            control_mask=control_mask, protected_mask=protected_mask, control_sigma_w=control_sigma_w,
            config=config, provenance={"source_archive_sha256": original_hash, "source_archive": str(source)},
            signal_control_regions_qualified=signal_control_regions_qualified,
            qualification_evidence=qualification_evidence, cancellation_check=cancellation_check,
        )
    if file_sha256(source, cancellation_check=cancellation_check) != original_hash:
        raise ExecutionError("Reference archive changed during model training.")
    if cancellation_check:
        cancellation_check()
    writer = Hdf5RunWriter(
        destination, recipe_source="schema_version: 1\nname: Reference-trained calibration\nsteps: []\n",
        settings_source=settings_source,
        plan_hash=calibration.content_hash, device_idn=device_idn, simulation_metadata=simulation_metadata,
        run_attributes={"interference_calibration_schema": "reference-trained-calibration-v1",
                        "source_archive_sha256": original_hash},
    )
    try:
        writer.store_background_profile(context, profile)
        writer.store_interference_calibration(calibration)
        trace = SpectrumTrace(tuple(context.frequencies_hz), tuple(10 * np.log10(profile.mean_w) + 30),
                              datetime.fromtimestamp(profile.completed_at_s, timezone.utc), "REFERENCE_MEAN")
        writer.append(MeasurementPoint(0, {}, {}, metadata={"derived_reference_mean": True,
                                                          "interference_model_hash": calibration.content_hash}), trace)
        if cancellation_check:
            cancellation_check()
        writer.close("completed")
    except Exception as exc:
        try:
            writer.close("aborted" if isinstance(exc, ProcessingCancelled) else "faulted")
        except Exception as close_error:
            exc.add_note(f"Closing the training artifact failed: {close_error}")
        raise
    return calibration


def train_from_specification(source, destination, specification, *, cancellation_check=None):
    """Shared CLI/worker entry: units, masks and memory checked before training."""
    from .hdf5_reader import Hdf5RunReader

    allowed = {"model_id", "nuisance_regions", "control_regions", "protected_regions", "control_sigma",
               "components", "maximum_training_frames", "signal_control_regions_qualified", "qualification_evidence"}
    if not isinstance(specification, dict) or set(specification) - allowed:
        raise ValueError("Unknown fields in the training specification.")
    if cancellation_check:
        cancellation_check()
    config = InterferenceTrainingConfig(components=specification.get("components", 2),
                                       maximum_training_frames=specification.get("maximum_training_frames", 64))
    with h5py.File(source, "r") as file:
        root = file.get("spectrum_processing_v1/profiles")
        if root is None or len(root) != 1:
            raise ValueError("Choose an archive containing exactly one reference profile.")
        group = next(iter(root.values()))
        count = json.loads(group.attrs["metadata_json"])["sweep_count"]
        if training_buffer_bytes(group["frequency_hz"].shape[0], count, config) > config.working_memory_limit_bytes:
            raise ValueError("Reference training buffers exceed the working-memory budget.")
    context, _profile = Hdf5RunReader.background_profiles(source)[0]
    masks = {name: frequency_region_mask(context.frequencies_hz, specification[name + "_regions"])
             for name in ("nuisance", "control", "protected")}
    sigma_w = parse_quantity(specification["control_sigma"], DIMENSION_POWER).si_value
    if sigma_w <= 0:
        raise ValueError("Control sigma must be a positive power quantity.")
    return train_interference_archive(source, destination, model_id=specification["model_id"],
        nuisance_mask=masks["nuisance"], control_mask=masks["control"], protected_mask=masks["protected"],
        control_sigma_w=np.full(context.frequencies_hz.size, sigma_w), config=config,
        signal_control_regions_qualified=specification.get("signal_control_regions_qualified", False),
        qualification_evidence=specification.get("qualification_evidence", ""), cancellation_check=cancellation_check)
