"""Map actionable configuration failures to their editable settings fields."""

from __future__ import annotations

from dataclasses import dataclass


SettingsPath = tuple[str | int, ...]


@dataclass(frozen=True)
class SettingsIssue:
    """A configuration problem that can be corrected in the Settings workspace."""

    paths: tuple[SettingsPath, ...]
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
