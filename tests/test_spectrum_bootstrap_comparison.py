"""Paired denominators, unverifiable identities and exclusive diagnostic reports."""

from copy import deepcopy
from dataclasses import asdict
import json
import subprocess
import sys

import pytest

from app.spectrum.resonance_bootstrap import PARAMETERS, ResonanceBootstrapConfig
from tools.compare_spectrum_bootstrap_trials import compare_trials


def reports():
    first = {"algorithm": "synthetic-spectral-bootstrap-coverage-v1", "seed": 12,
             "software_versions": {"numpy": "test", "scipy": "test"}, "points": 101,
             "blocks_per_source": 40, "truth": dict.fromkeys(PARAMETERS, 1),
             "generator": {"kind": "declared-test"}, "units": dict.fromkeys(PARAMETERS, "test"),
             "bootstrap_config_template": asdict(ResonanceBootstrapConfig(resamples=200)),
             "repetitions": 4, "bootstrap_resamples": 200, "trials": []}
    second = deepcopy(first)
    second["bootstrap_config_template"]["resamples"] = 1000
    second["bootstrap_resamples"] = 1000
    for report, flags in ((first, [True, True, False, False]), (second, [True, False, True, False])):
        for index, flag in enumerate(flags):
            report["trials"].append({"trial": index, "bootstrap_seed": 100 + index,
                "covered": dict.fromkeys(PARAMETERS, flag),
                "intervals": {name: [0, 2] if flag else [-2, 0] for name in PARAMETERS}})
    return first, second


def test_contingency_records_every_pair_and_exposes_unmatched_prefix():
    first, second = reports()
    result = compare_trials(first, second)
    for item in result["parameters"].values():
        assert item["coverage_pairs"] == {"both_covered": 1, "first_only_covered": 1,
                                          "second_only_covered": 1, "neither_covered": 1}
        assert item["paired_coverage_difference"] == 0
        assert item["second_to_first_width_ratio_median"] == 1
    second["trials"].pop()
    second["repetitions"] -= 1
    short = compare_trials(first, second)
    assert short["matched_trials"] == 3 and short["unmatched_first_trials"] == 1
    assert not short["coverage_qualified"]


def test_missing_intervals_are_counted_in_pairs_but_not_as_zero_width_ratios():
    first, second = reports()
    first["trials"][0]["covered"] = dict.fromkeys(PARAMETERS, False)
    first["trials"][0]["intervals"] = None
    report = compare_trials(first, second)
    for item in report["parameters"].values():
        assert sum(item["coverage_pairs"].values()) == 4
        assert item["coverage_pairs"]["second_only_covered"] == 2
        assert item["positive_first_width_pairs"] == 3


@pytest.mark.parametrize("defect", ["generator", "seed", "method", "trial_seed", "sequence", "flag", "bounds", "false_coverage", "resamples"])
def test_invalid_pair_identity_and_corrupted_intervals_are_rejected(defect):
    first, second = reports()
    if defect in {"generator", "seed"}:
        second[defect] = "changed"
    elif defect == "method":
        second["bootstrap_config_template"]["interval_method"] = "studentized"
    elif defect == "trial_seed":
        second["trials"][0]["bootstrap_seed"] = 999
    elif defect == "sequence":
        second["trials"][1]["trial"] = 0
    elif defect == "resamples":
        second["bootstrap_resamples"] = 200
    else:
        row = second["trials"][0]
        if defect == "flag":
            row["covered"]["center_hz"] = "true"
        elif defect == "false_coverage":
            row["covered"]["center_hz"] = False
        else:
            row["intervals"]["center_hz"] = [float("nan"), 2]
    with pytest.raises(ValueError):
        compare_trials(first, second)


def test_cli_writes_diagnostic_without_overwriting_sources_or_destination(tmp_path):
    first, second = reports()
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    for path, report in zip(paths, (first, second), strict=True):
        path.write_text(json.dumps(report), encoding="utf-8")
    before = [path.read_bytes() for path in paths]
    output = tmp_path / "comparison.json"
    command = [sys.executable, "-m", "tools.compare_spectrum_bootstrap_trials", *map(str, paths), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["matched_trials"] == 4
    assert all(path.read_bytes() == content for path, content in zip(paths, before, strict=True))
    saved = output.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and output.read_bytes() == saved
