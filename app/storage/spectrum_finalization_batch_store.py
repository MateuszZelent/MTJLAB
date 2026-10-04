"""Sequential, journaled finalization of explicitly selected continuous blocks."""

from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import h5py
import numpy as np

from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.spectrum_finalization import (
    SpectrumFinalizationBatchRequest, SpectrumFinalizationResumeRequest, SpectrumResumeBlockSummary,
    SpectrumResumeInspection, SpectrumResumeInspectionRequest,
)
from .finalized_spectrum_store import file_sha256, finalize_spectrum_archives, replay_finalized_artifact


SCHEMA = "spectrum-finalization-batch-journal-v1"
RESUME_SCHEMA = "spectrum-finalization-batch-journal-v2"
ADOPTION_SCHEMA = "spectrum-finalization-batch-journal-v3"
MAX_RECORD_BYTES = 8 * 1024 * 1024


def _encoded(record):
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ExecutionError("Finalization batch journal record exceeds the 8 MiB limit.")
    return encoded


def _hash(record):
    return hashlib.sha256(_encoded(record)).hexdigest()


def _write(stream, record):
    record = {**record, "utc": datetime.now(timezone.utc).isoformat()}
    stream.write(_encoded(record) + b"\n")
    stream.flush()
    os.fsync(stream.fileno())
    return record


def finalize_spectrum_batch(request, *, cancellation_check=None, progress_callback=None):
    """Keep completed blocks on failure; never overwrite or infer block boundaries.

    All destinations are checked before the exclusive journal is created.
    One block is processed at a time; the returned records retain no spectra.
    The journal is the batch authority, including failed/cancelled operations.
    Each completed HDF5 remains independently replayable and PyThat readable.
    """
    if not isinstance(request, SpectrumFinalizationBatchRequest):
        raise ExecutionError("Batch finalization requires an immutable typed request.")
    if sum(len(block.point_indices or ()) for block in request.blocks) > 1048576:
        raise ExecutionError("Batch checkpoint selections exceed the bounded 1048576-index budget.")
    journal = Path(request.journal_path).resolve()
    selections = []
    for block in request.blocks:
        # Point tuples are immutable: avoid deepcopy of potentially large
        # selections before serializing the bounded plan exactly once.
        selection = {field.name: getattr(block, field.name) for field in fields(block)}
        for key in ("signal_path", "before_path", "after_path", "destination"):
            selection[key] = str(Path(selection[key]).resolve())
        selections.append(selection)
    sources = {Path(item[key]) for item in selections for key in ("signal_path", "before_path", "after_path")}
    targets = [Path(item["destination"]) for item in selections] + [journal]
    if len(set(targets)) != len(targets) or any(path in sources or path.exists() for path in targets):
        raise ExecutionError("Batch outputs and journal must be distinct new files separate from every source.")
    if any(not path.parent.is_dir() for path in targets):
        raise ExecutionError("Create the output and journal directories before batch finalization.")
    try:
        hashes = {}
        for source in sorted(sources):
            if cancellation_check is not None:
                cancellation_check()
            hashes[str(source)] = file_sha256(source, cancellation_check=cancellation_check)
        plan = {"schema": SCHEMA, "selections": selections, "source_sha256": hashes,
                "residual_unit": "W", "confidence_interval_qualified": False}
        return _run_plan(plan, journal, (), cancellation_check=cancellation_check, progress_callback=progress_callback)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Cannot finalize spectrum batch: {exc}") from exc


def _run_plan(plan, journal, inherited, *, cancellation_check=None, progress_callback=None):
    selections, hashes = plan["selections"], plan["source_sha256"]
    completed = []
    try:
        header = {"type": "plan", "plan": plan, "plan_sha256": _hash(plan)}
        # Validate the full envelope before exclusive creation, including its
        # timestamp and hash overhead rather than only the plan payload.
        _encoded({**header, "utc": datetime.now(timezone.utc).isoformat()})
        with journal.open("xb") as stream:
            _write(stream, header)
            index = None
            try:
                for index, previous in enumerate(inherited):
                    if cancellation_check is not None:
                        cancellation_check()
                    if file_sha256(previous["destination"], cancellation_check=cancellation_check) != previous["sha256"]:
                        raise ExecutionError("A completed output changed while preparing resume.")
                    _write(stream, {"type": "started", "block_index": index})
                    record = {**previous, "carried_from_parent": True,
                              "inherited_commit_utc": previous.get("inherited_commit_utc", previous.get("utc"))}
                    record = _write(stream, record)
                    completed.append(record)
                    if progress_callback is not None:
                        progress_callback(len(completed), len(selections), record["destination"])
                for index in range(len(inherited), len(selections)):
                    selection = selections[index]
                    if cancellation_check is not None:
                        cancellation_check()
                    _write(stream, {"type": "started", "block_index": index})
                    block = finalize_spectrum_archives(
                        selection["signal_path"], selection["before_path"], selection["after_path"],
                        selection["destination"], point_indices=selection["point_indices"],
                        before_profile_id=selection["before_profile_id"],
                        after_profile_id=selection["after_profile_id"],
                        signal_profile_id=selection["signal_profile_id"],
                        expected_source_hashes=tuple(hashes[selection[key]] for key in
                            ("signal_path", "before_path", "after_path")),
                        cancellation_check=cancellation_check,
                    )
                    # Once close succeeds, record this durable output even if
                    # cancellation arrives while hashing it. Check next block.
                    record = {"type": "completed", "block_index": index,
                        "destination": selection["destination"],
                        "sha256": file_sha256(selection["destination"]),
                        "count": block.result.count, "source_frame_ids": list(block.source_frame_ids),
                        "segment_id": block.result.segment_id, "quality": block.result.quality.value,
                        "final": block.result.final}
                    record = _write(stream, record)
                    completed.append(record)
                    del block
                    if progress_callback is not None:
                        progress_callback(len(completed), len(selections), record["destination"])
                for record in completed:
                    if file_sha256(record["destination"], cancellation_check=cancellation_check) != record["sha256"]:
                        raise ExecutionError("A completed batch output changed before terminal commit.")
                _write(stream, {"type": "terminal", "status": "completed",
                    "completed_blocks": len(completed), "requested_blocks": len(selections)})
            except Exception as exc:
                try:
                    _write(stream, {"type": "terminal",
                        "status": "aborted" if isinstance(exc, ProcessingCancelled) else "faulted",
                        "completed_blocks": len(completed), "requested_blocks": len(selections),
                        "block_index": index, "error": str(exc)[:4096]})
                except Exception as journal_error:
                    exc.add_note(f"Batch terminal journal write failed: {journal_error}")
                raise
        return tuple(completed)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Cannot finalize spectrum batch: {exc}") from exc


def replay_finalization_batch(journal_path, *, require_completed=True, cancellation_check=None):
    """Verify plan, event order, output hashes and each self-contained result.

    Original sources are not needed. Explicit partial verification returns
    only durably journaled completed outputs, without declaring batch success.
    A torn or malformed journal is rejected rather than silently repaired.
    """
    _plan, completed, _terminal, _tail = _read_batch(journal_path, require_completed=require_completed,
                                             cancellation_check=cancellation_check)
    return completed


def _read_batch(journal_path, *, require_completed, cancellation_check=None, allow_torn_tail=False):
    completed = []
    started = None
    terminal = None
    tail_hash = None
    try:
        if cancellation_check is not None:
            cancellation_check()
        with Path(journal_path).open("rb") as stream:
            line = stream.readline(MAX_RECORD_BYTES + 2)
            if len(line) > MAX_RECORD_BYTES + 1 or not line.endswith(b"\n"):
                raise ExecutionError("Missing, oversized or torn batch plan.")
            header = json.loads(line)
            plan = header["plan"]
            if (header["type"] != "plan" or plan["schema"] not in {SCHEMA, RESUME_SCHEMA, ADOPTION_SCHEMA} or _hash(plan) != header["plan_sha256"]
                    or plan["residual_unit"] != "W" or plan["confidence_interval_qualified"] is not False):
                raise ExecutionError("Batch plan identity is corrupted or unsupported.")
            selections = plan["selections"]
            if not isinstance(selections, list) or not 1 <= len(selections) <= 256:
                raise ExecutionError("Invalid batch selection count.")
            carried = 0
            if plan["schema"] in {RESUME_SCHEMA, ADOPTION_SCHEMA}:
                parent = plan["resume_parent"]
                carried = parent["completed_blocks"]
                if (type(carried) is not int or not 0 <= carried <= len(selections)
                        or type(parent["previous_processing_stopped"]) is not bool or not parent["previous_processing_stopped"]
                        or any(type(parent[key]) is not str or len(parent[key]) != 64 or any(
                            character not in "0123456789abcdef" for character in parent[key]
                        ) for key in ("journal_sha256", "plan_sha256"))):
                    raise ExecutionError("Invalid resume parent identity or stopped-processing boundary.")
                if plan["schema"] == ADOPTION_SCHEMA:
                    adopted = parent["adopted_output"]
                    if (type(parent["journal_completed_blocks"]) is not int
                            or parent["journal_completed_blocks"] != carried - 1
                            or type(adopted["block_index"]) is not int or adopted["block_index"] != carried - 1
                            or type(adopted["sha256"]) is not str or len(adopted["sha256"]) != 64
                            or any(character not in "0123456789abcdef" for character in adopted["sha256"])):
                        raise ExecutionError("Invalid closed-output adoption boundary.")
            while line := stream.readline(MAX_RECORD_BYTES + 2):
                if cancellation_check is not None:
                    cancellation_check()
                if not line.endswith(b"\n") and allow_torn_tail and len(line) <= MAX_RECORD_BYTES and not stream.read(1):
                    tail_hash = hashlib.sha256(line).hexdigest()
                    break
                if len(line) > MAX_RECORD_BYTES + 1 or not line.endswith(b"\n"):
                    raise ExecutionError("Oversized or torn batch progress record.")
                event = json.loads(line)
                if terminal is not None:
                    raise ExecutionError("Batch progress continues after terminal status.")
                if event["type"] == "started":
                    if started is not None or event["block_index"] != len(completed) or len(completed) >= len(selections):
                        raise ExecutionError("Batch block start order is corrupted.")
                    started = event["block_index"]
                elif event["type"] == "completed":
                    index = event["block_index"]
                    if started is None or index != started or index != len(completed):
                        raise ExecutionError("Batch completion has no matching block start.")
                    selection = selections[index]
                    if event.get("carried_from_parent", False) is not (index < carried):
                        raise ExecutionError("Batch carried result differs from its resume boundary.")
                    if plan["schema"] == ADOPTION_SCHEMA and index == carried - 1 and (
                        event.get("recovered_without_journal_commit") is not True
                        or event["sha256"] != plan["resume_parent"]["adopted_output"]["sha256"]
                    ):
                        raise ExecutionError("Adopted output differs from its recorded recovery decision.")
                    if event["destination"] != selection["destination"] or file_sha256(
                        event["destination"], cancellation_check=cancellation_check
                    ) != event["sha256"]:
                        raise ExecutionError("Batch output identity is corrupted.")
                    block = replay_finalized_artifact(event["destination"], cancellation_check=cancellation_check)
                    if (event["count"] != block.result.count or event["source_frame_ids"] != list(block.source_frame_ids)
                            or event["segment_id"] != block.result.segment_id or event["quality"] != block.result.quality.value
                            or event["final"] is not True):
                        raise ExecutionError("Batch result summary differs from the replayed block.")
                    # The artifact must belong to this operator selection and
                    # pinned source identity, not merely be a valid other file.
                    _verify_selection(event["destination"], selection, plan)
                    del block
                    completed.append(event)
                    started = None
                elif event["type"] == "terminal":
                    if event["completed_blocks"] != len(completed) or event["requested_blocks"] != len(selections):
                        raise ExecutionError("Batch terminal count is corrupted.")
                    if event["status"] not in {"completed", "faulted", "aborted"} or (
                        event["status"] == "completed" and (started is not None or len(completed) != len(selections))
                    ):
                        raise ExecutionError("Batch terminal status is inconsistent.")
                    terminal = event
                else:
                    raise ExecutionError("Unknown batch journal record.")
            if require_completed and (terminal is None or terminal["status"] != "completed"):
                raise ExecutionError("Batch is incomplete; explicitly request partial verification to inspect completed blocks.")
        return plan, tuple(completed), terminal, tail_hash
    except (OSError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise ExecutionError(f"Cannot replay spectrum batch: {exc}") from exc


def resume_spectrum_batch(request, *, cancellation_check=None, progress_callback=None):
    """Continue a stopped offline batch into a new, parent-linked journal.

    Existing completed artifacts are verified and carried; neither the parent
    journal nor partial HDF5 files are rewritten. Source hashes stay pinned to
    the original plan. A partial output requires an explicit new destination.
    Torn-tail recovery requires explicit selection and records its byte hash.
    """
    if not isinstance(request, SpectrumFinalizationResumeRequest):
        raise ExecutionError("Resume requires an immutable request and a confirmed stopped-processing boundary.")
    previous = Path(request.previous_journal).resolve()
    journal = Path(request.journal_path).resolve()
    try:
        parent_hash = file_sha256(previous, cancellation_check=cancellation_check)
        if request.expected_parent_hash is not None and parent_hash != request.expected_parent_hash:
            raise ExecutionError("The previous journal changed since inspection. Inspect it again before resume.")
        plan, completed, terminal, tail_hash = _read_batch(previous, require_completed=False,
            cancellation_check=cancellation_check, allow_torn_tail=request.recover_torn_tail)
        if terminal is not None and terminal["status"] == "completed":
            raise ExecutionError("A completed batch cannot be resumed.")
        original_plan_hash = _hash(plan)
        selections = [dict(selection) for selection in plan["selections"]]
        journal_completed = len(completed)
        adopted = None
        if request.adopt_closed_output:
            if journal_completed >= len(selections) or any(index == journal_completed for index, _path in request.replacement_destinations):
                raise ExecutionError("Adoption requires the first unfinished original output without a replacement path.")
            adopted = _verify_closed_output(plan, journal_completed, cancellation_check=cancellation_check)
            if request.expected_closed_output_hash is not None and adopted["sha256"] != request.expected_closed_output_hash:
                raise ExecutionError("The closed output changed since inspection. Inspect it again before adoption.")
            completed = (*completed, adopted)
        for index, destination in request.replacement_destinations:
            if not len(completed) <= index < len(selections):
                raise ExecutionError("Replace only unfinished block destinations; completed selections are immutable.")
            selections[index]["destination"] = str(destination.resolve())
        sources = {Path(selection[key]) for selection in selections for key in
                   ("signal_path", "before_path", "after_path")}
        outputs = [Path(selection["destination"]) for selection in selections]
        if (len(set(outputs + [journal])) != len(outputs) + 1 or journal == previous
                or any(path in sources or path == previous for path in outputs + [journal])
                or journal.exists() or any(path.exists() for path in outputs[len(completed):])):
            raise ExecutionError("Resume requires a new journal and distinct new unfinished outputs. "
                                 "Give an explicit replacement destination for every existing partial output.")
        if any(not path.parent.is_dir() for path in outputs[len(completed):] + [journal]):
            raise ExecutionError("Create resume output directories before processing.")
        remaining_sources = {str(selection[key]) for selection in selections[len(completed):] for key in
                             ("signal_path", "before_path", "after_path")}
        for path in sorted(remaining_sources):
            if file_sha256(path, cancellation_check=cancellation_check) != plan["source_sha256"][path]:
                raise ExecutionError("A source changed since the original batch plan; resume is rejected.")
        if file_sha256(previous, cancellation_check=cancellation_check) != parent_hash:
            raise ExecutionError("The previous journal changed during resume validation.")
        new_plan = {**plan, "schema": ADOPTION_SCHEMA if adopted is not None else RESUME_SCHEMA,
            "selections": selections, "resume_parent": {
            "journal_path": str(previous), "journal_sha256": parent_hash,
            "plan_sha256": original_plan_hash, "completed_blocks": len(completed),
            "previous_processing_stopped": True, "discarded_torn_tail_sha256": tail_hash,
        }}
        if adopted is not None:
            new_plan["resume_parent"].update({"journal_completed_blocks": journal_completed,
                "adopted_output": {"block_index": adopted["block_index"], "sha256": adopted["sha256"]}})
        return _run_plan(new_plan, journal, completed,
                         cancellation_check=cancellation_check, progress_callback=progress_callback)
    except (OSError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise ExecutionError(f"Cannot resume spectrum batch: {exc}") from exc


def inspect_spectrum_resume(request, *, cancellation_check=None):
    """Return bounded scalar choices after independently verifying completed files."""
    if not isinstance(request, SpectrumResumeInspectionRequest):
        raise ExecutionError("Resume inspection requires typed journal selection.")
    path = Path(request.previous_journal).resolve()
    try:
        identity = file_sha256(path, cancellation_check=cancellation_check)
        plan, completed, terminal, tail = _read_batch(path, require_completed=False,
            cancellation_check=cancellation_check, allow_torn_tail=request.recover_torn_tail)
        if file_sha256(path, cancellation_check=cancellation_check) != identity:
            raise ExecutionError("The journal changed during resume inspection.")
        blocks = []
        for index, selection in enumerate(plan["selections"]):
            exists = Path(selection["destination"]).exists()
            candidate, error = None, None
            if index == len(completed) and exists:
                try:
                    candidate = _verify_closed_output(plan, index, cancellation_check=cancellation_check)
                except ProcessingCancelled:
                    raise
                except ExecutionError as exc:
                    error = str(exc)[:1024]
            blocks.append(SpectrumResumeBlockSummary(index, Path(selection["destination"]), index < len(completed),
                exists, Path(selection["signal_path"]), candidate is not None,
                None if candidate is None else candidate["sha256"], error))
        sources = tuple(sorted({Path(selection[key]) for selection in plan["selections"]
                                for key in ("signal_path", "before_path", "after_path")}))
        if file_sha256(path, cancellation_check=cancellation_check) != identity:
            raise ExecutionError("The journal changed during closed-output inspection.")
        return SpectrumResumeInspection(path, identity, len(completed),
            "interrupted" if terminal is None else terminal["status"], tuple(blocks), sources, tail is not None)
    except (OSError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise ExecutionError(f"Cannot inspect spectrum resume: {exc}") from exc


def _verify_selection(path, selection, plan, *, verify_whole_selection=False, cancellation_check=None):
    with h5py.File(path, "r") as file:
        attrs = file["run"].attrs
        manifest = json.loads(attrs["finalization_sources_json"])
        if len(manifest) != 3 or [source["role"] for source in manifest] != ["signal", "before", "after"]:
            raise ExecutionError("Batch output source roles are corrupted.")
        for source, key, profile_key in zip(manifest,
                ("signal_path", "before_path", "after_path"),
                ("signal_profile_id", "before_profile_id", "after_profile_id")):
            if (source["path"] != selection[key] or source["sha256"] != plan["source_sha256"][selection[key]]
                    or selection[profile_key] is not None and source["profile_id"] != selection[profile_key]):
                raise ExecutionError("Batch output source/profile selection differs from its plan.")
        indices = json.loads(attrs["original_signal_point_indices_json"])
        if selection["point_indices"] is not None and indices != selection["point_indices"]:
            raise ExecutionError("Batch output checkpoint selection differs from its plan.")
        if selection["point_indices"] is None and verify_whole_selection:
            # Old plans did not pin the SIGNAL point count. A valid subset
            # artifact must not be adopted as an entire-archive selection.
            source = selection["signal_path"]
            if file_sha256(source, cancellation_check=cancellation_check) != plan["source_sha256"][source]:
                raise ExecutionError("Whole-archive adoption requires the original unchanged SIGNAL source.")
            with h5py.File(source, "r") as raw:
                if len(indices) != len(raw["points"]) or any(index != value for index, value in enumerate(indices)):
                    raise ExecutionError("Closed output does not contain the entire selected SIGNAL archive.")
            if file_sha256(source, cancellation_check=cancellation_check) != plan["source_sha256"][source]:
                raise ExecutionError("The whole-archive SIGNAL source changed during adoption verification.")


def _verify_closed_output(plan, index, *, cancellation_check=None):
    from .thatec_validator import ThatecCompatibilityValidator

    selection = plan["selections"][index]
    path = selection["destination"]
    try:
        identity = file_sha256(path, cancellation_check=cancellation_check)
        block = replay_finalized_artifact(path, cancellation_check=cancellation_check)
        _verify_selection(path, selection, plan, verify_whole_selection=True, cancellation_check=cancellation_check)
        if cancellation_check is not None:
            cancellation_check()
        if not block.result.final or not ThatecCompatibilityValidator().validate(path, require_pythat=True).valid:
            raise ExecutionError("Closed-output adoption requires a final result and valid public thaTEC/PyThat data.")
        _verify_public_result(path, block, cancellation_check=cancellation_check)
        if file_sha256(path, cancellation_check=cancellation_check) != identity:
            raise ExecutionError("The closed output changed during adoption verification.")
        return {"type": "completed", "block_index": index, "destination": str(Path(path).resolve()),
            "sha256": identity, "count": block.result.count, "source_frame_ids": list(block.source_frame_ids),
            "segment_id": block.result.segment_id, "quality": block.result.quality.value,
            "final": True, "recovered_without_journal_commit": True}
    except (OSError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise ExecutionError(f"Cannot adopt closed spectrum output: {exc}") from exc


def _verify_public_result(path, block, *, cancellation_check=None):
    """Compare the public projection against independently replayed SI data."""
    from .thatec_reader import ThatecRunReader

    rows = ThatecRunReader.describe(path).rows.values()
    raw_rows = [row for row in rows if dict(row.definition).get("lab control role") == "spectrum"]
    processed_rows = [row for row in rows if dict(row.definition).get("lab control role") == "spectrum_processed"]
    count = block.result.count
    if len(raw_rows) != 1 or len(processed_rows) != 1 or any(
        row.shape[0] != count + 1 for row in (*raw_rows, *processed_rows)
    ):
        raise ExecutionError("Closed-output public rows must contain every raw sweep and one derived result.")
    with h5py.File(path, "r") as file:
        if len(file["points"]) != count + 1 or len(file["_pending"]) or not file[f"points/{count}"].attrs.get("complete", False):
            raise ExecutionError("Closed-output derived checkpoint is not fully committed.")
        for index in range(count + 1):
            if cancellation_check is not None:
                cancellation_check()
            private = file[f"spectra/{index}"]
            expected_dbm = private["power_dbm"][:] if index < count else 10 * np.log10(block.raw_mean_w) + 30
            if index == count and not np.allclose(private["power_dbm"][:], expected_dbm, rtol=1e-12, atol=0):
                raise ExecutionError("Closed-output derived raw mean differs from independent replay.")
            raw = ThatecRunReader.spectrum_slice(path, raw_rows[0].id, index)
            if (raw.x_unit != "Hz" or raw.y_unit != "dBm" or len(raw.traces) != 1
                    or not np.allclose(raw.x_values, private["frequency_hz"][:], rtol=1e-12, atol=0)
                    or not np.allclose(raw.traces[0].values, expected_dbm, rtol=1e-12, atol=0)):
                raise ExecutionError("Closed-output public raw spectrum differs from its replayed checkpoint.")
            processed = ThatecRunReader.row_slice(path, processed_rows[0].id, index)
            if index < count and not np.all(np.isnan(processed.values)):
                raise ExecutionError("Closed-output public raw rows contain an unexpected processed result.")
        signed = ThatecRunReader.spectrum_slice(path, processed_rows[0].id, count)
        if (signed.x_unit != "Hz" or signed.y_unit != "W" or len(signed.traces) != 1
                or not np.allclose(signed.x_values, file[f"spectra/{count}/frequency_hz"][:], rtol=1e-12, atol=0)
                or not np.allclose(signed.traces[0].values, block.result.values_w, rtol=1e-12, atol=0)):
            raise ExecutionError("Closed-output public signed result differs from independent replay.")
