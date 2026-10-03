"""Qualified Model 475 diagnostics, fresh conversions and bounded read-only I/O."""

from threading import Event
from types import SimpleNamespace

import pytest

from app.devices.lakeshore_475.adapter import LakeShore475Adapter
from app.devices.lakeshore_475.models import GaussmeterConfig
from app.devices.lakeshore_475.simulator import simulated_475_session
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, RunInterrupted
from app.domain.models import DeviceState


@pytest.fixture
def reference(monkeypatch):
    clock = [10.0]

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr("app.devices.lakeshore_475.adapter.time",
                        SimpleNamespace(monotonic=lambda: clock[0], sleep=advance))
    session = simulated_475_session(field=.02)
    adapter = LakeShore475Adapter(
        GaussmeterConfig("SIM::LAKESHORE"), session_factory=FakeVisaSessionFactory(session),
        official_model_factory=lambda connection: connection)
    adapter.connect()
    session.writes.clear()
    return adapter, session, clock


@pytest.mark.parametrize("status,message", [(1, "no probe"), (2, "overload"),
                                           (8, "alarm"), (64, "calibration error"),
                                           (-1, "Invalid"), (128, "Invalid"), ("NaN", "Invalid")])
def test_invalid_instrument_status_never_returns_a_field(reference, status, message):
    adapter, session, _ = reference
    session.responses["OPST?"] = str(status)
    with pytest.raises(DeviceError, match=message):
        adapter.read_measurement()
    assert "RDGFIELD?" not in session.writes
    assert adapter.state is DeviceState.FAULT


def test_diagnostic_error_after_numeric_reply_discards_measurement(reference):
    adapter, session, _ = reference

    def fault_after_field(command):
        session.responses["OPST?"] = "2"
        return ".02"

    session.responses["RDGFIELD?"] = fault_after_field
    with pytest.raises(DeviceError, match="overload"):
        adapter.read_measurement()
    assert "RDGFIELD?" in session.writes


def test_fresh_acquisition_drains_old_events_and_waits_for_new_conversion(reference):
    adapter, session, clock = reference
    events = iter(["4", "0", "4"])
    session.responses["OPSTR?"] = lambda command: next(events)
    reading = adapter.read_measurement(require_fresh=True, deadline_s=clock[0] + 3)
    assert reading.field_t == .02
    assert reading.operation_event_code == 4
    assert session.writes.count("OPSTR?") == 3
    assert session.writes.index("RDGFIELD?") > max(i for i, command in enumerate(session.writes) if command == "OPSTR?")
    assert all(command.endswith("?") for command in session.writes)
    assert session.timeout == 1000


def test_no_new_conversion_times_out_without_reading_stale_field(reference):
    adapter, session, clock = reference
    session.responses["OPSTR?"] = "0"
    with pytest.raises(TimeoutError):
        adapter.read_measurement(require_fresh=True, deadline_s=clock[0] + .3)
    assert "RDGFIELD?" not in session.writes
    assert session.timeout == 1000
    assert adapter.state is DeviceState.FAULT


def test_whole_operation_reduces_each_query_timeout_and_discards_late_reply(reference):
    adapter, session, clock = reference
    seen = []

    def delayed_status(command):
        seen.append(session.timeout)
        clock[0] += .11
        return "0"

    session.responses["OPST?"] = delayed_status
    with pytest.raises(TimeoutError):
        with adapter.io_timeout(.2):
            adapter.read_measurement()
    assert seen and max(seen) <= 200
    assert len(seen) > 1 and seen[0] > seen[-1] > 0
    assert session.timeout == 1000
    assert "RDGFIELD?" not in session.writes


def test_stop_interrupts_fresh_conversion_polling(reference):
    adapter, session, clock = reference
    cancel = Event()

    def stop(command):
        cancel.set()
        return "0"

    session.responses["OPSTR?"] = stop
    with pytest.raises(RunInterrupted):
        adapter.read_measurement(require_fresh=True, deadline_s=clock[0] + 3, cancel=cancel)
    assert "RDGFIELD?" not in session.writes
    assert session.timeout == 1000
    assert adapter.state is DeviceState.VERIFIED


def test_expired_deadline_sends_no_query(reference):
    adapter, session, clock = reference
    with pytest.raises(TimeoutError):
        adapter.read_snapshot(deadline_s=clock[0] - 1)
    assert not session.writes
