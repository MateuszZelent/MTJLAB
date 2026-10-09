"""Recorded views, shared scientific tools, independent peak trajectories."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest

from app.storage import Hdf5RunReader, ThatecRunReader
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.peak_tools import recorded_peaks, track_result_series
from app.ui.results.processing import ResultProcessing
from app.ui.results.read_session import ResultReadSession
from app.ui.results.spectrum_views import SpectrumView, read_private_view, read_public_view
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_results_series_navigation import nested_archive as nested_archive, load_tab, close_tab  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.mark.parametrize("public", [False, True])
def test_three_overlay_curves_subtract_in_power_and_preserve_signed_values(nested_archive, public):
    before = hashlib.sha256(nested_archive.read_bytes()).hexdigest()
    session = ResultReadSession(nested_archive)
    point = session.point(0)
    raw = session.spectrum(nested_archive, 0)
    view = SpectrumView(show_analysis=False, show_raw=True, show_background=True, show_reference=True)
    if public:
        run = ThatecRunReader.describe(nested_archive)
        row = find_heatmap_rows(run)[0]
        spectrum = session.public_spectrum(nested_archive, row.id, 0)
        payload = read_public_view(session, spectrum, 0, (point,), ResultProcessing(), view)
    else:
        payload = read_private_view(session, point, ResultProcessing(), view)
    assert [trace.label for trace in payload.traces] == ["Raw", "Raw − background", "Raw − reference"]
    assert {trace.unit for trace in payload.traces} == {"W"}
    assert {baseline.purpose for baseline in payload.baselines} == {"background", "reference"}
    assert all(baseline.configuration_fingerprint == "settings" for baseline in payload.baselines)
    power = np.power(10., (np.asarray(raw.powers_dbm) - 30) / 10)
    np.testing.assert_allclose(payload.traces[0].values, power)
    np.testing.assert_allclose(payload.traces[1].values, power - 1e-10)
    np.testing.assert_allclose(payload.traces[2].values, power - 1e-9)
    assert np.any(np.asarray(payload.traces[2].values) < 0)
    logarithmic = read_private_view(session, point, ResultProcessing(), replace(view, unit="dBm"))
    invalid = np.asarray(payload.traces[2].values) <= 0
    assert np.all(np.isnan(np.asarray(logarithmic.traces[2].values)[invalid]))
    assert any("non-positive" in note for note in logarithmic.notes)
    assert hashlib.sha256(nested_archive.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="background"):
        read_private_view(session, point, ResultProcessing(), replace(view, background_index=1))


def test_recorded_markers_fixed_ranges_hold_float_and_export(nested_archive, shell_qt_application, tmp_path):
    app = shell_qt_application
    tab = load_tab(app, nested_archive)
    try:
        tab.view_controls.boxes["show_background"].setChecked(True)
        tab.view_controls.boxes["show_reference"].setChecked(True)
        wait_until(app, lambda: not tab._read_tasks)
        plot = tab.spectrum_plot
        assert {"Raw", "Raw − background", "Raw − reference"} <= plot._traces.keys()
        plot.show_tools()
        app.processEvents()
        assert plot.tools.isVisible() and plot._tools_window.isVisible()
        _, lower, upper = plot.axis_controls["x"]
        lower.setText("1.3 MHz")
        upper.setText("1.8 MHz")
        assert plot.apply_axis_range("x")
        plot.add_frequency_marker(1.5e6)
        plot.add_frequency_marker(1.6e6)
        plot.trace_selector.setCurrentText("Raw − reference")
        assert "Δf" in plot.marker_readout.text()
        plot.band_start.setText("1.4 MHz")
        plot.band_stop.setText("1.8 MHz")
        assert plot.apply_band() and "Samples:" in plot.band_readout.text()
        plot.toggle_max_hold()
        old = plot._max_hold.copy()
        plot.show_floating()
        mirror = plot._floating_window.mirror
        app.processEvents()
        np.testing.assert_array_equal(mirror._traces["Raw − reference"][1], plot._traces["Raw − reference"][1])
        mirror.freeze.setChecked(True)
        frozen = mirror._traces["Raw − reference"][1].copy()
        tab.next_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        np.testing.assert_allclose(plot.plot.viewRange()[0], (1.3e6, 1.8e6))
        assert len(plot.frequency_markers) == 2
        np.testing.assert_array_equal(mirror._traces["Raw − reference"][1], frozen)
        np.testing.assert_array_equal(plot._max_hold, np.maximum(old, plot._traces["Raw − reference"][1]))
        mirror.freeze.setChecked(False)
        np.testing.assert_array_equal(mirror._traces["Raw − reference"][1], plot._traces["Raw − reference"][1])
        destination = tmp_path / "overlay.csv"
        plot._export_csv(destination)
        metadata = json.loads(Path(str(destination) + ".analysis.json").read_text(encoding="utf-8"))
        assert metadata["checkpoint"] == 1
        assert metadata["value_unit"] == "W" and metadata["view"]["show_reference"]
        assert "Raw − reference" in metadata["visible_traces"]
        assert metadata["hold_checkpoints"]["Max hold"] == [0, 1]
        assert {baseline["purpose"] for baseline in metadata["recorded_baselines"]} == {"background", "reference"}
        tab.view_controls.unit.setCurrentIndex(tab.view_controls.unit.findData("dBm"))
        wait_until(app, lambda: not tab._read_tasks)
        assert plot._max_hold is None  # incompatible unit histories are discarded
        tab.view_controls.reset.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert "Stored spectrum" in plot._traces and not tab.view_controls.state.active
    finally:
        tab.peak_tools.close()
        assert tab.peak_tools.pool.waitForDone(5000)
        close_tab(app, tab)


def test_trajectory_uses_exact_filtered_checkpoints_with_loss_and_exports(nested_archive, shell_qt_application, monkeypatch, tmp_path):
    app = shell_qt_application
    tab = load_tab(app, nested_archive)
    try:
        tab.series_controls.axis.setCurrentIndex(tab.series_controls.axis.findData("keithley.A.current"))
        combo = tab.series_controls.fixed["keithley.B.current"]
        combo.setCurrentIndex(combo.findData(-.005))
        wait_until(app, lambda: tab._filter_task is None and not tab._read_tasks)
        from app.spectrum.analysis import SpectrumAnalysisParameters
        tab.peak_tools.parameters = SpectrumAnalysisParameters(peak_fit_models=False, peak_min_prominence_db=.5)
        import app.ui.results.peak_tools as module
        original = module.track_result_series
        def worker(*args, **kwargs):
            assert QThread.currentThread() != app.thread()
            return original(*args, **kwargs)
        monkeypatch.setattr(module, "track_result_series", worker)
        tab.peak_tools.open()
        wait_until(app, lambda: not tab.peak_tools._tasks)
        assert tab.peak_tools.peaks, tab.peak_tools.table.status.text()
        tab.peak_tools.table.track_requested.emit(0)
        tab.peak_tools.table.track_requested.emit(0)
        assert len(tab.peak_tools.windows) == 2
        first, second = tab.peak_tools.windows
        first.gate.setText("250 kHz")
        first.run.click()
        wait_until(app, lambda: not tab.peak_tools._tasks)
        assert len(first.records) == 41
        assert [p.checkpoint for p in first.records] == list(range(41, 82))
        assert sum(p.state == "detected" for p in first.records) >= 35, first.status.text()
        assert first.axis_unit == "A"
        assert len(second.records) == 0  # independent analysis windows
        first.quantity.setCurrentIndex(first.quantity.findData("amplitude"))
        assert first.plot._y_unit == "dBm"
        first.resize(760, 580)
        QTest.qWait(100)
        assert first.grab().save(str(tmp_path / "results-peak-trajectory-760.png"))
        first.quantity.setCurrentIndex(first.quantity.findData("frequency_hz"))
        assert first.plot._y_unit == "Hz"
        QTest.qWait(100)
        assert first.grab().save(str(tmp_path / "results-peak-frequency-760.png"))
        destination = tmp_path / "trajectory.csv"
        first.export_csv(destination)
        manifest = json.loads(Path(str(destination) + ".analysis.json").read_text(encoding="utf-8"))
        assert manifest["trajectory"]["checkpoints"] == list(range(41, 82))
        assert manifest["trajectory"]["gate_half_width_hz"] == 250e3
        plot_destination = tmp_path / "frequency-plot.csv"
        first.plot._export_csv(plot_destination)
        plot_manifest = json.loads(Path(str(plot_destination) + ".analysis.json").read_text(encoding="utf-8"))
        assert plot_manifest["plotted_quantity"] == "frequency_hz"
        assert plot_manifest["axes"] == {"x_unit": "A", "y_unit": "Hz"}
        with pytest.raises(ValueError, match="read-only"):
            first.plot._export_csv(nested_archive)
        first.close()
        app.processEvents()
        assert tab.peak_tools.windows == [second]
    finally:
        tab.peak_tools.close()
        assert tab.peak_tools.pool.waitForDone(5000)
        close_tab(app, tab)


def test_missing_bins_and_tracking_gate_never_create_a_false_bridge(nested_archive):
    from app.spectrum.analysis import SpectrumAnalysisParameters
    parameters = SpectrumAnalysisParameters(peak_fit_models=False)
    f = np.linspace(1e6, 2e6, 501)
    y = -80 + 30 * np.exp(-((f - 1.5e6) / 20e3) ** 2)
    assert recorded_peaks(f, y, "dBm", parameters)
    y[230:271] = np.nan
    assert not recorded_peaks(f, y, "dBm", parameters)
    records = Hdf5RunReader.points(nested_archive, include_details=False)[:3]
    result, _ = track_result_series(nested_archive, records, axis=None,
        source=("Stored spectrum", "raw", None, 0), processing=ResultProcessing(), view=SpectrumView(),
        points=records, parameters=parameters, target_hz=1.01e6, gate_hz=1e3)
    assert all(point.state == "lost within gate" and point.frequency_hz is None for point in result)


def test_baseline_units_and_peak_source_policy_are_explicit(nested_archive, shell_qt_application):
    from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
    app = shell_qt_application
    tab = load_tab(app, nested_archive)
    try:
        tab.show_reference(0)
        wait_until(app, lambda: not tab._read_tasks)
        tab.view_controls.unit.setCurrentIndex(tab.view_controls.unit.findData("W"))
        wait_until(app, lambda: not tab._read_tasks)
        assert tab.spectrum_plot._y_unit == "W"
        np.testing.assert_allclose(tab.spectrum_plot._traces["Background spectrum"][1], 1e-10)
        assert tab.spectrum_plot.export_metadata["recorded_baselines"][0]["purpose"] == "background"
        dialog = SpectrumAnalysisSettingsDialog(tab, section="peaks", selected_trace_only=True)
        assert not dialog.peak_measure_filtered.isEnabled() and dialog.peak_measure_filtered.isChecked()
        dialog.reset_to_defaults()
        assert dialog.peak_measure_filtered.isChecked()
        dialog.deleteLater()
    finally:
        tab.peak_tools.close()
        assert tab.peak_tools.pool.waitForDone(5000)
        close_tab(app, tab)


@pytest.mark.parametrize("width,theme", [(1440, "light"), (1024, "dark")])
def test_new_workbench_rendered_in_fluent_shell(nested_archive, shell_qt_application, tmp_path, width, theme):
    app = shell_qt_application
    font = Path("C:/Windows/Fonts/arial.ttf")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Arial", 10))
    window = MainWindow(".config/settings.yml", simulation=True)
    page = window.results_page
    try:
        window._set_theme_mode(theme, persist=False)
        window.resize(width, 900)
        window.show()
        window._navigate_to("results")
        page.set_output_directory(nested_archive.parent)
        page.file_browser.select_path(nested_archive)
        wait_until(app, lambda: page._result_task is None and page.spectrum_tab._filter_task is None
                   and not page.spectrum_tab._read_tasks and page._selected_path == nested_archive)
        page.result_tabs.setCurrentIndex(page._spectrum_index)
        tab = page.spectrum_tab
        tab.view_controls.boxes["show_background"].setChecked(True)
        tab.view_controls.boxes["show_reference"].setChecked(True)
        wait_until(app, lambda: not tab._read_tasks)
        QTest.qWait(150)
        host = window.navigation_routes["results"].scroll_area
        assert host.horizontalScrollBar().maximum() == 0
        for control in (tab.view_controls.unit, *tab.view_controls.boxes.values(), *tab.view_controls.baselines.values()):
            assert control.height() >= 22
            assert tab.view_controls.rect().contains(control.mapTo(tab.view_controls, control.rect().bottomRight()))
        host.ensureWidgetVisible(tab.view_controls)
        assert window.grab().save(str(tmp_path / f"results-analysis-shell-{theme}-{width}.png"))
        tab.spectrum_plot.show_floating()
        floating = tab.spectrum_plot._floating_window
        floating.resize(760, 620)
        QTest.qWait(100)
        assert floating.mirror.plot.width() > 200
        assert floating.mirror.tools.width() >= 300
        assert floating.grab().save(str(tmp_path / f"results-analysis-floating-{theme}-760.png"))
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        wait_until(app, lambda: not page.heatmap_tab._read_tasks)
        heatmap = page.heatmap_tab
        heatmap.y_axis_combo.setCurrentIndex(heatmap.y_axis_combo.findData("keithley.A.current"))
        heatmap.load_button.click()
        wait_until(app, lambda: not heatmap._read_tasks and heatmap.heatmap._data is not None)
        from app.ui.results.map_processing import MapProcessing
        heatmap.map_controls.set_state(MapProcessing(component="median_power", colour_range="symmetric"), emit=True)
        wait_until(app, lambda: not heatmap._read_tasks)
        host.ensureWidgetVisible(heatmap.map_controls)
        QTest.qWait(100)
        assert host.horizontalScrollBar().maximum() == 0
        assert heatmap.map_controls.operation.height() >= 30
        assert window.grab().save(str(tmp_path / f"results-map-shell-{theme}-{width}.png"))
    finally:
        window.recipe_page._close_discard_confirmed = True
        assert page.shutdown()
        window.close()
