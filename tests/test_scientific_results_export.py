"""Actual Results export routes produce figures without modifying live state."""

import json
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtGui import QImage

from app.storage.heatmap_analysis_store import read_heatmap_analysis
from app.ui.dialogs import StationFileDialog
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_results_reference_workflow import reference_page as reference_page  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.mark.parametrize("route", ["toolbar", "context"])
def test_heatmap_publication_export_preserves_live_view_and_reloadable_data(reference_page, monkeypatch, tmp_path, route):
    app, page = reference_page
    tab = page.heatmap_tab
    page.result_tabs.setCurrentIndex(page._heatmap_index)
    tab.load_button.click()
    wait_until(app, lambda: tab.heatmap._data is not None and not tab._read_tasks)
    view = tab.heatmap
    scientific_title = "Field-dependent relative spectrum - reference repeat 1 - Keithley A current"
    view.plot.setTitle(scientific_title)
    view.plot.getViewBox().invertY(True)
    view.crosshair_x.show()
    view.crosshair_y.show()
    data = view._data.copy()
    ranges = view.plot.viewRange()
    levels = view.color_bar.levels()
    font_size = view.plot.getAxis("left").style["tickFont"].pointSizeF()
    path = tmp_path / "scientific-heatmap.png"
    monkeypatch.setattr(StationFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(path), "PNG (*.png)"))
    if route == "context":
        view.plot.scene().contextMenuItem = view.plot.plotItem
        view.plot.scene().contextMenu[0].trigger()
    else:
        view.export()
    wait_until(app, lambda: view._export_task is None)
    image = QImage(str(path))
    assert not image.isNull(), view.readout.text()
    assert (image.width(), image.height()) == (1500, 1500)
    assert image.dotsPerMeterX() * .0254 == pytest.approx(300, abs=.1)
    assert image.pixelColor(0, 0).name() == "#ffffff"
    stored = read_heatmap_analysis(path.with_suffix(".h5"))
    np.testing.assert_array_equal(stored.values, data)
    assert "figure_presentation" in stored.metadata
    assert stored.metadata["figure_presentation"]["title"] == scientific_title
    assert stored.metadata["inverted_axes"] == [False, True]
    np.testing.assert_array_equal(view._data, data)
    np.testing.assert_allclose(view.plot.viewRange(), ranges)
    np.testing.assert_allclose(view.color_bar.levels(), levels)
    assert view.plot.getAxis("left").style["tickFont"].pointSizeF() == font_size
    assert view.crosshair_x.isVisible() and view.crosshair_y.isVisible()
    page._on_file_selected(path.with_suffix(".h5"))
    wait_until(app, lambda: page._result_task is None and tab.heatmap._data is not None)
    assert tab.heatmap.plot.getViewBox().state["yInverted"]
    np.testing.assert_array_equal(tab.heatmap._data, data)


@pytest.mark.parametrize("suffix", ["png", "svg"])
def test_spectrum_publication_export_retains_explicit_annotations_and_state(reference_page, monkeypatch, tmp_path, suffix):
    _app, page = reference_page
    view = page.spectrum_tab.spectrum_plot
    view.set_trace("Visible control", [1e6, 1.5e6, 2e6], [-70, -60, -70], color="#89b4fa")
    view.set_trace("Hidden control", [1e6, 1.5e6, 2e6], [-65, -55, -65], visible=False)
    view.set_trace("Single sample", [1.85e6], [-55.], color="#d62728", show_points=True)
    marker = view.add_frequency_marker(1.4e6)
    view.band_enabled.setChecked(True)
    view.band.setRegion((1.2e6, 1.7e6))
    view.plot.getViewBox().invertY(True)
    view.plot.setRange(xRange=(1e6, 2e6), yRange=(-80., -50.), padding=0)
    if suffix == "svg":
        view.plot.plotItem.setLogMode(x=True)
        view.plot.setRange(xRange=(6., np.log10(2e6)), yRange=(-80., -50.), padding=0)
    view.crosshair_x.show()
    view.crosshair_y.show()
    ranges = view.plot.viewRange()
    originals = {name: (x.copy(), y.copy()) for name, (x, y) in view._traces.items()}
    path = tmp_path / f"scientific-spectrum.{suffix}"
    messages = []
    view.status_changed.connect(messages.append)
    monkeypatch.setattr(StationFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(path), f"{suffix.upper()} (*.{suffix})"))
    if suffix == "png":
        view.plot.scene().contextMenuItem = view.plot.plotItem
        view.plot.scene().contextMenu[0].trigger()
    else:
        view.export()
    assert path.exists(), "; ".join(messages)
    if suffix == "png":
        image = QImage(str(path))
        assert image.width() == 1500 and image.dotsPerMeterX() * .0254 == pytest.approx(300, abs=.1)
    else:
        svg = path.read_text(encoding="utf-8")
        assert "Visible control" in svg and "Hidden control" not in svg
    manifest = json.loads(Path(str(path) + ".analysis.json").read_text(encoding="utf-8"))
    if suffix == "png":
        left, top, width, height = manifest["figure_presentation"]["plot_rect_px"]
        column, row = round(left + .85 * width), round(top + height * 5 / 6)
        colors = [image.pixelColor(x, y) for x in range(column - 12, column + 13)
                  for y in range(row - 12, row + 13)]
        assert any(color.red() > 120 and color.green() < 100 and color.blue() < 100 for color in colors)
    assert manifest["frequency_markers_hz"][marker] == 1.4e6
    assert manifest["band_hz"] == [1.2e6, 1.7e6]
    assert "figure_presentation" in manifest
    assert manifest["inverted_axes"] == [False, True]
    assert manifest["log_mode"] == [suffix == "svg", False]
    np.testing.assert_allclose(view.plot.viewRange(), ranges)
    for name, (x, y) in originals.items():
        np.testing.assert_array_equal(view._traces[name][0], x)
        np.testing.assert_array_equal(view._traces[name][1], y)
    assert view.crosshair_x.isVisible() and view.crosshair_y.isVisible()
