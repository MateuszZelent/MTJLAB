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


def plot_theme(tokens: ThemeTokens, *, publication: bool = False) -> PlotTheme:
    """Resolve plot colours from application tokens on screen and paper style for publication."""
    if publication:
        return PlotTheme(
            background=PAPER_STYLE.colour("figure.facecolor"),
            axes=PAPER_STYLE.colour("axes.labelcolor"),
            grid=PAPER_STYLE.colour("grid.color"),
            reference=PAPER_STYLE.colour_cycle[1],
            measurement=PAPER_STYLE.colour_cycle[0],
            danger=PAPER_STYLE.colour_cycle[3],
            success=PAPER_STYLE.colour_cycle[2],
        )
    return PlotTheme(
        background=tokens.plot_background,
        axes=tokens.plot_axes,
        grid=tokens.plot_grid,
        reference=tokens.plot_reference,
        measurement=tokens.plot_measurement,
        danger=tokens.danger,
        success=tokens.success,
    )
