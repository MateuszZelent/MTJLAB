"""Final artifact provenance, self-contained replay and public compatibility."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import subprocess
import sys

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import CorrectionConfig
from app.storage.background_profile_store import BackgroundProfileHdf5Store
from app.storage.finalized_spectrum_store import (
    file_sha256, finalize_spectrum_archives, inspect_finalization_sources, replay_finalized_artifact,
)
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.thatec_validator import ThatecCompatibilityValidator
from app.storage.spectrum_correction_codec import read_profile, write_profile
from tests.test_spectrum_correction_finalize import fixture


def archives(tmp_path):
    context, before, after, signal, frames = fixture()
    paths = [tmp_path / name for name in ("signal.h5", "before.h5", "after.h5")]
    BackgroundProfileHdf5Store.save(paths[1], context, before)
    BackgroundProfileHdf5Store.save(paths[2], context, after)
    writer = Hdf5RunWriter(paths[0], recipe_source="schema_version: 1\nname: fixture\nsteps: []\n",
                           settings_source="fixture: true\n", plan_hash="fixture", device_idn={},
                           simulation_metadata={"enabled": True}, run_attributes={
                               "spectrum_correction_config_json": json.dumps(asdict(CorrectionConfig())),
                           })
    writer.store_background_profile(context, before)
    for index, (envelope, dbm) in enumerate(frames):
        trace = SpectrumTrace(tuple(context.frequencies_hz), tuple(dbm),
                              datetime.fromtimestamp(envelope.acquired_at_s, timezone.utc), "TRAC1")
        writer.append(MeasurementPoint(index, {}, {}), trace, acquisition_envelope=envelope)
    writer.close("aborted")
    return context, signal, paths


def test_finalized_artifact_preserves_sources_and_replays_without_originals(tmp_path):
    _context, expected, paths = archives(tmp_path)
    hashes = [file_sha256(path) for path in paths]
    output = tmp_path / "final.h5"
    block = finalize_spectrum_archives(*paths, output)
    assert block.result.final and block.result.standard_uncertainty_w is None
    np.testing.assert_allclose(block.result.values_w, expected, rtol=1e-12)
    assert hashes == [file_sha256(path) for path in paths]
    stored = Hdf5RunReader.finalized_spectrum_blocks(output)
    assert len(stored) == 1 and stored[0][1] == (0, 1, 2)
    assert stored[0][0].source_frame_ids == (0, 1, 2)
    assert Hdf5RunReader.spectrum(output, 3).processing_operation == "bracketed_reference_block"
    assert ThatecCompatibilityValidator().validate(output, require_pythat=True).valid
    with h5py.File(output, "r") as file:
        sources = json.loads(file["run"].attrs["finalization_sources_json"])
        assert [item["sha256"] for item in sources] == hashes
        assert json.loads(file["run/simulation_json"].asstr()[()])["enabled"]
        assert file["run"].attrs["status"] == "completed"
        assert len(file["_pending"]) == 0
    for path in paths:
        path.rename(path.with_suffix(".moved"))
    replayed = replay_finalized_artifact(output)
    np.testing.assert_array_equal(replayed.result.values_w, block.result.values_w)


def test_explicit_profiles_finalize_multi_profile_sources_without_guessing(tmp_path):
    _context, expected, paths = archives(tmp_path)
    for path in paths:
        with h5py.File(path, "r+") as file:
            root = file["spectrum_processing_v1/profiles"]
            context, profile = read_profile(next(iter(root.values())))
            decoy = replace(profile, profile_id="000_decoy", mean_w=profile.mean_w * 2)
            group = root.create_group(decoy.profile_id)
            write_profile(group, context, decoy)
            group.attrs["complete"] = True
    # A single REF history file can contain both bracketing profiles.
    with h5py.File(paths[2], "r") as after_file, h5py.File(paths[1], "r+") as before_file:
        after_context, after = read_profile(after_file["spectrum_processing_v1/profiles/after"])
        group = before_file["spectrum_processing_v1/profiles"].create_group("after")
        write_profile(group, after_context, after)
        group.attrs["complete"] = True
    sources = (paths[0], paths[1], paths[1])
    hashes = [file_sha256(path) for path in sources]
    output = tmp_path / "selected.h5"
    with pytest.raises(ExecutionError, match="explicit profile ID"):
        finalize_spectrum_archives(*sources, output)
    assert not output.exists()
    block = finalize_spectrum_archives(*sources, output, point_indices=(0, 1, 2),
        before_profile_id="before", after_profile_id="after", signal_profile_id="before")
    np.testing.assert_allclose(block.result.values_w, expected, rtol=1e-12)
    assert hashes == [file_sha256(path) for path in sources]
    with h5py.File(output, "r") as file:
        manifest = json.loads(file["run"].attrs["finalization_sources_json"])
        assert [item["profile_id"] for item in manifest] == ["before", "before", "after"]
        assert all(len(item["profile_content_hash"]) == 64 for item in manifest)
        assert [item["sha256"] for item in manifest] == hashes
    assert ThatecCompatibilityValidator().validate(output, require_pythat=True).valid
    for path in paths:
        path.rename(path.with_suffix(".moved"))
    replayed = replay_finalized_artifact(output)
    np.testing.assert_array_equal(replayed.result.values_w, block.result.values_w)


@pytest.mark.parametrize("selection", ["missing", "../before", "", 42])
def test_invalid_profile_selection_fails_before_output_creation(tmp_path, selection):
    _context, _expected, paths = archives(tmp_path)
    output = tmp_path / "invalid-selection.h5"
    hashes = [file_sha256(path) for path in paths]
    with pytest.raises(ExecutionError):
        finalize_spectrum_archives(*paths, output, before_profile_id=selection)
    assert not output.exists()
    assert hashes == [file_sha256(path) for path in paths]


def test_cli_finalizes_selected_block_and_refuses_output_reuse(tmp_path):
    _context, expected, paths = archives(tmp_path)
    output = tmp_path / "cli-selected.h5"
    command = [sys.executable, "-m", "tools.finalize_spectrum_history_block",
        "--signal", str(paths[0]), "--before", str(paths[1]), "--after", str(paths[2]),
        "--output", str(output), "--before-profile-id", "before", "--after-profile-id", "after",
        "--signal-profile-id", "before", "--point-indices", "1", "2"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    message = json.loads(completed.stdout)
    assert message["signal_sweeps"] == 2 and message["source_frame_ids"] == [1, 2]
    assert message["residual_unit"] == "W" and not message["confidence_interval_qualified"]
    block = replay_finalized_artifact(output)
    np.testing.assert_allclose(block.result.values_w, expected, rtol=1e-12)
    original = file_sha256(output)
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode != 0 and "new file" in completed.stderr
    assert file_sha256(output) == original


@pytest.mark.parametrize("corruption", ["uncommitted", "oversized", "shape", "group"])
def test_profile_inspection_rejects_corrupt_or_unbounded_vectors(tmp_path, corruption):
    _context, _expected, paths = archives(tmp_path)
    with h5py.File(paths[1], "r+") as file:
        group = file["spectrum_processing_v1/profiles/before"]
        if corruption == "uncommitted":
            group.attrs["complete"] = False
        elif corruption == "oversized":
            del group["frequency_hz"]
            group.create_dataset("frequency_hz", shape=(1048577,), dtype="f8")
        elif corruption == "shape":
            del group["mean_w"]
            group.create_dataset("mean_w", shape=(1048577,), dtype="f8")
        else:
            del group["frequency_hz"]
            group.create_group("frequency_hz")
    with pytest.raises(ExecutionError):
        inspect_finalization_sources(paths)


@pytest.mark.parametrize("corruption", ["result", "raw", "source_ids", "incomplete", "profile", "source_point",
                                         "provenance", "policy"])
def test_corrupted_finalized_artifact_is_rejected(tmp_path, corruption):
    _context, _expected, paths = archives(tmp_path)
    output = tmp_path / "corrupt.h5"
    finalize_spectrum_archives(*paths, output)
    with h5py.File(output, "r+") as file:
        group = next(iter(file["spectrum_processing_v1/finalized_blocks"].values()))
        if corruption == "result":
            group["result/values_w"][1] *= 2
        elif corruption == "raw":
            file["spectra/1/power_dbm"][1] += 1
        elif corruption == "source_ids":
            group["source_frame_ids"][0] = 99
        elif corruption == "incomplete":
            group.attrs["complete"] = False
        elif corruption == "source_point":
            file["points/1"].attrs["complete"] = False
        elif corruption == "provenance":
            sources = json.loads(file["run"].attrs["finalization_sources_json"])
            sources[0]["sha256"] = "0" * 64
            file["run"].attrs["finalization_sources_json"] = json.dumps(sources, sort_keys=True)
        elif corruption == "policy":
            policy = json.loads(file["run"].attrs["finalization_policy_json"])
            policy["maximum_gap_s"] = 1000
            file["run"].attrs["finalization_policy_json"] = json.dumps(policy, sort_keys=True)
        else:
            file["spectrum_processing_v1/profiles/before/mean_w"][0] *= 2
    with pytest.raises(ExecutionError):
        replay_finalized_artifact(output)


def test_sources_or_existing_destination_cannot_be_overwritten(tmp_path):
    _context, _signal, paths = archives(tmp_path)
    before_hash = file_sha256(paths[0])
    with pytest.raises(ExecutionError, match="new file"):
        finalize_spectrum_archives(*paths, paths[0])
    assert file_sha256(paths[0]) == before_hash
    destination = tmp_path / "existing.txt"
    destination.write_text("keep this", encoding="utf-8")
    with pytest.raises(ExecutionError, match="new file"):
        finalize_spectrum_archives(*paths, destination)
    assert destination.read_text(encoding="utf-8") == "keep this"


def test_invalid_selection_fails_before_output_creation(tmp_path):
    _context, _signal, paths = archives(tmp_path)
    output = tmp_path / "never-created.h5"
    with pytest.raises(ExecutionError, match="strictly ordered"):
        finalize_spectrum_archives(*paths, output, point_indices=(1, 0))
    assert not output.exists()


def test_missing_source_reports_a_domain_error_without_creating_output(tmp_path):
    output = tmp_path / "missing-final.h5"
    with pytest.raises(ExecutionError, match="Cannot finalize"):
        finalize_spectrum_archives(tmp_path / "missing.h5", tmp_path / "before.h5",
                                   tmp_path / "after.h5", output)
    assert not output.exists()


@pytest.mark.parametrize("phase", ["before", "raw_commit", "final_hash"])
def test_cancellation_preserves_sources_and_never_completes_partial_output(tmp_path, monkeypatch, phase):
    _context, _expected, paths = archives(tmp_path)
    original_hashes = [file_sha256(path) for path in paths]
    output = tmp_path / "cancelled.h5"
    cancel = [phase == "before"]
    original_append = Hdf5RunWriter.append

    def append_then_cancel(writer, point, *args, **kwargs):
        result = original_append(writer, point, *args, **kwargs)
        if (phase == "raw_commit" and point.index == 0) or (phase == "final_hash" and point.index == 3):
            cancel[0] = True
        return result

    def check():
        if cancel[0]:
            raise ProcessingCancelled("operator canceled")

    monkeypatch.setattr(Hdf5RunWriter, "append", append_then_cancel)
    with pytest.raises(ProcessingCancelled, match="operator canceled"):
        finalize_spectrum_archives(*paths, output, cancellation_check=check)
    assert original_hashes == [file_sha256(path) for path in paths]
    if phase == "before":
        assert not output.exists()
    else:
        with h5py.File(output, "r") as file:
            assert file["run"].attrs["status"] == "aborted"
            assert len(file["points"]) == (1 if phase == "raw_commit" else 4)
            assert len(file["_pending"]) == 0
        assert ThatecCompatibilityValidator().validate(output, require_pythat=True).valid
        with pytest.raises(ExecutionError, match="completed finalization"):
            replay_finalized_artifact(output)


def test_cancellation_close_failure_is_reported_as_failure(tmp_path, monkeypatch):
    _, _, paths = archives(tmp_path)
    cancel = [False]
    original_append, original_close = Hdf5RunWriter.append, Hdf5RunWriter.close

    def append(writer, point, *args, **kwargs):
        result = original_append(writer, point, *args, **kwargs)
        cancel[0] = True
        return result

    def close(writer, status):
        original_close(writer, status)
        if status == "aborted":
            raise OSError("close validation failed")

    def check():
        if cancel[0]:
            raise ProcessingCancelled("cancel requested")

    monkeypatch.setattr(Hdf5RunWriter, "append", append)
    monkeypatch.setattr(Hdf5RunWriter, "close", close)
    with pytest.raises(ExecutionError, match="output close failed") as error:
        finalize_spectrum_archives(*paths, tmp_path / "close-failed.h5", cancellation_check=check)
    assert not isinstance(error.value, ProcessingCancelled)
    assert isinstance(error.value.__cause__, ProcessingCancelled)
