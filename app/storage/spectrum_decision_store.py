"""Private, committed decision records with an explicit raw-checkpoint boundary."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

import h5py

from app.domain.errors import ExecutionError
from app.domain.spectrum_correction import CorrectionConfig, SpectrumAcquisitionContext


SCHEMA = "spectrum-processing-decisions-v1"
ROOT = "spectrum_processing_v1/decisions"
MAX_DECISIONS = 65536
OPERATIONS = {"initialize", "set_profile", "begin_reference", "finish_reference", "cancel_reference",
              "reset_segment", "set_model", "status_at"}


def _json(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > 65536:
        raise ExecutionError("A processing decision exceeds the 64 KiB record limit.")
    return text


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _read_json(value):
    if type(value) is not str or len(value.encode("utf-8")) > 65536:
        raise ExecutionError("Processing decision JSON exceeds its bounded record contract.")
    return json.loads(value)


def initialize_decisions(writer, context, config):
    if writer._closed or ROOT in writer._file:
        raise ExecutionError("Decision history requires a new open processing archive.")
    metadata = {"context_id": context.context_id, "configuration_fingerprint": context.configuration_fingerprint,
        "configuration_generation": context.configuration_generation, "settings_verified": context.settings_verified,
        "independent_sweeps_qualified": context.independent_sweeps_qualified, "config": asdict(config)}
    encoded = _json(metadata)
    if context.frequencies_hz.size > 1048576:
        raise ExecutionError("Processing decision context exceeds its bounded frequency-grid budget.")
    def write_context(pending):
        pending.attrs.update({"schema": SCHEMA, "metadata_json": encoded, "metadata_sha256": _digest(metadata),
                              "count": 0, "last_sha256": ""})
        axis = pending.create_dataset("frequency_hz", data=context.frequencies_hz, dtype="f8")
        axis.attrs["unit"] = "Hz"
        pending.create_group("records")

    writer._file.require_group("spectrum_processing_v1")
    writer._commit_processing_record("decision_initial", ROOT, write_context, "processing decision context")


def append_decision(writer, operation, parameters):
    if writer._closed or operation not in OPERATIONS or type(parameters) is not dict:
        raise ExecutionError("Cannot append an unknown processing decision to this archive.")
    root = writer._file[ROOT]
    index = int(root.attrs["count"])
    if index != len(root["records"]) or index >= MAX_DECISIONS:
        raise ExecutionError("Processing decision history is inconsistent or exceeds its record budget.")
    record = {"ordinal": index, "before_point_index": writer.point_count,
        "utc": datetime.now(timezone.utc).isoformat(), "operation": operation,
        "parameters": parameters, "previous_sha256": str(root.attrs["last_sha256"])}
    encoded, identity = _json(record), _digest(record)
    pending_name = f"decision_{index}"
    destination = f"{ROOT}/records/{index:08d}"
    previous_hash = str(root.attrs["last_sha256"])
    if pending_name in writer._pending or destination in writer._file:
        raise ExecutionError("Processing decision transaction already exists; recover the archive.")
    try:
        pending = writer._pending.create_group(pending_name)
        pending.attrs.update({"record_json": encoded, "sha256": identity, "complete": True})
        writer._file.flush()
        writer._file.move(f"_pending/{pending_name}", destination)
        root.attrs["count"] = index + 1
        root.attrs["last_sha256"] = identity
        writer._file.flush()
    except Exception as exc:
        errors = []
        for key, value in (("count", index), ("last_sha256", previous_hash)):
            try:
                root.attrs[key] = value
            except Exception as rollback_exc:
                errors.append(f"{key}: {rollback_exc}")
        for path in (destination, f"_pending/{pending_name}"):
            try:
                if path in writer._file:
                    del writer._file[path]
            except Exception as rollback_exc:
                errors.append(f"{path}: {rollback_exc}")
        try:
            writer._file.flush()
        except Exception as rollback_exc:
            errors.append(f"flush: {rollback_exc}")
        detail = ""
        if errors:
            writer._storage_faulted = True
            detail = "; rollback failed: " + "; ".join(errors)
        raise ExecutionError(f"Could not commit processing decision: {exc}{detail}") from exc
    return identity


def read_decision_context(file):
    root = file[ROOT]
    if root.attrs.get("schema") != SCHEMA or not root.attrs.get("complete", False):
        raise ExecutionError("Unsupported or incomplete processing decision context.")
    metadata = _read_json(root.attrs["metadata_json"])
    if type(metadata) is not dict:
        raise ExecutionError("Processing decision context requires a metadata object.")
    if _digest(metadata) != root.attrs["metadata_sha256"]:
        raise ExecutionError("Processing decision context identity is corrupted.")
    axis = root["frequency_hz"]
    if (not isinstance(axis, h5py.Dataset) or axis.ndim != 1
            or axis.dtype.kind not in "fiu" or not 2 <= axis.shape[0] <= 1048576
            or axis.attrs.get("unit") != "Hz"):
        raise ExecutionError("Processing decision context requires a bounded SI frequency grid.")
    config = CorrectionConfig(**metadata.pop("config"))
    identity = metadata.pop("context_id")
    context = SpectrumAcquisitionContext(axis[:], **metadata)
    if context.context_id != identity:
        raise ExecutionError("Processing decision frequency/context identity is corrupted.")
    count = int(root.attrs["count"])
    if not 1 <= count <= MAX_DECISIONS or count != len(root["records"]):
        raise ExecutionError("Processing decision history has no complete contiguous initialization.")
    return context, config


def iter_decisions(file):
    root = file[ROOT]
    previous, boundary = "", 0
    for ordinal in range(int(root.attrs["count"])):
        group = root[f"records/{ordinal:08d}"]
        record = _read_json(group.attrs["record_json"])
        if type(record) is not dict or set(record) != {
                "ordinal", "before_point_index", "utc", "operation", "parameters", "previous_sha256"}:
            raise ExecutionError("Processing decision record differs from its schema.")
        identity = _digest(record)
        point = record["before_point_index"]
        if (not group.attrs.get("complete", False) or identity != group.attrs["sha256"]
                or record["previous_sha256"] != previous or type(record["ordinal"]) is not int
                or record["ordinal"] != ordinal or type(point) is not int or not boundary <= point <= len(file["points"])
                or record["operation"] not in OPERATIONS or type(record["parameters"]) is not dict
                or ordinal == 0 and (point != 0 or record["operation"] != "initialize")
                or ordinal != 0 and record["operation"] == "initialize"):
            raise ExecutionError("Processing decision order, boundary or identity is corrupted.")
        timestamp = datetime.fromisoformat(record["utc"])
        if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
            raise ExecutionError("Processing decision timestamp must have explicit UTC evidence.")
        previous, boundary = identity, point
        yield record
    if previous != root.attrs["last_sha256"]:
        raise ExecutionError("Processing decision terminal identity is corrupted.")
