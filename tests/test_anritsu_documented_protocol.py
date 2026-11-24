"""Regression for headers actually rejected by the station MS2830A."""
import pytest

from app.devices.anritsu_ms2830a import MODULE, AnritsuAdapter, InstrumentAverageResult, InstrumentAverageTarget
from app.devices.anritsu_ms2830a.configuration import SpectrumConfig
from app.devices.anritsu_ms2830a.hardware import parse_anritsu_hardware_catalog
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.models import DeviceState
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


@pytest.mark.parametrize("language,header", [
    ("SCPI", "TRAC1:STOR:MODE "), ("NAT", "TRAC:STOR:MODE 1,"),
])
def test_instrument_average_triggers_once_fetches_once_and_restores_settings(language, header):
    session = AnritsuSimulator()
    session.remote_language = language
    session.sweep_time_s = .039
    session.trace_storage_modes[0] = "AVER"
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    try:
        trace, count, sweep_time = adapter.acquire_instrument_average(5.0, acquisition_timeout_s=30.0)
        assert count == 129 and sweep_time == .039
        assert len(trace.powers_dbm) == session.points
        commands = session.commands
        assert commands.count("INIT:MODE:SING") == commands.count("TRAC? TRAC1") == 1
        assert commands.index(header + "LAV") < commands.index("INIT:MODE:SING")
        assert commands.index("AVER:COUN 129") < commands.index("INIT:MODE:SING")
        assert commands.index("TRAC? TRAC1") < commands.index(header + "AVER")
        assert session.trace_storage_modes[0] == "AVER" and session.average_count == 10
        assert not any(command.startswith(("FREQ:STAR ", "FREQ:STOP ", "SWE:TIME ", "OUTP ")) for command in commands)
    finally:
        adapter.disconnect()


def test_instrument_average_restores_settings_after_failed_transfer(monkeypatch):
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        def failed_fetch(_trace):
            raise DeviceError("test transfer failure")

        monkeypatch.setattr(adapter, "fetch_trace", failed_fetch)
        with pytest.raises(DeviceError, match="test transfer failure"):
            adapter.acquire_instrument_average(5.0, acquisition_timeout_s=30.0)
        assert session.trace_storage_modes[0] == "OFF" and session.average_count == 10
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("language,counter_query", [("SCPI", "TRAC:SWE:COUN?"), ("NAT", "SWEEPCOUNT?")])
def test_instrument_average_accepts_a_fixed_count(language, counter_query):
    session = AnritsuSimulator()
    session.remote_language = language
    session.sweep_time_s = .006
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        trace, count, sweep_time_s = adapter.acquire_instrument_average(sweep_count=50, acquisition_timeout_s=30.)
        assert count == session.completed_sweep_count == 50
        assert sweep_time_s == .006
        assert trace.trace_name == "TRAC1"
        assert session.commands.index("INIT:MODE:SING") < session.commands.index("*WAI")
        assert session.commands.index("*WAI") < session.commands.index("INIT:SWP?") < session.commands.index(counter_query)
        assert not session.continuous_sweep  # Recipe checkpoints keep Single between points.
    finally:
        adapter.disconnect()


def test_instrument_average_duration_uses_the_smallest_count_covering_nominal_time():
    session = AnritsuSimulator()
    session.sweep_time_s = .01
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        _, exact_count, _ = adapter.acquire_instrument_average(.07, acquisition_timeout_s=30.)
        _, rounded_count, _ = adapter.acquire_instrument_average(.071, acquisition_timeout_s=30.)
        assert (exact_count, rounded_count) == (7, 8)
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("target", [
    {}, {"duration_s": 1., "sweep_count": 5}, {"duration_s": False}, {"duration_s": float("nan")},
    {"sweep_count": 1}, {"sweep_count": 10000}, {"sweep_count": 2.5}, {"sweep_count": True},
    {"duration_s": 30.},
])
def test_invalid_instrument_average_targets_do_not_reach_the_instrument(target):
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    try:
        with pytest.raises(SafetyViolation):
            adapter.acquire_instrument_average(**target, acquisition_timeout_s=30.)
        assert session.commands == []
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("operation,continuous", [("instrument_average", False), ("instrument_average_live", True)])
def test_manual_instrument_average_restores_native_front_panel_settings(operation, continuous):
    session = AnritsuSimulator()
    session.remote_language = "NAT"
    session.trace_storage_modes[0] = "MAXH"
    session.trace_write_mode = "BLAN"
    session.trace_format = "REAL,32"
    session.byte_order = "NORM"
    session.continuous_sweep = continuous
    session.sweep_time_s = .006
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    try:
        target = InstrumentAverageTarget(frame_count=50) if continuous else InstrumentAverageTarget(duration_s=.3)
        result = MODULE.dispatch(adapter, operation, target)
        assert isinstance(result, InstrumentAverageResult)
        assert result.count == 50
        assert result.nominal_sweep_time_s == .006
        assert result.trace.trace_name == "TRAC1_ANRITSU_LAV50"
        assert result.trace.acquisition_started_at_utc <= result.trace.acquisition_completed_at_utc
        assert (session.remote_language, session.trace_format, session.byte_order) == ("NAT", "REAL,32", "NORM")
        assert (session.trace_write_mode, session.trace_storage_modes[0], session.average_count) == ("BLAN", "MAXH", 10)
        assert session.continuous_sweep is continuous
        assert session.commands.count("TRAC? TRAC1") == session.commands.count("INIT:MODE:SING") == 1
        assert session.commands.count("TRAC:TYPE? 1") >= 2
        assert not any(command.startswith(("OUTP ", "INST ", "SYST:LANG ")) for command in session.commands)
    finally:
        adapter.disconnect()


def test_manual_instrument_average_can_suspend_and_resume_raw_live(monkeypatch):
    session = AnritsuSimulator()
    session.trace_storage_modes[0] = "AVER"
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        adapter.start_live(ensure_continuous=True)
        fetched = adapter.fetch_current_trace_fast()
        fetch_trace = adapter.fetch_trace

        def fetch_average(trace):
            assert not adapter.live
            assert session.trace_storage_modes[0] == "LAV"
            return fetch_trace(trace)

        monkeypatch.setattr(adapter, "fetch_trace", fetch_average)
        result = MODULE.dispatch(adapter, "instrument_average", InstrumentAverageTarget(frame_count=7))
        assert result.trace.frequencies_hz == fetched.frequencies_hz
        assert adapter.live and session.continuous_sweep
        assert session.trace_format == "REAL,32" and session.trace_storage_modes[0] == "OFF"
        assert adapter.fetch_current_trace_fast().trace_name == "TRAC1"
        adapter.stop_live()
        assert session.trace_storage_modes[0] == "AVER"
    finally:
        adapter.disconnect()


def test_incomplete_native_average_is_rejected_and_live_resumes(monkeypatch):
    session = AnritsuSimulator()
    session.remote_language = "NAT"
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        adapter.start_live(ensure_continuous=True)
        original_query = session.query
        monkeypatch.setattr(session, "query", lambda command: "49" if command == "SWEEPCOUNT?" else original_query(command))
        with pytest.raises(DeviceError, match="completed 49 of 50 requested sweeps"):
            MODULE.dispatch(adapter, "instrument_average", InstrumentAverageTarget(frame_count=50))
        assert adapter.live and session.continuous_sweep
        assert session.trace_storage_modes[0] == "OFF" and session.average_count == 10
    finally:
        adapter.disconnect()


def test_unconfirmed_restoration_marks_state_unknown_without_resuming_live(monkeypatch):
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        adapter.start_live(ensure_continuous=True)
        original_write = session.write

        def discard_restore(command):
            if command != "AVER:COUN 10":
                original_write(command)

        monkeypatch.setattr(session, "write", discard_restore)
        with pytest.raises(DeviceError, match="could not be restored"):
            MODULE.dispatch(adapter, "instrument_average", InstrumentAverageTarget(frame_count=50))
        assert adapter.state == DeviceState.UNKNOWN and not adapter.live
        assert not session.continuous_sweep
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("confirmed_abort", [True, False])
def test_instrument_average_timeout_confirms_abort_before_resuming_raw_live(monkeypatch, confirmed_abort):
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        adapter.start_live(ensure_continuous=True)
        session.commands.clear()
        original_query = session.query
        normal_timeout_ms = session.timeout

        def time_out(command):
            if command == "INIT:SWP?":
                if "ABOR" not in session.commands:
                    assert session.timeout > normal_timeout_ms
                    raise DeviceError("test VISA sweep timeout")
                assert session.timeout == normal_timeout_ms
                if not confirmed_abort:
                    return "1"
            return original_query(command)

        monkeypatch.setattr(session, "query", time_out)
        with pytest.raises(DeviceError, match="test VISA sweep timeout") as error:
            MODULE.dispatch(adapter, "instrument_average", InstrumentAverageTarget(frame_count=50))
        assert session.commands.count("ABOR") == 1
        assert "TRAC? TRAC1" not in session.commands
        assert session.trace_storage_modes[0] == "OFF" and session.average_count == 10
        assert adapter.live is confirmed_abort and session.continuous_sweep is confirmed_abort
        if not confirmed_abort:
            assert adapter.state == DeviceState.UNKNOWN
            assert "could not confirm acquisition stopped" in str(error.value)
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
