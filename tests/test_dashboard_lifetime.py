"""Visible discovery page reflow and bounded cooperative worker shutdown."""

import os
import time
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QObject, QSettings, QThread
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from app.ui.dashboard import StationDashboardController
from app.settings import SettingsRepository
from app.ui.shell import MainWindow
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


class ControlledWorker(QThread):
    def __init__(self, parent):
        super().__init__(parent)
        self.entered, self.release = Event(), Event()
        self.stop_requests = 0

    def request_stop(self):
        self.stop_requests += 1

    def run(self):
        self.entered.set()
        self.release.wait(5)


def test_dashboard_coordinator_reflows_visible_page_and_waits_for_workers():
    application = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    application.setFont(QFont("Segoe UI", 10))
    owner = QWidget()
    registry = MagicMock()
    registry.all_modules.return_value = [SimpleNamespace(key=key, display_name=key)
        for key in StationDashboardController._DEVICE_KEYS]
    controller = StationDashboardController(simulation_settings(), registry, owner,
                                            discovery_enabled=False)
    workers = [ControlledWorker(controller) for _ in range(3)]
    controller._discovery_worker, controller._tcp_discovery_worker, controller._moke_identification_worker = workers
    try:
        assert isinstance(controller, QObject) and not isinstance(controller, QWidget)
        assert controller.parent() is owner
        owner.resize(1400, 900)
        controller.discovery_page.resize(1200, 800)
        owner.show()
        controller.discovery_page.show()
        application.processEvents()
        assert controller.discovery_page.isVisibleTo(owner)
        assert not controller._tcp_controls_compact
        controller.discovery_page.resize(800, 700)
        application.processEvents()
        assert controller._tcp_controls_compact
        assert controller.discovery_page.width() == 800
        controller.discovery_page.resize(1200, 800)
        application.processEvents()
        assert not controller._tcp_controls_compact
        for worker in workers:
            worker.start()
            assert worker.entered.wait(2)
        started = time.monotonic()
        assert not controller.shutdown(wait_ms=20)
        assert time.monotonic() - started < .5
        assert all(worker.isRunning() and worker.isInterruptionRequested() for worker in workers)
        assert workers[1].stop_requests == 1
        with pytest.raises(ValueError):
            controller.shutdown(wait_ms=-1)
        for worker in workers:
            worker.release.set()
        assert controller.shutdown(wait_ms=1000)
    finally:
        for worker in workers:
            worker.release.set()
            assert worker.wait(2000)
        owner.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
    assert not isValid(controller)
    assert all(not isValid(worker) for worker in workers)


def test_shell_refuses_close_until_discovery_thread_finishes(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    settings = simulation_settings()
    isolated = settings.model_copy(update={
        "storage": {**settings.storage, "output_directory": str(tmp_path / "measurements"),
                    "catalogue_directory": str(tmp_path / "catalogue")},
        "application": {**settings.application, "audit_log_directory": str(tmp_path / "logs"),
                        "restore_last_recipe": False},
    })
    path = tmp_path / "settings.yml"
    SettingsRepository(path).save(isolated)
    monkeypatch.setattr("app.ui.shell.main_window.QSettings",
        lambda *_args: QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    window = MainWindow(path, simulation=True)
    worker = ControlledWorker(window.dashboard)
    window.dashboard._tcp_discovery_worker = worker
    try:
        window.resize(1500, 950)
        window.show()
        application.processEvents()
        worker.start()
        assert worker.entered.wait(2)
        assert not window.close()
        assert window.isVisible() and isValid(worker) and worker.isRunning()
        assert worker.stop_requests == 1 and worker.isInterruptionRequested()
        worker.release.set()
        assert worker.wait(2000)
        cpu = window.anritsu_page.correction_workspace._cpu
        close_cpu = cpu.close
        # Inject an unfinished drain: the host must retain the same controller
        # for retry, rather than orphan it and later access a deleted wrapper.
        monkeypatch.setattr(cpu, "close", lambda **_kwargs: False)
        assert not window.close()
        assert window.isVisible() and isValid(cpu)
        assert cpu.parent() is window.anritsu_page.correction_workspace
        monkeypatch.setattr(cpu, "close", close_cpu)
        assert window.close()
        assert not window.anritsu_page.correction_workspace._cpu._thread.isRunning()
    finally:
        worker.release.set()
        assert worker.wait(2000)
        window.close()
        window.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
    assert not isValid(worker)


def test_pending_layout_callbacks_cancel_when_their_owner_is_deleted():
    from app.ui.measurement_tree.view import MeasurementTreeView
    from app.ui.results.page import _FluentResultSections
    from app.ui.results.metadata_panel import _FluentMetadataSections

    application = QApplication.instance() or QApplication([])
    owner = QWidget()
    tree = MeasurementTreeView(owner)
    sections = [_FluentResultSections(owner), _FluentMetadataSections(owner)]
    for section in sections:
        section.addTab(QWidget(section), "Details")
        section._schedule_navigation_sync()
        assert section._navigation_sync_pending
    tree._expand_all_after_reset()
    # Delete before the zero-delay callbacks can run. QObject context must
    # cancel them; processing their callables against deleted controls raises.
    owner.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
    assert all(not isValid(widget) for widget in [tree, *sections])
