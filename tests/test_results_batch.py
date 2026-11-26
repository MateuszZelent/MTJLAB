import csv
import json
from datetime import UTC, datetime
from threading import Event

import numpy as np
from pptx import Presentation

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter
from app.storage.filesystem_paths import native_path
from app.storage.heatmap_analysis_store import read_heatmap_analysis
from app.ui.results.analysis_export import postprocessing_directory
from app.ui.results.batch_processing import SpectrumBatch, export_batch, plan_heatmaps, plan_spectra
from app.ui.results.heatmap_coordinates import SERIES_DIMENSION_ID, HeatmapCoordinates, HeatmapDimension, HeatmapRequest
from app.ui.results.heatmap_tab import _archive_identity
from app.ui.results.map_processing import MapProcessing
from app.ui.results.page import ResultsPage
from app.ui.results.processing import ResultProcessing
from app.ui.results.series_heatmap_views import (
    SeriesHeatmapView,
    read_series_heatmap_views,
    save_series_heatmap_view,
)
from app.ui.results.spectrum_views import SpectrumView
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_heatmap_coordinates import _write_current_sweep, _write_two_axis_sweep
from tests.test_spectrum_correction_controller import wait_until


def test_batch_groups_only_recorded_devices_and_visits_with_selected_axis_range():
    def dimension(key, name, unit, values):
        return HeatmapDimension(key, name, unit, values)

    coordinates = HeatmapCoordinates((
        HeatmapDimension("frequency", "Frequency", "Hz", (), True),
        dimension("moke", "VOUT 0 programming voltage", "V", (-.004, -.004, -.004, .002)),
        dimension("b", "Keithley B current", "A", (.004, .004, .004, -.002)),
        dimension("a", "Keithley A current", "A", (-.014, .014, -.014, .014)),
        dimension("measurement.visit", "Visit", "", (1, 1, 2, 1)),
    ), 4)
    plans = plan_heatmaps(coordinates, HeatmapRequest("frequency", "a", {
        "moke": -.004, "b": .004, "a": (-.014, .014), "measurement.visit": 1,
    }))
    assert [item.checkpoints for item in plans] == [(0, 1), (2,), (3,)]
    assert "MOKE Box VOUT 0 -4 mV" in plans[0].title
    assert "Keithley B 4 mA" in plans[0].title
    assert "Keithley A" not in plans[0].title
    assert ("a", "Keithley A", "A", -.014, .014) in plans[0].parameters
    assert "Visit 2" in plans[1].title
    assert plans[2].request.filters["b"] == -.002
    limited = plan_heatmaps(coordinates, HeatmapRequest("frequency", "a", {"a": (0, .014)}))
    assert [item.checkpoints for item in limited] == [(1,), (3,)]


def test_dialog_exports_recorded_heatmaps_and_powerpoint(tmp_path, shell_qt_application):
    source = tmp_path / "dut" / "measurements" / "sweeps" / "run" / "data.h5"
    source.parent.mkdir(parents=True)
    _write_two_axis_sweep(source)
    before = source.read_bytes()
    app = shell_qt_application
    page = ResultsPage(str(source.parent))
    page.resize(1400, 900)
    page.show()
    try:
        page._on_file_selected(source)
        wait_until(app, lambda: page._result_task is None and page._thatec_run is not None)
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        tab = page.heatmap_tab
        wait_until(app, lambda: not tab._read_tasks)
        tab.x_axis_combo.setCurrentIndex(tab.x_axis_combo.findData("frequency"))
        tab.y_axis_combo.setCurrentIndex(tab.y_axis_combo.findData("keithley.B.current"))
        tab.load_button.click()
        wait_until(app, tab.heatmap.batch_button.isEnabled)
        tab.heatmap.color_bar.setLevels((-102, -88))
        tab.heatmap.batch_button.click()
        dialog = page._batch_dialog
        app.processEvents()
        assert dialog.isVisible()
        assert dialog.preview.width() > 400 and dialog.preview.height() > 200
        assert not dialog.preview.pixmap().isNull()
        assert dialog.start_button.geometry().bottom() < dialog.modal_shell.surface.height()
        dialog.power_point.setChecked(True)
        dialog.start_button.click()
        wait_until(app, lambda: dialog._task is None)
        assert dialog.progress.value() == 2
        assert "2 of 2 saved" in dialog.status.text()
        assert dialog.folder.parent == tmp_path / "dut" / "postprocessing" / "sweeps" / "run" / "Heatmaps"
        index = json.loads((dialog.folder / "index.json").read_text(encoding="utf-8"))
        assert index["exported"] == 2
        assert dialog.current.text() == index["items"][-1]["title"]
        for entry in index["items"]:
            assert "frequency" in entry["title"].lower() and "Keithley B" not in entry["title"]
            assert "Keithley B" not in entry["image"]
            current = next(parameter for parameter in entry["parameters"] if parameter["id"] == "keithley.B.current")
            assert (current["minimum"], current["maximum"]) == (0., .001)
            derived = read_heatmap_analysis(dialog.folder / entry["data"])
            assert derived.metadata["title"] == entry["title"]
            assert derived.metadata["axes"]["x"]["unit"] == "Hz"
            assert derived.metadata["colour_levels"] == [-102, -88]
            assert derived.values.shape == (2, 2)
        deck = Presentation(dialog.folder / index["powerpoint"])
        assert len(deck.slides) == 2
        assert deck.slides[0].shapes[0].text == index["items"][0]["title"]
        dialog.close()
        page.result_tabs.setCurrentIndex(page._spectrum_index)
        spectrum = page.spectrum_tab
        spectrum.show_stored_spectrum(0)
        wait_until(app, spectrum.spectrum_plot.batch_button.isEnabled)
        spectrum.spectrum_plot.batch_button.click()
        assert page._batch_dialog is not dialog
        spectrum_dialog = page._batch_dialog
        spectrum_dialog.start_button.click()
        wait_until(app, lambda: spectrum_dialog._task is None)
        assert "4 of 4 saved" in spectrum_dialog.status.text()
        assert len(list(spectrum_dialog.folder.glob("*.csv"))) == 4
        assert source.read_bytes() == before
    finally:
        page.close()


def test_sweep_powerpoint_action_is_visible_before_loading_heatmap(tmp_path, shell_qt_application):
    source = tmp_path / "data.h5"
    _write_two_axis_sweep(source)
    app = shell_qt_application
    page = ResultsPage(str(source.parent))
    page.resize(1400, 900)
    page.show()
    try:
        page._on_file_selected(source)
        wait_until(app, lambda: page._result_task is None and page._thatec_run is not None)
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        tab = page.heatmap_tab
        wait_until(app, lambda: not tab._read_tasks)
        app.processEvents()
        action = tab.powerpoint_button
        assert action.isVisibleTo(page) and action.isEnabled()
        assert tab.controls_card.rect().contains(action.geometry())
        assert tab.heatmap_view.currentWidget() is tab.heatmap_state
        action.click()
        dialog = page._batch_dialog
        assert dialog.isVisible() and dialog.power_point.isChecked()
        dialog.start_button.click()
        wait_until(app, lambda: dialog._task is None)
        assert len(Presentation(dialog.folder / "Plots.pptx").slides) == 2
        assert not dialog.preview.pixmap().isNull()
    finally:
        page.close()


def test_series_views_restore_after_reopen_and_export_individually(tmp_path, shell_qt_application):
    source = tmp_path / "data.h5"
    writer = Hdf5RunWriter(source, recipe_source="name: series\n", settings_source="",
                          plan_hash="series", device_idn={}, expected_points=9)
    for series in (4, 5, 6):
        for current in (-.001, 0, .001):
            index = writer.point_count
            writer.append(MeasurementPoint(
                index=index, setpoints={"keithley.A.current": current, "keithley.B.current": .001 * series},
                measurements={}, metadata={"series_number": series, "series_id": str(series)},
            ), SpectrumTrace((1e6, 2e6, 3e6), (-80. - index, -70., -78.), datetime.now(UTC), "TRAC1"))
    writer.close("completed")
    original = source.read_bytes()
    app = shell_qt_application
    ranges = {
        4: ((1.1e6, 2.4e6), (-.0008, .0008)),
        5: ((1.6e6, 3.1e6), (-.0012, .0005)),
    }
    levels = {4: (-92, -75), 5: (-81, -68)}
    palettes = {4: "inferno", 5: "viridis"}

    def open_page():
        page = ResultsPage(str(source.parent))
        page.resize(1400, 900)
        page.show()
        page._on_file_selected(source)
        wait_until(app, lambda: page._result_task is None and page._thatec_run is not None)
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        tab = page.heatmap_tab
        wait_until(app, lambda: not tab._read_tasks)
        return page, tab

    page, tab = open_page()
    try:
        plot = tab.heatmap
        for series in (4, 5):
            tab.series_combo.setCurrentIndex(tab.series_combo.findData(series))
            if series == 5:
                _, maximum = tab._range_combos["keithley.A.current"]
                maximum.setCurrentIndex(maximum.findData(0.0))
            tab.load_button.click()
            wait_until(app, tab.save_series_view_button.isEnabled)
            assert tab.save_series_view_button.isVisibleTo(page)
            if series == 5:
                tab.processing_controls.set_state(ResultProcessing(modes=("denoise",)), emit=True)
                wait_until(app, tab.save_series_view_button.isEnabled)
            plot.colormap_combo.setCurrentText(palettes[series])
            plot.color_bar.setLevels(levels[series])
            plot.plot.setRange(xRange=ranges[series][0], yRange=ranges[series][1], padding=0)
            plot.plot.getViewBox().invertY(series == 5)
            if series == 5:
                tab.map_controls.colour.setCurrentIndex(tab.map_controls.colour.findData("robust"))
                wait_until(app, tab.save_series_view_button.isEnabled)
                plot.color_bar.setLevels(levels[series])
            tab.save_series_view_button.click()
            assert "Saved" in tab.series_view_status.text()
    finally:
        assert page.shutdown()
        page.close()
        app.processEvents()

    page, tab = open_page()
    try:
        for series in (4, 5):
            tab.series_combo.setCurrentIndex(tab.series_combo.findData(series))
            wait_until(app, tab.save_series_view_button.isEnabled)
            assert tab.heatmap.colormap_combo.currentText() == palettes[series]
            assert tab.heatmap.color_bar.levels() == levels[series]
            assert tab.heatmap.plot.getViewBox().state["yInverted"] == (series == 5)
            np.testing.assert_allclose(tab.heatmap.plot.viewRange(), ranges[series])
        assert tab.map_controls.state.colour_range == "robust"
        assert tab.processing_controls.state.modes == ("denoise",)
        assert tab._range_combos["keithley.A.current"][1].currentData() == 0.0
        tab.powerpoint_button.click()
        dialog = page._batch_dialog
        assert dialog.power_point.isChecked()
        dialog.start_button.click()
        wait_until(app, lambda: dialog._task is None)
        index = json.loads((dialog.folder / "index.json").read_text(encoding="utf-8"))
        assert index["exported"] == 3
        deck = Presentation(dialog.folder / index["powerpoint"])
        assert len(deck.slides) == 3
        for series, entry, slide in zip((4, 5, 6), index["items"], deck.slides, strict=True):
            assert f"Series {series}" in entry["title"]
            assert slide.shapes[0].text == entry["title"]
            assert slide.shapes[1].image.blob == (dialog.folder / entry["image"]).read_bytes()
            metadata = read_heatmap_analysis(dialog.folder / entry["data"]).metadata
            assert metadata["saved_series_view"] == (series in ranges)
            assert metadata["map_processing"]["colour_range"] == ("robust" if series == 5 else "full")
            assert metadata["spectrum_processing"]["modes"] == (["denoise"] if series == 5 else [])
            assert entry["checkpoints"] == {4: [0, 1, 2], 5: [3, 4], 6: [6, 7, 8]}[series]
            if series in ranges:
                assert metadata["palette"] == palettes[series]
                assert metadata["colour_levels"] == list(levels[series])
                assert metadata["inverted_axes"] == [False, series == 5]
                np.testing.assert_allclose(metadata["view_ranges"], ranges[series])
        assert source.read_bytes() == original
    finally:
        assert page.shutdown()
        page.close()
        app.processEvents()


def test_series_views_can_be_updated_on_a_long_path(tmp_path, shell_qt_application):
    bundle = tmp_path / ("sample-" + "x" * max(65, 162 - len(str(tmp_path))))
    source = bundle / "measurements" / "sweeps" / ("sweep-" + "z" * 55) / "data.h5"
    native_path(source.parent).mkdir(parents=True)
    native_path(source).touch()
    identity = _archive_identity(source)
    folder = postprocessing_directory(source)
    file = native_path(folder / "data.series-heatmap-views.json")
    lock_file = native_path(folder / "data.series-heatmap-views.lock")
    assert len(str(file)) > 260
    try:
        for number in (4, 5):
            request = HeatmapRequest("frequency", "current", {SERIES_DIMENSION_ID: number})
            view = SeriesHeatmapView("row_1", request, "viridis", (-85., -68.), ((1., 2.), (-.001, .001)),
                                     (False, False), False, ResultProcessing(), MapProcessing())
            save_series_heatmap_view(identity, view)
        assert [key[-1] for key in read_series_heatmap_views(identity)] == [4, 5]
        assert not lock_file.exists()
    finally:
        file.unlink(missing_ok=True)
        lock_file.unlink(missing_ok=True)
        native_path(source).unlink(missing_ok=True)
        if native_path(folder).is_dir():
            native_path(folder).rmdir()
        native_path(source.parent).rmdir()


def test_spectrum_batch_plots_and_csv_stay_valid_when_cancelled(tmp_path):
    source = tmp_path / "spectra.h5"
    _write_current_sweep(source)
    points = Hdf5RunReader.points(source, include_details=False)
    items = plan_spectra((point.index, point.setpoints) for point in points)
    stop = Event()
    folder, index = export_batch(
        source, items, SpectrumBatch(points, ResultProcessing(), SpectrumView(), False),
        expected_identity=_archive_identity(source), power_point=True, cancelled=stop.is_set,
        progress=lambda *_: None, preview=lambda *_: stop.set(),
    )
    assert index["cancelled"]
    assert index["exported"] == 1
    assert "powerpoint" not in index
    item = index["items"][0]
    assert "Keithley B 0 A" in item["title"]
    with (folder / item["data"]).open(encoding="utf-8") as stream:
        values = list(csv.DictReader(stream))
    np.testing.assert_array_equal([float(row["frequency_hz"]) for row in values], [1e6, 2e6, 3e6])
    assert [row["unit"] for row in values] == ["dBm"] * 3
    assert len(list(folder.glob("*.png"))) == 1


def test_descriptive_batch_files_support_deep_windows_sweep_directories(tmp_path):
    source = tmp_path / ("long-catalogue-" + "a" * 100) / "measurements" / "sweeps" / ("test-" + "b" * 70) / "data.h5"
    native_path(source.parent).mkdir(parents=True)
    _write_current_sweep(native_path(source), currents=(.004,))
    source = native_path(source)
    points = Hdf5RunReader.points(source, include_details=False)
    items = plan_spectra((point.index, point.setpoints) for point in points)
    folder, index = export_batch(
        source, items, SpectrumBatch(points, ResultProcessing(), SpectrumView(), False),
        expected_identity=_archive_identity(source), power_point=True, cancelled=lambda: False,
        progress=lambda *_: None, preview=lambda *_: None,
    )
    assert index["exported"] == 1, index
    image = folder / index["items"][0]["image"]
    assert len(str(image)) > 260
    assert native_path(image).read_bytes().startswith(b"\x89PNG")
    with native_path(folder / index["powerpoint"]).open("rb") as stream:
        assert len(Presentation(stream).slides) == 1
