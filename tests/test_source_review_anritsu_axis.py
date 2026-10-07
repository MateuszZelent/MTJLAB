"""Current-buffer axis coherence and explicit fresh-sweep evidence."""
from unittest.mock import Mock

import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.domain.errors import DeviceError
from tests.helpers import simulation_settings


@pytest.fixture
def adapter():
    device = AnritsuAdapter(simulation_settings(), session_factory=SimulatedVisaFactory("anritsu"))
    device.connect()
    try:
        yield device
    finally:
        device.disconnect()


def test_front_panel_range_change_replaces_cached_axis(adapter):
    first = adapter.fetch_current_trace_fast()
    session = adapter._require_session()
    session.write("FREQ:STAR 2000000HZ")
    session.write("FREQ:STOP 20000000HZ")
    second = adapter.fetch_current_trace_fast()
    assert len(second.powers_dbm) == len(first.powers_dbm)
    assert second.frequencies_hz[0] == 2e6
    assert second.frequencies_hz[-1] == 20e6
    assert first.frequencies_hz != second.frequencies_hz
    third = adapter.fetch_current_trace_fast()
    assert third.frequencies_hz is second.frequencies_hz


def test_range_change_during_binary_transfer_is_rejected(adapter, monkeypatch):
    adapter.fetch_current_trace_fast()
    session = adapter._require_session()
    original = session.query_binary_values

    def transfer(*args, **kwargs):
        values = original(*args, **kwargs)
        session.write("FREQ:STOP 20000000HZ")
        return values

    monkeypatch.setattr(session, "query_binary_values", transfer)
    with pytest.raises(DeviceError, match="grid changed"):
        adapter.fetch_current_trace_fast()
    assert adapter._cached_grid is None


def test_fresh_acquisition_does_not_fallback_to_current_buffer(adapter, monkeypatch):
    failure = Mock(side_effect=DeviceError("single sweep failed"))
    fallback = Mock()
    monkeypatch.setattr(adapter, "acquire_single_sweep", failure)
    monkeypatch.setattr(adapter, "fetch_current_trace", fallback)
    with pytest.raises(DeviceError, match="single sweep failed"):
        adapter.acquire_fresh_trace(timeout_s=2)
    failure.assert_called_once_with("TRAC1", timeout_s=2)
    fallback.assert_not_called()


def test_fresh_acquisition_contains_sweep_identity(adapter):
    first = adapter.acquire_fresh_trace(timeout_s=2)
    second = adapter.acquire_fresh_trace(timeout_s=2)
    assert first.sweep_id and second.sweep_id != first.sweep_id
    assert first.acquisition_completed_at_utc >= first.acquisition_started_at_utc


def test_sweep_wait_uses_acquisition_budget_and_restores_io_timeout(adapter, monkeypatch):
    session = adapter._require_session()
    session.timeout = 10
    observed = []
    query = session.query

    def status(command):
        if command == "INIT:SWP?":
            observed.append(session.timeout)
            return "0"
        return query(command)

    monkeypatch.setattr(session, "query", status)
    adapter.wait_complete(deadline_s=2)
    assert len(observed) == 1 and 1000 < observed[0] <= 2000
    assert session.timeout == 10


def test_wait_failure_aborts_acquisition_without_selecting_signal_generator(adapter, monkeypatch):
    abort = Mock(return_value=True)
    emergency = Mock()
    monkeypatch.setattr(adapter, "abort_acquisition", abort)
    monkeypatch.setattr(adapter, "emergency_off", emergency)
    monkeypatch.setattr(adapter._require_session(), "query", Mock(side_effect=DeviceError("wait failed")))
    with pytest.raises(DeviceError, match="wait failed"):
        adapter.wait_complete(deadline_s=2)
    abort.assert_called_once()
    emergency.assert_not_called()
