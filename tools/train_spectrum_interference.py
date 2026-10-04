"""Train local nuisance templates from a closed REF archive and an explicit JSON specification."""

import argparse
import json
from pathlib import Path

from app.domain.errors import ExecutionError
from app.spectrum.frequency_regions import frequency_region_mask as frequency_region_mask
from app.storage.interference_training_store import train_from_specification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--specification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        calibration = train_from_specification(args.source, args.output,
                                               json.loads(args.specification.read_text(encoding="utf-8")))
    except (ExecutionError, ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}: {calibration.basis_w.shape[1]} local reference components.")
    print("No independence, uncertainty or signal-control qualification inferred from training.")


if __name__ == "__main__":
    main()
