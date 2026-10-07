"""Native command and safety sampling acceptance for production sweeps."""

from dataclasses import replace
import hashlib
import json
import threading

import h5py
import pytest

from app.bootstrap import StationComposition
from app.domain.errors import ConfigurationError
from app.engine.estimation import PlanEstimator
from app.engine.compiler import RecipeCompiler
from app.settings.models import StationSettings
from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps
from app.recipes import parse_recipe_text
from app.recipes.models import RecipeNode
from app.recipes.baseline_authoring import explicit_baseline_mapping
from tests.test_sweep_audit_contracts import SETUP, audit_settings, compile_source, execute


def test_visible_baseline_preserves_native_keithley_aliases():
    node = RecipeNode("source", "sequence", {"device_module": "keithley",
        "operation": "configure_selected_parameters", "configuration": {
            "channel": "A", "mode": "voltage", "level": "10 mV", "source_range": "100 mV",
            "compliance": "1 mA", "settle_time": "3 s", "nplc": 8}})
    baseline = explicit_baseline_mapping(node, node_id="initial")
    assert baseline["mode"] == "voltage" and baseline["level"] == "10 mV"
    assert baseline["settle_time"] == "3 s" and baseline["source_range"] == "100 mV"
    node.data["configuration"]["source_mode"] = "current"
    with pytest.raises(ConfigurationError, match="conflicting aliases"):
        explicit_baseline_mapping(node, node_id="invalid")


@pytest.mark.parametrize("operation", ["none", "difference_db", "subtract_power_signed", "add_power", "subtract_power", "multiply_linear", "ratio_linear"])
def test_builder_acquisition_metadata_roundtrips_without_hidden_acquisition(tmp_path, operation):
    source = f"""schema_version: 1
name: editor-document
root:
  id: analyzer
  type: sequence
  device_module: anritsu
  operation: configure_selected_parameters
  text: Spectrum settings only
  configuration_required: false
  roi_required: false
  acquire_single: false
  managed_acquisition_id: ''
  acquisition_average_count: 3
  acquisition_reference_operation: {operation}
  post_configuration_operation: configure
  parameter_actions: []
  configuration: {{start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '0 dBm', points: 101}}
  children: [{{id: review-point, type: checkpoint}}]
"""
    recipe = parse_recipe_text(source)
    assert RecipeCompiler(audit_settings(tmp_path)).compile(recipe).total_spectra == 0


@pytest.mark.parametrize("field", ["acquire_single: true", "acquisition_average_count: 1.5", "roi_required: 1", "acquisition_average_cout: 3"])
def test_invalid_builder_metadata_is_rejected(field):
    with pytest.raises(ConfigurationError):
        parse_recipe_text("schema_version: 1\nname: invalid\nroot:\n  id: root\n  type: sequence\n  " + field + "\n")


@pytest.mark.parametrize("stored_budget", [1, 1.5, True])
def test_resume_memory_budget_is_checked_before_any_instrument_connection(tmp_path, monkeypatch, stored_budget):
    from unittest.mock import Mock
    from app.engine.recovery import RecoveryCheckpoint
    from app.ui.run_worker import RunWorker
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, "    - {id: point, type: checkpoint}\n")
    path = tmp_path / "resume.h5"
    with h5py.File(path, "w") as file:
        file.create_group("run").attrs["validation_memory_budget_bytes"] = stored_budget
    adapter = Mock()
    monkeypatch.setattr(RunWorker, "_adapter_for_run", lambda *_args: adapter)
    recovery = RecoveryCheckpoint(path, 0, 0, 0, "aborted", ())
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True, recovery=recovery)
    # This test targets the persisted memory budget, so provide a valid run
    # identity to reach that gate without bypassing recovery verification.
    with h5py.File(path, "a") as file:
        file["run"].attrs.update({
            "plan_sha256": plan.sha256,
            "recipe_source_sha256": hashlib.sha256(plan.recipe_source.encode("utf-8")).hexdigest(),
            "settings_sha256": hashlib.sha256(worker._settings_snapshot().encode("utf-8")).hexdigest(),
        })
    errors = []
    worker.failed.connect(errors.append)
    worker.run()
    assert len(errors) == 1 and "memory budget" in errors[0].lower()
    adapter.connect.assert_not_called()


def test_spectrum_baseline_preserves_fields_and_acquisition_prepares_fresh_buffer(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP.splitlines()[0] + "\n")
    adapter = StationComposition(settings, simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        session = adapter._require_session()
        session.write("TRAC1:TYPE VIEW")
        session.write("INIT:MODE:SING")
        session.commands.clear()
        adapter.configure_spectrum(plan.actions[0].payload["config"])
        mutations = [command for command in session.commands if not command.endswith("?")]
        assert not any(command.startswith(("TRAC", "INIT", "BAND", "DET", "POW:ATT")) for command in mutations)
        assert session.trace_write_mode == "VIEW" and not session.continuous_sweep
        session.commands.clear()
        adapter.acquire_single_sweep()
        assert session.trace_write_mode == "WRIT" and not session.continuous_sweep
        assert "TRAC:TYPE?" not in session.commands
        assert session.commands.index("TRAC1:TYPE WRIT") < session.commands.index("INIT:MODE:SING")
        session.write("INIT:MODE:CONT")
        session.commands.clear()
        adapter.acquire_single_sweep()
        assert session.continuous_sweep and session.trace_write_mode == "WRIT"
        assert not any(command.startswith(("INST ", "OUTP ", "BAND", "DET ", "POW:", "FREQ:", "DISP:")) and "?" not in command for command in session.commands)
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("continuous", [False, True])
def test_fresh_acquisition_does_not_probe_unresponsive_trace_type(tmp_path, monkeypatch, continuous):
    from app.devices.anritsu_ms2830a.module import MODULE
    from app.domain.errors import DeviceError

    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        session = adapter._require_session()
        session.write("TRAC1:TYPE VIEW")
        session.write("INIT:MODE:CONT" if continuous else "INIT:MODE:SING")
        query = session.query

        def hardware_query(command):
            if command == "TRAC:TYPE?":
                raise DeviceError("VI_ERROR_TMO: target does not answer TRAC:TYPE?")
            return query(command)

        monkeypatch.setattr(session, "query", hardware_query)
        for expected_id in ("1", "2"):
            session.commands.clear()
            trace = MODULE.dispatch(adapter, "single_sweep", "TRAC1")
            assert trace.sweep_id == expected_id
            assert trace.acquisition_completed_at_utc >= trace.acquisition_started_at_utc
            assert len(trace.powers_dbm) == len(trace.frequencies_hz) > 1
            # Module-dispatched quantitative acquisition intentionally leaves
            # the analyser in Single between reference/background/ROI points.
            assert session.continuous_sweep is False
            commands = session.commands
            assert "TRAC:TYPE?" not in commands
            ordered = [commands.index(c) for c in ("TRAC1:TYPE WRIT", "INIT:MODE:SING", "*WAI", "INIT:SWP?", "TRAC? TRAC1")]
            assert ordered == sorted(ordered)
    finally:
        adapter.disconnect()


def test_trace_preparation_failure_does_not_start_or_fetch_spectrum(tmp_path, monkeypatch):
    from app.domain.errors import DeviceError

    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        session = adapter._require_session()
        write = session.write
        def fail_prepare(command):
            if command == "TRAC1:TYPE WRIT":
                raise DeviceError("Trace preparation failed")
            return write(command)
        monkeypatch.setattr(session, "write", fail_prepare)
        session.commands.clear()
        with pytest.raises(DeviceError, match="Trace preparation failed"):
            adapter.acquire_single_sweep()
        assert "INIT:MODE:SING" not in session.commands
        assert "TRAC? TRAC1" not in session.commands
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("query,response", [("SWE:POIN?", "101.5"), ("AVER:COUN?", "1.5"), ("INIT:CONT?", "UNKNOWN")])
def test_full_analyzer_readback_rejects_ambiguous_values(tmp_path, monkeypatch, query, response):
    from app.domain.errors import DeviceError
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        session = adapter._require_session()
        original = session.query
        monkeypatch.setattr(session, "query", lambda command: response if command == query else original(command))
        with pytest.raises(DeviceError, match="invalid"):
            adapter.read_full_configuration()
    finally:
        adapter.disconnect()


def test_selected_sg_frequency_writes_only_frequency(tmp_path):
    settings = audit_settings(tmp_path)
    raw = settings.model_dump(mode="python")
    raw["devices"]["anritsu"]["signal_generator"].update(control_protocol="basic_scpi", frequency={"min": "100 MHz", "max": "6 GHz"}, power={"min": "-100 dBm", "max": "0 dBm"})
    settings = StationSettings.model_validate(raw)
    plan = compile_source(settings, """    - {id: baseline, type: configure_anritsu_sg, frequency: '1 GHz', power: '-30 dBm'}
    - id: selected
      type: sequence
      device_module: anritsu_sg
      operation: configure_selected_parameters
      configuration: {frequency: '1 GHz', power: '-50 dBm'}
      parameter_actions: [{parameter_id: sg.frequency, mode: set, value: '1.1 GHz'}]
""")
    adapter = StationComposition(settings, simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        adapter.configure_signal_generator(plan.actions[0].payload["config"])
        session = adapter._require_session()
        session.commands.clear()
        result = adapter.update_signal_generator(plan.actions[1].payload["config"])
        assert result.power_dbm == -30
        assert [c for c in session.commands if not c.endswith("?")] == ["FREQ 1100000000HZ"]
    finally:
        adapter.disconnect()


def test_sg_spectrum_transition_cannot_silently_switch_output_or_mode(tmp_path):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["anritsu"]["signal_generator"].update(control_protocol="basic_scpi", frequency={"min": "100 MHz", "max": "6 GHz"}, power={"min": "-100 dBm", "max": "0 dBm"})
    settings = StationSettings.model_validate(raw)
    with pytest.raises(ConfigurationError, match="explicit Spectrum Analyzer"):
        compile_source(settings, """    - {id: sg, type: configure_anritsu_sg, frequency: '1 GHz', power: '-30 dBm'}
    - {id: spectrum, type: acquire_spectrum}
""")


def test_automatic_smu_sampling_is_durable_for_every_raw_frame(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    source = "\n".join(SETUP.splitlines()[:5]).replace("'-80 mA'", "'1 mA'") + "\n"
    plan = compile_source(settings, source + "    - {id: signal, type: acquire_spectrum, average_count: 3, inter_sweep_delay: '3 s'}\n")
    path = tmp_path / "sampled.h5"
    result, waits = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    assert waits == [3, 3]
    frames = tuple(frame for _index, _point, frame in iter_recipe_spectrum_sweeps(path))
    assert len(frames) == 3
    for frame in frames:
        measurements = dict(frame.safety_measurements_si)
        assert measurements["keithley.A.current_a"] == pytest.approx(.0002)
        assert measurements["keithley.B.current_a"] == pytest.approx(.001)
        assert measurements["keithley.A.compliance_detected"] == 0
        assert measurements["keithley.B.compliance_detected"] == 0
        assert frame.safety_sampled_at_s <= frame.started_at_s
    with h5py.File(path) as file:
        metadata = json.loads(file["points/0/metadata_json"].asstr()[()])
        assert metadata["safety_sampling_policy"] == "active_smu_iv_before_each_raw_frame"


def test_compliance_stops_before_acquisition_even_without_authored_measure_node(tmp_path, monkeypatch):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["keithley"]["safety"].update(compliance_policy="stop", stop_on_compliance=True)
    settings = StationSettings.model_validate(raw)
    source = "\n".join(SETUP.splitlines()[:5]) + "\n    - {id: signal, type: acquire_spectrum}\n"
    plan = compile_source(settings, source)
    path = tmp_path / "compliance.h5"
    result, _waits = execute(settings, plan, path, monkeypatch)
    assert "compliance" in result.error.lower()
    assert result.stored_points == 1
    assert not tuple(iter_recipe_spectrum_sweeps(path))
    with h5py.File(path) as file:
        assert file["points/0"].attrs["status"] == "compliance"
        assert len(file["spectra"]) == 0
        events = file["events/name"].asstr()[:]
        assert "compliance_detected" in events


def test_disabled_large_axis_does_not_allocate_or_affect_expansion_budget(tmp_path):
    settings = audit_settings(tmp_path)
    settings.execution["max_expanded_points"] = 10
    plan = compile_source(settings, """    - id: unused
      type: sweep
      disabled: true
      target: keithley.B.current
      start: '0 A'
      stop: '1 mA'
      points: 1000000
      children: [{id: unused-point, type: checkpoint}]
    - {id: used, type: checkpoint}
""")
    assert plan.total_points == 1 and len(plan.actions) == 1


def test_sampling_time_is_included_in_nominal_estimate(tmp_path):
    settings = audit_settings(tmp_path)
    source = "\n".join(SETUP.splitlines()[:5]).replace("'-80 mA'", "'1 mA'") + "\n"
    on = compile_source(settings, source + "    - {id: signal, type: acquire_spectrum}\n")
    off = replace(on, actions=tuple(replace(action, payload={**action.payload, "enabled": False})
        if action.kind == "set_keithley_output" else action for action in on.actions))
    assert PlanEstimator(settings).estimate(on).nominal_duration_s > PlanEstimator(settings).estimate(off).nominal_duration_s


def test_blocked_moke_emergency_session_does_not_delay_other_outputs(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from app.devices.anritsu_ms2830a import AnritsuAdapter
    from app.devices.keithley_2600 import KeithleyAdapter
    from app.devices.rigol_dg1000z import RigolAdapter
    from app.domain.errors import DeviceError
    from app.ui.run_worker import EmergencyStopWorker

    settings = audit_settings(tmp_path)
    moke = StationComposition(settings, simulation=True).create_adapter("moke_box")
    blocked, release, other_outputs_stopped = threading.Event(), threading.Event(), threading.Event()
    confirmed = set()
    lock = threading.Lock()
    for adapter_class in (AnritsuAdapter, KeithleyAdapter, RigolAdapter):
        original = adapter_class.emergency_off
        def stop(adapter, method=original):
            result = method(adapter)
            with lock:
                confirmed.add(type(adapter).__name__)
                if len(confirmed) == 3:
                    other_outputs_stopped.set()
            return result
        monkeypatch.setattr(adapter_class, "emergency_off", stop)
    def blocked_stop(*_args, **_kwargs):
        blocked.set()
        assert release.wait(3)
        raise DeviceError("SIMULATED MOKE reply timeout; DAC state unconfirmed")
    monkeypatch.setattr(moke, "stop_vout", blocked_stop)
    monkeypatch.setattr("app.ui.run_worker.StationComposition", lambda *_args, **_kwargs:
        type("Factory", (), {"create_adapter": lambda _self, _name: moke})())
    finished = []
    worker = EmergencyStopWorker(settings, simulation=True)
    worker.finished.connect(finished.append, Qt.ConnectionType.DirectConnection)
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        assert blocked.wait(2)
        assert other_outputs_stopped.wait(2)
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(4)
    assert not thread.is_alive() and len(finished) == 1
    assert any("qualified DAC zero was not confirmed" in message for message in finished[0])
    assert any("Kepco power state remains unknown" in message for message in finished[0])
