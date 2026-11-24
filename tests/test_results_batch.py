import csv
import json
from threading import Event

import numpy as np
from pptx import Presentation

from app.storage import Hdf5RunReader
from app.storage.filesystem_paths import native_path
from app.storage.heatmap_analysis_store import read_heatmap_analysis
from app.ui.results.batch_processing import SpectrumBatch, export_batch, plan_heatmaps, plan_spectra
from app.ui.results.heatmap_coordinates import HeatmapCoordinates, HeatmapDimension, HeatmapRequest
from app.ui.results.heatmap_tab import _archive_identity
from app.ui.results.page import ResultsPage
from app.ui.results.processing import ResultProcessing
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
    assert "Keithley A -14 to +14 mA" in plans[0].title
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
            assert "Keithley B 0 to 1 mA" in entry["title"]
            assert "Keithley B 0 to 1 mA" in entry["image"]
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
