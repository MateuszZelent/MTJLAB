"""Coverage denominators, binomial endpoints and independent repeatability."""

import json
import subprocess
import sys

import pytest

pytest.importorskip("scipy", reason="Optional qualification dependencies are required")

from tools.qualify_spectrum_bootstrap_coverage import bootstrap_coverage_report, coverage_statistics


def test_exact_binomial_limits_include_all_failures_and_all_successes():
    zero, all_success = coverage_statistics(0, 1000), coverage_statistics(1000, 1000)
    assert zero["fraction"] == 0 and zero["binomial_95_interval"][0] == 0
    assert all_success["fraction"] == 1 and all_success["binomial_95_interval"][1] == 1
    assert not all_success["nominal_within_binomial_interval"]
    nominal = coverage_statistics(950, 1000)
    assert nominal["binomial_95_interval"][0] < .95 < nominal["binomial_95_interval"][1]


def test_absent_intervals_are_uncovered_trials_not_removed_from_denominator(monkeypatch):
    import tools.qualify_spectrum_bootstrap_coverage as module

    monkeypatch.setattr(module, "bootstrap_resonance_blocks", lambda *args, **kwargs: {
        "status": "unstable_fit", "confidence_intervals": None, "failed_fits": 1,
    })
    report = bootstrap_coverage_report(repetitions=3)
    assert report["failures"] == {"unstable_fit": 3}
    assert all(item["trials"] == 3 and item["covered"] == 0 for item in report["coverage"].values())
    assert not report["gate"]["all_intervals_available"]
    assert not report["gate"]["minimum_1000_independent_trials"]


def test_point_fit_failure_is_explicit_and_json_report_remains_valid(monkeypatch):
    import tools.qualify_spectrum_bootstrap_coverage as module

    def fail(*args, **kwargs):
        raise ValueError("unidentifiable point fit")

    monkeypatch.setattr(module, "bootstrap_resonance_blocks", fail)
    report = bootstrap_coverage_report(repetitions=2)
    assert report["failures"] == {"point_fit_failed": 2}
    assert "unidentifiable" in report["trials"][0]["failure_detail"]
    json.dumps(report, allow_nan=False)


def test_real_bootstrap_trials_preserve_negative_truth_and_reproduce_seed():
    progress = []
    report = bootstrap_coverage_report(repetitions=2, negative=True, progress=lambda *args: progress.append(args))
    assert report == bootstrap_coverage_report(repetitions=2, negative=True)
    assert progress == [(1, 2), (2, 2)]
    assert report["truth"]["amplitude_w"] < 0 and report["truth"]["finite_window_area_w_hz"] < 0
    assert report["trials"][0]["bootstrap_seed"] != report["trials"][1]["bootstrap_seed"]
    assert not report["laboratory_qualified"] and report["false_detection_rate"] is None
    assert report["units"]["fwhm_hz"] == "Hz"
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("shape", ["gaussian", "lorentzian"])
def test_cli_persists_trial_journal_and_refuses_report_reuse(tmp_path, shape):
    output = tmp_path / "coverage.json"
    command = [sys.executable, "-m", "tools.qualify_spectrum_bootstrap_coverage",
               "--output", str(output), "--repetitions", "2", "--shape", shape]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    journal = [json.loads(line) for line in output.with_suffix(".trials.jsonl").read_text(encoding="utf-8").splitlines()]
    assert journal[0]["type"] == "incomplete_trial_journal"
    assert journal[0]["shape"] == shape
    assert report["generator"]["resonance_shape"] == shape
    assert report["limitations"][0] == f"Known single {shape.capitalize()} hypothesis; no automatic peak detector"
    assert journal[1:] == report["trials"]
    assert not report["gate"]["minimum_1000_independent_trials"]
    before = output.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "new report" in result.stderr
    assert output.read_bytes() == before


def test_lorentzian_negative_coverage_uses_matching_truth_and_fit():
    from app.spectrum.resonance_metrics import resonance_area

    report = bootstrap_coverage_report(repetitions=2, negative=True,
        shape="lorentzian", interval_method="studentized")
    assert report["generator"]["resonance_shape"] == "lorentzian"
    truth = report["truth"]
    assert truth["amplitude_w"] < 0
    assert truth["finite_window_area_w_hz"] == resonance_area(
        truth["amplitude_w"], truth["center_hz"], truth["fwhm_hz"],
        1e6, 2e6, shape="lorentzian")
    assert not report["failures"]
    assert all(row["intervals"] is not None for row in report["trials"])
    assert not report["gate"]["minimum_1000_independent_trials"]
    assert report == bootstrap_coverage_report(repetitions=2, negative=True,
        shape="lorentzian", interval_method="studentized")


@pytest.mark.parametrize("shape", [None, ["gaussian"], "unknown"])
def test_coverage_rejects_unknown_shape_before_starting_trials(shape):
    with pytest.raises(ValueError, match="shape must be"):
        bootstrap_coverage_report(repetitions=1, shape=shape)
