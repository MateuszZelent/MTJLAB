"""Closing analysis is bounded and retains ownership until completion."""

import threading
import time
from types import SimpleNamespace

from app.devices.anritsu_ms2830a.ui import analysis_worker
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


def test_slow_analysis_close_returns_without_publishing_late_result(shell_qt_application, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = analysis_worker.clean_spectrum_dbm
    calls, results = [], []
    def slow(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(analysis_worker, "clean_spectrum_dbm", slow)
    controller = analysis_worker.SpectrumAnalysisController()
    controller.result.connect(results.append)
    request = analysis_worker.SpectrumAnalysisRequest(1, (1., 2., 3., 4., 5.),
        (-80., -70., -30., -70., -80.), "raw", (), False)
    try:
        controller.submit(request)
        assert entered.wait(2)
        controller.submit(request)
        started = time.monotonic()
        assert not controller.close(timeout_ms=10)
        assert time.monotonic() - started < .5
        assert controller._thread.isRunning()
        assert controller._pending is None
        controller.submit(request)
        release.set()
        assert controller.close(timeout_ms=2000)
        shell_qt_application.processEvents()
        assert results == [] and calls == [1]
    finally:
        release.set()
        assert controller.close(timeout_ms=2000)


def test_page_requests_both_workers_even_when_first_is_busy():
    calls = []
    def worker(name, stopped):
        return SimpleNamespace(close=lambda: calls.append(name) or stopped)
    page = SimpleNamespace(
        _timer=SimpleNamespace(stop=lambda: None),
        _background_config_timer=SimpleNamespace(stop=lambda: None),
        _analysis_controller=worker("spectrum", False),
        _spectrogram_analysis_controller=worker("spectrogram", True),
    )
    assert not AnritsuPage.shutdown_analysis(page)
    assert calls == ["spectrum", "spectrogram"]


def test_shown_shell_keeps_window_alive_until_analysis_stops(shell_qt_application, monkeypatch):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        shell_qt_application.processEvents()
        with monkeypatch.context() as patch:
            patch.setattr(window.anritsu_page, "shutdown_analysis", lambda: False)
            assert not window.close()
            assert window.isVisible() and window.width() > 0
        assert window.close()
    finally:
        window.close()
