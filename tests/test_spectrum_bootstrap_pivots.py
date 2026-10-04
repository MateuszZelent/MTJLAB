"""Pivot inversion, denominators and strict matching of diagnostic provenance."""

from copy import deepcopy
import json
import subprocess
import sys

import numpy as np
import pytest

pytest.importorskip("scipy")

from app.spectrum.resonance_bootstrap import PARAMETERS
from tools.diagnose_spectrum_bootstrap_pivots import diagnose_pivots
from tools.diagnose_spectrum_standard_errors import standard_error_report
from tools.qualify_spectrum_bootstrap_coverage import bootstrap_coverage_report


@pytest.fixture(scope="module")
def reports():
    return (bootstrap_coverage_report(repetitions=2, seed=20261006, interval_method="studentized"),
            standard_error_report(repetitions=3, seed=20261006))


def test_exact_inversion_known_cutoffs_and_tail_counts(reports):
    coverage, diagnostic = deepcopy(reports)
    for i, row in enumerate(diagnostic["trials"][:2]):
        row["estimate"] = {name: coverage["truth"][name] + (0 if i == 0 else 3) for name in PARAMETERS}
        row["covariance"] = np.eye(4).tolist()
        coverage["trials"][i]["intervals"] = {name: [value - 2, value + 2] for name, value in row["estimate"].items()}
        coverage["trials"][i]["covered"] = dict.fromkeys(PARAMETERS, i == 0)
    report = diagnose_pivots(coverage, diagnostic)
    assert report["matched_trials"] == 2
    assert report["unmatched_standard_error_trials"] == 1
    for item in report["parameters"].values():
        assert item["bootstrap_cutoff_medians"] == [-2, 2]
        assert item["below_bootstrap_cutoff"] == 0
        assert item["above_bootstrap_cutoff"] == 1
        assert item["within_bootstrap_cutoffs"] == 1
        assert item["covered_fraction_all_matched_trials"] == .5
    assert not report["coverage_qualified"]


def test_missing_pairs_are_explicit_and_remain_in_denominator(reports):
    coverage, diagnostic = deepcopy(reports)
    coverage["trials"][0]["intervals"] = None
    coverage["trials"][0]["covered"] = dict.fromkeys(PARAMETERS, False)
    diagnostic["trials"][1]["status"] = "covariance_failed"
    diagnostic["trials"][1]["covariance"] = None
    report = diagnose_pivots(coverage, diagnostic)
    assert report["missing_pairs"] == {"missing_interval": 1, "missing_standard_error": 1, "both_missing": 0}
    assert all(item["covered_fraction_all_matched_trials"] == 0 for item in report["parameters"].values())
    assert all(item["bootstrap_cutoff_medians"] is None for item in report["parameters"].values())
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("mutation", ["seed", "units", "order", "trial", "trial_seed", "variance", "method", "generator", "count"])
def test_mismatched_or_invalid_provenance_is_rejected(reports, mutation):
    coverage, diagnostic = deepcopy(reports)
    if mutation == "seed":
        diagnostic["seed"] += 1
    elif mutation == "units":
        diagnostic["units"]["amplitude_w"] = "dBm"
    elif mutation == "order":
        diagnostic["parameter_order"].reverse()
    elif mutation == "trial":
        diagnostic["trials"][1]["trial"] = 0
    elif mutation == "trial_seed":
        diagnostic["trials"][0]["bootstrap_seed"] += 1
    elif mutation == "variance":
        diagnostic["trials"][0]["covariance"][0][0] = 0
    elif mutation == "method":
        coverage["bootstrap_config_template"]["interval_method"] = "percentile"
    elif mutation == "generator":
        coverage["generator"]["gamma_shape"] = 8
    else:
        diagnostic["summary"]["trials"] = 0
    with pytest.raises(ValueError):
        diagnose_pivots(coverage, diagnostic)


def test_cli_preserves_sources_and_refuses_existing_output(reports, tmp_path):
    paths = [tmp_path / "coverage.json", tmp_path / "errors.json"]
    for path, report in zip(paths, reports, strict=True):
        path.write_text(json.dumps(report), encoding="utf-8")
    original = [path.read_bytes() for path in paths]
    output = tmp_path / "pivots.json"
    command = [sys.executable, "-m", "tools.diagnose_spectrum_bootstrap_pivots",
               *(str(path) for path in paths), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(report["source_sha256"]) == 2
    assert report["parameters"]["center_hz"]["within_bootstrap_cutoffs"] == sum(
        row["covered"]["center_hz"] for row in reports[0]["trials"])
    before = output.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert output.read_bytes() == before
    assert [path.read_bytes() for path in paths] == original


@pytest.mark.parametrize("negative", [False, True])
def test_lorentzian_pivots_match_exact_coverage_and_reject_shape_mismatch(negative):
    coverage = bootstrap_coverage_report(repetitions=2, seed=20261026, negative=negative,
                                        shape="lorentzian", interval_method="studentized")
    diagnostic = standard_error_report(repetitions=2, seed=20261026, negative=negative, shape="lorentzian")
    report = diagnose_pivots(coverage, diagnostic)
    assert report["resonance_shape"] == "lorentzian"
    for name in PARAMETERS:
        assert report["parameters"][name]["within_bootstrap_cutoffs"] == sum(
            trial["covered"][name] for trial in coverage["trials"])
    diagnostic["resonance_shape"] = "gaussian"
    with pytest.raises(ValueError, match="shapes differ"):
        diagnose_pivots(coverage, diagnostic)


def test_legacy_gaussian_metadata_can_pair_without_guessing_lorentzian(reports):
    coverage, diagnostic = deepcopy(reports)
    del coverage["generator"]["resonance_shape"]
    del diagnostic["generator"]
    del diagnostic["resonance_shape"]
    assert diagnose_pivots(coverage, diagnostic)["resonance_shape"] == "gaussian"
