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
