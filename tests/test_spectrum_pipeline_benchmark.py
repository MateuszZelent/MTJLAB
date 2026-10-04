"""Exercise the actual benchmark archive and GUI publication, not timing thresholds."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from app.storage.hdf5_reader import Hdf5RunReader
from tools.benchmark_spectrum_async_pipeline import benchmark_async_pipeline
from tools.benchmark_spectrum_pipeline import benchmark_pipeline


def test_pipeline_benchmark_preserves_archive_counts_sign_and_output_exclusivity(tmp_path):
    output = tmp_path / "pipeline.json"
    report = benchmark_pipeline(output, points=101, frames=5, warmup=2, rate_hz=20)
    assert report["frames_committed"] == 7
    assert report["lost_frames"] == 0
    assert report["publication_source"] == "committed_frame_acknowledgement"
    assert report["queue_max_observed"] <= report["queue_limit"]
    assert report["receive_to_publication"]["p50_ms"] >= report["receive_to_commit"]["p50_ms"]
    assert report["stop_archive_close_ms"] >= report["stop_submission_ms"]
    stored = Hdf5RunReader.spectrum_correction(output.with_suffix(".h5"), 6)
    assert stored.count == 7 and stored.values_w[51] < 0 < stored.values_w[50]
    assert Hdf5RunReader.summary(output.with_suffix(".h5")).status == "aborted"
    assert json.loads(output.read_text(encoding="utf-8"))["frames_committed"] == 7
    assert output.with_suffix(".png").is_file()
    if os.name == "nt":
        assert all(sample["rss_bytes"] > 0 for sample in report["memory_samples"])
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        benchmark_pipeline(output, points=101, frames=5, warmup=2, rate_hz=20)
    assert output.read_bytes() == before


@pytest.mark.parametrize("rate", [0, -1, np.nan, np.inf])
def test_pipeline_benchmark_rejects_invalid_source_rate_before_creating_files(tmp_path, rate):
    with pytest.raises(ValueError):
        benchmark_pipeline(tmp_path / "bad.json", rate_hz=rate)
    assert not list(tmp_path.iterdir())


def test_async_pipeline_archives_every_frame_with_slower_preview(tmp_path):
    output = tmp_path / "async.json"
    report = benchmark_async_pipeline(output, points=101, frames=12, warmup=2,
                                      rate_hz=20, render_interval_s=0.2)
    assert report["frames_committed"] == 14
    assert report["lost_frames"] == 0
    assert 0 < report["snapshots_received"] < report["frames_committed"]
    assert report["publication_source"] == "committed_frame_acknowledgement"
    assert report["publication_samples"] == 12
    assert report["receive_to_publication"] == report["receive_to_commit"]
    assert 0 < report["plot_updates"] < report["frames_committed"]
    assert report["last_rendered_frame_id"] == 13
    assert report["queue_max_observed"] <= report["queue_limit"]
    archive = output.with_suffix(".h5")
    assert Hdf5RunReader.summary(archive).point_count == 14
    result = Hdf5RunReader.spectrum_correction(archive, 13)
    assert result.count == 14
    assert result.values_w[51] < 0 < result.values_w[50]
    for index in range(14):
        assert Hdf5RunReader.spectrum(archive, index) is not None
    assert report["qt_paint_event"]["max_ms"] >= 0
    journal = [json.loads(line) for line in output.with_suffix(".resources.jsonl").read_text(encoding="utf-8").splitlines()]
    assert journal[0]["type"] == "incomplete_benchmark_resource_journal"
    assert journal[1:] == report["memory_samples"]
    assert report["disk_budget"]["free_bytes_before"] > report["disk_budget"]["reserve_bytes"]
    if os.name == "nt":
        assert all(sample["os_threads"] > 0 and sample["os_handles"] > 0 for sample in report["memory_samples"])
        assert report["resources_after_worker_shutdown"]["os_threads"] <= report["memory_samples"][-1]["os_threads"]
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        benchmark_async_pipeline(output, points=101, frames=12)
    assert output.read_bytes() == before


@pytest.mark.parametrize("rate,interval", [(0, 0.05), (np.inf, 0.05), (20, 0.01), (20, np.nan)])
def test_async_pipeline_rejects_invalid_schedule_before_creating_files(tmp_path, rate, interval):
    with pytest.raises(ValueError):
        benchmark_async_pipeline(tmp_path / "bad.json", rate_hz=rate, render_interval_s=interval)
    assert not list(tmp_path.iterdir())
