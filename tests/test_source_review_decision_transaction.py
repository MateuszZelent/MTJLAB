"""Faults cannot publish a decision or advance its hash chain partially."""
import h5py
import pytest

from app.domain.errors import ExecutionError
from app.domain.spectrum_correction import CorrectionConfig
from app.storage.spectrum_decision_store import ROOT, iter_decisions
from tests.test_spectrum_correction_store import fixture_profile, run_writer


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("stage", ["write", "move", "count", "hash", "flush"])
@pytest.mark.parametrize("rollback_failure", [False, True])
def test_decision_commit_restores_previous_history(tmp_path, monkeypatch, initial, stage, rollback_failure):
    writer = run_writer(tmp_path / "decision.h5")
    context, _ = fixture_profile()
    config = CorrectionConfig()
    if not initial:
        writer.initialize_spectrum_decisions(context, config)
        previous = writer.record_spectrum_decision("initialize", {})
    destination = ROOT if initial else ROOT + "/records/00000001"
    pending = "_pending/decision_initial" if initial else "_pending/decision_1"
    move_original, flush_original = h5py.Group.move, h5py.File.flush
    set_original, delete_original = h5py.AttributeManager.__setitem__, h5py.Group.__delitem__
    fired = False

    def move(group, source, target):
        move_original(group, source, target)
        if stage == "move" and target == destination:
            raise OSError("primary decision failure")

    def set_attribute(attrs, name, value):
        nonlocal fired
        if stage == "write" and name == ("metadata_json" if initial else "record_json"):
            raise OSError("primary decision failure")
        result = set_original(attrs, name, value)
        if not fired and ((stage == "count" and name == "count") or (stage == "hash" and name == "last_sha256")):
            fired = True
            raise OSError("primary decision failure")
        return result

    def flush(file):
        nonlocal fired
        if stage == "flush" and destination in file and not fired:
            fired = True
            raise OSError("primary decision failure")
        return flush_original(file)

    def delete(group, name):
        if rollback_failure and name in (destination, pending):
            raise OSError("rollback deletion failure")
        return delete_original(group, name)

    def commit():
        if initial:
            writer.initialize_spectrum_decisions(context, config)
        else:
            writer.record_spectrum_decision("reset_segment", {})

    with monkeypatch.context() as patch:
        patch.setattr(h5py.Group, "move", move)
        patch.setattr(h5py.File, "flush", flush)
        patch.setattr(h5py.AttributeManager, "__setitem__", set_attribute)
        patch.setattr(h5py.Group, "__delitem__", delete)
        with pytest.raises(ExecutionError, match="primary decision failure") as error:
            commit()
    assert isinstance(error.value.__cause__, OSError)
    if rollback_failure:
        assert "rollback deletion failure" in str(error.value)
        with pytest.raises(ExecutionError, match="close and recover"):
            writer.append_event("must not continue", {})
        with pytest.raises(ExecutionError, match="rollback failed"):
            writer.close("completed")
    else:
        assert destination not in writer._file and pending not in writer._file
        if not initial:
            assert writer._file[ROOT].attrs["count"] == 1
            assert writer._file[ROOT].attrs["last_sha256"] == previous
            assert len(list(iter_decisions(writer._file))) == 1
        commit()
        if initial:
            writer.record_spectrum_decision("initialize", {})
        assert len(list(iter_decisions(writer._file))) == (1 if initial else 2)
        writer.close("aborted")
