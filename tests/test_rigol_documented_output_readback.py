"""DG1000Z documented output responses must work through the DC workflow."""

import pytest

from app.devices.rigol_dg1000z.adapter import RigolAdapter, RigolChannelConfig, RigolOutputConfig
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError
from tests.helpers import simulation_settings


class DocumentedOutputSession(RigolSimulator):
    def query(self, command):
        response = super().query(command)
        if command.endswith(":LOAD?") and response.upper() in {"INF", "HIGHZ"}:
            return "9.900000E+37"
        if command.endswith((":GAT:POL?", ":SYNC:POL?")):
            return {"NORM": "POSITIVE", "INV": "NEGATIVE"}[response]
        if command.endswith(":POL?"):
            return {"NORM": "NORMAL", "INV": "INVERTED"}[response]
        if command.endswith(":MODE?"):
            return {"NORM": "NORMAL", "GAT": "GATED"}[response]
        return response


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("dc_v", [-.082, .082])
def test_documented_responses_allow_validated_dc_output(channel, dc_v):
    session = DocumentedOutputSession()
    settings = simulation_settings()
    raw = settings.model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    raw["devices"]["rigol"]["safety"]["channels"][str(channel)]["enabled"] = True
    settings = type(settings).model_validate(raw)
    adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_channel(RigolChannelConfig(channel, "DC", 1.0, dc_v, dc_v))
    adapter.configure_output(RigolOutputConfig(channel, gate_polarity="INV", sync_polarity="INV"))
    assert f":OUTP{channel}:GAT:POL NEG" in session.commands
    assert f":OUTP{channel}:SYNC:POL NEG" in session.commands
    assert f":OUTP{channel} ON" not in session.commands
    adapter.set_output(channel, True)
    assert session.output[channel]
    adapter.set_output(channel, False)
    assert not session.output[channel]


@pytest.mark.parametrize("field", ["POL", "MODE", "GAT:POL", "SYNC:POL"])
def test_unknown_readback_still_fails_closed(field):
    with pytest.raises(DeviceError, match="UNRECOGNIZED"):
        RigolAdapter._parse_output_enum("UNRECOGNIZED", field=field)


class EmptyModeAfterPolaritySession(DocumentedOutputSession):
    empty_pending = False

    def query(self, command):
        if command.endswith(":MODE?") and self.empty_pending:
            self.empty_pending = False
            self.commands.append(command)
            return ""
        response = super().query(command)
        if command in {":OUTP1:POL?", ":OUTP2:POL?"}:
            self.empty_pending = True
        return response


def test_one_empty_mode_reply_after_polarity_recovers_during_dc_validation():
    session = EmptyModeAfterPolaritySession()
    settings = simulation_settings()
    raw = settings.model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    adapter = RigolAdapter(type(settings).model_validate(raw),
                           session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .082, .082))
    adapter.configure_output(RigolOutputConfig(1))
    assert not session.output[1]
    adapter.set_output(1, True)
    assert session.output[1]
    adapter.set_output(1, False)
    assert not session.output[1]


@pytest.mark.parametrize("response,query_count", [("", 2), ("INVALID", 1)])
def test_missing_or_invalid_mode_stays_blocked_without_mutations(response, query_count):
    session = DocumentedOutputSession()
    adapter = RigolAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_channel(RigolChannelConfig(1, "DC", 1., .082, .082))
    original_query = session.query
    mode_queries = []

    def faulty_query(command):
        if command == ":OUTP1:MODE?":
            mode_queries.append(command)
            session.commands.append(command)
            return response
        return original_query(command)

    session.query = faulty_query
    before = len(session.commands)
    with pytest.raises(DeviceError, match="MODE readback"):
        adapter.configure_output(RigolOutputConfig(1))
    assert len(mode_queries) == query_count
    assert all(command.endswith("?") for command in session.commands[before:])
    assert not session.output[1]
