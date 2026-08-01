"""Derive coherent Keithley safety-limit edits before they are staged."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Mapping

from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_POWER,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    QuantityError,
    format_quantity_auto,
    parse_quantity,
)
from app.safety.keithley import (
    KEITHLEY_2602A_MAX_CURRENT_RANGE_A,
    KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V,
)


@dataclass(frozen=True, slots=True)
class KeithleyLimitAdjustment:
    """One dependent lab-limit leaf that needs an explicit operator approval."""

    path: tuple[str, ...]
    previous: str
    proposed: str
    reason: str


@dataclass(frozen=True, slots=True)
class KeithleyLimitProposal:
    """The dependent changes required to keep a primary limit edit."""

    primary_path: tuple[str, ...]
    primary_value: str
    adjustments: tuple[KeithleyLimitAdjustment, ...]


_RANGE_DIMENSIONS = {
    "source_current": DIMENSION_CURRENT,
    "current_compliance": DIMENSION_CURRENT,
    "measured_current_trip": DIMENSION_CURRENT,
    "source_voltage": DIMENSION_VOLTAGE,
    "voltage_compliance": DIMENSION_VOLTAGE,
    "measured_voltage_trip": DIMENSION_VOLTAGE,
    "point_settle_time": DIMENSION_TIME,
}


def propose_keithley_limit_adjustments(
    limits: Mapping[str, Any], primary_path: tuple[str, ...], primary_value: str
) -> KeithleyLimitProposal:
    """Return dependent limits needed to preserve one explicit-unit primary edit.

    The primary leaf remains outside ``adjustments`` so callers can keep it as
    the central action and ask approval only for the values that must follow.
    """

    if not primary_path:
        raise ConfigurationError("A Keithley limit edit must name a limit leaf.")
    draft = deepcopy(dict(limits))
    _set_leaf(draft, primary_path, primary_value)
    _validate_primary_hardware_boundary(draft, primary_path)

    adjustments: list[KeithleyLimitAdjustment] = []
    range_name = primary_path[0]
    if range_name in {"source_current", "source_voltage"}:
        _synchronise_max_abs(draft, range_name, adjustments)

    if primary_path == ("max_abs_power",):
        _reduce_enabled_envelopes_for_power(draft, adjustments)
    if range_name in {"measured_current_trip", "measured_voltage_trip"}:
        _reduce_enabled_envelopes_for_trip(draft, range_name, adjustments)
    _expand_trip_for_current_envelope(draft, adjustments)
    _expand_trip_for_voltage_envelope(draft, adjustments)
    if primary_path != ("max_abs_power",):
        _raise_power_for_enabled_envelopes(draft, adjustments)
    return KeithleyLimitProposal(primary_path, primary_value, tuple(adjustments))


def _set_leaf(draft: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    current: Any = draft
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise ConfigurationError(f"Unknown Keithley limit path {'.'.join(path)}.")
        current = current[part]
    if not isinstance(current, dict) or path[-1] not in current:
        raise ConfigurationError(f"Unknown Keithley limit path {'.'.join(path)}.")
    dimension = _dimension_for_path(path)
    try:
        parse_quantity(value, dimension, require_unit=True)
    except QuantityError as exc:
        raise ConfigurationError(str(exc)) from exc
    current[path[-1]] = value
    if len(path) == 2 and path[1] in {"min", "max"}:
        _checked_range(draft, path[0])


def _dimension_for_path(path: tuple[str, ...]) -> str:
    if path == ("max_abs_power",):
        return DIMENSION_POWER
    try:
        return _RANGE_DIMENSIONS[path[0]]
    except KeyError as exc:
        raise ConfigurationError(f"Unsupported Keithley limit path {'.'.join(path)}.") from exc


def _checked_range(draft: Mapping[str, Any], name: str) -> tuple[float, float]:
    try:
        values = draft[name]
        dimension = _RANGE_DIMENSIONS[name]
        minimum = parse_quantity(values["min"], dimension).si_value
        maximum = parse_quantity(values["max"], dimension).si_value
    except (KeyError, TypeError, QuantityError) as exc:
        raise ConfigurationError(f"Invalid Keithley {name} safety range.") from exc
    if minimum > maximum:
        raise ConfigurationError(f"Keithley {name} minimum cannot exceed its maximum.")
    return minimum, maximum


def _range_is_enabled(draft: Mapping[str, Any], name: str) -> bool:
    values = draft.get(name)
    return isinstance(values, Mapping) and bool(values.get("enabled", True))


def _range_abs(draft: Mapping[str, Any], name: str) -> float:
    minimum, maximum = _checked_range(draft, name)
    return max(abs(minimum), abs(maximum))


def _synchronise_max_abs(
    draft: dict[str, Any], name: str, adjustments: list[KeithleyLimitAdjustment]
) -> None:
    values = draft[name]
    existing = values.get("max_abs")
    if existing is None:
        return
    proposed = format_quantity_auto(_range_abs(draft, name), _RANGE_DIMENSIONS[name])
    if proposed != existing:
        adjustments.append(
            KeithleyLimitAdjustment(
                (name, "max_abs"),
                str(existing),
                proposed,
                "Synchronise the absolute source envelope with the edited range.",
            )
        )
        values["max_abs"] = proposed


def _expand_trip_for_current_envelope(
    draft: dict[str, Any], adjustments: list[KeithleyLimitAdjustment]
) -> None:
    if not _range_is_enabled(draft, "measured_current_trip"):
        return
    required_minimum, required_maximum = _checked_range(draft, "measured_current_trip")
    if _range_is_enabled(draft, "source_current"):
        source_minimum, source_maximum = _checked_range(draft, "source_current")
        required_minimum = min(required_minimum, source_minimum)
        required_maximum = max(required_maximum, source_maximum)
    if _range_is_enabled(draft, "current_compliance"):
        required_maximum = max(required_maximum, _range_abs(draft, "current_compliance"))
    _expand_range_boundaries(
        draft,
        "measured_current_trip",
        required_minimum,
        required_maximum,
        DIMENSION_CURRENT,
        "Cover the configured current source or compliance envelope.",
        adjustments,
    )


def _expand_trip_for_voltage_envelope(
    draft: dict[str, Any], adjustments: list[KeithleyLimitAdjustment]
) -> None:
    if not _range_is_enabled(draft, "measured_voltage_trip"):
        return
    required_minimum, required_maximum = _checked_range(draft, "measured_voltage_trip")
    if _range_is_enabled(draft, "source_voltage"):
        source_minimum, source_maximum = _checked_range(draft, "source_voltage")
        required_minimum = min(required_minimum, source_minimum)
        required_maximum = max(required_maximum, source_maximum)
    if _range_is_enabled(draft, "voltage_compliance"):
        required_maximum = max(required_maximum, _range_abs(draft, "voltage_compliance"))
    _expand_range_boundaries(
        draft,
        "measured_voltage_trip",
        required_minimum,
        required_maximum,
        DIMENSION_VOLTAGE,
        "Cover the configured voltage source or compliance envelope.",
        adjustments,
    )


def _expand_range_boundaries(
    draft: dict[str, Any],
    name: str,
    required_minimum: float,
    required_maximum: float,
    dimension: str,
    reason: str,
    adjustments: list[KeithleyLimitAdjustment],
) -> None:
    values = draft[name]
    minimum, maximum = _checked_range(draft, name)
    for boundary, current, required in (
        ("min", minimum, required_minimum),
        ("max", maximum, required_maximum),
    ):
        if current == required:
            continue
        proposed = format_quantity_auto(required, dimension)
        adjustments.append(
            KeithleyLimitAdjustment((name, boundary), str(values[boundary]), proposed, reason)
        )
        values[boundary] = proposed


def _raise_power_for_enabled_envelopes(
    draft: dict[str, Any], adjustments: list[KeithleyLimitAdjustment]
) -> None:
    if not bool(draft.get("max_abs_power_enabled", True)):
        return
    products: list[float] = []
    if _range_is_enabled(draft, "source_current") and _range_is_enabled(
        draft, "voltage_compliance"
    ):
        products.append(
            _range_abs(draft, "source_current") * _range_abs(draft, "voltage_compliance")
        )
    if _range_is_enabled(draft, "source_voltage") and _range_is_enabled(
        draft, "current_compliance"
    ):
        products.append(
            _range_abs(draft, "source_voltage") * _range_abs(draft, "current_compliance")
        )
    if not products:
        return
    try:
        configured = parse_quantity(draft["max_abs_power"], DIMENSION_POWER).si_value
    except (KeyError, QuantityError) as exc:
        raise ConfigurationError("Invalid Keithley maximum absolute power limit.") from exc
    required = max(products)
    if configured >= required:
        return
    proposed = format_quantity_auto(required, DIMENSION_POWER)
    adjustments.append(
        KeithleyLimitAdjustment(
            ("max_abs_power",),
            str(draft["max_abs_power"]),
            proposed,
            "Cover the worst-case source multiplied by compliance power.",
        )
    )
    draft["max_abs_power"] = proposed


def _reduce_enabled_envelopes_for_power(
    draft: dict[str, Any], adjustments: list[KeithleyLimitAdjustment]
) -> None:
    try:
        maximum_power_w = parse_quantity(draft["max_abs_power"], DIMENSION_POWER).si_value
    except (KeyError, QuantityError) as exc:
        raise ConfigurationError("Invalid Keithley maximum absolute power limit.") from exc
    if maximum_power_w <= 0:
        raise ConfigurationError("Keithley maximum absolute power must be positive.")
    _reduce_pair_for_power(
        draft,
        "source_current",
        "voltage_compliance",
        maximum_power_w,
        DIMENSION_VOLTAGE,
        adjustments,
    )
    _reduce_pair_for_power(
        draft,
        "source_voltage",
        "current_compliance",
        maximum_power_w,
        DIMENSION_CURRENT,
        adjustments,
    )


def _reduce_pair_for_power(
    draft: dict[str, Any],
    source_name: str,
    compliance_name: str,
    maximum_power_w: float,
    compliance_dimension: str,
    adjustments: list[KeithleyLimitAdjustment],
) -> None:
    if not (_range_is_enabled(draft, source_name) and _range_is_enabled(draft, compliance_name)):
        return
    source_magnitude = _range_abs(draft, source_name)
    compliance_magnitude = _range_abs(draft, compliance_name)
    if source_magnitude * compliance_magnitude <= maximum_power_w:
        return
    allowed_compliance = maximum_power_w / source_magnitude
    if _cap_range_magnitude(
        draft,
        compliance_name,
        allowed_compliance,
        compliance_dimension,
        "Keep the configured source x compliance envelope within maximum power.",
        adjustments,
    ):
        return
    minimum_compliance, _maximum_compliance = _checked_range(draft, compliance_name)
    minimum_magnitude = abs(minimum_compliance)
    if minimum_magnitude == 0:
        raise ConfigurationError(
            f"Keithley {compliance_name} cannot be reduced enough to honour maximum power."
        )
    allowed_source = maximum_power_w / minimum_magnitude
    if not _cap_range_magnitude(
        draft,
        source_name,
        allowed_source,
        _RANGE_DIMENSIONS[source_name],
        "Keep the configured source x compliance envelope within maximum power.",
        adjustments,
    ):
        raise ConfigurationError(
            f"Keithley {source_name} and {compliance_name} cannot honour maximum power."
        )
    _synchronise_max_abs(draft, source_name, adjustments)
    _cap_range_magnitude(
        draft,
        compliance_name,
        maximum_power_w / _range_abs(draft, source_name),
        compliance_dimension,
        "Keep the configured source x compliance envelope within maximum power.",
        adjustments,
    )


def _reduce_enabled_envelopes_for_trip(
    draft: dict[str, Any],
    trip_name: str,
    adjustments: list[KeithleyLimitAdjustment],
) -> None:
    if trip_name == "measured_current_trip":
        source_name = "source_current"
        compliance_name = "current_compliance"
        dimension = DIMENSION_CURRENT
    else:
        source_name = "source_voltage"
        compliance_name = "voltage_compliance"
        dimension = DIMENSION_VOLTAGE
    trip_minimum, trip_maximum = _checked_range(draft, trip_name)
    if _range_is_enabled(draft, source_name):
        _cap_range_to_interval(
            draft,
            source_name,
            trip_minimum,
            trip_maximum,
            dimension,
            f"Keep {source_name} inside the edited {trip_name} range.",
            adjustments,
        )
        _synchronise_max_abs(draft, source_name, adjustments)
    if _range_is_enabled(draft, compliance_name):
        _cap_range_magnitude(
            draft,
            compliance_name,
            max(abs(trip_minimum), abs(trip_maximum)),
            dimension,
            f"Keep {compliance_name} inside the edited {trip_name} range.",
            adjustments,
        )


def _cap_range_to_interval(
    draft: dict[str, Any],
    name: str,
    allowed_minimum: float,
    allowed_maximum: float,
    dimension: str,
    reason: str,
    adjustments: list[KeithleyLimitAdjustment],
) -> None:
    minimum, maximum = _checked_range(draft, name)
    capped_minimum = max(minimum, allowed_minimum)
    capped_maximum = min(maximum, allowed_maximum)
    if capped_minimum > capped_maximum:
        raise ConfigurationError(
            f"Keithley {name} cannot fit inside the edited measurement trip range."
        )
    values = draft[name]
    for boundary, current, capped in (
        ("min", minimum, capped_minimum),
        ("max", maximum, capped_maximum),
    ):
        if current == capped:
            continue
        proposed = format_quantity_auto(capped, dimension)
        adjustments.append(
            KeithleyLimitAdjustment((name, boundary), str(values[boundary]), proposed, reason)
        )
        values[boundary] = proposed


def _cap_range_magnitude(
    draft: dict[str, Any],
    name: str,
    maximum_magnitude: float,
    dimension: str,
    reason: str,
    adjustments: list[KeithleyLimitAdjustment],
) -> bool:
    minimum, maximum = _checked_range(draft, name)
    capped_minimum = max(minimum, -maximum_magnitude)
    capped_maximum = min(maximum, maximum_magnitude)
    if capped_minimum > capped_maximum:
        if not math.isclose(capped_minimum, capped_maximum, rel_tol=1e-12):
            return False
        capped_maximum = capped_minimum
    values = draft[name]
    for boundary, current, capped in (
        ("min", minimum, capped_minimum),
        ("max", maximum, capped_maximum),
    ):
        if current == capped:
            continue
        proposed = format_quantity_auto(capped, dimension)
        adjustments.append(
            KeithleyLimitAdjustment((name, boundary), str(values[boundary]), proposed, reason)
        )
        values[boundary] = proposed
    return True


def _validate_primary_hardware_boundary(
    draft: Mapping[str, Any], primary_path: tuple[str, ...]
) -> None:
    dimension = _RANGE_DIMENSIONS.get(primary_path[0])
    if dimension not in {DIMENSION_CURRENT, DIMENSION_VOLTAGE}:
        return
    magnitude = _range_abs(draft, primary_path[0])
    maximum = (
        KEITHLEY_2602A_MAX_CURRENT_RANGE_A
        if dimension == DIMENSION_CURRENT
        else KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V
    )
    if magnitude > maximum:
        raise ConfigurationError(
            f"Keithley {primary_path[0]} exceeds the immutable 2602A hardware limit."
        )
