"""Reconstruct studentized bootstrap cutoffs from paired archived CI and SE trials."""

import argparse
import json
from pathlib import Path

import numpy as np

from app.spectrum.resonance_bootstrap import PARAMETERS
from app.storage.finalized_spectrum_store import file_sha256
from tools.compare_spectrum_bootstrap_trials import compare_trials


def diagnose_pivots(coverage, diagnostic):
    # Also validates each interval against the stored truth and coverage flags.
    compare_trials(coverage, coverage)
    if (coverage["bootstrap_config_template"]["interval_method"] != "studentized"
            or coverage["bootstrap_config_template"]["confidence_level"] != .95):
        raise ValueError("Pivot diagnosis requires studentized 95% marginal intervals.")
    if (diagnostic.get("algorithm") != "synthetic-spectral-sandwich-diagnostic-v1"
            or diagnostic.get("generator_algorithm") != coverage["algorithm"]):
        raise ValueError("Unsupported standard-error diagnostic generator.")
    for name in ("seed", "software_versions", "points", "blocks_per_source", "truth", "units"):
        if diagnostic.get(name) != coverage[name]:
            raise ValueError(f"Paired pivot diagnosis requires identical {name}.")
    generator = {"floor_w": 1e-9, "gamma_shape": 16, "gamma_mean_w": 1e-11,
                 "frequency_kernel": [.25, .5, .25], "common_shape_standard_deviation_w": 3e-12,
                 "independent_blocks": True, "stationary_signal": True, "reference_equivalence": True}
    shape = coverage["generator"].get("resonance_shape", "gaussian")
    if type(shape) is not str or shape not in {"gaussian", "lorentzian"}:
        raise ValueError("Unsupported paired resonance shape.")
    # Legacy diagnostics predate explicit shape metadata and describe only
    # the fixed Gaussian generator. They cannot be paired with a Lorentzian.
    if diagnostic.get("resonance_shape", "gaussian") != shape:
        raise ValueError("Paired resonance shapes differ.")
    generator["resonance_shape"] = shape
    coverage_generator = dict(coverage["generator"])
    coverage_generator.setdefault("resonance_shape", "gaussian")
    diagnostic_generator = diagnostic.get("generator")
    if diagnostic_generator is None:
        if shape != "gaussian":
            raise ValueError("Lorentzian diagnostics require explicit generator provenance.")
        diagnostic_generator = generator
    if (coverage_generator != generator
            or diagnostic_generator != generator
            or diagnostic["parameter_order"] != list(PARAMETERS)):
        raise ValueError("Diagnostic generator or parameter order differs.")
    negative = diagnostic["negative"]
    if type(negative) is not bool or negative != (coverage["truth"]["amplitude_w"] < 0):
        raise ValueError("Paired signal sign differs.")
    rows = diagnostic["trials"]
    if (not 2 <= len(rows) <= 10000 or diagnostic["summary"]["trials"] != len(rows)
            or any(type(row["trial"]) is not int for row in rows)
            or [row["trial"] for row in rows] != list(range(len(rows)))):
        raise ValueError("Standard-error diagnostic requires complete contiguous trial identities.")
    count = min(coverage["repetitions"], len(rows))
    errors, cutoffs = [], []
    missing = {"missing_interval": 0, "missing_standard_error": 0, "both_missing": 0}
    for row, ci in zip(rows[:count], coverage["trials"][:count], strict=True):
        if type(row["bootstrap_seed"]) is not int or row["bootstrap_seed"] != ci["bootstrap_seed"]:
            raise ValueError("Paired trial seeds differ.")
        if row["status"] not in ("ok", "point_fit_failed", "covariance_failed"):
            raise ValueError("Unknown standard-error trial status.")
        if row["status"] != "ok" or ci["intervals"] is None:
            key = ("both_missing" if row["status"] != "ok" and ci["intervals"] is None
                   else "missing_standard_error" if row["status"] != "ok" else "missing_interval")
            missing[key] += 1
            continue
        covariance = np.asarray(row["covariance"])
        estimate = np.asarray([row["estimate"][name] for name in PARAMETERS])
        if (covariance.shape != (4, 4) or covariance.dtype.kind not in "iuf"
                or estimate.dtype.kind not in "iuf" or not np.all(np.isfinite(estimate))
                or not np.all(np.isfinite(covariance)) or np.any(np.diag(covariance) <= 0)):
            raise ValueError("Paired standard errors require positive finite variances and real estimates.")
        se = np.sqrt(np.diag(covariance))
        truth = np.array([coverage["truth"][name] for name in PARAMETERS])
        bounds = np.array([ci["intervals"][name] for name in PARAMETERS])
        # CI = estimate - reversed pivot quantiles * original SE.
        cutoffs.append(np.column_stack(((estimate - bounds[:, 1]) / se,
                                       (estimate - bounds[:, 0]) / se)))
        errors.append((estimate - truth) / se)
    parameters = {}
    for index, name in enumerate(PARAMETERS):
        observed = np.array(errors)[:, index] if errors else np.array([])
        tails = np.array(cutoffs)[:, index, :] if cutoffs else np.empty((0, 2))
        low = int(np.sum(observed < tails[:, 0]))
        high = int(np.sum(observed > tails[:, 1]))
        parameters[name] = {
            "available_pairs": len(errors), "below_bootstrap_cutoff": low, "above_bootstrap_cutoff": high,
            "within_bootstrap_cutoffs": len(errors) - low - high,
            "unavailable_pairs": count - len(errors),
            "covered_fraction_all_matched_trials": (len(errors) - low - high) / count,
            "observed_error_025_975_quantiles": np.quantile(observed, [.025, .975]).tolist() if len(errors) >= 2 else None,
            "bootstrap_cutoff_medians": np.median(tails, axis=0).tolist() if len(errors) else None,
            "bootstrap_cutoff_10_90_quantiles": np.quantile(tails, [.1, .9], axis=0).tolist() if len(errors) >= 2 else None,
        }
    return {"algorithm": "paired-studentized-pivot-diagnostic-v1", "matched_trials": count,
            "resonance_shape": shape,
            "unmatched_coverage_trials": coverage["repetitions"] - count,
            "unmatched_standard_error_trials": len(rows) - count, "missing_pairs": missing,
            "parameters": parameters, "coverage_qualified": False, "laboratory_qualified": False,
            "limitations": ["Quantiles reconstructed from archived bounds and paired point fits/SE",
                            "Matched generator, versions and seeds do not prove absence of unrecorded code changes",
                            "Observed error quantiles use only available paired trials; failures remain explicit",
                            "Median bootstrap cutoffs are descriptive, not a new confidence interval",
                            "No automatic scale correction or coverage qualification"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage", type=Path)
    parser.add_argument("standard_errors", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = [path.resolve() for path in (args.coverage, args.standard_errors)]
        if args.output.exists() or args.output.resolve() in paths:
            raise ValueError("Choose a new diagnostic destination.")
        if any(path.stat().st_size > 64 * 1024 * 1024 for path in paths):
            raise ValueError("Source exceeds the bounded JSON read budget.")
        hashes = {str(path): file_sha256(path) for path in paths}
        report = diagnose_pivots(*(json.loads(path.read_text(encoding="utf-8")) for path in paths))
        if any(file_sha256(path) != hashes[str(path)] for path in paths):
            raise ValueError("Source report changed during diagnosis.")
        report["source_sha256"] = hashes
        serialized = json.dumps(report, indent=2, allow_nan=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved diagnostic for {report['matched_trials']} paired trials: {args.output}")


if __name__ == "__main__":
    main()
