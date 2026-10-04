"""Independent null validation of one fixed whole-spectrum difference search."""

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path

import numpy as np

from app.spectrum.spectral_difference_test import SpectralDifferenceTestConfig, spectral_difference_test
from app.spectrum.resonance_metrics import resonance_values
from tools.qualify_spectrum_bootstrap_coverage import coverage_statistics, synthetic_trial


def difference_validation_report(*, repetitions=1000, points=101, blocks=40, permutations=999,
                                 seed=20261010, alpha=.005, progress=None):
    for name, value, low, high in (("repetitions", repetitions, 1, 10000), ("points", points, 101, 10001),
                                  ("blocks", blocks, 20, 256), ("seed", seed, 0, 2**64 - 1)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Difference validation {name} must be an integer in {low}..{high}.")
    config = SpectralDifferenceTestConfig(permutations=permutations, alpha=alpha,
        independent_blocks_qualified=True, null_exchangeability_qualified=True,
        qualification_evidence="Known independent identically distributed synthetic null vectors")
    rows, alarms, failures = [], 0, 0
    for trial in range(repetitions):
        frequencies, refs, signals, permutation_seed = synthetic_trial(seed, trial, blocks, points)
        # Remove exactly the deterministic injected signal from this generator;
        # both groups now retain independent draws of the same background law.
        signals -= resonance_values(frequencies, 1e-10, 1.500123e6, 100000.)
        row = {"trial": trial, "permutation_seed": permutation_seed, "p_value": None,
               "global_difference_detected": None, "failure_detail": None}
        try:
            result = spectral_difference_test(frequencies, refs, signals, np.ones(points, dtype=bool),
                config=replace(config, seed=permutation_seed))
            if result["p_value"] is None:
                raise ValueError(f"Unavailable test: {result['status']}")
        except ValueError as exc:
            failures += 1
            row["failure_detail"] = str(exc)
        else:
            row["p_value"] = result["p_value"]
            row["global_difference_detected"] = result["global_difference_detected"]
            alarms += int(result["global_difference_detected"])
        rows.append(row)
        if progress:
            progress(trial + 1, repetitions)
    # A failed test could have alarmed: bound that worst case, never drop it.
    bound = coverage_statistics(alarms + failures, repetitions, nominal=.01)
    gate = {"minimum_1000_independent_trials": repetitions >= 1000,
            "all_tests_available": failures == 0, "false_alarm_95_upper_bound_at_most_one_percent":
                bool(bound["binomial_95_interval"][1] <= .01)}
    return {"algorithm": "synthetic-global-difference-null-validation-v1", "seed": seed,
            "numpy_version": np.__version__, "repetitions": repetitions, "points": points,
            "blocks_per_source": blocks, "test_config_template": asdict(config),
            "search": {"minimum_hz": 1e6, "maximum_hz": 2e6, "bins": points, "one_fixed_look": True},
            "generator": "synthetic-spectral-bootstrap-coverage-v1 with known injected signal removed",
            "false_alarms_observed": alarms, "failed_tests": failures,
            "conservative_alarm_count_including_failures": alarms + failures,
            "conservative_false_alarm_fraction": (alarms + failures) / repetitions,
            "binomial_95_interval": bound["binomial_95_interval"], "gate": gate,
            "synthetic_gate_passed": all(gate.values()), "laboratory_qualified": False, "trials": rows,
            "limitations": ["One fixed complete-null Gaussian-mode/gamma background generator",
                            "Global difference alarm does not identify a resonance",
                            "No temporal drift, model-EMI residual, real RBW or repeated-live-look qualification",
                            "No detector sensitivity qualified; alpha fixed before this validation",
                            "Failures count conservatively as alarms for the binomial upper bound"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1000)
    parser.add_argument("--points", type=int, default=101)
    parser.add_argument("--blocks", type=int, default=40)
    parser.add_argument("--permutations", type=int, default=999)
    parser.add_argument("--seed", type=int, default=20261010)
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(1, "Choose a new report destination.\n")

    def progress(done, total):
        if done % 100 == 0 or done == total:
            print(f"Difference null trials {done}/{total}", flush=True)

    try:
        report = difference_validation_report(repetitions=args.repetitions, points=args.points,
            blocks=args.blocks, permutations=args.permutations, seed=args.seed, progress=progress)
        serialized = json.dumps(report, indent=2, allow_nan=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}; gates: {report['gate']}")


if __name__ == "__main__":
    main()
