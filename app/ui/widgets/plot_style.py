"""Map the licensed MMPP paper profile to interactive and detached Qt figures."""

from __future__ import annotations

import math
import weakref
from math import ceil, floor

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QFont, QPainter, QPicture
from shiboken6 import isValid

from app.ui.design_system import plot_theme, tokens_for
from app.ui.design_system.paper_plot_style import PAPER_STYLE
from app.ui.design_system.tokens import PLOT_TYPOGRAPHY

_NON_SI_UNITS_LOWER = frozenset({
    "db",
    "dbm",
    "dbv",
    "dbw",
    "dbc",
    "dbi",
    "dbuv",
    "dbµv",
    "dbμv",
    "np",
    "ratio",
    "linear ratio",
    "%",
    "percent",
    "ppm",
    "ppb",
    "1",
    "a.u.",
    "au",
    "arb",
    "arb.",
    "counts",
    "count",
    "deg",
    "rad",
    "°",
    "samples",
    "points",
    "bins",
})

_NON_SI_UNIT_CHARS = frozenset("²³⁴⁵⁶⁷⁸⁹⁰^*·⋅×/ \t()[]")
_SI_PREFIX_CHARS = frozenset("yzafpnµuμmkMGTPEZY")
_PREFIXABLE_BASE_UNITS = frozenset({
    "Hz",
    "W",
    "V",
    "A",
    "s",
    "T",
    "ohm",
    "Ω",
    "F",
    "H",
    "J",
    "N",
    "Pa",
    "C",
    "S",
    "Wb",
    "K",
    "m",
    "G",
    "Oe",
})


def unit_allows_auto_si_prefix(unit: str | None) -> bool:
    """Return True only when linear SI auto-prefixing is mathematically valid.

    Logarithmic units (dB/dBm), dimensionless ratios, squared/powered units
    (W², mW²), compound units (W*Hz, V/Hz), and already-prefixed units (mW,
    MHz) must never be auto-scaled linearly by pyqtgraph.
    """
    if not unit:
        return False
    cleaned = str(unit).strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered in _NON_SI_UNITS_LOWER or lowered.startswith("db"):
        return False
    if any(ch in _NON_SI_UNIT_CHARS or ch.isdigit() for ch in cleaned):
        return False
    # Reject units that already carry an SI prefix (e.g. mW, MHz, kV, µA).
    return not (
        len(cleaned) >= 2
        and cleaned[0] in _SI_PREFIX_CHARS
        and cleaned[1:] in _PREFIXABLE_BASE_UNITS
    )


class _PaperGridItem(pg.GridItem):
    """One major grid below the data, without GridItem's adaptive alpha override."""

    def __init__(self, plot_item):
        super().__init__(textPen=None)
        # Never call setParent(plot_item): PlotItem.addItem(grid) reparents the
        # QGraphicsItem under ViewBox/scene, and dual QObject + QGraphicsItem
        # ownership causes native double-free / access violations on teardown.
        self._plot_item_ref = weakref.ref(plot_item)
        self.setTickSpacing(x=[None], y=[None])

    def generatePicture(self):
        self.picture = QPicture()
        plot_item = self._plot_item_ref()
        if plot_item is None or not isValid(plot_item):
            return
        view = getattr(plot_item, "vb", None)
        if view is None or not isValid(view):
            return
        painter = QPainter(self.picture)
        try:
            painter.setPen(self.opts["pen"])
            bounds = self.boundingRect().normalized()
            for index, (name, low, high, length) in enumerate((
                ("bottom", bounds.left(), bounds.right(), view.width()),
                ("left", bounds.top(), bounds.bottom(), view.height()),
            )):
                if not (length > 0 and math.isfinite(low) and math.isfinite(high) and high > low):
                    continue
                step = self.opts["tickSpacing"][index][0]
                if step is None:
                    axis = plot_item.getAxis(name)
                    if axis is None or not isValid(axis):
                        continue
                    levels = axis.tickValues(low, high, length)
                    values = levels[0][1] if levels else ()
                else:
                    if not (math.isfinite(step) and step > 0):
                        continue
                    values = (step * tick for tick in range(ceil(low / step), floor(high / step) + 1))
                for value in values:
                    if not low <= value <= high:
                        continue
                    if name == "bottom":
                        painter.drawLine(QPointF(value, bounds.top()), QPointF(value, bounds.bottom()))
                    else:
                        painter.drawLine(QPointF(bounds.left(), value), QPointF(bounds.right(), value))
        finally:
            painter.end()


def _paper_font(size):
    font = QFont()
    font.setFamilies(list(PAPER_STYLE.font_families))
    font.setPointSizeF(size)
    return font


def _style_grid(plot_item, *, grid_color: str, alpha: float):
    # AxisItem draws grids above images and couples them to tick strokes.
    plot_item.showGrid(x=False, y=False)
    grid = getattr(plot_item, "_paper_grid", None)
    if grid is None or not isValid(grid):
        grid = plot_item._paper_grid = _PaperGridItem(plot_item)
    if grid not in plot_item.items:
        plot_item.addItem(grid, ignoreBounds=True)
    colour = pg.mkColor(grid_color)
    colour.setAlphaF(alpha)
    # Cosmetic pens use logical pixels; detached rendering scales 96 dpi to print dpi.
    grid.setPen(pg.mkPen(colour, width=PAPER_STYLE.number("grid.linewidth") * 96 / 72,
                         style=Qt.PenStyle.DashLine, cosmetic=True))
    grid.setZValue(-10 if PAPER_STYLE.boolean("axes.axisbelow") else 10)
    grid.setVisible(PAPER_STYLE.boolean("axes.grid"))


def _style_legend_entries(legend):
    font = legend.font()
    family = ", ".join(PAPER_STYLE.font_families)
    size = legend.labelTextSize()
    colour = legend.labelTextColor()
    colour_hex = pg.mkColor(colour).name()
    view = legend.getViewWidget()
    em = font.pointSizeF() * (view.logicalDpiY() if view is not None else 96) / 72
    changed = False
    for sample, label in legend.items:
        # LegendItem.setLabelTextColor mutates label.opts["color"] without
        # rebuilding label.item's HTML; track the rendered colour explicitly so
        # Light <-> Dark theme swaps always re-render legend text contrast.
        if (
            label.item.font() == font
            and label.opts.get("family") == family
            and label.opts.get("size") == size
            and getattr(label, "_styled_color_hex", None) == colour_hex
        ):
            continue
        sample.setFixedWidth(PAPER_STYLE.number("legend.handlelength") * em)
        sample.setFixedHeight(PAPER_STYLE.number("legend.handleheight") * em)
        label.setFont(font)
        label.item.setFont(font)
        label.setText(label.text, family=family, color=colour_hex, size=size)
        label._styled_color_hex = colour_hex
        changed = True
    if changed:
        legend.updateSize()


class _PaperLegendStyle(QObject):
    """Style entries created after the initial theme without replacing their labels."""

    def eventFilter(self, legend, event):
        if event.type() == QEvent.Type.LayoutRequest:
            _style_legend_entries(legend)
        return False


def _style_legend(plot_item, font, size, foreground, *, face_color: str, edge_color: str):
    legend = plot_item.legend
    if legend is None:
        return
    if not hasattr(legend, "_paper_style_filter"):
        legend._paper_style_filter = _PaperLegendStyle(legend)
        legend.installEventFilter(legend._paper_style_filter)
    legend.setFont(font)
    legend.setLabelTextColor(foreground)
    legend.setLabelTextSize(f"{size:g}pt")
    legend.frame = PAPER_STYLE.boolean("legend.frameon")
    face = pg.mkColor(face_color)
    edge = pg.mkColor(edge_color)
    face.setAlphaF(PAPER_STYLE.number("legend.framealpha"))
    edge.setAlphaF(PAPER_STYLE.number("legend.framealpha"))
    legend.setBrush(pg.mkBrush(face))
    legend.setPen(pg.mkPen(edge))
    view = plot_item.getViewWidget()
    em = size * (view.logicalDpiY() if view is not None else 96) / 72
    padding = PAPER_STYLE.number("legend.borderpad") * em
    legend.layout.setContentsMargins(padding, padding, padding, padding)
    legend.layout.setVerticalSpacing(PAPER_STYLE.number("legend.labelspacing") * em)
    legend.layout.setHorizontalSpacing(PAPER_STYLE.number("legend.handletextpad") * em)
    for column in range(1, legend.columnCount):
        legend.layout.setColumnSpacing(2 * column - 1,
                                       PAPER_STYLE.number("legend.columnspacing") * em)
    _style_legend_entries(legend)
    legend.layout.activate()
    legend.updateSize()
    inset = PAPER_STYLE.number("legend.borderaxespad") * em
    legend.setOffset((-inset, inset))


def apply_scientific_style(plot_item, theme="light", *, color_bar=None, publication=False):
    """Style labels without rescaling stored values or changing view ranges."""
    if plot_item is None or not isValid(plot_item):
        return
    normalized_theme = "light" if publication else str(theme).strip().lower()
    tokens = tokens_for(normalized_theme)
    palette = plot_theme(tokens, publication=publication)
    is_dark = not publication and normalized_theme == "dark"
    view = plot_item.getViewWidget()
    if view is not None:
        view.setBackground(palette.background)
    axes_background = PAPER_STYLE.colour("axes.facecolor") if publication else palette.background
    plot_item.vb.setBackgroundColor(axes_background)
    grid_alpha = PAPER_STYLE.number("grid.alpha") if publication else (0.75 if is_dark else 0.7)
    _style_grid(plot_item, grid_color=palette.grid, alpha=grid_alpha)
    prefix = "print_" if publication else ""
    tick_size = PLOT_TYPOGRAPHY[prefix + "tick"]
    label_size = PLOT_TYPOGRAPHY[prefix + "label"]
    tick_font, label_font = _paper_font(tick_size), _paper_font(label_size)
    family = ", ".join(PAPER_STYLE.font_families)
    axes = [plot_item.getAxis(name) for name in ("left", "bottom", "top", "right")]
    if color_bar is not None:
        axes.extend(color_bar.getAxis(name) for name in ("left", "bottom", "top", "right"))
        # QFluent's default 45 px slot clips enlarged colour-scale numbers.
        color_bar.axis.setWidth(None)
    for axis in axes:
        prefix_enabled = unit_allows_auto_si_prefix(axis.labelUnits)
        axis.enableAutoSIPrefix(prefix_enabled)
        if not prefix_enabled:
            # pyqtgraph 0.13 recomputes these even when disabling the prefix.
            axis.autoSIPrefixScale = 1.0
            axis.labelUnitPrefix = ""
        direction = "x" if axis.orientation in {"bottom", "top"} else "y"
        tick_colour = (
            PAPER_STYLE.colour(direction + "tick.color") if publication else palette.axes
        )
        edge_colour = (
            PAPER_STYLE.colour("axes.edgecolor") if publication else palette.axes
        )
        spine = PAPER_STYLE.boolean("axes.spines." + axis.orientation)
        ticks = PAPER_STYLE.boolean(direction + "tick." + axis.orientation)
        axis.setGrid(False)
        axis.setPen(pg.mkPen(edge_colour,
                             width=PAPER_STYLE.number("axes.linewidth")) if spine else None)
        axis.setTickPen(pg.mkPen(tick_colour,
                                 width=PAPER_STYLE.number(direction + "tick.major.width"))
                        if ticks else None)
        axis.setTextPen(pg.mkPen(tick_colour))
        axis.setStyle(tickFont=tick_font,
                      tickTextOffset=round(PAPER_STYLE.number(direction + "tick.major.pad")),
                      tickLength=round(PAPER_STYLE.number(direction + "tick.major.size")) if ticks else 0,
                      autoExpandTextSpace=True, tickAlpha=1.0, maxTickLevel=0, maxTextLevel=0)
        axis.label.setFont(label_font)
        axis.setLabel(text=axis.labelText, units=axis.labelUnits, unitPrefix=axis.labelUnitPrefix,
                      **{"font-size": f"{label_size:g}pt", "font-family": family,
                         "color": palette.axes})
    title_size = PLOT_TYPOGRAPHY[prefix + "title"]
    text_colour = PAPER_STYLE.colour("text.color") if publication else tokens.text_primary
    plot_item.titleLabel.item.setFont(_paper_font(title_size))
    plot_item.titleLabel.setText(plot_item.titleLabel.text, color=text_colour,
                                size=f"{title_size:g}pt", family=family)
    legend_size = PLOT_TYPOGRAPHY[prefix + "legend"]
    legend_face = (
        PAPER_STYLE.colour("legend.facecolor")
        if publication
        else (tokens.surface_raised if is_dark else tokens.surface)
    )
    legend_edge = (
        PAPER_STYLE.colour("legend.edgecolor")
        if publication
        else tokens.border
    )
    _style_legend(
        plot_item,
        _paper_font(legend_size),
        legend_size,
        text_colour,
        face_color=legend_face,
        edge_color=legend_edge,
    )
