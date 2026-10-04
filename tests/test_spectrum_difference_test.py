"""Whole-vector permutations preserve signs, correlation and bounded work."""

from dataclasses import replace

import numpy as np
import pytest

from app.spectrum.spectral_difference_test import SpectralDifferenceTestConfig, spectral_difference_test


def fixture():
    random = np.random.default_rng(12)
    refs = 1e-9 + random.gamma(16, 1e-12 / 16, (20, 31))
    signals = 1e-9 + random.gamma(16, 1e-12 / 16, (20, 31))
    return np.linspace(1e6, 2e6, 31), refs, signals, np.ones(31, dtype=bool)


def config():
    return SpectralDifferenceTestConfig(permutations=99, independent_blocks_qualified=True,
        null_exchangeability_qualified=True, qualification_evidence="Known independent synthetic null blocks")


@pytest.mark.parametrize("sign", [1, -1])
def test_strong_signed_difference_is_flagged_without_mutating_data(sign):
    frequencies, refs, signals, mask = fixture()
    signals[:, 15] += sign * 1e-10
    original = refs.copy(), signals.copy()
    progress = []
    report = spectral_difference_test(frequencies, refs, signals, mask, config=config(),
                                      progress_callback=lambda *values: progress.append(values))
    assert report["p_value"] == .01
    assert report["global_difference_detected"]
    assert report == spectral_difference_test(frequencies, refs, signals, mask, config=config())
    assert progress[-1] == (99, 99)
    np.testing.assert_array_equal(refs, original[0])
    np.testing.assert_array_equal(signals, original[1])
    assert not report["false_alarm_rate_qualified"] and not report["laboratory_qualified"]


def test_fixed_mask_excludes_unsearched_difference_and_constant_bins_are_safe():
    frequencies, refs, signals, mask = fixture()
    refs[:] = 1e-9
    signals[:] = 1e-9
    signals[:, 15] += 1e-10
    mask[15] = False
    report = spectral_difference_test(frequencies, refs, signals, mask, config=config())
    assert report["p_value"] == 1
    assert not report["global_difference_detected"]
    assert 15 not in report["search_bin_indices"]
    assert report["analytical_constant_case"]
    assert report["permutations_completed"] == 0 and report["exceedances"] is None


def test_units_and_duplicate_correlated_bins_do_not_change_global_statistic():
    frequencies, refs, signals, mask = fixture()
    report = spectral_difference_test(frequencies, refs, signals, mask, config=config())
    scaled = spectral_difference_test(frequencies, refs * 1e-6, signals * 1e-6, mask, config=config())
    duplicated = spectral_difference_test(np.arange(62), np.repeat(refs, 2, axis=1),
        np.repeat(signals, 2, axis=1), np.repeat(mask, 2), config=config())
    assert report["p_value"] == scaled["p_value"] == duplicated["p_value"]
    assert report["observed_statistic"] == pytest.approx(scaled["observed_statistic"], rel=1e-10)
    assert report["observed_statistic"] == pytest.approx(duplicated["observed_statistic"], rel=1e-10)


def test_unqualified_or_small_sources_do_not_produce_p_values():
    data = fixture()
    assert spectral_difference_test(*data)["p_value"] is None
    assert spectral_difference_test(*data, config=replace(config(), null_exchangeability_qualified=False))["p_value"] is None
    frequencies, refs, signals, mask = data
    report = spectral_difference_test(frequencies, refs[:19], signals, mask, config=config())
    assert report["status"] == "insufficient_blocks" and report["p_value"] is None


def test_input_memory_and_unresolved_variance_fail_explicitly():
    frequencies, refs, signals, mask = fixture()
    with pytest.raises(ValueError, match="memory"):
        spectral_difference_test(frequencies, refs, signals, mask,
            config=replace(config(), working_memory_limit_bytes=1024))
    with pytest.raises(ValueError, match="precision"):
        spectral_difference_test(frequencies, refs * 1e-290, signals * 1e-290, mask, config=config())
    with pytest.raises(ValueError, match="boolean"):
        spectral_difference_test(frequencies, refs, signals, mask.astype(int), config=config())
    with pytest.raises(ValueError, match="positive"):
        spectral_difference_test(frequencies, -refs, signals, mask, config=config())


def test_cancel_is_checked_during_permutations():
    checks = []

    def cancel():
        checks.append(True)
        if len(checks) == 4:
            raise RuntimeError("canceled")

    with pytest.raises(RuntimeError, match="canceled"):
        spectral_difference_test(*fixture(), config=config(), cancellation_check=cancel)
    assert len(checks) == 4


@pytest.mark.parametrize("kwargs", [{"alpha": 0}, {"alpha": True}, {"alpha": .001, "permutations": 99},
                                   {"seed": -1}, {"independent_blocks_qualified": True},
                                   {"null_exchangeability_qualified": 1}])
def test_config_rejects_unresolvable_or_undeclared_assumptions(kwargs):
    with pytest.raises(ValueError):
        SpectralDifferenceTestConfig(**kwargs)


def test_null_validation_counts_failures_as_alarms_for_upper_bound(monkeypatch):
    pytest.importorskip("scipy")
    import tools.qualify_spectrum_difference_test as module

    def fail(*args, **kwargs):
        raise ValueError("unavailable variance")

    monkeypatch.setattr(module, "spectral_difference_test", fail)
    report = module.difference_validation_report(repetitions=3)
    assert report["false_alarms_observed"] == 0
    assert report["failed_tests"] == 3
    assert report["conservative_alarm_count_including_failures"] == 3
    assert report["conservative_false_alarm_fraction"] == 1
    assert report["binomial_95_interval"][1] == 1
    assert not report["synthetic_gate_passed"]


def test_null_validation_is_independent_reproducible_and_never_laboratory_qualified():
    pytest.importorskip("scipy")
    from tools.qualify_spectrum_difference_test import difference_validation_report

    report = difference_validation_report(repetitions=3)
    assert report == difference_validation_report(repetitions=3)
    assert report["failed_tests"] == 0
    assert len({row["permutation_seed"] for row in report["trials"]}) == 3
    assert not report["laboratory_qualified"]
    assert not report["gate"]["minimum_1000_independent_trials"]
    assert report["test_config_template"]["alpha"] == .005
    assert all(row["p_value"] >= .001 for row in report["trials"])
    import json

    json.dumps(report, allow_nan=False)


def test_validation_cli_publishes_json_and_refuses_overwrite(tmp_path):
    pytest.importorskip("scipy")
    import json
    import subprocess
    import sys

    output = tmp_path / "validation.json"
    command = [sys.executable, "-m", "tools.qualify_spectrum_difference_test", "--output", str(output),
               "--repetitions", "3"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    original = output.read_bytes()
    assert not json.loads(original)["synthetic_gate_passed"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert output.read_bytes() == original


def test_wide_grid_batches_match_scalar_permutations_without_storing_all_replicas():
    from time import perf_counter
    import json
    from pathlib import Path

    frequencies, refs, signals, mask = fixture()
    # Repeat a correlated bin pattern to exercise the full grid without
    # introducing a different statistical problem or additional independence.
    indices = np.arange(10001) % len(frequencies)
    refs, signals = refs[:, indices], signals[:, indices]
    frequencies = np.linspace(1e6, 2e6, 10001)
    mask = np.ones(10001, dtype=bool)
    settings = replace(config(), permutations=999)
    pooled = np.concatenate((refs, signals))
    pooled -= pooled.mean(axis=0)
    pooled /= pooled.std(axis=0, ddof=1) * np.sqrt(1 / len(refs) + 1 / len(signals))
    total = pooled.sum(axis=0)

    def scalar_statistic(selected):
        ref_sum = pooled[selected].sum(axis=0)
        return np.max(np.abs((total - ref_sum) / len(signals) - ref_sum / len(refs)))

    observed = scalar_statistic(np.arange(len(refs)))
    threshold = observed - 64 * np.finfo(float).eps * max(1., observed)
    random = np.random.default_rng(settings.seed)
    start = perf_counter()
    count = sum(scalar_statistic(random.permutation(len(pooled))[:len(refs)]) >= threshold
                for _ in range(settings.permutations))
    scalar_elapsed = perf_counter() - start
    progress = []
    start = perf_counter()
    report = spectral_difference_test(frequencies, refs, signals, mask, config=settings,
        progress_callback=lambda *values: progress.append(values))
    batched_elapsed = perf_counter() - start
    assert report["exceedances"] == int(count)
    assert report["p_value"] == (int(count) + 1) / 1000
    assert progress[-1] == (999, 999) and len(progress) == 63
    assert report["estimated_buffer_bytes"] < 16 * 1024 * 1024
    assert report["permutation_batch_size"] == 16
    directory = Path("artifacts/spectrum-bootstrap")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "wide-permutation-comparison.json").write_text(json.dumps({
        "points": 10001, "reference_blocks": len(refs), "signal_blocks": len(signals),
        "permutations": 999, "scalar_elapsed_s": scalar_elapsed,
        "batched_elapsed_s": batched_elapsed, "exceedances": int(count),
        "scope": "One correlated synthetic case; no throughput or laboratory qualification",
    }, indent=2), encoding="utf-8")


def test_wide_grid_cancellation_checks_each_batch():
    frequencies, refs, signals, mask = fixture()
    indices = np.arange(2001) % len(frequencies)
    progress = []

    def cancel():
        if progress:
            raise RuntimeError("canceled after first batch")

    with pytest.raises(RuntimeError, match="first batch"):
        spectral_difference_test(np.arange(2001), refs[:, indices], signals[:, indices],
            np.ones(2001, dtype=bool), config=config(), cancellation_check=cancel,
            progress_callback=lambda *values: progress.append(values))
    assert progress == [(16, 99)]
