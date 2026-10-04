"""Export a standalone comparison of declared synthetic temporal-noise laws."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(1, "Choose a new figure destination.\n")
    if args.output.suffix.lower() not in {".png", ".svg"}:
        parser.exit(1, "Choose PNG or SVG output.\n")
    report = json.loads(args.source.read_text(encoding="utf-8"))
    if report.get("algorithm") != "synthetic-reference-temporal-noise-oracles-v1":
        parser.exit(1, "Unsupported diagnostic report.\n")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, 2, figsize=(12, 4.8), layout="constrained")
    for name, case in report["scenarios"].items():
        label = name.replace("_", " ")
        axes[0].loglog(case["allan_tau_s"], case["allan_variance_w2"], marker=".", label=label)
        axes[1].plot(case["correlation_lag_s"], case["autocorrelation"], label=label)
    axes[0].set(xlabel="Averaging duration (s)", ylabel="Allan power variance (W²)",
                title="Adjacent block averages, overlapping pairs")
    axes[1].set(xlabel="Lag (s)", ylabel="Autocorrelation (1)",
                title="Correlation of power block means")
    axes[1].axhline(0, color="gray", linewidth=.6)
    for axis in axes:
        axis.grid(True, alpha=.2)
        axis.legend(fontsize=8)
    figure.suptitle("Synthetic diagnostic comparison · independence, TTL and CI remain unqualified", fontsize=11)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        figure.savefig(stream, format=args.output.suffix[1:].lower(), dpi=180)
    plt.close(figure)


if __name__ == "__main__":
    main()
