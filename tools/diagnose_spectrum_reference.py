"""Export diagnostics from completed REF raw; python -m tools.diagnose_spectrum_reference."""

import argparse
from pathlib import Path

from app.domain.errors import ExecutionError
from app.spectrum.reference_diagnostics import ReferenceDiagnosticConfig
from app.storage.reference_diagnostic_store import diagnose_reference_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--block-duration", default="1 s", help="Explicit time quantity, e.g. '1 s'.")
    parser.add_argument("--bins", nargs="+", type=int, help="Zero-based bin indices; at most 16 by default.")
    args = parser.parse_args()
    from app.domain.quantities import DIMENSION_TIME, parse_quantity

    try:
        config = ReferenceDiagnosticConfig(block_duration_s=parse_quantity(args.block_duration, DIMENSION_TIME).si_value)
        diagnostics, _report = diagnose_reference_archive(args.source, bin_indices=args.bins,
                                                          config=config, destination=args.output)
    except (ExecutionError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}: {len(diagnostics.block_counts)} blocks; {diagnostics.total_sweeps} raw sweeps.")
    print("Diagnostic only; no qualified TTL, sweep independence or confidence interval.")
    if diagnostics.issues:
        print("Issues: " + ", ".join(diagnostics.issues))


if __name__ == "__main__":
    main()
