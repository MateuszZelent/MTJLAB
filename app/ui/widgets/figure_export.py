"""Detached publication figures; call on the QApplication thread.

Inputs stay in their supplied units (normally SI); only axis text is prefixed.
Heatmap values have shape (len(y_edges)-1, len(x_edges)-1), with Y increasing
upwards. ``view_ranges`` is ((xmin, xmax), (ymin, ymax)), without extra padding.
PNG and native vector SVG use MMPP's 5 x 5 inch style at 300 dpi.
Nothing is shown and no event loop is entered. The caller owns file selection,
scientific titles, provenance/manifest writing, and filtering visible traces.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from contextlib import contextmanager
from html import escape
from itertools import pairwise
from math import ceil, floor, log10
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.ScatterPlotItem import Symbols
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QPointF,
    QRectF,
    QSaveFile,
    QSize,
    Qt,
    QThread,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontInfo,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPolygonF,
    QTransform,
)
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import QApplication, QGraphicsRectItem, QStyleOptionGraphicsItem
from shiboken6 import delete

from app.ui.design_system.paper_plot_style import PAPER_STYLE
from app.ui.design_system.tokens import PLOT_TYPOGRAPHY
from app.ui.widgets import plot_style
from app.ui.widgets.plot_ownership import own_plot_item_menus

_DPI = int(PAPER_STYLE.provenance["export_dpi"])
_WIDTH, _HEIGHT = (round(inches * _DPI) for inches in PAPER_STYLE.figure_inches)
_LAYOUT_DPI = 96
_CANVAS = QRectF(0, 0, _WIDTH * _LAYOUT_DPI / _DPI, _HEIGHT * _LAYOUT_DPI / _DPI)


def _numeric_array(value, name, ndim):
    try:
        if np.iscomplexobj(value):
            raise ValueError("complex numbers are not supported")
        array = np.array(value, dtype=np.float64, copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a real numeric array") from exc
    if array.ndim != ndim or not array.size:
        raise ValueError(f"{name} must be a nonempty {ndim}-dimensional array")
    return array


def _range_pair(value, name):
    pair = _numeric_array(value, name, 1)
    if pair.shape != (2,) or not np.isfinite(pair).all() or not pair[0] < pair[1]:
        raise ValueError(f"{name} must contain two finite, strictly increasing limits")
    if not np.isfinite(pair[1] - pair[0]):
        raise ValueError(f"{name} span must be finite")
    return tuple(float(number) for number in pair)


def _destination_and_ranges(path, view_ranges):
    path = Path(path)
    if path.suffix.lower() not in (".png", ".svg"):
        raise ValueError("Scientific figures require a .png or .svg destination")
    ranges = _numeric_array(view_ranges, "view_ranges", 2)
    if ranges.shape != (2, 2):
        raise ValueError("view_ranges must contain X and Y limit pairs")
    return path, [_range_pair(pair, "view_ranges") for pair in ranges]


def _edges(value, name, count):
    edges = _numeric_array(value, name, 1)
    if edges.shape != (count + 1,) or not np.isfinite(edges).all():
        raise ValueError(f"{name} must contain {count + 1} finite cell boundaries")
    if not np.all(np.diff(edges) > 0) or not np.isfinite(edges[-1] - edges[0]):
        raise ValueError(f"{name} must be strictly increasing with a finite span")
    return edges


def _axis_label(axis, label, unit):
    axis.setLabel(text=escape(str(label)), units=escape(str(unit)))
    # Logarithmic units never acquire a metric prefix; no numeric rescaling.
    axis.enableAutoSIPrefix(bool(unit) and unit not in ("dB", "dBm"))


def _axis_flags(value, name):
    try:
        flags = tuple(value)
    except TypeError as exc:
        raise ValueError(f"{name} requires two boolean flags") from exc
    if len(flags) != 2 or not all(isinstance(flag, (bool, np.bool_)) for flag in flags):
        raise ValueError(f"{name} requires two boolean flags")
    return tuple(bool(flag) for flag in flags)


class _PublicationAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        # AxisItem 0.13 rounds a 2.5-unit interval to whole numbers. Retain the
        # fractional part of our nice spacing after the engineering SI prefix.
        step = abs(spacing * scale) if spacing is not None else 0
        if not self.logMode and step > 0:
            exponent = floor(log10(step))
            mantissa = step / (10.0**exponent)
            if abs(mantissa - 2.5) < 1e-10:
                places = max(0, 1 - exponent)
                return [f"{value * scale:.{places}f}" for value in values]
        return super().tickStrings(values, scale, spacing)


@contextmanager
def _detached_figure(
    ranges,
    x_label,
    x_unit,
    y_label,
    y_unit,
    title,
    log_mode=(False, False),
    inverted_axes=(False, False),
):
    app = QApplication.instance()
    if not isinstance(app, QApplication) or QThread.currentThread() != app.thread():
        raise RuntimeError("Scientific figure export requires the QApplication GUI thread")
    widget = pg.GraphicsLayoutWidget(show=False)
    try:
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.setBackground("white")
        widget.scene().setBackgroundBrush(QColor("white"))
        widget.resize(round(_CANVAS.width()), round(_CANVAS.height()))
        widget.viewport().resize(widget.size())
        widget.setRange(_CANVAS, padding=0)
        widget.ci.layout.setContentsMargins(18, 18, 18, 18)
        plot = widget.addPlot(
            viewBox=pg.ViewBox(enableMenu=False),
            enableMenu=False,
            axisItems={name: _PublicationAxis(name) for name in ("left", "bottom")},
        )
        own_plot_item_menus(plot, widget)
        plot.hideButtons()
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=False, y=False)
        plot.showGrid(x=False, y=False)
        plot.setLogMode(x=log_mode[0], y=log_mode[1])
        plot.getViewBox().invertX(inverted_axes[0])
        plot.getViewBox().invertY(inverted_axes[1])
        plot.setDefaultPadding(0)
        plot.disableAutoRange()
        plot.setRange(xRange=ranges[0], yRange=ranges[1], padding=0)
        _axis_label(plot.getAxis("bottom"), x_label, x_unit)
        _axis_label(plot.getAxis("left"), y_label, y_unit)
        font = QFont()
        font.setFamilies(list(PAPER_STYLE.font_families))
        font.setPointSizeF(PLOT_TYPOGRAPHY["print_title"])
        metrics = QFontMetricsF(font)
        lines, line = [], ""
        for word in str(title).split():
            candidate = f"{line} {word}".strip()
            if line and metrics.horizontalAdvance(candidate) > _CANVAS.width() * .72:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
        plot.setTitle("<br>".join(escape(part) for part in lines))
        yield widget, plot
    finally:
        # Native ownership releases menus, scene and graphics items immediately,
        # even after an encoder/filesystem failure, without deferred-delete events.
        delete(widget)


class _VectorCells(pg.GraphicsObject):
    """Physical quadrilaterals, with NaN cells absent in both raster and SVG."""

    def __init__(self, values, x_edges, y_edges, levels, cmap, ranges):
        super().__init__()
        self._bounds = QRectF(
            float(x_edges[0]),
            float(y_edges[0]),
            float(x_edges[-1] - x_edges[0]),
            float(y_edges[-1] - y_edges[0]),
        )
        lut = cmap.getLookupTable(nPts=256, alpha=True, mode="qcolor")
        finite = np.isfinite(values)
        indices = np.zeros(values.shape, dtype=np.int32)
        # Do not widen tiny signed levels, round to watts, or change input data.
        indices[finite] = np.clip(
            (np.clip(values[finite], *levels) - levels[0]) / (levels[1] - levels[0]) * 255, 0, 255
        ).astype(np.int32)
        rgba = np.array([color.rgba() for color in lut], dtype=np.int64)
        colors = np.where(finite, rgba[indices], -1)
        runs = {}
        for row, row_colors in enumerate(colors):
            y0 = max(float(y_edges[row]), ranges[1][0])
            y1 = min(float(y_edges[row + 1]), ranges[1][1])
            if y0 >= y1:
                continue
            boundaries = np.r_[
                0, np.flatnonzero(row_colors[1:] != row_colors[:-1]) + 1, values.shape[1]
            ]
            for first, stop in pairwise(boundaries):
                color = int(row_colors[first])
                if color < 0:  # NaN gaps break runs, even between identical colors.
                    continue
                x0 = max(float(x_edges[first]), ranges[0][0])
                x1 = min(float(x_edges[stop]), ranges[0][1])
                if x0 >= x1 or y0 >= y1:
                    continue
                runs.setdefault(color, []).append(
                    QPolygonF([QPointF(x0, y0), QPointF(x1, y0), QPointF(x1, y1), QPointF(x0, y1)])
                )
        # Merge only identical displayed RGBA colors within one physical row;
        # each run retains the exact original outer edges, including nonuniform grids.
        self._cells = [(QColor.fromRgba(color), polygons) for color, polygons in runs.items()]

    def boundingRect(self):
        return self._bounds

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        for color, polygons in self._cells:
            painter.setBrush(color)
            for polygon in polygons:
                painter.drawPolygon(polygon)


def _vector_color_bar(widget, plot, levels, cmap, label, unit):
    bar = pg.ColorBarItem(
        values=levels,
        colorMap=cmap,
        interactive=False,
        colorMapMenu=False,
        width=18,
        rounding=(levels[1] - levels[0]) / 1000,
    )
    own_plot_item_menus(bar, widget)
    bar.setImageItem([], insert_in=plot)
    _axis_label(bar.axis, label, unit)
    # ColorBarItem's default gradient is a pixmap. Replace only this detached
    # bar with a Qt gradient, retaining its axes and exact unrounded levels.
    bar.bar.hide()
    gradient = QLinearGradient(0, 0, 0, 256)
    colors = cmap.getLookupTable(nPts=256, alpha=True, mode="qcolor")
    for index, color in enumerate(colors):
        gradient.setColorAt(index / 255, color)
    strip = QGraphicsRectItem(QRectF(0, 0, 1, 256))
    strip.setPen(pg.mkPen(None))
    strip.setBrush(gradient)
    bar.addItem(strip)
    return bar


def _paint_scene(widget, painter, target):
    widget.scene().prepareForPaint()
    widget.scene().render(painter, target, _CANVAS, Qt.AspectRatioMode.IgnoreAspectRatio)


def _major_spacing(lower, upper):
    target = (upper - lower) / 6
    decade = 10.0 ** floor(log10(target))
    candidates = [factor * decade for factor in (1, 2, 2.5, 5, 10)]

    def spacing_score(step):
        count = floor(upper / step) - ceil(lower / step) + 1
        return max(0, 5 - count, count - 7), abs(log10(step / target))

    return min(candidates, key=spacing_score)


def _settle_layout(widget, plot, color_bar):
    """Measure ticks by painting offscreen and synchronously activate layouts."""
    for name, (lower, upper) in zip(("bottom", "left"), plot.viewRange()):
        axis = plot.getAxis(name)
        axis.setStyle(hideOverlappingLabels=False)
        if axis.logMode:
            # Log axes use pyqtgraph's decade/subdecade strategy in view space.
            axis.setStyle(maxTickLevel=1, maxTextLevel=1)
            continue
        step = _major_spacing(lower, upper)
        axis.setTickSpacing(levels=[(step, 0), (step / 2, 0)])
        axis.setStyle(maxTickLevel=1, maxTextLevel=0)
    scratch = QImage(
        QSize(round(_CANVAS.width()), round(_CANVAS.height())),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    scratch.setDotsPerMeterX(round(_LAYOUT_DPI / 0.0254))
    scratch.setDotsPerMeterY(round(_LAYOUT_DPI / 0.0254))
    previous = None
    for _ in range(12):
        widget.ci.layout.activate()
        plot.layout.activate()
        if color_bar is not None:
            color_bar.layout.activate()
        painter = QPainter(scratch)
        try:
            _paint_scene(widget, painter, _CANVAS)
        finally:
            painter.end()
        geometry = (
            plot.getViewBox().sceneBoundingRect(),
            color_bar.sceneBoundingRect() if color_bar is not None else None,
        )
        if geometry == previous and not any(
            not item.layout.isActivated()
            for item in (widget.ci, plot, *([color_bar] if color_bar is not None else []))
        ):
            return
        previous = geometry
    raise RuntimeError("Detached figure layout did not settle")


def _axis_metadata(axis, label, unit):
    return {
        "label": str(label),
        "unit": str(unit),
        "display_unit": f"{axis.labelUnitPrefix}{unit}",
        "display_scale": float(axis.autoSIPrefixScale),
        "major_tick_spacing": float(
            axis.tickSpacing(*axis.range, max(axis.geometry().width(), axis.geometry().height()))[
                0
            ][0]
        ),
    }


def _presentation(plot, path, title):
    rect = plot.getViewBox().mapRectToScene(plot.getViewBox().rect())
    scale = _DPI / _LAYOUT_DPI
    font = plot.getAxis("bottom").style["tickFont"]
    return {
        "profile": "mmpp-paper",
        "style_source": dict(PAPER_STYLE.provenance),
        "renderer": "pyqtgraph-qt-detached",
        "format": path.suffix[1:].lower(),
        "width_px": _WIDTH,
        "height_px": _HEIGHT,
        "dpi": _DPI,
        "size_inches": [_WIDTH / _DPI, _HEIGHT / _DPI],
        "font_family": font.family(),
        "resolved_font_family": QFontInfo(font).family(),
        "font_sizes_pt": {
            name: PLOT_TYPOGRAPHY[f"print_{name}"] for name in ("tick", "label", "title", "legend")
        },
        "background": "#ffffff",
        "foreground": PAPER_STYLE.colour("text.color"),
        "title": str(title),
        "log_mode": [bool(plot.getAxis(name).logMode) for name in ("bottom", "left")],
        "inverted_axes": [bool(plot.getViewBox().xInverted()), bool(plot.getViewBox().yInverted())],
        "view_ranges": [[float(value) for value in pair] for pair in plot.viewRange()],
        "plot_rect_px": [
            rect.x() * scale,
            rect.y() * scale,
            rect.width() * scale,
            rect.height() * scale,
        ],
    }


def _print_strokes(widget):
    # pyqtgraph uses cosmetic (device pixel) pens. Scale those explicitly so
    # an enlarged publication canvas also has publication-weight strokes.
    scale = _DPI / _LAYOUT_DPI
    for item in widget.scene().items():
        if isinstance(item, pg.ScatterPlotItem):
            item.setExportMode(True, {"antialias": True, "resolutionScale": scale})
            continue
        if isinstance(item, pg.AxisItem):
            pen = item.pen()
        elif isinstance(item, pg.PlotDataItem):
            pen = item.opts["pen"]
        elif isinstance(item, pg.InfiniteLine):
            pen = item.pen
        elif isinstance(item, pg.GridItem):
            pen = item.opts["pen"]
        else:
            continue
        if pen is not None and pen.style() != Qt.PenStyle.NoPen:
            pen = pg.mkPen(pen)
            pen.setWidthF(pen.widthF() * scale)
            item.setPen(pen)


def _native_svg(widget, plot, title):
    """Retain Qt vectors and text, adding the clipping its SVG engine omits.

    Paint items independently, preserving their scene transforms. Unlike the
    bundled pyqtgraph SVG exporter this never reparses Qt's path command syntax.
    """
    namespace = "http://www.w3.org/2000/svg"
    tag = lambda name: f"{{{namespace}}}{name}"
    root = ET.Element(
        tag("svg"),
        {
            "width": f"{PAPER_STYLE.figure_inches[0] * 25.4:g}mm",
            "height": f"{PAPER_STYLE.figure_inches[1] * 25.4:g}mm",
            "viewBox": f"0 0 {_WIDTH} {_HEIGHT}",
            "version": "1.1",
        },
    )
    ET.SubElement(root, tag("title")).text = str(title)
    definitions = ET.SubElement(root, tag("defs"))
    ET.SubElement(root, tag("rect"), {"width": "100%", "height": "100%", "fill": "white"})
    rect = plot.getViewBox().mapRectToScene(plot.getViewBox().rect())
    scale = _DPI / _LAYOUT_DPI
    clip = ET.SubElement(definitions, tag("clipPath"), {"id": "data-viewport"})
    ET.SubElement(
        clip,
        tag("rect"),
        {
            "x": str(rect.x() * scale),
            "y": str(rect.y() * scale),
            "width": str(rect.width() * scale),
            "height": str(rect.height() * scale),
        },
    )
    serial = 0

    def paint_item(item, parent):
        nonlocal serial
        if not item.isVisible():
            return
        serial += 1
        prefix = f"item{serial}-"
        group = ET.SubElement(parent, tag("g"))
        if item is plot.getViewBox().childGroup:
            group.set("clip-path", "url(#data-viewport)")
        children = sorted(item.childItems(), key=lambda child: child.zValue())
        behind = [
            child
            for child in children
            if child.flags() & child.GraphicsItemFlag.ItemStacksBehindParent
        ]
        for child in behind:
            paint_item(child, group)
        data = QByteArray()
        buffer = QBuffer(data)
        generator = QSvgGenerator()
        generator.setOutputDevice(buffer)
        generator.setResolution(_LAYOUT_DPI)
        generator.setSize(QSize(_WIDTH, _HEIGHT))
        painter = QPainter()
        if not painter.begin(generator):
            raise OSError("Qt could not start the SVG figure encoder")
        try:
            painter.setTransform(item.sceneTransform() * QTransform.fromScale(scale, scale))
            option = QStyleOptionGraphicsItem()
            option.exposedRect = item.boundingRect()
            try:
                item.paint(painter, option, None)
            except NotImplementedError:
                # QGraphicsItem groups have no paint implementation of their own.
                pass
        finally:
            painter.end()
        document = ET.fromstring(bytes(data))
        identifiers = {node.attrib["id"] for node in document.iter() if "id" in node.attrib}
        for node in document.iter():
            for key, value in list(node.attrib.items()):
                if key == "id":
                    node.set(key, prefix + value)
                else:
                    for identifier in identifiers:
                        value = value.replace(f"url(#{identifier})", f"url(#{prefix}{identifier})")
                        if value == f"#{identifier}":
                            value = f"#{prefix}{identifier}"
                    node.set(key, value)
        for node in document:
            if node.tag == tag("defs"):
                definitions.extend(node)
            elif node.tag == tag("g"):
                group.append(node)
        for child in children:
            if child not in behind:
                paint_item(child, group)

    widget.scene().prepareForPaint()
    paint_item(widget.ci, root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _save_figure(widget, plot, path, title):
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError("Cannot allocate figure output buffer")
    target = QRectF(0, 0, _WIDTH, _HEIGHT)
    if path.suffix.lower() == ".png":
        image = QImage(_WIDTH, _HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("white"))
        # Text layout uses 96 dpi scene coordinates. Set print metadata after
        # painting so that QPainter does not apply the print DPI a second time.
        image.setDotsPerMeterX(round(_LAYOUT_DPI / 0.0254))
        image.setDotsPerMeterY(round(_LAYOUT_DPI / 0.0254))
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            _paint_scene(widget, painter, target)
        finally:
            painter.end()
        image.setDotsPerMeterX(round(_DPI / 0.0254))
        image.setDotsPerMeterY(round(_DPI / 0.0254))
        if not image.save(buffer, "PNG"):
            raise OSError("Qt could not encode the PNG figure")
        payload = bytes(data)
    else:
        payload = _native_svg(widget, plot, title)
    buffer.close()
    output = QSaveFile(str(path))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(f"Cannot open figure {path}: {output.errorString()}")
    if output.write(payload) != len(payload):
        output.cancelWriting()
        raise OSError(f"Cannot write figure {path}: {output.errorString()}")
    if not output.commit():
        raise OSError(f"Cannot save figure {path}: {output.errorString()}")


def export_heatmap_figure(
    path,
    *,
    values,
    x_edges,
    y_edges,
    view_ranges,
    x_label,
    x_unit,
    y_label,
    y_unit,
    z_label,
    levels,
    colormap,
    title="",
    z_unit="",
    inverted_axes=(False, False),
) -> dict:
    """Save physical cells and the current viewport, with fixed color limits.

    NaNs are blank; infinities and invalid boundaries/levels are rejected before
    creating a file. Returns JSON-safe presentation metadata for the manifest.
    ``colormap`` accepts a pyqtgraph palette name or ColorMap instance.
    """
    path, ranges = _destination_and_ranges(path, view_ranges)
    inverted_axes = _axis_flags(inverted_axes, "inverted_axes")
    matrix = _numeric_array(values, "values", 2)
    if np.isinf(matrix).any():
        raise ValueError("Heatmap values may contain NaN gaps, but not infinity")
    x = _edges(x_edges, "x_edges", matrix.shape[1])
    y = _edges(y_edges, "y_edges", matrix.shape[0])
    limits = _range_pair(levels, "levels")
    try:
        cmap = colormap if isinstance(colormap, pg.ColorMap) else pg.colormap.get(colormap)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Unknown heatmap colormap: {colormap!r}") from exc
    with _detached_figure(
        ranges, x_label, x_unit, y_label, y_unit, title, inverted_axes=inverted_axes
    ) as (widget, plot):
        plot.addItem(_VectorCells(matrix, x, y, limits, cmap, ranges))
        bar = _vector_color_bar(widget, plot, limits, cmap, z_label, z_unit)
        plot_style.apply_scientific_style(plot, "light", color_bar=bar, publication=True)
        _settle_layout(widget, plot, bar)
        info = _presentation(plot, path, title)
        info.update(
            color_levels=list(limits),
            colormap=str(cmap.name),
            axes={
                "x": _axis_metadata(plot.getAxis("bottom"), x_label, x_unit),
                "y": _axis_metadata(plot.getAxis("left"), y_label, y_unit),
                "z": _axis_metadata(bar.axis, z_label, z_unit),
            },
        )
        _print_strokes(widget)
        _save_figure(widget, plot, path, title)
        return info


def _print_color(value):
    try:
        color = pg.mkColor(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid figure color: {value!r}") from exc
    if not color.isValid():
        raise ValueError(f"Invalid figure color: {value!r}")
    if color.alpha() == 255 and color.name().lower() in PAPER_STYLE.colour_cycle:
        return QColor(color)
    # Composite against white before measuring legibility. Retain hue and
    # darken only colors with less than 3:1 contrast on the print background.
    rgb = np.array(color.getRgbF()[:3]) * color.alphaF() + (1 - color.alphaF())
    while True:
        linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
        luminance = float(linear @ np.array([0.2126, 0.7152, 0.0722]))
        if 1.05 / (luminance + 0.05) >= 3:
            break
        rgb *= 0.9
    return QColor.fromRgbF(*rgb)


def export_spectrum_figure(
    path,
    *,
    traces,
    view_ranges,
    x_label,
    x_unit,
    y_label,
    y_unit,
    title="",
    markers=(),
    band=None,
    points=(),
    log_mode=(False, False),
    inverted_axes=(False, False),
) -> dict:
    """Save full supplied traces, NaN gaps and explicit markers in the viewport.

    Each trace supplies name/x/y/color/width and optional symbol/symbol_size.
    Markers supply position/label/color;
    positions and an optional (low, high) band use the source ViewBox domain.
    Traces supply raw data; log_mode applies PlotItem's logarithmic transform.
    view_ranges and scatter coordinates remain in the source view domain, as
    do the original InfiniteLine/LinearRegionItem/ScatterPlotItem annotations.
    Light trace/marker colors are darkened to at least 3:1 contrast on white.
    ``points`` supplies scatter groups with x/y/color/symbol/size, using native
    pyqtgraph symbols and the same contrast policy. Size and trace width are
    logical pixels at 96 dpi, enlarged with the publication canvas.
    """
    path, ranges = _destination_and_ranges(path, view_ranges)
    log_mode = _axis_flags(log_mode, "log_mode")
    inverted_axes = _axis_flags(inverted_axes, "inverted_axes")
    prepared = []
    for trace in traces:
        x = _numeric_array(trace["x"], "trace x", 1)
        y = _numeric_array(trace["y"], "trace y", 1)
        if x.shape != y.shape or not np.isfinite(x).all() or np.isinf(y).any():
            raise ValueError("Trace x/y must match, with finite X and finite Y or NaN gaps")
        width = float(trace.get("width", 1.5))
        if not np.isfinite(width) or width <= 0:
            raise ValueError("Trace width must be finite and positive")
        width = max(width, PAPER_STYLE.number("lines.linewidth") * _LAYOUT_DPI / 72)
        symbol = trace.get("symbol")
        symbol_size = float(trace.get("symbol_size", 6))
        if symbol is not None and (not isinstance(symbol, str) or symbol not in Symbols):
            raise ValueError(f"Unknown trace symbol: {symbol!r}")
        if symbol is not None and (not np.isfinite(symbol_size) or symbol_size <= 0):
            raise ValueError("Trace symbol size must be finite and positive")
        if symbol is not None:
            symbol_size = max(symbol_size, PAPER_STYLE.number("lines.markersize") * _LAYOUT_DPI / 72)
        prepared.append(
            (str(trace.get("name", "")), x, y, _print_color(trace.get("color", "#1f77b4")), width, symbol, symbol_size)
        )
    if not prepared:
        raise ValueError("At least one visible trace is required")
    prepared_markers = []
    for marker in markers:
        position = float(marker["position"])
        if not np.isfinite(position):
            raise ValueError("Marker position must be finite")
        prepared_markers.append(
            (position, str(marker.get("label", "")), _print_color(marker.get("color", "#444444")))
        )
    band = _range_pair(band, "band") if band is not None else None
    prepared_points = []
    for group in points:
        x = _numeric_array(group["x"], "point x", 1)
        y = _numeric_array(group["y"], "point y", 1)
        if x.shape != y.shape or not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ValueError("Scatter x/y must match and contain finite coordinates")
        size = float(group.get("size", 8))
        symbol = group.get("symbol", "o")
        if not np.isfinite(size) or size <= 0:
            raise ValueError("Scatter size must be finite and positive")
        if not isinstance(symbol, str) or symbol not in Symbols:
            raise ValueError(f"Unknown scatter symbol: {symbol!r}")
        prepared_points.append((x, y, _print_color(group.get("color", "#444444")), symbol, size))
    with _detached_figure(
        ranges,
        x_label,
        x_unit,
        y_label,
        y_unit,
        title,
        log_mode=log_mode,
        inverted_axes=inverted_axes,
    ) as (widget, plot):
        legend = plot.addLegend(offset=(-12, 12))
        legend.setFlag(legend.GraphicsItemFlag.ItemIgnoresTransformations, False)
        if band is not None:
            region = pg.LinearRegionItem(
                band, movable=False, brush=pg.mkBrush(70, 110, 170, 28), pen=pg.mkPen(None)
            )
            region.setZValue(-5)
            plot.addItem(region, ignoreBounds=True)
        for name, x, y, color, width, symbol, symbol_size in prepared:
            plot.plot(
                x,
                y,
                name=escape(name),
                pen=pg.mkPen(color, width=width),
                connect="finite",
                antialias=True,
                autoDownsample=False,
                symbol=symbol,
                symbolSize=symbol_size,
                symbolPen=pg.mkPen(color),
                symbolBrush=pg.mkBrush(color),
            )
        for x, y, color, symbol, size in prepared_points:
            scatter = pg.ScatterPlotItem(
                x=x,
                y=y,
                symbol=symbol,
                size=size,
                pen=pg.mkPen(color),
                brush=pg.mkBrush(color),
                useCache=False,
                antialias=True,
            )
            scatter.setZValue(10)
            plot.addItem(scatter, ignoreBounds=True)
        for index, (position, label, color) in enumerate(prepared_markers):
            line = pg.InfiniteLine(
                pos=position, angle=90, movable=False, pen=pg.mkPen(color, width=1.3)
            )
            plot.addItem(line, ignoreBounds=True)
            # Keep scientific labels below the legend and inside both X edges.
            anchor_x = int((position > (ranges[0][0] + ranges[0][1]) / 2) != inverted_axes[0])
            text = pg.TextItem(text=label, color=color, anchor=(anchor_x, 1))
            font = plot.getAxis("bottom").label.font()
            font.setFamilies(list(PAPER_STYLE.font_families))
            font.setPointSize(PLOT_TYPOGRAPHY["print_legend"])
            text.setFont(font)
            text.setPos(
                position,
                (ranges[1][1] if inverted_axes[1] else ranges[1][0])
                + (-1 if inverted_axes[1] else 1)
                * (ranges[1][1] - ranges[1][0])
                * (0.04 + 0.075 * (index % 3)),
            )
            plot.addItem(text, ignoreBounds=True)
        plot_style.apply_scientific_style(plot, "light", publication=True)
        for _, label in legend.items:
            # pyqtgraph 0.13's legend setters only update LabelItem options.
            # Refresh the rich text so the shared font/color actually render.
            label.setText(label.text, family=", ".join(PAPER_STYLE.font_families))
        legend.layout.activate()
        legend.updateSize()
        _settle_layout(widget, plot, None)
        info = _presentation(plot, path, title)
        info.update(
            axes={
                "x": _axis_metadata(plot.getAxis("bottom"), x_label, x_unit),
                "y": _axis_metadata(plot.getAxis("left"), y_label, y_unit),
            },
            traces=[
                {"name": name, "color": color.name(), "width": width, "points": len(x)}
                for name, x, y, color, width, symbol, symbol_size in prepared
            ],
            markers=[
                {"position": pos, "label": label, "color": color.name()}
                for pos, label, color in prepared_markers
            ],
            points=[
                {"count": len(x), "color": color.name(), "symbol": symbol, "size": size}
                for x, y, color, symbol, size in prepared_points
            ],
            band=list(band) if band is not None else None,
        )
        _print_strokes(widget)
        _save_figure(widget, plot, path, title)
        return info
