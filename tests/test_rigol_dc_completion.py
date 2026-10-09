"""DC writes must finish before verification, without replaying live setpoints."""

from dataclasses import replace

import pytest

from app.devices.rigol_dg1000z.adapter import RigolAdapter, RigolChannelConfig
from app.devices.rigol_dg1000z.module import _dispatch
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.models import DeviceState
from app.domain.quick_controls import QuickConfigureCommand
from tests.helpers import simulation_settings


class DeferredDcSession(RigolSimulator):
    """Error-free writes can leave the previous voltage until OPC completes."""

    def __init__(self):
        super().__init__()
        self.pending = []
        self.completion = "1"
        self.discard_writes = False

    def _write(self, command):
        if ":APPL:DC " in command or ":VOLT:OFFS " in command:
            self.pending.append(command)
            return
        super()._write(command)

    def _query(self, command):
        if command == "*OPC?":
            if self.completion == "timeout":
                raise DeviceError("Simulated operation-complete timeout")
            if self.completion != "1":
                return self.completion
            pending, self.pending = self.pending, []
            if not self.discard_writes:
                for write in pending:
                    super()._write(write)
            return "1"
        return super()._query(command)


def connected(channel=1):
    session = DeferredDcSession()
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    raw["devices"]["rigol"]["safety"]["channels"][str(channel)]["enabled"] = True
    settings = type(simulation_settings()).model_validate(raw)
    adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    baseline = RigolChannelConfig(channel, "DC", 1., .082, .082)
    adapter.configure_channel(baseline)
    session.commands.clear()
    return adapter, session, baseline


def test_reproducer_error_queue_does_not_mean_new_voltage_is_applied():
    _, session, _ = connected()
    session.write(":SOUR1:VOLT:OFFS 0.099")
    assert session.query(":SYST:ERR?").startswith("0,")
    assert float(session.query(":SOUR1:VOLT:OFFS?")) == .082
    assert session.query("*OPC?") == "1"
    assert float(session.query(":SOUR1:VOLT:OFFS?")) == .099


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("selected", [False, True])
def test_quick_configure_waits_for_dc_completion_before_readback(channel, selected):
    adapter, session, baseline = connected(channel)
    requested = replace(baseline, high_level_v=.099, low_level_v=.099,
                        changed_fields=("high_level_v", "low_level_v") if selected else None)
    result = _dispatch(adapter, "quick_configure",
                       QuickConfigureCommand(f"rigol.{channel}.offset", requested))
    assert result == pytest.approx(.099)
    assert adapter.last_channel_config(channel).high_level_v == pytest.approx(.099)
    assert not session.output[channel]
    assert not session.pending
    write = session.commands.index(f":SOUR{channel}:VOLT:OFFS 0.099")
    opc = session.commands.index("*OPC?", write)
    read = session.commands.index(f":SOUR{channel}:VOLT:OFFS?", opc)
    assert write < opc < read
    assert session.commands.count("*OPC?") == 1
    assert not any(command.endswith(" ON") for command in session.commands)


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("enabled", [False, True])
def test_live_dc_waits_without_replaying_or_cycling_output(channel, enabled):
    adapter, session, _ = connected(channel)
    if enabled:
        adapter.set_output(channel, True)
    session.commands.clear()
    assert adapter.update_offset(channel, .099) == pytest.approx(.099)
    assert session.output[channel] is enabled
    assert session.commands.count(f":SOUR{channel}:VOLT:OFFS 0.099") == 1
    assert session.commands.count("*OPC?") == 1
    assert not any(":APPL:" in command for command in session.commands)
    assert not any(command.endswith((" ON", " OFF")) for command in session.commands)


@pytest.mark.parametrize("fault", ["0", "", "INVALID", "timeout", "stale"])
@pytest.mark.parametrize("enabled", [False, True])
def test_unconfirmed_or_stale_live_update_cannot_be_accepted(fault, enabled):
    adapter, session, _ = connected()
    if enabled:
        adapter.set_output(1, True)
    session.commands.clear()
    session.discard_writes = fault == "stale"
    session.completion = "1" if fault == "stale" else fault
    with pytest.raises(DeviceError):
        adapter.update_offset(1, .099)
    assert not any(session.output.values())
    if enabled:
        assert adapter.state == DeviceState.OUTPUT_OFF
    assert session.commands.count(":SOUR1:VOLT:OFFS 0.099") == 1
    assert session.commands.count("*OPC?") == 1
    with pytest.raises(SafetyViolation):
        adapter.last_channel_config(1)


@pytest.mark.parametrize("fault", ["0", "timeout", "stale"])
def test_failed_quick_configuration_keeps_output_off_and_blocks_enable(fault):
    adapter, session, baseline = connected()
    session.discard_writes = fault == "stale"
    session.completion = "1" if fault == "stale" else fault
    with pytest.raises(DeviceError):
        adapter.configure_channel(replace(baseline, high_level_v=.099, low_level_v=.099))
    with pytest.raises(SafetyViolation):
        adapter.set_output(1, True)
    assert not any(session.output.values())
    assert ":OUTP1 ON" not in session.commands


def test_invalid_dc_request_is_rejected_before_visa_or_completion_query():
    adapter, session, _ = connected()
    with pytest.raises(SafetyViolation):
        adapter.update_offset(1, .1001)
    assert session.commands == []
