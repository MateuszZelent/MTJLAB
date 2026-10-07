"""Failed initialization de-energizes devices before potentially slow HDF5 close."""

from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import contextmanager

import pytest

from app.ui import run_worker
from app.domain.models import ApplicationState
from app.engine.runner import RunResult
from tests.helpers import simulation_settings
from tests.test_sweep_worker_initialization_faults import plan


@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("abort_fails", [False, True])
@pytest.mark.parametrize("failure_stage", ["construction", "run"])
def test_hardware_cleanup_precedes_failed_run_archive_close(monkeypatch, tmp_path, close_fails, abort_fails, failure_stage):
    settings = simulation_settings()
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
    calls = []
    def abort():
        calls.append("abort")
        if abort_fails:
            raise RuntimeError("abort failed")
        return True
    device = SimpleNamespace(
        connected=True, capabilities={}, identity=SimpleNamespace(idn="TEST"),
        connect=Mock(return_value=SimpleNamespace(idn="TEST")),
        abort_acquisition=abort, disconnect=lambda: calls.append("disconnect"),
    )
    def close(status):
        assert status == "faulted"
        calls.append("close")
        assert calls[:2] == ["abort", "disconnect"]
        if close_fails:
            raise OSError("filesystem failed")
    writer = SimpleNamespace(path=tmp_path / "run.h5", close=close)
    monkeypatch.setattr(worker, "_adapter_for_run", lambda *_: device)
    monkeypatch.setattr(run_worker, "require_storage_capacity", lambda *_: None)
    monkeypatch.setattr(run_worker, "Hdf5RunWriter", lambda *args, **kwargs: writer)
    error = RuntimeError(f"runner {failure_stage} failed")
    factory = (Mock(side_effect=error) if failure_stage == "construction" else
               Mock(return_value=SimpleNamespace(run=Mock(side_effect=error))))
    monkeypatch.setattr(run_worker, "RecipeRunner", factory)
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    assert calls == ["abort", "disconnect", "close"]
    assert len(failures) == 1 and f"runner {failure_stage} failed" in failures[0]
    assert ("filesystem failed" in failures[0]) == close_fails
    assert ("abort failed" in failures[0]) == abort_fails


@pytest.mark.parametrize("fault", ["disconnect", "release"])
@pytest.mark.parametrize("prior_error", [None, "measurement failed"])
def test_post_run_cleanup_fault_keeps_archive_but_cannot_report_success(monkeypatch, tmp_path, fault, prior_error):
    settings = simulation_settings()
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
    device = SimpleNamespace(
        connected=True, capabilities={}, identity=SimpleNamespace(idn="TEST"),
        connect=Mock(return_value=SimpleNamespace(idn="TEST")),
        disconnect=Mock(side_effect=RuntimeError("disconnect failed") if fault == "disconnect" else None),
    )
    path = tmp_path / "run.h5"
    writer = SimpleNamespace(path=path, close=Mock())
    result = RunResult(ApplicationState.FAULT if prior_error else ApplicationState.SAFE, 10, 3, prior_error)
    monkeypatch.setattr(worker, "_adapter_for_run", lambda *_: device)
    monkeypatch.setattr(run_worker, "require_storage_capacity", lambda *_: None)
    monkeypatch.setattr(run_worker, "Hdf5RunWriter", lambda *args, **kwargs: writer)
    monkeypatch.setattr(run_worker, "RecipeRunner", lambda *args, **kwargs: SimpleNamespace(run=lambda *args, **kwargs: result))
    monkeypatch.setattr(worker, "_release_run_leases", lambda: ["release failed"] if fault == "release" else [])
    failures, finished = [], []
    worker.failed.connect(failures.append)
    worker.finished.connect(finished.append)
    worker.run()
    assert not failures and len(finished) == 1
    outcome = finished[0]
    assert outcome["path"] == str(path)
    assert outcome["result"].state == ApplicationState.FAULT
    assert outcome["result"].stored_points == 3
    assert outcome["result"].completed_actions == 10
    assert f"{fault} failed" in outcome["result"].error
    if prior_error:
        assert outcome["result"].error.startswith(prior_error)
    assert outcome["cleanup_errors"]


@pytest.mark.parametrize("first_fails", [False, True])
def test_initialization_cleanup_shares_deadline_and_reserves_other_devices(monkeypatch, tmp_path, first_fails):
    settings = simulation_settings()
    settings.execution["shutdown_timeout"] = "20 s"
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
    monkeypatch.setattr(worker, "_required_devices", lambda: {"keithley", "anritsu"})
    now, calls, budgets = [0.], [], []
    monkeypatch.setattr(run_worker.time, "monotonic", lambda: now[0])

    class Device:
        active = False

        def __init__(self, name):
            self.name = name

        @contextmanager
        def operation_timeout(self, seconds):
            budgets.append((self.name, seconds))
            self.active = True
            try:
                yield
            finally:
                self.active = False

        @property
        def connected(self):
            assert self.active
            calls.append((self.name, "connected"))
            return True

        def emergency_off(self):
            assert self.active
            calls.append((self.name, "off"))
            now[0] += budgets[-1][1] - .5
            if self.name == "anritsu" and first_fails:
                raise TimeoutError("injected cleanup fault")
            return True

        abort_acquisition = emergency_off

        def disconnect(self):
            assert self.active
            calls.append((self.name, "disconnect"))
            now[0] += .25

    devices = {name: Device(name) for name in ("keithley", "anritsu", "unrelated")}
    errors = worker._cleanup_devices(devices, runner_owned_shutdown=False)
    assert budgets == [("anritsu", 10.), ("keithley", 10.25)]
    assert now[0] == 19.75
    assert calls == [(name, operation) for name in ("anritsu", "keithley")
                     for operation in ("connected", "off", "disconnect")]
    assert bool(errors) == first_fails
    assert all(not device.active for device in devices.values())


def test_noncooperative_cleanup_overrun_cannot_report_success(monkeypatch, tmp_path):
    settings = simulation_settings()
    settings.execution["shutdown_timeout"] = "1 s"
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
    now = [0.]
    monkeypatch.setattr(run_worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker, "_required_devices", lambda: {"anritsu", "keithley"})
    def disconnect():
        now[0] += 2.
    other = SimpleNamespace(connected=True, disconnect=Mock())
    device = SimpleNamespace(connected=True, disconnect=disconnect)
    errors = worker._cleanup_devices({"keithley": other, "anritsu": device}, runner_owned_shutdown=True)
    assert any("anritsu" in error and "exceeded" in error for error in errors)
    assert any("keithley" in error and "unconfirmed" in error for error in errors)
    other.disconnect.assert_not_called()
