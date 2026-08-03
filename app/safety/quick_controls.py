"""Authoritative UI preflight bounds for floating instrument controls."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.domain.quantities import format_quantity_auto, parse_quantity
from app.settings.models import RangeSettings, StationSettings
from app.safety.keithley import (
    KEITHLEY_2602A_MAX_CURRENT_RANGE_A,
    KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V,
)
from app.safety.rigol_current import (
    rigol_hardware_amplitude_bounds_vpp,
    rigol_hardware_frequency_max_hz,
    rigol_hardware_frequency_min_hz,
    rigol_hardware_voltage_bounds_v,
)


@dataclass(frozen=True, slots=True)
class QuickControlSafetyBound:
    minimum_si: float
    maximum_si: float
    minimum_text: str
    maximum_text: str


def _effective_range(
    configured: RangeSettings,
    dimension: str,
    *,
    hard_minimum_si: float | None = None,
    hard_maximum_si: float | None = None,
) -> QuickControlSafetyBound:
    if not configured.enabled:
        minimum = hard_minimum_si if hard_minimum_si is not None else -math.inf
        maximum = hard_maximum_si if hard_maximum_si is not None else math.inf
        return QuickControlSafetyBound(
            minimum,
            maximum,
            format_quantity_auto(minimum, dimension)
            if math.isfinite(minimum)
            else "HARDWARE",
            format_quantity_auto(maximum, dimension)
            if math.isfinite(maximum)
            else "HARDWARE",
        )
    configured_minimum = parse_quantity(configured.min, dimension).si_value
    configured_maximum = parse_quantity(configured.max, dimension).si_value
    minimum = configured_minimum
    maximum = configured_maximum
    if configured.max_abs is not None:
        maximum_absolute = parse_quantity(configured.max_abs, dimension).si_value
        minimum = max(minimum, -maximum_absolute)
        maximum = min(maximum, maximum_absolute)
    if hard_minimum_si is not None:
        minimum = max(minimum, hard_minimum_si)
    if hard_maximum_si is not None:
        maximum = min(maximum, hard_maximum_si)
    return QuickControlSafetyBound(
        minimum,
        maximum,
        configured.min
        if minimum == configured_minimum
        else format_quantity_auto(minimum, dimension),
        configured.max
        if maximum == configured_maximum
        else format_quantity_auto(maximum, dimension),
    )


def quick_control_safety_bounds(
    settings: StationSettings,
) -> dict[str, QuickControlSafetyBound]:
    """Return the limits shared by device cards and Quick Controls.

    Adapters remain the final authority and additionally validate coupled
    constraints such as Rigol High/Low, estimated current and DUT power.
    """

    bounds: dict[str, QuickControlSafetyBound] = {}
    current_hardware_minimum = -KEITHLEY_2602A_MAX_CURRENT_RANGE_A
    current_hardware_maximum = KEITHLEY_2602A_MAX_CURRENT_RANGE_A
    voltage_hardware_minimum = -KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V
    voltage_hardware_maximum = KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V
    for channel, channel_settings in settings.keithley.safety.channels.items():
        limits = channel_settings.lab_limits
        bounds[f"keithley.{channel}.current"] = _effective_range(
            limits.source_current,
            "current",
            hard_minimum_si=current_hardware_minimum,
            hard_maximum_si=current_hardware_maximum,
        )
        bounds[f"keithley.{channel}.voltage"] = _effective_range(
            limits.source_voltage,
            "voltage",
            hard_minimum_si=voltage_hardware_minimum,
            hard_maximum_si=voltage_hardware_maximum,
        )
    rigol_voltage_minimum, rigol_voltage_maximum = rigol_hardware_voltage_bounds_v()
    rigol_amplitude_minimum, rigol_amplitude_maximum = (
        rigol_hardware_amplitude_bounds_vpp()
    )
    for channel, channel_settings in settings.rigol.safety.channels.items():
        limits = channel_settings.lab_limits
        bounds[f"rigol.{channel}.frequency"] = _effective_range(
            limits.frequency,
            "frequency",
            hard_minimum_si=rigol_hardware_frequency_min_hz(),
            hard_maximum_si=rigol_hardware_frequency_max_hz(),
        )
        bounds[f"rigol.{channel}.amplitude"] = _effective_range(
            limits.amplitude_vpp,
            "voltage",
            hard_minimum_si=rigol_amplitude_minimum,
            hard_maximum_si=rigol_amplitude_maximum,
        )
        bounds[f"rigol.{channel}.offset"] = _effective_range(
            limits.offset,
            "voltage",
            hard_minimum_si=rigol_voltage_minimum,
            hard_maximum_si=rigol_voltage_maximum,
        )
        bounds[f"rigol.{channel}.high_level"] = _effective_range(
            limits.high_level,
            "voltage",
            hard_minimum_si=rigol_voltage_minimum,
            hard_maximum_si=rigol_voltage_maximum,
        )
        bounds[f"rigol.{channel}.low_level"] = _effective_range(
            limits.low_level,
            "voltage",
            hard_minimum_si=rigol_voltage_minimum,
            hard_maximum_si=rigol_voltage_maximum,
        )
    return bounds


def quick_control_safety_bound(
    settings: StationSettings, target: str
) -> QuickControlSafetyBound:
    """Resolve one target through the same map used by every UI surface."""

    try:
        return quick_control_safety_bounds(settings)[target]
    except KeyError as exc:
        raise KeyError(f"Unknown quick-control target {target!r}.") from exc
