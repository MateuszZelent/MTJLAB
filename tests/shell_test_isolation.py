"""Persistence isolation for shown-window tests; the actual shell is unchanged."""

from pathlib import Path
import time

import pytest
from PySide6.QtCore import QEvent, QSettings
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase
from shiboken6 import isValid

from app.settings import SettingsRepository


@pytest.fixture(scope="session", autouse=True)
def shell_qt_application():
    application = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 10))
    yield application


@pytest.fixture(autouse=True)
def isolated_shell_persistence(tmp_path, monkeypatch, request):
    from app.ui.shell import MainWindow
    if not hasattr(request.module, "MainWindow"):
        yield
        return
    windows = []

    def preferences(*_args):
        return QSettings(str(tmp_path / "ui-state.ini"), QSettings.Format.IniFormat)

    monkeypatch.setattr("app.ui.shell.main_window.QSettings", preferences)
    if hasattr(request.module, "QSettings"):
        monkeypatch.setattr(request.module, "QSettings", preferences)

    def create_window(settings_path, **kwargs):
        source = Path(settings_path)
        settings = SettingsRepository(source).load().settings
        isolated = settings.model_copy(update={
            "storage": {**settings.storage,
                "output_directory": str(tmp_path / "measurements"),
                "catalogue_directory": str(tmp_path / "catalogue")},
            "application": {**settings.application,
                "audit_log_directory": str(tmp_path / "logs"), "restore_last_recipe": False},
        })
        # Explicit temporary profiles retain their identity for save/reload tests.
        target = tmp_path / "settings.yml" if source.resolve().is_relative_to(Path.cwd()) else source
        SettingsRepository(target).save(isolated)
        window = MainWindow(target, **kwargs)
        windows.append(window)
        return window

    monkeypatch.setattr(request.module, "MainWindow", create_window)
    yield
    for window in windows:
        if isValid(window):
            window.recipe_page._close_discard_confirmed = True
            deadline = time.monotonic() + 30
            while not window.anritsu_page.prepare_manual_archive_shutdown() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(.005)
            assert window.close(), "Test shell still has an active shutdown task"
            window.deleteLater()
    application = QApplication.instance()
    if application is not None:
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
