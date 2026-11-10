"""Scientific axes stay readable and heatmaps fit physical cell edges exactly."""

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPointF
from PySide6.QtWidgets import QApplication

from app.ui.design_system import apply_application_theme
from app.ui.results.heatmap_tab import HeatmapPlotWidget
from app.ui.widgets.spectrum_plot import SpectrumPlotWidget
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


@pytest.mark.parametrize("nonuniform", [False, True])
def test_heatmap_auto_range_has_no_blank_padding_and_preserves_manual_zoom(shell_qt_application, nonuniform):
    app = shell_qt_application
    view = HeatmapPlotWidget()
    x = np.array([1e9, 1.1e9, 1.8e9]) if nonuniform else np.linspace(1e9, 2e9, 3)
    data = np.arange(9.).reshape(3, 3)
    try:
        view.resize(1100, 700)
        view.show()
        view.set_data(data, x_values=x, y_values=np.array([.002, .003, .004]))
        for _ in range(3):
            app.processEvents()
        np.testing.assert_allclose(view.plot.viewRange(), [view._x_edges[[0, -1]], view._y_edges[[0, -1]]], rtol=1e-12)
        zoom = [[1.01e9, 1.09e9], [.0021, .0029]]
        view.plot.setRange(xRange=zoom[0], yRange=zoom[1], padding=0)
        view.set_data(data + 1, x_values=x, y_values=np.array([.002, .003, .004]))
        np.testing.assert_allclose(view.plot.viewRange(), zoom, rtol=1e-12)
        view.auto_range()
        np.testing.assert_allclose(view.plot.viewRange(), [view._x_edges[[0, -1]], view._y_edges[[0, -1]]], rtol=1e-12)
        np.testing.assert_array_equal(view._data, data + 1)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("kind", ["heatmap", "spectrum"])
def test_plot_axes_title_and_legend_use_readable_fonts(shell_qt_application, tmp_path, theme, kind):
    app = shell_qt_application
    apply_application_theme(app, theme)
    view = HeatmapPlotWidget() if kind == "heatmap" else SpectrumPlotWidget()
    try:
        view.resize(1120, 700)
        view.show()
        if kind == "heatmap":
            view.set_data(np.arange(60.).reshape(5, 12), np.linspace(1e9, 2e9, 12),
                          np.linspace(.002, .004, 5), y_label="Current", y_unit="A")
            view.plot.setTitle("Relative spectrum")
            axes = [view.plot.getAxis("left"), view.plot.getAxis("bottom"), view.color_bar.axis]
        else:
            view.set_trace("Recorded spectrum", np.linspace(1e9, 2e9, 101), np.linspace(-80, -60, 101), primary=True)
            view.set_title("Recorded spectrum")
            axes = [view.plot.getAxis("left"), view.plot.getAxis("bottom")]
        view.apply_theme(theme)
        for _ in range(3):
            app.processEvents()
        for axis in axes:
            assert axis.style["tickFont"] is not None
            assert axis.style["tickFont"].pointSizeF() >= 11
            assert axis.label.font().pointSizeF() == 11
        assert float(view.plot.plotItem.titleLabel.opts["size"].removesuffix("pt")) == 11
        if kind == "spectrum":
            assert float(view.plot.plotItem.legend.labelTextSize().removesuffix("pt")) >= 11
        assert view.plot.getViewBox().sceneBoundingRect().height() >= 150
        assert view.grab().save(str(tmp_path / f"{kind}-{theme}.png"))
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
        apply_application_theme(app, "dark")


@pytest.mark.parametrize("unit", ["dB", "dBm", "W"])
def test_heatmap_scale_keeps_log_units_and_scales_watts_only_for_display(shell_qt_application, unit):
    app = shell_qt_application
    view = HeatmapPlotWidget()
    data = np.array([[-2., 1.], [3., -1.]]) * (1e-12 if unit == "W" else 1e-6)
    try:
        view.resize(1000, 640)
        view.show()
        view.set_data(data, z_label="Relative power" if unit == "dB" else "Power", z_unit=unit)
        for _ in range(3):
            app.processEvents()
        view.apply_theme("light")
        view.apply_theme("dark")
        axis = view.color_bar.axis
        assert axis.labelUnits == unit
        assert axis.autoSIPrefixScale == pytest.approx(1e12 if unit == "W" else 1.)
        np.testing.assert_array_equal(view._data, data)
        np.testing.assert_allclose(view.color_bar.levels(), (float(data.min()), float(data.max())), rtol=1e-12, atol=0)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


def test_heatmap_mouse_guides_only_show_over_a_recorded_cell(shell_qt_application):
    app = shell_qt_application
    view = HeatmapPlotWidget()
    try:
        view.resize(1000, 640)
        view.show()
        view.set_data(np.arange(9.).reshape(3, 3))
        app.processEvents()
        assert not view.crosshair_x.isVisible() and not view.crosshair_y.isVisible()
        position = view.plot.getViewBox().mapViewToScene(QPointF(1., 1.))
        view._mouse_moved((position,))
        assert view.crosshair_x.isVisible() and view.crosshair_y.isVisible()
        QApplication.sendEvent(view.plot.viewport(), QEvent(QEvent.Type.Leave))
        assert not view.crosshair_x.isVisible() and not view.crosshair_y.isVisible()
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
