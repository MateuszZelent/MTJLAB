"""Whole-spectrum covariance, shared REF and explicit uncertainty qualification."""

from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("scipy", reason="Optional qualification dependencies are required")

from app.domain.errors import ProcessingCancelled
from app.spectrum.resonance_bootstrap import ResonanceBootstrapConfig, bootstrap_resonance_blocks
from app.spectrum.resonance_metrics import resonance_values


def fixture():
    frequencies = np.linspace(1e6, 2e6, 101)
    shape = resonance_values(frequencies, 1, 1.5e6, 1e5)
    random = np.random.default_rng(301)
    references = 1e-9 + random.normal(0, 1e-11, 24)[:, None] * shape
    signals = 1e-9 + np.full((24, 1), 1e-10) * shape
    config = ResonanceBootstrapConfig(resamples=256, independent_blocks_qualified=True,
                                      reference_equivalence_qualified=True, stationary_signal_qualified=True,
                                      qualification_evidence="Known independent synthetic block means")
    return frequencies, references, signals, config


def calculate(frequencies, references, signals, config, **extra):
    return bootstrap_resonance_blocks(frequencies, references, signals, initial_center_hz=1.5e6,
                                      initial_fwhm_hz=1e5, config=config, **extra)


def test_no_ci_is_published_without_all_three_assumptions():
    frequencies, references, signals, config = fixture()
    for name in ("independent_blocks_qualified", "reference_equivalence_qualified", "stationary_signal_qualified"):
        result = calculate(frequencies, references, signals, replace(config, **{name: False}))
        assert result["confidence_intervals"] is None
        assert result["resamples_completed"] == 0 and result["status"] == "unqualified"


def test_frequency_correlations_and_reference_error_survive_many_signal_blocks():
    frequencies, references, signals, config = fixture()
    before_ref, before_signal = references.copy(), signals.copy()
    result = calculate(frequencies, references, signals, config)
    repeat_signal = calculate(frequencies, references, np.tile(signals[:1], (128, 1)), config)
    low, high = result["confidence_intervals"]["amplitude_w"]
    repeated_low, repeated_high = repeat_signal["confidence_intervals"]["amplitude_w"]
    assert high - low > 4e-12  # Full-frequency common error is not averaged away as independent bins.
    assert .7 < (repeated_high - repeated_low) / (high - low) < 1.3
    assert result["status"] == "conditional_interval" and not result["coverage_qualified"]
    assert result["failed_fits"] == 0 and result["resamples_completed"] == 256
    covariance = np.asarray(result["parameter_covariance"])
    assert covariance.shape == (4, 4) and covariance[0, 0] > 1e-24
    np.testing.assert_allclose(covariance, covariance.T)
    np.testing.assert_array_equal(references, before_ref)
    np.testing.assert_array_equal(signals, before_signal)


def test_seed_reproduces_signed_intervals_and_few_blocks_remain_unqualified():
    frequencies, references, signals, config = fixture()
    signals = 2e-9 - signals
    result = calculate(frequencies, references, signals, config)
    assert result == calculate(frequencies, references, signals, config)
    assert result["confidence_intervals"]["amplitude_w"][1] < 0
    few = calculate(frequencies, references[:8], signals[:8], config)
    assert few["status"] == "insufficient_blocks" and few["confidence_intervals"] is None


def test_memory_limit_and_cancellation_precede_bootstrap(monkeypatch):
    frequencies, references, signals, config = fixture()
    with pytest.raises(ValueError, match="memory"):
        calculate(frequencies, references, signals, replace(config, working_memory_limit_bytes=1024))

    def cancel():
        raise ProcessingCancelled("injected bootstrap cancellation")

    with pytest.raises(ProcessingCancelled):
        calculate(frequencies, references, signals, config, cancellation_check=cancel)


def test_unstable_bootstrap_fit_never_publishes_conditioned_ci(monkeypatch):
    import app.spectrum.resonance_bootstrap as module

    frequencies, references, signals, config = fixture()
    original = module.fit_linear_resonance
    calls = 0

    def fail_one(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("unidentifiable resample")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "fit_linear_resonance", fail_one)
    result = calculate(frequencies, references, signals, config)
    assert result["failed_fits"] == 1 and result["status"] == "unstable_fit"
    assert result["confidence_intervals"] is None and result["parameter_covariance"] is None


def test_qualification_requires_boolean_flags_and_recorded_evidence():
    with pytest.raises(ValueError, match="evidence"):
        ResonanceBootstrapConfig(independent_blocks_qualified=True)
    with pytest.raises(ValueError, match="booleans"):
        ResonanceBootstrapConfig(independent_blocks_qualified="true")
