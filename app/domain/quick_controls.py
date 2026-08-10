"""Typed quantity stepping and quick-setpoint boundary models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
import re

from app.domain.quantities import parse_quantity


_QUANTITY = re.compile(
    r"^\s*(?P<number>"
    r"(?P<sign>[+-]?)"
    r"(?P<mantissa>(?:\d+(?:[.,]\d*)?|[.,]\d+))"
    r"(?P<exponent>[eE][+-]?\d+)?"
    r")\s*(?P<unit>\S.*?)\s*$"
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


@dataclass(frozen=True, slots=True)
class QuickConfigureCommand:
    """Complete device-page configuration used for a safe OUTPUT-OFF apply."""

    target: str
    configuration: object


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
    quantum = _written_quantum(match)
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
    try:
        quantum = _written_quantum(match)
        updated = number + Decimal(direction) * quantum * multiplier
        unit = match.group("unit").strip()
        rendered_number = _render_written_number(
            match,
            updated,
            extra_quantum=quantum * multiplier,
        )
    except (DecimalException, OverflowError) as exc:
        raise ValueError(f"Numeric value {number_text!r} cannot be stepped.") from exc
    rendered = f"{rendered_number} {unit}"
    return rendered, parse_quantity(rendered, dimension).si_value


def render_quantity_si_like(text: str, dimension: str, value_si: float) -> str:
    """Render an SI boundary in the user's current unit and written precision."""

    match = _QUANTITY.fullmatch(text)
    if match is None:
        raise ValueError("Enter a number followed by an explicit unit.")
    unit = match.group("unit").strip()
    scale = Decimal(str(parse_quantity(f"1 {unit}", dimension).si_value))
    value_in_unit = Decimal(str(value_si)) / scale
    # A safety boundary may be finer than the operator's current display
    # precision (for example 0.15 A while the field shows 0.1 A).  Add only
    # the digits required to represent that boundary exactly; rounding it
    # back outside the limit would make the subsequent submit fail closed
    # instead of landing on the configured bound.
    boundary_quantum = Decimal(1).scaleb(
        value_in_unit.normalize().as_tuple().exponent
    )
    rendered = _render_written_number(
        match,
        value_in_unit,
        extra_quantum=boundary_quantum,
    )
    return f"{rendered} {unit}"


def _written_quantum(match: re.Match[str]) -> Decimal:
    """Return the value represented by the last written mantissa digit."""

    mantissa = match.group("mantissa")
    separator = "," if "," in mantissa else "."
    decimal_places = (
        len(mantissa.rsplit(separator, 1)[1]) if separator in mantissa else 0
    )
    exponent = match.group("exponent")
    exponent_value = int(exponent[1:]) if exponent else 0
    return Decimal(1).scaleb(exponent_value - decimal_places)


def _render_written_number(
    match: re.Match[str],
    value: Decimal,
    *,
    extra_quantum: Decimal | None = None,
) -> str:
    """Render ``value`` with the input's decimal separator and exponent scale.

    ``extra_quantum`` accounts for keyboard modifiers that deliberately request
    a fraction of the normal step.  It may add a displayed digit, but never
    removes precision the operator wrote.
    """

    mantissa = match.group("mantissa")
    decimal_separator = "," if "," in mantissa else "."
    original_places = (
        len(mantissa.rsplit(decimal_separator, 1)[1])
        if decimal_separator in mantissa
        else 0
    )
    exponent = match.group("exponent") or ""
    exponent_value = int(exponent[1:]) if exponent else 0
    rendered_value = value.scaleb(-exponent_value) if exponent else value
    decimal_places = original_places
    if extra_quantum is not None:
        displayed_quantum = (
            extra_quantum.scaleb(-exponent_value) if exponent else extra_quantum
        )
        decimal_places = max(
            decimal_places,
            max(0, -displayed_quantum.normalize().as_tuple().exponent),
        )
    rendered = f"{rendered_value:.{decimal_places}f}"
    if decimal_separator == ",":
        rendered = rendered.replace(".", ",")
    if match.group("sign") == "+" and not rendered.startswith(("+", "-")):
        rendered = f"+{rendered}"
    return f"{rendered}{exponent}"
