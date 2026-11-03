"""A single-field readback must not confirm unrelated UI drafts."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.devices.rigol_dg1000z.adapter import RigolChannelConfig
from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.mark.parametrize("waveform", ["SIN", "DC"])
def test_off_offset_edit_uses_single_parameter_path(shell_qt_application, waveform):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.resize(1360, 880)
        window.show()
        shell_qt_application.processEvents()
        page = window.rigol_page
        page.channel.setCurrentText("1")
        page.waveform.setCurrentText(waveform)
        page.level_mode.setCurrentText(page.LEVEL_MODE_HIGH_LOW)
        page.high_level.setText("1 mV")
        page.low_level.setText("1 mV" if waveform == "DC" else "-1 mV")
        baseline = page._visible_channel_config()
        page._confirmed_carrier_configs[1] = baseline
        page._output_state_known[1] = True
        page._output_states[1] = False
        operation, payload = page.quick_control_hardware_request(
            "rigol.1.offset", "2 mV", "output_off"
        )
        assert operation == "quick_setpoint"
        assert payload.quantity_text == "2 mV"
        # An unapplied carrier change must still get a complete configuration.
        page.waveform.setCurrentText("SQU")
        page.high_level.setText("1 mV")
        page.low_level.setText("-1 mV")
        operation, _ = page.quick_control_hardware_request(
            "rigol.1.offset", "2 mV", "output_off"
        )
        assert operation == "quick_configure"
        assert window.isVisible() and window.width() >= 820
    finally:
        window.close()
        shell_qt_application.processEvents()


def test_quick_field_routing_preserves_other_voltage_components():
    baseline = RigolChannelConfig(1, "SIN", 1000, .003, -.001)
    assert RigolPage._only_quick_field_changed(
        baseline, replace(baseline, high_level_v=.004, low_level_v=0), "offset"
    )
    assert not RigolPage._only_quick_field_changed(
        baseline, replace(baseline, high_level_v=.004), "offset"
    )
    assert not RigolPage._only_quick_field_changed(
        baseline, replace(baseline, phase_deg=90), "amplitude"
    )


def test_held_arrow_does_not_postpone_every_offset_update():
    timer = Mock()
    timer.isActive.side_effect = [False, True, True]
    editor = Mock()
    editor.property.return_value = True
    page = SimpleNamespace(
        _live_rigol_timers={"offset": timer},
        live_control_switch=SimpleNamespace(isChecked=lambda: True),
        _is_device_connected=lambda: True,
    )
    for _ in range(3):
        RigolPage._submit_active_voltage(page, "offset", editor)
    timer.start.assert_called_once_with(60)
    timer.stop.assert_not_called()


def test_partial_readback_cannot_invent_full_configuration():
    page = SimpleNamespace(_confirmed_carrier_configs={1: None, 2: None})
    RigolPage._record_confirmed_quick_readback(page, 1, "frequency", 2000)
    assert page._confirmed_carrier_configs[1] is None
    baseline = RigolChannelConfig(2, "SIN", 1000, .003, -.001, phase_deg=20)
    page._confirmed_carrier_configs[2] = baseline
    RigolPage._record_confirmed_quick_readback(page, 2, "frequency", 3000)
    assert page._confirmed_carrier_configs[2] == replace(baseline, frequency_hz=3000)
    RigolPage._record_confirmed_quick_readback(page, 2, "amplitude", .002)
    assert page._confirmed_carrier_configs[2].high_level_v == .002
    assert page._confirmed_carrier_configs[2].low_level_v == 0


def test_hidden_channel_readback_updates_evidence_without_touching_visible_form():
    record = Mock()
    page = SimpleNamespace(channel=SimpleNamespace(currentText=lambda: "1"),
                           _record_confirmed_quick_readback=record)
    RigolPage.quick_setpoint_value_read(page, "rigol.2.frequency", 7000)
    record.assert_called_once_with(2, "frequency", 7000)


def test_unapplied_phase_draft_is_not_metadata_or_output_confirmation(monkeypatch, shell_qt_application):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        shell_qt_application.processEvents()
        page = window.rigol_page
        page.channel.setCurrentText("1")
        page.waveform.setCurrentText("SIN")
        page.level_mode.setCurrentText(page.LEVEL_MODE_HIGH_LOW)
        page.frequency.setText("1 kHz")
        page.high_level.setText("1 mV")
        page.low_level.setText("-1 mV")
        page.phase.setText("0")
        baseline = page._visible_channel_config()
        page._confirmed_carrier_configs[1] = baseline
        page.phase.setText("15")
        page.quick_setpoint_value_read("rigol.1.frequency", 2000)
        assert page._confirmed_carrier_configs[1] == replace(baseline, frequency_hz=2000)
        assert page.phase.text() == "15"
        metadata = {value.key: value.value_si for value in page.manual_metadata_values()}
        assert metadata["rigol.1.phase_deg"] == 0
        assert metadata["rigol.1.frequency_hz"] == 2000
        issued = Mock()
        monkeypatch.setattr(page, "_is_device_connected", lambda: True)
        monkeypatch.setattr(page, "_issue_ui_operation", issued)
        page.request_output(True)
        request = issued.call_args.args[0]
        assert request.operation == "configure"
        assert request.payload.phase_deg == 15
        assert window.isVisible() and window.width() >= 820
    finally:
        window.close()
        shell_qt_application.processEvents()
