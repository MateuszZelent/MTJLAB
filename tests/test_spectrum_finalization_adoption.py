"""Explicit recovery at the closed-HDF5 / missing-journal-commit boundary."""

from dataclasses import replace
import json
import subprocess
import sys

import h5py
import pytest

from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.spectrum_finalization import SpectrumFinalizationResumeRequest, SpectrumResumeInspectionRequest
from app.storage import spectrum_finalization_batch_store as store
from app.storage.finalized_spectrum_store import file_sha256, finalize_spectrum_archives
from tests.test_spectrum_finalization_batch import batch, records


def closed_but_unjournaled(tmp_path, monkeypatch, *, lost_index=0, whole_archive=False):
    original, _expected, paths = batch(tmp_path)
    if whole_archive:
        original = replace(original, blocks=(replace(original.blocks[0], point_indices=None), *original.blocks[1:]))
    write = store._write

    def interrupted(stream, record):
        if record["type"] == "completed" and record["block_index"] == lost_index:
            raise KeyboardInterrupt("process lost after HDF5 close before journal commit")
        return write(stream, record)

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "_write", interrupted)
        with pytest.raises(KeyboardInterrupt):
            store.finalize_spectrum_batch(original)
    request = SpectrumFinalizationResumeRequest(original.journal_path, tmp_path / "adopted.jsonl", True,
        adopt_closed_output=True)
    return original, request, paths


@pytest.mark.parametrize("lost_index", [0, 1])
def test_explicit_adoption_preserves_existing_file_and_records_recovery_decision(tmp_path, monkeypatch, lost_index):
    original, request, paths = closed_but_unjournaled(tmp_path, monkeypatch, lost_index=lost_index)
    inspection = store.inspect_spectrum_resume(SpectrumResumeInspectionRequest(original.journal_path))
    candidate = inspection.blocks[lost_index]
    assert candidate.adoptable and candidate.closed_output_hash and not candidate.completed
    target_hash = file_sha256(candidate.destination)
    parent_hash = file_sha256(original.journal_path)
    source_hashes = [file_sha256(path) for path in paths]
    with pytest.raises(ExecutionError, match="replacement"):
        store.resume_spectrum_batch(replace(request, adopt_closed_output=False))
    assert not request.journal_path.exists()
    completed = store.resume_spectrum_batch(replace(request, expected_closed_output_hash=candidate.closed_output_hash))
    assert len(completed) == 2
    assert completed[lost_index]["recovered_without_journal_commit"] is True
    assert completed[lost_index]["inherited_commit_utc"] is None
    plan = records(request.journal_path)[0]["plan"]
    assert plan["schema"] == store.ADOPTION_SCHEMA
    assert plan["resume_parent"]["journal_completed_blocks"] == lost_index
    assert plan["resume_parent"]["adopted_output"] == {"block_index": lost_index, "sha256": target_hash}
    assert len(store.replay_finalization_batch(request.journal_path)) == 2
    assert target_hash == file_sha256(candidate.destination)
    assert parent_hash == file_sha256(original.journal_path)
    assert source_hashes == [file_sha256(path) for path in paths]


@pytest.mark.parametrize("corruption", ["raw", "public", "public_value", "private_mean", "status", "wrong_points",
    "changed_since_inspection", "replacement"])
def test_invalid_or_ambiguous_adoption_is_rejected_before_mutation(tmp_path, monkeypatch, corruption):
    original, request, paths = closed_but_unjournaled(tmp_path, monkeypatch)
    target = original.blocks[0].destination
    if corruption == "changed_since_inspection":
        inspection = store.inspect_spectrum_resume(SpectrumResumeInspectionRequest(original.journal_path))
        request = replace(request, expected_closed_output_hash=inspection.blocks[0].closed_output_hash)
        with target.open("ab") as stream:
            stream.write(b"external change")
    elif corruption == "replacement":
        request = replace(request, replacement_destinations=((0, tmp_path / "new.h5"),))
    elif corruption == "wrong_points":
        alternate = tmp_path / "alternate.h5"
        finalize_spectrum_archives(*paths, alternate, point_indices=(1,),
            signal_profile_id="before", before_profile_id="before", after_profile_id="after")
        # Test fixture substitutes a valid other selection, never production recovery.
        target.write_bytes(alternate.read_bytes())
    else:
        with h5py.File(target, "r+") as file:
            if corruption == "raw":
                file["spectra/0/power_dbm"][0] += 1
            elif corruption == "status":
                file["run"].attrs["status"] = "aborted"
            elif corruption == "private_mean":
                file["spectra/1/power_dbm"][0] += 1
            elif corruption == "public_value":
                definition = file["scan_definition"]
                row = next(key for key in definition if key.startswith("row_") and
                    dict(definition[key].asstr()[()]).get("lab control role") == "spectrum_processed")
                file[f"measurement/{row}/data"][1, 0] += 1e-12
            else:
                del file["measurement"]
    target_hash, parent_hash = file_sha256(target), file_sha256(original.journal_path)
    with pytest.raises(ExecutionError):
        store.resume_spectrum_batch(request)
    assert not request.journal_path.exists()
    assert target_hash == file_sha256(target) and parent_hash == file_sha256(original.journal_path)


def test_whole_archive_adoption_rejects_valid_subset_artifact(tmp_path, monkeypatch):
    original, request, paths = closed_but_unjournaled(tmp_path, monkeypatch, whole_archive=True)
    subset = tmp_path / "subset.h5"
    finalize_spectrum_archives(*paths, subset, point_indices=(0,),
        signal_profile_id="before", before_profile_id="before", after_profile_id="after")
    original.blocks[0].destination.write_bytes(subset.read_bytes())
    with pytest.raises(ExecutionError, match="entire selected SIGNAL"):
        store.resume_spectrum_batch(request)
    assert not request.journal_path.exists()


def test_adoption_cli_requires_explicit_option(tmp_path, monkeypatch):
    original, request, _paths = closed_but_unjournaled(tmp_path, monkeypatch)
    command = [sys.executable, "-m", "tools.finalize_spectrum_batch", "--resume-journal", str(original.journal_path),
        "--journal", str(request.journal_path), "--confirm-previous-processing-stopped"]
    rejected = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert rejected.returncode != 0 and not request.journal_path.exists()
    adopted = subprocess.run(command + ["--adopt-closed-output"], capture_output=True, text=True, timeout=60)
    assert adopted.returncode == 0, adopted.stderr
    assert json.loads(adopted.stdout)["verified_blocks"] == 2


def test_real_process_loss_after_hdf5_close_before_journal_commit_is_recoverable(tmp_path):
    original, _expected, _paths = batch(tmp_path)
    child = """
import os
import sys
from pathlib import Path
from app.domain.spectrum_finalization import SpectrumFinalizationBatchRequest, SpectrumFinalizationRequest
from app.storage import spectrum_finalization_batch_store as store
root = Path(sys.argv[1])
paths = [root / name for name in ('signal.h5', 'before.h5', 'after.h5')]
blocks = tuple(SpectrumFinalizationRequest(*paths, root / f'block-{index}.h5', point_indices=points,
    signal_profile_id='before', before_profile_id='before', after_profile_id='after')
    for index, points in enumerate(((0,), (1, 2))))
write = store._write
def lost(stream, record):
    if record['type'] == 'completed' and record['block_index'] == 0:
        os._exit(81)
    return write(stream, record)
store._write = lost
store.finalize_spectrum_batch(SpectrumFinalizationBatchRequest(blocks, root / 'batch.jsonl'))
"""
    ended = subprocess.run([sys.executable, "-c", child, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert ended.returncode == 81, ended.stderr
    assert [event["type"] for event in records(original.journal_path)] == ["plan", "started"]
    digest = file_sha256(original.blocks[0].destination)
    request = SpectrumFinalizationResumeRequest(original.journal_path, tmp_path / "recovered.jsonl", True,
        adopt_closed_output=True)
    store.resume_spectrum_batch(request)
    assert len(store.replay_finalization_batch(request.journal_path)) == 2
    assert digest == file_sha256(original.blocks[0].destination)


@pytest.mark.parametrize("corruption", ["marker", "boundary", "identity"])
def test_adoption_journal_rejects_corrupt_recovery_provenance(tmp_path, monkeypatch, corruption):
    _original, request, _paths = closed_but_unjournaled(tmp_path, monkeypatch)
    store.resume_spectrum_batch(request)
    events = records(request.journal_path)
    if corruption == "marker":
        events[2]["recovered_without_journal_commit"] = False
    else:
        parent = events[0]["plan"]["resume_parent"]
        if corruption == "boundary":
            parent["journal_completed_blocks"] = 1
        else:
            parent["adopted_output"]["sha256"] = "0" * 64
        events[0]["plan_sha256"] = store._hash(events[0]["plan"])
    request.journal_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    with pytest.raises(ExecutionError):
        store.replay_finalization_batch(request.journal_path)


def test_adopted_output_remains_traceable_after_another_interruption_and_resume(tmp_path, monkeypatch):
    original, request, _paths = closed_but_unjournaled(tmp_path, monkeypatch)
    digest = file_sha256(original.blocks[0].destination)

    def cancel_after_adoption(_done, _total, _path):
        raise ProcessingCancelled("cancel after adoption commit")

    with pytest.raises(ProcessingCancelled):
        store.resume_spectrum_batch(request, progress_callback=cancel_after_adoption)
    assert records(request.journal_path)[-1]["status"] == "aborted"
    assert len(store.replay_finalization_batch(request.journal_path, require_completed=False)) == 1
    next_request = replace(request, previous_journal=request.journal_path,
        journal_path=request.journal_path.with_name("next-resume.jsonl"), adopt_closed_output=False)
    store.resume_spectrum_batch(next_request)
    completed = store.replay_finalization_batch(next_request.journal_path)
    assert len(completed) == 2 and completed[0]["recovered_without_journal_commit"]
    assert completed[0]["inherited_commit_utc"] is None
    assert digest == file_sha256(original.blocks[0].destination)
