"""A failed optional query must not shift subsequent Rigol responses."""

import pytest

from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.visa import FakeVisaSession, FakeVisaSessionFactory
from app.domain.errors import DeviceError
from app.domain.models import DeviceState
from tests.helpers import simulation_settings


@pytest.mark.parametrize("query", [":SOUR1:MOD?", ":SOUR1:PHAS?", ":COUP?"])
def test_probe_timeout_stops_queries_and_releases_uncertain_session(query):
    def timeout(_command):
        raise TimeoutError("delayed instrument response")
    session = FakeVisaSession(responses={
        "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
        ":OUTP1?": "OFF", ":OUTP2?": "OFF", query: timeout,
    })
    adapter = RigolAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    with pytest.raises(DeviceError, match="synchronization is unconfirmed"):
        adapter.connect()
    assert session.writes[-1] == query
    assert ":OUTP1 OFF" in session.writes and ":OUTP2 OFF" in session.writes
    assert session.closed and adapter._session is None
    assert adapter.state == DeviceState.UNKNOWN
    assert adapter._capabilities is None


def test_answered_unsupported_probe_still_allows_basic_connection():
    session = FakeVisaSession(responses={
        "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
        ":OUTP1?": "OFF", ":OUTP2?": "OFF", ":SOUR1:MOD?": "unsupported",
    })
    adapter = RigolAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    try:
        adapter.connect()
        assert adapter.capabilities.supports("basic_waveform")
        assert not adapter.capabilities.supports("modulation")
        assert "modulation" in adapter.capabilities.unsupported_commands
    finally:
        adapter.disconnect()
