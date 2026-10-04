"""Parent-linked offline resume preserves checkpoints and pinned source identity."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import h5py
import pytest

from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.spectrum_finalization import SpectrumFinalizationResumeRequest
from app.storage import spectrum_finalization_batch_store as store
from app.storage.finalized_spectrum_store import file_sha256
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_spectrum_finalization_batch import batch, records


def interrupted_batch(tmp_path, monkeypatch, *, partial=False, crash=False):
    request, _expected, paths = batch(tmp_path)
    real = store.finalize_spectrum_archives
    calls = 0

    def stopped(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if partial:
                Path(args[3]).write_bytes(b"partial artifact: keep for inspection")
            raise KeyboardInterrupt("lost process") if crash else ProcessingCancelled("cancelled")
        return real(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "finalize_spectrum_archives", stopped)
        with pytest.raises(KeyboardInterrupt if crash else ProcessingCancelled):
            store.finalize_spectrum_batch(request)
    resume = SpectrumFinalizationResumeRequest(request.journal_path, tmp_path / "resumed.jsonl", True)
    return request, resume, paths


@pytest.mark.parametrize("crash", [False, True])
def test_resume_reuses_verified_completed_output_and_original_source_hashes(tmp_path, monkeypatch, crash):
    original, resume, paths = interrupted_batch(tmp_path, monkeypatch, crash=crash)
    hashes = [file_sha256(path) for path in paths]
    parent_hash = file_sha256(original.journal_path)
    first_hash = file_sha256(original.blocks[0].destination)
    progress = []
    results = store.resume_spectrum_batch(resume, progress_callback=lambda *args: progress.append(args))
    assert len(results) == 2 and results[0]["carried_from_parent"] is True
    assert results[0]["inherited_commit_utc"] == records(original.journal_path)[2]["utc"]
    assert "carried_from_parent" not in results[1]
    assert first_hash == file_sha256(original.blocks[0].destination)
    assert parent_hash == file_sha256(original.journal_path)
    assert hashes == [file_sha256(path) for path in paths]
    events = records(resume.journal_path)
    assert results[0]["utc"] == events[2]["utc"]
    assert results[1]["utc"] == events[4]["utc"]
    plan = events[0]["plan"]
    assert plan["schema"] == store.RESUME_SCHEMA
    assert plan["resume_parent"]["journal_sha256"] == parent_hash
    assert plan["resume_parent"]["completed_blocks"] == 1
    assert plan["source_sha256"] == records(original.journal_path)[0]["plan"]["source_sha256"]
    assert [(done, total) for done, total, _path in progress] == [(1, 2), (2, 2)]
    assert len(store.replay_finalization_batch(resume.journal_path)) == 2
    assert ThatecCompatibilityValidator().validate(original.blocks[1].destination, require_pythat=True).valid
    # Resume replay is self-contained even without parent journal or sources.
    original.journal_path.rename(original.journal_path.with_suffix(".moved"))
    for path in paths:
        path.rename(path.with_suffix(".moved"))
    assert len(store.replay_finalization_batch(resume.journal_path)) == 2


def test_partial_output_requires_explicit_replacement_and_is_never_modified(tmp_path, monkeypatch):
    original, resume, _paths = interrupted_batch(tmp_path, monkeypatch, partial=True)
    partial = original.blocks[1].destination
    partial_hash, parent_hash = file_sha256(partial), file_sha256(original.journal_path)
    with pytest.raises(ExecutionError, match="replacement"):
        store.resume_spectrum_batch(resume)
    assert not resume.journal_path.exists()
    replacement = tmp_path / "replacement.h5"
    result = store.resume_spectrum_batch(replace(resume, replacement_destinations=((1, replacement),)))
    assert result[-1]["destination"] == str(replacement.resolve())
    assert partial_hash == file_sha256(partial) and parent_hash == file_sha256(original.journal_path)
    assert len(store.replay_finalization_batch(resume.journal_path)) == 2


@pytest.mark.parametrize("corruption", ["source", "output", "parent", "completed_replacement", "journal_collision"])
def test_resume_rejects_identity_or_path_change_before_mutation(tmp_path, monkeypatch, corruption):
    original, resume, paths = interrupted_batch(tmp_path, monkeypatch)
    if corruption == "source":
        with h5py.File(paths[0], "r+") as file:
            file["run"].attrs["external_edit"] = "mutated"
    elif corruption == "output":
        with original.blocks[0].destination.open("ab") as stream:
            stream.write(b"mutated")
    elif corruption == "parent":
        events = records(original.journal_path)
        events[0]["plan"]["selections"][0]["signal_profile_id"] = "mutated"
        original.journal_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    elif corruption == "completed_replacement":
        resume = replace(resume, replacement_destinations=((0, tmp_path / "different.h5"),))
    else:
        resume = replace(resume, journal_path=original.journal_path)
    parent_hash = file_sha256(original.journal_path)
    with pytest.raises(ExecutionError):
        store.resume_spectrum_batch(resume)
    assert parent_hash == file_sha256(original.journal_path)
    assert not original.blocks[1].destination.exists()
    if corruption != "journal_collision":
        assert not resume.journal_path.exists()


def test_torn_last_record_recovery_is_explicit_and_preserves_original_bytes(tmp_path, monkeypatch):
    original, resume, _paths = interrupted_batch(tmp_path, monkeypatch, crash=True)
    tail = b'{"type":"terminal","status":'
    with original.journal_path.open("ab") as stream:
        stream.write(tail)
    parent_hash = file_sha256(original.journal_path)
    with pytest.raises(ExecutionError, match="torn"):
        store.resume_spectrum_batch(resume)
    assert not resume.journal_path.exists()
    store.resume_spectrum_batch(replace(resume, recover_torn_tail=True))
    assert parent_hash == file_sha256(original.journal_path)
    import hashlib
    assert records(resume.journal_path)[0]["plan"]["resume_parent"]["discarded_torn_tail_sha256"] == hashlib.sha256(tail).hexdigest()
    assert len(store.replay_finalization_batch(resume.journal_path)) == 2


def test_completed_batch_cannot_resume(tmp_path):
    request, _expected, _paths = batch(tmp_path)
    store.finalize_spectrum_batch(request)
    with pytest.raises(ExecutionError, match="completed batch"):
        store.resume_spectrum_batch(SpectrumFinalizationResumeRequest(request.journal_path, tmp_path / "new.jsonl", True))
    assert not (tmp_path / "new.jsonl").exists()


def test_resume_cancelled_during_verification_creates_no_new_journal(tmp_path, monkeypatch):
    _original, request, _paths = interrupted_batch(tmp_path, monkeypatch)

    def cancelled():
        raise ProcessingCancelled("cancel verification")

    with pytest.raises(ProcessingCancelled):
        store.resume_spectrum_batch(request, cancellation_check=cancelled)
    assert not request.journal_path.exists()


def test_interrupted_resume_can_resume_again_without_rewriting_carried_output(tmp_path, monkeypatch):
    original, first_resume, paths = interrupted_batch(tmp_path, monkeypatch)
    original_hash, output_hash = file_sha256(original.journal_path), file_sha256(original.blocks[0].destination)

    def stop_after_carried(_done, _total, _path):
        raise ProcessingCancelled("cancelled after carried checkpoint")

    with pytest.raises(ProcessingCancelled):
        store.resume_spectrum_batch(first_resume, progress_callback=stop_after_carried)
    assert records(first_resume.journal_path)[-1]["status"] == "aborted"
    assert len(store.replay_finalization_batch(first_resume.journal_path, require_completed=False)) == 1
    first_resume_hash = file_sha256(first_resume.journal_path)
    second_resume = SpectrumFinalizationResumeRequest(first_resume.journal_path, tmp_path / "second-resume.jsonl", True)
    store.resume_spectrum_batch(second_resume)
    assert file_sha256(original.journal_path) == original_hash
    assert file_sha256(first_resume.journal_path) == first_resume_hash
    assert file_sha256(original.blocks[0].destination) == output_hash
    assert len(store.replay_finalization_batch(second_resume.journal_path)) == 2
    assert records(second_resume.journal_path)[0]["plan"]["resume_parent"]["journal_sha256"] == first_resume_hash
    assert records(second_resume.journal_path)[2]["inherited_commit_utc"] == records(original.journal_path)[2]["utc"]
    assert [item["sha256"] for item in store.replay_finalization_batch(second_resume.journal_path)] == [
        file_sha256(block.destination) for block in original.blocks
    ]
    assert all(path.exists() for path in paths)


def test_missing_terminal_after_all_commits_can_be_completed_without_sources(tmp_path):
    request, _expected, paths = batch(tmp_path)
    store.finalize_spectrum_batch(request)
    events = records(request.journal_path)
    request.journal_path.write_text("".join(json.dumps(event) + "\n" for event in events[:-1]), encoding="utf-8")
    output_hashes = [file_sha256(block.destination) for block in request.blocks]
    parent_hash = file_sha256(request.journal_path)
    for path in paths:
        path.rename(path.with_suffix(".moved"))
    resume = SpectrumFinalizationResumeRequest(request.journal_path, tmp_path / "terminal-recovery.jsonl", True)
    completed = store.resume_spectrum_batch(resume)
    assert len(completed) == 2 and all(item["carried_from_parent"] for item in completed)
    assert len(store.replay_finalization_batch(resume.journal_path)) == 2
    assert output_hashes == [file_sha256(block.destination) for block in request.blocks]
    assert parent_hash == file_sha256(request.journal_path)


def test_parent_changed_during_verification_is_rejected_before_resume_creation(tmp_path, monkeypatch):
    original, request, _paths = interrupted_batch(tmp_path, monkeypatch)
    real_read = store._read_batch

    def changed(*args, **kwargs):
        result = real_read(*args, **kwargs)
        with original.journal_path.open("ab") as stream:
            stream.write(b"external process change")
        return result

    monkeypatch.setattr(store, "_read_batch", changed)
    with pytest.raises(ExecutionError, match="journal changed"):
        store.resume_spectrum_batch(request)
    assert not request.journal_path.exists() and not original.blocks[1].destination.exists()


def test_resume_cli_requires_stopped_confirmation_then_preserves_parent(tmp_path, monkeypatch):
    original, resume, _paths = interrupted_batch(tmp_path, monkeypatch)
    command = [sys.executable, "-m", "tools.finalize_spectrum_batch", "--resume-journal", str(original.journal_path),
               "--journal", str(resume.journal_path)]
    parent_hash = file_sha256(original.journal_path)
    rejected = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert rejected.returncode != 0 and "previous offline processing" in rejected.stderr
    assert not resume.journal_path.exists()
    accepted = subprocess.run(command + ["--confirm-previous-processing-stopped"], capture_output=True, text=True, timeout=60)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["verified_blocks"] == 2
    assert parent_hash == file_sha256(original.journal_path)


def test_real_process_exit_preserves_checkpoint_for_parent_linked_resume(tmp_path):
    original, _expected, _paths = batch(tmp_path)
    selection = tmp_path / "selection.json"
    from dataclasses import asdict
    blocks = []
    for block in original.blocks:
        item = asdict(block)
        for key in ("signal_path", "before_path", "after_path", "destination"):
            item[key] = str(item[key].resolve())
        blocks.append(item)
    selection.write_text(json.dumps({"schema": "spectrum-finalization-batch-selection-v1", "blocks": blocks}), encoding="utf-8")
    child = """
import os
import sys
from pathlib import Path
from app.storage import spectrum_finalization_batch_store as store
from tools.finalize_spectrum_batch import load_request
request = load_request(Path(sys.argv[1]), Path(sys.argv[2]))
real = store.finalize_spectrum_archives
count = 0
def interrupted(*args, **kwargs):
    global count
    count += 1
    if count == 2:
        os._exit(73)
    return real(*args, **kwargs)
store.finalize_spectrum_archives = interrupted
store.finalize_spectrum_batch(request)
"""
    ended = subprocess.run([sys.executable, "-c", child, str(selection), str(original.journal_path)],
                           capture_output=True, text=True, timeout=60)
    assert ended.returncode == 73, ended.stderr
    events = records(original.journal_path)
    assert [event["type"] for event in events] == ["plan", "started", "completed", "started"]
    assert len(store.replay_finalization_batch(original.journal_path, require_completed=False)) == 1
    before_hash = file_sha256(original.journal_path)
    output_hash = file_sha256(original.blocks[0].destination)
    resume = SpectrumFinalizationResumeRequest(original.journal_path, tmp_path / "after-process-exit.jsonl", True)
    store.resume_spectrum_batch(resume)
    assert len(store.replay_finalization_batch(resume.journal_path)) == 2
    assert before_hash == file_sha256(original.journal_path)
    assert output_hash == file_sha256(original.blocks[0].destination)


@pytest.mark.parametrize("confirmed", [False, None, 1, "yes"])
def test_resume_boundary_cannot_be_inferred(confirmed):
    with pytest.raises(ValueError, match="previous offline"):
        SpectrumFinalizationResumeRequest(Path("old.jsonl"), Path("new.jsonl"), confirmed)
