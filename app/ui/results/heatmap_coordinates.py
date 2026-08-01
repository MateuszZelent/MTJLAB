"""Physical-coordinate reconstruction for read-only spectral heatmaps."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path

import numpy as np

from app.recipes.parameter_registry import parameter_descriptor
from app.storage import (
    StoredPoint,
    ThatecRow,
    ThatecRun,
    ThatecRunReader,
    ThatecSchemaMapper,
)


_FREQUENCY_ID = "frequency"
_CHECKPOINT_ID = "measurement.checkpoint"


@dataclass(frozen=True, slots=True)
class HeatmapDimension:
    """One selectable heatmap coordinate in canonical persisted units."""

    id: str
    label: str
    unit: str
    values: tuple[float, ...]
    is_frequency: bool = False


@dataclass(frozen=True, slots=True)
class HeatmapCoordinates:
    """Validated coordinates aligned one-to-one with spectral checkpoints."""

    dimensions: tuple[HeatmapDimension, ...]
    checkpoint_count: int
    fallback_reason: str | None = None

    def dimension(self, dimension_id: str) -> HeatmapDimension:
        for dimension in self.dimensions:
            if dimension.id == dimension_id:
                return dimension
        raise ValueError(f"Heatmap coordinate {dimension_id!r} is unavailable.")


@dataclass(frozen=True, slots=True)
class HeatmapRequest:
    """Two distinct axes and exact values for all remaining dimensions."""

    x_id: str
    y_id: str
    filters: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class HeatmapMatrix:
    """Colour values plus the immutable checkpoint represented by each cell."""

    values: np.ndarray
    x_values: np.ndarray
    y_values: np.ndarray
    cell_checkpoints: np.ndarray
    x_label: str
    x_unit: str
    y_label: str
    y_unit: str
    z_label: str
    z_unit: str
    missing_checkpoints: int


def build_heatmap_coordinates(
    path: Path,
    run: ThatecRun,
    spectrum_row: ThatecRow,
    points: Sequence[StoredPoint] = (),
) -> HeatmapCoordinates:
    """Build sweep coordinates without changing or guessing persisted values."""

    if len(spectrum_row.shape) != 2:
        raise ValueError(f"THATEC row {spectrum_row.id} is not a spectral matrix.")
    checkpoint_count = spectrum_row.shape[0]
    dimensions = _private_dimensions(points, run, checkpoint_count)
    if not dimensions:
        dimensions = _public_control_dimensions(path, run, checkpoint_count)
    if not dimensions:
        dimensions = _recipe_dimensions(run, checkpoint_count)
    fallback_reason: str | None = None
    if not dimensions:
        fallback_reason = (
            "No complete physical sweep coordinate is stored for this spectrum; "
            "using checkpoint order."
        )
        dimensions = (
            HeatmapDimension(
                _CHECKPOINT_ID,
                "Checkpoint",
                "",
                tuple(float(index) for index in range(checkpoint_count)),
            ),
        )
    return HeatmapCoordinates(
        dimensions=(
            HeatmapDimension(_FREQUENCY_ID, "Frequency", "Hz", (), True),
            *dimensions,
        ),
        checkpoint_count=checkpoint_count,
        fallback_reason=fallback_reason,
    )


def read_heatmap_matrix(
    path: Path,
    row: ThatecRow,
    coordinates: HeatmapCoordinates,
    request: HeatmapRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> HeatmapMatrix:
    """Build one exact coordinate plane; never aggregate duplicate checkpoints."""

    x_dimension = coordinates.dimension(request.x_id)
    y_dimension = coordinates.dimension(request.y_id)
    if x_dimension.id == y_dimension.id:
        raise ValueError("Heatmap X and Y axes must be different.")
    frequency_on_axis = x_dimension.is_frequency or y_dimension.is_frequency
    if not frequency_on_axis and _FREQUENCY_ID not in request.filters:
        raise ValueError(
            "Choose one exact Frequency filter when Frequency is not a heatmap axis."
        )
    checkpoint_dimensions = tuple(
        dimension for dimension in coordinates.dimensions if not dimension.is_frequency
    )
    filter_dimensions = tuple(
        dimension
        for dimension in checkpoint_dimensions
        if dimension.id not in {x_dimension.id, y_dimension.id}
    )
    missing_filters = [
        dimension.label
        for dimension in filter_dimensions
        if dimension.id not in request.filters
    ]
    if missing_filters:
        raise ValueError(
            "Choose an exact filter value for " + ", ".join(missing_filters) + "."
        )
    selected = _selected_checkpoints(coordinates, filter_dimensions, request.filters)
    if not selected:
        raise ValueError("No stored checkpoint matches the selected heatmap filters.")

    traces: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    x_frequency: np.ndarray | None = None
    z_label = "Amplitude"
    z_unit = ""
    missing = 0
    for checkpoint in selected:
        if cancelled is not None and cancelled():
            raise RuntimeError("Heatmap read cancelled.")
        try:
            spectrum = ThatecRunReader.spectrum_slice(path, row.id, checkpoint)
        except Exception:
            missing += 1
            continue
        if len(spectrum.traces) != 1:
            raise ValueError(
                f"THATEC row {row.id} has multiple trace components; inspect it in Spectrum."
            )
        frequencies = np.asarray(spectrum.x_values, dtype=float)
        values = np.asarray(spectrum.traces[0].values, dtype=float)
        if frequencies.ndim != 1 or values.ndim != 1 or frequencies.shape != values.shape:
            raise ValueError(f"THATEC row {row.id} has an invalid spectral grid.")
        if x_frequency is None:
            x_frequency = frequencies
            z_label = spectrum.y_label
            z_unit = spectrum.y_unit
        elif not np.array_equal(x_frequency, frequencies):
            raise ValueError("Spectrum frequency grids differ between selected checkpoints.")
        traces[checkpoint] = (frequencies, values)
    if x_frequency is None:
        raise ValueError("No readable spectrum matches the selected heatmap filters.")

    if frequency_on_axis:
        varying_dimension = y_dimension if x_dimension.is_frequency else x_dimension
        axis_values = _unique_values(varying_dimension, selected)
        matrix = np.full((len(axis_values), len(x_frequency)), np.nan, dtype=float)
        cell_checkpoints = np.full(matrix.shape, -1, dtype=int)
        for checkpoint, (_frequencies, values) in traces.items():
            coordinate = _checkpoint_value(varying_dimension, checkpoint)
            row_index = _value_index(axis_values, coordinate)
            if np.any(cell_checkpoints[row_index] >= 0):
                raise ValueError(
                    "Multiple checkpoints map to one heatmap cell; refine the filters."
                )
            matrix[row_index, :] = values
            cell_checkpoints[row_index, :] = checkpoint
        if x_dimension.is_frequency:
            x_values, y_values = x_frequency, axis_values
        else:
            matrix = matrix.T
            cell_checkpoints = cell_checkpoints.T
            x_values, y_values = axis_values, x_frequency
    else:
        frequency = float(request.filters[_FREQUENCY_ID])
        frequency_index = _value_index(x_frequency, frequency)
        x_values = _unique_values(x_dimension, selected)
        y_values = _unique_values(y_dimension, selected)
        matrix = np.full((len(y_values), len(x_values)), np.nan, dtype=float)
        cell_checkpoints = np.full(matrix.shape, -1, dtype=int)
        for checkpoint, (_frequencies, values) in traces.items():
            x_index = _value_index(x_values, _checkpoint_value(x_dimension, checkpoint))
            y_index = _value_index(y_values, _checkpoint_value(y_dimension, checkpoint))
            if cell_checkpoints[y_index, x_index] >= 0:
                raise ValueError(
                    "Multiple checkpoints map to one heatmap cell; refine the filters."
                )
            matrix[y_index, x_index] = values[frequency_index]
            cell_checkpoints[y_index, x_index] = checkpoint

    finite = matrix[np.isfinite(matrix)]
    if finite.size == 0:
        raise ValueError("The selected heatmap plane contains no finite spectrum values.")
    return HeatmapMatrix(
        values=matrix,
        x_values=x_values,
        y_values=y_values,
        cell_checkpoints=cell_checkpoints,
        x_label=x_dimension.label,
        x_unit=x_dimension.unit,
        y_label=y_dimension.label,
        y_unit=y_dimension.unit,
        z_label=z_label,
        z_unit=z_unit,
        missing_checkpoints=missing,
    )


def _private_dimensions(
    points: Sequence[StoredPoint], run: ThatecRun, checkpoint_count: int
) -> tuple[HeatmapDimension, ...]:
    if len(points) != checkpoint_count:
        return ()
    keys = set.intersection(*(set(point.setpoints) for point in points)) if points else set()
    # Stored setpoints are authoritative, while the recipe (when available)
    # retains the deliberately chosen outer-to-inner nesting order.
    ordered_keys = _ordered_private_keys(keys, run, checkpoint_count)
    dimensions: list[HeatmapDimension] = []
    for key in ordered_keys:
        values = tuple(float(point.setpoints[key]) for point in points)
        if not _finite_and_varying(values):
            continue
        dimensions.append(_dimension_for_key(key, values))
    return tuple(dimensions)


def _ordered_private_keys(
    keys: set[str], run: ThatecRun, checkpoint_count: int
) -> tuple[str, ...]:
    try:
        schema = ThatecSchemaMapper.from_recipe_source(
            run.recipe_source, expected_points=checkpoint_count
        )
    except Exception:
        schema = None
    recipe_keys = (
        tuple(axis.target for axis in schema.axes if axis.target in keys)
        if schema is not None and schema.mode == "recipe_sweeps"
        else ()
    )
    return (*recipe_keys, *(key for key in sorted(keys) if key not in recipe_keys))


def _public_control_dimensions(
    path: Path, run: ThatecRun, checkpoint_count: int
) -> tuple[HeatmapDimension, ...]:
    dimensions: list[HeatmapDimension] = []
    for row in run.rows.values():
        if row.shape != (checkpoint_count,) or "control" not in row.function.lower():
            continue
        try:
            values, _timestamps = ThatecRunReader.scalar_series(path, row.id)
            coordinate_values = tuple(float(value) for value in values)
        except (TypeError, ValueError):
            continue
        if not _finite_and_varying(coordinate_values):
            continue
        label, unit = _label_and_unit(row.control_name or row.id)
        dimensions.append(HeatmapDimension(f"public:{row.id}", label, unit, coordinate_values))
    return tuple(dimensions)


def _recipe_dimensions(run: ThatecRun, checkpoint_count: int) -> tuple[HeatmapDimension, ...]:
    schema = ThatecSchemaMapper.from_recipe_source(
        run.recipe_source, expected_points=checkpoint_count
    )
    if schema.mode != "recipe_sweeps" or schema.point_count != checkpoint_count:
        return ()
    meshes = np.meshgrid(*(axis.values_si for axis in schema.axes), indexing="ij")
    return tuple(
        HeatmapDimension(
            axis.target,
            axis.control_name,
            axis.unit,
            tuple(float(value) for value in meshes[index].ravel()),
        )
        for index, axis in enumerate(schema.axes)
        if _finite_and_varying(tuple(float(value) for value in meshes[index].ravel()))
    )


def _dimension_for_key(key: str, values: tuple[float, ...]) -> HeatmapDimension:
    try:
        descriptor = parameter_descriptor(key)
    except KeyError:
        return HeatmapDimension(key, key, "", values)
    return HeatmapDimension(key, descriptor.control_name, descriptor.unit, values)


def _label_and_unit(value: str) -> tuple[str, str]:
    if value.endswith(")") and " (" in value:
        label, unit = value.rsplit(" (", 1)
        return label, unit[:-1]
    return value, ""


def _finite_and_varying(values: tuple[float, ...]) -> bool:
    return bool(values) and all(math.isfinite(value) for value in values) and len(set(values)) > 1


def _selected_checkpoints(
    coordinates: HeatmapCoordinates,
    dimensions: tuple[HeatmapDimension, ...],
    filters: Mapping[str, float],
) -> tuple[int, ...]:
    selected = []
    for checkpoint in range(coordinates.checkpoint_count):
        if all(
            _values_equal(_checkpoint_value(dimension, checkpoint), float(filters[dimension.id]))
            for dimension in dimensions
        ):
            selected.append(checkpoint)
    return tuple(selected)


def _checkpoint_value(dimension: HeatmapDimension, checkpoint: int) -> float:
    if dimension.is_frequency:
        raise ValueError("Frequency does not have one value per checkpoint.")
    return dimension.values[checkpoint]


def _unique_values(dimension: HeatmapDimension, checkpoints: Sequence[int]) -> np.ndarray:
    return np.asarray(sorted({_checkpoint_value(dimension, checkpoint) for checkpoint in checkpoints}), dtype=float)


def _value_index(values: np.ndarray, value: float) -> int:
    matches = np.flatnonzero(np.isclose(values, value, rtol=1e-9, atol=1e-15))
    if len(matches) != 1:
        raise ValueError(f"No exact stored coordinate matches {value:.12g}.")
    return int(matches[0])


def _values_equal(left: float, right: float) -> bool:
    return bool(np.isclose(left, right, rtol=1e-9, atol=1e-15))
