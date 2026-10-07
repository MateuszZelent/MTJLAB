"""Pending compliance keeps the request channel and mode, not the visible tab."""

from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.ui.page import KeithleyPage
from app.devices.simulators import simulated_station_settings
from tests.helpers import loaded_settings


@pytest.fixture(scope="module")
def qt_application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(monkeypatch, tmp_path, qt_application):
    monkeypatch.setattr("app.devices.keithley_2600.ui.page.QSettings", lambda *_: QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    widget = KeithleyPage(Mock(), simulated_station_settings(loaded_settings()))
    monkeypatch.setattr(widget, "_is_device_connected", lambda: True)
    widget.live_control_switch.setChecked(True)
    widget._set_channel_output("A", True)
    widget._set_channel_output("B", True)
    widget._controller.call.reset_mock()
    yield widget
    widget.close()
    widget.deleteLater()
    qt_application.processEvents()


def test_compliance_queue_captures_channel_mode_and_latest_value(page):
    messages = []
    page.status.connect(messages.append)
    page._queue_compliance_update("A", "current", .05)
    page._queue_compliance_update("B", "voltage", .001)
    page._queue_compliance_update("B", "voltage", .002)
    page.channel.setCurrentText("B")
    page.compliance.setText("999 mV")
    assert page._pending_channels["update_source_compliance"] == "A"
    assert page._controller.call.call_count == 1
    page._result("update_source_compliance", .05)
    assert any("CH A: compliance verified: 50 mV" in message for message in messages)
    page._controller.call.assert_called_with("update_source_compliance", ("B", "voltage", .002))
    page.channel.setCurrentText("A")
    page._result("update_source_compliance", .002)
    assert messages[-1] == "Keithley CH B: compliance verified: 2 mA"
    assert "update_source_compliance" not in page._pending_channels


@pytest.mark.parametrize("stop", ["error", "missing_readback", "off", "group_off"])
def test_pending_compliance_is_discarded_on_error_or_off(page, stop):
    page._queue_compliance_update("A", "current", .05)
    page._queue_compliance_update("B", "voltage", .002)
    if stop == "error":
        page._error("update_source_compliance", "timeout")
    elif stop == "missing_readback":
        page._result("update_source_compliance", None)
    else:
        if stop == "off":
            page.request_output_off("B")
        else:
            page.request_output_group(("B",), False)
        page._result("update_source_compliance", .05)
    compliance_calls = [call for call in page._controller.call.call_args_list if call.args[0] == "update_source_compliance"]
    assert len(compliance_calls) == 1
    assert not page._deferred_compliance


def test_on_cannot_overwrite_pending_request_or_off(page):
    page._pending_channels["configure"] = "A"
    page.channel.setCurrentText("B")
    page._output_toggled(True)
    page.request_output_group(("A", "B"), True)
    assert page._pending_channels["configure"] == "A"
    page._controller.call.assert_not_called()
    page._auto_enable_channel = "A"
    page._pending_config_modes["A"] = "current"
    page.request_output_off("B")
    page._result("configure", None)
    page._controller.call.assert_called_once_with("set_output", ("B", False))
    assert page._auto_enable_channel is None
    assert page._pending_channels["set_output"] == "B"
