"""Human-readable settings validation messages shared by persistence and UI."""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError


_TECHNICAL_NAMES = {
    "measured_current_trip": "Measured current trip",
    "measured_voltage_trip": "Measured voltage trip",
    "source_current": "source current",
    "source_voltage": "source voltage",
    "current_compliance": "current compliance",
    "voltage_compliance": "voltage compliance",
    "max_abs_power": "maximum absolute power",
    "point_settle_time": "point settling time",
}


def _human_path(location: tuple[Any, ...]) -> str:
    parts: list[str] = []
    index = 0
    while index < len(location):
        value = str(location[index])
        if value == "devices" and index + 1 < len(location):
            parts.append(str(location[index + 1]).replace("_", " ").title())
            index += 2
            continue
        if value == "channels" and index + 1 < len(location):
            parts.append(f"Channel {location[index + 1]}")
            index += 2
            continue
        if value == "lab_limits":
            parts.append("Safety limits")
        elif value not in {"safety"}:
            parts.append(value.replace("_", " ").title())
        index += 1
    return " → ".join(parts) or "Settings"


def _human_message(message: str) -> str:
    cleaned = message.removeprefix("Value error, ").strip()
    for technical, label in _TECHNICAL_NAMES.items():
        cleaned = cleaned.replace(technical, label)
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _hint_for(message: str) -> str | None:
    lowered = message.lower()
    if "must contain the complete" in lowered or "must cover the maximum" in lowered:
        return (
            "Reduce the source/compliance range, increase the corresponding measured "
            "trip range, or select ‘Disable limit’ for the software limit you do not need."
        )
    if "expected dimension" in lowered or "invalid value or unit" in lowered:
        return "Enter the value with an explicit unit, for example 10 mA or 67 mV."
    return None


def format_settings_validation_error(error: Exception) -> str:
    """Return concise actionable text without Pydantic implementation details."""

    if isinstance(error, ValidationError):
        blocks: list[str] = []
        hints: list[str] = []
        for detail in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            message = _human_message(str(detail.get("msg", "Invalid value.")))
            blocks.append(f"• {_human_path(tuple(detail.get('loc', ())))}\n  {message}")
            hint = _hint_for(message)
            if hint and hint not in hints:
                hints.append(hint)
        result = "Check the following setting:\n\n" + "\n\n".join(blocks)
        if hints:
            result += "\n\nHow to fix:\n" + "\n".join(f"• {hint}" for hint in hints)
        return result + "\n\nNo changes were saved."

    text = str(error).strip()
    text = text.removeprefix("Invalid settings.yml:").strip()
    text = re.sub(r"\nFor further information visit https?://\S+", "", text)
    text = re.sub(r"\s*\[type=.+$", "", text, flags=re.DOTALL)
    return text or "The settings are invalid. No changes were saved."
