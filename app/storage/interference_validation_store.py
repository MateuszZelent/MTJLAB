"""Read-only validation sources and exclusive atomic JSON publication."""

import json
import os
from pathlib import Path
import tempfile

import h5py
import numpy as np

from app.domain.errors import ExecutionError
from app.spectrum.frequency_regions import frequency_region_mask
from app.spectrum.interference_validation import validate_interference_references
from app.spectrum.streaming_statistics import dbm_to_w
from .finalized_spectrum_store import file_sha256
from .hdf5_reader import Hdf5RunReader
from .spectrum_correction_codec import read_envelope, read_profile


def validate_interference_archive(model_source, reference_source, destination, *, model_id=None,
                                  validation_mask=None, validation_regions=None, cancellation_check=None):
    if validation_mask is not None and validation_regions is not None:
        raise ValueError("Choose validation bins or frequency regions, not both.")
    model_source, reference_source, destination = map(lambda path: Path(path).resolve(),
                                                       (model_source, reference_source, destination))
    if destination in (model_source, reference_source) or destination.exists():
        raise ExecutionError("Validation needs a new report separate from its source files.")
    source_hashes = {path: file_sha256(path, cancellation_check=cancellation_check)
                     for path in (model_source, reference_source)}
    with h5py.File(model_source, "r") as file:
        if file["run"].attrs.get("status") not in {"completed", "aborted"} or len(file["_pending"]):
            raise ExecutionError("Validation requires a closed committed model archive.")
        root = file.get("spectrum_processing_v1/interference_models")
        if root is None or (model_id is None and len(root) != 1) or (model_id is not None and model_id not in root):
            raise ExecutionError("Choose exactly one committed interference model.")
        selected = root[model_id] if model_id is not None else next(iter(root.values()))
        selected_id = str(json.loads(selected.attrs["metadata_json"])["model_id"])
        if selected_id != selected.name.rsplit("/", 1)[-1]:
            raise ExecutionError("Interference model key differs from its recorded identity.")
        points = selected["baseline_w"].shape[0]
        if points * 64 * 8 > 64 * 1024 * 1024:
            raise ExecutionError("Validation vectors exceed the 64 MiB buffer budget.")
    (calibration,) = Hdf5RunReader.interference_calibrations(model_source, model_id=selected_id)
    provenance = json.loads(calibration.training_provenance_json or "{}")
    if not provenance.get("source_archive_sha256"):
        raise ExecutionError("Calibration lacks raw training provenance; regenerate it.")
    if provenance["source_archive_sha256"] == source_hashes[reference_source]:
        raise ExecutionError("Validation cannot reuse the raw training archive.")
    if validation_regions is not None:
        validation_mask = frequency_region_mask(calibration.context.frequencies_hz, validation_regions)
    elif validation_mask is None:
        validation_mask = calibration.protected_mask
    with h5py.File(reference_source, "r") as file:
        if file["run"].attrs.get("status") != "completed" or len(file["_pending"]):
            raise ExecutionError("Validation requires a completed raw REF acquisition.")
        root = file.get("spectrum_processing_v1/profiles")
        if root is None or len(root) != 1:
            raise ExecutionError("Validation requires exactly one committed reference profile.")
        group = next(iter(root.values()))
        if group["frequency_hz"].shape != (points,):
            raise ExecutionError("Validation reference frequency grid size differs from calibration.")
        context, profile = read_profile(group)
        if context.context_id != calibration.context.context_id:
            raise ExecutionError("Validation REF configuration differs from calibration.")

        def frames():
            for index in range(len(file["points"])):
                if cancellation_check:
                    cancellation_check()
                point = file.get(f"points/{index}")
                if point is None or not point.attrs.get("complete", False):
                    raise ExecutionError("Validation encountered an uncommitted raw checkpoint.")
                raw = file[f"spectra/{index}"]
                if raw["frequency_hz"].shape != (points,) or raw["power_dbm"].shape != (points,):
                    raise ExecutionError("Validation raw vector dimensions differ from calibration.")
                envelope = read_envelope(raw)
                if envelope is None or envelope.role.value != "reference":
                    raise ExecutionError("Validation requires raw REFERENCE evidence.")
                if not np.array_equal(raw["frequency_hz"][:], context.frequencies_hz):
                    raise ExecutionError("Validation raw frequency grid changed.")
                accepted = json.loads(point["metadata_json"].asstr()[()]).get("quantitative_accepted")
                if type(accepted) is not bool:
                    raise ExecutionError("Validation raw lacks an explicit contribution decision.")
                if accepted:
                    yield envelope, dbm_to_w(raw["power_dbm"][:])

        report = validate_interference_references(calibration, profile, frames(), validation_mask,
                                                  reference_context=context,
                                                  cancellation_check=cancellation_check)
    report["source_sha256"] = {str(path): digest for path, digest in source_hashes.items()}
    for path, digest in source_hashes.items():
        if file_sha256(path, cancellation_check=cancellation_check) != digest:
            raise ExecutionError("Validation source changed during analysis.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".pending", dir=destination.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if cancellation_check:
            cancellation_check()
        os.link(pending, destination)
    finally:
        pending.unlink(missing_ok=True)
    return report
