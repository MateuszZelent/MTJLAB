import json

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.domain.errors import ConfigurationError, DeviceError
from app.domain.models import DeviceState
from app.engine.estimation import PlanEstimator
from app.engine.runner import RecipeRunner
from app.storage.hdf5_reader import Hdf5RunReader, iter_recipe_spectrum_sweeps
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_sweep_audit_contracts import audit_settings, compile_source, execute


@pytest.mark.parametrize(("target", "request_key", "request_value", "sweep_time", "nominal_duration"), [
    ("instrument_average_duration: '5 s'", "target_duration_s", 5.0, "100 ms", 5.0),
    ("instrument_average_count: 50", "requested_sweep_count", 50, "200 ms", 10.0),
])
def test_instrument_average_writes_one_trace_with_measured_count_per_block(
    tmp_path, monkeypatch, target, request_key, request_value, sweep_time, nominal_duration,
):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, f"""
    - {{id: timing, type: configure_anritsu_advanced, sweep_time_mode: manual, sweep_time: '{sweep_time}'}}
    - {{id: reference, type: acquire_reference, purpose: background, {target}}}
    - {{id: measurement, type: acquire_spectrum, {target}, reference_operation: difference_db}}
""")
    original = AnritsuAdapter.fetch_trace
    transfers = []
    previews = {}
    emit_telemetry = RecipeRunner._emit_telemetry

    def fetch(adapter, trace):
        snapshot = original(adapter, trace)
        transfers.append(snapshot.powers_dbm)
        return snapshot

    def capture_preview(runner, name, payload):
        if name in {"reference_preview", "spectrum_preview"}:
            previews[name] = payload
        emit_telemetry(runner, name, payload)

    monkeypatch.setattr(AnritsuAdapter, "fetch_trace", fetch)
    monkeypatch.setattr(RecipeRunner, "_emit_telemetry", capture_preview)
    path = tmp_path / "instrument-average.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None
    assert result.stored_points == 1 and len(transfers) == 2
    assert previews["reference_preview"]["raw_minus_reference_db"] is None
    assert previews["reference_preview"]["instrument_average"]["sweep_count"] == 50
    assert previews["spectrum_preview"]["instrument_average"]["sweep_count"] == 50
    reference = Hdf5RunReader.reference(path, 0)
    assert reference.purpose == "background"
    assert reference.average_count == 50 and not reference.source_sweep_indices
    stored = Hdf5RunReader.spectrum(path, 0)
    assert stored.reference_index == 0
    np.testing.assert_array_equal(reference.powers_dbm, transfers[0])
    np.testing.assert_array_equal(stored.powers_dbm, transfers[1])
    with h5py.File(path) as archive:
        assert "recipe_raw_sweeps_v1" not in archive
        point = json.loads(archive["points/0/metadata_json"].asstr()[()])
        assert point["spectrum_average_count"] == 50
        assert point["raw_recipe_sweep_indices"] == []
        reference_meta = json.loads(archive["references/0"].attrs["acquisition_metadata_json"])
        assert reference_meta["requested_minimum_sweeps"] == 50
        for metadata in (point["spectrum_processing_v1"], reference_meta):
            assert metadata["source_spectra_stored"] is False
            acquisition = metadata["instrument_average"]
            assert acquisition[request_key] == request_value
            assert acquisition["sweep_count"] == 50
            assert acquisition["nominal_duration_s"] == nominal_duration
            assert acquisition["trace_storage_mode"] == "LAV"
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors


def test_instrument_average_does_not_retry_with_unconfirmed_restore(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """
    - {id: measurement, type: acquire_spectrum, instrument_average_count: 4}
""")
    restore_attempts = []

    def fail_restore(adapter, *_args, **_kwargs):
        restore_attempts.append(1)
        adapter._state = DeviceState.UNKNOWN
        raise DeviceError("Cannot confirm original Anritsu settings")

    monkeypatch.setattr(AnritsuAdapter, "_restore_instrument_average", fail_restore)
    result, _ = execute(settings, plan, tmp_path / "restore-fault.h5", monkeypatch)

    assert "Cannot confirm original Anritsu settings" in result.error
    assert result.stored_points == 0
    assert len(restore_attempts) == 1


@pytest.mark.parametrize("option", ["average_count: 32", "acquisition_mode: continuous_live", "store_source_spectra: true"])
@pytest.mark.parametrize("target", ["instrument_average_duration: '5 s'", "instrument_average_count: 50"])
def test_instrument_average_rejects_a_second_average_or_unavailable_input_frames(tmp_path, option, target):
    settings = audit_settings(tmp_path)
    with pytest.raises(ConfigurationError, match="instrument averaging requires"):
        compile_source(settings, f"""
    - {{id: measurement, type: acquire_spectrum, {target}, {option}}}
""")


@pytest.mark.parametrize(("fields", "message"), [
    ("instrument_average_count: true", "must be an integer"),
    ("instrument_average_count: 4.5", "must be an integer"),
    ("instrument_average_count: 1", r"2\.\.9999"),
    ("instrument_average_count: 10000", r"2\.\.9999"),
    ("instrument_average_count: 50, instrument_average_duration: '5 s'", "choose either"),
    ("instrument_average_count: 50, processing: {filters: [emi_reject]}", "EMI requires individual"),
])
def test_exact_instrument_average_validates_target_and_source_history(tmp_path, fields, message):
    with pytest.raises(ConfigurationError, match=message):
        compile_source(audit_settings(tmp_path), f"""
    - {{id: measurement, type: acquire_spectrum, {fields}}}
""")


def test_hardware_reference_and_signal_can_use_different_average_targets(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """
    - {id: reference, type: acquire_reference, instrument_average_duration: '5 s'}
    - {id: measurement, type: acquire_spectrum, instrument_average_count: 50, reference_operation: difference_db}
""")
    reference, signal = plan.actions
    assert reference.payload["instrument_average_duration_s"] == 5.0
    assert signal.payload["instrument_average_count"] == 50
    assert signal.payload["average_count"] == 1


def test_hardware_reference_cannot_correct_pylab_sweep(tmp_path):
    with pytest.raises(ConfigurationError, match="matching acquisition modes"):
        compile_source(audit_settings(tmp_path), """
    - {id: reference, type: acquire_reference, instrument_average_count: 50}
    - {id: measurement, type: acquire_spectrum, reference_operation: difference_db}
""")


def test_hardware_count_estimate_uses_configured_sweep_time_and_one_transfer(tmp_path):
    settings = audit_settings(tmp_path)
    source = """
    - {id: analyzer, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '0 dBm', points: 101}
    - {id: timing, type: configure_anritsu_advanced, sweep_time_mode: manual, sweep_time: '200 ms'}
    - {id: measurement, type: acquire_spectrum, instrument_average_count: 50}
"""
    estimator = PlanEstimator(settings)
    long = estimator.estimate(compile_source(settings, source))
    short = estimator.estimate(compile_source(settings, source.replace("count: 50", "count: 2")))
    assert long.nominal_duration_s - short.nominal_duration_s == pytest.approx(9.6)
    assert long.spectrum_values == short.spectrum_values == 101
    assert any("50 sweeps" in warning for warning in long.warnings)
    with pytest.raises(ConfigurationError, match="acquisition timeout"):
        compile_source(settings, source.replace("200 ms", "600 ms"))


def test_triggered_recipe_archives_individual_sweeps_with_existing_analyzer_average(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """
    - {id: measurement, type: acquire_spectrum, average_count: 3}
""")
    connect = AnritsuAdapter.connect
    fetch_trace = AnritsuAdapter.fetch_trace
    sessions = []
    transfers = []

    def connect_with_panel_average(adapter):
        identity = connect(adapter)
        session = adapter._require_session()
        session.write("TRAC1:STOR:MODE LAV")
        session.write("TRAC2:STOR:MODE AVER")
        session.write("AVER:COUN 50")
        sessions.append(session)
        return identity

    def capture(adapter, trace):
        session = adapter._require_session()
        transfers.append((session.completed_sweep_count, tuple(session.trace_storage_modes[:2])))
        return fetch_trace(adapter, trace)

    monkeypatch.setattr(AnritsuAdapter, "connect", connect_with_panel_average)
    monkeypatch.setattr(AnritsuAdapter, "fetch_trace", capture)
    path = tmp_path / "raw-triggered.h5"
    result, _ = execute(settings, plan, path, monkeypatch)

    assert result.error is None
    assert transfers == [(1, ("OFF", "OFF"))] * 3
    assert sessions[0].trace_storage_modes[:2] == ["LAV", "AVER"]
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 3


def test_average_only_preserves_counts_and_mean_for_timed_reference_and_signal(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    recipe = """
    - {id: background, type: acquire_reference, purpose: background, average_count: 3, minimum_duration: '20 ms'}
    - {id: measurement, type: acquire_spectrum, average_count: 4}
"""
    archive_all = compile_source(settings, recipe)
    plan = compile_source(settings, recipe.replace("average_count:", "store_source_spectra: false, average_count:"))
    estimator = PlanEstimator(settings)
    old, new = estimator.estimate(archive_all), estimator.estimate(plan)
    assert new.nominal_duration_s == old.nominal_duration_s
    assert new.total_upper_bytes < old.total_upper_bytes

    acquire = AnritsuAdapter.acquire_single_sweep
    acquired = []
    previews = {}
    emit_telemetry = RecipeRunner._emit_telemetry

    def capture(adapter, trace, **kwargs):
        result = acquire(adapter, trace, **kwargs)
        acquired.append(result.powers_dbm)
        return result

    def capture_preview(runner, name, payload):
        if name in {"reference_preview", "spectrum_preview"}:
            previews[name] = payload
        emit_telemetry(runner, name, payload)

    monkeypatch.setattr(AnritsuAdapter, "acquire_single_sweep", capture)
    monkeypatch.setattr(RecipeRunner, "_emit_telemetry", capture_preview)
    path = tmp_path / "mean-only.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None
    assert result.stored_points == 1

    baseline = Hdf5RunReader.reference(path, 0)
    assert baseline.purpose == "background"
    assert not baseline.source_sweep_indices
    assert baseline.average_count >= 3
    assert len(acquired) == baseline.average_count + 4
    split = baseline.average_count
    assert previews["reference_preview"]["average_count"] == split
    assert previews["spectrum_preview"]["average_count"] == 4
    assert previews["spectrum_preview"]["acquisition_mode"] == "single_sweep"
    with h5py.File(path) as archive:
        assert "recipe_raw_sweeps_v1" not in archive
        baseline_meta = json.loads(archive["references/0"].attrs["acquisition_metadata_json"])
        assert baseline_meta["source_spectra_stored"] is False
        assert baseline_meta["collection_elapsed_s"] >= 0.02
        point = json.loads(archive["points/0/metadata_json"].asstr()[()])
        assert point["raw_recipe_sweep_indices"] == []
        assert point["spectrum_average_count"] == 4
        assert point["spectrum_processing_v1"]["source_spectra_stored"] is False
        for samples, mean in (
            (acquired[:split], baseline.powers_dbm),
            (acquired[split:], archive["spectra/0/power_dbm"][:]),
        ):
            expected_w = np.mean(10 ** ((np.asarray(samples) - 30) / 10), axis=0)
            np.testing.assert_allclose(10 ** ((np.asarray(mean) - 30) / 10), expected_w)
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors


def test_reference_and_measurement_choose_retention_independently(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """
    - {id: reference, type: acquire_reference, average_count: 3, store_source_spectra: false}
    - {id: measurement, type: acquire_spectrum, average_count: 4}
""")
    path = tmp_path / "mixed-retention.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None
    reference = Hdf5RunReader.reference(path, 0)
    assert reference.average_count == 3 and not reference.source_sweep_indices
    records = list(iter_recipe_spectrum_sweeps(path))
    assert len(records) == 4 and all(record.recipe_node_id == "measurement" for _, _, record in records)
    with h5py.File(path) as archive:
        point = json.loads(archive["points/0/metadata_json"].asstr()[()])
        assert point["raw_recipe_sweep_indices"] == [0, 1, 2, 3]
        assert point["spectrum_processing_v1"]["source_spectra_stored"] is True
