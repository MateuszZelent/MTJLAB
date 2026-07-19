from __future__ import annotations

from dataclasses import dataclass

from .tokens import ThemeTokens


@dataclass(frozen=True, slots=True)
class PlotTheme:
    background: str
    axes: str
    grid: str
    reference: str
    measurement: str


def plot_theme(tokens: ThemeTokens) -> PlotTheme:
    return PlotTheme(
        background=tokens.plot_background,
        axes=tokens.plot_axes,
        grid=tokens.plot_grid,
        reference=tokens.plot_reference,
        measurement=tokens.plot_measurement,
    )
