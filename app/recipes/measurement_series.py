"""Identify independently runnable current scans within fixed MOKE/B groups."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_VOLTAGE, parse_quantity
from app.recipes.models import Recipe, RecipeNode
from app.recipes.sweep_points import estimate_sweep_point_count


@dataclass(frozen=True, slots=True)
class MeasurementSeries:
    id: str
    number: int
    group_id: str
    moke_channel: int
    moke_voltage_v: float
    keithley_b_a: float
    keithley_a_start_a: float
    keithley_a_stop_a: float
    points: int
    already_measured: bool


def _active(node: RecipeNode) -> bool:
    return not node.data.get("disabled") and not node.else_children


def _is_current_ramp(node: RecipeNode, channel: str) -> bool:
    return node.type == "ramp_keithley_current" and node.data.get("channel") == channel and _active(node)


def _series_scan(node: RecipeNode) -> tuple[float, float, float, int] | None:
    if node.type != "sequence" or not _active(node) or set(node.data) - {"label", "description", "disabled"}:
        return None
    if len(node.children) != 4:
        return None
    bias, approach, scan, retract = node.children
    if not (_is_current_ramp(bias, "B") and _is_current_ramp(approach, "A")
            and _is_current_ramp(retract, "A") and scan.type == "sweep" and _active(scan)
            and scan.data.get("target") == "keithley.A.current"
            and scan.data.get("respect_ramp_step") is True
            and all(child.type in {"wait", "measure_keithley", "acquire_spectrum", "comment"}
                    for child in scan.children)
            and any(child.type == "acquire_spectrum" and _active(child) for child in scan.children)):
        return None
    if parse_quantity(retract.data.get("level"), DIMENSION_CURRENT).si_value != 0:
        return None
    segments = scan.data.get("segments")
    if segments is None:
        segments = [{key: scan.data[key] for key in ("start", "stop", "points", "spacing") if key in scan.data}]
    count = estimate_sweep_point_count(segments, DIMENSION_CURRENT)
    if count == 0:
        return None
    first = parse_quantity(segments[0].get("value", segments[0].get("start")), DIMENSION_CURRENT).si_value
    last = parse_quantity(segments[-1].get("value", segments[-1].get("stop")), DIMENSION_CURRENT).si_value
    if parse_quantity(approach.data.get("level"), DIMENSION_CURRENT).si_value != first:
        return None
    return parse_quantity(bias.data.get("level"), DIMENSION_CURRENT).si_value, first, last, count


def measurement_series(recipe: Recipe) -> tuple[MeasurementSeries, ...]:
    """Recognize fixed MOKE groups containing complete, independently positioned A scans."""
    root = recipe.root
    if (root.type != "sequence" or not _active(root)
            or set(root.data) - {"label", "description", "disabled"}):
        return ()
    completed = set(recipe.completed_series)
    catalog = []
    for group in root.children:
        if group.type != "sweep" or not _active(group):
            continue
        target = re.fullmatch(r"moke_box\.vout([0-7])\.voltage", str(group.data.get("target", "")))
        segments = group.data.get("segments")
        if (target is None or not isinstance(segments, list) or len(segments) != 1
                or not isinstance(segments[0], dict) or set(segments[0]) != {"value"} or not group.children):
            continue
        try:
            scans = [_series_scan(child) for child in group.children]
            voltage = parse_quantity(segments[0]["value"], DIMENSION_VOLTAGE).si_value
        except (ConfigurationError, KeyError, TypeError, ValueError):
            # Let normal preflight diagnose invalid setpoints and ROI definitions.
            continue
        if any(scan is None for scan in scans):
            continue
        for child, scan in zip(group.children, scans, strict=True):
            bias, start, stop, points = scan
            catalog.append(MeasurementSeries(
                child.id, len(catalog) + 1, group.id, int(target[1]), voltage,
                bias, start, stop, points, child.id in completed,
            ))
    return tuple(catalog)


def apply_series_selection(recipe: Recipe, catalog: tuple[MeasurementSeries, ...]) -> Recipe:
    """Disable whole series and empty MOKE groups before normal plan compilation."""
    if not recipe.completed_series:
        return recipe
    completed = set(recipe.completed_series)
    series_ids = {item.id for item in catalog}
    unknown = completed - series_ids
    if unknown:
        raise ConfigurationError(f"Unknown or changed series: {', '.join(sorted(unknown))}. Review the series selection.")
    if completed == series_ids:
        raise ConfigurationError("All series are marked as already measured. Select at least one series to run.")
    groups = {item.group_id for item in catalog}

    def disable(node: RecipeNode) -> RecipeNode:
        return replace(node, data={**node.data, "disabled": True})

    children = []
    for group in recipe.root.children:
        if group.id not in groups:
            children.append(group)
        elif all(child.id in completed for child in group.children):
            children.append(disable(group))
        else:
            children.append(replace(group, children=tuple(
                disable(child) if child.id in completed else child for child in group.children
            )))
    return replace(recipe, root=replace(recipe.root, children=tuple(children)))
