"""Disk preflight and read-only OS telemetry for long pipeline qualification."""

import os
from types import SimpleNamespace

import pytest

from tools.spectrum_benchmark_resources import archive_disk_budget, process_resources


def test_disk_preflight_rejects_insufficient_space_without_creating_directories(tmp_path, monkeypatch):
    import tools.spectrum_benchmark_resources as module

    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024))
    destination = tmp_path / "new" / "benchmark"
    with pytest.raises(OSError, match="Insufficient archive disk budget"):
        archive_disk_budget(destination, points=10001, frames=36020)
    assert not destination.exists()


def test_disk_estimate_includes_metadata_and_explicit_reserve(tmp_path, monkeypatch):
    import tools.spectrum_benchmark_resources as module

    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**12))
    report = archive_disk_budget(tmp_path, points=10001, frames=36020)
    assert report["estimated_archive_bytes"] > 36020 * 10001 * 48
    assert report["reserve_bytes"] == 1024**3
    assert report["required_free_bytes"] == (report["estimated_archive_bytes"]
                                            + report["estimated_conversion_bytes"] + report["reserve_bytes"])
    assert report["free_bytes_before"] == 10**12
    with pytest.raises(ValueError):
        archive_disk_budget(tmp_path, points=True, frames=20)


@pytest.mark.skipif(os.name != "nt", reason="Windows native telemetry")
def test_repeated_thread_snapshots_do_not_leak_os_handles():
    first = process_resources()
    for _ in range(30):
        sample = process_resources()
        assert sample["os_threads"] > 0 and sample["os_handles"] > 0
    last = process_resources()
    assert last["os_handles"] <= first["os_handles"] + 2
