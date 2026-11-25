"""Reproducible evidence for the 2026-10-04 sweep audit.

Regression contracts for the repaired findings; every assertion must pass.
All instrument communication uses simulators; station files are never modified.
"""

import json
from itertools import product

import h5py
import pytest

from app.bootstrap import StationComposition
from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.registry import built_in_device_registry
from app.devices.simulators import KeithleySimulator, simulated_station_settings
from app.domain.errors import ConfigurationError, DeviceError, ExecutionError, SafetyViolation
from app.domain.models import MeasurementPoint
from app.engine.compiler import RecipeCompiler
from app.engine.runner import RecipeRunner
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.recipes.sweep_points import generate_sweep_points
from app.settings.models import StationSettings
from app.storage.hdf5_reader import Hdf5RunReader, iter_recipe_spectrum_sweeps
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.helpers import loaded_settings


def audit_settings(tmp_path):
    raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
    moke = raw["devices"]["moke_box"]
    # Deliberately synthetic qualification, never a real hardware approval.
    moke["voltage_control"]["channel"] = 0
    moke["channel_profiles"].pop("0", None)
    moke["allowed_vout_channels"] = (0,)
    moke["calibration_directory"] = str(tmp_path / "calibration")
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    raw["devices"]["anritsu"]["advanced_spectrum"] = {
        "control_protocol": "standard_scpi", "qualified_firmware": ["sim-1.0"],
    }
    for name, span, trip, source_range in (
        ("A", "2 mA", "3 mA", "10 mA"),
        ("B", "100 mA", "110 mA", "100 mA"),
    ):
        channel = raw["devices"]["keithley"]["safety"]["channels"][name]
        channel["enabled"] = True
        channel["defaults"]["source_range"] = source_range
        channel["lab_limits"]["source_current"].update(min=f"-{span}", max=span, max_abs=span)
        channel["lab_limits"]["measured_current_trip"].update(min=f"-{trip}", max=trip)
        channel["lab_limits"]["max_abs_power"] = "10 mW"
    return StationSettings.model_validate(raw)


def compile_source(settings, children):
    return RecipeCompiler(settings).compile(parse_recipe_text(
        "schema_version: 1\nname: audit-probe\nroot:\n  id: root\n  type: sequence\n  children:\n" + children
    ))


KEITHLEY_RAMP = """    - {id: source, type: configure_keithley, channel: A, mode: current, level: '0 mA', compliance: '20 mV', source_range: '10 mA'}
    - {id: enable, type: set_keithley_output, channel: A, enabled: true}
    - {id: approach, type: ramp_keithley_current, channel: A, level: '-0.2 mA', step: '100 uA', settle_time: '2 ms'}
    - {id: trace, type: acquire_spectrum, store_source_spectra: false}
    - {id: return, type: ramp_keithley_current, channel: A, level: '0 mA', step: '100 uA', settle_time: '2 ms'}
    - {id: disable, type: set_keithley_output, channel: A, enabled: false}
"""


def test_keithley_current_ramp_is_one_action_and_spectrum_has_ramped_setpoint(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, KEITHLEY_RAMP)
    assert len(plan.actions) == 6 and plan.total_spectra == plan.total_points == 1
    calls = []
    ramp_to_level = KeithleyAdapter.ramp_to_level

    def record_ramp(adapter, request, cancellation_token=None):
        assert cancellation_token is not None
        result = ramp_to_level(adapter, request, cancellation_token)
        calls.append((result, request))
        return result

    monkeypatch.setattr(KeithleyAdapter, "ramp_to_level", record_ramp)
    path = tmp_path / "ramped-point.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    assert result.stored_points == 1
    assert [ramp.target_si for ramp, _ in calls] == pytest.approx([-0.0002, 0.0])
    for ramp, request in calls:
        assert ramp.channel == "A" and ramp.final_measurement.output_enabled
        assert request.settle_time_s == pytest.approx(0.002)
        assert all(abs(level - previous) <= 0.0001 + 1e-12 for previous, level in zip(
            (ramp.start_si, *ramp.levels_si), ramp.levels_si,
        ))
    point, = Hdf5RunReader.points(path)
    assert point.setpoints["keithley.A.current"] == pytest.approx(-0.0002)


@pytest.mark.parametrize("cancel", [False, True])
def test_keithley_current_ramp_failure_never_retries_or_stores_a_spectrum(tmp_path, monkeypatch, cancel):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, KEITHLEY_RAMP)
    ramp_attempts = []
    confirmed_shutdowns = []
    emergency_off = KeithleyAdapter.emergency_off

    def fault(adapter, request, cancellation_token=None):
        ramp_attempts.append(request)
        assert adapter.read_output_states()["A"]
        if cancel:
            cancellation_token.set()
        raise DeviceError("Injected ramp fault")

    def capture_shutdown(adapter):
        result = emergency_off(adapter)
        if result:
            confirmed_shutdowns.append(adapter.read_output_states())
        return result

    monkeypatch.setattr(KeithleyAdapter, "ramp_to_level", fault)
    monkeypatch.setattr(KeithleyAdapter, "emergency_off", capture_shutdown)
    result, _ = execute(settings, plan, tmp_path / "failed-ramp.h5", monkeypatch)
    assert result.error and result.stored_points == 0
    assert len(ramp_attempts) == 1
    assert confirmed_shutdowns and all(states == {"A": False, "B": False} for states in confirmed_shutdowns)


@pytest.mark.parametrize(("replacement", "message"), [
    (("enabled: true", "enabled: false"), "OUTPUT ON"),
    (("step: '100 uA'", "step: '100 mA'"), "ramp step"),
    (("level: '-0.2 mA'", "level: '-20 mA'"), "outside"),
    (("settle_time: '2 ms'", "settle_time: '2 s', deadline: '1 s'"), "deadline"),
])
def test_keithley_current_ramp_preflight_rejects_invalid_transitions(tmp_path, replacement, message):
    with pytest.raises((ConfigurationError, SafetyViolation), match=message):
        compile_source(audit_settings(tmp_path), KEITHLEY_RAMP.replace(*replacement, 1))


def test_keithley_sweep_limits_physical_transitions_without_extra_checkpoints(tmp_path, monkeypatch):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["channels"]["A"]["lab_limits"]["ramp_current_step_max"] = "100 uA"
    settings = StationSettings.model_validate(raw)
    plan = compile_source(settings, """    - {id: a-config, type: configure_keithley, channel: A, mode: current, level: '-102 uA', compliance: '20 mV', source_range: '10 mA'}
    - {id: a-on, type: set_keithley_output, channel: A, enabled: true}
    - id: scan
      type: sweep
      target: keithley.A.current
      respect_ramp_step: true
      segments: [{start: '-102 uA', stop: '102 uA', points: 3}]
      children: [{id: record, type: checkpoint}]
""")
    updates = [a.payload["level_si"] for a in plan.actions if a.kind == "update_keithley_level"]
    assert updates == pytest.approx([-102e-6, -51e-6, 0, 51e-6, 102e-6])
    checkpoints = [a.setpoints_si["keithley.A.current"] for a in plan.actions if a.kind == "checkpoint"]
    assert checkpoints == pytest.approx([-102e-6, 0, 102e-6])
    assert plan.total_points == 3
    result, _ = execute(settings, plan, tmp_path / "bounded-transition.h5", monkeypatch)
    assert result.error is None
    assert result.stored_points == 3


SETUP = """    - {id: analyzer, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '0 dBm', points: 101}
    - {id: a-config, type: configure_keithley, channel: A, mode: current, level: '0.2 mA', compliance: '20 mV', source_range: '10 mA'}
    - {id: b-config, type: configure_keithley, channel: B, mode: current, level: '-80 mA', compliance: '20 mV', source_range: '100 mA'}
    - {id: a-on, type: set_keithley_output, channel: A, enabled: true}
    - {id: b-on, type: set_keithley_output, channel: B, enabled: true}
    - {id: moke-config, type: configure_moke_box, channel: 0, minimum_voltage: '0 mV', maximum_voltage: '100 mV'}
    - {id: moke-arm, type: arm_moke_voltage}
"""

AXES = """    - id: moke-axis
      type: sweep
      target: moke_box.vout0.voltage
      start: '0 mV'
      stop: '100 mV'
      points: 2
      children:
        - id: b-axis
          type: sweep
          target: keithley.B.current
          start: '-80 mA'
          stop: '80 mA'
          points: 3
          children:
            - id: a-axis
              type: sweep
              target: keithley.A.current
              start: '0.2 mA'
              stop: '1.4 mA'
              points: 3
              children:
                - {id: settle, type: wait, duration: '3 s'}
                - {id: measure-a, type: measure_keithley, channel: A}
                - {id: measure-b, type: measure_keithley, channel: B}
                - {id: hall, type: measure_moke_hall, checkpoint: false}
                - {id: spectrum, type: acquire_spectrum, average_count: 1}
"""


def execute(settings, plan, path, monkeypatch, *, keithley_resistance_ohm=None):
    composition = StationComposition(settings, simulation=True)
    devices = {name: composition.create_adapter(name) for name in ("rigol", "keithley", "anritsu", "moke_box")}
    for adapter in devices.values():
        adapter.connect()
    if keithley_resistance_ohm is not None:
        session = devices["keithley"]._require_session()
        session.resistance_ohm = dict.fromkeys(("smua", "smub"), keithley_resistance_ohm)
    writer = Hdf5RunWriter(path, recipe_source=plan.recipe_source,
        settings_source=settings.model_dump_json(), plan_hash=plan.sha256,
        expected_points=plan.total_points, device_idn={name: adapter.identity.idn for name, adapter in devices.items()},
        allow_skipped_checkpoints=plan.may_skip_compliance_points,
        device_capabilities={name: adapter.capabilities for name, adapter in devices.items()},
        simulation_metadata={"enabled": True}, csv_summary_path=path.with_suffix(".csv"))
    runner = RecipeRunner(rigol=devices["rigol"], keithley=devices["keithley"],
        anritsu=devices["anritsu"], moke_box=devices["moke_box"], writer=writer)
    # Sequence evidence without waiting 54 seconds; real wait duration is tested separately.
    waits = []
    monkeypatch.setattr(runner, "_interruptible_wait", waits.append)
    try:
        result = runner.run(plan)
    finally:
        for adapter in devices.values():
            adapter.disconnect()
    return result, waits


@pytest.mark.parametrize("polarity", [1, -1])
@pytest.mark.parametrize("policy", ["warn_clamp", "stop", "measured_trip"])
def test_current_sweep_compliance_skips_outward_points_and_takes_bounded_return(
    tmp_path, monkeypatch, polarity, policy,
):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["keithley"]["safety"].update(
        compliance_policy="stop" if policy == "stop" else "warn_clamp",
        stop_on_compliance=policy == "stop",
    )
    raw["devices"]["keithley"]["safety"]["channels"]["A"]["lab_limits"]["ramp_current_step_max"] = "100 uA"
    settings = StationSettings.model_validate(raw)
    commands = []
    original_write = KeithleySimulator._write

    def record_write(simulator, command):
        simulator.resistance_ohm["smua"] = 100.0
        if simulator.output["smua"] and command.startswith("smua.source.leveli = "):
            commands.append(float(command.rsplit(" ", 1)[-1]))
        return original_write(simulator, command)

    monkeypatch.setattr(KeithleySimulator, "_write", record_write)
    original_measure = KeithleySimulator._measured_iv

    def trip_measurement(simulator, smu):
        voltage, current = original_measure(simulator, smu)
        if smu == "smua" and abs(simulator.level[smu]) >= 0.0002:
            current = polarity * 0.004
        return voltage, current

    if policy == "measured_trip":
        monkeypatch.setattr(KeithleySimulator, "_measured_iv", trip_measurement)
    plan = compile_source(settings, f"""    - {{id: cfg, type: configure_keithley, channel: A, mode: current, level: '0 uA', compliance: '20 mV', source_range: '10 mA'}}
    - {{id: on, type: set_keithley_output, channel: A, enabled: true}}
    - id: scan
      type: sweep
      target: keithley.A.current
      respect_ramp_step: true
      segments:
      - {{value: '0 uA'}}
      - {{value: '{100 * polarity} uA'}}
      - {{value: '{300 * polarity} uA'}}
      - {{value: '{500 * polarity} uA'}}
      - {{value: '{100 * polarity} uA'}}
      children:
      - {{id: trace, type: acquire_spectrum, store_source_spectra: false}}
    - {{id: off, type: ramp_keithley_to_zero, channel: A}}
""")
    path = tmp_path / "clipped.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert all(abs(level) <= 0.0002 + 1e-12 for level in commands)
    assert all(abs(b - a) <= 0.0001 + 1e-12 for a, b in zip([0.0, *commands], commands))
    if policy == "stop":
        assert result.error and "final checkpoint" in result.error
        assert result.stored_points == 3
        return
    if policy == "measured_trip":
        assert result.error and "measurement safety trip" in result.error
        assert result.stored_points == 2
        return
    assert result.error is None
    assert result.compliance_points_skipped == 2
    assert result.completed_actions == len(plan.actions)
    stored = Hdf5RunReader.points(path)
    assert [point.setpoints["keithley.A.current"] for point in stored] == pytest.approx(
        [0, polarity * 0.0001, polarity * 0.0001],
    )
    with h5py.File(path) as file:
        events = file["events/name"].asstr()[:].tolist()
        assert "sweep_compliance_limited" in events
        assert "run_fault" not in events
        assert file["scan_definition"].attrs["allow_skipped_checkpoints"]
    assert ThatecCompatibilityValidator().validate(path).valid


@pytest.mark.parametrize("hit_mid_scan", [False, True])
def test_unreachable_b_bias_skips_dependent_scan_and_allows_next_bias(tmp_path, monkeypatch, hit_mid_scan):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]["ramp_current_step_max"] = "100 uA"
    settings = StationSettings.model_validate(raw)
    written_b = []
    original_write = KeithleySimulator._write

    def record_write(simulator, command):
        if not hit_mid_scan or (
            command.startswith("smua.source.leveli = 0.0001") and simulator.level["smub"] >= 0.0004
        ):
            simulator.resistance_ohm["smub"] = 100.0
        if simulator.output["smub"] and command.startswith("smub.source.leveli = "):
            written_b.append(float(command.rsplit(" ", 1)[-1]))
        return original_write(simulator, command)

    monkeypatch.setattr(KeithleySimulator, "_write", record_write)
    initial = """    - {id: cfg-a, type: configure_keithley, channel: A, mode: current, level: '0 uA', compliance: '20 mV', source_range: '10 mA'}
    - {id: cfg-b, type: configure_keithley, channel: B, mode: current, level: '0 uA', compliance: '20 mV', source_range: '100 mA'}
    - {id: on-a, type: set_keithley_output, channel: A, enabled: true}
    - {id: on-b, type: set_keithley_output, channel: B, enabled: true}
"""
    for group, bias in (("first", 400), ("further", 500), ("return", 100)):
        initial += f"""    - id: {group}
      type: sequence
      children:
      - {{id: {group}-bias, type: ramp_keithley_current, channel: B, level: '{bias} uA', step: '100 uA', settle_time: '2 ms'}}
      - {{id: {group}-approach, type: ramp_keithley_current, channel: A, level: '0 uA', step: '100 uA', settle_time: '2 ms'}}
      - id: {group}-scan
        type: sweep
        target: keithley.A.current
        segments: [{{start: '0 uA', stop: '100 uA', points: 2, spacing: linear}}]
        children:
        - {{id: {group}-trace, type: acquire_spectrum, store_source_spectra: false}}
      - {{id: {group}-return, type: ramp_keithley_current, channel: A, level: '0 uA', step: '100 uA', settle_time: '2 ms'}}
"""
    plan = compile_source(settings, initial)
    path = tmp_path / "bias-clipped.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None
    assert result.compliance_points_skipped == (3 if hit_mid_scan else 4)
    forward = [0.0001, 0.0002, 0.0003, 0.0004] if hit_mid_scan else [0.0001, 0.0002]
    assert written_b == pytest.approx([*forward, *reversed(forward[:-1])])
    stored = Hdf5RunReader.points(path)
    expected_b = [0.0004, 0.0001, 0.0001] if hit_mid_scan else [0.0001, 0.0001]
    expected_a = [0, 0, 0.0001] if hit_mid_scan else [0, 0.0001]
    assert [point.setpoints["keithley.B.current"] for point in stored] == pytest.approx(expected_b)
    assert [point.setpoints["keithley.A.current"] for point in stored] == pytest.approx(expected_a)


def test_requested_three_axis_topology_and_durable_simulated_archive(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP + AXES)
    assert plan.total_spectra == plan.total_points == 18
    assert sum(action.kind == "configure_keithley" for action in plan.actions) == 2
    assert sum(action.kind == "update_moke_voltage" for action in plan.actions) == 2
    assert sum(action.kind == "update_keithley_level" and action.payload["channel"] == "B" for action in plan.actions) == 6
    assert sum(action.kind == "update_keithley_level" and action.payload["channel"] == "A" for action in plan.actions) == 18
    path = tmp_path / "cartesian.h5"
    # Keep this storage/topology fixture inside the 20 mV compliance envelope.
    result, waits = execute(settings, plan, path, monkeypatch, keithley_resistance_ohm=0.1)
    assert result.error is None, result.error
    assert result.stored_points == 18 and waits == [3.0] * 18
    expected = list(product((0.0, 0.1), (-0.08, 0.0, 0.08), (0.0002, 0.0008, 0.0014)))
    with h5py.File(path) as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["points"]) == len(file["spectra"]) == 18
        assert len(file["_pending"]) == 0
        for index, (moke, b, a) in enumerate(expected):
            row = file[f"points/{index}"]
            setpoints = json.loads(row["setpoints_json"].asstr()[()])
            measurements = json.loads(row["measurements_json"].asstr()[()])
            assert setpoints["keithley.A.current"] == pytest.approx(a)
            assert setpoints["keithley.B.current"] == pytest.approx(b)
            # Existing adapter acceptance is 1 mV; exact DAC behavior is a
            # separate expected-failure contract below.
            assert abs(setpoints["moke_box.vout0.voltage"] - moke) <= 0.001
            assert "keithley.A.current_a" in measurements and "keithley.B.current_a" in measurements
            assert "moke_box.hall1_voltage_v" in measurements
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 18
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


def test_moke_ramp_reaches_its_exact_applied_dac_target(tmp_path):
    from app.safety.moke_box import MokeVoltagePlan
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("moke_box")
    adapter.connect()
    profile = adapter.get_control_profile(0)
    plan = MokeVoltagePlan(profile.fingerprint, 0, 0.0, 0.1, (0.1,))
    try:
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        result = adapter.ramp_vout(0, 0.1)
        assert result.actual_v == result.applied_v
    finally:
        adapter.stop_vout()
        adapter.disconnect()


def test_real_three_second_wait_is_interruptible_and_at_least_three_seconds():
    import time

    from tests.test_adapters_and_runner import MemoryWriter, ShutdownProbe
    runner = RecipeRunner(rigol=ShutdownProbe(), keithley=ShutdownProbe(),
                          anritsu=ShutdownProbe(), writer=MemoryWriter())
    started = time.monotonic()
    runner._interruptible_wait(3.0)
    assert 3.0 <= time.monotonic() - started < 4.0
    runner.request_stop()
    started = time.monotonic()
    with pytest.raises(ExecutionError):
        runner._interruptible_wait(3.0)
    assert time.monotonic() - started < 0.1


def test_approved_secondary_moke_channel_is_usable_in_sweep(tmp_path):
    raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
    moke = raw["devices"]["moke_box"]
    moke["channel_profiles"]["0"].update(approved=True, qualification_reference="SIMULATION ONLY")
    moke["test_vout_channels"] = (0,)
    moke["allowed_vout_channels"] = (2, 0)
    moke["calibration_directory"] = str(tmp_path / "calibration")
    settings = StationSettings.model_validate(raw)
    composition = StationComposition(settings, simulation=True)
    adapter = composition.create_adapter("moke_box")
    adapter.connect()
    try:
        assert adapter.get_control_profile(0).channel == 0
    finally:
        adapter.disconnect()
    compile_source(settings, """    - {id: config, type: configure_moke_box, channel: 0, minimum_voltage: '0 V', maximum_voltage: '0.1 V'}
    - {id: arm, type: arm_moke_voltage}
    - {id: axis, type: sweep, target: moke_box.vout0.voltage, start: '0 V', stop: '0.1 V', points: 2, children: [{id: checkpoint, type: checkpoint}]}
""")


def test_analyzer_reference_axis_preserves_frequency_and_points(tmp_path):
    plan = compile_source(audit_settings(tmp_path), """    - {id: config, type: configure_anritsu, start_frequency: '300 MHz', stop_frequency: '6 GHz', reference_level: '-10 dBm', points: 10001}
    - id: axis
      type: sweep
      target: anritsu.spectrum.reference_level
      start: '-10 dBm'
      stop: '-5 dBm'
      points: 2
      children: [{id: checkpoint, type: checkpoint}]
""")
    for action in plan.actions[1:]:
        if action.kind == "configure_anritsu":
            config = action.payload["config"]
            assert (config.start_hz, config.stop_hz, config.points) == (300e6, 6e9, 10001)


def test_a_axis_does_not_drop_explicit_b_update(tmp_path):
    plan = compile_source(audit_settings(tmp_path), SETUP[:SETUP.index("    - {id: moke-config")] + """    - id: axis
      type: sweep
      target: keithley.A.current
      start: '0.2 mA'
      stop: '1.4 mA'
      points: 2
      children:
        - {id: explicit-b, type: update_keithley_level, channel: B, mode: current, level: '2 mA'}
        - {id: checkpoint, type: checkpoint}
""")
    assert sum(action.kind == "update_keithley_level" and action.payload["channel"] == "B" for action in plan.actions) == 2


def test_duplicate_active_axis_is_rejected_by_compiler(tmp_path):
    source = """schema_version: 1
name: duplicate
root:
  id: outer
  type: sweep
  target: keithley.B.current
  start: '0 A'
  stop: '1 mA'
  points: 2
  children:
    - id: inner
      type: sweep
      target: keithley.B.current
      start: '0 A'
      stop: '2 mA'
      points: 2
      children: [{id: checkpoint, type: checkpoint}]
"""
    recipe = parse_recipe_text(source)
    with pytest.raises(ConfigurationError, match="duplicate active"):
        normalize_recipe_tree(recipe, built_in_device_registry().sweep_providers())
    with pytest.raises(ConfigurationError, match="duplicate active"):
        RecipeCompiler(audit_settings(tmp_path)).compile(recipe)


def test_unconfigured_source_axis_cannot_claim_measurements(tmp_path):
    with pytest.raises(ConfigurationError):
        compile_source(audit_settings(tmp_path), """    - {id: axis, type: sweep, target: keithley.B.current, start: '0 A', stop: '1 mA', points: 2, children: [{id: checkpoint, type: checkpoint}]}
""")


def test_return_path_has_correct_point_indices(tmp_path):
    plan = compile_source(audit_settings(tmp_path), """    - {id: config, type: configure_keithley, channel: B, mode: current, level: '0 A', compliance: '20 mV', source_range: '100 mA'}
    - id: axis
      type: sweep
      target: keithley.B.current
      segments:
        - {start: '0 A', stop: '1 mA', points: 3}
        - {start: '1 mA', stop: '0 A', points: 3}
      children: [{id: checkpoint, type: checkpoint}]
""")
    contexts = [action.axis_context for action in plan.actions if action.kind == "update_keithley_level"]
    assert [context.point_index for context in contexts] == [0, 1, 2, 3, 4]
    assert [context.stage_index for context in contexts] == [0, 0, 0, 1, 1]


def test_maximum_expanded_points_is_enforced(tmp_path):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["execution"]["max_expanded_points"] = 2
    with pytest.raises(SafetyViolation):
        compile_source(StationSettings.model_validate(raw), """    - {id: axis, type: sweep, target: keithley.B.current, start: '0 A', stop: '1 mA', points: 3, children: [{id: checkpoint, type: checkpoint}]}
""")


@pytest.mark.parametrize("value", [2.5, "3"])
def test_roi_point_count_is_a_strict_integer(value):
    with pytest.raises(ConfigurationError):
        generate_sweep_points([{"start": "0 A", "stop": "1 mA", "points": value}], "current")


def test_raw_source_setpoints_match_checkpoint_readback(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP + AXES)
    path = tmp_path / "readback.h5"
    result, _ = execute(settings, plan, path, monkeypatch, keithley_resistance_ohm=0.1)
    assert result.error is None
    raw = list(iter_recipe_spectrum_sweeps(path))[-1][2]
    with h5py.File(path) as file:
        setpoints = json.loads(file["points/17/setpoints_json"].asstr()[()])
    assert raw.setpoint_metadata_version == 2
    assert dict(raw.setpoints_si)["moke_box.vout0.voltage"] == 0.1
    assert dict(raw.requested_setpoints_si)["moke_box.vout0.voltage"] == 0.1
    assert dict(raw.readback_setpoints_si)["moke_box.vout0.voltage"] == setpoints["moke_box.vout0.voltage"]


def test_checkpoint_keeps_confirmed_current_axis_readback(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP + AXES)
    path = tmp_path / "semantic-readback.h5"
    result, _ = execute(settings, plan, path, monkeypatch, keithley_resistance_ohm=0.1)
    assert result.error is None
    with h5py.File(path) as file:
        metadata = json.loads(file["points/17/metadata_json"].asstr()[()])
    assert metadata["applied_si"] == pytest.approx(0.0014)
    assert metadata["readback_si"] == pytest.approx(0.0014)


def test_explicit_analyzer_axis_executes_with_production_simulated_adapter(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: config, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '-10 dBm', points: 101}
    - {id: axis, type: sweep, target: anritsu.spectrum.reference_level, start: '-10 dBm', stop: '-5 dBm', points: 2, children: [{id: spectrum, type: acquire_spectrum}]}
""")
    result, _ = execute(settings, plan, tmp_path / "analyzer-axis.h5", monkeypatch)
    assert result.error is None, result.error


def test_settling_axis_is_preserved_as_actual_wait_value(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: baseline, type: configure_keithley, channel: B, mode: current, level: '1 mA', compliance: '20 mV', source_range: '100 mA'}
    - id: device
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: B
      source_mode: current
      output_policy: off
      configuration: {channel: B, source_mode: current, source_level: '1 mA', compliance: '20 mV', source_range: '100 mA', settling_time: '1 s'}
      parameter_actions:
        - {parameter_id: measurement.settling_time, mode: sweep, segments: [{start: '1 s', stop: '3 s', points: 2}]}
      children: [{id: checkpoint, type: checkpoint}]
""")
    result, waits = execute(settings, plan, tmp_path / "settling-axis.h5", monkeypatch)
    assert result.error is None and waits == [1.0, 3.0]
    with h5py.File(tmp_path / "settling-axis.h5") as file:
        values = [json.loads(file[f"points/{index}/setpoints_json"].asstr()[()])["keithley.B.settling_time"] for index in range(2)]
    assert values == [1.0, 3.0]


def test_recovery_counts_sensor_checkpoints(tmp_path):
    from app.engine.recovery import RunRecoveryManager
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, "    - {id: hall, type: measure_moke_hall}\n")
    assert plan.total_points == 1
    path = tmp_path / "sensor-recovery.h5"
    with h5py.File(path, "w") as file:
        run = file.create_group("run")
        run.attrs.update(plan_sha256=plan.sha256, status="aborted")
        file.create_group("points/0").attrs["complete"] = True
        events = file.create_group("events")
        events.create_dataset("name", data=["safe_resume_boundary"], dtype=h5py.string_dtype())
        events.create_dataset("message", data=[json.dumps({"stored_points": 1, "next_action_index": 1, "plan_sha256": plan.sha256})], dtype=h5py.string_dtype())
    assert RunRecoveryManager().inspect(path, plan).stored_points == 1


def test_selecting_detector_does_not_reset_other_advanced_settings(tmp_path):
    plan = compile_source(audit_settings(tmp_path), """    - id: analyzer
      type: sequence
      device_module: anritsu
      operation: configure_selected_parameters
      configuration: {start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '-10 dBm', points: 101}
      parameter_actions: [{parameter_id: advanced.detector, mode: set, value: POS}]
      children: []
""")
    action = next(action for action in plan.actions if action.kind == "configure_anritsu_advanced")
    config = action.payload["config"]
    assert config.rbw_auto is None and config.attenuation_auto is None


def test_misspelled_settling_time_is_rejected(tmp_path):
    with pytest.raises(ConfigurationError):
        compile_source(audit_settings(tmp_path), "    - {id: config, type: configure_keithley, channel: B, mode: current, level: '1 mA', compliance: '20 mV', source_range: '100 mA', settlng_time: '3 s'}\n")


def test_compiled_moke_applied_value_matches_prepared_working_envelope(tmp_path):
    plan = compile_source(audit_settings(tmp_path), SETUP + AXES)
    prepared = next(action.payload["plan"] for action in plan.actions if action.kind == "configure_moke_box")
    action = [action for action in plan.actions if action.kind == "update_moke_voltage"][-1]
    assert action.payload["applied_si"] == prepared.applied_voltage(action.payload["voltage_v"])


@pytest.mark.parametrize("key,expected", [
    ("moke_box.field_estimated_ascending_t", "T"),
    ("keithley.A.compliance_stop_required", ""),
    ("anritsu.sg.power", "dBm"),
])
def test_public_scalar_units_match_quantity_meaning(key, expected):
    from app.storage.thatec_writer import ThatecHdf5Writer
    assert ThatecHdf5Writer._describe_quantity("measurement", key)[2] == expected


def test_archive_upper_estimate_covers_actual_simulated_archive(tmp_path, monkeypatch):
    from app.engine.estimation import PlanEstimator
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP + AXES)
    path = tmp_path / "estimate.h5"
    result, _ = execute(settings, plan, path, monkeypatch, keithley_resistance_ohm=0.1)
    assert result.error is None
    assert PlanEstimator(settings).estimate(plan).uncompressed_hdf5_bytes >= path.stat().st_size


def test_disconnected_sources_do_not_establish_confirmed_safe_shutdown(tmp_path):
    from dataclasses import replace
    from app.domain.models import ApplicationState
    from tests.test_adapters_and_runner import MemoryWriter
    settings = audit_settings(tmp_path)
    composition = StationComposition(settings, simulation=True)
    devices = {name: composition.create_adapter(name) for name in ("rigol", "keithley", "anritsu")}
    plan = compile_source(settings, "    - {id: checkpoint, type: checkpoint}\n")
    # Only sources declared by the run belong to its shutdown scope.
    plan = replace(plan, required_devices=frozenset(devices))
    result = RecipeRunner(rigol=devices["rigol"], keithley=devices["keithley"],
                          anritsu=devices["anritsu"], writer=MemoryWriter()).run(plan)
    assert result.state is not ApplicationState.SAFE


def test_flush_failure_rolls_back_memory_count(tmp_path, monkeypatch):
    path = tmp_path / "flush-fault.h5"
    writer = Hdf5RunWriter(path, recipe_source="", settings_source="", plan_hash="audit", device_idn={})
    original = h5py.File.flush
    calls = 0
    def fault(file):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected post-count flush failure")
        return original(file)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(h5py.File, "flush", fault)
            with pytest.raises(ExecutionError, match="injected"):
                writer.append(MeasurementPoint(index=0, setpoints={}, measurements={}))
        assert len(writer._file["points"]) == 0
        assert writer.point_count == 0
    finally:
        writer.close("faulted")
