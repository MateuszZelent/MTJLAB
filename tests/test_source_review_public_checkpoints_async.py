"""Public scalar checkpoint metadata must be read without blocking Qt."""
import threading
from pathlib import Path

from PySide6.QtCore import QThread, QTimer

from app.storage import ThatecRunReader
from app.ui.results import spectrum_tab
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_results_browser import _write_public_fixture
from tests.test_spectrum_correction_controller import wait_until


def test_public_metadata_read_is_off_gui_and_processing_change_keeps_catalogue(shell_qt_application, tmp_path, monkeypatch):
    app = shell_qt_application
    path = tmp_path / "public.h5"
    _write_public_fixture(path)
    run = ThatecRunReader.describe(path)
    scalar = ThatecRunReader.scalar_series
    entered, release = threading.Event(), threading.Event()
    threads, ticks = [], []

    def read(*args):
        threads.append(QThread.currentThread())
        assert threads[-1] != app.thread()
        entered.set()
        assert release.wait(5)
        return scalar(*args)

    monkeypatch.setattr(ThatecRunReader, "scalar_series", read)
    page = spectrum_tab.SpectrumResultsTab()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        page.resize(1280, 800)
        page.show()
        timer.start(5)
        page.load(path, run, ())
        wait_until(app, lambda: entered.is_set() and len(ticks) >= 3)
        assert page.points_model.rowCount() == 0
        page._processing_changed()
        release.set()
        wait_until(app, lambda: page.points_model.rowCount() == 2 and not page._read_tasks)
        assert threads
        assert page._public_checkpoints[0].values["Current (A)"] == .1
        assert page._public_spectrum is not None
        assert page.points.isVisible() and page.points.width() > 0
    finally:
        release.set()
        timer.stop()
        assert page._read_pool.waitForDone(5000)
        app.processEvents()
        page.close()
        page.deleteLater()


def test_late_public_index_cannot_overwrite_new_file(shell_qt_application, monkeypatch):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    def build(path, run, *, cancelled):
        if path.name == "old.h5":
            entered.set()
            assert release.wait(5)
        return (spectrum_tab.PublicCheckpoint(10 if path.name == "old.h5" else 20, None, {}, ()),)
    monkeypatch.setattr(spectrum_tab, "build_public_checkpoints", build)
    page = spectrum_tab.SpectrumResultsTab()
    monkeypatch.setattr(page, "_populate_thatec_rows", lambda: None)
    try:
        page.load(Path("old.h5"), None, ())
        wait_until(app, entered.is_set)
        page.load(Path("new.h5"), None, ())
        release.set()
        wait_until(app, lambda: not page._read_tasks)
        assert page._selected_path.name == "new.h5"
        assert [point.index for point in page._public_checkpoints] == [20]
        assert page.points_model.index(0, 0).data() == "20"
    finally:
        release.set()
        assert page._read_pool.waitForDone(5000)
        app.processEvents()
        page.close()
        page.deleteLater()


def test_cancelled_catalogue_does_not_read_the_next_scalar(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / "public.h5"
    _write_public_fixture(path)
    run = ThatecRunReader.describe(path)
    def forbidden(*args):
        raise AssertionError("Cancelled catalogue must not begin scalar I/O")
    monkeypatch.setattr(ThatecRunReader, "scalar_series", forbidden)
    with pytest.raises(InterruptedError, match="cancelled"):
        spectrum_tab.build_public_checkpoints(path, run, cancelled=lambda: True)
