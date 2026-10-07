"""Even small private spectra and reference inspections stay off GUI."""
import threading
from pathlib import Path

from PySide6.QtCore import QThread, QTimer

from app.storage.hdf5_reader import Hdf5RunReader, StoredPoint, StoredSpectrum
from app.ui.results.spectrum_tab import SpectrumResultsTab
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_small_spectrum_does_not_inspect_or_read_hdf5_on_gui(shell_qt_application, monkeypatch):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    ticks = []
    trace = StoredSpectrum(0, "TRAC1", None, (1., 2., 3.), (-80., -40., -80.), 3, reference_index=0)

    def read(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        entered.set()
        assert release.wait(5)
        return trace

    def forbidden(*args, **kwargs):
        raise AssertionError("Synchronous spectrum/reference inspection")

    monkeypatch.setattr(Hdf5RunReader, "spectrum", read)
    monkeypatch.setattr(Hdf5RunReader, "spectrum_point_count", forbidden)
    monkeypatch.setattr(Hdf5RunReader, "reference", forbidden)
    page = SpectrumResultsTab()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        page.resize(1100, 700)
        page.show()
        page._selected_path = Path("not-opened.h5")
        point = StoredPoint(0, None, "completed", {}, {}, {}, {}, True)
        page.points_model.set_records((point,))
        page._on_point_selected(page.points_model.index(0, 0), None)
        timer.start(5)
        wait_until(app, lambda: entered.is_set() and len(ticks) >= 3)
        release.set()
        wait_until(app, lambda: page._selected_private_spectrum is trace)
        assert page.spectrum_variant_combo.findData("reference") >= 0
        assert page.spectrum_plot.width() > 0
    finally:
        release.set()
        timer.stop()
        page._read_pool.waitForDone(3000)
        app.processEvents()
        page.close()
        page.deleteLater()


def test_late_reference_error_cannot_replace_new_raw_view(shell_qt_application, monkeypatch):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()

    def reference(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise ValueError("Old reference failed")

    monkeypatch.setattr(Hdf5RunReader, "reference", reference)
    page = SpectrumResultsTab()
    try:
        page._selected_path = Path("unused.h5")
        point = StoredPoint(0, None, "completed", {}, {}, {}, {}, True)
        trace = StoredSpectrum(0, "TRAC1", None, (1., 2., 3.), (-80., -40., -80.), 3, reference_index=0)
        page._render_stored_spectrum(point, trace)
        page.spectrum_variant_combo.setCurrentIndex(page.spectrum_variant_combo.findData("raw_reference"))
        wait_until(app, entered.is_set)
        page.spectrum_variant_combo.setCurrentIndex(page.spectrum_variant_combo.findData("raw"))
        release.set()
        assert page._read_pool.waitForDone(3000)
        app.processEvents()
        assert page.spectrum_view.currentWidget() is page.spectrum_plot
        assert page._selected_reference is None
        assert "Old reference failed" not in page.spectrum_info.text()
    finally:
        release.set()
        page._read_pool.waitForDone(3000)
        page.close()
        page.deleteLater()


def test_return_from_reference_reloads_same_selected_point(shell_qt_application, monkeypatch):
    app = shell_qt_application
    trace = StoredSpectrum(0, "TRAC1", None, (1., 2., 3.), (-80., -40., -80.), 3)
    calls = []
    monkeypatch.setattr(Hdf5RunReader, "spectrum", lambda *a, **k: calls.append(1) or trace)
    monkeypatch.setattr(Hdf5RunReader, "reference", lambda *a, **k: None)
    page = SpectrumResultsTab()
    try:
        page._selected_path = Path("unused.h5")
        point = StoredPoint(0, None, "completed", {}, {}, {}, {}, True)
        page.points_model.set_records((point,))
        page.show_stored_spectrum(0)
        wait_until(app, lambda: page._selected_private_spectrum is trace)
        page.show_reference(0)
        assert page._selected_private_point is None
        wait_until(app, lambda: not page._read_tasks)
        page.show_stored_spectrum(0)
        wait_until(app, lambda: page._selected_private_spectrum is trace)
        assert calls == [1, 1]
    finally:
        page._read_pool.waitForDone(3000)
        app.processEvents()
        page.close()
        page.deleteLater()


def test_small_archive_initial_read_keeps_gui_alive_and_discards_stale_error(shell_qt_application, tmp_path, monkeypatch):
    from app.ui.results import page as results
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    ticks = []
    # Construct with an empty catalogue so no automatic selection races the test.
    page = results.ResultsPage(str(tmp_path))
    path = tmp_path / "tiny.h5"
    path.write_bytes(b"small file, slow reader")

    def read(selected):
        assert selected == path
        assert QThread.currentThread() != app.thread()
        entered.set()
        assert release.wait(5)
        raise ValueError("Stale archive failure")

    monkeypatch.setattr(results, "_read_result_payload", read)
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    errors = []
    original = page._on_result_failed
    def failed(*args):
        errors.append(args)
        original(*args)
    monkeypatch.setattr(page, "_on_result_failed", failed)
    try:
        page.resize(1440, 900)
        page.show()
        timer.start(5)
        page._on_file_selected(path)
        wait_until(app, lambda: entered.is_set() and len(ticks) >= 3)
        assert page._result_task is not None
        assert not page.resume_button.isEnabled()
        assert page.result_state.isVisible()
        page._on_file_selected(None)
        release.set()
        assert page._read_pool.waitForDone(5000)
        app.processEvents()
        assert page._selected_path is None
        assert not errors
        assert page._result_task is None
    finally:
        release.set()
        timer.stop()
        assert page.shutdown()
        page.close()
        page.deleteLater()
