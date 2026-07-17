"""Deterministic point generation for visual multi-segment sweeps."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from app.domain.errors import ConfigurationError
from app.domain.quantities import Quantity, parse_quantity


@dataclass(frozen=True, slots=True)
class SweepSegment:
    """One inclusive interval in a visual sweep generator."""

    start: Quantity
    stop: Quantity
    points: int | None = None
    step: Quantity | None = None
    spacing: str = "linear"


def generate_segment_points(segment: SweepSegment) -> tuple[Quantity, ...]:
    """Generate exact inclusive points for one interval.

    A segment uses either an explicit number of points or a positive linear
    step.  The stop endpoint is always retained, including when the supplied
    step does not divide the interval exactly.
    """

    if segment.start.dimension != segment.stop.dimension:
        raise ConfigurationError("Sweep segment start and stop must use the same dimension.")
    if (segment.points is None) == (segment.step is None):
        raise ConfigurationError("A sweep segment requires exactly one of points or step.")
    if segment.spacing not in {"linear", "log"}:
        raise ConfigurationError("Sweep segment spacing must be linear or log.")
    if segment.points is not None:
        if not isinstance(segment.points, int) or segment.points < 2:
            raise ConfigurationError("Sweep segment points must be an integer >= 2.")
        if segment.spacing == "linear":
            delta = (segment.stop.si_value - segment.start.si_value) / (segment.points - 1)
            return tuple(
                Quantity(segment.start.si_value + index * delta, segment.start.dimension)
                for index in range(segment.points)
            )
        if segment.start.si_value <= 0 or segment.stop.si_value <= 0:
            raise ConfigurationError("A logarithmic sweep segment requires positive endpoints.")
        ratio = (segment.stop.si_value / segment.start.si_value) ** (1 / (segment.points - 1))
        return tuple(
            Quantity(segment.start.si_value * ratio**index, segment.start.dimension)
            for index in range(segment.points)
        )

    assert segment.step is not None
    if segment.spacing != "linear":
        raise ConfigurationError("A logarithmic sweep segment requires a point count, not a step.")
    if segment.step.dimension != segment.start.dimension or segment.step.si_value <= 0:
        raise ConfigurationError("Sweep segment step must be positive and use the sweep dimension.")
    direction = 1.0 if segment.stop.si_value >= segment.start.si_value else -1.0
    step = direction * segment.step.si_value
    values = [segment.start.si_value]
    while True:
        candidate = values[-1] + step
        if (direction > 0 and candidate >= segment.stop.si_value) or (
            direction < 0 and candidate <= segment.stop.si_value
        ):
            break
        values.append(candidate)
        if len(values) > 1_000_000:
            raise ConfigurationError("A sweep segment exceeds the 1,000,000 point generator limit.")
    if not math.isclose(values[-1], segment.stop.si_value, rel_tol=1e-12, abs_tol=1e-15):
        values.append(segment.stop.si_value)
    return tuple(Quantity(value, segment.start.dimension) for value in values)


def generate_sweep_points(
    segments: Iterable[dict[str, Any]],
    dimension: str,
    *,
    deduplicate_boundaries: bool = True,
) -> tuple[Quantity, ...]:
    """Build one axis from arbitrary visual intervals, preserving stage order."""

    return tuple(
        point
        for stage in generate_sweep_stage_points(
            segments, dimension, deduplicate_boundaries=deduplicate_boundaries
        )
        for point in stage
    )


def generate_sweep_stage_points(
    segments: Iterable[dict[str, Any]],
    dimension: str,
    *,
    deduplicate_boundaries: bool = True,
) -> tuple[tuple[Quantity, ...], ...]:
    """Generate point collections per stage using the runner's exact semantics."""

    values: list[Quantity] = []
    stages: list[tuple[Quantity, ...]] = []
    for index, raw in enumerate(segments):
        if not isinstance(raw, dict):
            raise ConfigurationError(f"sweep.segments[{index}] must be a mapping.")
        has_points = "points" in raw
        has_step = "step" in raw
        if has_points == has_step:
            raise ConfigurationError(
                f"sweep.segments[{index}] requires exactly one of points or step."
            )
        try:
            segment = SweepSegment(
                start=parse_quantity(raw["start"], dimension),
                stop=parse_quantity(raw["stop"], dimension),
                points=int(raw["points"]) if has_points else None,
                step=parse_quantity(raw["step"], dimension) if has_step else None,
                spacing=str(raw.get("spacing", "linear")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid sweep.segments[{index}]: {exc}") from exc
        generated = generate_segment_points(segment)
        if values and deduplicate_boundaries and math.isclose(
            values[-1].si_value,
            generated[0].si_value,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            generated = generated[1:]
        values.extend(generated)
        stages.append(generated)
    if len(values) < 2:
        raise ConfigurationError("A multi-segment sweep must generate at least two points.")
    return tuple(stages)
