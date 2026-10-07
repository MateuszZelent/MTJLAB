"""Protocol failures must not be hidden by permissive simulator defaults."""

import pytest

from app.bootstrap import StationComposition
from app.domain.errors import DeviceError
from tests.test_sweep_audit_contracts import audit_settings, compile_source


@pytest.fixture
def analyzer(tmp_path):
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        yield adapter
    finally:
        adapter.disconnect()


def test_full_readback_does_not_probe_redundant_center_span(analyzer, monkeypatch):
    session = analyzer._require_session()
    query = session.query
    calls = []
    def hardware_query(command):
        calls.append(command)
        if command in {"FREQ:CENT?", "FREQ:SPAN?"}:
            raise DeviceError("Injected unsupported query timeout")
        return query(command)
    monkeypatch.setattr(session, "query", hardware_query)
    actual = analyzer.read_full_configuration()
    assert actual.center_hz == (actual.start_hz + actual.stop_hz) / 2
    assert actual.span_hz == actual.stop_hz - actual.start_hz
    assert not {"FREQ:CENT?", "FREQ:SPAN?"}.intersection(calls)


def test_wrong_application_stops_before_spectrum_queries(analyzer, monkeypatch):
    calls = []
    def wrong_application(command):
        calls.append(command)
        if command == "INST?":
            return "SG"
        raise AssertionError("Spectrum query sent in wrong application")
    with monkeypatch.context() as patch:
        patch.setattr(analyzer._require_session(), "query", wrong_application)
        with pytest.raises(DeviceError, match="Spectrum Analyzer mode"):
            analyzer.read_full_configuration()
    assert calls == ["INST?"]


@pytest.mark.parametrize("failed_query", ["FREQ:STAR?", "SWE:POIN?", "BAND?",
    "BAND:VID:MODE?", "BAND:VID?", "POW:ATT?", "DET?", "AVER:COUN?"])
def test_required_query_timeout_stops_without_fabricated_metadata(analyzer, monkeypatch, failed_query):
    session = analyzer._require_session()
    query = session.query
    calls = []
    def injected(command):
        calls.append(command)
        if command == failed_query:
            raise DeviceError(f"Injected timeout: {command}")
        return query(command)
    monkeypatch.setattr(session, "query", injected)
    with pytest.raises(DeviceError, match="Injected timeout"):
        analyzer.read_full_configuration()
    assert calls[-1] == failed_query


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("waveform", ["DC", "NOIS"])
def test_rigol_nonperiodic_configuration_does_not_query_phase(tmp_path, monkeypatch, channel, waveform):
    from app.settings.models import StationSettings
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["channels"][str(channel)]["enabled"] = True
    settings = StationSettings.model_validate(raw)
    low = "1 mV" if waveform == "DC" else "-1 mV"
    plan = compile_source(settings, f"    - {{id: carrier, type: configure_rigol, channel: {channel}, waveform: {waveform}, frequency: '1 kHz', high_level: '1 mV', low_level: '{low}'}}\n")
    adapter = StationComposition(settings, simulation=True).create_adapter("rigol")
    adapter.connect()
    try:
        session = adapter._require_session()
        query = session.query
        calls = []
        def hardware_query(command):
            calls.append(command)
            if command == f":SOUR{channel}:PHAS?":
                raise DeviceError("Phase is unavailable for this waveform")
            return query(command)
        monkeypatch.setattr(session, "query", hardware_query)
        adapter.configure_channel(plan.actions[0].payload["config"])
        assert adapter.last_channel_config(channel).waveform == waveform
        assert f":SOUR{channel}:PHAS?" not in calls
    finally:
        adapter.disconnect()
