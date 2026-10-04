"""Reproducible CPU baseline; run with python -m tools.benchmark_spectrum_correction.

This measures the deterministic numerical core and immutable publication,
not instrument throughput, GUI painting, disk I/O or scientific qualification.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import time

import numpy as np

from app.domain.spectrum_correction import (
    BackgroundProfile, CorrectionConfig, SpectrumAcquisitionContext,
    SpectrumFrameEnvelope, SweepEvidence, TemporalAverageMode,
)
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor


def benchmark(points: int, frames: int, mode: TemporalAverageMode):
    context = SpectrumAcquisitionContext(np.arange(points, dtype=float), "synthetic-rms-benchmark")
    reference = np.full(points, 1e-9)
    raw = reference.copy()
    raw[points // 2] += 1e-10
    raw[points // 2 + 1] -= 1e-10
    dbm = 10 * np.log10(raw) + 30
    profile = BackgroundProfile("benchmark-reference", context.context_id, reference,
                                np.zeros(points), None, 100, 0, 10, "synthetic baseline")
    processor = RealtimeSpectrumProcessor(context, CorrectionConfig(average_mode=mode))
    processor.set_background_profile(profile)
    ingest_ns, publish_ns = [], []
    for index in range(frames + 100):
        envelope = SpectrumFrameEnvelope(index, "signal", context.context_id, 0, 20 + index * 0.05,
                                         evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
        start = time.perf_counter_ns()
        accepted = processor.ingest(envelope, dbm)
        finish = time.perf_counter_ns()
        assert accepted
        if index >= 100:
            ingest_ns.append(finish - start)
        if index % 4 == 0:  # 20 Hz publication at a synthetic 80 Hz source.
            start = time.perf_counter_ns()
            result = processor.snapshot()
            elapsed = time.perf_counter_ns() - start
            if index >= 100:
                publish_ns.append(elapsed)
    result = processor.snapshot()
    np.testing.assert_allclose(result.values_w[points // 2:points // 2 + 2],
                               [1e-10, -1e-10], rtol=1e-11)

    def summary(values):
        values_ms = np.asarray(values) / 1e6
        return dict(zip(("p50_ms", "p95_ms", "p99_ms", "max_ms"),
                        map(float, np.r_[np.percentile(values_ms, [50, 95, 99]), values_ms.max()])))

    return {
        "points": points, "frames": frames, "mode": str(mode),
        "ingest": summary(ingest_ns), "immutable_publication": summary(publish_ns),
        "accepted_frames": processor.accepted_frames, "rejected_frames": processor.rejected_frames,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=2000)
    parser.add_argument("--points", type=int, nargs="+", default=[1001, 10001, 100001])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.frames < 100 or any(points < 3 for points in args.points):
        parser.error("Use at least 100 frames and at least 3 points.")
    report = {
        "scope": "CPU core and immutable publication only; no hardware/GUI/storage/soak qualification",
        "python": platform.python_version(), "numpy": np.__version__, "platform": platform.platform(),
        "results": [benchmark(points, args.frames, mode) for points in args.points
                    for mode in TemporalAverageMode],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
