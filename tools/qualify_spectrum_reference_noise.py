"""Synthetic temporal noise through reference diagnostics; no inferred independence."""

import argparse
import json
from pathlib import Path

import numpy as np

from app.domain.spectrum_correction import (
    SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole, SweepEvidence,
)
from app.spectrum.reference_diagnostics import ReferenceDiagnosticConfig, diagnose_reference

SCENARIOS = ("white_gamma", "correlated_ar1", "telegraph", "impulses", "flicker_1f", "linear_drift")


def synthetic_power_history(name, samples, seed):
    """Positive W, explicit temporal law; no clipping, smoothing or detrending."""
    if name not in SCENARIOS:
        raise ValueError("Choose a supported temporal-noise scenario.")
    if type(samples) is not int or not 8 <= samples <= 16388:
        raise ValueError("Require 8..16388 synthetic power samples.")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("Require an unsigned 64-bit seed.")
    random = np.random.default_rng(np.random.SeedSequence([seed, SCENARIOS.index(name)]))
    amplitude = 2e-11
    noise = random.gamma(1, amplitude, samples)
    parameters = {"floor_w": 1e-9, "gamma_shape": 1, "gamma_mean_w": amplitude}
    if name == "correlated_ar1":
        coefficient = .98
        noise = np.empty(samples)
        noise[0] = random.normal(0, amplitude)
        for index in range(1, samples):
            noise[index] = coefficient * noise[index - 1] + random.normal(0, amplitude * np.sqrt(1 - coefficient**2))
        parameters = {"floor_w": 1e-9, "ar1_coefficient": coefficient, "stationary_std_w": amplitude}
    elif name == "telegraph":
        flips = random.random(samples) < .01
        initial = random.choice([-1, 1])
        noise += initial * amplitude * (-1.0)**np.cumsum(flips)
        parameters.update({"flip_probability_per_sweep": .01, "level_offset_w": amplitude,
                           "realized_flips": int(flips.sum())})
    elif name == "impulses":
        impulses = random.random(samples) < .01
        noise += impulses * 5e-10
        parameters.update({"impulse_probability_per_sweep": .01, "impulse_amplitude_w": 5e-10,
                           "realized_impulses": int(impulses.sum())})
    elif name == "flicker_1f":
        # Finite periodic Gaussian synthesis with declared PSD ~ 1/f. The
        # zero-frequency mode is absent; no claim of an infinite stationary law.
        bins = np.fft.rfftfreq(samples)
        coefficients = random.normal(size=bins.size) + 1j * random.normal(size=bins.size)
        coefficients[0] = 0
        coefficients[1:] /= np.sqrt(bins[1:])
        noise = np.fft.irfft(coefficients, n=samples)
        noise *= amplitude / np.std(noise)
        parameters = {"floor_w": 1e-9, "psd_exponent": -1, "realized_std_w": amplitude,
                      "construction": "finite_periodic_fourier_gaussian_DC_zero",
                      "lowest_frequency_cycles_per_sweep": float(bins[1])}
    elif name == "linear_drift":
        noise = np.linspace(0, 1e-10, samples)
        parameters = {"floor_w": 1e-9, "total_power_change_w": 1e-10}
    powers = 1e-9 + noise
    if not np.all(np.isfinite(powers)) or np.any(powers <= 0):
        raise ValueError("Synthetic law generated invalid power; no clipping permitted.")
    return powers, parameters


def temporal_noise_report(*, blocks=1024, seed=20261025, scenarios=SCENARIOS):
    if type(blocks) is not int or not 32 <= blocks <= 4096:
        raise ValueError("Require 32..4096 complete diagnostic blocks.")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("Require an unsigned 64-bit seed.")
    if not scenarios or any(name not in SCENARIOS for name in scenarios) or len(set(scenarios)) != len(scenarios):
        raise ValueError("Choose unique supported scenarios.")
    samples, sweeps_per_block, cadence_s = (blocks + 1) * 4, 4, .025
    context = SpectrumAcquisitionContext([1e6, 2e6, 3e6], "synthetic-temporal-noise-diagnostics")
    config = ReferenceDiagnosticConfig(block_duration_s=.1, maximum_blocks=blocks,
                                       maximum_lag=min(128, blocks - 1))
    results = {}
    for name in scenarios:
        powers, parameters = synthetic_power_history(name, samples, seed)

        def frames():
            for index, power in enumerate(powers):
                envelope = SpectrumFrameEnvelope(index, name, context.context_id, 0,
                    1_700_000_000 + index * cadence_s, role=SpectrumFrameRole.REFERENCE,
                    evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
                yield envelope, 10 * np.log10([1e-9, power, 2e-9]) + 30

        diagnostics = diagnose_reference(context, frames(), (1,), config)
        # Independent oracle uses original SI powers and direct adjacent slices,
        # rather than the production prefix-sum implementation or FFT ACF.
        means = powers[:-sweeps_per_block].reshape(blocks, sweeps_per_block).mean(axis=1)
        factors = np.rint(diagnostics.allan_tau_s / config.block_duration_s).astype(int)
        allan = np.array([.5 * np.mean([
            (means[start + m:start + 2 * m].mean() - means[start:start + m].mean())**2
            for start in range(blocks - 2 * m + 1)]) for m in factors])
        centered = means - means.mean()
        denominator = np.dot(centered, centered)
        acf = np.array([np.dot(centered[:blocks - lag], centered[lag:]) / denominator
                        for lag in range(len(diagnostics.correlation_lag_s))])
        means_match = bool(np.allclose(diagnostics.block_mean_w[:, 0], means, rtol=1e-13, atol=0))
        allan_match = bool(np.allclose(diagnostics.allan_variance_w2[:, 0], allan, rtol=1e-9, atol=1e-38))
        acf_match = bool(np.allclose(diagnostics.autocorrelation[:, 0], acf, rtol=1e-8, atol=1e-10))
        counts_match = diagnostics.total_sweeps == samples and diagnostics.discarded_tail_sweeps == 4 and bool(
            np.all(diagnostics.block_counts == 4))
        gates = {"block_means_match_original_SI": means_match,
                 "allan_matches_direct_oracle": allan_match, "acf_matches_direct_oracle": acf_match,
                 "all_sweep_counts_preserved": counts_match,
                 "cadence_valid": diagnostics.cadence_valid and not diagnostics.issues}
        results[name] = {"generator": parameters, "gates": gates, "passed": all(gates.values()),
            "raw_power_w": powers.tolist(), "block_mean_w": diagnostics.block_mean_w[:, 0].tolist(),
            "allan_tau_s": diagnostics.allan_tau_s.tolist(),
            "allan_variance_w2": diagnostics.allan_variance_w2[:, 0].tolist(),
            "correlation_lag_s": diagnostics.correlation_lag_s.tolist(),
            "autocorrelation": diagnostics.autocorrelation[:, 0].tolist(),
            "sweep_independence_inferred": False, "qualified_ttl_s": None,
            "stationarity_automatically_classified": False}
    return {"algorithm": "synthetic-reference-temporal-noise-oracles-v1", "seed": seed,
        "numpy_version": np.__version__, "blocks": blocks, "sweeps_per_block": sweeps_per_block,
        "cadence_s": cadence_s, "block_duration_s": config.block_duration_s,
        "context_id": context.context_id, "frequency_hz": context.frequencies_hz.tolist(),
        "diagnostic_bin_indices": [1], "selected_frequency_hz": 2e6,
        "units": {"raw_power_w": "W", "block_mean_w": "W", "allan_variance_w2": "W^2",
                  "allan_tau_s": "s", "correlation_lag_s": "s", "autocorrelation": "1",
                  "frequency_hz": "Hz", "cadence_s": "s", "block_duration_s": "s"},
        "scenarios": results, "all_numerical_gates_passed": all(row["passed"] for row in results.values()),
        "laboratory_qualified": False, "confidence_intervals_qualified": False,
        "limitations": ["Numerical diagnostic validation only; not automatic noise classification",
            "No signal-free state, sweep independence, effective N, TTL or CI inferred",
            "No instrument RBW, physical hardware averaging or archived measurement replay qualified",
            "1/f construction is finite and periodic with a declared low-frequency cutoff"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--blocks", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=20261025)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=SCENARIOS)
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(1, "Choose a new report destination.\n")
    report = temporal_noise_report(blocks=args.blocks, seed=args.seed, scenarios=args.scenarios)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({name: row["gates"] for name, row in report["scenarios"].items()}, indent=2))
    if not report["all_numerical_gates_passed"]:
        parser.exit(1, "Numerical diagnostic gates failed; report preserved.\n")


if __name__ == "__main__":
    main()
