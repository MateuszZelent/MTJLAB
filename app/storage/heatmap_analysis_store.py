"""Standalone derived heatmap snapshots; never a replacement for a run archive."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path

import h5py
import numpy as np

SCHEMA = "pylab.heatmap-analysis.v1"


@dataclass(frozen=True, slots=True)
class StoredHeatmapAnalysis:
    values: np.ndarray
    x_values: np.ndarray
    y_values: np.ndarray
    checkpoints: np.ndarray
    metadata: dict
    x_edges: np.ndarray | None = None
    y_edges: np.ndarray | None = None


def validate_cell_edges(axis, edges):
    edges = np.asarray(edges, dtype=float)
    if edges.shape != (axis.size + 1,) or not np.isfinite(edges).all():
        raise ValueError("Heatmap cell edges must be finite and match the axis.")
    if not (np.all(np.diff(edges) > 0) or np.all(np.diff(edges) < 0)):
        raise ValueError("Heatmap cell edges must be strictly monotonic.")
    if not np.all((axis >= np.minimum(edges[:-1], edges[1:])) &
                  (axis <= np.maximum(edges[:-1], edges[1:]))):
        raise ValueError("Heatmap coordinate is outside its cell edges.")
    return edges


def _validate(values, x, y, checkpoints, metadata):
    if values.ndim != 2 or values.shape != (y.size, x.size) or not x.size or not y.size:
        raise ValueError("Heatmap matrix and axes have incompatible shapes.")
    if x.ndim != 1 or y.ndim != 1 or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("Heatmap axes must be finite vectors.")
    for axis in (x, y):
        if axis.size > 1 and not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0)):
            raise ValueError("Heatmap axes must be strictly monotonic.")
    if np.isinf(values).any():
        raise ValueError("Heatmap values cannot be infinite; gaps may be NaN.")
    if checkpoints.shape != values.shape or checkpoints.dtype.kind not in "iu" or (checkpoints < -1).any():
        raise ValueError("Invalid source checkpoint map.")
    for axis in ("x", "y"):
        definition = metadata.get("axes", {}).get(axis)
        if (not isinstance(definition, dict) or not isinstance(definition.get("label"), str)
                or not isinstance(definition.get("unit", ""), str)):
            raise ValueError("Heatmap axis metadata is missing.")
    if not isinstance(metadata.get("value_unit"), str):
        raise ValueError("Heatmap value unit is missing.")
    if "view_ranges" in metadata:
        bounds = np.asarray(metadata["view_ranges"], dtype=float)
        if bounds.shape != (2, 2) or not np.isfinite(bounds).all() or np.any(bounds[:, 0] >= bounds[:, 1]):
            raise ValueError("Invalid stored heatmap view ranges.")
    if "colour_levels" in metadata:
        bounds = np.asarray(metadata["colour_levels"], dtype=float)
        if bounds.shape != (2,) or not np.isfinite(bounds).all() or bounds[0] > bounds[1]:
            raise ValueError("Invalid stored colour levels.")
    json.dumps(metadata, allow_nan=False)


def write_heatmap_analysis(path, values, x_values, y_values, checkpoints, metadata, *, x_edges=None, y_edges=None):
    """Exclusive, compressed snapshot with units, original checkpoint IDs and gaps."""
    path = Path(path)
    values, x, y = (np.asarray(item, dtype=float) for item in (values, x_values, y_values))
    checkpoints = np.asarray(checkpoints)
    _validate(values, x, y, checkpoints, metadata)
    edges = {key: validate_cell_edges(axis, edge) for key, axis, edge in
             (("x_edges", x, x_edges), ("y_edges", y, y_edges)) if edge is not None}
    source = metadata.get("source_file")
    if source and path.resolve() == Path(source).resolve():
        raise ValueError("The source measurement archive is read-only.")
    encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False, sort_keys=True)
    created = False
    try:
        with h5py.File(path, "x") as file:
            created = True
            file.attrs.update(schema=SCHEMA, status="writing", created_at_utc=datetime.now(UTC).isoformat())
            file.attrs["source_data_modified"] = False
            group = file.create_group("heatmap")
            for name, data, unit in (("values", values, metadata["value_unit"]),
                                    ("x", x, metadata["axes"]["x"].get("unit", "")),
                                    ("y", y, metadata["axes"]["y"].get("unit", "")),
                                    ("source_checkpoints", checkpoints, "index")):
                dataset = group.create_dataset(name, data=data, compression="gzip", shuffle=True)
                dataset.attrs["unit"] = unit
            file.create_dataset("metadata_json", data=encoded, dtype=h5py.string_dtype("utf-8"))
            for key, edge in edges.items():
                group.create_dataset(key, data=edge).attrs["unit"] = metadata["axes"][key[0]].get("unit", "")
            file.flush()
            file.attrs["status"] = "completed"
            file.flush()
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def read_heatmap_analysis(path):
    """Return None for measurement archives; reject incomplete derived snapshots."""
    with h5py.File(path, "r") as file:
        if file.attrs.get("schema") != SCHEMA:
            return None
        if file.attrs.get("status") != "completed":
            raise ValueError("The derived heatmap export is incomplete.")
        group = file["heatmap"]
        if any(group[key].dtype.kind not in "fiu" for key in ("values", "x", "y")):
            raise ValueError("Derived heatmap axes and values must be numeric datasets.")
        values, x, y, checkpoints = (group[key][...] for key in ("values", "x", "y", "source_checkpoints"))
        metadata = json.loads(file["metadata_json"].asstr()[()])
        _validate(values, x, y, checkpoints, metadata)
        for key, unit in (("values", metadata["value_unit"]),
                          ("x", metadata["axes"]["x"].get("unit", "")),
                          ("y", metadata["axes"]["y"].get("unit", ""))):
            if group[key].attrs.get("unit") != unit:
                raise ValueError("Derived heatmap units disagree with their metadata.")
        edges = {}
        for key, axis in (("x_edges", x), ("y_edges", y)):
            if key in group:
                edges[key] = validate_cell_edges(axis, group[key][...])
                if group[key].attrs.get("unit") != metadata["axes"][key[0]].get("unit", ""):
                    raise ValueError("Derived cell-edge units disagree with their metadata.")
        return StoredHeatmapAnalysis(values, x, y, checkpoints, metadata, **edges)
