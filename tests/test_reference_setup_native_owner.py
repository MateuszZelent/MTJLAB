"""Windows HWND ownership regression, without showing test windows on screen."""
import sys
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from app.ui.shell import MainWindow
from tests.helpers import SETTINGS_TEMPLATE
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


@pytest.mark.parametrize("workflow", ["reference", "recording"])
def test_workflow_native_window_survives_shell_insertion(shell_qt_application, workflow):
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        pytest.skip("Requires the native Windows Qt backend; use QT_QPA_PLATFORM=windows.")
    import win32con
    import win32gui

    window = MainWindow(SETTINGS_TEMPLATE, simulation=True)
    # Zero opacity hides the test without bypassing native window creation.
    window.setWindowOpacity(0)
    page = window.anritsu_page
    dialog = page.reference_dialog if workflow == "reference" else page.recording_dialog
    button = page.correction_controls.configure_reference if workflow == "reference" else page.record_spectra
    dialog.setWindowOpacity(0)
    try:
        window.show()
        window._navigate_to("anritsu")
        shell_qt_application.processEvents()
        # Page insertion used to destroy these eager dialogs at OS level
        # while Qt continued reporting them visible after show().
        assert win32gui.IsWindow(int(dialog.winId()))
        with patch.object(page._controller, "call") as dispatch:
            for minimized in (False, True):
                if minimized:
                    dialog.showMinimized()
                    shell_qt_application.processEvents()
                    assert dialog.isMinimized()
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                QTest.qWait(80)
                assert dialog.isVisible() and not dialog.isMinimized()
                assert win32gui.IsWindow(int(dialog.winId()))
                assert win32gui.IsWindowVisible(int(dialog.winId()))
                assert dialog.parentWidget() is window
                assert dialog.windowHandle().transientParent() is window.windowHandle()
                assert win32gui.GetWindow(int(dialog.winId()), win32con.GW_OWNER) == int(window.winId())
            assert not dispatch.called
            dialog.accept()
            assert not win32gui.IsWindowVisible(int(dialog.winId()))
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            QTest.qWait(80)
            assert win32gui.IsWindowVisible(int(dialog.winId()))
            assert not dispatch.called
    finally:
        window.close()
        shell_qt_application.processEvents()
