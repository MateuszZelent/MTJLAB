"""Compliance mutations must stop on output-state or readback uncertainty."""
import pytest

from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.simulators import KeithleySimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError
from app.domain.models import DeviceState
from tests.test_keithley_coupled_ranges import request_for, settings_for


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode", ["current", "voltage"])
@pytest.mark.parametrize("fault", ["readback_mismatch", "external_output_on"])
def test_compliance_fault_establishes_off(monkeypatch, channel, mode, fault):
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(channel), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    baseline = request_for(channel, mode)
    adapter.configure_source(baseline)
    smu = f"smu{channel.lower()}"
    limit = "limitv" if mode == "current" else "limiti"
    query = session.query
    if fault == "readback_mismatch":
        adapter.set_output(channel, True)
        def mismatched_query(command):
            if command == f"print({smu}.source.{limit})":
                return str(baseline.compliance_si * .5)
            return query(command)
        monkeypatch.setattr(session, "query", mismatched_query)
    else:
        session.output[smu] = True
    session.commands.clear()
    try:
        with pytest.raises(DeviceError):
            adapter.update_source_compliance(channel, mode=mode, compliance_si=baseline.compliance_si * .8)
        assert not any(session.output.values())
        assert adapter.state == DeviceState.FAULT
        assert adapter.last_source_request(channel) == baseline
        if fault == "external_output_on":
            assert not any(f".source.{limit} = " in command for command in session.commands)
    finally:
        monkeypatch.setattr(session, "query", query)
        adapter.disconnect()
