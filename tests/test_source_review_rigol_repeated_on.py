"""Repeated ON verifies the carrier without interrupting an active output."""

import pytest

from app.devices.rigol_dg1000z import RigolAdapter, RigolChannelConfig
from app.devices.rigol_dg1000z.adapter import (
    RigolBurstConfig, RigolFrequencySweepConfig, RigolModulationConfig,
)
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError
from app.settings.models import StationSettings
from tests.helpers import simulation_settings


@pytest.fixture
def rigol():
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    for channel in raw["devices"]["rigol"]["safety"]["channels"].values():
        channel["enabled"] = True
    settings = StationSettings.model_validate(raw)
    session = RigolSimulator()
    adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    yield adapter, session
    adapter.disconnect()


@pytest.mark.parametrize("channel", [1, 2])
def test_repeated_on_has_only_queries_and_preserves_level_updates(rigol, channel):
    adapter, session = rigol
    adapter.configure_channel(RigolChannelConfig(channel, "SIN", 1000, .001, -.001))
    assert adapter.set_output(channel, True)
    adapter.update_frequency(channel, 2000)
    before = len(session.commands)
    assert adapter.set_output(channel, True)
    commands = session.commands[before:]
    assert commands and all("?" in command for command in commands)
    assert float(session.query(f":SOUR{channel}:FREQ?")) == 2000
    assert session.query(f":OUTP{channel}?").strip().upper() in {"1", "ON"}


@pytest.mark.parametrize("mode", ["modulation", "sweep", "burst"])
@pytest.mark.parametrize("tampered", [False, True])
def test_repeated_on_advanced_modes_preserve_output_and_reject_drift(rigol, mode, tampered):
    adapter, session = rigol
    adapter.configure_channel(RigolChannelConfig(1, "SIN", 1000, .001, -.001))
    if mode == "modulation":
        adapter.configure_modulation(RigolModulationConfig(1, True, "AM", parameter=10))
        drift = ":SOUR1:AM:DEPT 20"
    elif mode == "sweep":
        adapter.configure_frequency_sweep(RigolFrequencySweepConfig(1, True, 100, 1000, 1))
        drift = ":SOUR1:SWE:TIME 2"
    else:
        adapter.configure_burst(RigolBurstConfig(1, True))
        drift = ":SOUR1:BURS:NCYC 2"
    adapter.set_output(1, True)
    if tampered:
        session.write(drift)
    session.commands.clear()
    if tampered:
        with pytest.raises(DeviceError):
            adapter.set_output(1, True)
        assert not any(session.output.values())
        assert ":OUTP1 ON" not in session.commands
    else:
        assert adapter.set_output(1, True)
        assert all("?" in command for command in session.commands)


@pytest.mark.parametrize("fault", ["frequency", "external_off", "query"])
def test_repeated_on_revalidates_and_shuts_down_on_failure(rigol, fault, monkeypatch):
    adapter, session = rigol
    adapter.configure_channel(RigolChannelConfig(1, "SIN", 1000, .001, -.001))
    adapter.set_output(1, True)
    if fault == "frequency":
        session.write(":SOUR1:FREQ 2000")
    elif fault == "external_off":
        session.write(":OUTP1 OFF")
    else:
        original_query = session.query
        def query(command):
            if command == ":SOUR1:FREQ?":
                raise DeviceError("Injected read timeout")
            return original_query(command)
        monkeypatch.setattr(session, "query", query)
    before = len(session.commands)
    reason = {"frequency": "FREQ", "external_off": "outside the confirmed", "query": "Injected read timeout"}[fault]
    with pytest.raises(DeviceError, match=reason):
        adapter.set_output(1, True)
    writes = [command for command in session.commands[before:] if "?" not in command]
    assert ":OUTP1 ON" not in writes
    assert ":OUTP1 OFF" in writes and ":OUTP2 OFF" in writes
    assert session.query(":OUTP1?").strip().upper() in {"0", "OFF"}
    assert session.query(":OUTP2?").strip().upper() in {"0", "OFF"}
