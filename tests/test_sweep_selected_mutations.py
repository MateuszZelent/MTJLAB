"""Command-level regression contracts for selected fields and acquisition dwell."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from app.bootstrap import StationComposition
from app.domain.errors import ConfigurationError, SafetyViolation
from app.engine.estimation import require_storage_capacity
from app.recipes.sweep_points import estimate_sweep_point_count, generate_sweep_points
from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps
from tests.test_sweep_audit_contracts import SETUP, audit_settings, compile_source, execute


def test_selected_keithley_level_preserves_full_baseline_and_commands(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: baseline, type: configure_keithley, channel: B, mode: current, level: '1 mA', compliance: '20 mV', nplc: 4, source_range: '100 mA'}
    - id: selected
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: B
      source_mode: current
      configuration: {channel: B, source_mode: current, source_level: '9 mA', compliance: '50 mV', nplc: 1, source_range: '1 A'}
      parameter_actions: [{parameter_id: source.level, mode: set, value: '2 mA'}]
""")
    baseline, selected = [a.payload["request"] for a in plan.actions if a.kind == "configure_keithley"]
    assert selected.changed_fields == ("level_si",)
    assert selected.nplc == baseline.nplc == 4
    assert selected.compliance_si == baseline.compliance_si == .02
    assert selected.source_range_si == baseline.source_range_si == .1
    adapter = StationComposition(settings, simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        adapter.configure_source(baseline)
        session = adapter._require_session()
        session.commands.clear()
        applied = adapter.configure_source(selected)
        mutations = [command for command in session.commands if " = " in command]
        assert mutations == ["smub.source.output = smub.OUTPUT_OFF", "smub.source.leveli = 0.002"]
        assert applied == replace(baseline, level_si=.002, changed_fields=None)
    finally:
        adapter.disconnect()


def test_selected_parameters_cannot_establish_hidden_baseline(tmp_path):
    settings = audit_settings(tmp_path)
    with pytest.raises(ConfigurationError, match="explicit.*baseline"):
        compile_source(settings, """    - id: selected
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: B
      source_mode: current
      configuration: {channel: B, source_mode: current, source_level: '1 mA', compliance: '20 mV', source_range: '100 mA'}
      parameter_actions: [{parameter_id: source.level, mode: set, value: '2 mA'}]
""")


def test_literal_rigol_configuration_preserves_omitted_phase_load_and_advanced_modes(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, "    - {id: carrier, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}\n")
    config = plan.actions[0].payload["config"]
    assert set(config.changed_fields) == {"waveform", "frequency_hz", "high_level_v", "low_level_v"}
    adapter = StationComposition(settings, simulation=True).create_adapter("rigol")
    adapter.connect()
    try:
        session = adapter._require_session()
        session.write(":OUTP1:LOAD 50")
        session.write(":SOUR1:PHAS 37")
        session.commands.clear()
        adapter.configure_channel(config)
        applied = adapter.last_channel_config(1)
        assert applied.phase_deg == 37 and float(applied.output_load) == 50
        mutations = [command for command in session.commands if not command.endswith("?")]
        assert not any(token in command for command in mutations for token in (":LOAD", ":PHAS", ":MOD", ":SWE:STAT", ":BURS", ":HARM", ":SUM", ":VOLT:UNIT"))
        session.write(":SOUR1:MOD ON")
        session.commands.clear()
        with pytest.raises(SafetyViolation, match="advanced mode"):
            adapter.configure_channel(config)
        assert not any(not command.endswith("?") for command in session.commands)
    finally:
        adapter.disconnect()


def test_literal_baseline_preserves_unspecified_hardware_fields(tmp_path):
    from app.safety.keithley import KeithleySourceRequest

    settings = audit_settings(tmp_path)
    plan = compile_source(settings, "    - {id: authored, type: configure_keithley, channel: B, mode: current, level: '1 mA', compliance: '20 mV', source_range: '100 mA'}\n")
    requested = plan.actions[0].payload["request"]
    assert "nplc" not in requested.changed_fields
    assert "sense_mode" not in requested.changed_fields
    adapter = StationComposition(settings, simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        adapter.configure_source(KeithleySourceRequest("B", "voltage", .01, .001,
            nplc=8, sense_mode="2wire", source_range_si=.1))
        session = adapter._require_session()
        session.commands.clear()
        applied = adapter.configure_source(requested)
        assert applied.mode == "current"
        assert applied.nplc == 8 and applied.sense_mode == "2wire"
        mutations = [command for command in session.commands if " = " in command]
        assert not any(".nplc" in command or ".sense" in command or ".measure." in command for command in mutations)
    finally:
        adapter.disconnect()


def test_average_waits_only_between_raw_frames_and_persists_policy(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP.splitlines()[0] + "\n    - {id: averaged, type: acquire_spectrum, average_count: 3, inter_sweep_delay: '3 s'}\n")
    path = tmp_path / "average-delay.h5"
    result, waits = execute(settings, plan, path, monkeypatch)
    assert result.error is None
    assert waits == [3.0, 3.0]
    frames = tuple(frame for _index, _point_index, frame in iter_recipe_spectrum_sweeps(path))
    assert len(frames) == 3
    assert [frame.inter_sweep_delay_s for frame in frames] == [3.0] * 3


def test_repeated_checkpoints_rejected_before_expansion(tmp_path):
    settings = audit_settings(tmp_path)
    settings.execution["max_expanded_points"] = 10
    with pytest.raises(SafetyViolation, match="before allocation"):
        compile_source(settings, """    - id: many
      type: repeat
      count: 11
      children: [{id: stored, type: checkpoint}]
""")


def test_legacy_level_alias_is_current_and_cannot_form_a_second_axis(tmp_path):
    settings = audit_settings(tmp_path)
    baseline = "    - {id: baseline, type: configure_keithley, channel: B, mode: current, level: '1 mA', compliance: '20 mV', source_range: '100 mA'}\n"
    source = """    - id: alias
      type: sweep
      target: keithley.B.level
      start: 1 mA
      stop: 2 mA
      points: 2
      children: [{id: checkpoint, type: checkpoint}]
"""
    plan = compile_source(settings, baseline + source)
    assert all(action.payload["mode"] == "current" for action in plan.actions if action.kind == "update_keithley_level")
    duplicate = source.replace("      children: [{id: checkpoint, type: checkpoint}]", """      children:
        - id: same-physical-control
          type: sweep
          target: keithley.B.current
          start: 1 mA
          stop: 2 mA
          points: 2
          children: [{id: checkpoint, type: checkpoint}]""")
    with pytest.raises(ConfigurationError, match="duplicate active sweep"):
        compile_source(settings, baseline + duplicate)


def test_recovery_restores_actual_unspecified_fields_instead_of_request_defaults(tmp_path):
    from app.engine.recovery import RunRecoveryManager
    from app.safety.keithley import KeithleySourceRequest
    from app.domain.errors import ExecutionError

    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: baseline, type: configure_keithley, channel: B, mode: current, level: '1 mA', compliance: '20 mV', source_range: '100 mA'}
    - {id: update, type: update_keithley_level, channel: B, mode: current, level: '1.2 mA'}
""")
    with pytest.raises(ExecutionError, match="confirmed Keithley"):
        RunRecoveryManager._configuration_prelude(plan, len(plan.actions))
    adapter = StationComposition(settings, simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        baseline = KeithleySourceRequest("B", "current", .001, .02,
            nplc=8, sense_mode="2wire", source_range_si=.1)
        actual = adapter.configure_source(baseline)
        adapter.update_source_level("B", mode="current", level_si=.0012)
        actual = replace(actual, level_si=.0012)
        state = {**asdict(actual), "source_level_si": actual.level_si}
        prelude = RunRecoveryManager._configuration_prelude(plan, len(plan.actions), {
            "keithley": {"channel_B": {"actual": state}},
        })
        restored = prelude[0].payload["request"]
        assert restored == actual
        assert restored.nplc == 8 and restored.sense_mode == "2wire"
        adapter.configure_source(replace(actual, nplc=1, sense_mode="2wire"))
        assert adapter.configure_source(restored) == actual
    finally:
        adapter.disconnect()


def test_literal_nested_overwrite_invalidates_outer_axis_confirmation(tmp_path, monkeypatch):
    import h5py
    import json
    from tests.test_sweep_audit_contracts import AXES

    settings = audit_settings(tmp_path)
    axes = AXES.replace("points: 3", "points: 2").replace(
        "                - {id: settle,", "                - {id: explicit-b, type: update_keithley_level, channel: B, mode: current, level: '1 mA'}\n                - {id: settle,")
    plan = compile_source(settings, SETUP + axes)
    path = tmp_path / "overwrite.h5"
    result, _waits = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    with h5py.File(path) as file:
        for index in range(8):
            metadata = json.loads(file[f"points/{index}/metadata_json"].asstr()[()])
            confirmations = metadata["axis_confirmations_v1"]
            assert not any(proof["target"] == "keithley.B.current" for proof in confirmations.values())
            proof = metadata["setpoint_evidence_v1"]["keithley.B.current"]
            # The literal action explicitly supersedes the outer axis command.
            assert proof["requested_si"] == pytest.approx(.001)
            assert proof["readback_si"] == pytest.approx(.001)
            assert proof["confirmed_at_utc"]


def test_disk_preflight_resolves_existing_ancestor_and_rejects_low_space(tmp_path, monkeypatch):
    calls = []
    def usage(path):
        calls.append(path)
        return SimpleNamespace(free=100)
    monkeypatch.setattr("app.engine.estimation.shutil.disk_usage", usage)
    with pytest.raises(ConfigurationError, match="Insufficient free space"):
        require_storage_capacity(tmp_path / "new-directory" / "run.h5", 200)
    assert calls == [tmp_path.resolve()]


@pytest.mark.parametrize("segment", [
    {"start": "0 A", "stop": "1e-16 A", "step": "2e-17 A"},
    {"start": "0.1 mA", "stop": "0.3 mA", "step": "0.1 mA"},
    {"start": "0.3 mA", "stop": "0.1 mA", "step": "0.1 mA"},
])
def test_step_preflight_and_generation_agree_at_si_boundaries(segment):
    from app.domain.quantities import parse_quantity
    values = generate_sweep_points([segment], "current")
    assert len(values) == estimate_sweep_point_count([segment], "current")
    assert values[0] == parse_quantity(segment["start"], "current")
    assert values[-1] == parse_quantity(segment["stop"], "current")
    assert all(left != right for left, right in zip(values, values[1:]))


@pytest.mark.parametrize("spacing,start,stop", [
    ("linear", "0.2 mA", "1.4 mA"), ("linear", "1e308 A", "-1e308 A"),
    ("log", "1e-308 A", "1e308 A"), ("log", "1e308 A", "1e-308 A"),
])
def test_count_generator_keeps_exact_endpoints_without_overflow(spacing, start, stop):
    import math
    from app.domain.quantities import parse_quantity
    values = generate_sweep_points([{"start": start, "stop": stop, "points": 7, "spacing": spacing}], "current")
    assert values[0] == parse_quantity(start, "current")
    assert values[-1] == parse_quantity(stop, "current")
    assert all(math.isfinite(value.si_value) for value in values)


def test_generator_cancellation_is_observed_during_axis_allocation():
    from app.domain.errors import ExecutionError
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 4
    with pytest.raises(ExecutionError, match="cancelled"):
        generate_sweep_points([{"start": "0 A", "stop": "1 A", "points": 100_000}],
            "current", cancellation_requested=cancel)
    assert calls == 4


def test_nearby_stage_boundary_is_not_silently_deduplicated():
    stages = [{"start": "0 A", "stop": "1 A", "points": 2},
              {"start": "1.0000000000001 A", "stop": "2 A", "points": 2}]
    values = generate_sweep_points(stages, "current")
    assert len(values) == estimate_sweep_point_count(stages, "current") == 4
    assert values[1] != values[2]


@pytest.mark.parametrize("channel", [True, 1.1, "1.1", "1"])
def test_rigol_recipe_channels_cannot_be_coerced_or_truncated(tmp_path, channel):
    from app.recipes.models import validate_action_fields
    for kind in ("configure_rigol", "configure_rigol_output", "update_rigol_frequency", "set_rigol_output", "enable_rigol_output"):
        with pytest.raises(ConfigurationError, match="integer 1 or 2"):
            validate_action_fields(kind, {"channel": channel}, "invalid-channel")


def test_independent_estop_attempts_moke_and_preserves_unknown_amplifier_state(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.ui.run_worker import EmergencyStopWorker

    settings = audit_settings(tmp_path)
    settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "moke_box": settings.moke_box.model_copy(update={"enabled": True, "allow_vout_control": True}),
    })})
    moke = StationComposition(settings, simulation=True).create_adapter("moke_box")
    moke.stop_vout = Mock(wraps=moke.stop_vout)
    factory = SimpleNamespace(create_adapter=lambda _name: moke)
    monkeypatch.setattr("app.ui.run_worker.StationComposition", lambda *_args, **_kwargs: factory)
    finished = []
    worker = EmergencyStopWorker(settings, simulation=True)
    worker.finished.connect(finished.append)
    worker.run()
    moke.stop_vout.assert_called_with(0)
    assert len(finished) == 1
    assert any("Kepco power state remains unknown" in message for message in finished[0])
    assert not any("qualified DAC zero was not confirmed" in message for message in finished[0])
