"""OFF revokes queued configure→ON intent; policy rendering preserves all states."""
from unittest.mock import Mock

import pytest

from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.mark.parametrize("channel", ["A", "B"])
def test_off_cancels_enable_after_pending_configuration(monkeypatch, shell_qt_application, channel):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        window._navigate_to("keithley")
        shell_qt_application.processEvents()
        page = window.keithley_page
        calls = Mock()
        monkeypatch.setattr(page._controller, "call", calls)
        page.channel.setCurrentText(channel)
        page._auto_enable_channel = channel
        page._pending_channels["configure"] = channel
        page._pending_config_modes[channel] = "current"
        page._output_toggled(False)
        assert page._auto_enable_channel is None
        page._result("configure", None)
        calls.assert_called_once_with("set_output", (channel, False))
        assert window.isVisible() and page.width() > 0
    finally:
        window.close()


def test_skip_compliance_survives_confirmation_and_readiness_refresh(monkeypatch, shell_qt_application):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        page = window.keithley_page
        monkeypatch.setattr(page._controller, "call", Mock())
        page._pending_compliance_policy["A"] = "skip"
        page._result("set_compliance_policy", "skip")
        page._update_output_readiness()
        assert page._compliance_policy["A"] == "skip"
        assert page.channel_cards["A"]["compliance_policy_combo"].currentData() == "skip"
        assert not page.channel_cards["A"]["stop_compliance_toggle"].isChecked()
    finally:
        window.close()


@pytest.mark.parametrize("failed", [False, True])
def test_group_off_is_retained_while_group_on_is_pending(monkeypatch, shell_qt_application, failed):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        window._navigate_to("keithley")
        shell_qt_application.processEvents()
        page = window.keithley_page
        calls = Mock()
        monkeypatch.setattr(page._controller, "call", calls)
        monkeypatch.setattr("app.devices.keithley_2600.ui.page.QMessageBox.warning", Mock())
        page.request_output_group(("A", "B"), True)
        page.request_output_group(("A",), False)
        page.request_output_group(("B",), False)
        assert calls.call_count == 1
        assert page._pending_output_group == (("A", "B"), True)
        if failed:
            page._error("set_output_group", "readback timeout")
            assert not any(page._output_state_known.values())
        else:
            page._result("set_output_group", {"A": True, "B": True})
        assert calls.call_count == 2
        calls.assert_called_with("set_output_group", (("A", "B"), False))
        assert page._pending_output_group == (("A", "B"), False)
        page._result("set_output_group", {"A": False, "B": False})
        assert page._pending_output_group is None
        assert all(page._output_state_known.values())
        assert not any(page._output_states.values())
        assert window.isVisible() and page.width() > 0
    finally:
        window.close()


@pytest.mark.parametrize("result,state,known_b", [
    ({"A": False, "B": True}, "MIXED", True),
    ({"A": False}, "UNKNOWN", False),
    ({"A": False, "B": 0}, "UNKNOWN", False),
])
def test_group_confirmation_does_not_infer_all_off(monkeypatch, shell_qt_application, result, state, known_b):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        page = window.keithley_page
        monkeypatch.setattr(page._controller, "call", Mock())
        page._set_channel_output("B", False)
        messages = []
        page.status.connect(messages.append)
        page.request_output_group(("A", "B"), False)
        page._result("set_output_group", result)
        assert messages[-1] == f"Keithley CH A+B OUTPUT {state}"
        assert page._output_state_known["B"] is known_b
    finally:
        window.close()


def test_channel_off_confirmations_keep_original_channel(monkeypatch, shell_qt_application):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        page = window.keithley_page
        calls = Mock()
        monkeypatch.setattr(page._controller, "call", calls)
        monkeypatch.setattr("app.devices.keithley_2600.ui.page.QMessageBox.warning", Mock())
        page._set_channel_output("A", True)
        page._set_channel_output("B", True)
        page.request_output_off("A")
        page.request_output_off("B")
        page.channel.setCurrentText("B")
        assert page._pending_channels["set_output"] == "A"
        assert calls.call_count == 1
        page._result("set_output", False)
        assert page._output_states == {"A": False, "B": True}
        calls.assert_called_with("set_output", ("B", False))
        assert page._pending_channels["set_output"] == "B"
        page._error("set_output", "timeout")
        assert page._output_state_known == {"A": True, "B": False}
        assert "set_output" not in page._pending_channels
        # Neither resetting the toggle nor a result without a boolean may
        # convert the unknown output to confirmed OFF.
        page._reset_output_toggle()
        assert not page._output_state_known["B"]
        page.request_output_off("B")
        page._result("set_output", None)
        assert not page._output_state_known["B"]
    finally:
        window.close()
