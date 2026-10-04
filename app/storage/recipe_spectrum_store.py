"""Private raw recipe sweeps committed independently of averaged checkpoints."""

from dataclasses import fields
import hashlib
import json

import h5py
import numpy as np

from app.domain.errors import ExecutionError
from app.domain.recipe_spectrum import MAX_RECIPE_SWEEP_JSON_BYTES, RecipeSpectrumSweep
from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence


ROOT = "recipe_raw_sweeps_v1"
GRID_ROOT = "recipe_raw_grids_v1"
SCHEMA = "recipe-raw-sweeps-v1"


def _encoded(record, ordinal, boundary):
    metadata = {field.name: getattr(record, field.name) for field in fields(record)
                if field.name not in {"frequencies_hz", "powers_dbm"}}
    metadata.update(ordinal=ordinal, checkpoint_count_at_capture=boundary)
    text = json.dumps(metadata, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_RECIPE_SWEEP_JSON_BYTES:
        raise ExecutionError("Recipe sweep metadata exceeds its 64 KiB budget.")
    return text


def _identity(text, record):
    digest = hashlib.sha256(text.encode("utf-8"))
    digest.update(record.frequencies_hz.astype("<f8", copy=False).tobytes())
    digest.update(record.powers_dbm.astype("<f8", copy=False).tobytes())
    return digest.hexdigest()


def append_recipe_sweep(writer, record):
    if writer._closed or not isinstance(record, RecipeSpectrumSweep):
        raise ExecutionError("Recipe raw sweep requires an immutable source record and an open archive.")
    root = writer._file.get(ROOT)
    if root is not None and root.attrs.get("schema") != SCHEMA:
        raise ExecutionError("Unsupported recipe raw sweep archive schema.")
    ordinal = 0 if root is None else len(root)
    if ordinal and str(ordinal - 1) not in root:
        raise ExecutionError("Recipe raw sweep archive is discontinuous.")
    encoded = _encoded(record, ordinal, writer.point_count)
    root = writer._file.require_group(ROOT)
    pending_name = f"recipe_sweep_{ordinal}"
    grid_identity = hashlib.sha256(record.frequencies_hz.astype("<f8", copy=False).tobytes()).hexdigest()
    pending_grid = f"recipe_grid_{grid_identity}"
    try:
        root.attrs["schema"] = SCHEMA
        grids = writer._file.require_group(GRID_ROOT)
        if grid_identity not in grids:
            axis = writer._pending.create_dataset(pending_grid, data=record.frequencies_hz, dtype="f8")
            axis.attrs.update(unit="Hz", sha256=grid_identity, complete=True)
            writer._file.flush()
            writer._file.move(f"_pending/{pending_grid}", f"{GRID_ROOT}/{grid_identity}")
        axis = grids[grid_identity]
        if (not isinstance(axis, h5py.Dataset) or axis.shape != record.frequencies_hz.shape
                or axis.dtype.kind not in "fiu" or axis.attrs.get("unit") != "Hz"
                or not bool(axis.attrs.get("complete", False))
                or axis.attrs.get("sha256") != grid_identity
                or not np.array_equal(axis[:], record.frequencies_hz)):
            raise ExecutionError("Committed recipe frequency grid differs from its content identity.")
        pending = writer._pending.create_group(pending_name)
        pending.attrs.update(metadata_json=encoded, sha256=_identity(encoded, record), complete=True)
        # Source records retain every bin; compression is deliberately absent
        # to avoid a second gzip pass inside recipe acquisition.
        pending["frequency_hz"] = axis  # HDF5 hard link; no repeated frequency payload.
        pending.create_dataset("power_dbm", data=record.powers_dbm, dtype="f8").attrs["unit"] = "dBm"
        writer._file.flush()
        writer._file.move(f"_pending/{pending_name}", f"{ROOT}/{ordinal}")
        writer._file.flush()
    except Exception as exc:
        if pending_name in writer._pending:
            del writer._pending[pending_name]
        if pending_grid in writer._pending:
            del writer._pending[pending_grid]
        writer._file.flush()
        raise ExecutionError(f"Could not commit recipe raw sweep: {exc}") from exc
    return ordinal


def iter_recipe_sweeps(file):
    if ROOT not in file:
        return
    root = file[ROOT]
    if root.attrs.get("schema") != SCHEMA:
        raise ExecutionError("Unsupported recipe raw sweep archive schema.")
    boundaries = {}
    try:
        for ordinal in range(len(root)):
            group = root[str(ordinal)]
            text = group.attrs["metadata_json"]
            complete = group.attrs.get("complete")
            if (type(text) is not str or len(text.encode("utf-8")) > MAX_RECIPE_SWEEP_JSON_BYTES
                    or not isinstance(complete, (bool, np.bool_)) or not complete):
                raise ExecutionError("Recipe raw sweep metadata is incomplete or exceeds its budget.")
            metadata = json.loads(text)
            if (type(metadata) is not dict or type(metadata.get("ordinal")) is not int
                    or metadata.pop("ordinal") != ordinal):
                raise ExecutionError("Recipe raw sweep identity differs from its archive position.")
            point = metadata.pop("checkpoint_count_at_capture")
            execution = metadata.get("execution_id")
            if type(execution) is not str or not execution:
                raise ExecutionError("Recipe raw sweep has no execution identity.")
            if execution not in boundaries and len(boundaries) >= 4096:
                raise ExecutionError("Recipe raw sweep history exceeds its execution-attempt budget.")
            # A recovery can roll public points back to an earlier safe
            # boundary. Preserve sources from the abandoned attempt; this
            # historical count is not a pointer into the current point tree.
            if type(point) is not int or not boundaries.get(execution, 0) <= point <= 2**63 - 1:
                raise ExecutionError("Recipe raw sweep checkpoint boundary is inconsistent.")
            for name, unit in (("frequency_hz", "Hz"), ("power_dbm", "dBm")):
                axis = group[name]
                if (not isinstance(axis, h5py.Dataset) or axis.ndim != 1 or axis.dtype.kind not in "fiu"
                        or not 2 <= axis.shape[0] <= 1048576 or axis.attrs.get("unit") != unit):
                    raise ExecutionError("Recipe raw sweep requires bounded arrays with explicit Hz/dBm units.")
            metadata["role"] = SpectrumFrameRole(metadata["role"])
            metadata["evidence"] = SweepEvidence(metadata["evidence"])
            metadata["setpoints_si"] = tuple(tuple(pair) for pair in metadata["setpoints_si"])
            record = RecipeSpectrumSweep(group["frequency_hz"][:], group["power_dbm"][:], **metadata)
            if _identity(text, record) != group.attrs["sha256"]:
                raise ExecutionError("Recipe raw sweep content identity is corrupted.")
            boundaries[execution] = point
            yield ordinal, point, record
    except (KeyError, ValueError, TypeError) as exc:
        raise ExecutionError(f"Malformed recipe raw sweep archive: {exc}") from exc
