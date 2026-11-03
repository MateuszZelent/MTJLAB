"""Regression cases from the DG1000Z communication audit."""

import pytest

from app.devices.rigol_dg1000z.adapter import (
    RigolAdapter, RigolChannelConfig, RigolCounterConfig, RigolOutputConfig,
)
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from tests.helpers import simulation_settings


@pytest.fixture
def rigol():
    session = RigolSimulator()
    adapter = RigolAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    yield adapter, session
    adapter.disconnect()


@pytest.mark.parametrize("delay", [0, .000999, .001, .001001])
def test_sync_delay_is_bounded_by_carrier_period_before_writes(rigol, delay):
    adapter, session = rigol
    adapter.configure_channel(RigolChannelConfig(1, "SIN", 1000, .001, -.001))
    session.commands.clear()
    config = RigolOutputConfig(1, sync_delay_s=delay)
    if delay > .001:
        with pytest.raises(SafetyViolation, match="carrier period"):
            adapter.configure_output(config)
        assert not session.commands
    else:
        adapter.configure_output(config)
        assert not session.output[1]


@pytest.mark.parametrize("requested,response,valid", [
    ("USER3", "USER3", True), ("USER3", "USER1", False),
    ("AUTO", "AUTO", True), ("AUTO", "USER6", True),
    ("AUTO", "INVALID", False),
])
def test_counter_gate_requires_documented_readback(rigol, monkeypatch, requested, response, valid):
    adapter, session = rigol
    original = session.query

    def query(command):
        if command == ":COUN:GATE?":
            return response
        return original(command)

    monkeypatch.setattr(session, "query", query)
    config = RigolCounterConfig(gate_time=requested)
    if valid:
        adapter.configure_counter(config)
    else:
        with pytest.raises(DeviceError, match="counter gate"):
            adapter.configure_counter(config)

