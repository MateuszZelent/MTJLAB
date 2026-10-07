"""Reference setup must open through actual clicks in the Fluent shell."""
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.ui.shell import MainWindow
from tests.helpers import SETTINGS_TEMPLATE
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


@pytest.mark.parametrize("size", [(1500, 900), (820, 560)])
def test_reference_button_opens_and_restores_without_device_commands(shell_qt_application, size):
    window = MainWindow(SETTINGS_TEMPLATE, simulation=True)
    window.resize(*size)
    window.show()
    window._navigate_to("anritsu")
    page = window.anritsu_page
    dialog = page.reference_dialog
    try:
        QTest.qWait(100)
        with patch.object(page._controller, "call") as dispatch:
            for minimized in (False, True):
                if minimized:
                    dialog.showMinimized()
                    shell_qt_application.processEvents()
                    assert dialog.isMinimized()
                if page.compact_plot_settings.isVisibleTo(window):
                    QTest.mouseClick(page.compact_plot_settings, Qt.MouseButton.LeftButton)
                    QTest.qWait(60)
                button = page.correction_controls.configure_reference
                assert button.isVisible() and button.isEnabled()
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                QTest.qWait(100)
                assert dialog.isVisible() and not dialog.isMinimized()
                assert dialog.parentWidget() is window
                assert dialog.windowHandle().transientParent() is window.windowHandle()
                assert not page._presentation_popup.isVisible()
                assert page.reference_status.isVisibleTo(dialog)
                assert page.load_reference.isVisibleTo(dialog)
                assert page.use_current_reference.isVisibleTo(dialog)
                assert dialog.screen().availableGeometry().contains(dialog.geometry().center())
                for control in (page.load_reference, page.capture_reference, page.reference_operation):
                    assert dialog.rect().contains(control.rect().translated(control.mapTo(dialog, control.rect().topLeft())))
            assert not dispatch.called
            folder = Path("artifacts/reference-setup")
            folder.mkdir(parents=True, exist_ok=True)
            assert dialog.grab().save(str(folder / f"reference-{size[0]}.png"))
            dialog.accept()
            assert not dialog.isVisible()
    finally:
        window.close()
        shell_qt_application.processEvents()
