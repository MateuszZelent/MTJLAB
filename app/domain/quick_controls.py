"""Typed quantity stepping and quick-setpoint boundary models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from app.domain.quantities import parse_quantity


_QUANTITY = re.compile(
    r"^\s*(?P<number>[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?)\s*(?P<unit>\S.*)\s*$"
)


@dataclass(frozen=True, slots=True)
class QuickSetpoint:
    target: str
    text: str
    value_si: float
    sequence: int


@dataclass(frozen=True, slots=True)
class QuickControlCommand:
    """Explicit-unit command crossing from UI into an instrument worker."""

    target: str
    quantity_text: str


def quantity_step_si(text: str, dimension: str) -> float:
    """Return the SI step represented by the last written decimal place."""

    match = _QUANTITY.fullmatch(text)
    if match is None:
        raise ValueError("Enter a number followed by an explicit unit.")
    numeric_text = match.group("number").replace(",", ".")
    try:
        number = Decimal(numeric_text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value {numeric_text!r}.") from exc
    if not number.is_finite():
        raise ValueError("Quick-control value must be finite.")
    quantum = Decimal(1).scaleb(number.as_tuple().exponent)
    unit_scale = parse_quantity(f"1 {match.group('unit')}", dimension).si_value
    return float(abs(quantum) * Decimal(str(unit_scale)))


def step_quantity_text(
    text: str,
    dimension: str,
    direction: int,
    *,
    multiplier: Decimal = Decimal(1),
) -> tuple[str, float]:
    """Step a quantity while preserving its written decimal precision and unit."""

    if direction not in {-1, 1}:
        raise ValueError("Quick-control direction must be -1 or +1.")
    match = _QUANTITY.fullmatch(text)
    if match is None:
        raise ValueError("Enter a number followed by an explicit unit.")
    number_text = match.group("number").replace(",", ".")
    try:
        number = Decimal(number_text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value {number_text!r}.") from exc
    quantum = Decimal(1).scaleb(number.as_tuple().exponent)
    updated = number + Decimal(direction) * quantum * multiplier
    unit = match.group("unit").strip()
    rendered = f"{format(updated, 'f')} {unit}"
    return rendered, parse_quantity(rendered, dimension).si_value


def render_quantity_si_like(text: str, dimension: str, value_si: float) -> str:
    """Render an SI boundary in the user's current unit and written precision."""

    match = _QUANTITY.fullmatch(text)
    if match is None:
        raise ValueError("Enter a number followed by an explicit unit.")
    original = Decimal(match.group("number").replace(",", "."))
    unit = match.group("unit").strip()
    scale = Decimal(str(parse_quantity(f"1 {unit}", dimension).si_value))
    value_in_unit = Decimal(str(value_si)) / scale
    quantum = Decimal(1).scaleb(original.as_tuple().exponent)
    rendered = value_in_unit.quantize(quantum)
    return f"{format(rendered, 'f')} {unit}"
