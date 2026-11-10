"""Map the licensed MMPP paper profile to interactive and detached Qt figures."""

from math import ceil, floor

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QFont, QPainter, QPicture

from app.ui.design_system import plot_theme, tokens_for
from app.ui.design_system.paper_plot_style import PAPER_STYLE
from app.ui.design_system.tokens import PLOT_TYPOGRAPHY


class _PaperGridItem(pg.GridItem):
    """One major grid below the data, without GridItem's adaptive alpha override."""

    def __init__(self, plot_item):
        super().__init__(textPen=None)
        self.setParent(plot_item)
        self.setTickSpacing(x=[None], y=[None])

    def generatePicture(self):
        self.picture = QPicture()
        painter = QPainter(self.picture)
        try:
            painter.setPen(self.opts["pen"])
            bounds = self.boundingRect().normalized()
            plot_item = self.parent()
            view = plot_item.vb
            for index, (name, low, high, length) in enumerate((
                ("bottom", bounds.left(), bounds.right(), view.width()),
                ("left", bounds.top(), bounds.bottom(), view.height()),
            )):
                step = self.opts["tickSpacing"][index][0]
                if step is None:
                    axis = plot_item.getAxis(name)
                    levels = axis.tickValues(low, high, length)
                    values = levels[0][1] if levels else ()
                else:
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


def _style_grid(plot_item):
    # AxisItem draws grids above images and couples them to tick strokes.
    plot_item.showGrid(x=False, y=False)
    grid = getattr(plot_item, "_paper_grid", None)
    if grid is None:
        grid = plot_item._paper_grid = _PaperGridItem(plot_item)
    if grid not in plot_item.items:
        plot_item.addItem(grid, ignoreBounds=True)
    colour = pg.mkColor(PAPER_STYLE.colour("grid.color"))
    colour.setAlphaF(PAPER_STYLE.number("grid.alpha"))
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
    view = legend.getViewWidget()
    em = font.pointSizeF() * (view.logicalDpiY() if view is not None else 96) / 72
    changed = False
    for sample, label in legend.items:
        if (label.item.font() == font and label.opts.get("family") == family
                and label.opts.get("size") == size and label.opts.get("color") == colour):
            continue
        sample.setFixedWidth(PAPER_STYLE.number("legend.handlelength") * em)
        sample.setFixedHeight(PAPER_STYLE.number("legend.handleheight") * em)
        label.setFont(font)
        label.item.setFont(font)
        label.setText(label.text, family=family, color=colour, size=size)
        changed = True
    if changed:
        legend.updateSize()


class _PaperLegendStyle(QObject):
    """Style entries created after the initial theme without replacing their labels."""

    def eventFilter(self, legend, event):
        if event.type() == QEvent.Type.LayoutRequest:
            _style_legend_entries(legend)
        return False


def _style_legend(plot_item, font, size, foreground):
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
    face = pg.mkColor(PAPER_STYLE.colour("legend.facecolor"))
    edge = pg.mkColor(PAPER_STYLE.colour("legend.edgecolor"))
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
    palette = plot_theme(tokens_for(theme))
    view = plot_item.getViewWidget()
    if view is not None:
        view.setBackground(palette.background)
    plot_item.vb.setBackgroundColor(PAPER_STYLE.colour("axes.facecolor"))
    _style_grid(plot_item)
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
        prefix_enabled = axis.labelUnits not in {"dB", "dBm", "ratio", "mW²"}
        axis.enableAutoSIPrefix(prefix_enabled)
        if not prefix_enabled:
            # pyqtgraph 0.13 recomputes these even when disabling the prefix.
            axis.autoSIPrefixScale = 1.0
            axis.labelUnitPrefix = ""
        direction = "x" if axis.orientation in {"bottom", "top"} else "y"
        tick_colour = PAPER_STYLE.colour(direction + "tick.color")
        spine = PAPER_STYLE.boolean("axes.spines." + axis.orientation)
        ticks = PAPER_STYLE.boolean(direction + "tick." + axis.orientation)
        axis.setGrid(False)
        axis.setPen(pg.mkPen(PAPER_STYLE.colour("axes.edgecolor"),
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
    plot_item.titleLabel.item.setFont(_paper_font(title_size))
    plot_item.titleLabel.setText(plot_item.titleLabel.text, color=PAPER_STYLE.colour("text.color"),
                                size=f"{title_size:g}pt", family=family)
    legend_size = PLOT_TYPOGRAPHY[prefix + "legend"]
    _style_legend(plot_item, _paper_font(legend_size), legend_size, PAPER_STYLE.colour("text.color"))
