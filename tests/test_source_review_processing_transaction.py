"""A failed publication must not become an immutable successful record."""
from dataclasses import replace

import h5py
import pytest

from app.domain.errors import ExecutionError
from tests.test_spectrum_correction_store import fixture_profile, run_writer


@pytest.mark.parametrize("stage", ["write", "move", "flush"])
@pytest.mark.parametrize("rollback_failure", [False, True])
def test_processing_publication_rollback(tmp_path, monkeypatch, stage, rollback_failure):
    writer = run_writer(tmp_path / "processing.h5")
    context, baseline = fixture_profile()
    writer.store_background_profile(context, baseline)
    profile = replace(baseline, profile_id="new-profile")
    destination = "spectrum_processing_v1/profiles/new-profile"
    pending = "_pending/profile_new-profile"
    original_move = h5py.Group.move
    original_flush = h5py.File.flush
    original_delete = h5py.Group.__delitem__
    fired = False

    def move(group, source, target):
        original_move(group, source, target)
        if stage == "move" and target == destination:
            raise OSError("primary publication failure")

    def flush(file):
        nonlocal fired
        if stage == "flush" and destination in file and not fired:
            fired = True
            raise OSError("primary publication failure")
        return original_flush(file)

    def create(group, name, *args, **kwargs):
        if stage == "write" and group.name == "/" + pending:
            raise OSError("primary publication failure")
        return original_create(group, name, *args, **kwargs)

    def delete(group, name):
        if rollback_failure and name in (pending, destination):
            raise OSError("rollback deletion failure")
        return original_delete(group, name)

    original_create = h5py.Group.create_dataset
    with monkeypatch.context() as patch:
        patch.setattr(h5py.Group, "move", move)
        patch.setattr(h5py.File, "flush", flush)
        patch.setattr(h5py.Group, "create_dataset", create)
        patch.setattr(h5py.Group, "__delitem__", delete)
        with pytest.raises(ExecutionError, match="primary publication failure") as error:
            writer.store_background_profile(context, profile)
    assert isinstance(error.value.__cause__, OSError)
    assert writer._file["spectrum_processing_v1/profiles/fixture-profile"].attrs["complete"]
    if rollback_failure:
        assert "rollback deletion failure" in str(error.value)
        with pytest.raises(ExecutionError, match="close and recover"):
            writer.store_background_profile(context, profile)
        with pytest.raises(ExecutionError, match="rollback failed"):
            writer.close("completed")
    else:
        assert destination not in writer._file and pending not in writer._file
        assert writer.store_background_profile(context, profile) == "new-profile"
        writer.close("aborted")
