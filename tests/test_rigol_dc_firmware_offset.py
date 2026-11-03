"""DG1032Z 00.01.08 capture: DC writes work; numeric queries expose AC state."""
from dataclasses import replace

import pytest

from app.devices.rigol_dg1000z.adapter import RigolAdapter, RigolChannelConfig
from app.devices.rigol_dg1000z.module import _dispatch
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.models import DeviceState
from app.domain.quick_controls import AcceptedSetpoint, QuickControlCommand
from tests.helpers import simulation_settings


class DcOffsetIgnoringFirmware(RigolSimulator):
    """Keep the historical fixture name, but model the captured split state."""
    fault = None

    def __init__(self):
        super().__init__()
        self.dc_panel = {1: 0., 2: 0.}

    def _query(self, command):
        if command == self.fault:
            raise DeviceError("Injected DC failure")
        if command == "*IDN?":
            return "Rigol Technologies,DG1032Z,SIM-DC-BUG,00.01.08"
        response = super()._query(command)
        if command.endswith(":APPL?") and response.startswith('"DC,'):
            return response.removesuffix(',DEF"') + '"'
        return response

    def _write(self, command):
        if command == self.fault:
            raise DeviceError("Injected DC failure")
        if command.startswith((":SOUR1:APPL:DC ", ":SOUR2:APPL:DC ")):
            channel = int(command[5])
            self.waveform[channel] = "DC"
            self.dc_panel[channel] = float(command.split(",")[-1])
            return
        if (command.startswith((":SOUR1:VOLT:OFFS ", ":SOUR2:VOLT:OFFS "))
                and self.waveform[int(command[5])] == "DC"):
            self.dc_panel[int(command[5])] = float(command.split()[-1])
            return
        super()._write(command)


def connected(session=None):
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    for channel in raw["devices"]["rigol"]["safety"]["channels"].values():
        channel["enabled"] = True
    session = session or DcOffsetIgnoringFirmware()
    adapter = RigolAdapter(type(simulation_settings()).model_validate(raw),
                           session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    return adapter, session


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("enabled", [False, True])
def test_dc_steps_never_change_waveform_or_output(channel, enabled):
    adapter, session = connected()
    config = RigolChannelConfig(channel, "DC", 1., .083, .083)
    adapter.configure_channel(config)
    if enabled:
        adapter.set_output(channel, True)
    session.commands.clear()
    for level in [0., .01, .02, .08, -.08]:
        assert adapter.update_offset(channel, level) == pytest.approx(level)
        assert session.dc_panel[channel] == pytest.approx(level)
        assert session.waveform[channel] == "DC"
        assert session.output[channel] == enabled
        assert not session.output[3-channel]
        assert adapter.dc_readback_limitation(channel)
    writes = [c for c in session.commands if "?" not in c]
    assert writes == [f":SOUR{channel}:VOLT:OFFS {v}" for v in ["0", "0.01", "0.02", "0.08", "-0.08"]]
    # Firmware queries remain stale; they are not actual DC voltage evidence.
    assert float(session.query(f":SOUR{channel}:VOLT:OFFS?")) == 0.


def test_configure_dc_directly_and_reapply_without_sin():
    adapter, session = connected()
    config = RigolChannelConfig(1, "DC", 1., .083, .083)
    adapter.configure_channel(config)
    adapter.configure_channel(replace(config, high_level_v=.02, low_level_v=.02,
                                      changed_fields=("high_level_v", "low_level_v")))
    assert session.dc_panel[1] == pytest.approx(.02)
    assert ":SOUR1:FUNC SIN" not in session.commands
    assert ":SOUR1:VOLT MIN" not in session.commands
    assert not any(session.output.values())


def test_module_reports_command_acceptance_not_numeric_readback():
    adapter, session = connected()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    result = _dispatch(adapter, "quick_setpoint", QuickControlCommand("rigol.1.offset", "20 mV"))
    assert isinstance(result, AcceptedSetpoint)
    assert float(result) == pytest.approx(.02)
    assert "readback unavailable" in result.detail
    snapshot = _dispatch(adapter, "quick_readback", None)
    assert isinstance(snapshot["rigol.1.offset"], AcceptedSetpoint)
    assert float(snapshot["rigol.1.offset"]) == pytest.approx(session.dc_panel[1])


@pytest.mark.parametrize("fault", [":SOUR1:VOLT:OFFS 0.02", "*OPC?"])
@pytest.mark.parametrize("enabled", [False, True])
def test_failed_update_invalidates_config_and_shuts_down_if_on(fault, enabled):
    adapter, session = connected()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    if enabled:
        adapter.set_output(1, True)
    session.fault = fault
    with pytest.raises(DeviceError, match="Injected"):
        adapter.update_offset(1, .02)
    assert not any(session.output.values())
    with pytest.raises(SafetyViolation):
        adapter.last_channel_config(1)


@pytest.mark.parametrize("reply", ["SIN", "ARB", "invalid"])
def test_wrong_mode_blocks_dc_write(monkeypatch, reply):
    adapter, session = connected()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    original = session.query
    monkeypatch.setattr(session, "query", lambda c: reply if c == ":SOUR1:FUNC?" else original(c))
    session.commands.clear()
    with pytest.raises(DeviceError):
        adapter.update_offset(1, .02)
    assert not any(":VOLT:OFFS " in c for c in session.commands)


def test_scpi_error_fails_dc_update(monkeypatch):
    adapter, session = connected()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    adapter.set_output(1, True)
    original = session.query
    session.commands.clear()
    def query(c):
        if c == ":SYST:ERR?" and ":SOUR1:VOLT:OFFS 0.02" in session.commands:
            return '-222,"Data out of range"'
        return original(c)
    monkeypatch.setattr(session, "query", query)
    with pytest.raises(DeviceError, match="Data out of range"):
        adapter.update_offset(1, .02)
    assert not any(session.output.values())
    assert adapter.state == DeviceState.OUTPUT_OFF


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 99.])
def test_invalid_level_rejected_before_traffic(value):
    adapter, session = connected()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    session.commands.clear()
    with pytest.raises(SafetyViolation):
        adapter.update_offset(1, value)
    assert not session.commands


def test_other_firmware_keeps_numeric_readback_validation(monkeypatch):
    adapter, session = connected(RigolSimulator())
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    original = session.query
    monkeypatch.setattr(session, "query", lambda c: "0" if c == ":SOUR1:VOLT:OFFS?" else original(c))
    with pytest.raises(DeviceError):
        adapter.update_offset(1, .02)
    assert adapter.dc_readback_limitation(1) is None


def test_unexpected_output_on_during_dc_update_is_shut_down(monkeypatch):
    adapter, session = connected()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .083, .083))
    original = session._write
    def write(command):
        original(command)
        if command == ":SOUR1:VOLT:OFFS 0.02":
            session.output[1] = True
    monkeypatch.setattr(session, "_write", write)
    with pytest.raises(DeviceError, match="unexpectedly enabled"):
        adapter.update_offset(1, .02)
    assert not any(session.output.values())
    assert adapter.state == DeviceState.OUTPUT_OFF


def test_selected_dc_level_is_not_skipped_because_ac_register_matches():
    adapter, session = connected()
    config = RigolChannelConfig(1, "DC", 1., .02, .02)
    adapter.configure_channel(config)
    assert session.dc_panel[1] == .02
    # AC offset query says zero even though DC was set to 20 mV.
    adapter.configure_channel(replace(config, high_level_v=0., low_level_v=0.,
                                      changed_fields=("high_level_v", "low_level_v")))
    assert session.dc_panel[1] == 0.
