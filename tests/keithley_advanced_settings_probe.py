"""Real Windows click/ownership regression, using an isolated simulation shell."""

from __future__ import annotations

import faulthandler
import os
from pathlib import Path
import sys
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
import win32con
import win32gui

from app.settings import SettingsRepository
from app.ui.design_system.fluent_theme import configure_widget_style
from app.ui.shell.main_window import MainWindow
from app.ui.widgets import LimitField
from tests.helpers import loaded_settings


def run(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    application = QApplication([])
    configure_widget_style(application)
    settings = loaded_settings()
    settings.keithley.safety.channels["A"].defaults.update(
        source_mode="current", source_current="1 mA", voltage_compliance="67 mV",
    )
    settings = settings.model_copy(update={
        "storage": {**settings.storage,
                    "output_directory": str(destination / "measurements"),
                    "catalogue_directory": str(destination / "catalogue")},
        "application": {**settings.application,
                        "audit_log_directory": str(destination / "logs"),
                        "restore_last_recipe": False},
        "ui": {**settings.ui, "theme": "light"},
    })
    profile = destination / "settings.yml"
    SettingsRepository(profile).save(settings)

    def preferences(*_args):
        return QSettings(str(destination / "ui-state.ini"), QSettings.Format.IniFormat)

    with (
        patch("app.ui.shell.main_window.QSettings", preferences),
        patch("app.devices.keithley_2600.ui.page.QSettings", preferences),
    ):
        window = MainWindow(profile, simulation=True)
        page = window.keithley_page
        panel = page.configuration_panel
        assert panel._advanced_ranges_dialog is None
        window.resize(1280, 800)
        window.show()
        window._navigate_to("keithley")
        QTest.qWait(150)
        beats = []
        heartbeat = QTimer(window)
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: beats.append(1))
        heartbeat.start()
        faulthandler.enable()
        faulthandler.dump_traceback_later(15, repeat=True)
        try:
            # Creating the popup during an active run must retain the locks
            # captured while its controls were still hidden page children.
            window._set_run_ui_locked(True)
            page.advanced_ranges_button.click()
            dialog = panel.advanced_ranges_dialog
            QTest.qWait(50)
            assert not page.source_range.isEnabled()
            assert not page.max_abs_power.isEnabled()
            assert dialog.titleBar.closeBtn.isEnabled()
            QTest.mouseClick(dialog.titleBar.closeBtn, Qt.MouseButton.LeftButton)
            assert not dialog.isVisible()
            window._set_run_ui_locked(False)
            print("PASS first opening during execution", flush=True)

            for width, height, channel in ((1280, 800, "A"), (860, 640, "B")):
                window.resize(width, height)
                page.channel.setCurrentText(channel)
                QTest.qWait(100)
                with patch.object(page._controller, "call") as dispatch:
                    # No replacement for exec/show: the real slot must return.
                    page.advanced_ranges_button.click()
                    dialog = panel.advanced_ranges_dialog
                    initial_beats = len(beats)
                    QTest.qWait(200)
                    assert dialog.isVisible() and not dialog.isModal()
                    assert application.activeModalWidget() is None
                    assert len(beats) > initial_beats
                    assert win32gui.IsWindowEnabled(int(window.winId()))
                    assert win32gui.IsWindow(int(dialog.winId()))
                    assert win32gui.GetWindow(int(dialog.winId()), win32con.GW_OWNER) == int(window.winId())
                    assert dialog.parentWidget() is panel
                    assert page.source_range_field in page.findChildren(LimitField)
                    assert dialog.screen().availableGeometry().contains(dialog.frameGeometry())
                    assert f"Channel {channel}" in panel.advanced_ranges_title.text()
                    assert page.source_range.isVisible()
                    assert page.max_abs_power_field.isVisible()
                    done = dialog.findChild(QPushButton, "keithleyAdvancedSourceSettingsDone")
                    assert done is not None and done.isVisible()
                    assert dialog.rect().contains(done.mapTo(dialog, done.rect().bottomRight()))
                    assert dialog.grab().save(str(destination / f"advanced-source-{channel}-{width}-light.png"))
                    # The application remains operable while the window is open.
                    window._navigate_to("overview")
                    QTest.qWait(50)
                    assert window.stackedWidget.currentWidget() is window.navigation_routes["overview"]
                    window._navigate_to("keithley")
                    page.advanced_ranges_button.click()
                    assert panel.advanced_ranges_dialog is dialog
                    dispatch.assert_not_called()
                    if channel == "A":
                        QTest.mouseClick(done, Qt.MouseButton.LeftButton)
                    else:
                        QTest.keyClick(dialog, Qt.Key.Key_Escape)
                    QTest.qWait(50)
                    assert not dialog.isVisible()
                    dispatch.assert_not_called()
                print(f"PASS native {channel} {width}", flush=True)

            # A run started from the still-interactive shell must lock these
            # same controls, while retaining a working close action.
            page.advanced_ranges_button.click()
            window._set_run_ui_locked(True)
            QTest.qWait(50)
            assert not page.source_range.isEnabled()
            assert not page.max_abs_power.isEnabled()
            done = dialog.findChild(QPushButton, "keithleyAdvancedSourceSettingsDone")
            assert done.isEnabled()
            QTest.mouseClick(done, Qt.MouseButton.LeftButton)
            assert not dialog.isVisible()
            window._set_run_ui_locked(False)
            assert page.source_range.isEnabled()
            print("PASS execution interlocks", flush=True)
        finally:
            faulthandler.cancel_dump_traceback_later()
            heartbeat.stop()
            if panel._advanced_ranges_dialog is not None:
                panel._advanced_ranges_dialog.close()
            window.recipe_page._close_discard_confirmed = True
            assert window.close()
            window.deleteLater()
            application.processEvents()


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
