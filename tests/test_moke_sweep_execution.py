import json

import h5py
import pytest

from app.bootstrap import StationComposition
from app.devices.simulators import simulated_station_settings
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.models import ApplicationState
from app.engine.compiler import RecipeCompiler
from app.engine.runner import ExecutionMode, RecipeRunner
from app.recipes import parse_recipe_text
from app.settings.models import StationSettings
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.helpers import loaded_settings
from tests.test_adapters_and_runner import MemoryWriter, ShutdownProbe


SOURCE = """schema_version: 1
name: MOKE voltage sweep
root:
  id: main
  type: sequence
  children:
    - {id: configure, type: configure_moke_box, channel: 2, minimum_voltage: '-0.5 V', maximum_voltage: '0.5 V'}
    - {id: arm, type: arm_moke_voltage}
    - id: sweep
      type: sweep
      target: moke_box.vout2.voltage
      start: '-0.5 V'
      stop: '0.5 V'
      points: 3
      children:
        - {id: hall, type: measure_moke_hall}
finally:
  - {id: zero, type: stop_moke_voltage}
"""


def station(tmp_path):
    raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
    raw["devices"]["moke_box"]["calibration_directory"] = str(tmp_path / "calibrations")
    return StationSettings.model_validate(raw)


def test_compiler_binds_full_expanded_trajectory_to_one_arm_and_shutdown(tmp_path):
    settings = station(tmp_path)
    plan = RecipeCompiler(settings).compile(parse_recipe_text(SOURCE))
    kinds = [action.kind for action in plan.actions]
    assert kinds == ["configure_moke_box", "arm_moke_voltage",
                     "update_moke_voltage", "measure_moke_hall",
                     "update_moke_voltage", "measure_moke_hall",
                     "update_moke_voltage", "measure_moke_hall", "stop_moke_voltage"]
    trajectory = plan.actions[0].payload["plan"]
    assert trajectory.targets_v == (-0.5, 0, 0.5)
    assert plan.actions[1].payload["plan"] == trajectory
    assert plan.required_devices == frozenset({"moke_box"})
    assert "moke_box.dac_zero_or_unknown" in plan.safe_shutdown_actions
    assert plan.total_points == 3


@pytest.mark.parametrize("source", [
    SOURCE.replace("    - {id: arm, type: arm_moke_voltage}\n", ""),
    SOURCE.replace("minimum_voltage: '-0.5 V'", "minimum_voltage: '0.1 V'"),
    SOURCE.replace("channel: 2", "channel: 3"),
    SOURCE.replace("start: '-0.5 V'", "start: '-1 V'").replace("stop: '0.5 V'", "stop: '1 V'"),
    SOURCE.replace("maximum_voltage: '0.5 V'", "maximum_voltage: '0.5 A'"),
])
def test_unsafe_recipe_is_rejected_before_any_adapter_exists(tmp_path, source):
    with pytest.raises((ConfigurationError, SafetyViolation, ValueError)):
        RecipeCompiler(station(tmp_path)).compile(parse_recipe_text(source))


def test_execution_records_actual_dac_and_returns_zero_without_claiming_kepco_off(tmp_path):
    settings = station(tmp_path)
    plan = RecipeCompiler(settings).compile(parse_recipe_text(SOURCE))
    moke = StationComposition(settings, simulation=True).create_adapter("moke_box")
    moke.connect()
    path = tmp_path / "sweep.h5"
    writer = Hdf5RunWriter(path, recipe_source=SOURCE, settings_source=settings.model_dump_json(),
                          plan_hash=plan.sha256, device_idn={"moke_box": moke.identity.idn})
    events = []
    runner = RecipeRunner(
        rigol=ShutdownProbe(), keithley=ShutdownProbe(), anritsu=ShutdownProbe(),
        moke_box=moke, writer=writer, on_event=lambda name, data: events.append((name, data)),
    )
    result = runner.run(plan)
    assert result.error is None
    assert result.state is ApplicationState.UNKNOWN  # DAC-zero confirmation is distinct from power-off
    assert result.stored_points == 3
    assert moke.safe_target_confirmed
    assert moke.read_vouts()[2] == 0
    with h5py.File(path) as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["points"]) == 3
        point = file["points/0"]
        setpoints = json.loads(point["setpoints_json"].asstr()[()])
        measurements = json.loads(point["measurements_json"].asstr()[()])
        assert setpoints["moke_box.vout2.voltage"] == measurements["moke_box.vout_voltage_v"]
        assert "moke_box.hall1_voltage_v" in measurements
        assert "moke_box.hall1_field_t" not in measurements
        metadata = json.loads(point["metadata_json"].asstr()[()])
        assert metadata["safety_context"]["moke_box"]["maximum_v"] == 0.5
    assert any(name == "moke_dac_shutdown" and data["dac_zero_confirmed"] for name, data in events)
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors


def test_dry_run_never_arms_or_writes_voltage(tmp_path):
    settings = station(tmp_path)
    plan = RecipeCompiler(settings, outputs_forced_off=True).compile(parse_recipe_text(SOURCE))
    moke = StationComposition(settings, simulation=True).create_adapter("moke_box")
    moke.connect()
    writer = MemoryWriter()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Dry run must not arm or write MOKE voltage")
    moke.arm_voltage_plan = forbidden
    moke.ramp_vout = forbidden
    moke.stop_vout = forbidden
    result = RecipeRunner(
        rigol=ShutdownProbe(), keithley=ShutdownProbe(), anritsu=ShutdownProbe(),
        moke_box=moke, writer=writer, execution_mode=ExecutionMode.DRY_RUN,
    ).run(plan)
    assert result.error is None
    assert writer.status == "completed"
    assert all(point.measurements["moke_box.vout_voltage_v"] == 0 for point, _ in writer.points)


def test_recipe_stop_interrupts_ramp_and_prevents_remaining_points(tmp_path):
    settings = station(tmp_path)
    plan = RecipeCompiler(settings).compile(parse_recipe_text(SOURCE))
    moke = StationComposition(settings, simulation=True).create_adapter("moke_box")
    moke.connect()
    writer = MemoryWriter()
    runner = RecipeRunner(
        rigol=ShutdownProbe(), keithley=ShutdownProbe(), anritsu=ShutdownProbe(), moke_box=moke, writer=writer,
    )
    def stop_after_first_point(name, _data):
        if name == "point_stored":
            runner.request_stop()
    runner._on_event = stop_after_first_point
    result = runner.run(plan)
    assert result.state is ApplicationState.UNKNOWN
    assert result.stored_points == 1
    assert writer.status == "aborted"
    assert moke.safe_target_confirmed
