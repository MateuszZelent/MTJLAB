"""Pure plotting projection; retain sequence and gaps in stored field curves."""

from dataclasses import dataclass
import math

from app.domain.quantities import format_quantity_auto


@dataclass(frozen=True, slots=True)
class FieldPlotCurve:
    index: int
    label: str
    x: tuple[float, ...]
    y: tuple[float, ...]


def field_overlay_curves(series, *, resistance: bool) -> tuple[FieldPlotCurve, ...]:
    result = []
    for curve in series.curves:
        dataset = curve.dataset
        if dataset is None or curve.status == "skipped_field_compliance":
            continue
        x, y = [], []
        for point in dataset.points:
            value = (point.true_resistance_ohm if resistance else point.measured_voltage_v
                     if dataset.config.mode == "current" else point.measured_current_a)
            valid = point.valid and not point.compliance_active and math.isfinite(value)
            x.append(point.demanded_si)
            y.append(value if valid else math.nan)
        if any(math.isfinite(value) for value in y):
            result.append(FieldPlotCurve(
                curve.index,
                f"#{curve.index + 1}: B {format_quantity_auto(curve.current_a, 'current')} · h{curve.history_segment}",
                tuple(x), tuple(y),
            ))
    return tuple(result)
