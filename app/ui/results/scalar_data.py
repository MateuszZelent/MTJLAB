"""Committed scalar samples and sweep coordinates, independent of Qt."""
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.recipes.parameter_registry import parameter_descriptor
from app.storage import ThatecRunReader


@dataclass(frozen=True)
class ScalarColumn:
    id: str
    label: str
    unit: str
    role: str
    values: np.ndarray


def label_unit(text):
    if text.endswith(")") and " (" in text:
        label, unit = text.rsplit(" (", 1)
        return label, unit[:-1]
    return text, ""


def read_scalar_columns(path: Path, run, points=(), *, cancelled=None):
    """Read only scalar rows; never read spectral bins or modify the archive."""
    public = {}
    for row in run.rows.values():
        if cancelled and cancelled():
            raise RuntimeError("Scalar read cancelled")
        if len(row.shape) != 1:
            continue
        # Private checkpoint data are already loaded by the Results worker.
        # Use public definitions for units without reading a second full copy.
        values = np.empty(0) if points else ThatecRunReader.scalar_series(path, row.id)[0]
        if np.iscomplexobj(values):
            continue
        definition = dict(row.definition)
        role = definition.get("lab control role", "setpoint" if "control" in row.function.lower() else "measurement")
        if role not in {"setpoint", "measurement"} and "control" not in row.function.lower():
            continue
        key = definition.get("lab control key", f"public:{row.id}")
        label, unit = label_unit(row.control_name or row.id)
        public[(role, key)] = ScalarColumn(f"{role}:{key}", f"{row.device_name} · {label}", unit,
                                          "setpoint" if "control" in row.function.lower() else role,
                                          np.asarray(values, dtype=float))
    if points:
        columns = []
        for role, attribute in (("setpoint", "setpoints"), ("measurement", "measurements")):
            keys = sorted(set().union(*(getattr(point, attribute) for point in points)))
            for key in keys:
                match = public.get((role, key))
                if match:
                    label, unit = match.label, match.unit
                else:
                    try:
                        descriptor = parameter_descriptor(key)
                        label, unit = descriptor.control_name, descriptor.unit
                    except KeyError:
                        label, unit = key, ""
                columns.append(ScalarColumn(f"{role}:{key}", label, unit, role,
                                            np.asarray([getattr(point, attribute).get(key, np.nan) for point in points], dtype=float)))
        count = len(points)
        indices = np.asarray([point.index for point in points], dtype=float)
    else:
        columns = list(public.values())
        count = max((len(column.values) for column in columns), default=0)
        indices = np.arange(count, dtype=float)
        columns = [ScalarColumn(column.id, column.label, column.unit, column.role,
                                np.pad(column.values, (0, count - len(column.values)), constant_values=np.nan))
                   for column in columns]
    return (ScalarColumn("checkpoint", "Checkpoint", "", "setpoint", indices), *columns)


def scalar_curves(x, y, group=None):
    """Keep acquisition order and split repeated coordinates into distinct passes."""
    if len(x.values) != len(y.values) or (group is not None and len(group.values) != len(x.values)):
        raise ValueError("Scalar coordinates are not aligned to the same checkpoints.")
    buckets = {}
    seen = {}
    for index, (xv, yv) in enumerate(zip(x.values, y.values, strict=True)):
        gv = float(group.values[index]) if group else None
        if not np.isfinite(xv) or not np.isfinite(yv) or (gv is not None and not np.isfinite(gv)):
            # Missing measurements must not create a line bridging the gap.
            for passes in buckets.values():
                if passes[-1]:
                    passes.append([])
            seen.clear()
            continue
        passes = buckets.setdefault(gv, [[]])
        current = passes[-1]
        visited = seen.setdefault(gv, set())
        if xv in visited:
            current = []
            passes.append(current)
            visited.clear()
        current.append(index)
        visited.add(xv)
    return tuple((value, pass_index + 1, np.asarray(indices, dtype=int))
                 for value, passes in buckets.items() for pass_index, indices in enumerate(passes) if indices)
