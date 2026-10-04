"""Paired synthetic streams, empirical variance and explicit diagnostic failures."""

import json
import subprocess
import sys

import numpy as np
import pytest

pytest.importorskip("scipy")

from app.spectrum.resonance_bootstrap import PARAMETERS
from app.spectrum.resonance_metrics import resonance_values
from tools.diagnose_spectrum_standard_errors import standard_error_report, summarize_trials
from tools.qualify_spectrum_bootstrap_coverage import synthetic_trial


@pytest.mark.parametrize("negative", [False, True])
def test_factored_generator_preserves_v1_draws_exactly(negative):
    seed, trial, blocks, points = 20261006, 7, 40, 101
    frequencies = np.linspace(1e6, 2e6, points)
    shape = resonance_values(frequencies, 1, 1.500123e6, 100000.)
    data_seed, bootstrap_seed = np.random.SeedSequence([seed, trial]).spawn(2)
    random = np.random.default_rng(data_seed)

    def original_noise():
        power = random.gamma(16, 1e-11 / 16, size=(blocks, points))
        return (.25 * np.roll(power, 1, axis=1) + .5 * power + .25 * np.roll(power, -1, axis=1)
                + random.normal(0, 3e-12, (blocks, 1)) * shape)

    ref = 1e-9 + original_noise()
    sig = 1e-9 + (-1 if negative else 1) * 1e-10 * shape + original_noise()
    actual = synthetic_trial(seed, trial, blocks, points, negative)
    for expected, found in zip((frequencies, ref, sig), actual[:3], strict=True):
        np.testing.assert_array_equal(expected, found)
    assert actual[3] == int(bootstrap_seed.generate_state(1, dtype=np.uint64)[0])


def test_summary_uses_sample_dispersion_and_keeps_failures_visible():
    truth = dict.fromkeys(PARAMETERS, 0.)
    rows = [{"status": "ok", "estimate": dict.fromkeys(PARAMETERS, value),
             "covariance": (np.eye(4) * 4).tolist()} for value in (-2., 0., 2.)]
    rows.append({"status": "covariance_failed", "estimate": None, "covariance": None})
    report = summarize_trials(rows, truth)
    assert report["trials"] == 4 and report["successful_trials"] == 3
    assert report["failures"] == {"covariance_failed": 1}
    for item in report["parameters"].values():
        assert item["bias"] == 0
        assert item["empirical_standard_deviation"] == 2
        assert item["root_mean_estimated_variance"] == 2
        assert item["empirical_to_estimated_variance_ratio"] == 1
        assert item["standardized_error_standard_deviation"] == 1
        np.testing.assert_allclose(item["standardized_error_quantiles"], [-.95, 0, .95])
    assert summarize_trials(rows[-1:], truth)["parameters"] is None
    rows[0]["covariance"] = np.zeros((4, 4)).tolist()
    with pytest.raises(ValueError, match="positive"):
        summarize_trials(rows, truth)


def test_real_negative_diagnostic_is_repeatable_signed_and_unqualified():
    report = standard_error_report(repetitions=3, negative=True)
    assert report == standard_error_report(repetitions=3, negative=True)
    assert report["all_trials_available"]
    assert all(row["estimate"]["amplitude_w"] < 0 for row in report["trials"])
    assert not report["coverage_qualified"] and not report["laboratory_qualified"]
    assert report["units"]["finite_window_area_w_hz"] == "W*Hz"
    json.dumps(report, allow_nan=False)


def test_covariance_failures_preserve_point_estimates_and_no_qualification(monkeypatch):
    import tools.diagnose_spectrum_standard_errors as module

    def fail(*args):
        raise ValueError("unresolved variance")

    monkeypatch.setattr(module, "block_parameter_covariance", fail)
    report = standard_error_report(repetitions=2)
    assert not report["all_trials_available"]
    assert report["summary"]["failures"] == {"covariance_failed": 2}
    assert report["summary"]["parameters"] is None
    assert all(row["estimate"] is not None and row["covariance"] is None for row in report["trials"])


def test_point_fit_failure_is_explicit_not_a_zero_error(monkeypatch):
    import tools.diagnose_spectrum_standard_errors as module

    def fail(*args, **kwargs):
        raise ValueError("unidentifiable resonance")

    monkeypatch.setattr(module, "fit_linear_resonance", fail)
    report = standard_error_report(repetitions=2)
    assert report["summary"]["failures"] == {"point_fit_failed": 2}
    assert report["summary"]["successful_trials"] == 0
    assert report["summary"]["parameters"] is None
    assert all(row["estimate"] is None for row in report["trials"])
    json.dumps(report, allow_nan=False)


def test_cli_refuses_existing_report_without_modification(tmp_path):
    output = tmp_path / "diagnostic.json"
    command = [sys.executable, "-m", "tools.diagnose_spectrum_standard_errors",
               "--output", str(output), "--repetitions", "2"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    original = output.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "new report" in result.stderr
    assert output.read_bytes() == original


@pytest.mark.parametrize("negative", [False, True])
def test_lorentzian_diagnostic_uses_declared_generator_and_finite_window_truth(negative):
    from app.spectrum.resonance_metrics import resonance_area

    report = standard_error_report(repetitions=3, seed=20261026, negative=negative, shape="lorentzian")
    assert report["resonance_shape"] == report["generator"]["resonance_shape"] == "lorentzian"
    assert report["all_trials_available"]
    truth = report["truth"]
    assert truth["finite_window_area_w_hz"] == resonance_area(
        truth["amplitude_w"], truth["center_hz"], truth["fwhm_hz"], 1e6, 2e6, shape="lorentzian")
    assert all((row["estimate"]["amplitude_w"] < 0) is negative for row in report["trials"])
    assert not report["coverage_qualified"] and not report["laboratory_qualified"]
