"""Shutdown requires output evidence, not merely instrument identification."""

from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import contextmanager

import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import ExecutionError
from app.domain.models import DeviceState
from app.engine.runner import RecipeRunner
from tests.helpers import simulation_settings
from app.ui import run_worker


@pytest.mark.parametrize("name", ["rigol", "keithley", "anritsu"])
@pytest.mark.parametrize("result,state,accepted", [
    (None, DeviceState.VERIFIED, False),
    (False, DeviceState.UNKNOWN, False),
    (False, DeviceState.OUTPUT_OFF, False),
    (None, DeviceState.OUTPUT_OFF, True),
    (True, DeviceState.VERIFIED, True),
    (1, DeviceState.OUTPUT_OFF, False),
])
def test_runner_requires_shutdown_confirmation(name, result, state, accepted):
    runner = SimpleNamespace(_anritsu_owns_output=True)
    device = SimpleNamespace(emergency_off=Mock(return_value=result), state=state)
    if accepted:
        RecipeRunner._shutdown_owned_device(runner, name, device)
    else:
        with pytest.raises(ExecutionError, match="confirm a safe state"):
            RecipeRunner._shutdown_owned_device(runner, name, device)
    device.emergency_off.assert_called_once()


@pytest.mark.parametrize("fault", [None, "OUTP?", "ABOR", "INIT:SWP?"])
def test_anritsu_emergency_off_returns_explicit_result(fault, monkeypatch):
    session = AnritsuSimulator()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    assert adapter.emergency_off() is False
    adapter.connect()
    original_query, original_write = session.query, session.write
    def query(command):
        if fault == command and command in {"OUTP?", "INIT:SWP?"}:
            raise OSError("injected readback failure")
        return original_query(command)
    def write(command):
        if fault == command == "ABOR":
            raise OSError("injected abort failure")
        return original_write(command)
    try:
        monkeypatch.setattr(session, "query", query)
        monkeypatch.setattr(session, "write", write)
        assert adapter.emergency_off() is (fault is None)
        assert adapter.state == (DeviceState.VERIFIED if fault is None else DeviceState.UNKNOWN)
    finally:
        monkeypatch.setattr(session, "query", original_query)
        monkeypatch.setattr(session, "write", original_write)
        adapter.disconnect()


@pytest.mark.parametrize("abort_only", [False, True])
@pytest.mark.parametrize("result,state,output_ok", [
    (None, DeviceState.VERIFIED, False),
    (False, DeviceState.OUTPUT_OFF, False),
    (None, DeviceState.OUTPUT_OFF, True),
    (True, DeviceState.VERIFIED, True),
])
def test_emergency_worker_reports_unconfirmed_output_or_abort(monkeypatch, abort_only, result, state, output_ok):
    device = SimpleNamespace(
        connect=Mock(), disconnect=Mock(), state=state,
        emergency_off=Mock(return_value=result), abort_acquisition=Mock(return_value=result),
    )
    monkeypatch.setattr(run_worker, "AnritsuAdapter", lambda *args, **kwargs: device)
    worker = run_worker.EmergencyStopWorker(
        simulation_settings(), simulation=True, device_names=frozenset({"anritsu"}),
        anritsu_rf_output=not abort_only,
    )
    results = []
    worker.finished.connect(results.append)
    worker.run()
    expected_ok = result is True if abort_only else output_ok
    assert len(results) == 1
    assert bool(results[0]) is not expected_ok
    if not expected_ok:
        assert ("acquisition abort" if abort_only else "physical OUTPUT OFF") in results[0][0]
    device.disconnect.assert_called_once()
    assert device.abort_acquisition.call_count == int(abort_only)
    assert device.emergency_off.call_count == int(not abort_only)


@pytest.mark.parametrize("name", ["anritsu", "rigol", "keithley"])
def test_initialization_cleanup_reports_unconfirmed_shutdown(monkeypatch, tmp_path, name):
    from app.engine.compiler import RecipeCompiler
    from app.recipes import parse_recipe_text

    settings = simulation_settings()
    plan = RecipeCompiler(settings).compile(parse_recipe_text(
        f"schema_version: 1\nname: cleanup\nroot: {{id: device, type: connect, device: {name}}}\n"
    ))
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True)
    device = SimpleNamespace(
        connected=True, state=DeviceState.VERIFIED, capabilities={},
        connect=Mock(side_effect=lambda: worker.request_stop()), disconnect=Mock(),
        emergency_off=Mock(return_value=False), abort_acquisition=Mock(return_value=None),
    )
    monkeypatch.setattr(worker, "_adapter_for_run", lambda *_: device)
    monkeypatch.setattr(run_worker, "require_storage_capacity", lambda *_: None)
    events = []
    worker.event.connect(lambda event, data: events.append((event, data)))
    worker.run()
    warnings = [data for event, data in events if event == "worker_cleanup_warning"]
    assert warnings
    assert any("was not confirmed" in error for data in warnings for error in data["errors"])
    device.disconnect.assert_called_once()


@pytest.mark.parametrize("overrun", [False, True])
def test_emergency_scope_encloses_connect_off_and_disconnect(monkeypatch, overrun):
    clock = [0.0]
    monkeypatch.setattr(run_worker.time, "monotonic", lambda: clock[0])
    active, observed = [], []
    @contextmanager
    def operation_timeout(duration):
        assert duration == 1
        active.append(True)
        try:
            yield
        finally:
            active.pop()
    def operation(name):
        assert active
        observed.append(name)
        clock[0] += .5 if overrun else .2
        return True
    device = SimpleNamespace(
        operation_timeout=operation_timeout, state=DeviceState.VERIFIED,
        connect=lambda: operation("connect"), emergency_off=lambda: operation("off"),
        disconnect=lambda: operation("disconnect"),
    )
    monkeypatch.setattr(run_worker, "AnritsuAdapter", lambda *args, **kwargs: device)
    settings = simulation_settings()
    settings.execution["shutdown_timeout"] = "1 s"
    worker = run_worker.EmergencyStopWorker(settings, simulation=True, device_names=frozenset({"anritsu"}))
    results = []
    worker.finished.connect(results.append)
    worker.run()
    assert observed == ["connect", "off", "disconnect"]
    assert bool(results[0]) == overrun
    if overrun:
        assert "exceeded shutdown deadline" in results[0][0]
