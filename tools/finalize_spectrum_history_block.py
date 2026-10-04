"""Finalize an explicitly selected SIGNAL block and bracketing REF profiles."""

import argparse
import json
from pathlib import Path

from app.domain.errors import ExecutionError
from app.storage.finalized_spectrum_store import finalize_spectrum_archives


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signal-profile-id")
    parser.add_argument("--before-profile-id")
    parser.add_argument("--after-profile-id")
    parser.add_argument("--point-indices", nargs="+", type=int)
    args = parser.parse_args()
    try:
        block = finalize_spectrum_archives(args.signal, args.before, args.after, args.output,
            point_indices=None if args.point_indices is None else tuple(args.point_indices),
            signal_profile_id=args.signal_profile_id, before_profile_id=args.before_profile_id,
            after_profile_id=args.after_profile_id)
    except ExecutionError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"output": str(args.output), "signal_sweeps": block.result.count,
        "source_frame_ids": block.source_frame_ids, "quality": block.result.quality.value,
        "final": block.result.final, "profile_weights": block.result.profile_weights,
        "residual_unit": "W", "confidence_interval_qualified": False}, allow_nan=False))


if __name__ == "__main__":
    main()
