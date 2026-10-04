"""Compare local sandwich variance with independent synthetic fit dispersion."""

import argparse
import json
from pathlib import Path
from importlib.metadata import version

import numpy as np

from app.spectrum.resonance_bootstrap import PARAMETERS
from app.spectrum.resonance_metrics import fit_linear_resonance, resonance_area
from app.spectrum.resonance_studentization import block_parameter_covariance
from tools.qualify_spectrum_bootstrap_coverage import synthetic_trial


def summarize_trials(rows, truth):
    """Conditional moments are diagnostic; failures remain in the total count."""
    successful = [row for row in rows if row["status"] == "ok"]
    failures = {}
    for row in rows:
        if row["status"] != "ok":
            failures[row["status"]] = failures.get(row["status"], 0) + 1
    result = {"trials": len(rows), "successful_trials": len(successful), "failures": failures,
              "summary_scope": "Conditional on successful point fits and covariance estimates",
              "parameters": None, "empirical_covariance": None, "mean_estimated_covariance": None}
    if len(successful) < 2:
        return result
    estimates = np.array([[row["estimate"][name] for name in PARAMETERS] for row in successful])
    covariance = np.array([row["covariance"] for row in successful])
    if (covariance.shape != (len(successful), 4, 4) or not np.all(np.isfinite(covariance))
            or not np.all(np.isfinite(estimates)) or np.any(np.diagonal(covariance, axis1=1, axis2=2) <= 0)):
        raise ValueError("Diagnostic requires finite estimates and positive parameter variances.")
    errors = estimates - np.array([truth[name] for name in PARAMETERS])
    se = np.sqrt(np.diagonal(covariance, axis1=1, axis2=2))
    empirical = np.cov(errors, rowvar=False, ddof=1)
    mean_covariance = covariance.mean(axis=0)
    parameters = {}
    for index, name in enumerate(PARAMETERS):
        standardized = errors[:, index] / se[:, index]
        parameters[name] = {
            "bias": float(errors[:, index].mean()),
            "empirical_standard_deviation": float(np.sqrt(empirical[index, index])),
            "root_mean_estimated_variance": float(np.sqrt(mean_covariance[index, index])),
            "empirical_to_estimated_variance_ratio": float(empirical[index, index] / mean_covariance[index, index]),
            "standardized_error_mean": float(standardized.mean()),
            "standardized_error_standard_deviation": float(standardized.std(ddof=1)),
            "standardized_error_quantiles": np.quantile(standardized, [.025, .5, .975]).tolist(),
        }
    result.update(parameters=parameters, empirical_covariance=empirical.tolist(),
                  mean_estimated_covariance=mean_covariance.tolist())
    return result


def standard_error_report(*, repetitions=2000, seed=20261006, blocks=40, points=101, negative=False,
                          progress=None, shape="gaussian"):
    for name, value, low, high in (("repetitions", repetitions, 2, 10000), ("seed", seed, 0, 2**64 - 1),
                                  ("blocks", blocks, 20, 256), ("points", points, 101, 1001)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in {low}..{high}.")
    if type(negative) is not bool:
        raise ValueError("Signal sign must be an explicit boolean.")
    if type(shape) is not str or shape not in {"gaussian", "lorentzian"}:
        raise ValueError("Diagnostic shape must be gaussian or lorentzian.")
    amplitude, center, width = (-1 if negative else 1) * 1e-10, 1.500123e6, 100000.
    truth = dict(zip(PARAMETERS, (amplitude, center, width,
        resonance_area(amplitude, center, width, 1e6, 2e6, shape=shape)), strict=True))
    rows = []
    for trial in range(repetitions):
        frequencies, references, signals, bootstrap_seed = synthetic_trial(seed, trial, blocks, points, negative, shape)
        row = {"trial": trial, "bootstrap_seed": bootstrap_seed, "status": "point_fit_failed",
               "estimate": None, "covariance": None, "failure_detail": None}
        try:
            fit = fit_linear_resonance(frequencies, signals.mean(axis=0) - references.mean(axis=0),
                                      initial_center_hz=center, initial_fwhm_hz=width, shape=shape)
            row["estimate"] = {name: float(getattr(fit, name)) for name in PARAMETERS}
            row["status"] = "covariance_failed"
            row["covariance"] = block_parameter_covariance(frequencies, references, signals, fit).tolist()
            row["status"] = "ok"
        except ValueError as exc:
            row["failure_detail"] = str(exc)
        rows.append(row)
        if progress:
            progress(trial + 1, repetitions)
    summary = summarize_trials(rows, truth)
    return {"algorithm": "synthetic-spectral-sandwich-diagnostic-v1",
            "generator_algorithm": "synthetic-spectral-bootstrap-coverage-v1",
            "seed": seed, "blocks_per_source": blocks, "points": points, "negative": negative,
            "resonance_shape": shape,
            "generator": {"floor_w": 1e-9, "gamma_shape": 16, "gamma_mean_w": 1e-11,
                          "resonance_shape": shape, "frequency_kernel": [.25, .5, .25],
                          "common_shape_standard_deviation_w": 3e-12, "independent_blocks": True,
                          "stationary_signal": True, "reference_equivalence": True},
            "software_versions": {"numpy": np.__version__, "scipy": version("scipy")},
            "parameter_order": list(PARAMETERS), "truth": truth,
            "units": dict(zip(PARAMETERS, ("W", "Hz", "Hz", "W*Hz"), strict=True)),
            "summary": summary, "trials": rows,
            "all_trials_available": not summary["failures"],
            "coverage_qualified": False, "laboratory_qualified": False,
            "limitations": ["Diagnostic moments do not qualify bootstrap coverage",
                            "Failures are reported; conditional moments do not repair missing trials",
                            f"Known single {shape.capitalize()}, independent synthetic blocks and equivalent reference",
                            "No correction factor or changed confidence interval is inferred"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261006)
    parser.add_argument("--blocks", type=int, default=40)
    parser.add_argument("--points", type=int, default=101)
    parser.add_argument("--negative", action="store_true")
    parser.add_argument("--shape", choices=("gaussian", "lorentzian"), default="gaussian")
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(1, "Choose a new report destination.\n")

    def progress(done, total):
        if done % 250 == 0 or done == total:
            print(f"Standard error trials {done}/{total}", flush=True)

    try:
        report = standard_error_report(repetitions=args.repetitions, seed=args.seed, blocks=args.blocks,
                                       points=args.points, negative=args.negative, progress=progress, shape=args.shape)
        serialized = json.dumps(report, indent=2, allow_nan=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
