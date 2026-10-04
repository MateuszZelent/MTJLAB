"""Validate a trained model on disjoint raw REF; no automatic model approval."""

import argparse
from pathlib import Path

from app.domain.errors import ExecutionError
from app.storage.interference_validation_store import validate_interference_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--model-id")
    parser.add_argument("--region", nargs=2, action="append", metavar=("START", "STOP"),
                        help="Explicit held-out frequency region, with units; repeat for multiple regions. "
                             "Default: the model's recorded protected bins.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = validate_interference_archive(args.model, args.reference, args.output,
                                             model_id=args.model_id, validation_regions=args.region)
    except (ExecutionError, ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}: {report['accepted_fits']} accepted, {report['rejected_fits']} rejected fits.")
    print("Diagnostics only; temporal disjointness does not establish independence or SIGNAL preservation.")


if __name__ == "__main__":
    main()
