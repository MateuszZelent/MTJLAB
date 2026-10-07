"""Cancellation at initialization boundaries must never enter the recipe."""
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.ui import run_worker
from tests.helpers import simulation_settings
from tests.test_sweep_worker_initialization_faults import plan


@pytest.mark.parametrize("boundary", ["before_run", "connect", "storage"])
def test_initialization_stop_survives_until_recipe_dispatch(monkeypatch, tmp_path, boundary):
    settings = simulation_settings()
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
    device = Mock(
        spec=["connected", "capabilities", "connect", "abort_acquisition", "disconnect"],
        connected=True, capabilities={},
    )
    device.connect.return_value = SimpleNamespace(idn="FAKE")
    device.abort_acquisition.return_value = True
    monkeypatch.setattr(worker, "_adapter_for_run", lambda *_args: device)
    monkeypatch.setattr(run_worker, "require_storage_capacity", lambda *_args: None)
    writer = Mock(path=tmp_path / "run.h5")
    writer_factory = Mock(return_value=writer)
    monkeypatch.setattr(run_worker, "Hdf5RunWriter", writer_factory)
    runner = Mock()
    runner_factory = Mock(return_value=runner)
    monkeypatch.setattr(run_worker, "RecipeRunner", runner_factory)
    if boundary == "before_run":
        worker.request_stop()
    elif boundary == "connect":
        def connect():
            worker.request_stop()
            return SimpleNamespace(idn="FAKE")
        device.connect.side_effect = connect
    else:
        def open_writer(*_args, **_kwargs):
            worker.request_stop()
            return writer
        writer_factory.side_effect = open_writer
        # Assert the runner receives cancellation BEFORE any recipe dispatch.
        def run(*_args, **_kwargs):
            runner.request_stop.assert_called_once()
            return SimpleNamespace()
        runner.run.side_effect = run
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    assert worker._early_stop_requested.is_set()
    if boundary != "storage":
        runner_factory.assert_not_called()
        writer_factory.assert_not_called()
        assert failures and "stopped by operator" in failures[0]
    if boundary == "before_run":
        device.connect.assert_not_called()
    if boundary == "connect":
        device.abort_acquisition.assert_called_once()
        device.disconnect.assert_called_once()
        assert "cleanup incomplete" not in failures[0]


def test_unknown_connection_does_not_skip_cleanup(monkeypatch, tmp_path):
    settings = simulation_settings()
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
    calls = []

    class Device:
        @property
        def connected(self):
            raise RuntimeError("readback unavailable")

        def connect(self):
            worker.request_stop()
            return SimpleNamespace(idn="FAKE")

        def abort_acquisition(self):
            calls.append("abort")

        def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr(worker, "_adapter_for_run", lambda *_args: Device())
    monkeypatch.setattr(run_worker, "require_storage_capacity", lambda *_args: None)
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    assert calls == ["abort", "disconnect"]
    assert len(failures) == 1
    assert "connection state: readback unavailable" in failures[0]


def test_partial_reservation_failure_releases_previously_acquired_device(tmp_path):
    settings = simulation_settings()
    run_plan = replace(plan(settings), required_devices=frozenset({"rigol", "keithley", "anritsu"}))
    lease = Mock()
    rigol = SimpleNamespace(acquire_run_lease=Mock(return_value=lease))
    keithley = SimpleNamespace(acquire_run_lease=Mock(side_effect=RuntimeError("busy")))
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", run_plan,
                                  device_controllers={"rigol": rigol, "keithley": keithley})
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    rigol.acquire_run_lease.assert_called_once()
    lease.release.assert_called_once()
    assert not worker._run_leases
    assert failures and "busy" in failures[0]


def test_failed_release_retains_reservation_and_reports_uncertainty(tmp_path):
    settings = simulation_settings()
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings))
    lease = Mock()
    lease.release.side_effect = RuntimeError("operation in flight")
    worker._run_leases["anritsu"] = lease
    assert worker._release_run_leases() == ["anritsu reservation remains held: operation in flight"]
    assert worker._run_leases["anritsu"] is lease
