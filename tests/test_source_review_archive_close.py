"""Finalization errors do not prevent other handles from being closed."""
from unittest.mock import Mock

import h5py
import pytest

from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.storage.hdf5_writer import Hdf5RunWriter


@pytest.mark.parametrize("stage", ["public", "hdf5_flush", "csv_flush"])
def test_close_attempts_all_resources_and_latches_failure(tmp_path, monkeypatch, stage):
    path = tmp_path / "close.h5"
    writer = Hdf5RunWriter(path, recipe_source="", settings_source="", plan_hash="close",
                          device_idn={}, csv_summary_path=tmp_path / "close.csv")
    writer.append(MeasurementPoint(0, {}, {"voltage_v": .001}))
    stream = writer._csv_stream
    if stage == "public":
        monkeypatch.setattr(writer._thatec, "close", Mock(side_effect=OSError("public fault")))
    elif stage == "hdf5_flush":
        original = h5py.File.flush
        fired = False

        def flush(file):
            nonlocal fired
            if file is writer._file and not fired:
                fired = True
                raise OSError("flush fault")
            return original(file)

        monkeypatch.setattr(h5py.File, "flush", flush)
    else:
        writer._csv_stream = Mock(wraps=stream)
        writer._csv_stream.flush.side_effect = OSError("CSV flush fault")
    with pytest.raises(ExecutionError, match="finalize"):
        writer.close("completed")
    assert not writer._file.id.valid
    assert stream.closed
    with pytest.raises(ExecutionError, match="finalize"):
        writer.close("completed")
    with h5py.File(path) as stored:
        assert stored["run"].attrs["status"] == "faulted"
        assert not stored.attrs["measurement running"]
        assert "storage_close_error" in stored["run"].attrs


@pytest.mark.parametrize("stage", ["public", "csv"])
def test_constructor_failure_releases_partial_archive(tmp_path, monkeypatch, stage):
    import app.storage.hdf5_writer as module

    opened = []
    original_file = h5py.File

    def tracked_file(*args, **kwargs):
        result = original_file(*args, **kwargs)
        opened.append(result)
        return result

    monkeypatch.setattr(h5py, "File", tracked_file)
    streams = []
    if stage == "public":
        monkeypatch.setattr(module, "ThatecHdf5Writer", Mock(side_effect=OSError("initialization fault")))
    else:
        original_csv = Hdf5RunWriter._open_csv_summary

        def failed_csv(writer):
            original_csv(writer)
            streams.append(writer._csv_stream)
            raise OSError("initialization fault")

        monkeypatch.setattr(Hdf5RunWriter, "_open_csv_summary", failed_csv)
    with pytest.raises(OSError, match="initialization fault"):
        Hdf5RunWriter(tmp_path / "init.h5", recipe_source="", settings_source="",
                      plan_hash="init", device_idn={}, csv_summary_path=tmp_path / "init.csv")
    assert opened and all(not file.id.valid for file in opened)
    assert all(stream.closed for stream in streams)
    with original_file(tmp_path / "init.h5") as stored:
        assert stored["run"].attrs["status"] == "faulted"


def test_validation_exception_marks_fault_and_stays_latched(tmp_path, monkeypatch):
    from app.storage.thatec_validator import ThatecCompatibilityValidator

    path = tmp_path / "validation.h5"
    writer = Hdf5RunWriter(path, recipe_source="", settings_source="", plan_hash="validation", device_idn={})
    writer.append(MeasurementPoint(0, {}, {"voltage_v": .001}))
    monkeypatch.setattr(ThatecCompatibilityValidator, "validate", Mock(side_effect=OSError("validator failed")))
    for _ in range(2):
        with pytest.raises(ExecutionError, match="validator failed"):
            writer.close("completed")
    with h5py.File(path) as stored:
        assert stored["run"].attrs["status"] == "faulted"
        assert "validator failed" in stored["run"].attrs["storage_validation_error"]


def test_resume_failure_closes_csv_and_hdf5(tmp_path, monkeypatch):
    path = tmp_path / "resume.h5"
    kwargs = dict(recipe_source="", settings_source="", plan_hash="resume", device_idn={})
    writer = Hdf5RunWriter(path, **kwargs)
    writer.append(MeasurementPoint(0, {}, {"voltage_v": .001}))
    writer.close("aborted")
    handles = []

    def fail_rebuild(resumed):
        handles.extend([resumed._file, resumed._csv_stream])
        raise OSError("rebuild fault")

    monkeypatch.setattr(Hdf5RunWriter, "_rebuild_csv_summary_from_committed_points", fail_rebuild)
    del kwargs["device_idn"]
    with pytest.raises(OSError, match="rebuild fault"):
        Hdf5RunWriter.resume(path, **kwargs, checkpoint_count=1, csv_summary_path=tmp_path / "resume.csv")
    assert len(handles) == 2
    assert not handles[0].id.valid
    assert handles[1].closed
