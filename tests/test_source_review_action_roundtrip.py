"""Authored Set values survive opening the offline device node editor."""
import pytest

from app.devices.keithley_2600.ui import KeithleyNodeEditorDialog
from app.devices.rigol_dg1000z.ui import RigolNodeEditorDialog
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.mark.parametrize("channel", ["A", "B"])
def test_keithley_action_values_and_ranges_roundtrip(shell_qt_application, channel):
    dialog = KeithleyNodeEditorDialog(simulation_settings())
    try:
        dialog.channel.setCurrentText(channel)
        actions = [
            {"parameter_id": parameter, "mode": "set", "value": value}
            for parameter, value in [
                ("source.level", "0.35 mA"),
                ("source.compliance", "50 mV"),
                ("measurement.nplc", "2"),
                ("measurement.settling_time", "350 ms"),
                ("measurement.voltage_range", "1 V"),
                ("measurement.sense_mode", "2wire"),
            ]
        ]
        dialog.load_plan_actions(actions, "unchanged")
        dialog.resize(1100, 800)
        dialog.show()
        shell_qt_application.processEvents()
        assert dialog.configuration_snapshot().channel == channel
        assert not dialog.configuration_snapshot().measure_voltage_autorange
        assert {a['parameter_id']: a for a in dialog.planned_parameter_actions()} == {
            a['parameter_id']: a for a in actions
        }
        assert dialog.width() >= 800
        dialog.load_plan_actions([], "unchanged")
        assert dialog.planned_parameter_actions() == []
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize("values", [
    {"carrier.frequency": "3 kHz", "carrier.high_level": "30 mV", "carrier.low_level": "-10 mV"},
    {"carrier.amplitude": "40 mV", "carrier.offset": "10 mV"},
])
def test_rigol_action_values_roundtrip(shell_qt_application, values):
    actions = [{"parameter_id": key, "mode": "set", "value": value} for key, value in values.items()]
    dialog = RigolNodeEditorDialog(parameter_actions=actions)
    try:
        dialog.resize(1100, 800)
        dialog.show()
        shell_qt_application.processEvents()
        assert {a['parameter_id']: a['value'] for a in dialog.planned_parameter_actions()} == values
        snapshot = dialog.configuration_snapshot()
        assert snapshot.high_level == "30 mV"
        assert snapshot.low_level == "-10 mV"
        assert dialog.width() >= 800
    finally:
        dialog.close()
        dialog.deleteLater()
