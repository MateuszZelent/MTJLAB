"""Batch provenance, partial failure, bounded replay and exclusive outputs."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.spectrum_finalization import SpectrumFinalizationBatchRequest, SpectrumFinalizationRequest
from app.storage import spectrum_finalization_batch_store as store
from app.storage.finalized_spectrum_store import file_sha256, replay_finalized_artifact
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_spectrum_finalized_store import archives


def batch(tmp_path):
    _context, expected, paths = archives(tmp_path)
    selections = tuple(SpectrumFinalizationRequest(*paths, tmp_path / f"block-{index}.h5",
        point_indices=points, signal_profile_id="before", before_profile_id="before", after_profile_id="after")
        for index, points in enumerate(((0,), (1, 2))))
    return SpectrumFinalizationBatchRequest(selections, tmp_path / "batch.jsonl"), expected, paths


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_batch_preserves_sources_public_compatibility_and_replays_after_sources_move(tmp_path):
    request, expected, paths = batch(tmp_path)
    hashes = [file_sha256(path) for path in paths]
    progress = []
    completed = store.finalize_spectrum_batch(request, progress_callback=lambda *args: progress.append(args))
    assert [item["count"] for item in completed] == [1, 2]
    assert [(done, total) for done, total, _path in progress] == [(1, 2), (2, 2)]
    assert hashes == [file_sha256(path) for path in paths]
    assert records(request.journal_path)[-1]["status"] == "completed"
    for selection in request.blocks:
        result = replay_finalized_artifact(selection.destination)
        np.testing.assert_allclose(result.result.values_w, expected, rtol=1e-12)
        assert result.result.standard_uncertainty_w is None
        assert ThatecCompatibilityValidator().validate(selection.destination, require_pythat=True).valid
    for path in paths:
        path.rename(path.with_suffix(".moved"))
    assert [item["count"] for item in store.replay_finalization_batch(request.journal_path)] == [1, 2]


@pytest.mark.parametrize("collision", ["duplicate", "source", "journal", "existing"])
def test_all_destinations_checked_before_any_mutation(tmp_path, collision):
    request, _expected, paths = batch(tmp_path)
    original = request.blocks
    if collision == "duplicate":
        request = replace(request, blocks=(original[0], replace(original[1], destination=original[0].destination)))
    elif collision == "source":
        request = replace(request, blocks=(original[0], replace(original[1], destination=paths[0])))
    elif collision == "journal":
        request = replace(request, journal_path=original[1].destination)
    else:
        original[1].destination.write_text("preserve me", encoding="utf-8")
    hashes = [file_sha256(path) for path in paths]
    with pytest.raises(ExecutionError, match="distinct new"):
        store.finalize_spectrum_batch(request)
    assert not original[0].destination.exists() and not request.journal_path.exists()
    assert hashes == [file_sha256(path) for path in paths]
    if collision == "existing":
        assert original[1].destination.read_text(encoding="utf-8") == "preserve me"


@pytest.mark.parametrize("interruption", ["cancel", "error", "crash"])
def test_partial_batch_retains_committed_output_and_truthful_terminal_state(tmp_path, monkeypatch, interruption):
    request, _expected, paths = batch(tmp_path)
    hashes = [file_sha256(path) for path in paths]
    real = store.finalize_spectrum_archives
    count = 0

    def finalize(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise {"cancel": ProcessingCancelled, "error": ExecutionError, "crash": KeyboardInterrupt}[interruption]("injected")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "finalize_spectrum_archives", finalize)
    with pytest.raises({"cancel": ProcessingCancelled, "error": ExecutionError, "crash": KeyboardInterrupt}[interruption]):
        store.finalize_spectrum_batch(request)
    assert hashes == [file_sha256(path) for path in paths]
    assert request.blocks[0].destination.exists() and not request.blocks[1].destination.exists()
    events = records(request.journal_path)
    if interruption == "crash":
        assert events[-1]["type"] == "started"
    else:
        assert events[-1]["status"] == ("aborted" if interruption == "cancel" else "faulted")
        assert events[-1]["completed_blocks"] == 1
    with pytest.raises(ExecutionError, match="incomplete"):
        store.replay_finalization_batch(request.journal_path)
    assert len(store.replay_finalization_batch(request.journal_path, require_completed=False)) == 1
    first_hash = file_sha256(request.blocks[0].destination)
    with pytest.raises(ExecutionError, match="distinct new"):
        store.finalize_spectrum_batch(request)
    assert file_sha256(request.blocks[0].destination) == first_hash


def test_mutated_source_between_blocks_is_rejected_before_next_output(tmp_path):
    request, _expected, paths = batch(tmp_path)

    def mutate(done, _total, _path):
        if done == 1:
            import h5py
            with h5py.File(paths[0], "r+") as file:
                file["run"].attrs["external_edit"] = "changed"

    with pytest.raises(ExecutionError, match="changed since"):
        store.finalize_spectrum_batch(request, progress_callback=mutate)
    assert request.blocks[0].destination.exists() and not request.blocks[1].destination.exists()
    assert records(request.journal_path)[-1]["status"] == "faulted"
    assert len(store.replay_finalization_batch(request.journal_path, require_completed=False)) == 1


@pytest.mark.parametrize("corruption", ["plan", "order", "summary", "output", "torn", "after_terminal",
    "rehashed_profile", "rehashed_points"])
def test_replay_rejects_corruption(tmp_path, corruption):
    request, _expected, _paths = batch(tmp_path)
    store.finalize_spectrum_batch(request)
    events = records(request.journal_path)
    if corruption == "plan":
        events[0]["plan"]["selections"][0]["signal_profile_id"] = "other"
    elif corruption.startswith("rehashed_"):
        selection = events[0]["plan"]["selections"][0]
        selection["signal_profile_id" if corruption == "rehashed_profile" else "point_indices"] = (
            "other" if corruption == "rehashed_profile" else [1]
        )
        events[0]["plan_sha256"] = store._hash(events[0]["plan"])
    elif corruption == "order":
        events[1]["block_index"] = 1
    elif corruption == "summary":
        events[2]["count"] += 1
    elif corruption == "output":
        with request.blocks[0].destination.open("ab") as stream:
            stream.write(b"external change")
    elif corruption == "after_terminal":
        events.append(events[1])
    request.journal_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    if corruption == "torn":
        with request.journal_path.open("ab") as stream:
            stream.write(b'{"type":')
    with pytest.raises(ExecutionError):
        store.replay_finalization_batch(request.journal_path)


def test_cli_batch_and_replay_with_paths_relative_to_specification(tmp_path):
    request, _expected, _paths = batch(tmp_path)
    specification = tmp_path / "selection.json"
    blocks = []
    for block in request.blocks:
        blocks.append({"signal_path": block.signal_path.name, "before_path": block.before_path.name,
            "after_path": block.after_path.name, "destination": block.destination.name,
            "point_indices": list(block.point_indices), "signal_profile_id": "before",
            "before_profile_id": "before", "after_profile_id": "after"})
    specification.write_text(json.dumps({"schema": "spectrum-finalization-batch-selection-v1", "blocks": blocks}), encoding="utf-8")
    command = [sys.executable, "-m", "tools.finalize_spectrum_batch"]
    finished = subprocess.run(command + ["--specification", str(specification), "--journal", str(request.journal_path)],
        capture_output=True, text=True, timeout=60)
    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout)["verified_blocks"] == 2
    replay = subprocess.run(command + ["--replay-journal", str(request.journal_path)],
        capture_output=True, text=True, timeout=60)
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["verified_blocks"] == 2
    sha = file_sha256(request.journal_path)
    again = subprocess.run(command + ["--specification", str(specification), "--journal", str(request.journal_path)],
        capture_output=True, text=True, timeout=60)
    assert again.returncode != 0 and file_sha256(request.journal_path) == sha


@pytest.mark.parametrize("blocks", [[], (), (None,), [SpectrumFinalizationRequest(*(Path("test") for _ in range(4)))],
    (SpectrumFinalizationRequest(*(Path("test") for _ in range(4))),) * 257])
def test_request_rejects_mutable_or_unbounded_batch(blocks):
    with pytest.raises(ValueError):
        SpectrumFinalizationBatchRequest(blocks, Path("journal.jsonl"))
