"""Immutable, checksummed final blocks separate from provisional checkpoints."""

import hashlib
import json

import numpy as np

from app.domain.errors import ExecutionError
from app.spectrum.finalize import FinalizedSpectrumBlock
from .spectrum_correction_codec import read_corrected, write_corrected

SCHEMA = "spectrum-finalized-block-v1"


def block_hash(block, source_point_indices):
    result = block.result
    digest = hashlib.sha256(json.dumps({
        "source_frame_ids": block.source_frame_ids,
        "source_point_indices": tuple(source_point_indices),
        "result": {field: getattr(result, field) for field in (
            "frame_id", "segment_id", "context_id", "processing_generation", "count",
            "started_at_s", "completed_at_s", "reference_age_s", "profile_weights",
            "quality", "average_mode", "final", "algorithm_version",
        )},
        "uncertainty_known": result.standard_uncertainty_w is not None,
    }, sort_keys=True, allow_nan=False).encode())
    for values in (block.raw_mean_w, result.values_w, result.standard_uncertainty_w):
        if values is not None:
            digest.update(values.tobytes())
    return digest.hexdigest()


def write_finalized(group, block, source_point_indices):
    group.attrs["schema"] = SCHEMA
    group.attrs["content_hash"] = block_hash(block, source_point_indices)
    for name, values in (("source_frame_ids", block.source_frame_ids),
                         ("source_point_indices", source_point_indices)):
        group.create_dataset(name, data=values, dtype="i8")
    dataset = group.create_dataset("raw_mean_w", data=block.raw_mean_w, dtype="f8")
    dataset.attrs["unit"] = "W"
    write_corrected(group.create_group("result"), block.result)


def read_finalized(group):
    if group.attrs.get("schema") != SCHEMA or not group.attrs.get("complete", False):
        raise ExecutionError("Unknown or uncommitted finalized spectrum block.")
    try:
        dataset = group["raw_mean_w"]
        if dataset.attrs.get("unit") != "W":
            raise ExecutionError("Finalized raw block mean requires W units.")
        for name in ("source_frame_ids", "source_point_indices"):
            if group[name].ndim != 1 or group[name].dtype.kind not in "iu":
                raise ExecutionError("Finalized sources require integer vectors.")
        points = tuple(int(index) for index in group["source_point_indices"][:])
        block = FinalizedSpectrumBlock(read_corrected(group["result"]),
                                      tuple(int(index) for index in group["source_frame_ids"][:]), dataset[:])
        if len(points) != block.result.count or any(index < 0 for index in points) or any(
            first >= second for first, second in zip(points, points[1:])
        ):
            raise ExecutionError("Finalized source checkpoints are invalid.")
        if block_hash(block, points) != group.attrs["content_hash"]:
            raise ExecutionError("Finalized spectrum block checksum is corrupted.")
        if not np.isfinite(block.raw_mean_w).all():
            raise ExecutionError("Finalized block mean is not finite.")
        return block, points
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Malformed finalized spectrum block: {exc}") from exc
