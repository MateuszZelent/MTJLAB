"""Filter calculations cannot stall Qt or republish a previous catalogue."""
import threading
import pytest

from PySide6.QtCore import QThread, QTimer

from app.storage import StoredPoint
from app.ui.results.spectrum_tab import SpectrumResultsTab
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_catalogue_and_value_grouping_run_off_gui(shell_qt_application, monkeypatch):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    ticks, kinds = [], []
    original = SpectrumResultsTab._build_filter_options
    def build(kind, records, key, *, cancelled):
        assert QThread.currentThread() != app.thread()
        kinds.append(kind)
        entered.set()
        assert release.wait(5)
        return original(kind, records, key, cancelled=cancelled)
    monkeypatch.setattr(SpectrumResultsTab, "_build_filter_options", staticmethod(build))
    page = SpectrumResultsTab()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        page._stored_points = tuple(StoredPoint(i, None, "ok", {"current_a": i * 1e-6}, {}, {}, {}, False) for i in range(100))
        page.resize(1440, 900)
        page.show()
        timer.start(5)
        page._populate_parameter_filters()
        wait_until(app, lambda: entered.is_set() and len(ticks) >= 3)
        assert not page.filter_parameter_combo.isEnabled()
        page._processing_changed()  # Spectrum cancellation must not kill filters.
        release.set()
        wait_until(app, lambda: page._filter_task is None)
        assert page.parameter_set_combo.count() == 101
        assert page.filter_parameter_combo.isEnabled()
        page.filter_parameter_combo.setCurrentIndex(page.filter_parameter_combo.findData("current_a"))
        wait_until(app, lambda: page._filter_task is None)
        assert page.filter_value_combo.count() == 101
        page.filter_value_combo.setCurrentIndex(page.filter_value_combo.findData(99e-6))
        wait_until(app, lambda: page._filter_task is None)
        assert page.points_model.rowCount() == 1
        assert kinds == ["catalogue", "values", "selection"]
    finally:
        release.set()
        timer.stop()
        page._cancel_filter_read()
        assert page._filter_pool.waitForDone(5000)
        page.close()
        page.deleteLater()
        app.processEvents()


def test_cancelled_value_scan_stops_even_when_values_are_missing():
    point = StoredPoint(0, None, "ok", {}, {}, {}, {}, False)
    with pytest.raises(InterruptedError, match="cancelled"):
        SpectrumResultsTab._build_filter_options("values", (point,) * 10000, "absent", cancelled=lambda: True)


def test_cancelled_catalogue_cannot_replace_new_points(shell_qt_application, monkeypatch):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    calls = []
    def build(kind, records, key, *, cancelled):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return ["old"], [(("old", 1.),)]
        return ["new"], [(("new", 2.),)]
    monkeypatch.setattr(SpectrumResultsTab, "_build_filter_options", staticmethod(build))
    page = SpectrumResultsTab()
    try:
        page._populate_parameter_filters()
        wait_until(app, entered.is_set)
        page.clear()
        page._populate_parameter_filters()
        release.set()
        wait_until(app, lambda: page._filter_task is None)
        assert page.filter_parameter_combo.findData("old") == -1
        assert page.filter_parameter_combo.findData("new") == 1
    finally:
        release.set()
        page._cancel_filter_read()
        assert page._filter_pool.waitForDone(5000)
        page.close()
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("clear", [False, True])
def test_late_filter_selection_cannot_override_clear_or_new_value(shell_qt_application, monkeypatch, clear):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    original = SpectrumResultsTab._build_filter_options
    selection_calls, ticks = [], []
    def build(kind, records, key, *, cancelled):
        if kind == "selection":
            assert QThread.currentThread() != app.thread()
            selection_calls.append(key)
            if len(selection_calls) == 1:
                entered.set()
                assert release.wait(5)
                # Simulate a noncooperative late result from the first selection.
                return (records[0],)
        return original(kind, records, key, cancelled=cancelled)
    monkeypatch.setattr(SpectrumResultsTab, "_build_filter_options", staticmethod(build))
    page = SpectrumResultsTab()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        points = tuple(StoredPoint(i, None, "ok", {"current_a": float(i)}, {}, {}, {}, False) for i in range(2))
        page._stored_points = points
        page._populate_points(points)
        page._populate_parameter_filters()
        wait_until(app, lambda: page._filter_task is None)
        page.filter_parameter_combo.setCurrentIndex(1)
        wait_until(app, lambda: page._filter_task is None)
        timer.start(5)
        page.filter_value_combo.setCurrentIndex(1)
        wait_until(app, lambda: entered.is_set() and len(ticks) >= 3)
        assert page.filter_summary.text() == "Filtering checkpoints..."
        if clear:
            page.clear_parameter_filter()
            assert page.points_model.records is points
        else:
            page.filter_value_combo.setCurrentIndex(2)
        release.set()
        assert page._filter_pool.waitForDone(5000)
        wait_until(app, lambda: page._filter_task is None)
        app.processEvents()
        assert page.points_model.records == (points if clear else (points[1],))
    finally:
        release.set()
        timer.stop()
        page._cancel_filter_read()
        assert page._filter_pool.waitForDone(5000)
        page.close()
        page.deleteLater()
        app.processEvents()
