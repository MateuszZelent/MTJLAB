from __future__ import annotations

from dataclasses import dataclass

from .paper_plot_style import PAPER_STYLE
from .tokens import ThemeTokens


@dataclass(frozen=True, slots=True)
class PlotTheme:
    background: str
    axes: str
    grid: str
    reference: str
    measurement: str
    danger: str
    success: str


def plot_theme(tokens: ThemeTokens) -> PlotTheme:
    """Keep paper figures consistent while the surrounding application changes theme."""
    return PlotTheme(
        background=PAPER_STYLE.colour("figure.facecolor"),
        axes=PAPER_STYLE.colour("axes.labelcolor"),
        grid=PAPER_STYLE.colour("grid.color"),
        reference=PAPER_STYLE.colour_cycle[1],
        measurement=PAPER_STYLE.colour_cycle[0],
        danger=PAPER_STYLE.colour_cycle[3],
        success=PAPER_STYLE.colour_cycle[2],
    )
