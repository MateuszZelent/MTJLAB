"""Partial references stay private and cannot qualify recovery or correction."""
import h5py
import pytest

from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.engine.recovery import RunRecoveryManager
from app.storage.hdf5_reader import Hdf5RunReader
from tests.test_source_review_partial_append import trace, writer_for


@pytest.mark.parametrize("stage", ["dataset", "publish", "flush"])
def test_reference_failure_rolls_back_without_touching_previous_reference(tmp_path, monkeypatch, stage):
    writer = writer_for(tmp_path / "reference.h5")
    writer.store_reference(trace())
    original_dataset, original_move, original_flush = h5py.Group.create_dataset, h5py.Group.move, h5py.File.flush
    fired = False

    def dataset(group, name, *args, **kwargs):
        result = original_dataset(group, name, *args, **kwargs)
        if stage == "dataset" and group.name == "/_pending/reference_1" and name == "power_dbm":
            raise OSError("reference fault")
        return result

    def move(group, source, dest):
        original_move(group, source, dest)
        if stage == "publish" and dest == "references/1":
            raise OSError("reference fault")

    def flush(file):
        nonlocal fired
        if stage == "flush" and "references/1" in file and not fired:
            fired = True
            raise OSError("reference fault")
        return original_flush(file)

    with monkeypatch.context() as patch:
        patch.setattr(h5py.Group, "create_dataset", dataset)
        patch.setattr(h5py.Group, "move", move)
        patch.setattr(h5py.File, "flush", flush)
        with pytest.raises(ExecutionError, match="reference fault"):
            writer.store_reference(trace())
    assert list(writer._references) == ["0"]
    assert not list(writer._pending)
    assert writer._file["reference"].id == writer._references["0"].id
    assert writer.store_reference(trace()) == 1
    writer.close("aborted")
    assert len(Hdf5RunReader.references(writer.path)) == 2


def test_uncommitted_reference_rejected_by_readers_recovery_and_checkpoint(tmp_path):
    writer = writer_for(tmp_path / "incomplete.h5")
    writer.store_reference(trace())
    writer._references["0"].attrs["complete"] = False
    writer._file.flush()
    with pytest.raises(ExecutionError, match="not committed"):
        RunRecoveryManager._read_reference(writer._file, {"index": 0, "fingerprint": "identity"})
    with pytest.raises(ExecutionError, match="not committed"):
        writer.append(MeasurementPoint(0, {}, {}, metadata={"reference_index": 0}), trace())
    writer._file.close()
    for read in (Hdf5RunReader.references, Hdf5RunReader.reference):
        with pytest.raises(ExecutionError, match="not committed"):
            read(writer.path)


def test_pending_reference_is_not_visible_and_legacy_complete_reference_stays_readable(tmp_path):
    writer = writer_for(tmp_path / "pending.h5")
    writer.store_reference(trace())
    group = writer._references["0"]
    del group.attrs["reference_transaction_version"]
    del group.attrs["complete"]
    pending = writer._pending.create_group("reference_1")
    pending.attrs["complete"] = False
    pending.create_dataset("frequency_hz", data=[1., 2.])
    writer._file.flush()
    writer._file.close()
    assert len(Hdf5RunReader.references(writer.path)) == 1
    assert Hdf5RunReader.reference(writer.path, 1) is None
