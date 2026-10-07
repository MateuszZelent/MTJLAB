"""Acquisition must not trigger filesystem readiness I/O on the GUI thread."""
import threading
import time

from PySide6.QtWidgets import QApplication

from app.domain import readiness
from app.ui.dashboard.storage_probe import StorageReadinessProbe
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence, shell_qt_application  # noqa: F401


def spin_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.002)
    assert predicate()


def test_slow_directory_probe_is_off_gui_and_rejects_stale_path(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    owners = []
    def slow(path):
        owners.append(threading.get_ident())
        entered.set()
        if path == "old":
            assert release.wait(3)
        return True, path
    monkeypatch.setattr("app.ui.dashboard.storage_probe._probe_output_directory", slow)
    probe = StorageReadinessProbe()
    try:
        probe.set_path("old")
        assert entered.wait(1)
        probe.set_path("new")
        assert probe.result[0] is False
        for _ in range(10):
            QApplication.processEvents()
        assert probe.result[0] is False
        release.set()
        spin_until(lambda: probe.result == (True, "new"))
        assert all(owner != threading.get_ident() for owner in owners)
    finally:
        release.set()
        probe.close()


def test_live_safety_updates_do_not_probe_disk_but_preflight_does(monkeypatch):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        window._navigate_to("execution")
        QApplication.processEvents()
        calls = []
        def unavailable(path):
            calls.append(path)
            return False, "injected storage failure"
        monkeypatch.setattr(readiness, "_probe_output_directory", unavailable)
        for _ in range(100):
            window._refresh_safety_strip()
            window.dashboard.update_device_state("anritsu", "verified")
        assert calls == []
        result = window.dashboard.evaluate_readiness()
        assert len(calls) == 1
        assert any(item.key == "storage" for item in result.blocking_items)
        assert window.run_monitor.isVisibleTo(window)
    finally:
        window.close()
