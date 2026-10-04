"""Offline global REF/SIGNAL difference alarm for one explicitly bounded search."""

import argparse
from pathlib import Path

from app.domain.errors import ExecutionError
from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_RATIO, parse_quantity
from app.spectrum.spectral_difference_test import SpectralDifferenceTestConfig
from app.storage.spectral_difference_store import analyze_spectral_difference_archives


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("signal", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--block-sweeps", type=int, required=True)
    parser.add_argument("--search-start", required=True)
    parser.add_argument("--search-stop", required=True)
    parser.add_argument("--alpha", default="0.5 %")
    parser.add_argument("--permutations", type=int, default=999)
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--discard-partial-tail", action="store_true")
    parser.add_argument("--independent-blocks-qualified", action="store_true")
    parser.add_argument("--null-exchangeability-qualified", action="store_true")
    parser.add_argument("--qualification-evidence", default="")
    args = parser.parse_args()
    try:
        config = SpectralDifferenceTestConfig(permutations=args.permutations, seed=args.seed,
            alpha=parse_quantity(args.alpha, DIMENSION_RATIO).si_value,
            independent_blocks_qualified=args.independent_blocks_qualified,
            null_exchangeability_qualified=args.null_exchangeability_qualified,
            qualification_evidence=args.qualification_evidence)
        report = analyze_spectral_difference_archives(args.reference, args.signal, args.output,
            block_sweeps=args.block_sweeps, search_start_hz=parse_quantity(args.search_start, DIMENSION_FREQUENCY).si_value,
            search_stop_hz=parse_quantity(args.search_stop, DIMENSION_FREQUENCY).si_value,
            discard_partial_tail=args.discard_partial_tail, config=config)
    except (ExecutionError, ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}: {report['status']}; global difference={report['global_difference_detected']}")
    print("Global difference is not identification of a magnetic resonance or laboratory qualification.")


if __name__ == "__main__":
    main()
