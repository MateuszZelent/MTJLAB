"""Only complete event rows may authorize recovery after interruption."""

import h5py
import pytest

from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.storage.event_log import EVENT_COLUMNS, committed_event_count
from app.storage.hdf5_writer import Hdf5RunWriter
from tests.test_sweep_storage_faults import writer_for


@pytest.mark.parametrize("written_columns", range(1, 5))
@pytest.mark.parametrize("legacy", [False, True])
def test_resume_removes_partial_event_tail_before_next_append(tmp_path, written_columns, legacy):
    path = tmp_path / "interrupted.h5"
    writer = writer_for(path)
    writer.append(MeasurementPoint(0, {}, {"lakeshore.field_t": .001}))
    writer.append_event("complete", {"value": 1})
    # Model a process exit between column writes, without running rollback.
    events = writer._file["events"]
    if legacy:
        del events.attrs["committed_count"]
    for column in EVENT_COLUMNS[:written_columns]:
        events[column].resize((2,))
        # Last column remains unwritten to model resize-before-assignment.
        if column != EVENT_COLUMNS[written_columns - 1]:
            events[column][1] = "partial"
    writer._file.flush()
    writer._file.close()
    writer._closed = True
    assert committed_count_on_disk(path) == 1
    resumed = Hdf5RunWriter.resume(path, recipe_source="", settings_source="", plan_hash="faults", checkpoint_count=1)
    resumed.append_event("after_recovery", {"value": 2})
    resumed.close("completed")
    with h5py.File(path) as file:
        assert committed_event_count(file["events"]) == 3
        assert list(file["events/name"].asstr()[:]) == ["complete", "run_resumed", "after_recovery"]
        assert all(len(file["events"][column]) == 3 for column in EVENT_COLUMNS)


def committed_count_on_disk(path):
    with h5py.File(path) as file:
        return committed_event_count(file["events"])


@pytest.mark.parametrize("failed_flush", [1, 2])
def test_event_flush_failure_rolls_back_marker_and_all_columns(tmp_path, monkeypatch, failed_flush):
    writer = writer_for(tmp_path / "flush.h5")
    writer.append(MeasurementPoint(0, {}, {}))
    writer.append_event("before", {})
    original = h5py.File.flush
    calls = 0
    def flush(file):
        nonlocal calls
        calls += 1
        if calls == failed_flush:
            raise OSError("injected event flush")
        return original(file)
    with monkeypatch.context() as patch:
        patch.setattr(h5py.File, "flush", flush)
        with pytest.raises(OSError, match="injected"):
            writer.append_event("not_committed", {})
    assert committed_event_count(writer._file["events"]) == 1
    assert all(len(writer._file["events"][column]) == 1 for column in EVENT_COLUMNS)
    writer.append_event("after", {})
    writer.close("completed")


def test_committed_marker_beyond_available_rows_is_corruption(tmp_path):
    writer = writer_for(tmp_path / "corrupt.h5")
    writer._file["events"].attrs["committed_count"] = 1
    with pytest.raises(ValueError, match="committed event"):
        committed_event_count(writer._file["events"])
    writer._file.close()
    writer._closed = True


def test_uncommitted_boundary_is_never_used_for_resume(tmp_path):
    from app.engine.recovery import RunRecoveryManager
    from app.storage.thatec_validator import ThatecCompatibilityValidator
    from types import SimpleNamespace

    writer = writer_for(tmp_path / "boundary.h5")
    writer.append(MeasurementPoint(0, {}, {"lakeshore.field_t": .001}))
    writer.append_event("complete", {})
    events = writer._file["events"]
    for column, value in zip(EVENT_COLUMNS, ("timestamp", "info", "safe_resume_boundary", "{malformed"), strict=True):
        events[column].resize((2,))
        events[column][1] = value
    writer._file.flush()
    assert RunRecoveryManager._latest_boundary(writer._file, SimpleNamespace(), 1) is None
    writer._file["run"].attrs["status"] = "faulted"
    writer._file.attrs["measurement running"] = 0
    writer._file.close()
    writer._closed = True
    report = ThatecCompatibilityValidator().validate(writer.path)
    assert any(issue.path == "/events" and "uncommitted" in issue.message for issue in report.errors)


def test_failed_event_rollback_blocks_later_events(tmp_path, monkeypatch):
    writer = writer_for(tmp_path / "rollback.h5")
    writer.append(MeasurementPoint(0, {}, {"lakeshore.field_t": .001}))
    writer.append_event("before", {})
    original_set = h5py.Dataset.__setitem__
    original_resize = h5py.Dataset.resize
    def setitem(dataset, key, value):
        if dataset.name == "/events/message":
            raise OSError("primary event write")
        return original_set(dataset, key, value)
    def resize(dataset, size, *args, **kwargs):
        if dataset.name == "/events/name" and size == (1,):
            raise OSError("event rollback resize")
        return original_resize(dataset, size, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(h5py.Dataset, "__setitem__", setitem)
        patch.setattr(h5py.Dataset, "resize", resize)
        with pytest.raises(OSError, match="primary event write"):
            writer.append_event("failed", {})
    with pytest.raises(ExecutionError, match="close and recover"):
        writer.append_event("must not append", {})
    with pytest.raises(ExecutionError, match="close and recover"):
        writer.append(MeasurementPoint(1, {}, {"lakeshore.field_t": .002}))
    assert writer.point_count == 1
    with pytest.raises(ExecutionError, match="uncommitted event tail"):
        writer.close("faulted")


@pytest.mark.parametrize("fault", ["_event_log_fault", "_storage_faulted"])
def test_rollback_fault_blocks_all_new_records_and_completed_close(tmp_path, fault):
    writer = writer_for(tmp_path / "latched.h5")
    writer.append(MeasurementPoint(0, {}, {"lakeshore.field_t": .001}))
    setattr(writer, fault, True)
    # The guard must run before validating payloads or creating any group.
    operations = [
        lambda: writer.append(MeasurementPoint(1, {}, {})),
        lambda: writer.append_event("after_fault", {}),
        lambda: writer.store_reference(None),
        lambda: writer.initialize_spectrum_decisions(None, None),
        lambda: writer.store_recipe_spectrum_sweep(None),
        lambda: writer.record_spectrum_decision(None, None),
        lambda: writer.store_background_profile(None, None),
        lambda: writer.store_interference_calibration(None),
        lambda: writer.store_finalized_block(None, None),
    ]
    names_before = []
    writer._file.visit(names_before.append)
    for operation in operations:
        with pytest.raises(ExecutionError, match="close and recover"):
            operation()
    names_after = []
    writer._file.visit(names_after.append)
    assert names_after == names_before
    assert writer.point_count == 1
    writer.flush_checkpoint()  # Best-effort preservation remains available.
    for _ in range(2):
        with pytest.raises(ExecutionError, match="rollback failed"):
            writer.close("completed")
    assert not writer._file.id.valid
    with h5py.File(writer.path) as archive:
        assert archive["run"].attrs["status"] == "faulted"
        assert not archive.attrs["measurement running"]
