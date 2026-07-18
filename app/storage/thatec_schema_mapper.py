"""Map declarative recipe sweeps to thaTEC measurement-tree coordinates."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.recipes.models import RecipeNode, parse_recipe_text
from app.recipes.parameter_registry import parameter_descriptor
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
        elif (
            node.type == "sequence"
            and node.data.get("device_module") == "keithley"
            and node.data.get("operation") == "configure_selected_parameters"
        ):
            axis = cls._keithley_device_axis(node)
            if axis is not None:
                ancestors = (*ancestors, axis)
        elif (
            node.type == "sequence"
            and node.data.get("device_module") == "rigol"
            and node.data.get("operation") == "configure_selected_parameters"
        ):
            axis = cls._rigol_device_axis(node)
            if axis is not None:
                ancestors = (*ancestors, axis)
        elif (
            node.type == "sequence"
            and node.data.get("device_module") == "anritsu"
            and node.data.get("operation") == "configure_selected_parameters"
        ):
            axis = cls._anritsu_device_axis(node)
            if axis is not None:
                ancestors = (*ancestors, axis)
        elif (
            node.type == "sequence"
            and node.data.get("device_module") == "anritsu_sg"
            and node.data.get("operation") == "configure_selected_parameters"
        ):
            axis = cls._anritsu_sg_device_axis(node)
            if axis is not None:
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
    def _keithley_device_axis(node: RecipeNode) -> ThatecSweepAxis | None:
        raw_actions = node.data.get("parameter_actions")
        if not isinstance(raw_actions, list):
            return None
        sweep = next(
            (
                action
                for action in raw_actions
                if isinstance(action, dict) and action.get("mode") == "sweep"
            ),
            None,
        )
        if sweep is None:
            return None
        channel = str(node.data.get("channel", ""))
        mode = str(node.data.get("source_mode", ""))
        parameter_id = str(sweep.get("parameter_id", ""))
        if parameter_id == "source.level":
            target = f"keithley.{channel}.{mode}"
        elif parameter_id == "source.compliance":
            compliance_kind = (
                "compliance_voltage"
                if mode == "current"
                else "compliance_current"
            )
            target = f"keithley.{channel}.{compliance_kind}"
        elif parameter_id == "measurement.settling_time":
            target = f"keithley.{channel}.settling_time"
        else:
            return None
        descriptor = parameter_descriptor(target)
        segments = sweep.get("segments")
        if not isinstance(segments, list) or not segments:
            return None
        generated = generate_sweep_points(segments, descriptor.dimension)
        return ThatecSweepAxis(
            target=target,
            device_name=descriptor.device_name,
            control_name=descriptor.control_name,
            unit=descriptor.unit,
            values_si=tuple(point.si_value for point in generated),
            spacing="piecewise",
        )

    @staticmethod
    def _rigol_device_axis(node: RecipeNode) -> ThatecSweepAxis | None:
        raw_actions = node.data.get("parameter_actions")
        if not isinstance(raw_actions, list):
            return None
        sweep = next(
            (
                action
                for action in raw_actions
                if isinstance(action, dict) and action.get("mode") == "sweep"
            ),
            None,
        )
        if sweep is None:
            return None
        parameter_id = str(sweep.get("parameter_id", ""))
        suffix = {
            "carrier.frequency": "frequency",
            "carrier.high_level": "high_level",
            "carrier.low_level": "low_level",
        }.get(parameter_id)
        if suffix is None:
            return None
        target = f"rigol.{node.data.get('channel')}.{suffix}"
        descriptor = parameter_descriptor(target)
        segments = sweep.get("segments")
        if not isinstance(segments, list) or not segments:
            return None
        generated = generate_sweep_points(segments, descriptor.dimension)
        return ThatecSweepAxis(
            target=target,
            device_name=descriptor.device_name,
            control_name=descriptor.control_name,
            unit=descriptor.unit,
            values_si=tuple(point.si_value for point in generated),
            spacing="piecewise",
        )

    @staticmethod
    def _anritsu_device_axis(node: RecipeNode) -> ThatecSweepAxis | None:
        raw_actions = node.data.get("parameter_actions")
        if not isinstance(raw_actions, list):
            return None
        sweep = next(
            (
                action
                for action in raw_actions
                if isinstance(action, dict) and action.get("mode") == "sweep"
            ),
            None,
        )
        if sweep is None:
            return None
        target = {
            "spectrum.start_frequency": "anritsu.spectrum.start_frequency",
            "spectrum.stop_frequency": "anritsu.spectrum.stop_frequency",
            "spectrum.reference_level": "anritsu.spectrum.reference_level",
        }.get(str(sweep.get("parameter_id", "")))
        if target is None:
            return None
        descriptor = parameter_descriptor(target)
        segments = sweep.get("segments")
        if not isinstance(segments, list) or not segments:
            return None
        generated = generate_sweep_points(segments, descriptor.dimension)
        return ThatecSweepAxis(
            target=target,
            device_name=descriptor.device_name,
            control_name=descriptor.control_name,
            unit=descriptor.unit,
            values_si=tuple(point.si_value for point in generated),
            spacing="piecewise",
        )

    @staticmethod
    def _anritsu_sg_device_axis(node: RecipeNode) -> ThatecSweepAxis | None:
        raw_actions = node.data.get("parameter_actions")
        if not isinstance(raw_actions, list):
            return None
        sweep = next(
            (
                action
                for action in raw_actions
                if isinstance(action, dict) and action.get("mode") == "sweep"
            ),
            None,
        )
        if sweep is None:
            return None
        target = {
            "sg.frequency": "anritsu.sg.frequency",
            "sg.power": "anritsu.sg.power",
        }.get(str(sweep.get("parameter_id", "")))
        if target is None:
            return None
        descriptor = parameter_descriptor(target)
        segments = sweep.get("segments")
        if not isinstance(segments, list) or not segments:
            return None
        generated = generate_sweep_points(segments, descriptor.dimension)
        return ThatecSweepAxis(
            target=target,
            device_name=descriptor.device_name,
            control_name=descriptor.control_name,
            unit=descriptor.unit,
            values_si=tuple(point.si_value for point in generated),
            spacing="piecewise",
        )

    @staticmethod
    def _axis_from_node(node: RecipeNode) -> ThatecSweepAxis:
        target = str(node.data["target"])
        descriptor = parameter_descriptor(target)
        dimension = descriptor.dimension
        device = descriptor.device_name
        control = descriptor.control_name
        unit = descriptor.unit
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
