"""Synthetic Qt worker + HDF5 + plotted publication benchmark; no VISA calls."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import time

import h5py
import numpy as np

from app.domain.quantities import DIMENSION_FREQUENCY, parse_quantity
from app.domain.spectrum_correction import (
    BackgroundProfile, CorrectionConfig, SpectrumAcquisitionContext,
    SpectrumFrameEnvelope, SweepEvidence, TemporalAverageMode,
)


def process_memory():
    """Current working set on Windows; do not mislabel a high-water mark as RSS."""
    if os.name != "nt":
        return {"rss_bytes": None, "peak_rss_bytes": None, "source": "unavailable"}
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "peak", "working_set", "peak_paged", "paged", "peak_nonpaged", "nonpaged",
                "pagefile", "peak_pagefile",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return {"rss_bytes": counters.working_set, "peak_rss_bytes": counters.peak,
            "source": "Windows GetProcessMemoryInfo"}


def latency(values):
    samples = np.asarray(values, dtype=float)
    if not samples.size:
        return None
    return dict(zip(("p50_ms", "p95_ms", "p99_ms", "max_ms"),
                    map(float, np.r_[np.percentile(samples, [50, 95, 99]), samples.max()])))


def benchmark_pipeline(output: Path, *, points=10001, frames=200, warmup=20, rate_hz=20.0,
                       profiling_scope=None):
    if points < 3 or frames < 1 or warmup < 0 or not np.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError("Require points >= 3, frames >= 1, warmup >= 0, finite positive rate_hz.")
    output = Path(output)
    archive, screenshot = output.with_suffix(".h5"), output.with_suffix(".png")
    if any(path.exists() for path in (output, archive, screenshot)):
        raise FileExistsError("Benchmark output, archive and screenshot must all be new paths.")
    output.parent.mkdir(parents=True, exist_ok=True)
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
    from app.devices.anritsu_ms2830a.ui.correction_controller import (
        CorrectionSessionRequest, SpectrumCorrectionController,
    )
    from app.storage.hdf5_reader import Hdf5RunReader
    from app.ui.widgets import SpectrumPlotWidget

    application = QApplication.instance() or QApplication([])
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/segoeui.ttf").is_file():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        application.setFont(QFont("Segoe UI", 10))
    plot = SpectrumPlotWidget()
    plot.resize(1100, 650)
    plot.set_labels(x="Frequency", x_unit="Hz", y="Synthetic signed residual", y_unit="W")
    plot.show()
    application.processEvents()
    context = SpectrumAcquisitionContext(np.linspace(1e6, 6e9, points), "synthetic-pipeline-benchmark")
    baseline = np.full(points, 1e-9)
    watts = baseline.copy()
    watts[points // 2:points // 2 + 2] += [1e-10, -1e-10]
    seed = 20261003
    rng = np.random.default_rng(seed)
    expected_sum = np.zeros(points)
    frequencies = tuple(context.frequencies_hz)
    config = CorrectionConfig(average_mode=TemporalAverageMode.BLOCK)
    profile = BackgroundProfile("synthetic-background", context.context_id, baseline,
                                np.zeros(points), None, 100, 0, 10, "synthetic only")
    controller = SpectrumCorrectionController(queue_frames=8)
    acknowledgements, failures = [], []
    controller.completed.connect(lambda *args: acknowledgements.append(args))
    controller.failed.connect(lambda *args: failures.append(args))
    max_queue = 0

    def wait_for(operation, submit):
        nonlocal max_queue
        if acknowledgements:
            raise RuntimeError("Unexpected unconsumed worker acknowledgement")
        submit()
        deadline = time.monotonic() + 120
        while not acknowledgements and not failures:
            max_queue = max(max_queue, controller.queued_count)
            application.processEvents()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Worker timeout waiting for {operation}")
            time.sleep(0.0005)
        if failures:
            raise RuntimeError(f"Worker failed: {failures}")
        name, payload = acknowledgements.pop(0)
        if name != operation:
            raise RuntimeError(f"Expected {operation}, received {name}")
        return payload

    commit_ms, publication_ms, plot_ms, lateness_ms = [], [], [], []
    memory = []
    try:
        wait_for("start", lambda: controller.start_session(CorrectionSessionRequest(
            context, config, archive, "synthetic_benchmark: true\n", "SYNTHETIC;NO_VISA",
            profile=profile, simulation_mode=True,
        )))
        start = time.perf_counter()
        total = frames + warmup
        for index in range(total):
            scheduled = start + index / rate_hz
            while time.perf_counter() < scheduled:
                application.processEvents()
                time.sleep(min(0.001, max(0, scheduled - time.perf_counter())))
            raw_watts = watts * rng.lognormal(mean=-0.03 ** 2 / 2, sigma=0.03, size=points)
            expected_sum += raw_watts
            dbm = tuple(10 * np.log10(raw_watts) + 30)
            envelope = SpectrumFrameEnvelope(index, "synthetic-signal", context.context_id, 0,
                                             20 + index / rate_hz, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
            trace = SpectrumTrace(frequencies, dbm, datetime.fromtimestamp(envelope.acquired_at_s, timezone.utc),
                                  "TRAC1")
            frame_start = time.perf_counter()
            ack = wait_for("frame", lambda: controller.ingest(envelope, trace))
            committed = time.perf_counter()
            if not ack["accepted"] or ack["committed_point_count"] != index + 1:
                raise RuntimeError("Lost or rejected synthetic frame")
            view = ack.get("view")
            if view is None or view.corrected.frame_id != index:
                raise RuntimeError("Committed signal has no matching published view")
            published = time.perf_counter()
            plot.set_trace("Raw − background", context.frequencies_hz, view.corrected.values_w, primary=True)
            application.processEvents()
            rendered = time.perf_counter()
            if index >= warmup:
                commit_ms.append((committed - frame_start) * 1000)
                publication_ms.append((published - frame_start) * 1000)
                plot_ms.append((rendered - published) * 1000)
                lateness_ms.append(max(0, frame_start - scheduled) * 1000)
            if index % max(1, int(rate_hz)) == 0 or index == total - 1:
                memory.append({"frame": index, "elapsed_s": rendered - start, **process_memory()})
        elapsed = time.perf_counter() - start
        np.testing.assert_allclose(view.corrected.values_w, expected_sum / total - baseline,
                                   rtol=1e-8, atol=1e-23)
        stop_started = time.perf_counter()
        controller.stop_session("aborted")
        stop_submit_ms = (time.perf_counter() - stop_started) * 1000
        wait_for("stop", lambda: None)
        stop_close_ms = (time.perf_counter() - stop_started) * 1000
        memory.append({"phase": "after_stop", "elapsed_s": time.perf_counter() - start, **process_memory()})
        summary = Hdf5RunReader.summary(archive)
        if summary.point_count != total or summary.status != "aborted":
            raise RuntimeError("Closed archive count/status differs from worker acknowledgements")
        if not plot.grab().save(str(screenshot)):
            raise OSError("Could not save benchmark plot")
        report = {
            "scope": "Synthetic Qt worker + per-frame HDF5 commit + signed plot; no VISA/hardware qualification",
            "python": platform.python_version(), "numpy": np.__version__, "hdf5": h5py.version.hdf5_version,
            "platform": platform.platform(), "points": points, "frames_measured": frames,
            "cpu": platform.processor(),
            "blas_thread_environment": {name: os.environ.get(name) for name in (
                "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
            )},
            "warmup_frames": warmup, "frames_committed": summary.point_count, "lost_frames": 0,
            "simulation_seed": seed, "source_model": "positive lognormal power, sigma=0.03, known signed features",
            "profiling_scope": profiling_scope,
            "publication_source": "committed_frame_acknowledgement",
            "requested_rate_hz": rate_hz, "elapsed_s": elapsed, "achieved_rate_hz": total / elapsed,
            "receive_to_commit": latency(commit_ms), "receive_to_publication": latency(publication_ms),
            "plot_set_data_and_process_events": latency(plot_ms), "schedule_lateness": latency(lateness_ms),
            "queue_max_observed": max_queue, "queue_limit": 8, "stop_submission_ms": stop_submit_ms,
            "stop_archive_close_ms": stop_close_ms, "memory_samples": memory,
            "archive": str(archive), "archive_bytes": archive.stat().st_size, "screenshot": str(screenshot),
            "limitations": ["Acknowledgement-paced producer; schedule lateness exposes inability to sustain requested rate",
                            "GUI event processing includes painting but is not physical display latency",
                            "One Stop sample; no p95 Stop qualification", "OS thread/handle counts not measured",
                            "Short run does not establish 30-minute backlog or two-hour soak stability"],
        }
        if profiling_scope is not None:
            report["limitations"].append(
                "Profiled run includes overhead; Python 3.14 scope may capture overlapping worker/GUI callbacks"
            )
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        return report
    finally:
        if not controller.close(wait_ms=5000):
            raise RuntimeError("Benchmark worker did not terminate within 5 seconds")
        controller.deleteLater()
        plot.close()
        plot.deleteLater()
        application.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, default=10001)
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--rate", default="20 Hz")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rate = parse_quantity(args.rate, DIMENSION_FREQUENCY).si_value
    report = benchmark_pipeline(args.output, points=args.points, frames=args.frames, warmup=args.warmup, rate_hz=rate)
    print(json.dumps({key: report[key] for key in (
        "receive_to_commit", "receive_to_publication", "stop_archive_close_ms", "achieved_rate_hz",
    )}, indent=2))


if __name__ == "__main__":
    main()
