"""Map actionable configuration failures to their editable settings fields."""

from __future__ import annotations

from dataclasses import dataclass
import re


SettingsPath = tuple[str | int, ...]


@dataclass(frozen=True)
class SettingsIssue:
    """A configuration problem that can be corrected in the Settings workspace."""

    paths: tuple[SettingsPath, ...]
    message: str


@dataclass(frozen=True)
class RecipeDutIssue:
    """A missing or exceeded DUT envelope owned by the recipe document."""

    device: str
    channel: str | int | None
    message: str


_ANRITSU_SAFETY = ("devices", "anritsu", "safety")
_ANRITSU_ACQUISITION_PATHS = (
    _ANRITSU_SAFETY + ("acquisition_allowed",),
    _ANRITSU_SAFETY + ("rf_input", "max_expected_power_at_connector"),
    _ANRITSU_SAFETY + ("frequency", "min"),
    _ANRITSU_SAFETY + ("frequency", "max"),
)


def settings_issue_for_error(error: Exception | str) -> SettingsIssue | None:
    """Return the precise fields an operator can fix for a known safety error.

    This deliberately recognises only configuration failures.  A device fault,
    compliance trip, or other runtime safety event must remain an error and
    must never be presented as something this UI can clear.
    """

    message = str(error)
    normalized = message.casefold()
    # Hardware ceilings and recipe-owned DUT envelopes cannot be corrected by
    # changing the station profile. Never offer a misleading Settings route.
    if any(
        marker in normalized
        for marker in ("recipe dut", "recipe.dut_limits", "hardware limit", "hardware maximum", "documented")
    ):
        return None
    if "anritsu acquisition is locked by the safety profile" in normalized:
        return SettingsIssue(_ANRITSU_ACQUISITION_PATHS, message)
    if "maximum expected rf power" in normalized or "anritsu rf input limit" in normalized:
        return SettingsIssue(
            (_ANRITSU_SAFETY + ("rf_input", "max_expected_power_at_connector"),),
            message,
        )
    if "permitted anritsu frequency range" in normalized or "complete frequency limit" in normalized:
        return SettingsIssue(
            (
                _ANRITSU_SAFETY + ("frequency", "min"),
                _ANRITSU_SAFETY + ("frequency", "max"),
            ),
            message,
        )
    if "anritsu sg frequency" in normalized and "configured range" in normalized:
        return SettingsIssue(
            (
                ("devices", "anritsu", "signal_generator", "frequency", "min"),
                ("devices", "anritsu", "signal_generator", "frequency", "max"),
            ),
            message,
        )
    if "anritsu sg power" in normalized and "configured range" in normalized:
        return SettingsIssue(
            (
                ("devices", "anritsu", "signal_generator", "power", "min"),
                ("devices", "anritsu", "signal_generator", "power", "max"),
            ),
            message,
        )
    if "anritsu range" in normalized and "configured range" in normalized:
        return SettingsIssue(
            (
                _ANRITSU_SAFETY + ("frequency", "min"),
                _ANRITSU_SAFETY + ("frequency", "max"),
            ),
            message,
        )
    permission_paths = {
        "keithley": ("devices", "keithley", "safety", "allow_output_enable"),
        "rigol": ("devices", "rigol", "safety", "allow_output_enable"),
        "anritsu_sg": _ANRITSU_SAFETY + ("signal_generator_output_allowed",),
    }
    if "output permission is disabled" in normalized:
        for device, path in permission_paths.items():
            if device in normalized:
                return SettingsIssue((path,), message)

    keithley_field = next(
        (
            field
            for marker, field in (
                ("source current", "source_current"),
                ("current level", "source_current"),
                ("source voltage", "source_voltage"),
                ("voltage level", "source_voltage"),
                ("voltage compliance", "voltage_compliance"),
                ("current compliance", "current_compliance"),
                ("measured current", "measured_current_trip"),
                ("measured voltage", "measured_voltage_trip"),
                ("worst-case source", "max_abs_power"),
                ("station profile max_abs_power", "max_abs_power"),
            )
            if marker in normalized
        ),
        None,
    )
    if keithley_field is not None and any(
        marker in normalized for marker in ("outside", "exceeds", "station profile")
    ):
        channel_match = re.search(r"(?:keithley|channel)\s+([ab])", normalized)
        channels = (channel_match.group(1).upper(),) if channel_match else ("A", "B")
        return SettingsIssue(
            tuple(
                ("devices", "keithley", "safety", "channels", channel, "lab_limits", keithley_field)
                for channel in channels
            ),
            message,
        )

    rigol_field = next(
        (
            field
            for marker, field in (
                ("load current", "estimated_load_current"),
                ("dut power", "estimated_load_power"),
                ("sweep step count", "sweep_steps"),
                ("sweep time", "sweep_duration"),
                ("sweep duration", "sweep_duration"),
                ("modulation rate", "modulation_rate"),
                ("burst period", "burst_period"),
                ("burst cycle", "burst_cycles"),
                ("amplitude_vpp", "amplitude_vpp"),
                ("high_level", "high_level"),
                ("low_level", "low_level"),
                ("offset", "offset"),
                ("frequency", "frequency"),
            )
            if marker in normalized
        ),
        None,
    )
    if rigol_field is not None and any(
        marker in normalized for marker in ("configured", "station range")
    ):
        channel_match = re.search(r"(?:ch|channel\s*)([12])", normalized)
        channels = (channel_match.group(1),) if channel_match else ("1", "2")
        return SettingsIssue(
            tuple(
                ("devices", "rigol", "safety", "channels", channel, "lab_limits", rigol_field)
                for channel in channels
            ),
            message,
        )
    return None


def recipe_dut_issue_for_error(error: Exception | str) -> RecipeDutIssue | None:
    """Locate DUT-limit failures that cannot be fixed in station Settings."""

    message = str(error)
    normalized = message.casefold()
    if "recipe.dut_limits" in normalized:
        match = re.search(
            r"output for (keithley|rigol|anritsu_sg) channel ([a-z0-9]+)",
            normalized,
        )
        if match:
            device = "anritsu" if match.group(1) == "anritsu_sg" else match.group(1)
            channel: str | int | None = match.group(2).upper()
            if device == "rigol":
                channel = int(str(channel))
            if device == "anritsu":
                channel = None
            return RecipeDutIssue(device, channel, message)
    if "recipe dut limit" in normalized or "dut limit" in normalized:
        if "keithley" in normalized or "source current" in normalized or "source voltage" in normalized:
            channel_match = re.search(r"keithley\s+([ab])", normalized)
            return RecipeDutIssue(
                "keithley",
                channel_match.group(1).upper() if channel_match else None,
                message,
            )
        if "rigol" in normalized:
            channel_match = re.search(r"(?:ch|channel\s*)([12])", normalized)
            return RecipeDutIssue(
                "rigol", int(channel_match.group(1)) if channel_match else None, message
            )
        if "anritsu" in normalized:
            return RecipeDutIssue("anritsu", None, message)
    return None
