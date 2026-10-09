"""Reproduce the DG1032Z 00.01.08 DC offset bug observed on real hardware."""

from dataclasses import replace

import pytest

from app.devices.rigol_dg1000z.adapter import RigolAdapter, RigolChannelConfig
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.models import DeviceState
from tests.helpers import simulation_settings


class DcOffsetIgnoringFirmware(RigolSimulator):
    fault = None

    def _query(self, command):
        if command == "*IDN?":
            return "Rigol Technologies,DG1032Z,SIM-DC-BUG,00.01.08"
        return super()._query(command)

    def _write(self, command):
        if command == self.fault:
            raise DeviceError("Injected DC preparation failure")
        if command.startswith((":SOUR1:APPL:DC ", ":SOUR2:APPL:DC ")):
            self.waveform[int(command[5])] = "DC"
            return
        if (command.startswith((":SOUR1:VOLT:OFFS ", ":SOUR2:VOLT:OFFS "))
                and self.waveform[int(command[5])] == "DC"):
            return  # Hardware returns No error, but keeps the previous offset.
        super()._write(command)


def connected():
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    for channel in raw["devices"]["rigol"]["safety"]["channels"].values():
        channel["enabled"] = True
    session = DcOffsetIgnoringFirmware()
    adapter = RigolAdapter(type(simulation_settings()).model_validate(raw),
                           session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    return adapter, session


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("level", [.083, -.083, 0., .099])
def test_dc_configuration_and_off_update_survive_ignored_dc_writes(channel, level):
    adapter, session = connected()
    config = RigolChannelConfig(channel, "DC", 1., .082, .082)
    adapter.configure_channel(config)
    assert adapter.update_offset(channel, level) == pytest.approx(level)
    applied = adapter.last_channel_config(channel)
    assert applied.high_level_v == applied.low_level_v == pytest.approx(level)
    assert session.waveform[channel] == "DC"
    assert not any(session.output.values())
    assert not any(command.endswith(" ON") for command in session.commands)
    assert f":SOUR{channel}:APPL:DC DEF,DEF,0.082" not in session.commands
    assert f":SOUR{channel}:VOLT MIN" in session.commands


def test_selected_dc_level_patch_uses_workaround():
    adapter, session = connected()
    config = RigolChannelConfig(1, "DC", 1., .082, .082)
    adapter.configure_channel(config)
    adapter.configure_channel(replace(config, high_level_v=.085, low_level_v=.085,
                                      changed_fields=("high_level_v", "low_level_v")))
    assert adapter.last_channel_config(1).high_level_v == pytest.approx(.085)
    assert not session.output[1]


def test_live_dc_edit_rejected_before_any_write_or_waveform_transition():
    adapter, session = connected()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .082, .082))
    adapter.set_output(1, True)
    session.commands.clear()
    with pytest.raises(SafetyViolation, match="requires OUTPUT OFF"):
        adapter.update_offset(1, .083)
    assert all(command.endswith("?") for command in session.commands)
    assert session.waveform[1] == "DC" and session.output[1]
    assert adapter.last_channel_config(1).high_level_v == pytest.approx(.082)


@pytest.mark.parametrize("fault", [
    ":SOUR1:FUNC SIN", ":SOUR1:VOLT MIN", ":SOUR1:VOLT:OFFS 0.083", ":SOUR1:FUNC DC",
])
def test_failed_preparation_forces_off_and_never_leaves_a_confirmed_carrier(fault):
    adapter, session = connected()
    session.fault = fault
    with pytest.raises(DeviceError, match="Injected"):
        adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    assert adapter.state == DeviceState.OUTPUT_OFF
    assert not any(session.output.values())
    with pytest.raises(SafetyViolation):
        adapter.set_output(1, True)
    assert ":OUTP1 ON" not in session.commands


def test_workaround_refuses_to_change_waveform_if_off_is_not_confirmed():
    adapter, session = connected()
    session.output[1] = True
    session.commands.clear()
    with pytest.raises(DeviceError):
        adapter._configure_dc_offset_while_off(RigolChannelConfig(1, "DC", 1., .083, .083))
    assert not any(":FUNC " in command for command in session.commands)
