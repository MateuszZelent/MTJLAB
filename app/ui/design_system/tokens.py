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
    # Catppuccin Latte: every UI surface stays below pure white; only plotting
    # canvases use white so dense measurement grids remain crisp.
    background="#e6e9ef", surface="#eff1f5", surface_raised="#dce0e8",
    border="#bcc0cc", text_primary="#4c4f69", text_muted="#6c6f85",
    accent="#1e66f5", focus="#7287fd", success="#40a02b",
    caution="#df8e1d", danger="#d20f39", neutral="#7c7f93",
    output_active="#d20f39", compliance="#df8e1d", interlock="#df8e1d",
    emergency="#d20f39", plot_background="#ffffff", plot_axes="#5c5f77",
    plot_grid="#ccd0da", plot_reference="#8839ef",
    plot_measurement="#1e66f5",
)

_DARK = ThemeTokens(
    # Catppuccin Mocha.
    background="#1e1e2e", surface="#181825", surface_raised="#313244",
    border="#45475a", text_primary="#cdd6f4", text_muted="#a6adc8",
    accent="#89b4fa", focus="#b4befe", success="#a6e3a1",
    caution="#f9e2af", danger="#f38ba8", neutral="#9399b2",
    output_active="#f38ba8", compliance="#f9e2af", interlock="#f9e2af",
    emergency="#f38ba8", plot_background="#181825", plot_axes="#bac2de",
    plot_grid="#45475a", plot_reference="#cba6f7",
    plot_measurement="#89b4fa",
)


def tokens_for(theme: str) -> ThemeTokens:
    normalized = theme.strip().lower()
    if normalized == "light":
        return _LIGHT
    if normalized == "dark":
        return _DARK
    raise ValueError("Theme must be light or dark.")
