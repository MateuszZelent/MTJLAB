"""Real command acceptance, bounded diagnostics and receiver-only traffic."""
from dataclasses import replace

import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError
from app.domain.models import DeviceState
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


class NativeGroupedSimulator(AnritsuSimulator):
    def _query(self, command):
        if command == "*IDN?":
            return "ANRITSU,MS2830A,SIM000001,7.03.00"
        return super()._query(command)

    def query(self, command):
        if ";" not in command:
            return super().query(command)
        query_single = super().query
        answers = [query_single(part) for part in command.split(";")]
        del self.commands[-len(answers):]
        self.commands.append(command)
        return ";".join(answers)


@pytest.fixture
def native_gpib():
    baseline = simulation_settings()
    raw = baseline.model_dump(mode="python")
    raw["devices"]["anritsu"]["connection"]["resource"] = "GPIB0::23::INSTR"
    settings = type(baseline).model_validate(raw)
    session = NativeGroupedSimulator()
    session.remote_language = "NAT"
    session.trace_format = "REAL,32"
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        yield adapter, session
    finally:
        adapter.disconnect()


def test_grouped_live_checks_both_sides_of_binary_read_in_three_transactions(native_gpib):
    adapter, session = native_gpib
    session.trace_storage_modes[:2] = ["AVER", "LAV"]
    adapter.start_live(True)
    assert session.trace_storage_modes[:2] == ["OFF", "LAV"]
    session.commands.clear()

    trace = adapter.fetch_current_trace_fast()

    assert len(trace.powers_dbm) == len(trace.frequencies_hz) == session.points
    assert trace.frequencies_hz[0] == session.start_hz
    assert trace.frequencies_hz[-1] == session.stop_hz
    assert len(session.commands) == 3
    before, binary, after = session.commands
    assert binary == "TRAC? TRAC1"
    for group in (before, after):
        assert set(group.split(";")) == {
            "SWE:POIN?", "FREQ:STAR?", "FREQ:STOP?", "FORM?", "FORM:BORD?",
            "TRAC:STOR:MODE? 1", "DISP:WIND:TRAC:Y:SPAC?", "SYST:ERR?",
        }

    adapter.stop_live()
    assert session.trace_storage_modes[:2] == ["AVER", "LAV"]
    session.commands.clear()
    adapter.fetch_current_trace_fast()
    assert all(";" not in command for command in session.commands)


@pytest.mark.parametrize("transfer", ["fetch_current_trace", "fetch_current_trace_fast"])
def test_trace_requires_log_scale_for_dbm(connected, transfer):
    adapter, session = connected
    session.scale_mode = "LIN"

    with pytest.raises(DeviceError, match="Log scale.*dBm"):
        getattr(adapter, transfer)()
    assert "TRAC? TRAC1" not in session.commands


def test_grouped_live_rejects_linear_scale_changed_while_reading(native_gpib, monkeypatch):
    adapter, session = native_gpib
    adapter.start_live(True)
    read = session.query_binary_values

    def change_scale(command, **options):
        values = read(command, **options)
        session.scale_mode = "LIN"
        return values

    monkeypatch.setattr(session, "query_binary_values", change_scale)
    with pytest.raises(DeviceError, match="Log scale.*dBm"):
        adapter.fetch_current_trace_fast()


@pytest.mark.parametrize("finish", ["abort_acquisition", "disconnect"])
def test_raw_live_restores_trace_mode_on_transport_teardown(connected, finish):
    adapter, session = connected
    session.trace_storage_modes[0] = "AVER"
    adapter.start_live(True)
    assert session.trace_storage_modes[0] == "OFF"

    getattr(adapter, finish)()

    assert session.trace_storage_modes[0] == "AVER"
    assert not adapter.live


def test_failed_raw_live_start_restores_the_previous_trace_mode(connected):
    adapter, session = connected
    session.continuous_sweep = False
    session.trace_storage_modes[0] = "AVER"
    session.command_errors["INIT:MODE:CONT"] = '-200,"cannot start continuous sweep"'

    with pytest.raises(DeviceError, match="cannot start continuous sweep"):
        adapter.start_live(True)

    assert session.trace_storage_modes[0] == "AVER"
    assert not adapter.live


@pytest.mark.parametrize("change,expected", [
    ("axis", "frequency grid changed"),
    ("format", "transfer format changed"),
    ("storage", "storage mode changed"),
    ("error", "capture failed; instrument diagnostic"),
])
def test_grouped_live_discards_frame_if_context_changes(native_gpib, monkeypatch, change, expected):
    adapter, session = native_gpib
    adapter.start_live(True)
    read_binary = session.query_binary_values

    def read_with_change(*args, **kwargs):
        data = read_binary(*args, **kwargs)
        if change == "axis":
            session.start_hz += 100
        elif change == "format":
            session.byte_order = "NORM"
        elif change == "storage":
            session.trace_storage_modes[0] = "AVER"
        else:
            session.error_queue.append('-200,"capture failed; instrument diagnostic"')
        return data

    monkeypatch.setattr(session, "query_binary_values", read_with_change)
    with pytest.raises(DeviceError, match=expected):
        adapter.fetch_current_trace_fast()


def test_grouped_queries_are_not_used_for_unqualified_firmware(native_gpib):
    adapter, session = native_gpib
    adapter._identity = replace(adapter._identity, firmware="7.02.00")
    adapter.start_live(True)
    session.commands.clear()
    adapter.fetch_current_trace_fast()
    assert all(";" not in command for command in session.commands)


def test_grouped_live_reconfirms_binary_format_after_an_ascii_read(native_gpib):
    adapter, session = native_gpib
    adapter.start_live(True)
    adapter.fetch_current_trace()
    session.write("FORM:BORD NORM")
    session.commands.clear()

    trace = adapter.fetch_current_trace_fast()

    assert len(trace.powers_dbm) == session.points
    assert session.commands.index("FORM REAL,32") < session.commands.index("FORM?") < session.commands.index("TRAC? TRAC1")
    assert session.commands.index("FORM:BORD SWAP") < session.commands.index("FORM:BORD?") < session.commands.index("TRAC? TRAC1")


def test_grouped_live_rejects_incomplete_metadata_before_binary_read(native_gpib, monkeypatch):
    adapter, session = native_gpib
    adapter.start_live(True)
    query = session.query
    monkeypatch.setattr(session, "query", lambda command: "10001;123" if ";" in command else query(command))
    session.commands.clear()

    with pytest.raises(DeviceError, match="invalid grouped Live readback"):
        adapter.fetch_current_trace_fast()

    assert "TRAC? TRAC1" not in session.commands
    assert "FORM REAL,32" not in session.commands


def test_binary_after_ascii_or_external_byte_order_change_reconfigures(connected):
    adapter, session = connected
    adapter.fetch_current_trace_fast()
    adapter.fetch_current_trace()
    session.write("FORM:BORD NORM")
    session.commands.clear()
    adapter.fetch_current_trace_fast()
    assert session.commands.count("FORM REAL,32") == 1
    assert session.commands.count("FORM:BORD SWAP") == 1


@pytest.mark.parametrize("fixture_name,prefix", [
    ("connected", "TRAC1:STOR:MODE "),
    ("native_gpib", "TRAC:STOR:MODE 1,"),
])
def test_application_average_uses_one_unaveraged_sweep_per_request(request, fixture_name, prefix):
    from app.devices.anritsu_ms2830a.module import MODULE

    adapter, session = request.getfixturevalue(fixture_name)
    other_prefix = prefix.replace("1", "2")
    session.write(prefix + "AVER")
    session.write(other_prefix + "LAV")
    session.write("AVER:COUN 50")
    session.commands.clear()

    traces = [MODULE.dispatch(adapter, "unaveraged_sweep", "TRAC1") for _ in range(3)]

    assert len({trace.sweep_id for trace in traces}) == 3
    relevant = [command for command in session.commands
                if command.startswith((prefix, other_prefix)) or command in {"INIT:MODE:SING", "TRAC? TRAC1"}]
    assert relevant == [
        prefix + "OFF", other_prefix + "OFF", "INIT:MODE:SING", "TRAC? TRAC1",
        other_prefix + "LAV", prefix + "AVER",
    ] * 3
    assert session.average_count == 50
    assert session.trace_storage_modes[:2] == ["AVER", "LAV"]


def test_unaveraged_sweep_restores_storage_mode_on_failed_read(connected, monkeypatch):
    adapter, session = connected
    session.trace_storage_modes[:2] = ["LAV", "AVER"]

    def failed_read(_trace):
        raise DeviceError("trace transfer failed")

    monkeypatch.setattr(adapter, "fetch_trace", failed_read)
    with pytest.raises(DeviceError, match="trace transfer failed"):
        adapter.acquire_unaveraged_sweep()
    assert session.trace_storage_modes[:2] == ["LAV", "AVER"]


def test_unaveraged_sweep_requires_off_readback_before_trigger(connected):
    adapter, session = connected
    session.trace_storage_modes[0] = "AVER"
    session.command_errors["TRAC1:STOR:MODE OFF"] = '-200,"storage change rejected"'
    with pytest.raises(DeviceError, match="storage change rejected"):
        adapter.acquire_unaveraged_sweep()
    assert "INIT:MODE:SING" not in session.commands
    assert session.trace_storage_modes[0] == "AVER"


def test_unaveraged_sweep_attempts_remaining_restorations_after_a_failed_write(connected, monkeypatch):
    adapter, session = connected
    session.trace_storage_modes[:2] = ["AVER", "LAV"]
    write = session.write

    def fail_trace_b_restoration(command):
        if command == "TRAC2:STOR:MODE LAV":
            raise DeviceError("Trace B restoration failed")
        return write(command)

    monkeypatch.setattr(session, "write", fail_trace_b_restoration)
    with pytest.raises(DeviceError, match="storage restoration could not be confirmed"):
        adapter.acquire_unaveraged_sweep()
    assert session.trace_storage_modes[:2] == ["AVER", "OFF"]
    assert adapter.state == DeviceState.UNKNOWN


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
