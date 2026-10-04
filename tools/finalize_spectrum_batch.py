"""Finalize explicit blocks from JSON, or verify their durable batch journal."""

import argparse
import json
from pathlib import Path
import signal
from threading import Event

from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.spectrum_finalization import (
    SpectrumFinalizationBatchRequest, SpectrumFinalizationRequest, SpectrumFinalizationResumeRequest,
)
from app.storage.spectrum_finalization_batch_store import (
    MAX_RECORD_BYTES, finalize_spectrum_batch, replay_finalization_batch, resume_spectrum_batch,
)


def load_request(specification, journal):
    path = Path(specification).resolve()
    with path.open("rb") as stream:
        encoded = stream.read(MAX_RECORD_BYTES + 1)
    if len(encoded) > MAX_RECORD_BYTES:
        raise ExecutionError("Batch specification exceeds the 8 MiB limit.")
    spec = json.loads(encoded)
    if not isinstance(spec, dict) or set(spec) != {"schema", "blocks"} or spec["schema"] != "spectrum-finalization-batch-selection-v1":
        raise ExecutionError("Unsupported batch selection specification.")
    if not isinstance(spec["blocks"], list) or not 1 <= len(spec["blocks"]) <= 256:
        raise ExecutionError("Batch specification requires 1..256 explicit blocks.")
    blocks = []
    for item in spec["blocks"]:
        if not isinstance(item, dict):
            raise ExecutionError("Each selected block must be an object.")
        selection = dict(item)
        for key in ("signal_path", "before_path", "after_path", "destination"):
            if type(selection[key]) is not str or not selection[key].strip():
                raise ExecutionError("Each block requires explicit nonempty source and destination paths.")
            selection[key] = (path.parent / selection[key]).resolve()
        if selection.get("point_indices") is not None:
            if type(selection["point_indices"]) is not list:
                raise ExecutionError("Checkpoint selection must be an ordered integer list.")
            selection["point_indices"] = tuple(selection["point_indices"])
        blocks.append(SpectrumFinalizationRequest(**selection))
    return SpectrumFinalizationBatchRequest(tuple(blocks), Path(journal))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--specification", type=Path)
    mode.add_argument("--replay-journal", type=Path)
    mode.add_argument("--resume-journal", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--confirm-previous-processing-stopped", action="store_true",
        help="Confirm the previous offline processing job ended; this does not confirm hardware state.")
    parser.add_argument("--replacement-output", action="append", nargs=2, default=[], metavar=("BLOCK_INDEX", "NEW_PATH"))
    parser.add_argument("--recover-torn-tail", action="store_true")
    parser.add_argument("--adopt-closed-output", action="store_true",
        help="Verify and retain the first unfinished output if it is a valid closed artifact.")
    args = parser.parse_args()
    if args.specification is not None and (args.journal is None or args.allow_partial):
        parser.error("Finalization requires --journal; --allow-partial applies only to replay.")
    if args.replay_journal is not None and args.journal is not None:
        parser.error("Replay takes only --replay-journal and optional --allow-partial.")
    if args.resume_journal is not None and (args.journal is None or args.allow_partial):
        parser.error("Resume requires a new --journal; --allow-partial applies only to replay.")
    if args.resume_journal is None and (args.confirm_previous_processing_stopped or args.replacement_output
                                      or args.recover_torn_tail or args.adopt_closed_output):
        parser.error("Stopped-processing confirmation and recovery options apply only to --resume-journal.")
    cancelled = Event()
    previous_handler = signal.getsignal(signal.SIGINT)

    def check_cancelled():
        if cancelled.is_set():
            raise ProcessingCancelled("Batch finalization cancelled by operator.")

    try:
        if args.specification is not None:
            request = load_request(args.specification, args.journal)
            signal.signal(signal.SIGINT, lambda *_args: cancelled.set())
            records = finalize_spectrum_batch(request, cancellation_check=check_cancelled)
        elif args.resume_journal is not None:
            request = SpectrumFinalizationResumeRequest(args.resume_journal, args.journal,
                previous_processing_stopped=args.confirm_previous_processing_stopped,
                replacement_destinations=tuple((int(index), Path(path)) for index, path in args.replacement_output),
                recover_torn_tail=args.recover_torn_tail, adopt_closed_output=args.adopt_closed_output)
            signal.signal(signal.SIGINT, lambda *_args: cancelled.set())
            records = resume_spectrum_batch(request, cancellation_check=check_cancelled)
        else:
            records = replay_finalization_batch(args.replay_journal, require_completed=not args.allow_partial)
    except (ExecutionError, OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    print(json.dumps({"verified_blocks": len(records), "outputs": records,
        "residual_unit": "W", "confidence_interval_qualified": False}, allow_nan=False))


if __name__ == "__main__":
    main()
