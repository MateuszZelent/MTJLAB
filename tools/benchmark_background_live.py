"""Profile signed-background Live stages without opening a hardware session.

Run with python -m tools.benchmark_background_live --output artifacts/live.json.
Storage and GUI measurements use the production worker/page; source data are
synthetic. Instrument sweep/transport latency requires a separate hardware run.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import platform
import pstats
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from unittest.mock import MagicMock

import numpy as np
import scipy
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest,
    _CorrectionWorker,
)
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.domain.spectrum_correction import (
    BackgroundProfile,
    CorrectionConfig,
    SpectrumAcquisitionContext,
    SpectrumFrameEnvelope,
    SweepEvidence,
    TemporalAverageMode,
)
from app.settings import SettingsRepository
from app.spectrum.analysis import clean_spectrum_pipeline
from tools.benchmark_spectrum_pipeline import latency


def benchmark(output: Path, *, points: int, frames: int):
    application = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 10))
    archive = output.with_name(f"{output.stem}-{points}.h5")
    if archive.exists():
        raise FileExistsError(archive)
    context = SpectrumAcquisitionContext(np.linspace(200e6, 2.4e9, points), "synthetic-live", settings_verified=True)
    background = np.full(points, 1e-9)
    acquired_at = datetime.now(UTC).timestamp()
    profile = BackgroundProfile("benchmark", context.context_id, background,
                                np.zeros(points), None, 100, acquired_at-20, acquired_at-10, "synthetic noise")
    rng = np.random.default_rng(12)
    residual = rng.normal(0, 1e-13, points)
    residual[points // 3] += 8e-11
    residual[points // 2] -= 9e-11
    powers_dbm = tuple(10 * np.log10(background + residual) + 30)
    worker = _CorrectionWorker(deque(), Lock(), None, {}, Event())
    frame_ms, filter_ms, render_ms, processing_ms, commit_ms = [], {}, [], [], []
    profiler = cProfile.Profile()
    worker._execute("start", CorrectionSessionRequest(
        context, CorrectionConfig(average_mode=TemporalAverageMode.EMA_PREVIEW),
        archive, "synthetic: true", "ANRITSU,SIM", profile=profile, simulation_mode=True,
    ))
    try:
        for index in range(frames):
            envelope = SpectrumFrameEnvelope(index, "signal", context.context_id, 0, acquired_at + index * .05,
                                            evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
            trace = SpectrumTrace(tuple(context.frequencies_hz), powers_dbm,
                                  datetime.fromtimestamp(envelope.acquired_at_s, UTC), "TRAC1",
                                  configuration_generation=context.configuration_generation)
            started = time.perf_counter()
            ack = worker._execute("frame", (envelope, trace))
            frame_ms.append((time.perf_counter() - started) * 1000)
            processing_ms.append(ack["processing_duration_s"] * 1000)
            commit_ms.append(ack["commit_duration_s"] * 1000)
            assert ack["accepted"] and ack["committed_point_count"] == index + 1
        view = ack["view"]
        for modes in (("denoise",), ("narrow_reject",), ("narrow_reject", "denoise")):
            samples = []
            for _ in range(frames):
                started = time.perf_counter()
                clean_spectrum_pipeline(view.corrected.values_w, unit="W", modes=modes,
                                        frequencies_hz=context.frequencies_hz)
                samples.append((time.perf_counter() - started) * 1000)
            filter_ms["+".join(modes)] = latency(samples)
        profiler.enable()
        clean_spectrum_pipeline(view.corrected.values_w, unit="W", modes=("narrow_reject", "denoise"),
                                frequencies_hz=context.frequencies_hz)
        profiler.disable()
    finally:
        worker._execute("stop", "completed")
    settings = SettingsRepository(Path(".config/settings.yml")).load().settings
    page = AnritsuPage(MagicMock(), settings, single_sweep_available=True)
    page.resize(1450, 900)
    page.show()
    workspace = page.correction_workspace
    workspace._context, workspace._profile = context, profile
    page._shared_background_changed()
    page.cleanup_filters["background"].setChecked(True)
    page._background_config_snapshot = (context.configuration_generation, context.configuration_fingerprint)
    page._background_config_timer.stop()
    page._background_config_pending = None
    try:
        for _ in range(frames):
            started = time.perf_counter()
            workspace._receive_committed_view(view)
            workspace._render()
            application.processEvents()
            render_ms.append((time.perf_counter() - started) * 1000)
        page.cleanup_filters["narrow_reject"].setChecked(True)
        page.cleanup_filters["denoise"].setChecked(True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            application.processEvents()
            result = page._cleanup_result
            if result is not None and result.unit == "W" and "denoise" in result.method.lower():
                break
            time.sleep(.01)
        if page._cleanup_result is None or page._cleanup_result.unit != "W":
            raise RuntimeError(f"Background preview did not complete: {page.analysis_status.text()}")
        page.grab().save(str(output.with_name(f"{output.stem}-{points}.png")))
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()
    statistics = pstats.Stats(profiler)
    hotspots = []
    for (file, line, function), (_primitive, calls, own_s, total_s, _callers) in statistics.stats.items():
        hotspots.append({"function": f"{Path(file).name}:{line}:{function}",
                         "calls": calls, "self_ms": own_s * 1000, "cumulative_ms": total_s * 1000})
    return {"points": points, "frames": frames, "commit_and_correction": latency(frame_ms),
            "correction_only": latency(processing_ms), "commit_only": latency(commit_ms),
            "filters": filter_ms, "repeated_view_and_render": latency(render_ms),
            "filter_hotspots": sorted(hotspots, key=lambda row: row["cumulative_ms"], reverse=True)[:12]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--points", type=int, nargs="+", default=[1001, 10001])
    parser.add_argument("--frames", type=int, default=12)
    args = parser.parse_args()
    if args.frames < 1 or any(value not in (1001, 10001) for value in args.points):
        parser.error("Use positive frames and 1001/10001 points (supported default narrow-filter grid).")
    if args.output.exists():
        parser.error("Output must be a new path.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": "Synthetic CPU, filters, committed HDF5 and real page rendering; no hardware latency",
              "python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__,
              "results": [benchmark(args.output, points=points, frames=args.frames) for points in args.points]}
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
