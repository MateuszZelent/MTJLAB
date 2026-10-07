"""Regression for headers actually rejected by the station MS2830A."""
import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.configuration import SpectrumConfig
from app.devices.anritsu_ms2830a.hardware import parse_anritsu_hardware_catalog
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError
from tests.helpers import simulation_settings


@pytest.mark.parametrize("language,header", [
    ("SCPI", "SYST:HARD:OPT:CAT?"), ("NAT", None),
])
def test_connection_reads_documented_catalogue_without_mutations(language, header):
    session = AnritsuSimulator()
    session.remote_language = language
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    try:
        adapter.connect()
        assert session.commands == ["*IDN?", "SYST:LANG?"] + ([header] if header else [])
        assert adapter.capabilities.hardware_options == (("041", "008", "020") if header else ())
        assert "021" not in adapter.capabilities.hardware_options
        assert adapter._options_query_failed is (header is None)
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("operation", ["single", "live", "apply"])
def test_native_mode_uses_documented_index_argument_without_changing_language(operation):
    session = AnritsuSimulator()
    session.remote_language = "NAT"
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    calls = {
        "single": lambda: adapter.acquire_single_sweep(restore_continuous=False),
        "live": lambda: adapter.start_live(ensure_continuous=True),
        "apply": lambda: adapter.configure_spectrum(SpectrumConfig(1e6, 10e6, 0, 1001)),
    }
    try:
        calls[operation]()
        assert "SYST:LANG?" in session.commands
        assert "TRAC:TYPE 1,WRIT" in session.commands
        assert not any(command.startswith(("TRAC1:TYPE", "SYST:LANG ", "OUTP ", "INST SG")) for command in session.commands)
        assert session.error_queue == []
    finally:
        adapter.disconnect()


def test_language_change_after_connection_is_checked_before_next_sweep():
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        adapter.acquire_single_sweep(restore_continuous=False)
        session.remote_language = "NAT"
        session.commands.clear()
        adapter.acquire_single_sweep(restore_continuous=False)
        assert "INIT:MODE:SING" in session.commands
        assert "TRAC? TRAC1" in session.commands
        assert "TRAC:TYPE 1,WRIT" in session.commands
        assert "TRAC1:TYPE WRIT" not in session.commands
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("response,native,expected", [
    ('0', False, ()),
    ('3,41,ON,"Frequency, 6 GHz",008,OFF,Preamp,020,1,Generator', False, ("041", "020")),
    ('041,ON,Frequency,008,OFF,Preamp', True, ("041",)),
    ('2,041,ON,Frequency,008,OFF,Preamp', True, ("041",)),
])
def test_catalogue_switches_counts_and_csv_names(response, native, expected):
    assert parse_anritsu_hardware_catalog(response, native=native) == expected


@pytest.mark.parametrize("response", [
    "", "041,008", "2,041,ON,Frequency", "1,041,MAYBE,Frequency", "1,1000,ON,Frequency",
])
def test_malformed_catalogue_cannot_grant_capabilities(response):
    with pytest.raises(ValueError):
        parse_anritsu_hardware_catalog(response)


def test_required_option_marked_off_blocks_connection(monkeypatch):
    settings = simulation_settings()
    raw = settings.model_dump(mode="python")
    raw["devices"]["anritsu"]["identity"]["required_options"] = ["008"]
    session = AnritsuSimulator()
    query = session.query
    monkeypatch.setattr(session, "query", lambda command: '2,041,ON,Frequency,008,OFF,Preamp'
                        if command == "SYST:HARD:OPT:CAT?" else query(command))
    adapter = AnritsuAdapter(type(settings).model_validate(raw), session_factory=FakeVisaSessionFactory(session))
    with pytest.raises(DeviceError, match="missing profile-required.*008"):
        adapter.connect()
    assert not adapter.connected
    assert session.closed


def test_simulator_rejects_obsolete_options_header():
    with pytest.raises(DeviceError, match="unsupported query"):
        AnritsuSimulator().query("*OPT?")


def test_native_required_options_are_unknown_and_connection_fails_closed():
    settings = simulation_settings()
    raw = settings.model_dump(mode="python")
    raw["devices"]["anritsu"]["identity"]["required_options"] = ["008"]
    session = AnritsuSimulator()
    session.remote_language = "NAT"
    adapter = AnritsuAdapter(type(settings).model_validate(raw), session_factory=FakeVisaSessionFactory(session))
    with pytest.raises(DeviceError, match="Cannot verify profile-required"):
        adapter.connect()
    assert session.commands == ["*IDN?", "SYST:LANG?"]
    assert not adapter.connected
    assert session.closed


def test_native_trace_control_rejection_does_not_start_or_fetch_a_sweep():
    session = AnritsuSimulator()
    session.remote_language = "NAT"
    session.command_errors["TRAC:TYPE"] = '-113,"Undefined header;TRAC:TYPE 1,WRIT"'
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    try:
        with pytest.raises(DeviceError, match="Undefined header"):
            adapter.acquire_single_sweep(restore_continuous=False)
        assert "INIT:MODE:SING" not in session.commands
        assert "TRAC? TRAC1" not in session.commands
    finally:
        adapter.disconnect()
