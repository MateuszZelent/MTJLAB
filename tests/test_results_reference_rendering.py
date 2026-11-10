"""Real heatmap color-bar interactions at reference-subtraction power scales."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app.storage import ThatecRun
from app.ui.results.heatmap_tab import HeatmapPlotWidget, HeatmapResultsTab
from app.ui.results.map_processing import MapProcessing


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def heatmap(application):
    widget = HeatmapPlotWidget()
    widget.resize(1200, 800)
    widget.show()
    application.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    application.processEvents()


def _drag_levels(widget, application):
    # Exercise ColorBarItem's actual connected region handler, including its
    # minimum-span and rounding behavior; a direct setLevels() misses the bug.
    widget.color_bar.region.setRegion((63, 180))
    application.processEvents()
    levels = np.asarray(widget.color_bar.levels())
    assert np.all(np.isfinite(levels))
    assert levels[0] < levels[1]
    np.testing.assert_array_equal(widget.image_item.getLevels(), levels)
    return levels


@pytest.mark.parametrize("scale", [1., 1e-3, 1e-10, 1e-18, 1e-24])
@pytest.mark.parametrize("nonuniform", [False, True])
def test_drag_signed_power_keeps_the_displayed_scale(heatmap, application, scale, nonuniform):
    values = np.array([[-2., 0., 3.], [1., -1., 2.]]) * scale
    x = np.array([1e6, 2e6, 4e6 if nonuniform else 3e6])
    heatmap.set_data(values, x, z_label="Residual power (W)")
    application.processEvents()
    assert heatmap.plot.isVisible() and heatmap.plot.width() > 500
    assert not heatmap.grab().isNull()
    levels = _drag_levels(heatmap, application) / scale
    # A small inward movement of the upper handle preserves the lower bound,
    # retains signs and narrows the original [-2, 3] interval only slightly.
    assert -2.01 < levels[0] < -1.99
    assert 2.7 < levels[1] < 3.
    np.testing.assert_array_equal(heatmap._data, values)


def test_drag_is_invariant_under_power_scale_changes(heatmap, application):
    normalized = []
    for scale in (1., 1e-6, 1e-10, 1e-18, 1e-24):
        heatmap.set_data(np.array([[-2., 0., 3.], [1., -1., 2.]]) * scale)
        normalized.append(_drag_levels(heatmap, application) / scale)
    for levels in normalized[1:]:
        np.testing.assert_allclose(levels, normalized[0], rtol=0, atol=.002)


def test_unit_switches_recalculate_drag_scale(heatmap, application):
    for unit, scale, offset in (("W", 1e-10, 0.), ("dB", 1., 0.),
                                ("dBm", 1., -80.), ("W", 1e-20, 0.)):
        values = np.array([[-2., 0., 3.], [1., -1., 2.]]) * scale + offset
        heatmap.set_data(values, z_label=f"Power / contrast ({unit})")
        levels = (_drag_levels(heatmap, application) - offset) / scale
        assert -2.01 < levels[0] < -1.99
        assert 2.7 < levels[1] < 3.
        np.testing.assert_array_equal(heatmap._data, values)


@pytest.mark.parametrize("value", [0., 1e-20, -1e-20])
def test_constant_power_has_finite_local_levels_after_drag(heatmap, application, value):
    values = np.full((2, 3), value)
    heatmap.set_data(values, z_label="Residual power (W)")
    initial = np.asarray(heatmap.color_bar.levels())
    assert np.all(np.isfinite(initial)) and initial[0] < value < initial[1]
    if value:
        assert initial[1] - initial[0] < abs(value) * .01
    levels = _drag_levels(heatmap, application)
    assert levels[0] <= value <= levels[1]
    assert levels[1] - levels[0] <= (initial[1] - initial[0]) * 1.01
    np.testing.assert_array_equal(heatmap._data, values)


@pytest.mark.parametrize("reset", ["clear", "load"])
def test_reset_clears_map_processing_state(application, reset):
    tab = HeatmapResultsTab()
    tab.resize(1200, 800)
    tab.show()
    try:
        tab.map_controls.set_state(MapProcessing(
            component="reference_power", reference_value=2., mask_lines=True,
            protected_bands_hz=((1e6, 2e6),), view="component", colour_range="robust",
        ))
        if reset == "clear":
            tab.clear()
        else:
            # An empty public descriptor needs no disk, recipe or instrument.
            path = Path("in-memory-result.h5")
            tab.load(path, ThatecRun(path, {}, (), (), ()))
        application.processEvents()
        assert tab.map_controls.state == MapProcessing()
        assert tab.map_controls.operation.currentData() == "none"
        assert not tab.map_controls.lines.isChecked()
        assert tab.map_controls.view.currentData() == "result"
        assert tab.map_controls.colour.currentData() == "full"
        assert not tab.map_controls.reset.isEnabled()
    finally:
        tab.close()
        tab.deleteLater()
        application.processEvents()
