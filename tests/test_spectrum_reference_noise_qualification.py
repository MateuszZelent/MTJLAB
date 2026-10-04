"""Temporal noise has numerical oracles without fabricated independence gates."""

import json
import subprocess
import sys

import numpy as np
import pytest

from tools.qualify_spectrum_reference_noise import synthetic_power_history, temporal_noise_report


def test_all_temporal_laws_match_original_power_oracles_and_preserve_counts():
    report = temporal_noise_report(blocks=64)
    assert report["all_numerical_gates_passed"]
    for scenario in report["scenarios"].values():
        assert scenario["passed"]
        assert len(scenario["raw_power_w"]) == 65 * 4
        assert len(scenario["block_mean_w"]) == 64
        assert not scenario["sweep_independence_inferred"]
        assert not scenario["stationarity_automatically_classified"]
        assert scenario["qualified_ttl_s"] is None
    assert not report["laboratory_qualified"] and not report["confidence_intervals_qualified"]
    assert report["units"]["raw_power_w"] == "W"
    assert report["units"]["allan_variance_w2"] == "W^2"
    assert report["diagnostic_bin_indices"] == [1]
    assert report["frequency_hz"][1] == report["selected_frequency_hz"] == 2e6
    assert report == temporal_noise_report(blocks=64)
    json.dumps(report, allow_nan=False)


def test_impulses_and_flicker_are_not_clipped_or_detrended():
    powers, metadata = synthetic_power_history("impulses", 4096, 20261025)
    assert metadata["realized_impulses"] > 0
    assert powers.max() > 1e-9 + 5e-10
    flicker, metadata = synthetic_power_history("flicker_1f", 4096, 20261025)
    assert np.std(flicker - 1e-9) == pytest.approx(2e-11, rel=1e-12)
    assert metadata["psd_exponent"] == -1
    assert metadata["lowest_frequency_cycles_per_sweep"] == 1 / 4096
    assert np.all(flicker > 0)


def test_white_noise_averages_down_while_linear_drift_grows():
    report = temporal_noise_report(blocks=1024, scenarios=("white_gamma", "linear_drift"))
    assert report["all_numerical_gates_passed"]
    white = report["scenarios"]["white_gamma"]["allan_variance_w2"]
    assert white[-1] < white[0] / 20
    drift = report["scenarios"]["linear_drift"]
    tau = np.asarray(drift["allan_tau_s"])
    variance = np.asarray(drift["allan_variance_w2"])
    slope_w_per_s = 1e-10 / ((1025 * 4 - 1) * .025)
    np.testing.assert_allclose(variance, .5 * (slope_w_per_s * tau)**2, rtol=1e-9)


@pytest.mark.parametrize("kwargs", [{"blocks": 8}, {"seed": -1}, {"scenarios": ()},
                                    {"scenarios": ("unknown",)}, {"scenarios": ("impulses", "impulses")}])
def test_invalid_qualification_requests_are_rejected(kwargs):
    with pytest.raises(ValueError):
        temporal_noise_report(**kwargs)


def test_cli_preserves_synthetic_report_and_refuses_overwrite(tmp_path):
    output = tmp_path / "noise.json"
    command = [sys.executable, "-m", "tools.qualify_spectrum_reference_noise",
               "--blocks", "32", "--output", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    original = output.read_bytes()
    report = json.loads(original)
    assert report["all_numerical_gates_passed"]
    assert len(report["scenarios"]) == 6
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode != 0 and "new report" in completed.stderr
    assert output.read_bytes() == original


@pytest.mark.parametrize("samples,seed", [(1, 1), (16389, 1), (100., 1), (100, -1)])
def test_power_generator_enforces_sample_and_seed_bounds(samples, seed):
    with pytest.raises(ValueError):
        synthetic_power_history("white_gamma", samples, seed)
