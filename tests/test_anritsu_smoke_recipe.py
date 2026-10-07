"""Execute the shipped MOKE/Keithley A/analyzer recipe and verify its archive."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np

from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.engine.compiler import RecipeCompiler
from app.engine.runner import RecipeRunner
from app.recipes import load_recipe
from app.storage.thatec_validator import ThatecCompatibilityValidator
from app.storage import Hdf5RunReader
from app.spectrum.processing import apply_reference_operation
from app.ui.results.processing import ResultProcessing, read_processed_private
from app.ui.run_worker import RunWorker
from tests.test_moke_smoke_sweep_limits import settings_for_channel_two


def test_fixed_keithley_bias_order_and_channel_scope():
    plan = RecipeCompiler(settings_for_channel_two()).compile(load_recipe(
        Path("recipes/anritsu_background_reference_smoke_test.yml")))
    actions = [action for action in plan.actions if not action.is_finally]
    configuration, = [action for action in actions if action.kind == "configure_keithley"]
    request = configuration.payload["request"]
    assert request.channel == "A" and request.mode == "current"
    assert request.level_si == 0.0015 and request.compliance_si == 0.670
    assert request.sense_mode == "2wire" and request.source_range_si == 0.010
    assert set(request.changed_fields) == {
        "mode", "level_si", "compliance_si", "sense_mode", "source_autorange", "source_range_si"}
    enable, = [action for action in actions if action.kind == "set_keithley_output" and action.payload["enabled"]]
    enable_index = actions.index(enable)
    assert actions.index(configuration) < enable_index
    assert actions[enable_index + 1].kind == "wait"
    assert actions[enable_index + 1].payload["duration_s"] == 5
    baselines = [index for index, action in enumerate(actions) if action.kind == "acquire_reference"]
    off_index, = [index for index, action in enumerate(actions)
                 if action.kind == "set_keithley_output" and not action.payload["enabled"]]
    assert off_index < min(baselines) <= max(baselines) < actions.index(configuration)
    assert all(index > enable_index + 1 for index, action in enumerate(actions)
               if action.kind == "acquire_spectrum")
    assert not any(action.kind == "update_keithley_level" for action in actions)
    assert all(action.payload.get("channel", "A") == "A" for action in plan.actions
               if "keithley" in action.kind)
    shutdown = [action for action in plan.actions if action.is_finally]
    assert any(action.kind == "ramp_keithley_to_zero" and action.payload["channel"] == "A" for action in shutdown)
    assert any(action.kind == "set_keithley_output" and action.payload == {"channel": "A", "enabled": False}
               for action in shutdown)


def test_shipped_moke_analyzer_recipe(tmp_path, monkeypatch):
    # Exercise the recipe independently of the host's disk capacity. Dedicated
    # storage-fault tests cover the real preflight rejection for insufficient space.
    monkeypatch.setattr(
        "app.engine.estimation.shutil.disk_usage", lambda _path: SimpleNamespace(free=10**12)
    )
    recipe = load_recipe(Path("recipes/anritsu_background_reference_smoke_test.yml"))
    settings = settings_for_channel_two()
    plan = RecipeCompiler(settings).compile(recipe)
    assert plan.required_devices == frozenset({"anritsu", "moke_box", "keithley"})
    assert plan.total_points == plan.total_spectra == 10
    def forbidden(*args, **kwargs):
        raise AssertionError("MOKE/analyzer sweep contacted an unrelated device")
    for name in ("connect", "emergency_off", "disconnect"):
        monkeypatch.setattr(RigolAdapter, name, forbidden)
    # Even an unrelated already-connected device-page session must survive.
    monkeypatch.setattr(RigolAdapter, "connected", property(lambda self: True))
    output_events = []
    set_output = KeithleyAdapter.set_output
    def tracked_output(adapter, channel, enabled):
        result = set_output(adapter, channel, enabled)
        output_events.append((channel, enabled, time.monotonic(), adapter))
        return result
    monkeypatch.setattr(KeithleyAdapter, "set_output", tracked_output)
    waits = []
    wait = RecipeRunner._interruptible_wait
    def tracked_wait(runner, duration_s):
        started = time.monotonic()
        if duration_s == 5:
            channel, enabled, confirmed_at, adapter = output_events[-1]
            assert (channel, enabled) == ("A", True)
            assert started >= confirmed_at
            assert adapter._last_request["A"].level_si == 0.0015
        wait(runner, duration_s)
        waits.append((duration_s, time.monotonic() - started))
    monkeypatch.setattr(RecipeRunner, "_interruptible_wait", tracked_wait)
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True,
                       execution_mode="measurement", output_dir_override=str(tmp_path),
                       file_stem_override="anritsu-smoke-test")
    errors, finished = [], []
    worker.failed.connect(errors.append)
    worker.finished.connect(finished.append)
    worker.run()
    assert not errors, errors
    assert len(finished) == 1
    assert [(channel, enabled) for channel, enabled, *_ in output_events if enabled] == [("A", True)]
    enable_waits = [elapsed for duration, elapsed in waits if duration == 5]
    assert len(enable_waits) == 1 and enable_waits[0] >= 5
    assert output_events[-1][0:2] == ("A", False)
    path, = tmp_path.glob("*.h5")
    with h5py.File(path, "r") as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["spectra"]) == 10
        assert len(file["references"]) == 2
        background, reference = file["references/0"], file["references/1"]
        assert background.attrs["purpose"] == "background"
        assert reference.attrs["purpose"] == "reference"
        assert reference.attrs["average_count"] == 4
        metadata = json.loads(background.attrs["acquisition_metadata_json"])
        assert metadata["collection_elapsed_s"] >= 30
        assert background.attrs["average_count"] >= 4
        assert len(file["recipe_raw_sweeps_v1"]) == background.attrs["average_count"] + 14
        assert len(file["spectra/0/power_dbm"]) == len(reference["power_dbm"])
        assert "processed_values" not in file["spectra/0"]
        assert "reference_index" not in file["spectra/0"].attrs
        for group in (background, reference):
            evidence = json.loads(group.attrs["acquisition_metadata_json"])
            assert evidence["configuration_fingerprint"]
            assert evidence["inter_sweep_delay_s"] == 3
            assert len(group["source_recipe_sweep_indices"]) == group.attrs["average_count"]
    points = Hdf5RunReader.points(path)
    assert len(points) == 10
    assert all(point.setpoints["keithley.A.current"] == 0.0015 for point in points)
    np.testing.assert_allclose(
        [point.setpoints["moke_box.vout2.voltage"] for point in points],
        np.linspace(0, 0.010, 10),
        atol=10 / 32767, rtol=0,  # One positive DAC code; upper limit rounds inward.
    )
    point = points[0]
    raw = Hdf5RunReader.spectrum(path, point.index)
    for operation, index, purpose in (
        ("subtract_power_signed", 0, "background"),
        ("subtract_reference_signed", 1, "reference"),
    ):
        derived = read_processed_private(path, point, ResultProcessing(operation))
        expected, unit = apply_reference_operation(
            raw.powers_dbm, Hdf5RunReader.reference(path, index).powers_dbm, "subtract_power_signed"
        )
        np.testing.assert_allclose(derived.values, expected)
        assert derived.unit == unit == "W"
        assert f"Recorded {purpose} {index}" in derived.method
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors
