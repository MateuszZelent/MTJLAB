"""Offline marginal parameter intervals from complete raw spectral block means."""

import argparse
from pathlib import Path

from app.domain.errors import ExecutionError
from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_RATIO, parse_quantity
from app.spectrum.resonance_bootstrap import ResonanceBootstrapConfig
from app.storage.resonance_bootstrap_store import bootstrap_resonance_archives


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("signal", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--block-sweeps", type=int, required=True)
    parser.add_argument("--center", required=True, help="Explicit frequency quantity.")
    parser.add_argument("--fwhm", required=True, help="Explicit frequency quantity.")
    parser.add_argument("--shape", choices=("gaussian", "lorentzian"), default="gaussian")
    parser.add_argument("--resamples", type=int, default=1000)
    parser.add_argument("--interval-method", choices=("percentile", "studentized"), default="percentile")
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--confidence", default="95 %")
    parser.add_argument("--discard-partial-tail", action="store_true")
    parser.add_argument("--independent-blocks-qualified", action="store_true")
    parser.add_argument("--reference-equivalence-qualified", action="store_true")
    parser.add_argument("--stationary-signal-qualified", action="store_true")
    parser.add_argument("--qualification-evidence", default="")
    args = parser.parse_args()
    try:
        config = ResonanceBootstrapConfig(resamples=args.resamples, seed=args.seed, interval_method=args.interval_method,
            confidence_level=parse_quantity(args.confidence, DIMENSION_RATIO).si_value,
            independent_blocks_qualified=args.independent_blocks_qualified,
            reference_equivalence_qualified=args.reference_equivalence_qualified,
            stationary_signal_qualified=args.stationary_signal_qualified, qualification_evidence=args.qualification_evidence)
        report = bootstrap_resonance_archives(args.reference, args.signal, args.output, block_sweeps=args.block_sweeps,
            initial_center_hz=parse_quantity(args.center, DIMENSION_FREQUENCY).si_value,
            initial_fwhm_hz=parse_quantity(args.fwhm, DIMENSION_FREQUENCY).si_value, shape=args.shape,
            discard_partial_tail=args.discard_partial_tail, config=config)
    except (ExecutionError, ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}: {report['status']}; {report['reference_blocks']} REF and {report['signal_blocks']} SIGNAL blocks.")
    print("Marginal intervals are conditional on declared assumptions; coverage remains unqualified.")


if __name__ == "__main__":
    main()
