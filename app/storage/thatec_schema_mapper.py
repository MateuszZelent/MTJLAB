"""Map declarative recipe sweeps to thaTEC measurement-tree coordinates."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Final

from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_VOLTAGE,
)
from app.recipes.models import RecipeNode, parse_recipe_text
from app.recipes.sweep_points import generate_sweep_points


@dataclass(frozen=True, slots=True)
class ThatecSweepAxis:
    target: str
    device_name: str
    control_name: str
    unit: str
    values_si: tuple[float, ...]
    spacing: str = "linear"

    @property
    def points(self) -> int:
        return len(self.values_si)


@dataclass(frozen=True, slots=True)
class ThatecSchema:
    axes: tuple[ThatecSweepAxis, ...]
    axis_targets: frozenset[str]
    mode: str
    detail: str

    @property
    def point_count(self) -> int:
        return math.prod(axis.points for axis in self.axes)


_TARGETS: Final[dict[str, tuple[str, str, str, str]]] = {
    "keithley.A.level": (DIMENSION_CURRENT, "Keithley 2602A", "Keithley A level", "A"),
    "keithley.B.level": (DIMENSION_CURRENT, "Keithley 2602A", "Keithley B level", "A"),
    "keithley.A.current": (
        DIMENSION_CURRENT,
        "Keithley 2602A",
        "Keithley A current",
        "A",
    ),
    "keithley.B.current": (
        DIMENSION_CURRENT,
        "Keithley 2602A",
        "Keithley B current",
        "A",
    ),
    "keithley.A.voltage": (
        DIMENSION_VOLTAGE,
        "Keithley 2602A",
        "Keithley A voltage",
        "V",
    ),
    "keithley.B.voltage": (
        DIMENSION_VOLTAGE,
        "Keithley 2602A",
        "Keithley B voltage",
        "V",
    ),
    "rigol.1.high_level": (
        DIMENSION_VOLTAGE,
        "Rigol DG1032Z",
        "Rigol CH1 high level",
        "V",
    ),
    "rigol.2.high_level": (
        DIMENSION_VOLTAGE,
        "Rigol DG1032Z",
        "Rigol CH2 high level",
        "V",
    ),
    "rigol.1.low_level": (
        DIMENSION_VOLTAGE,
        "Rigol DG1032Z",
        "Rigol CH1 low level",
        "V",
    ),
    "rigol.2.low_level": (
        DIMENSION_VOLTAGE,
        "Rigol DG1032Z",
        "Rigol CH2 low level",
        "V",
    ),
    "rigol.1.frequency": (
        DIMENSION_FREQUENCY,
        "Rigol DG1032Z",
        "Rigol CH1 frequency",
        "Hz",
    ),
    "rigol.2.frequency": (
        DIMENSION_FREQUENCY,
        "Rigol DG1032Z",
        "Rigol CH2 frequency",
        "Hz",
    ),
    "anritsu.spectrum.start_frequency": (
        DIMENSION_FREQUENCY,
        "Anritsu Spectrum Analyzer",
        "Anritsu start frequency",
        "Hz",
    ),
    "anritsu.spectrum.stop_frequency": (
        DIMENSION_FREQUENCY,
        "Anritsu Spectrum Analyzer",
        "Anritsu stop frequency",
        "Hz",
    ),
    "anritsu.spectrum.reference_level": (
        DIMENSION_DBM,
        "Anritsu Spectrum Analyzer",
        "Anritsu reference level",
        "dBm",
    ),
    "anritsu.sg.frequency": (
        DIMENSION_FREQUENCY,
        "Anritsu Signal Generator",
        "Anritsu SG frequency",
        "Hz",
    ),
    "anritsu.sg.power": (
        DIMENSION_DBM,
        "Anritsu Signal Generator",
        "Anritsu SG power",
        "dBm",
    ),
}


class ThatecSchemaMapper:
    """Derive deterministic xarray dimensions from a compiled recipe source."""

    @classmethod
    def from_recipe_source(
        cls, recipe_source: str, *, expected_points: int | None
    ) -> ThatecSchema:
        count = max(1, int(expected_points or 1))
        try:
            recipe = parse_recipe_text(recipe_source, origin="HDF5 recipe snapshot")
            paths: list[tuple[ThatecSweepAxis, ...]] = []
            cls._collect_acquisition_paths(recipe.root, (), paths)
        except Exception as exc:
            return cls._checkpoint_schema(count, f"recipe mapping unavailable: {exc}")
        if not paths:
            return cls._checkpoint_schema(count, "recipe contains no spectrum acquisition")
        first = paths[0]
        if any(path != first for path in paths[1:]):
            return cls._checkpoint_schema(
                count, "spectrum acquisitions use different sweep ancestry"
            )
        base_points = math.prod(axis.points for axis in first)
        acquisitions_per_leaf = len(paths)
        axes = first
        if acquisitions_per_leaf > 1:
            axes = (
                *axes,
                ThatecSweepAxis(
                    target="measurement.acquisition",
                    device_name="Lab Control",
                    control_name="Acquisition",
                    unit="",
                    values_si=tuple(float(index) for index in range(acquisitions_per_leaf)),
                ),
            )
        if base_points * acquisitions_per_leaf != count:
            return cls._checkpoint_schema(
                count,
                "expanded recipe point count does not match acquisition topology",
            )
        return ThatecSchema(
            axes=axes,
            axis_targets=frozenset(axis.target for axis in first),
            mode="recipe_sweeps",
            detail="Nested sweep coordinates reconstructed from recipe YAML.",
        )

    @classmethod
    def _collect_acquisition_paths(
        cls,
        node: RecipeNode,
        ancestors: tuple[ThatecSweepAxis, ...],
        paths: list[tuple[ThatecSweepAxis, ...]],
    ) -> None:
        if node.type == "acquire_spectrum":
            paths.append(ancestors)
            return
        if node.type == "sweep":
            axis = cls._axis_from_node(node)
            ancestors = (*ancestors, axis)
        elif node.type == "repeat":
            count = int(node.data["count"])
            ancestors = (
                *ancestors,
                ThatecSweepAxis(
                    target=f"repeat.{node.id}.index",
                    device_name="Lab Control",
                    control_name=f"Repeat {node.id}",
                    unit="",
                    values_si=tuple(float(index) for index in range(count)),
                    spacing="step",
                ),
            )
        for child in node.children:
            cls._collect_acquisition_paths(child, ancestors, paths)
        for child in node.else_children:
            cls._collect_acquisition_paths(child, ancestors, paths)

    @staticmethod
    def _axis_from_node(node: RecipeNode) -> ThatecSweepAxis:
        target = str(node.data["target"])
        dimension, device, control, unit = _TARGETS[target]
        segments = node.data.get("segments")
        if isinstance(segments, list):
            generated = generate_sweep_points(segments, dimension)
            return ThatecSweepAxis(
                target,
                device,
                control,
                unit,
                tuple(point.si_value for point in generated),
                "piecewise",
            )
        generated = generate_sweep_points(
            [
                {
                    "start": node.data["start"],
                    "stop": node.data["stop"],
                    "points": node.data["points"],
                    "spacing": node.data.get("spacing", "linear"),
                }
            ],
            dimension,
        )
        return ThatecSweepAxis(
            target,
            device,
            control,
            unit,
            tuple(point.si_value for point in generated),
            str(node.data.get("spacing", "linear")),
        )

    @staticmethod
    def _checkpoint_schema(points: int, detail: str) -> ThatecSchema:
        axis = ThatecSweepAxis(
            target="measurement.checkpoint",
            device_name="Lab Control",
            control_name="Checkpoint",
            unit="",
            values_si=tuple(float(index) for index in range(points)),
        )
        return ThatecSchema((axis,), frozenset(), "checkpoint_fallback", detail)
