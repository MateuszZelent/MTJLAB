"""Independent synthetic coverage trials of whole-spectrum bootstrap intervals."""

import argparse
from dataclasses import asdict
import json
from importlib.metadata import version
from pathlib import Path

import numpy as np

from app.spectrum.resonance_bootstrap import PARAMETERS, ResonanceBootstrapConfig, bootstrap_resonance_blocks
from app.spectrum.resonance_metrics import resonance_area, resonance_values


def coverage_statistics(covered, repetitions, nominal=.95):
    from scipy.stats import binomtest

    if type(covered) is not int or type(repetitions) is not int or not 0 <= covered <= repetitions or repetitions < 1:
        raise ValueError("Coverage needs integer successes within a positive trial count.")
    interval = binomtest(covered, repetitions).proportion_ci(confidence_level=.95, method="exact")
    return {"covered": covered, "trials": repetitions, "fraction": covered / repetitions,
            "binomial_95_interval": [interval.low, interval.high],
            "nominal_within_binomial_interval": bool(interval.low <= nominal <= interval.high)}


def synthetic_trial(seed, trial, blocks, points, negative=False, shape="gaussian"):
    """The v1 coverage generator; preserve draw order for paired diagnostics."""
    frequencies = np.linspace(1e6, 2e6, points)
    amplitude, center, width = (-1 if negative else 1) * 1e-10, 1.500123e6, 100000.
    profile = resonance_values(frequencies, 1, center, width, shape=shape)
    data_seed, bootstrap_seed = np.random.SeedSequence([seed, trial]).spawn(2)
    random = np.random.default_rng(data_seed)

    def noise():
        power = random.gamma(16, 1e-11 / 16, size=(blocks, points))
        correlated = .25 * np.roll(power, 1, axis=1) + .5 * power + .25 * np.roll(power, -1, axis=1)
        return correlated + random.normal(0, 3e-12, (blocks, 1)) * profile

    references = 1e-9 + noise()
    signals = 1e-9 + amplitude * profile + noise()
    return frequencies, references, signals, int(bootstrap_seed.generate_state(1, dtype=np.uint64)[0])


def bootstrap_coverage_report(*, repetitions=1000, resamples=200, blocks=40, points=101,
                              seed=20261005, negative=False, progress=None, trial_record=None,
                              interval_method="percentile", shape="gaussian"):
    for name, value, low, high in (("repetitions", repetitions, 1, 10000), ("blocks", blocks, 20, 256),
                                  ("points", points, 101, 1001), ("seed", seed, 0, 2**64 - 1)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Coverage {name} must be an integer in {low}..{high}.")
    if type(negative) is not bool:
        raise ValueError("Signal sign must be an explicit boolean.")
    if type(shape) is not str or shape not in {"gaussian", "lorentzian"}:
        raise ValueError("Coverage shape must be gaussian or lorentzian.")
    config = ResonanceBootstrapConfig(resamples=resamples, interval_method=interval_method, independent_blocks_qualified=True,
        reference_equivalence_qualified=True, stationary_signal_qualified=True,
        qualification_evidence="Independent synthetic blocks with identical declared background distributions")
    frequencies = np.linspace(1e6, 2e6, points)
    amplitude, center, width = (-1 if negative else 1) * 1e-10, 1.500123e6, 100000.
    truth = dict(zip(PARAMETERS, (amplitude, center, width,
        resonance_area(amplitude, center, width, frequencies[0], frequencies[-1], shape=shape)), strict=True))
    counts = dict.fromkeys(PARAMETERS, 0)
    failures = {}
    rows = []
    for trial in range(repetitions):
        frequencies, references, signals, bootstrap_seed = synthetic_trial(seed, trial, blocks, points, negative, shape)
        trial_config = ResonanceBootstrapConfig(**{
            **asdict(config), "seed": bootstrap_seed,
        })
        try:
            result = bootstrap_resonance_blocks(frequencies, references, signals,
                initial_center_hz=center, initial_fwhm_hz=width, config=trial_config, shape=shape)
        except ValueError as exc:
            status, intervals = "point_fit_failed", None
            detail = str(exc)
        else:
            status, intervals = result["status"], result["confidence_intervals"]
            detail = f"{result['failed_fits']} bootstrap fits failed; {result.get('failed_studentizations', 0)} studentizations failed"
        covered = {name: bool(intervals is not None and intervals[name][0] <= value <= intervals[name][1])
                   for name, value in truth.items()}
        for name, value in covered.items():
            counts[name] += int(value)
        if intervals is None:
            failures[status] = failures.get(status, 0) + 1
        rows.append({"trial": trial, "bootstrap_seed": trial_config.seed, "status": status,
                     "covered": covered, "intervals": intervals, "failure_detail": detail if intervals is None else None})
        if trial_record:
            trial_record(rows[-1])
        if progress:
            progress(trial + 1, repetitions)
    coverage = {name: coverage_statistics(count, repetitions, config.confidence_level) for name, count in counts.items()}
    return {"algorithm": "synthetic-spectral-bootstrap-coverage-v1", "seed": seed,
        "software_versions": {"numpy": np.__version__, "scipy": version("scipy")},
        "units": {"amplitude_w": "W", "center_hz": "Hz", "fwhm_hz": "Hz", "finite_window_area_w_hz": "W*Hz"},
        "repetitions": repetitions, "bootstrap_resamples": resamples, "blocks_per_source": blocks,
        "points": points, "truth": truth, "bootstrap_config_template": asdict(config),
        "generator": {"floor_w": 1e-9, "gamma_shape": 16, "gamma_mean_w": 1e-11,
                      "resonance_shape": shape,
                      "frequency_kernel": [.25, .5, .25], "common_shape_standard_deviation_w": 3e-12,
                      "independent_blocks": True, "stationary_signal": True, "reference_equivalence": True},
        "coverage": coverage, "failures": failures, "trials": rows,
        "gate": {"minimum_1000_independent_trials": repetitions >= 1000,
                 "all_intervals_available": not failures,
                 "nominal_within_all_marginal_binomial_intervals": all(
                     item["nominal_within_binomial_interval"] for item in coverage.values())},
        "laboratory_qualified": False, "false_detection_rate": None,
        "limitations": [f"Known single {shape.capitalize()} hypothesis; no automatic peak detector",
                        "Missing intervals count as uncovered; failures are never discarded",
                        "Marginal coverage checks are not a simultaneous-family guarantee",
                        "Synthetic generator is not instrument RBW or laboratory qualification",
                        "No model-EMI, drift, coherent fields or correlated temporal blocks qualified"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1000)
    parser.add_argument("--resamples", type=int, default=200)
    parser.add_argument("--interval-method", choices=("percentile", "studentized"), default="percentile")
    parser.add_argument("--blocks", type=int, default=40)
    parser.add_argument("--points", type=int, default=101)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--negative", action="store_true")
    parser.add_argument("--shape", choices=("gaussian", "lorentzian"), default="gaussian")
    args = parser.parse_args()
    journal = args.output.with_suffix(".trials.jsonl")
    if args.output.exists() or journal.exists():
        parser.exit(1, "Choose a new report destination.\n")

    def progress(done, total):
        if done % 25 == 0 or done == total:
            print(f"Coverage trials {done}/{total}", flush=True)

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "incomplete_trial_journal", "seed": args.seed,
                "repetitions": args.repetitions, "resamples": args.resamples, "blocks": args.blocks,
                "points": args.points, "negative": args.negative, "interval_method": args.interval_method,
                "shape": args.shape}) + "\n")
            stream.flush()

            def record(row):
                stream.write(json.dumps(row, allow_nan=False) + "\n")
                stream.flush()

            report = bootstrap_coverage_report(repetitions=args.repetitions, resamples=args.resamples,
                blocks=args.blocks, points=args.points, seed=args.seed, negative=args.negative,
                progress=progress, trial_record=record, interval_method=args.interval_method, shape=args.shape)
        serialized = json.dumps(report, indent=2, allow_nan=False)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}; gates: {report['gate']}")


if __name__ == "__main__":
    main()
