"""Local influence verified against refitting, whole-block covariance and bootstrap-t."""

from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("scipy", reason="Optional qualification dependencies are required")

from app.spectrum.resonance_bootstrap import PARAMETERS, ResonanceBootstrapConfig, bootstrap_resonance_blocks
from app.spectrum.resonance_metrics import fit_linear_resonance, resonance_values
from app.spectrum.resonance_studentization import block_parameter_covariance, resonance_influence


def data(shape="gaussian", amplitude=1e-10):
    frequencies = np.linspace(7e9, 7.001e9, 101)
    center, width = 7.0005123e9, 1e5
    profile = resonance_values(frequencies, 1, center, width, shape=shape)
    random = np.random.default_rng(56)
    reference = 1e-9 + random.normal(0, 1e-13, (40, 101)) + random.normal(0, 1e-12, (40, 1)) * profile
    signal = 1e-9 + amplitude * profile + random.normal(0, 1e-13, (40, 101))
    kwargs = dict(initial_center_hz=center, initial_fwhm_hz=width, shape=shape)
    fit = fit_linear_resonance(frequencies, signal.mean(axis=0) - reference.mean(axis=0), **kwargs)
    return frequencies, reference, signal, fit, kwargs


@pytest.mark.parametrize("shape", ["gaussian", "lorentzian"])
@pytest.mark.parametrize("amplitude", [1e-10, -1e-10])
def test_influence_predicts_independent_small_perturbation_refit_in_si(shape, amplitude):
    frequencies, reference, signal, fit, kwargs = data(shape, amplitude)
    residual = signal.mean(axis=0) - reference.mean(axis=0)
    perturbation = np.random.default_rng(80).normal(0, 1e-14, 101)
    changed = fit_linear_resonance(frequencies, residual + perturbation, **kwargs)
    influence, transform = resonance_influence(frequencies, fit)
    prediction = transform @ (perturbation @ influence)
    actual = np.array([getattr(changed, name) - getattr(fit, name) for name in PARAMETERS])
    for measured, predicted, tolerance in zip(actual, prediction, (1e-16, .001, .001, 1e-11), strict=True):
        assert measured == pytest.approx(predicted, rel=.02, abs=tolerance)


@pytest.mark.parametrize("shape", ["gaussian", "lorentzian"])
@pytest.mark.parametrize("amplitude", [1e-10, -1e-10])
def test_sandwich_matches_two_source_delete_one_jackknife_without_bin_independence(shape, amplitude):
    frequencies, reference, signal, fit, kwargs = data(shape, amplitude)
    expected = np.zeros((4, 4))
    for role in ("reference", "signal"):
        values = []
        for index in range(40):
            ref = np.delete(reference, index, axis=0) if role == "reference" else reference
            sig = np.delete(signal, index, axis=0) if role == "signal" else signal
            jack = fit_linear_resonance(frequencies, sig.mean(axis=0) - ref.mean(axis=0), **kwargs)
            values.append([getattr(jack, name) for name in PARAMETERS])
        values = np.asarray(values)
        values -= values.mean(axis=0)
        expected += 39 / 40 * values.T @ values
    covariance = block_parameter_covariance(frequencies, reference, signal, fit)
    np.testing.assert_allclose(np.diag(covariance), np.diag(expected), rtol=.01)
    many_signals = np.tile(signal, (5, 1))
    repeated = block_parameter_covariance(frequencies, reference, many_signals, fit)
    assert repeated[0, 0] > .9 * covariance[0, 0]  # Shared REF amplitude error dominates.


def test_studentized_intervals_preserve_sign_seed_and_require_qualification():
    frequencies, reference, signal, _fit, kwargs = data(amplitude=-1e-10)
    config = ResonanceBootstrapConfig(resamples=200, interval_method="studentized",
        independent_blocks_qualified=True, reference_equivalence_qualified=True,
        stationary_signal_qualified=True, qualification_evidence="Independent synthetic blocks")
    result = bootstrap_resonance_blocks(frequencies, reference, signal, config=config, **kwargs)
    assert result["status"] == "conditional_interval", result
    assert result["confidence_intervals"]["amplitude_w"][1] < 0
    assert result["studentization"]["method"] == "whole-block-local-linear-sandwich-v1"
    assert not result["coverage_qualified"]
    assert result == bootstrap_resonance_blocks(frequencies, reference, signal, config=config, **kwargs)
    unqualified = bootstrap_resonance_blocks(frequencies, reference, signal,
        config=replace(config, stationary_signal_qualified=False), **kwargs)
    assert unqualified["confidence_intervals"] is None


@pytest.mark.parametrize("shape", ["gaussian", "lorentzian"])
@pytest.mark.parametrize("amplitude", [1e-10, -1e-10])
def test_multiplicity_weights_match_materialized_whole_block_resampling(shape, amplitude):
    frequencies, reference, signal, fit, _kwargs = data(shape, amplitude)
    random = np.random.default_rng(802)
    ref_counts = random.multinomial(40, np.full(40, 1 / 40))
    sig_counts = random.multinomial(40, np.full(40, 1 / 40))
    weighted = block_parameter_covariance(frequencies, reference, signal, fit, ref_counts / 40, sig_counts / 40)
    materialized = block_parameter_covariance(frequencies, np.repeat(reference, ref_counts, axis=0),
                                            np.repeat(signal, sig_counts, axis=0), fit)
    np.testing.assert_allclose(weighted, materialized, rtol=1e-9, atol=0)


def test_zero_block_variance_refuses_studentization_instead_of_dividing_by_zero():
    frequencies, reference, signal, _fit, kwargs = data()
    reference[:] = reference.mean(axis=0)
    signal[:] = signal.mean(axis=0)
    config = ResonanceBootstrapConfig(resamples=200, interval_method="studentized",
        independent_blocks_qualified=True, reference_equivalence_qualified=True,
        stationary_signal_qualified=True, qualification_evidence="Synthetic only")
    result = bootstrap_resonance_blocks(frequencies, reference, signal, config=config, **kwargs)
    assert result["status"] == "studentization_failed" and result["confidence_intervals"] is None
    assert result["resamples_completed"] == 0


def test_failed_studentization_does_not_publish_conditioned_ci(monkeypatch):
    import app.spectrum.resonance_bootstrap as module

    frequencies, reference, signal, _fit, kwargs = data()
    config = ResonanceBootstrapConfig(resamples=200, interval_method="studentized",
        independent_blocks_qualified=True, reference_equivalence_qualified=True,
        stationary_signal_qualified=True, qualification_evidence="Synthetic only")
    original = module.block_parameter_covariance
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("injected zero variance")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "block_parameter_covariance", fail_once)
    result = bootstrap_resonance_blocks(frequencies, reference, signal, config=config, **kwargs)
    assert result["failed_fits"] == 0 and result["failed_studentizations"] == 1
    assert result["status"] == "unstable_fit" and result["confidence_intervals"] is None
    with pytest.raises(ValueError, match="method"):
        ResonanceBootstrapConfig(interval_method="auto")
