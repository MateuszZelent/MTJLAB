"""A run owns its declared instruments; explicit station E-STOP owns all."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.devices.simulators import simulated_station_settings
from app.domain.models import DeviceState
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.ui import run_worker
from tests.helpers import simulation_settings
from tests.test_run_controller import _RunLeaseAdapter


@pytest.mark.parametrize("mode", ["measurement", "dry_run"])
def test_analyzer_run_leaves_other_connected_sessions_untouched(tmp_path, mode, monkeypatch):
    settings = simulation_settings()
    plan = RecipeCompiler(settings, outputs_forced_off=mode == "dry_run").compile(parse_recipe_text(
        "schema_version: 1\nname: scoped\nroot: {id: analyzer, type: connect, device: anritsu}\n"
    ))
    assert plan.safe_shutdown_actions == ("storage.flush_checkpoint",)
    adapters = {name: _RunLeaseAdapter() for name in ("rigol", "keithley", "anritsu")}
    for adapter in adapters.values():
        adapter.connect()
        monkeypatch.setattr(adapter, "release", Mock(), raising=False)
    for name in ("rigol", "keithley"):
        adapters[name]._state = DeviceState.OUTPUT_ON
    controllers = {name: SimpleNamespace(acquire_run_lease=lambda current=adapter: current)
                   for name, adapter in adapters.items()}
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan,
                                  simulation=True, execution_mode=mode,
                                  output_dir_override=str(tmp_path), device_controllers=controllers)
    errors, results = [], []
    worker.failed.connect(errors.append)
    worker.finished.connect(results.append)
    worker.run()
    assert not errors, errors
    assert len(results) == 1
    assert results[0]["result"].error is None
    adapters["anritsu"].release.assert_called_once()
    for name in ("rigol", "keithley"):
        adapter = adapters[name]
        assert adapter.connect_count == 1
        assert adapter.emergency_off_count == adapter.disconnect_count == 0
        assert adapter.state == DeviceState.OUTPUT_ON
        adapter.release.assert_not_called()
    assert adapters["anritsu"].abort_count == 0
    assert adapters["anritsu"].emergency_off_count == 0
    assert adapters["anritsu"].disconnect_count == 1


@pytest.mark.parametrize("scope,expected", [(frozenset({"anritsu"}), {"anritsu"}),
                                            (frozenset(), set()),
                                            (None, {"anritsu", "keithley", "rigol", "moke_box"})])
@pytest.mark.parametrize("rf_output", [False, True])
def test_emergency_worker_respects_watchdog_scope_but_manual_estop_is_global(monkeypatch, scope, expected, rf_output):
    settings = simulated_station_settings(simulation_settings())
    devices = {}
    def factory(name):
        def create(*args, **kwargs):
            adapter = _RunLeaseAdapter()
            devices[name] = adapter
            return adapter
        return create
    for name, symbol in (("rigol", "RigolAdapter"), ("keithley", "KeithleyAdapter"), ("anritsu", "AnritsuAdapter")):
        monkeypatch.setattr(run_worker, symbol, factory(name))
    monkeypatch.setattr(run_worker, "StationComposition", lambda *args, **kwargs:
                        SimpleNamespace(create_adapter=lambda name: factory(name)()))
    worker = run_worker.EmergencyStopWorker(settings, simulation=True, device_names=scope, anritsu_rf_output=rf_output)
    results = []
    worker.finished.connect(results.append)
    worker.run()
    assert set(devices) == expected
    assert results == [()]
    for name, adapter in devices.items():
        assert adapter.disconnect_count == 1
        assert adapter.abort_count == int(name == "anritsu" and not rf_output)
        assert adapter.emergency_off_count == int(name != "anritsu" or rf_output)
