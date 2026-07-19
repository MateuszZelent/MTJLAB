from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


SPACING = MappingProxyType({"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24})
RADII = MappingProxyType({"sm": 4, "md": 8, "lg": 12})
TYPOGRAPHY = MappingProxyType({
    "caption": 9,
    "body": 10,
    "subtitle": 12,
    "title": 20,
})
MOTION = MappingProxyType({"fast_ms": 120, "normal_ms": 180})


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    background: str
    surface: str
    surface_raised: str
    border: str
    text_primary: str
    text_muted: str
    accent: str
    focus: str
    success: str
    caution: str
    danger: str
    neutral: str
    output_active: str
    compliance: str
    interlock: str
    emergency: str
    plot_background: str
    plot_axes: str
    plot_grid: str
    plot_reference: str
    plot_measurement: str


_LIGHT = ThemeTokens(
    background="#f5f7fa", surface="#ffffff", surface_raised="#f9fafb",
    border="#d6dce5", text_primary="#18202a", text_muted="#5f6b7a",
    accent="#0067c0", focus="#005fb8", success="#0f7b4f",
    caution="#8a5d00", danger="#b4233a", neutral="#667085",
    output_active="#b4233a", compliance="#9a6700", interlock="#9a6700",
    emergency="#a4262c", plot_background="#ffffff", plot_axes="#344054",
    plot_grid="#d0d5dd", plot_reference="#7a5af8",
    plot_measurement="#0067c0",
)

_DARK = ThemeTokens(
    background="#111418", surface="#1b1f24", surface_raised="#22272e",
    border="#343a43", text_primary="#f2f4f7", text_muted="#a6adbb",
    accent="#60a5fa", focus="#7dd3fc", success="#43c58a",
    caution="#f4c152", danger="#ff6b7d", neutral="#98a2b3",
    output_active="#ff6b7d", compliance="#f4c152", interlock="#f4c152",
    emergency="#e5484d", plot_background="#111418", plot_axes="#d0d5dd",
    plot_grid="#343a43", plot_reference="#a78bfa",
    plot_measurement="#60a5fa",
)


def tokens_for(theme: str) -> ThemeTokens:
    normalized = theme.strip().lower()
    if normalized == "light":
        return _LIGHT
    if normalized == "dark":
        return _DARK
    raise ValueError("Theme must be light or dark.")
