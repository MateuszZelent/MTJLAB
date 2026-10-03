import json

import h5py
import pytest
from unittest.mock import patch

from app.bootstrap import StationComposition
from app.devices.simulators import simulated_station_settings
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.models import ApplicationState
from app.engine.compiler import RecipeCompiler
from app.engine.runner import ExecutionMode, RecipeRunner
from app.recipes import parse_recipe_text
from app.settings.models import StationSettings
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.hdf5_series_reader import Hdf5SeriesReader
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.helpers import loaded_settings
from tests.test_adapters_and_runner import MemoryWriter, ShutdownProbe
from app.integrations.elab.config import ElabCredentials, ElabIntegrationProfile
from app.integrations.elab.service import ElabUploadRequest, upload_result


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


@pytest.mark.parametrize('smu_failure', [False, True])
def test_moke_sweep_combines_keithley_and_lakeshore_in_each_stored_point(tmp_path, smu_failure):
    source = SOURCE.replace(
        "    - {id: configure, type: configure_moke_box",
        "    - {id: smu, type: configure_keithley, channel: B, mode: current, level: '1 mA', compliance: '67 mV', source_autorange: false, source_range: '10 mA'}\n"
        "    - {id: smu_on, type: set_keithley_output, channel: B, enabled: true}\n"
        "    - {id: configure, type: configure_moke_box",
    ).replace(
        "        - {id: hall, type: measure_moke_hall}",
        "        - {id: smu_read, type: measure_keithley, channel: B}\n"
        "        - {id: field_read, type: measure_lakeshore_field, checkpoint: false}\n"
        "        - {id: hall, type: measure_moke_hall}",
    )
    raw = station(tmp_path).model_dump(mode='python')
    raw['devices']['keithley']['safety']['allow_output_enable'] = True
    settings = StationSettings.model_validate(raw)
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    assert plan.required_devices == frozenset({'moke_box', 'keithley', 'lakeshore_gaussmeter'})
    composition = StationComposition(settings, simulation=True)
    devices = {key: composition.create_adapter(key) for key in plan.required_devices}
    for adapter in devices.values():
        adapter.connect()
    if smu_failure:
        def failed_measure(_channel):
            raise RuntimeError('Injected Keithley measurement failure')
        devices['keithley'].measure = failed_measure
    path = tmp_path / 'mixed.h5'
    writer = Hdf5RunWriter(
        path, recipe_source=source, settings_source=settings.model_dump_json(),
        plan_hash=plan.sha256, expected_points=plan.total_points,
        csv_summary_path=path.with_suffix('.csv'),
        device_idn={key: adapter.identity.idn for key, adapter in devices.items()},
        simulation_metadata={'enabled': True},
    )
    result = RecipeRunner(
        rigol=ShutdownProbe(), anritsu=ShutdownProbe(),
        keithley=devices['keithley'], moke_box=devices['moke_box'],
        lakeshore=devices['lakeshore_gaussmeter'], writer=writer,
    ).run(plan)
    if smu_failure:
        assert result.error is not None
        assert result.stored_points == 0
        assert devices['moke_box'].safe_target_confirmed
        assert devices['moke_box'].read_vouts()[2] == 0
        assert devices['keithley'].confirm_output_off('B')
        with h5py.File(path) as file:
            assert file['run'].attrs['status'] == 'faulted'
        return
    assert result.error is None
    assert result.stored_points == 3
    with h5py.File(path) as file:
        for index in range(3):
            point = file[f'points/{index}']
            measurements = json.loads(point['measurements_json'].asstr()[()])
            assert 'moke_box.vout_voltage_v' in measurements
            assert 'moke_box.hall1_voltage_v' in measurements
            assert 'lakeshore.field_t' in measurements
            assert any(key.startswith('keithley.B.') for key in measurements)
    assert devices['moke_box'].safe_target_confirmed
    series = Hdf5SeriesReader.read_series(path, preferred_y_channel='lakeshore.field_t')
    assert series.point_count == 3
    assert series.x_unit == 'V'
    assert series.y_unit == 'T'
    assert series.x_values == pytest.approx((-0.5, 0, 0.5), abs=0.001)
    for direction in ('ascending', 'descending'):
        label, unit = Hdf5SeriesReader._format_channel_label(f'moke_box.field_estimated_{direction}_t')
        assert direction in label and 'Estimated' in label
        assert unit == 'T'
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors
    original = path.read_bytes()
    uploaded = []
    bodies = []

    class OfflineElabClient:
        def __init__(self, _credentials, *, timeout_s):
            pass

        def create_experiment(self, *, template_id, title, body):
            bodies.append(body)
            return 42, 'https://elab.example.test/experiments/42'

        def add_tag(self, *, experiment_id, tag):
            pass

        def upload_file(self, *, experiment_id, path, comment):
            uploaded.append((path.name, path.read_bytes()))

    request = ElabUploadRequest(
        path=path, credentials=ElabCredentials.from_values('https://elab.example.test', 'test'),
        profile=ElabIntegrationProfile(template_id=1, upload_csv=True),
        ledger_path=tmp_path / 'elab-ledger.json',
    )
    with patch('app.integrations.elab.service.ElabApiClient', OfflineElabClient):
        first = upload_result(request)
        second = upload_result(request)
    assert first.record.status == 'uploaded'
    assert second.skipped_existing
    assert [name for name, _ in uploaded] == ['mixed.h5', 'mixed.csv']
    assert uploaded[0][1] == original == path.read_bytes()
    assert '<th>Committed points</th><td>3</td>' in bodies[0]
    assert plan.sha256 in bodies[0]


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
