"""Representative Cartesian scale qualification with an explicit synthetic DUT."""

import hashlib
import json
import time

import h5py
import pytest

from app.devices.keithley_2600 import KeithleyAdapter
from app.engine.estimation import PlanEstimator
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_sweep_audit_contracts import AXES, SETUP, audit_settings, compile_source, execute


@pytest.mark.parametrize("bins", [1001, 10001])
def test_1309_point_cartesian_archive_and_pythat(tmp_path, monkeypatch, bins):
    settings = audit_settings(tmp_path)
    setup = SETUP.replace("points: 101", f"points: {bins}")
    if bins == 10001:
        setup = setup.replace("'1 MHz'", "'100 MHz'").replace("'2 MHz'", "'6 GHz'").replace("'0 dBm'", "'-10 dBm'")
        lines = setup.splitlines(keepends=True)
        lines.insert(1, "    - {id: analyzer-input, type: configure_anritsu_advanced, rbw_mode: auto, vbw_mode: manual, vbw: '30 kHz', vbw_filter_mode: POW, detector: NORM, attenuation_mode: manual, attenuation: '10 dB', preamplifier_enabled: false, sweep_time_mode: auto}\n")
        setup = "".join(lines)
    source = (setup
              + AXES.replace("points: 2", "points: 11")
              .replace("points: 3", "points: 17", 1).replace("points: 3", "points: 7"))
    started = time.perf_counter()
    plan = compile_source(settings, source)
    compile_s = time.perf_counter() - started
    assert plan.total_points == plan.total_spectra == 1309
    estimate = PlanEstimator(settings).estimate(plan)
    connect = KeithleyAdapter.connect

    def connect_low_resistance_simulator(adapter):
        connect(adapter)
        # Only the in-memory simulator is modified. At 0.1 ohm, 80 mA needs
        # 8 mV, inside the authored 20 mV compliance; the 10 ohm default clips.
        session = adapter._require_session()
        assert hasattr(session, "resistance_ohm")
        session.resistance_ohm = {"smua": .1, "smub": .1}

    monkeypatch.setattr(KeithleyAdapter, "connect", connect_low_resistance_simulator)
    path = tmp_path / "cartesian-1309.h5"
    started = time.perf_counter()
    result, waits = execute(settings, plan, path, monkeypatch)
    run_s = time.perf_counter() - started
    assert result.error is None, result.error
    assert result.stored_points == 1309 and waits == [3.0] * 1309
    with h5py.File(path) as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["points"]) == len(file["spectra"]) == 1309
        assert len(file["_pending"]) == 0
        for index in range(1309):
            row = file[f"points/{index}"]
            assert bool(row.attrs["complete"])
            metadata = json.loads(row["metadata_json"].asstr()[()])
            evidence = metadata["setpoint_evidence_v1"]
            assert {"moke_box.vout0.voltage", "keithley.B.current", "keithley.A.current"} <= evidence.keys()
            assert all(value["readback_si"] is not None for value in evidence.values())
            assert len(file[f"spectra/{index}/power_dbm"]) == bins
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors
    assert path.stat().st_size <= estimate.total_upper_bytes
    with path.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    summary = {
        "simulation": True, "simulated_dut_resistance_ohm": .1,
        "dimensions": [11, 17, 7], "points": 1309, "bins": bins,
        "compile_s": compile_s, "run_and_close_s": run_s,
        "wait_policy_s": 3, "waits_recorded": len(waits),
        "actual_waits_bypassed_for_scale_test": True,
        "hdf5_bytes": path.stat().st_size,
        "estimated_archive_upper_bytes": estimate.total_upper_bytes,
        "estimated_public_import_upper_bytes": estimate.public_import_upper_bytes,
        "estimated_nominal_duration_s": estimate.nominal_duration_s,
        "hdf5_sha256": archive_hash,
        "pythat_valid": report.valid, "pythat_version": report.pythat_version,
    }
    (tmp_path / "qualification.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
