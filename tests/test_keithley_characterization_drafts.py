"""Restart preserves operator drafts, never a second hardware configuration."""

import json
from unittest.mock import Mock
from dataclasses import replace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.ui import characterization_card as module
from tests.helpers import loaded_settings


@pytest.mark.parametrize("channel", ["A", "B"])
def test_build_reads_fresh_hardware_configuration_without_cached_copy(environment, channel):
    from app.devices.keithley_2600 import KeithleySourceRequest
    from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
    app, _ = environment
    card = module.KeithleyCharacterizationCard(Mock(), loaded_settings())
    requests = {ch: KeithleySourceRequest(channel=ch, mode="current", level_si=0,
        compliance_si=.05, nplc=1, settle_time_s=.01) for ch in ("A", "B")}
    card.set_source_request_provider(lambda ch, mode, level: replace(requests[ch], mode=mode, level_si=level), lambda ch: "stop")
    try:
        card.channel_combo.setCurrentIndex(0 if channel == "A" else 1)
        card.start_level_edit.setText("1 uA")
        card.stop_level_edit.setText("3 uA")
        before = card._build_config()
        requests[channel] = replace(requests[channel], compliance_si=.04, nplc=2, settle_time_s=.02,
            source_autorange=False, source_range_si=.001,
            measure_voltage_autorange=False, measure_voltage_range_si=.2,
            measure_current_autorange=False, measure_current_range_si=.001)
        after = card._build_config()
        applied = KeithleyCharacterizationRunner.source_request_for_level(after, after.start_level_si)
        assert applied == replace(requests[channel], level_si=1e-6)
        assert before.compliance_si != after.compliance_si
        assert (after.start_level_si, after.stop_level_si) == (1e-6, 3e-6)
        other = "B" if channel == "A" else "A"
        assert card._build_config(channel=other).compliance_si == .05
    finally:
        card.close()
        app.processEvents()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "drafts.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(module, "QSettings", lambda *args: settings)
    return app, settings


def test_restart_preserves_both_channels_modes_and_field_list(environment):
    app, stored = environment
    controller = Mock()
    card = module.KeithleyCharacterizationCard(controller, loaded_settings())
    expected = {}
    for channel in (0, 1):
        card.channel_combo.setCurrentIndex(channel)
        for mode in (0, 1):
            card.mode_combo.setCurrentIndex(mode)
            unit = "uA" if mode == 0 else "mV"
            values = (f"{channel + 1} {unit}", f"{10 + channel} {unit}", 11 + channel)
            card.start_level_edit.setText(values[0])
            card.stop_level_edit.setText(values[1])
            card.points_spin.setValue(values[2])
            expected[channel, mode] = values
    card.field_panel.currents.setText("-1 mA; 0 A; 1 mA; 0 A")
    card.field_panel.ramp_step.setText("50 uA")
    card.field_panel.hold.setText("30 s")
    card.field_panel.enabled_box.setChecked(True)
    card.close()
    saved = json.loads(stored.value(module.KEY_SWEEP_DRAFTS))
    assert set(saved) == {"schema_version", "channel", "modes", "sweeps", "field"}
    assert stored.status() == QSettings.Status.NoError
    reopened = module.KeithleyCharacterizationCard(controller, loaded_settings())
    try:
        assert reopened._selected_channel() == "B"
        assert reopened.mode_combo.currentIndex() == 1
        assert reopened.field_panel.currents.text() == "-1 mA; 0 A; 1 mA; 0 A"
        assert reopened.field_panel.ramp_step.text() == "50 uA"
        # A field series cannot remain armed when the restored form shows B.
        assert not reopened.field_panel.enabled_box.isChecked()
        assert not reopened.field_panel.enabled_box.isEnabled()
        for (channel, mode), values in expected.items():
            reopened.channel_combo.setCurrentIndex(channel)
            reopened.mode_combo.setCurrentIndex(mode)
            assert (reopened.start_level_edit.text(), reopened.stop_level_edit.text(),
                    reopened.points_spin.value()) == values
        controller.call.assert_not_called()
    finally:
        reopened.close()
        app.processEvents()


@pytest.mark.parametrize("raw", ['{broken', '{"schema_version":99}',
    '{"schema_version":1,"channel":"C","modes":{},"sweeps":{}}'])
def test_corrupt_draft_does_not_restore_or_call_device(environment, raw):
    app, stored = environment
    stored.setValue(module.KEY_SWEEP_DRAFTS, raw)
    controller = Mock()
    card = module.KeithleyCharacterizationCard(controller, loaded_settings())
    try:
        assert card._selected_channel() == "A"
        assert not card.field_panel.enabled_box.isChecked()
        controller.call.assert_not_called()
    finally:
        card.close()
        app.processEvents()


def test_restart_preserves_interval_and_inactive_list(environment):
    app, _ = environment
    controller = Mock()
    card = module.KeithleyCharacterizationCard(controller, loaded_settings())
    card.field_panel.currents.setText('1 mA; 0 A; 1 mA')
    card.field_panel.input_mode.setCurrentIndex(1)
    card.field_panel.interval_start.setText('-10 mA')
    card.field_panel.interval_stop.setText('10 mA')
    card.field_panel.interval_points.setValue(5)
    card.close()
    reopened = module.KeithleyCharacterizationCard(controller, loaded_settings())
    try:
        assert reopened.field_panel.input_mode.currentIndex() == 1
        assert reopened.field_panel.current_values() == pytest.approx((-.01, -.005, 0, .005, .01))
        reopened.field_panel.input_mode.setCurrentIndex(0)
        assert reopened.field_panel.current_values() == (.001, 0, .001)
        controller.call.assert_not_called()
    finally:
        reopened.close()
        app.processEvents()
