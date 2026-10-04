"""Independent synthetic producer, bounded archive worker and 20 Hz GUI; no VISA."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import time

import h5py
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest, SpectrumCorrectionController,
)
from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_TIME, parse_quantity
from app.domain.spectrum_correction import (
    BackgroundProfile, CorrectionConfig, SpectrumAcquisitionContext,
    SpectrumFrameEnvelope, SweepEvidence, TemporalAverageMode,
)
from app.storage.hdf5_reader import Hdf5RunReader
from app.ui.widgets import SpectrumPlotWidget
from tools.benchmark_spectrum_pipeline import latency, process_memory
from tools.spectrum_benchmark_resources import archive_disk_budget, process_resources


def benchmark_async_pipeline(output: Path, *, points=10001, frames=200, warmup=20,
                             rate_hz=20.0, render_interval_s=0.05):
    if points < 3 or frames < 1 or warmup < 0 or not np.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError("Require points >=3, frames >=1, warmup >=0 and finite positive rate.")
    if not np.isfinite(render_interval_s) or render_interval_s < 0.05:
        raise ValueError("GUI interval must be finite and at least 50 ms (at most 20 Hz).")
    output = Path(output)
    archive, screenshot = output.with_suffix(".h5"), output.with_suffix(".png")
    telemetry = output.with_suffix(".resources.jsonl")
    if any(path.exists() for path in (output, archive, screenshot, telemetry)):
        raise FileExistsError("All benchmark paths must be new.")
    disk_budget = archive_disk_budget(output.parent, points=points, frames=frames + warmup)
    output.parent.mkdir(parents=True, exist_ok=True)
    application = QApplication.instance() or QApplication([])
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/segoeui.ttf").is_file():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        application.setFont(QFont("Segoe UI", 10))
    state = {"started": False, "inflight": False, "snapshot_pending": False,
             "submitted": 0, "committed": 0, "rendered": -1, "closed": False,
             "view": None, "queue_max": 0, "snapshot_count": 0, "render_count": 0, "errors": []}
    paint_ms, data_ms, publication_ms, commit_ms, lateness_ms, display_ms = [], [], [], [], [], []
    snapshot_ms = []
    starts, memory = {}, []
    total = frames + warmup
    expected_sum = np.zeros(points)
    rng = np.random.default_rng(20261003)
    context = SpectrumAcquisitionContext(np.linspace(1e6, 6e9, points), "synthetic-async-pipeline")
    baseline = np.full(points, 1e-9)
    source_mean = baseline.copy()
    source_mean[points // 2:points // 2 + 2] += [1e-10, -1e-10]
    frequencies = tuple(context.frequencies_hz)
    profile = BackgroundProfile("synthetic-background", context.context_id, baseline, np.zeros(points),
                                None, 100, 0, 10, "synthetic only")

    class TimedPlot(pg.PlotWidget):
        def paintEvent(self, event):  # noqa: N802
            started = time.perf_counter()
            super().paintEvent(event)
            if state["started"] and warmup <= state["rendered"] < total:
                paint_ms.append((time.perf_counter() - started) * 1000)

    plot = SpectrumPlotWidget(plot_widget_factory=TimedPlot)
    plot.resize(1100, 650)
    plot.set_labels(x="Frequency", x_unit="Hz", y="Synthetic signed residual", y_unit="W")
    plot.show()
    application.processEvents()
    resources_before = process_resources()
    controller = SpectrumCorrectionController(queue_frames=8)
    producer, renderer, monitor = QTimer(), QTimer(), QTimer()
    for timer in (producer, renderer):
        timer.setTimerType(Qt.TimerType.PreciseTimer)
    producer.setInterval(1)
    renderer.setInterval(round(render_interval_s * 1000))
    monitor.setInterval(1000)
    resource_journal = telemetry.open("x", encoding="utf-8")
    resource_journal.write(json.dumps({"type": "incomplete_benchmark_resource_journal",
        "points": points, "frames": frames, "warmup": warmup, "rate_hz": rate_hz,
        "disk_budget": disk_budget, "resources_before_worker": resources_before}) + "\n")
    resource_journal.flush()

    def guarded(function):
        def run(*args):
            try:
                function(*args)
            except Exception as exc:
                state["errors"].append(str(exc))
                producer.stop()
                renderer.stop()
        return run

    def render(view):
        if view is None or view.corrected.frame_id == state["rendered"]:
            return
        result = view.corrected
        started = time.perf_counter()
        plot.set_trace("Raw − background", context.frequencies_hz, result.values_w, primary=True)
        if state["rendered"] < 0:
            plot.auto_range()
        state["rendered"] = result.frame_id
        state["render_count"] += 1
        if result.frame_id >= warmup:
            data_ms.append((time.perf_counter() - started) * 1000)
            if result.frame_id in starts:
                display_ms.append((time.perf_counter() - starts[result.frame_id]) * 1000)
        for index in tuple(starts):
            if index <= result.frame_id:
                del starts[index]

    def request_snapshot():
        if not state["snapshot_pending"]:
            state["snapshot_pending"] = True
            controller.request_snapshot()

    def produce():
        if not state["started"] or state["inflight"] or state["submitted"] == total:
            return
        index = state["submitted"]
        scheduled = state["start"] + index / rate_hz
        if time.perf_counter() < scheduled:
            return
        watts = source_mean * rng.lognormal(mean=-0.03 ** 2 / 2, sigma=0.03, size=points)
        expected_sum[:] += watts
        envelope = SpectrumFrameEnvelope(index, "synthetic-signal", context.context_id, 0,
                                         20 + index / rate_hz, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
        trace = SpectrumTrace(frequencies, tuple(10 * np.log10(watts) + 30),
                              datetime.fromtimestamp(envelope.acquired_at_s, timezone.utc), "TRAC1")
        starts[index] = time.perf_counter()
        if index >= warmup:
            lateness_ms.append(max(0, starts[index] - scheduled) * 1000)
        state["inflight"] = True
        state["submitted"] += 1
        controller.ingest(envelope, trace)

    def preview_tick():
        render(state["view"])
        if state["committed"]:
            request_snapshot()

    def sample_memory():
        free = shutil.disk_usage(output.parent).free
        memory.append({"committed": state["committed"], "elapsed_s": time.perf_counter() - state["start"],
                       "queue": controller.queued_count, "archive_bytes": archive.stat().st_size,
                       "disk_free_bytes": free, **process_memory(), **process_resources()})
        resource_journal.write(json.dumps(memory[-1], allow_nan=False) + "\n")
        resource_journal.flush()
        if free < disk_budget["reserve_bytes"] + disk_budget["estimated_conversion_bytes"]:
            raise OSError("Disk reserve for PyThat close reached during benchmark; stopping acquisition.")

    def completed(operation, payload):
        now = time.perf_counter()
        if operation == "start":
            state.update(started=True, start=now, cpu_start=time.process_time())
            sample_memory()
            producer.start()
            renderer.start()
            monitor.start()
        elif operation == "frame":
            index = payload["frame_id"]
            if not payload["accepted"] or payload["committed_point_count"] != state["committed"] + 1:
                raise RuntimeError("Rejected or misordered raw checkpoint")
            state["committed"] += 1
            state["inflight"] = False
            view = payload.get("view")
            if view is None or view.corrected.frame_id != index:
                raise RuntimeError("Committed signal has no matching published view")
            state["view"] = view
            if index >= warmup:
                commit_ms.append((now - starts[index]) * 1000)
                publication_ms.append((now - starts[index]) * 1000)
            if state["committed"] == total:
                state["acquisition_end"] = now
                state["acquisition_cpu_s"] = time.process_time() - state["cpu_start"]
                producer.stop()
                renderer.stop()
                request_snapshot()
        elif operation == "snapshot":
            state["snapshot_pending"] = False
            if payload is not None:
                state["view"] = payload
                index = payload.corrected.frame_id
                state["snapshot_count"] += 1
                if index >= warmup and index in starts:
                    snapshot_ms.append((now - starts[index]) * 1000)
            if state["committed"] == total:
                if payload is None or payload.corrected.frame_id != total - 1:
                    request_snapshot()
                else:
                    render(payload)
                    state["stop_start"] = time.perf_counter()
                    controller.stop_session("aborted")
                    state["stop_submit_ms"] = (time.perf_counter() - state["stop_start"]) * 1000
        elif operation == "stop":
            state["stop_close_ms"] = (now - state["stop_start"]) * 1000
            state["closed"] = True

    controller.completed.connect(guarded(completed))
    controller.failed.connect(lambda *args: state["errors"].append(str(args)))
    producer.timeout.connect(guarded(produce))
    renderer.timeout.connect(guarded(preview_tick))
    monitor.timeout.connect(guarded(sample_memory))
    try:
        controller.start_session(CorrectionSessionRequest(
            context, CorrectionConfig(average_mode=TemporalAverageMode.BLOCK), archive,
            "synthetic_benchmark: true\n", "SYNTHETIC;NO_VISA", profile=profile, simulation_mode=True,
        ))
        deadline = time.monotonic() + 120 + 10 * total / rate_hz
        while not state["closed"] and not state["errors"]:
            application.processEvents()
            state["queue_max"] = max(state["queue_max"], controller.queued_count)
            if time.monotonic() >= deadline:
                raise TimeoutError("Independent pipeline benchmark timed out")
            time.sleep(0.0005)
        if state["errors"]:
            raise RuntimeError(f"Independent pipeline failed: {state['errors']}")
        application.processEvents()
        sample_memory()
        result = state["view"].corrected
        np.testing.assert_allclose(result.values_w, expected_sum / total - baseline, rtol=1e-8, atol=1e-23)
        summary = Hdf5RunReader.summary(archive)
        if summary.point_count != total or summary.status != "aborted":
            raise RuntimeError("Closed archive disagrees with checkpoint acknowledgements")
        if not plot.grab().save(str(screenshot)):
            raise OSError("Could not save benchmark image")
        elapsed = state["acquisition_end"] - state["start"]
        report = {
            "scope": "Independent synthetic producer + bounded worker/HDF5 + GUI mailbox; no VISA",
            "python": platform.python_version(), "numpy": np.__version__, "hdf5": h5py.version.hdf5_version,
            "platform": platform.platform(), "points": points, "frames_measured": frames, "warmup_frames": warmup,
            "cpu_identifier": platform.processor(), "logical_cpu_count": os.cpu_count(),
            "acquisition_process_cpu_s": state["acquisition_cpu_s"],
            "blas_thread_environment": {name: os.environ.get(name) for name in (
                "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")},
            "frames_committed": total, "lost_frames": 0, "requested_rate_hz": rate_hz,
            "elapsed_s": elapsed, "achieved_rate_hz": total / elapsed,
            "render_interval_s": render_interval_s, "snapshots_received": state["snapshot_count"],
            "plot_updates": state["render_count"], "plot_update_samples": len(data_ms),
            "last_rendered_frame_id": state["rendered"], "receive_to_commit": latency(commit_ms),
            "receive_to_publication": latency(publication_ms), "receive_to_plot_update": latency(display_ms),
            "publication_source": "committed_frame_acknowledgement", "publication_samples": len(publication_ms),
            "receive_to_requested_snapshot": latency(snapshot_ms), "requested_snapshot_samples": len(snapshot_ms),
            "plot_set_data": latency(data_ms), "qt_paint_event": latency(paint_ms),
            "schedule_lateness": latency(lateness_ms), "queue_max_observed": state["queue_max"],
            "queue_limit": 8, "stop_submission_ms": state["stop_submit_ms"],
            "stop_archive_close_ms": state["stop_close_ms"], "memory_samples": memory,
            "disk_budget": disk_budget, "resources_before_worker": resources_before,
            "resource_journal": str(telemetry),
            "simulation_seed": 20261003, "archive": str(archive), "archive_bytes": archive.stat().st_size,
            "screenshot": str(screenshot), "limitations": [
                "Publication covers every measured committed frame acknowledgement; plot-update covers rendered frames",
                "Earlier reports measured requested snapshots as publication; compare that timing with receive_to_requested_snapshot",
                "Requested-snapshot samples exclude frames whose timing entry was already retired by rendering; sample populations may differ between versions",
                "Producer is acknowledgement paced; lateness exposes backpressure",
                "Qt paintEvent is software painting, not physical display latency",
                "One Stop sample; p95 Stop is not qualified",
                "Duration and resource samples alone do not prove a leak-free application or instrument",
            ],
        }
    finally:
        producer.stop()
        renderer.stop()
        monitor.stop()
        # Parentless timers own signal closures that refer back to the timers
        # and retain this cycle's raw/view buffers. Stopping does not destroy
        # those native connections; explicitly release them on Qt's thread.
        producer.deleteLater()
        renderer.deleteLater()
        monitor.deleteLater()
        resource_journal.close()
        if not controller.close(wait_ms=5000):
            raise RuntimeError("Independent benchmark worker did not terminate")
        controller.deleteLater()
        plot.close()
        plot.deleteLater()
        application.processEvents()
    report["resources_after_worker_shutdown"] = process_resources()
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, default=10001)
    length = parser.add_mutually_exclusive_group()
    length.add_argument("--frames", type=int)
    length.add_argument("--duration", help="Explicit time quantity, e.g. '30 min' or '2 h'.")
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--rate", default="20 Hz")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rate = parse_quantity(args.rate, DIMENSION_FREQUENCY).si_value
    frames = args.frames if args.frames is not None else 200
    if args.duration is not None:
        duration = parse_quantity(args.duration, DIMENSION_TIME).si_value
        if duration <= 0:
            parser.exit(1, "Duration must be positive.\n")
        frames = int(np.ceil(duration * rate))
    report = benchmark_async_pipeline(args.output, points=args.points, frames=frames, warmup=args.warmup, rate_hz=rate)
    print(json.dumps({key: report[key] for key in (
        "achieved_rate_hz", "receive_to_publication", "qt_paint_event", "schedule_lateness",
    )}, indent=2))


if __name__ == "__main__":
    main()
