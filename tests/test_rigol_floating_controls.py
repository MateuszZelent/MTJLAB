"""Rigol controls retain their state and safety actions when undocked."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_floating_controls_render_and_restore_without_hardware_calls(theme):
    app = QApplication.instance() or QApplication([])
    apply_application_theme(app, theme)
    controller = Mock()
    page = RigolPage(controller, simulation_settings())
    page.resize(1440, 960)
    page.show()
    app.processEvents()
    frequency = page.frequency
    frequency.setText("2 kHz")
    controller.reset_mock()
    try:
        for close_method in ("window", "button", "escape", "placeholder"):
            page.open_floating_button.click()
            app.processEvents()
            window = page._control_window
            assert window is not None and window.isVisible()
            assert page._control_placeholder.isVisible()
            assert page.frequency is frequency
            assert frequency.text() == "2 kHz"
            for width in (1440, 900):
                window.resize(width, 960)
                app.processEvents()
                for control in (page.hero_card, page.output_off, frequency):
                    assert control.isVisibleTo(window)
                    assert control.width() > 0 and control.height() > 0
                    assert window.rect().contains(control.mapTo(window, control.rect().center()))
                artifacts = Path("artifacts/rigol-floating-controls")
                artifacts.mkdir(parents=True, exist_ok=True)
                assert window.grab().save(str(artifacts / f"rigol-{theme}-{width}.png"))
            if close_method == "button":
                page.open_floating_button.click()
            elif close_method == "escape":
                QTest.keyClick(window, Qt.Key.Key_Escape)
            elif close_method == "placeholder":
                page.dock_controls_button.click()
            else:
                window.close()
            app.processEvents()
            assert page._control_window is None
            assert page._control_placeholder is None
            assert page.frequency.isVisibleTo(page)
            assert page.output_off.isVisibleTo(page)
            assert frequency.text() == "2 kHz"
        controller.call.assert_not_called()
    finally:
        if page._control_window is not None:
            page._control_window.close()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_floating_output_off_uses_existing_request_path_and_readback():
    app = QApplication.instance() or QApplication([])
    controller = Mock()
    page = RigolPage(controller, simulation_settings())
    page.show()
    page._device_state_changed("output_on")
    page._set_rigol_channel_output(1, True)
    controller.reset_mock()
    try:
        page.open_floating_button.click()
        app.processEvents()
        assert page.output_off.isEnabled()
        page.output_off.click()
        controller.call.assert_called_once()
        assert controller.call.call_args.args == ("set_output", (1, False))
        page._set_rigol_channel_output(1, False)
        assert page.output_channel_state.text() == "CH1 OUTPUT OFF"
        page.set_execution_controlled(True)
        assert page.execution_badge.isVisibleTo(page._control_window)
        page._control_window.close()
        app.processEvents()
        assert page.output_channel_state.text() == "CH1 OUTPUT OFF"
    finally:
        if page._control_window is not None:
            page._control_window.close()
        page.close()
        page.deleteLater()
        app.processEvents()
