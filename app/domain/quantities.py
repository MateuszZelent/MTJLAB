"""Small, strict SI quantity parser used at the safety boundary.

The application deliberately keeps this module dependency-free.  It accepts a
short, explicit unit vocabulary used by the station profile and converts every
value to SI before a limit comparison is made.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Final
import unicodedata


class QuantityError(ValueError):
    """A value cannot be interpreted as a finite quantity with a known unit."""


DIMENSION_VOLTAGE: Final = "voltage"
DIMENSION_CURRENT: Final = "current"
DIMENSION_POWER: Final = "power"
DIMENSION_FREQUENCY: Final = "frequency"
DIMENSION_RESISTANCE: Final = "resistance"
DIMENSION_TIME: Final = "time"
DIMENSION_DBM: Final = "dbm"
DIMENSION_DB: Final = "db"
DIMENSION_RATIO: Final = "ratio"
DIMENSION_MAGNETIC_FIELD: Final = "magnetic_field"


@dataclass(frozen=True, slots=True)
class UnitDefinition:
    dimension: str
    scale: float
    display: str


_UNITS: Final[dict[str, UnitDefinition]] = {
    "v": UnitDefinition(DIMENSION_VOLTAGE, 1.0, "V"),
    "mv": UnitDefinition(DIMENSION_VOLTAGE, 1e-3, "mV"),
    "uv": UnitDefinition(DIMENSION_VOLTAGE, 1e-6, "uV"),
    "kv": UnitDefinition(DIMENSION_VOLTAGE, 1e3, "kV"),
    "a": UnitDefinition(DIMENSION_CURRENT, 1.0, "A"),
    "ma": UnitDefinition(DIMENSION_CURRENT, 1e-3, "mA"),
    "ua": UnitDefinition(DIMENSION_CURRENT, 1e-6, "uA"),
    "na": UnitDefinition(DIMENSION_CURRENT, 1e-9, "nA"),
    "w": UnitDefinition(DIMENSION_POWER, 1.0, "W"),
    "mw": UnitDefinition(DIMENSION_POWER, 1e-3, "mW"),
    "uw": UnitDefinition(DIMENSION_POWER, 1e-6, "uW"),
    "nw": UnitDefinition(DIMENSION_POWER, 1e-9, "nW"),
    "hz": UnitDefinition(DIMENSION_FREQUENCY, 1.0, "Hz"),
    "khz": UnitDefinition(DIMENSION_FREQUENCY, 1e3, "kHz"),
    "mhz": UnitDefinition(DIMENSION_FREQUENCY, 1e6, "MHz"),
    "ghz": UnitDefinition(DIMENSION_FREQUENCY, 1e9, "GHz"),
    "ohm": UnitDefinition(DIMENSION_RESISTANCE, 1.0, "ohm"),
    "kohm": UnitDefinition(DIMENSION_RESISTANCE, 1e3, "kohm"),
    "mohm": UnitDefinition(DIMENSION_RESISTANCE, 1e6, "Mohm"),
    "milliohm": UnitDefinition(DIMENSION_RESISTANCE, 1e-3, "mΩ"),
    "s": UnitDefinition(DIMENSION_TIME, 1.0, "s"),
    "ms": UnitDefinition(DIMENSION_TIME, 1e-3, "ms"),
    "us": UnitDefinition(DIMENSION_TIME, 1e-6, "us"),
    "ns": UnitDefinition(DIMENSION_TIME, 1e-9, "ns"),
    "dbm": UnitDefinition(DIMENSION_DBM, 1.0, "dBm"),
    "db": UnitDefinition(DIMENSION_DB, 1.0, "dB"),
    "%": UnitDefinition(DIMENSION_RATIO, 0.01, "%"),
    "t": UnitDefinition(DIMENSION_MAGNETIC_FIELD, 1.0, "T"),
    "mt": UnitDefinition(DIMENSION_MAGNETIC_FIELD, 1e-3, "mT"),
    "ut": UnitDefinition(DIMENSION_MAGNETIC_FIELD, 1e-6, "uT"),
}

_QUANTITY_RE: Final = re.compile(
    r"^\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?)\s*([^\s]+)?\s*$"
)


@dataclass(frozen=True, slots=True)
class Quantity:
    """Finite value normalised to SI, except dBm which is logarithmic by definition."""

    si_value: float
    dimension: str

    def require_dimension(self, expected: str) -> "Quantity":
        if self.dimension != expected:
            raise QuantityError(
                f"Expected dimension {expected}, received {self.dimension}."
            )
        return self

    def as_unit(self, unit: str) -> float:
        definition = _unit_definition(unit)
        if definition.dimension != self.dimension:
            raise QuantityError(
                f"Cannot convert {self.dimension} to {definition.dimension}."
            )
        return self.si_value / definition.scale

    def format(self, unit: str, precision: int = 9) -> str:
        definition = _unit_definition(unit)
        value = self.as_unit(unit)
        return f"{value:.{precision}g} {definition.display}"


def _canonical_unit(unit: str) -> str:
    normalized = unicodedata.normalize("NFKC", unit.strip())
    omega_aliases = {
        "Ω": "ohm",
        "ω": "ohm",
        "kΩ": "kohm",
        "KΩ": "kohm",
        "kω": "kohm",
        "Kω": "kohm",
        "MΩ": "Mohm",
        "Mω": "Mohm",
        "mΩ": "milliohm",
        "mω": "milliohm",
    }
    if normalized in omega_aliases:
        return omega_aliases[normalized].lower()
    return normalized.replace("μ", "u").replace("µ", "u").lower()


def _unit_definition(unit: str) -> UnitDefinition:
    normalized = _canonical_unit(unit)
    try:
        return _UNITS[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted({item.display for item in _UNITS.values()}))
        raise QuantityError(f"Unknown unit {unit!r}. Allowed units: {allowed}.") from exc


def parse_quantity(
    value: str | int | float | Quantity,
    expected_dimension: str | None = None,
    *,
    require_unit: bool = True,
) -> Quantity:
    """Parse an explicit quantity and reject non-finite numbers or unknown units."""

    if isinstance(value, Quantity):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if require_unit:
            raise QuantityError("The value must include an explicit unit.")
        result = Quantity(float(value), expected_dimension or DIMENSION_RATIO)
    elif isinstance(value, str):
        match = _QUANTITY_RE.match(value)
        if match is None:
            raise QuantityError(f"Invalid quantity: {value!r}.")
        numeric_text, unit = match.groups()
        if unit is None:
            if require_unit:
                raise QuantityError(f"Missing unit in value {value!r}.")
            result = Quantity(float(numeric_text.replace(",", ".")), expected_dimension or DIMENSION_RATIO)
        else:
            numeric = float(numeric_text.replace(",", "."))
            definition = _unit_definition(unit)
            result = Quantity(numeric * definition.scale, definition.dimension)
    else:
        raise QuantityError(f"Unsupported value type: {type(value).__name__}.")

    if not math.isfinite(result.si_value):
        raise QuantityError("The value must be finite.")
    if expected_dimension is not None:
        result.require_dimension(expected_dimension)
    return result


def quantity_range(
    minimum: str | int | float | Quantity,
    maximum: str | int | float | Quantity,
    expected_dimension: str,
) -> tuple[Quantity, Quantity]:
    """Parse and validate an inclusive range."""

    lower = parse_quantity(minimum, expected_dimension)
    upper = parse_quantity(maximum, expected_dimension)
    if lower.si_value > upper.si_value:
        raise QuantityError("The minimum range value is greater than the maximum.")
    return lower, upper


_AUTO_DISPLAY_UNITS: Final[dict[str, tuple[str, ...]]] = {
    DIMENSION_VOLTAGE: ("kV", "V", "mV", "uV"),
    DIMENSION_CURRENT: ("A", "mA", "uA", "nA"),
    DIMENSION_POWER: ("W", "mW", "uW", "nW"),
    DIMENSION_FREQUENCY: ("GHz", "MHz", "kHz", "Hz"),
    DIMENSION_RESISTANCE: ("Mohm", "kohm", "ohm"),
    DIMENSION_TIME: ("s", "ms", "us", "ns"),
    DIMENSION_DBM: ("dBm",),
    DIMENSION_DB: ("dB",),
    DIMENSION_RATIO: ("%",),
    DIMENSION_MAGNETIC_FIELD: ("T", "mT", "uT"),
}


def format_quantity_auto(value_si: float, dimension: str, precision: int = 9) -> str:
    """Format an SI value using a readable engineering unit for its dimension."""

    if not math.isfinite(value_si):
        raise QuantityError("The value must be finite.")
    try:
        units = _AUTO_DISPLAY_UNITS[dimension]
    except KeyError as exc:
        raise QuantityError(f"Unsupported dimension: {dimension}.") from exc
    quantity = Quantity(value_si, dimension)
    if dimension in {DIMENSION_DBM, DIMENSION_DB, DIMENSION_RATIO}:
        return quantity.format(units[0], precision)
    magnitude = abs(value_si)
    if magnitude == 0:
        base_unit = next((unit for unit in units if _unit_definition(unit).scale == 1.0), units[-1])
        return quantity.format(base_unit, precision)
    selected = units[-1]
    for unit in units:
        scale = _unit_definition(unit).scale
        converted = magnitude / scale
        if converted >= 1:
            selected = unit
            break
    return quantity.format(selected, precision)
