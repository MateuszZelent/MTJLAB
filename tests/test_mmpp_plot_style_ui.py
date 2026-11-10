"""MMPP's paper appearance must preserve the interactive scientific scene."""

import gc
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter, QRawFont
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from app.ui.design_system import apply_application_theme, plot_theme, tokens_for
from app.ui.results.heatmap_tab import HeatmapPlotWidget
from app.ui.widgets.plot_style import apply_scientific_style
from app.ui.widgets.spectrum_plot import SpectrumPlotWidget


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    # Windows' offscreen platform does not discover the system fonts in this sandbox.
    for name in ("arial.ttf", "segoeui.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    previous_font = app.font()
    app.setFont(QFont("Segoe UI", 10))
    yield app
    app.setFont(previous_font)


@pytest.fixture
def plot(application):
    widget = pg.PlotWidget()
    widget.resize(1120, 720)
    widget.show()
    application.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


def _render(widget, application):
    for _ in range(3):
        application.processEvents()
    return widget.grab().toImage()


def _assert_axis(axis):
    assert axis.pen().style() == Qt.PenStyle.NoPen
    assert axis.tickPen().style() == Qt.PenStyle.NoPen
    assert axis.textPen().color().name() == "#636363"
    assert axis.style["tickFont"].pointSizeF() == 11
    assert axis.style["tickFont"].families()[0] == "Arial"
    assert QRawFont.fromFont(axis.style["tickFont"]).supportsCharacter("0")
    assert axis.label.font().pointSizeF() == 11
    assert not axis.grid
    assert axis.style["showValues"]
    assert axis.picture is not None
    image = QImage(1200, 800, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    try:
        _, _, labels = axis.generateDrawSpecs(painter)
        assert any(text for _, _, text in labels)
    finally:
        painter.end()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_paper_palette_is_independent_of_application_theme(theme):
    palette = plot_theme(tokens_for(theme))
    assert palette.background == "#ffffff"
    assert palette.axes == palette.grid == "#636363"
    assert palette.measurement == "#1f77b4"
    assert palette.reference == "#ff7f0e"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("kind", ["spectrum", "heatmap"])
def test_paper_plots_render_with_source_typography(application, tmp_path, theme, kind):
    apply_application_theme(application, theme)
    widget = SpectrumPlotWidget() if kind == "spectrum" else HeatmapPlotWidget()
    try:
        widget.resize(1120, 720)
        widget.show()
        if kind == "spectrum":
            x = np.linspace(1e9, 2e9, 101)
            widget.set_trace("Recorded spectrum", x, np.linspace(-80, -60, 101), primary=True)
            widget.set_trace("Explicit colour", x, np.linspace(-75, -55, 101), color="#a020f0")
            widget.set_title("Recorded spectrum")
        else:
            widget.set_data(np.arange(60.).reshape(5, 12), np.linspace(1e9, 2e9, 12),
                            np.linspace(.002, .004, 5), y_label="Current", y_unit="A")
            widget.plot.setTitle("Recorded heatmap")
        widget.apply_theme(theme)
        rendered = _render(widget, application)
        item = widget.plot.plotItem
        for axis in (item.getAxis("left"), item.getAxis("bottom")):
            _assert_axis(axis)
        assert item.titleLabel.opts["size"] == "11pt"
        assert item.titleLabel.opts["color"] == "#636363"
        assert item.titleLabel.item.font().families()[0] == "Arial"
        assert widget.plot.backgroundBrush().color().name() == "#ffffff"
        assert item.vb.sceneBoundingRect().width() > 600
        assert item.vb.sceneBoundingRect().height() > 350
        if kind == "heatmap":
            _assert_axis(widget.color_bar.axis)
        else:
            assert widget._curves["Recorded spectrum"].opts["pen"].color().name() == "#1f77b4"
            assert widget._curves["Explicit colour"].opts["pen"].color().name() == "#a020f0"
            assert [label.text for _, label in item.legend.items] == [
                "Recorded spectrum", "Explicit colour"]
        assert rendered.save(str(tmp_path / f"mmpp-{kind}-{theme}.png"))
    finally:
        widget.close()
        widget.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()


@pytest.mark.parametrize("publication", [False, True])
def test_major_grid_is_below_opaque_images_and_keeps_source_alpha(plot, application, publication):
    plot.setRange(xRange=(-.5, 9.5), yRange=(-.5, 9.5), padding=0)
    plot.showGrid(x=True, y=True)
    apply_scientific_style(plot.plotItem, "dark", publication=publication)
    _render(plot, application)
    grids = [item for item in plot.plotItem.items if isinstance(item, pg.GridItem)]
    assert len(grids) == 1
    grid = grids[0]
    assert grid.opts["textPen"] is None
    assert grid.opts["tickSpacing"] == ([None], [None])
    assert grid.opts["pen"].color().name() == "#636363"
    assert grid.opts["pen"].color().alphaF() == pytest.approx(.5, abs=.003)
    assert grid.opts["pen"].widthF() == pytest.approx(.8)
    assert grid.opts["pen"].style() == Qt.PenStyle.DashLine
    # Rasterize the grid's picture directly: stock GridItem silently replaces alpha with 50/255.
    image = QImage(400, 400, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    try:
        painter.setWindow(QRectF(-.5, -.5, 10, 10).toRect())
        painter.drawPicture(QPointF(0, 0), grid.picture)
    finally:
        painter.end()
    pixels = np.frombuffer(image.bits(), dtype=np.uint8).reshape(400, 400, 4)
    assert pixels[:, :, :3].min() < 200
    data = np.zeros((10, 10, 3), dtype=np.uint8)
    data[:] = (36, 104, 166)
    heatmap = pg.ImageItem(data, axisOrder="row-major", autoLevels=False)
    heatmap.setRect(QRectF(-.5, -.5, 10, 10))
    plot.addItem(heatmap)
    rendered = _render(plot, application)
    assert grid.zValue() < heatmap.zValue()
    for x in (0, 2, 4, 6, 8):
        point = plot.mapFromScene(plot.plotItem.vb.mapViewToScene(QPointF(x, 4)))
        assert rendered.pixelColor(point).name() == "#2468a6"


def test_restyling_keeps_single_grid_zoom_orientation_and_autorange_bounds(plot, application):
    curve = plot.plot([100, 110], [200, 220], pen=pg.mkPen("#a020f0", width=0))
    plot.autoRange(padding=0)
    baseline_bounds = plot.plotItem.vb.childrenBounds()
    baseline_range = np.array(plot.viewRange())
    apply_scientific_style(plot.plotItem)
    _render(plot, application)
    grids = [item for item in plot.plotItem.items if isinstance(item, pg.GridItem)]
    assert len(grids) == 1
    grid = grids[0]
    assert grid not in plot.plotItem.vb.addedItems
    np.testing.assert_allclose(plot.plotItem.vb.childrenBounds(), baseline_bounds)
    plot.setRange(xRange=(102, 106), yRange=(204, 212), padding=0)
    plot.invertX(True)
    plot.invertY(True)
    for theme in ("dark", "light", "dark", "light"):
        apply_scientific_style(plot.plotItem, theme)
        _render(plot, application)
        assert [item for item in plot.plotItem.items if isinstance(item, pg.GridItem)] == [grid]
        np.testing.assert_allclose(plot.viewRange(), [(102, 106), (204, 212)])
        assert plot.plotItem.vb.state["xInverted"] and plot.plotItem.vb.state["yInverted"]
        assert curve.opts["pen"].color().name() == "#a020f0"
    plot.autoRange(padding=0)
    np.testing.assert_allclose(plot.viewRange(), baseline_range)
    plot.clear()
    plot.plot([100, 110], [200, 220], pen=pg.mkPen("#a020f0", width=0))
    apply_scientific_style(plot.plotItem)
    _render(plot, application)
    assert [item for item in plot.plotItem.items if isinstance(item, pg.GridItem)] == [grid]
    np.testing.assert_allclose(plot.plotItem.vb.childrenBounds(), baseline_bounds)


@pytest.mark.parametrize("unit", ["dB", "dBm", "ratio", "mW²", "W"])
def test_axis_units_keep_their_source_scale(plot, application, unit):
    plot.setLabel("left", "Power", units=unit)
    plot.plot([1, 2], [1e-12, 2e-12])
    for theme in ("light", "dark"):
        apply_scientific_style(plot.plotItem, theme)
        _render(plot, application)
    axis = plot.getAxis("left")
    assert axis.labelUnits == unit
    assert axis.autoSIPrefixScale == pytest.approx(1e12 if unit == "W" else 1)
    assert axis.autoSIPrefix is (unit == "W")


def test_legend_keeps_labels_and_uses_source_frame_spacing_and_location(plot, application):
    legend = plot.addLegend(offset=(10, 10))
    plot.plot([1, 2], [1, 2], name="Measured <b>signal</b>")
    plot.plot([1, 2], [2, 1], name="Reference")
    apply_scientific_style(plot.plotItem)
    _render(plot, application)
    assert [label.text for _, label in legend.items] == ["Measured <b>signal</b>", "Reference"]
    assert legend.frame
    assert legend.brush().color().name() == "#ffffff"
    assert legend.brush().color().alphaF() == pytest.approx(.8, abs=.003)
    assert legend.pen().color().name() == "#cccccc"
    assert legend.pen().color().alphaF() == pytest.approx(.8, abs=.003)
    assert legend.labelTextSize() == "11pt"
    assert legend.labelTextColor().name() == "#636363"
    assert legend.layout.verticalSpacing() / legend.layout.horizontalSpacing() == pytest.approx(.3 / .4)
    for _, label in legend.items:
        assert label.item.font().families()[0] == "Arial"
        assert label.item.font().pointSizeF() == 11
    assert legend.sceneBoundingRect().right() < plot.plotItem.vb.sceneBoundingRect().right()
    assert legend.sceneBoundingRect().center().x() > plot.plotItem.vb.sceneBoundingRect().center().x()


def test_legend_entries_added_after_styling_keep_paper_font(plot, application):
    legend = plot.addLegend()
    apply_scientific_style(plot.plotItem)
    plot.plot([1, 2], [1, 2], name="Added after theme")
    _render(plot, application)
    label = legend.items[0][1]
    assert label.text == "Added after theme"
    assert label.item.font().families()[0] == "Arial"
    assert label.item.font().pointSizeF() == 11


def test_explicit_grid_spacing_can_follow_publication_major_axes(plot, application):
    plot.setRange(xRange=(0, 10), yRange=(0, 10), padding=0)
    apply_scientific_style(plot.plotItem, publication=True)
    grid = plot.plotItem._paper_grid
    grid.setTickSpacing(x=[3], y=[7])
    rendered = _render(plot, application)
    def column_has_line(x):
        top = plot.mapFromScene(plot.plotItem.vb.mapViewToScene(QPointF(x, 2.1)))
        bottom = plot.mapFromScene(plot.plotItem.vb.mapViewToScene(QPointF(x, 2.9)))
        return any(rendered.pixelColor(top.x() + offset, y).red() < 230
                   for y in range(min(top.y(), bottom.y()), max(top.y(), bottom.y()))
                   for offset in (-1, 0, 1))
    assert column_has_line(3)
    assert not column_has_line(2)


def test_paper_scene_and_cursor_objects_are_deleted_with_the_widget(application):
    for _ in range(6):
        widget = SpectrumPlotWidget()
        widget.show()
        widget.set_trace("Recorded", [1, 2], [-80, -60], primary=True)
        _render(widget, application)
        grid = widget.plot.plotItem._paper_grid
        proxy = widget._mouse_proxy
        widget.close()
        widget.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(grid)
        assert not isValid(proxy)
        del widget, grid, proxy
        gc.collect()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_holds_and_annotations_keep_paper_colours_in_ui_and_export(application, tmp_path, theme):
    widget = SpectrumPlotWidget()
    try:
        widget.set_trace("Raw", [1, 2], [2, 3], primary=True)
        widget.toggle_max_hold()
        widget.toggle_min_hold()
        widget.apply_theme(theme)
        widget.set_trace("Raw", [1, 2], [3, 2], primary=True)
        widget.set_compliance_points([1], [3])
        widget.set_replacement_markers([2], [2])
        widget.set_peak_markers([1, 2], [3, 2])
        widget.select_peak_marker(1)
        assert widget._curves["Max hold"].opts["pen"].color().name() == "#d62728"
        assert widget._curves["Min hold"].opts["pen"].color().name() == "#2ca02c"
        assert widget.compliance_markers.points()[0].brush().color().name() == "#d62728"
        info = widget._export_figure(tmp_path / f"annotations-{theme}.png")
        exported = {(point["color"], point["symbol"], point["size"]) for point in info["points"]}
        assert exported == {("#1f77b4", "o", 10), ("#ff7f0e", "o", 14),
                            ("#ff7f0e", "x", 7), ("#d62728", "d", 10)}
    finally:
        widget.close()
