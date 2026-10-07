"""Real command acceptance, bounded diagnostics and receiver-only traffic."""
import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError
from tests.helpers import simulation_settings


@pytest.fixture
def connected():
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        yield adapter, session
    finally:
        adapter.disconnect()


def test_live_reuses_binary_transfer_settings_and_keeps_grid_checks(connected):
    adapter, session = connected
    first = adapter.fetch_current_trace_fast()
    session.commands.clear()
    second = adapter.fetch_current_trace_fast()
    assert first.frequencies_hz == second.frequencies_hz
    assert not any(command.startswith(("FORM ", "FORM:BORD ", "INIT:", "INST ", "OUTP ")) for command in session.commands)
    assert session.commands.count("SWE:POIN?") == 2
    assert session.commands.count("FREQ:STAR?") == 2
    assert session.commands.count("FREQ:STOP?") == 2
    assert "FORM?" in session.commands and "FORM:BORD?" in session.commands


def test_binary_after_ascii_or_external_byte_order_change_reconfigures(connected):
    adapter, session = connected
    adapter.fetch_current_trace_fast()
    adapter.fetch_current_trace()
    session.write("FORM:BORD NORM")
    session.commands.clear()
    adapter.fetch_current_trace_fast()
    assert session.commands.count("FORM REAL,32") == 1
    assert session.commands.count("FORM:BORD SWAP") == 1


@pytest.mark.parametrize("command,operation", [
    ("TRAC1:TYPE", "live"), ("TRAC1:TYPE", "single"),
    ("INIT:MODE:SING", "single"), ("FORM REAL", "binary"),
    ("FORM:BORD", "binary"), ("FORM ASC", "ascii"),
])
def test_visa_write_success_does_not_hide_instrument_command_error(connected, command, operation):
    adapter, session = connected
    if command == "FORM:BORD":
        session.write("FORM:BORD NORM")
    if command == "FORM ASC":
        session.write("FORM REAL,32")
    session.command_errors[command] = '-113,"Undefined header; [' + command + ']"'
    call = {"live": lambda: adapter.start_live(True), "single": adapter.acquire_single_sweep,
            "binary": adapter.fetch_current_trace_fast, "ascii": adapter.fetch_current_trace}[operation]
    with pytest.raises(DeviceError, match="Undefined header"):
        call()
    assert "SYST:ERR?" in session.commands
    if command == "TRAC1:TYPE":
        assert "INIT:MODE:SING" not in session.commands
        assert "TRAC? TRAC1" not in session.commands


def test_old_errors_are_reported_before_new_measurement_without_cls(connected):
    adapter, session = connected
    session.error_queue.append('-113,"Old command failed"')
    session.commands.clear()
    with pytest.raises(DeviceError, match="before single-sweep preparation.*Old command failed"):
        adapter.acquire_single_sweep()
    assert "*CLS" not in session.commands
    assert "INIT:MODE:SING" not in session.commands


@pytest.mark.parametrize("response", ["not a SCPI error", '1,"Vendor event"'])
def test_bad_or_nonempty_error_queue_fails_closed_and_is_bounded(connected, monkeypatch, response):
    adapter, session = connected
    original_query = session.query
    checks = []
    def query(command):
        if command == "SYST:ERR?":
            checks.append(command)
            return response
        return original_query(command)
    monkeypatch.setattr(session, "query", query)
    with pytest.raises(DeviceError, match="SYST:ERR|did not empty"):
        adapter.fetch_current_trace_fast()
    assert len(checks) <= 16


def test_recipe_single_keeps_hold_between_points_without_full_configuration(connected):
    adapter, session = connected
    session.commands.clear()
    traces = [adapter.acquire_single_sweep(restore_continuous=False) for _ in range(3)]
    assert len({trace.sweep_id for trace in traces}) == 3
    assert not session.continuous_sweep
    assert session.commands.count("INIT:MODE:SING") == 3
    assert "INIT:MODE:CONT" not in session.commands
    assert not any(command.startswith(("INST ", "OUTP ", "FREQ:", "BAND", "DET ", "POW:", "SWE:POIN ")) and "?" not in command for command in session.commands)


def test_gpib_remote_is_entered_once_at_acquisition_not_discovery():
    settings = simulation_settings()
    raw = settings.model_dump(mode="python")
    raw["devices"]["anritsu"]["connection"]["resource"] = "GPIB0::23::INSTR"
    settings = type(settings).model_validate(raw)
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        assert "VISA REN ASSERT_ADDRESS" not in session.commands
        for _ in range(3):
            adapter.acquire_single_sweep(restore_continuous=False)
        assert session.commands.count("VISA REN ASSERT_ADDRESS") == 1
        adapter.disconnect()
        session = AnritsuSimulator()
        adapter._factory = FakeVisaSessionFactory(session)
        adapter.connect()
        adapter.start_live(True)
        assert session.commands.count("VISA REN ASSERT_ADDRESS") == 1
    finally:
        adapter.disconnect()


def test_managed_transport_enters_remote_without_local_lockout():
    from unittest.mock import Mock
    import pyvisa
    from app.devices.visa import _ManagedVisaSession
    raw = Mock()
    events = []
    session = _ManagedVisaSession(raw, None, events.append)
    session.ensure_remote()
    raw.control_ren.assert_called_once_with(pyvisa.constants.RENLineOperation.asrt_address)
    assert "Remote" in events[0]


def test_remote_failure_does_not_start_measurement():
    from unittest.mock import Mock
    settings = simulation_settings()
    raw = settings.model_dump(mode="python")
    raw["devices"]["anritsu"]["connection"]["resource"] = "GPIB0::23::INSTR"
    settings = type(settings).model_validate(raw)
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.ensure_remote = Mock(side_effect=DeviceError("REN unavailable"))
    try:
        with pytest.raises(DeviceError, match="REN unavailable"):
            adapter.acquire_single_sweep()
        assert "INIT:MODE:SING" not in session.commands
    finally:
        adapter.disconnect()


def test_ignored_binary_format_write_prevents_trace_read(connected, monkeypatch):
    adapter, session = connected
    original_write = session.write
    monkeypatch.setattr(session, "write", lambda command: None if command == "FORM REAL,32" else original_write(command))
    with pytest.raises(DeviceError, match="did not confirm REAL,32"):
        adapter.fetch_current_trace_fast()
    assert "TRAC? TRAC1" not in session.commands


def test_ascii_transfer_is_prepared_only_when_format_changes(connected):
    adapter, session = connected
    adapter.fetch_current_trace_fast()
    session.commands.clear()
    adapter.acquire_single_sweep(restore_continuous=False)
    assert session.commands.count("FORM ASC") == 1
    session.commands.clear()
    adapter.acquire_single_sweep(restore_continuous=False)
    assert "FORM ASC" not in session.commands
    assert session.commands.count("TRAC? TRAC1") == 1
    assert session.commands.count("FREQ:STAR?") == 2
    assert session.commands.count("FREQ:STOP?") == 2


def test_ignored_ascii_format_write_prevents_trace_read(connected, monkeypatch):
    adapter, session = connected
    session.write("FORM REAL,32")
    original_write = session.write
    monkeypatch.setattr(session, "write", lambda command: None if command == "FORM ASC" else original_write(command))
    with pytest.raises(DeviceError, match="did not confirm ASCII"):
        adapter.fetch_current_trace()
    assert "TRAC? TRAC1" not in session.commands


def test_acquisition_configuration_has_no_duplicate_queries_or_writes(connected):
    adapter, session = connected
    session.commands.clear()
    full, advanced = adapter.read_acquisition_configuration()
    assert all(command.endswith("?") for command in session.commands)
    assert len(session.commands) == len(set(session.commands))
    assert advanced == adapter.read_advanced_spectrum_configuration()
    assert full == adapter.read_full_configuration()
    session.write("POW:ATT 20DB")
    _full, changed = adapter.read_acquisition_configuration()
    assert changed.attenuation_db == 20


def test_background_and_worker_single_sweep_do_not_restart_continuous(connected):
    from app.devices.anritsu_ms2830a.module import MODULE
    from app.ui.workers import InstrumentWorker
    adapter, session = connected
    session.commands.clear()
    MODULE.dispatch(adapter, "single_sweep", "TRAC1")
    worker = InstrumentWorker(adapter)
    worker._dispatch("single_sweep", "TRAC1")
    assert session.commands.count("INIT:MODE:SING") == 2
    assert "INIT:MODE:CONT" not in session.commands
