"""Paired diagnostics of CI replicas using exactly matching synthetic trials."""

import argparse
import json
from pathlib import Path

import numpy as np

from app.spectrum.resonance_bootstrap import PARAMETERS
from app.storage.finalized_spectrum_store import file_sha256


def compare_trials(first, second):
    if first.get("algorithm") != "synthetic-spectral-bootstrap-coverage-v1":
        raise ValueError("Unsupported synthetic coverage report algorithm.")
    for name in ("algorithm", "seed", "software_versions", "points", "blocks_per_source", "truth", "generator", "units"):
        if name not in first or name not in second or first[name] != second[name]:
            raise ValueError(f"Paired trials require identical {name}.")
    configs = [{key: value for key, value in report["bootstrap_config_template"].items() if key != "resamples"}
               for report in (first, second)]
    if configs[0] != configs[1]:
        raise ValueError("Paired comparison permits only a different number of bootstrap replicas.")
    for report in (first, second):
        resamples = report["bootstrap_resamples"]
        if type(resamples) is not int or not 200 <= resamples <= 10000 or report["bootstrap_config_template"]["resamples"] != resamples:
            raise ValueError("Replica metadata differs from the recorded bootstrap configuration.")
        count = report["repetitions"]
        rows = report["trials"]
        if type(count) is not int or not 1 <= count <= 10000 or len(rows) != count:
            raise ValueError("Paired report needs complete bounded trial records.")
        if any(type(row["trial"]) is not int for row in rows) or [row["trial"] for row in rows] != list(range(count)):
            raise ValueError("Paired report trial identities are not a contiguous sequence.")
        for row in rows:
            if type(row["bootstrap_seed"]) is not int or not 0 <= row["bootstrap_seed"] < 2**64:
                raise ValueError("Trial seeds must be unsigned integer identities.")
            if set(row["covered"]) != set(PARAMETERS) or any(type(value) is not bool for value in row["covered"].values()):
                raise ValueError("Coverage flags must be explicit per-parameter booleans.")
            intervals = row["intervals"]
            if intervals is None:
                if any(row["covered"].values()):
                    raise ValueError("Missing intervals must count as uncovered.")
                continue
            if set(intervals) != set(PARAMETERS):
                raise ValueError("Each available interval must identify all parameters.")
            for name, bounds in intervals.items():
                array = np.asarray(bounds)
                if array.shape != (2,) or array.dtype.kind not in "iuf" or not np.all(np.isfinite(array)) or bounds[0] > bounds[1]:
                    raise ValueError("Interval bounds must be ordered finite real pairs.")
                truth = report["truth"][name]
                if row["covered"][name] != (bounds[0] <= truth <= bounds[1]):
                    raise ValueError("Recorded coverage differs from its interval and truth.")
    count = min(first["repetitions"], second["repetitions"])
    pairs = list(zip(first["trials"][:count], second["trials"][:count], strict=True))
    if any(a["bootstrap_seed"] != b["bootstrap_seed"] for a, b in pairs):
        raise ValueError("Paired trials must use identical per-trial bootstrap seeds.")
    parameters = {}
    for name in PARAMETERS:
        contingency = {"both_covered": 0, "first_only_covered": 0, "second_only_covered": 0, "neither_covered": 0}
        ratios = []
        both_available = 0
        for a, b in pairs:
            flags = a["covered"][name], b["covered"][name]
            key = ("both_covered" if all(flags) else "first_only_covered" if flags[0]
                   else "second_only_covered" if flags[1] else "neither_covered")
            contingency[key] += 1
            if a["intervals"] is not None and b["intervals"] is not None:
                both_available += 1
                first_width = a["intervals"][name][1] - a["intervals"][name][0]
                second_width = b["intervals"][name][1] - b["intervals"][name][0]
                if first_width > 0:
                    ratios.append(second_width / first_width)
        parameters[name] = {"coverage_pairs": contingency, "both_intervals_available": both_available,
                            "positive_first_width_pairs": len(ratios),
                            "second_to_first_width_ratio_median": float(np.median(ratios)) if ratios else None,
                            "paired_coverage_difference": (contingency["second_only_covered"] - contingency["first_only_covered"]) / count}
    return {"algorithm": "paired-bootstrap-replicas-diagnostic-v1", "matched_trials": count,
            "first_total_trials": first["repetitions"], "second_total_trials": second["repetitions"],
            "unmatched_first_trials": first["repetitions"] - count, "unmatched_second_trials": second["repetitions"] - count,
            "first_resamples": first["bootstrap_resamples"], "second_resamples": second["bootstrap_resamples"],
            "parameters": parameters, "coverage_qualified": False,
            "limitations": ["Diagnostic paired prefix, not independent validation or a coverage qualification",
                            "Matching declared generator/configuration/seeds cannot prove absence of unrecorded code changes",
                            "No failures or unmatched trials silently contribute to a paired denominator",
                            "Width ratios use only paired available intervals with positive first width"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = [path.resolve() for path in (args.first, args.second)]
        if args.output.exists() or args.output.resolve() in paths:
            raise ValueError("Choose a new diagnostic report destination.")
        if any(path.stat().st_size > 64 * 1024 * 1024 for path in paths):
            raise ValueError("Input report exceeds the bounded JSON read budget.")
        hashes = {str(path): file_sha256(path) for path in paths}
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        report = compare_trials(*reports)
        if any(file_sha256(path) != hashes[str(path)] for path in paths):
            raise ValueError("Source report changed during analysis.")
        report["source_sha256"] = hashes
        serialized = json.dumps(report, indent=2, allow_nan=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved paired diagnostic of {report['matched_trials']} trials: {args.output}")


if __name__ == "__main__":
    main()
