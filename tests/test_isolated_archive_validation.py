"""Native validation remains mandatory without sharing Qt process state."""

import hashlib
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import h5py
import pytest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.validation_worker import validate_archive_isolated


def writer_at(path):
    writer = Hdf5RunWriter(
        path,
        recipe_source="{}",
        settings_source="{}",
        plan_hash="test",
        device_idn={"anritsu": "SIMULATED"},
        isolate_validation=True,
    )
    trace = SpectrumTrace((1e6, 2e6, 3e6), (-60.0, -50.0, -60.0), datetime.now(UTC), "TRAC1")
    writer.append(MeasurementPoint(0, {}, {}), trace)
    return writer


def test_isolated_roundtrip_is_read_only_and_returns_full_report(tmp_path):
    path = tmp_path / "result.h5"
    writer_at(path).close("completed")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = validate_archive_isolated(path)
    assert result.valid and result.pythat_version == "0.2.14"
    assert ("Frequency", 3) in result.dimensions
    assert "Spectrum" in result.data_variables
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("failure", ["timeout", "native crash", "missing report"])
def test_failed_validation_cannot_publish_completed_run(tmp_path, monkeypatch, failure):
    path = tmp_path / "failed.h5"
    writer = writer_at(path)

    def failed(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return SimpleNamespace(
            returncode=-1 if failure == "native crash" else 0, stderr=b"validation failed"
        )

    monkeypatch.setattr("app.storage.validation_worker.subprocess.run", failed)
    with pytest.raises(ExecutionError, match="Final HDF5 contract validation failed"):
        writer.close("completed")
    with h5py.File(path) as file:
        assert file["run"].attrs["status"] == "faulted"
        assert file["run"].attrs["storage_validation_error"]
        assert len(file["points"]) == 1


def test_child_reports_corrupt_archive_instead_of_success(tmp_path):
    path = tmp_path / "invalid.h5"
    with h5py.File(path, "w") as file:
        file.create_group("unrelated")
    assert not validate_archive_isolated(path).valid


def test_large_child_diagnostics_are_spooled_and_only_tail_is_loaded(tmp_path, monkeypatch):
    streams = []
    def noisy_failure(*args, **kwargs):
        stream = kwargs["stderr"]
        assert stream != subprocess.PIPE and stream.fileno() >= 0
        streams.append(stream)
        stream.write(b"HEAD-MARKER\n")
        for _ in range(32):
            stream.write(b"x" * 65536)
        stream.write(b"\nTAIL-MARKER: native validation failed")
        stream.flush()
        return SimpleNamespace(returncode=-1)
    monkeypatch.setattr("app.storage.validation_worker.subprocess.run", noisy_failure)
    report = validate_archive_isolated(tmp_path / "result.h5")
    assert not report.valid
    message = report.errors[0].message
    assert "HEAD-MARKER" not in message
    assert message.endswith("TAIL-MARKER: native validation failed")
    assert len(message) < 2100
    assert streams[0].closed
