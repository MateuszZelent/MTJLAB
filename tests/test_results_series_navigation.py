"""End-to-end Results series, passive live-math parity, I/O and scroll regressions."""

import hashlib
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QScrollArea

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.analysis_worker import SpectrumAnalysisRequest, _SpectrumAnalysisWorker
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader
from app.ui.design_system import apply_application_theme
from app.ui.results.page import ResultsPage, _read_result_payload
from app.ui.results.heatmap_tab import HeatmapPlotWidget
from app.ui.results.processing import ResultProcessing
from app.ui.results.read_session import ResultReadSession
from app.ui.results.spectrum_tab import SpectrumResultsTab
from app.ui.results.sweep_tree_panel import SweepTreePanel
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def nested_archive(tmp_path):
    path = tmp_path / "nested.h5"
    f = tuple(np.linspace(1e6, 2e6, 201))
    writer = Hdf5RunWriter(path, recipe_source="name: nested results\n", settings_source="schema_version: 1\n",
                          plan_hash="nested", device_idn={}, expected_points=82)
    def trace(values):
        return SpectrumTrace(f, tuple(values), datetime.now(UTC), "TRAC1")
    writer.store_reference(trace(np.full(len(f), -70.)),
                           acquisition_metadata={"purpose": "background", "configuration_fingerprint": "settings"})
    writer.store_reference(trace(np.full(len(f), -60.)),
                           acquisition_metadata={"purpose": "reference", "configuration_fingerprint": "settings"})
    for b in (-.004, -.005):
        for a in np.linspace(.002, .004, 41):
            index = writer.point_count
            values = -65 + 15 * np.exp(-((np.linspace(-1, 1, len(f)) - a * 100) / .12) ** 2)
            writer.append(MeasurementPoint(index=index,
                setpoints={"moke_box.vout0.voltage": .3217, "keithley.B.current": b, "keithley.A.current": float(a)},
                measurements={"keithley.A.current_a": float(a), "keithley.A.voltage_v": float(a * 100)},
                metadata={"spectrum_processing_v1": {"configuration_fingerprint": "settings"}}), trace(values))
    writer.close("completed")
    return path


def close_tab(app, tab):
    tab._invalidate_pending_reads()
    tab._cancel_filter_read()
    assert tab._read_pool.waitForDone(5000)
    assert tab._filter_pool.waitForDone(5000)
    tab.close()
    tab.deleteLater()
    app.processEvents()


def load_tab(app, path):
    tab = SpectrumResultsTab()
    tab.load(path, ThatecRunReader.describe(path), Hdf5RunReader.points(path, include_details=False),
             references=Hdf5RunReader.references(path, metadata_only=True))
    wait_until(app, lambda: tab._filter_task is None and not tab._read_tasks)
    return tab


@pytest.mark.parametrize("purpose,index", [("subtract_power_signed", 0), ("subtract_reference_signed", 1)])
def test_results_equals_actual_anritsu_analysis_worker(nested_archive, shell_qt_application, purpose, index):
    before = hashlib.sha256(nested_archive.read_bytes()).hexdigest()
    reader = ResultReadSession(nested_archive)
    raw = reader.spectrum(nested_archive, 0)
    ref = reader.reference(nested_archive, index)
    point = reader.point(0)
    state = ResultProcessing(purpose, index, ("narrow_reject", "denoise"))
    worker = _SpectrumAnalysisWorker()
    results, errors = [], []
    worker.completed.connect(results.append)
    worker.failed.connect(lambda *error: errors.append(error))
    worker.analyze(SpectrumAnalysisRequest(1, raw.frequencies_hz, raw.powers_dbm, state.modes, (), False,
        reference_values_dbm=ref.powers_dbm, reference_operation=state.math_operation, parameters=state.parameters))
    assert not errors and results
    processed = reader.processed_private(nested_archive, point, state)
    np.testing.assert_allclose(processed.values, results[0].cleanup.values, rtol=1e-13)
    assert processed.unit == results[0].cleanup.unit == "W"
    assert np.any(np.asarray(processed.values) < 0) if index == 1 else True
    assert hashlib.sha256(nested_archive.read_bytes()).hexdigest() == before


def test_fixed_b_series_navigates_41_a_values_and_exact_combination(nested_archive, shell_qt_application):
    app = shell_qt_application
    tab = load_tab(app, nested_archive)
    try:
        series = tab.series_controls
        series.axis.setCurrentIndex(series.axis.findData("keithley.A.current"))
        series.fixed["keithley.B.current"].setCurrentIndex(series.fixed["keithley.B.current"].findData(-.005))
        wait_until(app, lambda: tab._filter_task is None and not tab._read_tasks)
        assert tab.points_model.rowCount() == 41
        assert [p.index for p in tab.points_model.records] == list(range(41, 82))
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        assert "Post-processed spectrum" in tab.spectrum_plot._traces, tab.spectrum_info.text()
        for _ in range(3):
            tab.next_button.click()
            wait_until(app, lambda: not tab._read_tasks)
        assert tab._selected_private_point.index == 44
        assert tab.processing_controls.state.operation == "subtract_reference_signed"
        assert "4 / 41" in tab.position_label.text()
        # A full combination remains reachable; Clear restores the full run.
        series.axis.setCurrentIndex(0)
        series.fixed["keithley.A.current"].setCurrentIndex(1)
        wait_until(app, lambda: tab._filter_task is None and not tab._read_tasks)
        assert tab.points_model.rowCount() == 1
        tab.clear_parameter_filter()
        wait_until(app, lambda: not tab._read_tasks)
        assert tab.points_model.rowCount() == 82
    finally:
        close_tab(app, tab)


@pytest.mark.parametrize("width,theme", [(1440, "light"), (760, "dark")])
def test_navigation_keeps_plot_and_scroll_during_slow_read_and_cache_reuses_baseline(
    nested_archive, shell_qt_application, monkeypatch, tmp_path, width, theme
):
    app = shell_qt_application
    font = Path("C:/Windows/Fonts/arial.ttf")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Arial", 10))
    apply_application_theme(app, theme)
    entered, release = threading.Event(), threading.Event()
    calls = Counter()
    original_raw, original_ref = Hdf5RunReader.spectrum, Hdf5RunReader.reference
    def raw(path, index, **kwargs):
        assert QThread.currentThread() != app.thread()
        calls[("raw", index)] += 1
        if index == 1:
            entered.set()
            assert release.wait(5)
        return original_raw(path, index, **kwargs)
    def reference(path, index, **kwargs):
        assert QThread.currentThread() != app.thread()
        calls[("reference", index)] += 1
        return original_ref(path, index, **kwargs)
    monkeypatch.setattr(Hdf5RunReader, "spectrum", raw)
    monkeypatch.setattr(Hdf5RunReader, "reference", reference)
    tab = load_tab(app, nested_archive)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(tab)
    scroll.resize(width, 570)
    scroll.show()
    try:
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        assert "Post-processed spectrum" in tab.spectrum_plot._traces, tab.spectrum_info.text()
        app.processEvents()
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        offset = scroll.verticalScrollBar().value()
        rect = tab.spectrum_view.geometry()
        old_values = tab.spectrum_plot._traces["Post-processed spectrum"][1].copy()
        tab.next_button.click()
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(5)
        wait_until(app, lambda: entered.is_set() and len(ticks) >= 3)
        assert tab.spectrum_view.currentWidget() is tab.spectrum_plot
        assert tab.spectrum_view.geometry() == rect
        assert scroll.verticalScrollBar().value() == offset
        assert scroll.horizontalScrollBar().maximum() == 0
        np.testing.assert_array_equal(old_values, tab.spectrum_plot._traces["Post-processed spectrum"][1])
        release.set()
        wait_until(app, lambda: not tab._read_tasks)
        timer.stop()
        assert tab._selected_private_point.index == 1
        assert tab.spectrum_view.geometry() == rect
        assert scroll.verticalScrollBar().value() == offset
        tab.prev_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert calls[("raw", 0)] == calls[("raw", 1)] == 1
        assert calls[("reference", 1)] == 1
        assert tab._session.retained_bytes <= tab._session.budget_bytes
        scroll.grab().save(str(tmp_path / f"results-series-{theme}-{width}.png"))
    finally:
        release.set()
        close_tab(app, tab)
        scroll.close()
        scroll.deleteLater()
        apply_application_theme(app, "light")


def test_metadata_load_never_converts_all_spectra_with_pythat(nested_archive, monkeypatch):
    from app.ui.results import page
    def forbidden(*args, **kwargs):
        raise AssertionError("Whole-run PyThat conversion during browsing")
    monkeypatch.setattr(page, "read_pythat_run_data", forbidden)
    payload = _read_result_payload(nested_archive)
    assert len(payload.points) == 82 and len(payload.references) == 2
    assert payload.pythat_data is None


def test_unchanged_catalogue_refresh_keeps_selected_spectrum_and_processing(nested_archive, shell_qt_application, monkeypatch):
    app = shell_qt_application
    page = ResultsPage(str(nested_archive.parent))
    try:
        wait_until(app, lambda: page.file_browser._refresh_task is None)
        page.file_browser.select_path(nested_archive)
        wait_until(app, lambda: page._result_task is None and not page.spectrum_tab._read_tasks)
        tab = page.spectrum_tab
        tab.show_stored_spectrum(5)
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        assert "Post-processed spectrum" in tab.spectrum_plot._traces, tab.spectrum_info.text()
        session = tab._session
        from app.ui.results import page as module
        monkeypatch.setattr(module, "_read_result_payload", lambda p: pytest.fail("Reloaded unchanged run"))
        page.refresh()
        wait_until(app, lambda: page.file_browser._refresh_task is None)
        assert tab._session is session
        assert tab._selected_private_point.index == 5
        assert tab.processing_controls.state.operation == "subtract_reference_signed"
    finally:
        assert page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_cache_evicts_without_modifying_data(nested_archive):
    reader = ResultReadSession(nested_archive, budget_bytes=256)
    raw = reader.spectrum(nested_archive, 0)
    assert reader.retained_bytes == 0 and not reader._cache
    assert reader.spectrum(nested_archive, 0) == raw


def test_processing_change_after_loaded_before_finished_is_not_lost(nested_archive, shell_qt_application):
    app = shell_qt_application
    tab = load_tab(app, nested_archive)
    try:
        # The loaded signal has supplied the raw trace while the worker's
        # finished signal is still queued. This was a real lost-update window.
        assert tab._selected_private_spectrum is not None
        tab._read_context[tab._active_read_request] = ("private_details", tab._selected_private_point)
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        actual = tab.spectrum_plot._traces["Post-processed spectrum"][1]
        raw = np.asarray(tab._selected_private_spectrum.powers_dbm)
        np.testing.assert_allclose(actual, 10 ** ((raw - 30) / 10) - 1e-9)
    finally:
        close_tab(app, tab)


def test_selecting_new_run_during_refresh_keeps_full_catalogue(nested_archive, shell_qt_application, monkeypatch):
    from app.ui.results import file_browser
    import shutil
    other = nested_archive.with_name("second.h5")
    shutil.copyfile(nested_archive, other)
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    original = file_browser._read_catalogue
    def read(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(file_browser, "_read_catalogue", read)
    page = ResultsPage(str(nested_archive.parent))
    try:
        wait_until(app, entered.is_set)
        assert page.file_browser.select_path(other)
        release.set()
        wait_until(app, lambda: page.file_browser._refresh_task is None and page._selected_path == other
                   and page._result_task is None)
        assert len(page.file_browser._all_summaries) == 2
        assert page.file_browser.selected_path == other
    finally:
        release.set()
        assert page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_scalar_tree_inspection_reads_off_gui_and_uses_virtual_rows(nested_archive, shell_qt_application, monkeypatch):
    app = shell_qt_application
    run = ThatecRunReader.describe(nested_archive)
    row = next(row for row in run.rows.values() if len(row.shape) == 1)
    original = ThatecRunReader.scalar_series
    threads = []
    def read(*args, **kwargs):
        threads.append(QThread.currentThread())
        assert threads[-1] != app.thread()
        return original(*args, **kwargs)
    monkeypatch.setattr(ThatecRunReader, "scalar_series", read)
    panel = SweepTreePanel()
    try:
        panel._selected_path = nested_archive
        panel._selected_thatec_row = row
        panel._render_selected_row()
        wait_until(app, lambda: panel._detail_task is None)
        assert threads and panel._values_model.rowCount() == 82
        assert panel._values_model.data(panel._values_model.index(0, 1)) is not None
    finally:
        panel.cancel_detail_read()
        assert panel._detail_pool.waitForDone(5000)
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_oversized_cache_entry_does_not_evict_existing_working_set(nested_archive):
    reader = ResultReadSession(nested_archive, budget_bytes=2048)
    value = reader.cached(("small", 1), lambda: (1., 2., 3.))
    reader.cached(("large", 2), lambda: tuple(range(1000)))
    assert reader.cached(("small", 1), lambda: pytest.fail("Evicted useful cached entry")) is value
    assert reader.retained_bytes <= 2048


def test_display_and_processing_use_complete_10001_bin_raw_grid(tmp_path, shell_qt_application):
    path = tmp_path / "full-grid.h5"
    f = tuple(np.linspace(1e6, 6e9, 10001))
    values = np.full(10001, -70.)
    values[1234] = -45.
    writer = Hdf5RunWriter(path, recipe_source="name: full grid\n", settings_source="schema_version: 1\n",
                          plan_hash="full-grid", device_idn={})
    writer.store_reference(SpectrumTrace(f, tuple(np.full(10001, -80.)), datetime.now(UTC), "TRAC1"),
                           acquisition_metadata={"purpose": "reference"})
    writer.append(MeasurementPoint(index=0, setpoints={}, measurements={}),
                  SpectrumTrace(f, tuple(values), datetime.now(UTC), "TRAC1"))
    writer.close("completed")
    app = shell_qt_application
    tab = load_tab(app, path)
    try:
        assert len(tab._selected_private_spectrum.powers_dbm) == 10001
        assert len(tab.spectrum_plot._traces["Stored spectrum"][1]) == 10001
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        assert len(tab.spectrum_plot._traces["Post-processed spectrum"][1]) == 10001
    finally:
        close_tab(app, tab)


def test_public_processing_error_preserves_raw_for_reset(nested_archive, shell_qt_application, monkeypatch):
    app = shell_qt_application
    tab = load_tab(app, nested_archive)
    try:
        row = next(row for row in tab._run.rows.values() if len(row.shape) >= 2)
        tab.show_thatec_spectrum(row.id, 0)
        wait_until(app, lambda: not tab._read_tasks)
        original = tab._public_spectrum
        assert original is not None
        def fail(*args):
            raise ValueError("Injected processing failure")
        monkeypatch.setattr(tab._session, "processed_public", fail)
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        assert "Injected processing failure" in tab.spectrum_info.text()
        assert tab._public_spectrum is original
        tab.processing_controls.reset.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert tab.spectrum_view.currentWidget() is tab.spectrum_plot
        assert tab.spectrum_plot._traces
        assert not tab.processing_controls.state.active
    finally:
        close_tab(app, tab)


def test_nonuniform_heatmap_mesh_changes_when_only_interior_axis_changes(shell_qt_application):
    plot = HeatmapPlotWidget()
    try:
        values = np.arange(10, dtype=float).reshape(2, 5)
        plot.set_data(values, x_values=np.asarray((0., 1., 3., 5., 6.)), y_values=np.asarray((0., 1.)))
        before = plot._cached_mesh_vertices[0].copy()
        plot.set_data(values, x_values=np.asarray((0., 1., 4., 5., 6.)), y_values=np.asarray((0., 1.)))
        after = plot._cached_mesh_vertices[0]
        assert before.shape == after.shape
        np.testing.assert_array_equal(before[:, [0, -1]], after[:, [0, -1]])
        assert not np.array_equal(before, after)
        np.testing.assert_array_equal(after[0], plot._x_edges)
    finally:
        plot.close()
        plot.deleteLater()
        shell_qt_application.processEvents()


def test_catalogue_cancellation_stops_between_archive_reads(nested_archive, monkeypatch):
    import shutil
    shutil.copyfile(nested_archive, nested_archive.with_name("second.h5"))
    original = Hdf5RunReader.summary
    read = []
    def summary(path):
        read.append(path)
        return original(path)
    monkeypatch.setattr(Hdf5RunReader, "summary", summary)
    with pytest.raises(InterruptedError, match="indexing cancelled"):
        Hdf5RunReader.list_runs(nested_archive.parent, recursive=True, cancelled=lambda: bool(read))
    assert len(read) == 1
