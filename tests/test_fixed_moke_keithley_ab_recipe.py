"""Compiled contract for the requested 5 x 41 sweep and untouched MOKE."""
from pathlib import Path

import pytest

from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.settings import SettingsRepository


def read_only_plan(settings):
    from dataclasses import replace
    recipe = parse_recipe_text(Path("recipes/example_nested_keithley_ab_fixed_moke.yml").read_text(encoding="utf-8"))
    return RecipeCompiler(settings).compile(replace(recipe,
        root=replace(recipe.root, children=recipe.root.children[1:]),
        finally_nodes=recipe.finally_nodes[:-1]))


@pytest.mark.parametrize("initial_v", [.1, .3217])
def test_explicit_moke_setting_reaches_target_and_holds(initial_v):
    from dataclasses import replace
    from types import SimpleNamespace
    from app.bootstrap import StationComposition
    from app.devices.simulators import simulated_station_settings
    from app.domain.models import ApplicationState, DeviceState
    from app.engine.runner import RecipeRunner
    from tests.test_adapters_and_runner import MemoryWriter
    settings = simulated_station_settings(SettingsRepository(Path(".config/settings.yml")).load().settings)
    recipe = parse_recipe_text(Path("recipes/example_nested_keithley_ab_fixed_moke.yml").read_text(encoding="utf-8"))
    moke_only = replace(recipe, root=replace(recipe.root, children=recipe.root.children[:1]),
                        finally_nodes=recipe.finally_nodes[-1:])
    plan = RecipeCompiler(settings).compile(moke_only)
    adapter = StationComposition(settings, simulation=True).create_adapter("moke_box")
    adapter._transport._vouts[0] = initial_v
    adapter.connect()
    unused = SimpleNamespace(state=DeviceState.OUTPUT_OFF)
    try:
        result = RecipeRunner(rigol=unused, keithley=unused, anritsu=unused, moke_box=adapter,
                              writer=MemoryWriter()).run(plan)
        assert result.error is None
        assert result.state is ApplicationState.HOLDING
        assert adapter.connected
        assert adapter.read_vouts()[0] == pytest.approx(.3217, abs=.00031)
    finally:
        adapter.disconnect()


def test_requested_recipe_has_exact_currents_and_moke_hold():
    settings = SettingsRepository(Path(".config/settings.yml")).load().settings
    recipe = parse_recipe_text(Path("recipes/example_nested_keithley_ab_fixed_moke.yml").read_text(encoding="utf-8"))
    plan = RecipeCompiler(settings).compile(recipe)
    assert plan.total_points == plan.total_spectra == 205
    assert plan.required_devices == {"keithley", "anritsu", "moke_box"}
    initial = [a for a in plan.actions if a.kind == "update_moke_voltage" and not a.is_finally]
    assert len(initial) == 1 and initial[0].payload["channel"] == 0
    assert initial[0].payload["voltage_v"] == pytest.approx(.3217)
    assert plan.retained_outputs == {"moke_box.vout0"}
    assert sum(a.kind == "measure_moke_hall" for a in plan.actions) == 205
    assert not any(action.kind.startswith("configure_anritsu") for action in plan.actions)
    spectra = [a for a in plan.actions if a.kind == "acquire_spectrum"]
    for outer, current_b in enumerate((-.004, -.005, -.006, -.007, -.008)):
        group = spectra[outer * 41:(outer + 1) * 41]
        assert [a.setpoints_si["keithley.B.current"] for a in group] == pytest.approx([current_b] * 41)
        assert [a.setpoints_si["keithley.A.current"] for a in group] == pytest.approx([.002 + i * .00005 for i in range(41)])
    main = [a for a in plan.actions if not a.is_finally]
    assert [(a.payload["channel"], a.payload["enabled"]) for a in main if a.kind == "set_keithley_output"] == [("A", False), ("B", False), ("B", True), ("A", True)]
    assert sum(a.kind == "configure_keithley" for a in main) == 2
    assert not any(a.kind == "ramp_keithley_to_zero" for a in main)
    assert [a.payload["duration_s"] for a in main if a.kind == "wait"] == [3.] * 3 + [5.] + [3.] * 205
    baselines = [a for a in main if a.kind == "acquire_reference"]
    assert [a.payload["purpose"] for a in baselines] == ["background", "reference"]
    assert all(a.payload["average_count"] == 4 and a.payload["inter_sweep_delay_s"] == 3 for a in baselines)
    assert baselines[0].payload["minimum_duration_s"] == 30
    assert main.index(baselines[-1]) < next(i for i, a in enumerate(main) if a.kind == "configure_keithley")
    for action in main:
        if action.kind == "configure_keithley":
            assert set(action.payload["request"].changed_fields) == {"mode", "level_si", "compliance_si", "source_range_si", "source_autorange", "sense_mode"}


@pytest.mark.parametrize("runner_owned_shutdown", [False, True])
def test_read_only_moke_cleanup_preserves_borrowed_session(tmp_path, runner_owned_shutdown):
    from unittest.mock import Mock
    from app.ui.run_worker import RunWorker
    settings = SettingsRepository(Path(".config/settings.yml")).load().settings
    plan = read_only_plan(settings)
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True)
    device = Mock()
    worker._run_leases["moke_box"] = device
    errors = []
    worker._cleanup_device("moke_box", device, runner_owned_shutdown, errors)
    assert not errors
    device.disconnect.assert_not_called()
    device.emergency_off.assert_not_called()
    device.stop_vout.assert_not_called()


def test_moke_mutation_still_requires_fault_shutdown(tmp_path):
    from dataclasses import replace
    from unittest.mock import Mock
    from app.engine.compiler import PlanAction
    from app.ui.run_worker import RunWorker
    settings = SettingsRepository(Path(".config/settings.yml")).load().settings
    recipe = parse_recipe_text(Path("recipes/example_nested_keithley_ab_fixed_moke.yml").read_text(encoding="utf-8"))
    plan = RecipeCompiler(settings).compile(recipe)
    plan = replace(plan, actions=plan.actions + (PlanAction("moke-write", "update_moke_voltage", {"channel": 0}, {}),))
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True)
    device = Mock(connected=True)
    worker._run_leases["moke_box"] = device
    errors = []
    worker._cleanup_device("moke_box", device, False, errors)
    assert not errors
    device.emergency_off.assert_called_once()
    device.disconnect.assert_not_called()


@pytest.mark.parametrize("name", ["moke_box", "keithley", "anritsu"])
@pytest.mark.parametrize("runner_owned_shutdown", [False, True])
@pytest.mark.parametrize("lease_already_released", [False, True])
def test_borrowed_sessions_remain_connected_after_success_or_fault(tmp_path, name, runner_owned_shutdown, lease_already_released):
    from unittest.mock import Mock
    from app.domain.models import DeviceState
    from app.ui.run_worker import RunWorker
    settings = SettingsRepository(Path(".config/settings.yml")).load().settings
    source = Path("recipes/example_nested_keithley_ab_fixed_moke.yml").read_text(encoding="utf-8")
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True)
    device = Mock(connected=True, state=DeviceState.OUTPUT_OFF)
    device.emergency_off.return_value = True
    device.abort_acquisition.return_value = True
    if lease_already_released:
        worker._device_controllers[name] = Mock()
    else:
        worker._run_leases[name] = device
    errors = []
    worker._cleanup_device(name, device, runner_owned_shutdown, errors)
    assert not errors
    device.disconnect.assert_not_called()
    if runner_owned_shutdown:
        device.emergency_off.assert_not_called()
        device.abort_acquisition.assert_not_called()
    elif name == "anritsu":
        device.abort_acquisition.assert_called_once()
    else:
        device.emergency_off.assert_called_once()


def test_simulated_full_sweep_keeps_existing_moke_connected_and_unchanged(tmp_path, monkeypatch):
    from unittest.mock import Mock
    import h5py
    from PySide6.QtWidgets import QApplication
    from app.bootstrap import StationComposition
    from app.devices.simulators import simulated_station_settings
    from app.engine.runner import RecipeRunner
    from app.storage.hdf5_reader import Hdf5RunReader
    from app.ui.run_worker import RunWorker
    app = QApplication.instance() or QApplication([])
    settings = simulated_station_settings(SettingsRepository(Path(".config/settings.yml")).load().settings)
    # Compile a separate, explicitly simulated source with the 30 s collection
    # floor removed. The test above verifies the production floor; this test
    # verifies acquisition/storage without creating thousands of fast frames.
    source = Path("recipes/example_nested_keithley_ab_fixed_moke.yml").read_text(encoding="utf-8")
    recipe = parse_recipe_text(source.replace("minimum_duration: 30 s", "minimum_duration: 0 s"))
    plan = RecipeCompiler(settings).compile(recipe)
    composition = StationComposition(settings, simulation=True)
    moke = composition.create_adapter("moke_box")
    moke._transport._vouts[0] = .3217  # Seed a pre-existing physical state, no recipe write.
    moke.connect()
    before = moke.read_vouts()[0]
    monkeypatch.setattr(moke, "connect", Mock(wraps=moke.connect))
    monkeypatch.setattr(moke, "disconnect", Mock(wraps=moke.disconnect))
    monkeypatch.setattr(moke, "emergency_off", Mock(wraps=moke.emergency_off))
    monkeypatch.setattr(moke, "release", Mock(), raising=False)
    devices = {"moke_box": moke, "keithley": composition.create_adapter("keithley"),
               "anritsu": composition.create_adapter("anritsu")}
    controllers = {}
    for name, device in devices.items():
        if name != "moke_box":
            device.connect()
            monkeypatch.setattr(device, "connect", Mock(wraps=device.connect))
            monkeypatch.setattr(device, "disconnect", Mock(wraps=device.disconnect))
            monkeypatch.setattr(device, "release", Mock(), raising=False)
        controller = Mock()
        controller.acquire_run_lease.return_value = device
        controllers[name] = controller
    waits = []
    monkeypatch.setattr(RecipeRunner, "_interruptible_wait", lambda self, duration: waits.append(duration))
    worker = RunWorker(settings, Path(".config/settings.yml"), plan, simulation=True,
                       output_dir_override=tmp_path, device_controllers=controllers)
    finished, failed = [], []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)
    try:
        worker.run()
        assert not failed, failed
        assert len(finished) == 1
        assert finished[0]["result"].error is None
        assert finished[0]["result"].stored_points == 205
        assert moke.connected and moke.read_vouts()[0] == before
        moke.connect.assert_not_called()
        moke.disconnect.assert_not_called()
        moke.emergency_off.assert_not_called()
        moke.release.assert_called_once()
        for device in devices.values():
            assert device.connected
            device.connect.assert_not_called()
            device.disconnect.assert_not_called()
            device.release.assert_called_once()
        assert waits.count(3.) == 214 and waits.count(5.) == 1
        assert len(waits) == 215
        with h5py.File(finished[0]["path"], "r") as data:
            assert data["run"].attrs["status"] == "completed"
            assert len(data["points"]) == len(data["spectra"]) == 205
            assert len(data["references"]) == 2
            assert [data[f"references/{i}"].attrs["purpose"] for i in range(2)] == ["background", "reference"]
        assert len(Hdf5RunReader.points(finished[0]["path"], include_details=False)) == 205
    finally:
        # Test teardown owns its simulated session after asserting no run disconnect.
        for device in devices.values():
            device.disconnect()
        app.processEvents()
