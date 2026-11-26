"""Physical-coordinate reconstruction for read-only spectral heatmaps."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeAlias

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
VISIT_DIMENSION_ID = "measurement.visit"
SERIES_DIMENSION_ID = "measurement.series"


@dataclass(frozen=True, slots=True)
class HeatmapDimension:
    """One selectable heatmap coordinate in canonical persisted units."""

    id: str
    label: str
    unit: str
    values: tuple[float, ...]
    is_frequency: bool = False

    @property
    def is_constant(self) -> bool:
        return bool(self.values) and all(value == self.values[0] for value in self.values)


@dataclass(frozen=True, slots=True)
class HeatmapCoordinates:
    """Validated coordinates aligned one-to-one with spectral checkpoints."""

    dimensions: tuple[HeatmapDimension, ...]
    checkpoint_count: int
    fallback_reason: str | None = None
    sweep_dimension_ids: tuple[str, ...] = ()

    @property
    def axis_dimensions(self) -> tuple[HeatmapDimension, ...]:
        return tuple(
            dimension for dimension in self.dimensions
            if dimension.id != SERIES_DIMENSION_ID
            and (dimension.is_frequency or dimension.id == _CHECKPOINT_ID or not dimension.is_constant)
        )

    def dimension(self, dimension_id: str) -> HeatmapDimension:
        for dimension in self.dimensions:
            if dimension.id == dimension_id:
                return dimension
        raise ValueError(f"Heatmap coordinate {dimension_id!r} is unavailable.")


@dataclass(frozen=True, slots=True)
class HeatmapRange:
    """Inclusive range of already-normalised coordinate values."""

    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        minimum = float(self.minimum)
        maximum = float(self.maximum)
        if not math.isfinite(minimum) or not math.isfinite(maximum):
            raise ValueError("Heatmap range limits must be finite.")
        if minimum > maximum:
            raise ValueError(
                "Heatmap range minimum must not exceed the maximum."
            )
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)


HeatmapSelection: TypeAlias = float | HeatmapRange | tuple[float, float]


@dataclass(frozen=True, slots=True)
class HeatmapRequest:
    """Two distinct axes and inclusive selections for remaining dimensions.

    A scalar selection is retained for callers that need one exact value.
    A two-item tuple or :class:`HeatmapRange` selects all persisted points in
    the inclusive interval.
    """

    x_id: str
    y_id: str
    filters: Mapping[str, HeatmapSelection]


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
    input_values: np.ndarray | None = None
    input_unit: str = ""


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
    if not any(not dimension.is_constant for dimension in dimensions):
        fallback_reason = (
            "No varying physical coordinate is stored for this spectrum; "
            "using checkpoint order."
        )
        dimensions = (
            HeatmapDimension(
                _CHECKPOINT_ID,
                "Checkpoint",
                "",
                tuple(float(index) for index in range(checkpoint_count)),
            ),
            *dimensions,
        )
    numbers = ()
    if len(points) == checkpoint_count and all(point.index == index for index, point in enumerate(points)):
        numbers = tuple(
            float(number) if type(number) is int and 0 < number < 2**53 else 0.0
            for point in points for number in (point.metadata.get("series_number"),)
        )
    if any(numbers):
        dimensions = (*dimensions, HeatmapDimension(SERIES_DIMENSION_ID, "Series", "", numbers))
    # A return sweep can store several spectra at identical settings in the
    # same series. A different series has its own sequence of visits.
    # Give each occurrence its own selector instead of merging measurements.
    visits = {}
    ordinals = []
    for coordinate in zip(*(dimension.values for dimension in dimensions), strict=True):
        visit = visits.get(coordinate, 0) + 1
        visits[coordinate] = visit
        ordinals.append(float(visit))
    if len(visits) < checkpoint_count and len(ordinals) == checkpoint_count:
        dimensions = (*dimensions, HeatmapDimension(VISIT_DIMENSION_ID, "Visit", "", tuple(ordinals)))
    return HeatmapCoordinates(
        dimensions=(
            HeatmapDimension(_FREQUENCY_ID, "Frequency", "Hz", (), True),
            *dimensions,
        ),
        checkpoint_count=checkpoint_count,
        fallback_reason=fallback_reason,
        sweep_dimension_ids=tuple(
            key for key in _recipe_axis_ids(run, checkpoint_count)
            if any(dimension.id == key and not dimension.is_constant for dimension in dimensions)
        ),
    )


def default_heatmap_ranges(
    coordinates: HeatmapCoordinates, x_id: str, y_id: str
) -> dict[str, HeatmapRange]:
    """Keep linked recorded views open; slice only independent coordinates.

    A non-axis coordinate needs an exact slice only when it has different
    values at the same plotted coordinate. Re-evaluate after each slice so
    derived views (e.g. High/Low from amplitude and offset) cannot pin the
    chosen sweep to its first point. Prefer the slice preserving the most
    plotted coordinates; recipe axes break ties when available. This changes
    selection defaults only; it never derives or averages data.
    """

    axes = tuple(coordinates.dimension(key) for key in (x_id, y_id))
    physical_axes = tuple(axis for axis in axes if not axis.is_frequency)
    dimensions = tuple(d for d in coordinates.dimensions if not d.is_frequency and d.values)
    ranges = {d.id: HeatmapRange(min(d.values), max(d.values)) for d in dimensions}
    if not dimensions or not coordinates.checkpoint_count:
        return ranges
    values_by_id = {d.id: np.asarray(d.values, dtype=float) for d in dimensions}
    group_ids = (
        np.unique(np.column_stack([axis.values for axis in physical_axes]),
                  axis=0, return_inverse=True)[1]
        if physical_axes else np.zeros(coordinates.checkpoint_count, dtype=int)
    )
    group_count = int(group_ids.max()) + 1
    selected = np.arange(coordinates.checkpoint_count)
    axis_ids = {x_id, y_id}
    while selected.size:
        groups = group_ids[selected]
        active_groups = np.unique(groups)
        best = None
        for dimension in dimensions:
            if dimension.id in axis_ids or dimension.id == SERIES_DIMENSION_ID or dimension.is_constant:
                continue
            values = values_by_id[dimension.id][selected]
            lows, highs = np.full(group_count, np.inf), np.full(group_count, -np.inf)
            np.minimum.at(lows, groups, values)
            np.maximum.at(highs, groups, values)
            if np.all(np.isclose(highs[active_groups], lows[active_groups], rtol=1e-9, atol=1e-15)):
                continue
            value = float(values.min())
            indices = selected[np.isclose(values, value, rtol=1e-9, atol=1e-15)]
            # Approximate equality is asymmetric near zero. Every chosen
            # slice must remove a checkpoint, otherwise selection can stall.
            if indices.size == selected.size:
                continue
            coverage = np.unique(group_ids[indices]).size
            score = (coverage, -indices.size, dimension.id in coordinates.sweep_dimension_ids)
            if best is None or score > best[0]:
                best = (score, dimension.id, value, indices)
        if best is None:
            break
        _, key, value, selected = best
        ranges[key] = HeatmapRange(value, value)
    return ranges


def linked_heatmap_ranges(
    coordinates: HeatmapCoordinates,
    x_id: str,
    y_id: str,
    *,
    series: int | None = None,
    preferred: Mapping[str, HeatmapRange] | None = None,
) -> dict[str, tuple[list[float], HeatmapRange]]:
    """Expose choices in recorded scope, then narrow to the selected child range."""
    axes = {x_id, y_id}
    dimensions = [d for d in coordinates.dimensions
                  if not d.is_frequency and d.values and d.id != SERIES_DIMENSION_ID]
    series_filter = {SERIES_DIMENSION_ID: series} if series is not None else {}
    indices = np.asarray(_selected_checkpoints(coordinates, series_filter), dtype=int)
    if not indices.size:
        return {}
    scope = replace(coordinates, checkpoint_count=len(indices), dimensions=tuple(
        d if d.is_frequency else replace(d, values=tuple(np.asarray(d.values)[indices]))
        for d in coordinates.dimensions
    ))
    defaults = default_heatmap_ranges(scope, x_id, y_id)
    moke_ids = {d.id for d in dimensions if d.id.startswith("moke_box.vout")
                and d.id.endswith(".voltage")}
    device_order = {"keithley.B.current": 1, "keithley.A.current": 2}
    device_order.update(dict.fromkeys(moke_ids, 0))
    moke_scan = bool(moke_ids) and {"keithley.A.current", "keithley.B.current"} <= {d.id for d in dimensions}
    dimensions.sort(key=lambda d: (
        d.id in axes, d.id == VISIT_DIMENSION_ID, device_order.get(d.id, 3),
    ))
    chosen = {}
    preferred = preferred or {}
    for dimension in dimensions:
        scoped_values = np.asarray(dimension.values)[indices]
        options = np.unique(scoped_values)
        desired = preferred.get(dimension.id, defaults[dimension.id])
        if moke_scan and dimension.id in device_order and dimension.id not in axes and dimension.id not in preferred:
            value = float(options[0])
            desired = HeatmapRange(value, value)
        matching = options[_selection_mask(options, desired)]
        if not matching.size:
            matching = options if dimension.id in axes or not _is_exact_range(desired) else options[:1]
        bounds = HeatmapRange(matching[0], matching[-1])
        chosen[dimension.id] = (options.tolist(), bounds)
        indices = indices[_selection_mask(scoped_values, bounds)]
    return chosen


def read_heatmap_matrix(
    path: Path,
    row: ThatecRow,
    coordinates: HeatmapCoordinates,
    request: HeatmapRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
    processor=None,
    capture_input: bool = False,
    spectrum_reader=None,
    checkpoint_indices: Sequence[int] | None = None,
) -> HeatmapMatrix:
    """Build one exact coordinate plane; never aggregate duplicate checkpoints."""

    x_dimension = coordinates.dimension(request.x_id)
    y_dimension = coordinates.dimension(request.y_id)
    if x_dimension.id == y_dimension.id:
        raise ValueError("Heatmap X and Y axes must be different.")
    known_dimension_ids = {dimension.id for dimension in coordinates.dimensions}
    duplicate_cell_message = (
        "Multiple checkpoints map to one heatmap cell; select a measurement series or refine the filters."
        if SERIES_DIMENSION_ID in known_dimension_ids else
        "Multiple checkpoints map to one heatmap cell; refine the filters."
    )
    unknown_filters = sorted(set(request.filters) - known_dimension_ids)
    if unknown_filters:
        raise ValueError(
            "Heatmap filter coordinate(s) are unavailable: "
            + ", ".join(unknown_filters)
            + "."
        )
    frequency_on_axis = x_dimension.is_frequency or y_dimension.is_frequency
    frequency_selection = request.filters.get(_FREQUENCY_ID)
    if not frequency_on_axis and frequency_selection is None:
        raise ValueError(
            "Choose one exact Frequency filter when Frequency is not a heatmap axis."
        )
    frequency_range = (
        _selection_range(frequency_selection)
        if frequency_selection is not None
        else None
    )
    if not frequency_on_axis and frequency_range is not None and not _is_exact_range(
        frequency_range
    ):
        raise ValueError(
            "Frequency must be an exact value when it is not a heatmap axis."
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
        if dimension.id not in request.filters and dimension.id != SERIES_DIMENSION_ID and not dimension.is_constant
    ]
    if missing_filters:
        raise ValueError(
            "Choose a filter range for " + ", ".join(missing_filters) + "."
        )
    selected = _selected_checkpoints(coordinates, request.filters)
    if checkpoint_indices is not None:
        exact = set(checkpoint_indices)
        selected = tuple(index for index in selected if index in exact)
        if set(selected) != exact:
            raise ValueError("The batch checkpoints no longer match this heatmap plane.")
    if not selected:
        raise ValueError("No stored checkpoint matches the selected heatmap filters.")

    traces: dict[int, np.ndarray] = {}
    input_traces: dict[int, np.ndarray] = {}
    input_unit = ""
    raw_frequency_grid: np.ndarray | None = None
    x_frequency: np.ndarray | None = None
    frequency_mask: np.ndarray | None = None
    z_label = "Amplitude"
    z_unit = ""
    missing = 0
    for checkpoint in selected:
        if cancelled is not None and cancelled():
            raise RuntimeError("Heatmap read cancelled.")
        try:
            spectrum = (spectrum_reader or ThatecRunReader.spectrum_slice)(path, row.id, checkpoint)
        except Exception:
            missing += 1
            continue
        if len(spectrum.traces) != 1:
            raise ValueError(
                f"THATEC row {row.id} has multiple trace components; inspect it in Spectrum."
            )
        if processor is not None:
            derived = processor.process(checkpoint, spectrum.x_values, spectrum.traces[0].values, spectrum.y_unit)
            spectrum_values, spectrum_unit = derived.values, derived.unit
        else:
            spectrum_values, spectrum_unit = spectrum.traces[0].values, spectrum.y_unit
        frequencies = np.asarray(spectrum.x_values, dtype=float)
        values = np.asarray(spectrum_values, dtype=float)
        if frequencies.ndim != 1 or values.ndim != 1 or frequencies.shape != values.shape:
            raise ValueError(f"THATEC row {row.id} has an invalid spectral grid.")
        if raw_frequency_grid is None:
            raw_frequency_grid = frequencies
            z_label = spectrum.y_label
            z_unit = spectrum_unit
            if frequency_on_axis and frequency_range is not None:
                frequency_mask = _selection_mask(frequencies, frequency_range)
                if not np.any(frequency_mask):
                    raise ValueError(
                        "The selected frequency range contains no spectrum bins."
                    )
            else:
                frequency_mask = np.ones(frequencies.shape, dtype=bool)
            x_frequency = frequencies[frequency_mask]
        elif not np.array_equal(raw_frequency_grid, frequencies):
            raise ValueError("Spectrum frequency grids differ between selected checkpoints.")
        elif spectrum_unit != z_unit:
            raise ValueError("Spectrum power units differ between selected checkpoints.")
        assert frequency_mask is not None
        traces[checkpoint] = values[frequency_mask]
        if capture_input:
            input_traces[checkpoint] = np.asarray(spectrum.traces[0].values, dtype=float)[frequency_mask]
            if input_unit and input_unit != spectrum.y_unit:
                raise ValueError("Input power units differ between selected checkpoints.")
            input_unit = spectrum.y_unit
    if x_frequency is None or x_frequency.size == 0:
        raise ValueError("No readable spectrum matches the selected heatmap filters.")

    if frequency_on_axis:
        varying_dimension = y_dimension if x_dimension.is_frequency else x_dimension
        axis_values = _unique_values(varying_dimension, selected)
        matrix = np.full((len(axis_values), len(x_frequency)), np.nan, dtype=float)
        input_matrix = np.full(matrix.shape, np.nan) if capture_input else None
        cell_checkpoints = np.full(matrix.shape, -1, dtype=int)
        for checkpoint, values in traces.items():
            coordinate = _checkpoint_value(varying_dimension, checkpoint)
            row_index = _value_index(axis_values, coordinate)
            if np.any(cell_checkpoints[row_index] >= 0):
                raise ValueError(duplicate_cell_message)
            matrix[row_index, :] = values
            if input_matrix is not None:
                input_matrix[row_index, :] = input_traces[checkpoint]
            cell_checkpoints[row_index, :] = checkpoint
        if x_dimension.is_frequency:
            x_values, y_values = x_frequency, axis_values
        else:
            matrix = matrix.T
            if input_matrix is not None:
                input_matrix = input_matrix.T
            cell_checkpoints = cell_checkpoints.T
            x_values, y_values = axis_values, x_frequency
    else:
        assert frequency_range is not None
        frequency = frequency_range.minimum
        frequency_index = _value_index(x_frequency, frequency)
        x_values = _unique_values(x_dimension, selected)
        y_values = _unique_values(y_dimension, selected)
        matrix = np.full((len(y_values), len(x_values)), np.nan, dtype=float)
        input_matrix = np.full(matrix.shape, np.nan) if capture_input else None
        cell_checkpoints = np.full(matrix.shape, -1, dtype=int)
        for checkpoint, values in traces.items():
            x_index = _value_index(x_values, _checkpoint_value(x_dimension, checkpoint))
            y_index = _value_index(y_values, _checkpoint_value(y_dimension, checkpoint))
            if cell_checkpoints[y_index, x_index] >= 0:
                raise ValueError(duplicate_cell_message)
            matrix[y_index, x_index] = values[frequency_index]
            if input_matrix is not None:
                input_matrix[y_index, x_index] = input_traces[checkpoint][frequency_index]
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
        input_values=input_matrix,
        input_unit=input_unit,
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
        if not _finite_coordinates(values):
            continue
        dimensions.append(_dimension_for_key(key, values))
    return tuple(dimensions)


def _ordered_private_keys(
    keys: set[str], run: ThatecRun, checkpoint_count: int
) -> tuple[str, ...]:
    recipe_keys = tuple(key for key in _recipe_axis_ids(run, checkpoint_count) if key in keys)
    return (*recipe_keys, *(key for key in sorted(keys) if key not in recipe_keys))


def _recipe_axis_ids(run: ThatecRun, checkpoint_count: int) -> tuple[str, ...]:
    schema = ThatecSchemaMapper.from_recipe_source(
        run.recipe_source, expected_points=checkpoint_count
    )
    return tuple(axis.target for axis in schema.axes) if schema.mode == "recipe_sweeps" else ()


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
        if not _finite_coordinates(coordinate_values):
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


def _finite_coordinates(values: tuple[float, ...]) -> bool:
    return bool(values) and all(math.isfinite(value) for value in values)


def _selected_checkpoints(
    coordinates: HeatmapCoordinates,
    filters: Mapping[str, HeatmapSelection],
) -> tuple[int, ...]:
    selected = np.ones(coordinates.checkpoint_count, dtype=bool)
    for dimension in coordinates.dimensions:
        if dimension.is_frequency or dimension.id not in filters:
            continue
        selected &= _selection_mask(np.asarray(dimension.values), _selection_range(filters[dimension.id]))
    return tuple(np.flatnonzero(selected).tolist())


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


def _selection_range(selection: HeatmapSelection) -> HeatmapRange:
    if isinstance(selection, HeatmapRange):
        return selection
    if isinstance(selection, tuple):
        if len(selection) != 2:
            raise ValueError("Heatmap ranges must contain exactly two limits.")
        return HeatmapRange(selection[0], selection[1])
    if isinstance(selection, (str, bytes, bool)):
        raise ValueError("Heatmap selections must be finite numeric values.")
    try:
        value = float(selection)
    except (TypeError, ValueError) as exc:
        raise ValueError("Heatmap selections must be finite numeric values.") from exc
    return HeatmapRange(value, value)


def _selection_mask(values: np.ndarray, selection: HeatmapRange) -> np.ndarray:
    minimum, maximum = selection.minimum, selection.maximum
    return (
        ((values >= minimum) | np.isclose(values, minimum, rtol=1e-9, atol=1e-15))
        & ((values <= maximum) | np.isclose(values, maximum, rtol=1e-9, atol=1e-15))
    )


def _is_exact_range(selection: HeatmapRange) -> bool:
    return _values_equal(selection.minimum, selection.maximum)
