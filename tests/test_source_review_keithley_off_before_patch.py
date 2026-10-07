"""A failed OUTPUT OFF must prevent all subsequent configuration writes."""
from dataclasses import replace

import pytest

from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.simulators import KeithleySimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError
from app.domain.models import DeviceState
from tests.test_keithley_coupled_ranges import request_for, settings_for


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode", ["current", "voltage"])
def test_ignored_off_blocks_selected_configuration_before_any_parameter_write(
    monkeypatch, channel, mode
):
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(channel), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    baseline = request_for(channel, mode)
    adapter.configure_source(baseline)
    adapter.set_output(channel, True)
    smu = f"smu{channel.lower()}"
    write = session.write

    def ignored_off(command):
        if command == f"{smu}.source.output = {smu}.OUTPUT_OFF":
            session.commands.append(command)
            return
        write(command)

    monkeypatch.setattr(session, "write", ignored_off)
    session.commands.clear()
    try:
        with pytest.raises(DeviceError):
            adapter.configure_source(replace(baseline, nplc=2, changed_fields=("nplc",)))
        mutations = [command for command in session.commands if " = " in command]
        assert mutations
        assert all(".source.output = " in command and "OUTPUT_OFF" in command for command in mutations)
        assert float(session.programmed[f"{smu}.measure.nplc"]) == baseline.nplc
        assert adapter.last_source_request(channel) == baseline
        assert adapter.state == DeviceState.UNKNOWN
    finally:
        monkeypatch.setattr(session, "write", write)
        adapter.disconnect()


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode", ["current", "voltage"])
def test_selected_parameter_write_follows_confirmed_off(channel, mode):
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(channel), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    baseline = request_for(channel, mode)
    adapter.configure_source(baseline)
    adapter.set_output(channel, True)
    smu = f"smu{channel.lower()}"
    session.commands.clear()
    try:
        applied = adapter.configure_source(replace(baseline, nplc=2, changed_fields=("nplc",)))
        off_index = session.commands.index(f"{smu}.source.output = {smu}.OUTPUT_OFF")
        nplc_index = session.commands.index(f"{smu}.measure.nplc = 2")
        assert f"print({smu}.source.output)" in session.commands[off_index + 1:nplc_index]
        assert adapter.last_source_request(channel) == applied == replace(baseline, nplc=2)
        assert session.output[smu] is False
    finally:
        adapter.disconnect()
