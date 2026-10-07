"""Timed background semantics, source durability and operator recipe contract."""
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from app.domain.errors import ConfigurationError
from app.engine.compiler import RecipeCompiler
from app.engine.policy import ExecutionPolicy
from app.recipes import parse_recipe_text
from app.settings.models import StationSettings
from app.storage.hdf5_reader import Hdf5RunReader, iter_recipe_spectrum_sweeps
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_sweep_audit_contracts import audit_settings, compile_source, execute


RECIPE = Path(__file__).parent / "fixtures" / "requested_sweep_297.yml"


def requested_settings(tmp_path):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    for channel in raw["devices"]["keithley"]["safety"]["channels"].values():
        limits = channel["lab_limits"]
        limits["voltage_compliance"] = {"min": "10 mV", "max": "710 mV"}
        limits["source_voltage"] = {"min": "-710 mV", "max": "710 mV", "max_abs": "710 mV"}
        limits["measured_voltage_trip"] = {"min": "-720 mV", "max": "720 mV"}
        limits["max_abs_power"] = "100 mW"
    return StationSettings.model_validate(raw)


def test_requested_recipe_compiles_exact_cartesian_points_and_waits(tmp_path):
    plan = RecipeCompiler(requested_settings(tmp_path)).compile(parse_recipe_text(RECIPE.read_text(encoding="utf-8")))
    assert plan.total_points == plan.total_spectra == 297
    spectra = [action for action in plan.actions if action.kind == "acquire_spectrum"]
    assert sum(action.payload["average_count"] for action in spectra) == 9504
    expected = [(m / 1000, b / 1000, a / 20000) for m in (30, 40, 50) for b in (25, 30, 35) for a in range(33)]
    actual = [tuple(action.setpoints_si[key] for key in ("moke_box.vout0.voltage", "keithley.B.current", "keithley.A.current")) for action in spectra]
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-15)
    for index, action in enumerate(plan.actions):
        if action.kind == "acquire_spectrum":
            preceding = plan.actions[index - 5:index]
            assert preceding[0].kind == "update_keithley_level"
            assert preceding[1].kind == "wait" and preceding[1].payload["duration_s"] == 5
            assert action.payload["average_count"] == 32
    references = [action for action in plan.actions if action.kind == "acquire_reference"]
    assert [action.payload["purpose"] for action in references] == ["reference", "background"]
    assert references[1].payload["minimum_duration_s"] == 30
    assert ExecutionPolicy().deadline_for(references[1]) > 30


@pytest.mark.parametrize("extra", ["minimum_duration: '-1 s'", "minimum_duration: '30 mA'", "purpose: signal", "minimum_duration: '3601 s'"])
def test_invalid_timed_acquisition_rejected(tmp_path, extra):
    with pytest.raises((ConfigurationError, ValueError)):
        compile_source(audit_settings(tmp_path), f"    - {{id: background, type: acquire_reference, {extra}}}\n")


def test_timed_background_and_reference_are_both_durable(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: ref, type: acquire_reference, purpose: reference, average_count: 2}
    - {id: bg, type: acquire_reference, purpose: background, average_count: 3, minimum_duration: '200 ms'}
    - {id: signal, type: acquire_spectrum, average_count: 2}
""")
    path = tmp_path / "timed.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    records = list(iter_recipe_spectrum_sweeps(path))
    with h5py.File(path) as file:
        assert len(file["references"]) == 2
        ref, bg = file["references/0"], file["references/1"]
        assert ref.attrs["purpose"] == "reference" and bg.attrs["purpose"] == "background"
        metadata = json.loads(bg.attrs["acquisition_metadata_json"])
        assert metadata["collection_elapsed_s"] >= .2
        assert "spectrum" in metadata["device_states"]["anritsu"]
        assert "advanced_spectrum" in metadata["device_states"]["anritsu"]
        assert "output_status" in metadata
        indices = list(bg["source_recipe_sweep_indices"][:])
        assert len(indices) == bg.attrs["average_count"] >= 3
        selected = [records[int(i)][2] for i in indices]
        assert all(record.average_count is None and record.minimum_duration_s == .2 for record in selected)
        expected_w = np.mean([10 ** ((record.powers_dbm - 30) / 10) for record in selected], axis=0)
        np.testing.assert_allclose(10 ** ((bg["power_dbm"][:] - 30) / 10), expected_w, rtol=1e-12)
        assert file["run"].attrs["status"] == "completed"
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    assert [reference.purpose for reference in Hdf5RunReader.references(path)] == ["reference", "background"]
    assert Hdf5RunReader.reference(path, 1).purpose == "background"


def test_interrupted_background_keeps_raw_without_publishing_partial_mean(tmp_path, monkeypatch):
    from app.devices.anritsu_ms2830a import AnritsuAdapter
    from app.domain.errors import DeviceError

    original = AnritsuAdapter.acquire_single_sweep
    calls = 0

    def fail_during_background(adapter, trace):
        nonlocal calls
        calls += 1
        if calls >= 6:
            raise DeviceError("Injected acquisition failure")
        return original(adapter, trace)

    monkeypatch.setattr(AnritsuAdapter, "acquire_single_sweep", fail_during_background)
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: ref, type: acquire_reference, purpose: reference, average_count: 2}
    - {id: bg, type: acquire_reference, purpose: background, average_count: 10, minimum_duration: '30 s'}
""")
    path = tmp_path / "interrupted.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert "Injected acquisition failure" in result.error
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 5
    with h5py.File(path) as file:
        assert len(file["references"]) == 1
        assert len(file["_pending"]) == 0


def test_full_requested_sweep_archive(tmp_path, monkeypatch):
    """All 297 points and Avg32 sources, with real 30 s background timing.

    Only setpoint waiting is bypassed, recorded and checked explicitly.
    """
    import hashlib
    import time

    from app.devices.anritsu_ms2830a import AnritsuAdapter
    from app.devices.anritsu_ms2830a import SpectrumConfig

    settings = requested_settings(tmp_path)
    original_connect = AnritsuAdapter.connect

    def prepare_simulated_analyzer(adapter):
        original_connect(adapter)
        adapter.configure_spectrum(SpectrumConfig(100e6, 6e9, -10, 10001, "TRAC1"))

    monkeypatch.setattr(AnritsuAdapter, "connect", prepare_simulated_analyzer)
    plan = RecipeCompiler(settings).compile(parse_recipe_text(RECIPE.read_text(encoding="utf-8")))
    path = tmp_path / "requested-297-avg32.h5"
    started = time.monotonic()
    result, waits = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    assert result.stored_points == 297 and waits == [5.] * 298
    with h5py.File(path) as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["points"]) == len(file["spectra"]) == 297
        assert len(file["references"]) == 2 and len(file["_pending"]) == 0
        for reference_index in (0, 1):
            base = json.loads(file[f"references/{reference_index}"].attrs["acquisition_metadata_json"])
            assert base["output_status"]["keithley.A"] == "off"
            assert base["output_status"]["keithley.B"] == "off"
            assert "spectrum" in base["device_states"]["anritsu"]
            assert base["device_states"]["moke_box"]["dac_shutdown"]["actual"]["safe_target_confirmed"]
        background = file["references/1"]
        bg_count = int(background.attrs["average_count"])
        assert json.loads(background.attrs["acquisition_metadata_json"])["collection_elapsed_s"] >= 30
        assert len(file["recipe_raw_sweeps_v1"]) == 9504 + 32 + bg_count
        for index in range(297):
            group = file[f"points/{index}"]
            metadata = json.loads(group["metadata_json"].asstr()[()])
            assert metadata["spectrum_average_count"] == 32
            evidence = metadata["setpoint_evidence_v1"]
            assert all(evidence[key]["readback_si"] is not None for key in (
                "moke_box.vout0.voltage", "keithley.A.current", "keithley.B.current"))
            assert len(file[f"spectra/{index}/power_dbm"]) == 10001
            indices = metadata["raw_recipe_sweep_indices"]
            assert len(indices) == 32
            if index in (0, 148, 296):
                raw_w = [10 ** ((file[f"recipe_raw_sweeps_v1/{i}/power_dbm"][:] - 30) / 10) for i in indices]
                np.testing.assert_allclose(10 ** ((file[f"spectra/{index}/power_dbm"][:] - 30) / 10),
                                           np.mean(raw_w, axis=0), rtol=1e-12)
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    evidence = {"simulation": True, "points": 297, "signal_raw_sweeps": 9504,
                "bins": 10001, "background_sweeps": bg_count, "background_minimum_duration_s": 30,
                "waits_s": 5, "point_waits_bypassed_in_simulation": True,
                "pythat_valid": report.valid, "archive_sha256": digest,
                "archive_bytes": path.stat().st_size, "runtime_s": time.monotonic() - started}
    directory = Path("docs/audits/2026-10-05-requested-sweep")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "simulation-297-avg32.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_successful_zero_ramp_updates_persisted_device_snapshot(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: configure, type: configure_keithley, channel: A, mode: current, level: '0.2 mA', compliance: '20 mV', source_range: '10 mA'}
    - {id: on, type: set_keithley_output, channel: A, enabled: true}
    - {id: zero, type: ramp_keithley_to_zero, channel: A, deadline: '10 s'}
    - {id: saved, type: checkpoint}
""")
    path = tmp_path / "zero.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    with h5py.File(path) as file:
        device_states = json.loads(file["points/0/device_states_json"].asstr()[()])
        state = device_states["keithley"]["channel_A"]["actual"]
        assert state["source_level_si"] == 0
        assert state["output_enabled"] is False
        events = [json.loads(value) for value in file["events/message"].asstr()[:]]
        assert events[-1]["state_snapshot"]["output_status"]["keithley.A"] == "off"
