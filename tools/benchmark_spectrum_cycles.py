"""Repeated full pipeline lifecycles in one process; no VISA or soak qualification."""

import argparse
import gc
import json
from pathlib import Path
import time

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from app.domain.quantities import DIMENSION_FREQUENCY, parse_quantity
from tools.benchmark_spectrum_async_pipeline import benchmark_async_pipeline
from tools.benchmark_spectrum_pipeline import latency, process_memory
from tools.spectrum_benchmark_resources import archive_disk_budget, process_resources


def drain_deleted_objects(application):
    """Let Qt destroy deleteLater objects before measuring post-cycle resources."""
    for _ in range(2):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
        gc.collect()


def resource_summary(rows):
    """Descriptive same-process changes; no leak gate or false confidence interval."""
    result = {}
    for name in ("rss_bytes", "os_threads", "os_handles"):
        values = [row["resources_after_cleanup"].get(name) for row in rows]
        if any(value is None for value in values) or not values:
            result[name] = None
            continue
        samples = np.asarray(values, dtype=float)
        x = np.arange(len(samples), dtype=float)
        slope = None if len(samples) < 2 else float(np.dot(x - x.mean(), samples - samples.mean()) /
                                                   np.dot(x - x.mean(), x - x.mean()))
        result[name] = {"samples": len(samples), "first": values[0], "last": values[-1],
                        "last_minus_first": values[-1] - values[0], "minimum": min(values),
                        "maximum": max(values), "linear_slope_per_cycle": slope}
    return result


def benchmark_cycles(output, *, cycles=20, warmup_cycles=2, points=10001, frames=20,
                     warmup_frames=5, rate_hz=20.0):
    for value, lower, upper, name in ((cycles, 1, 200, "cycles"), (warmup_cycles, 0, 199, "warmup_cycles"),
                                     (points, 3, 10001, "points"), (frames, 1, 10000, "frames"),
                                     (warmup_frames, 0, 10000, "warmup_frames")):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"Invalid bounded integer {name}.")
    if warmup_cycles >= cycles or not np.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError("Require at least one measured cycle and a finite positive rate.")
    output = Path(output)
    directory = output.with_suffix(".cycles")
    journal = output.with_suffix(".cycles.jsonl")
    if any(path.exists() for path in (output, directory, journal)):
        raise FileExistsError("Cycle report, journal and archive directory must all be new.")
    budget = archive_disk_budget(output.parent, points=points, frames=cycles * (frames + warmup_frames))
    output.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    application = QApplication.instance() or QApplication([])
    drain_deleted_objects(application)
    initial_resources = {**process_memory(), **process_resources()}
    rows = []
    started = time.perf_counter()
    with journal.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "incomplete_pipeline_cycle_journal", "cycles_requested": cycles,
            "warmup_cycles": warmup_cycles, "points": points, "frames": frames, "warmup_frames": warmup_frames,
            "requested_rate_hz": rate_hz, "disk_budget": budget,
            "resources_before_first_cycle": initial_resources}) + "\n")
        stream.flush()
        for index in range(cycles):
            path = directory / f"cycle-{index:03d}.json"
            report = benchmark_async_pipeline(path, points=points, frames=frames, warmup=warmup_frames, rate_hz=rate_hz)
            drain_deleted_objects(application)
            row = {"cycle": index, "warmup_cycle": index < warmup_cycles,
                   "elapsed_s": time.perf_counter() - started, "cycle_report": str(path),
                   "frames_committed": report["frames_committed"], "lost_frames": report["lost_frames"],
                   "archive_bytes": report["archive_bytes"], "requested_rate_hz": report["requested_rate_hz"],
                   "achieved_rate_hz": report["achieved_rate_hz"], "queue_max_observed": report["queue_max_observed"],
                   "stop_submission_ms": report["stop_submission_ms"],
                   "stop_archive_close_ms": report["stop_archive_close_ms"],
                   "receive_to_publication": report["receive_to_publication"],
                   "publication_source": report["publication_source"],
                   "publication_samples": report["publication_samples"],
                   "receive_to_requested_snapshot": report["receive_to_requested_snapshot"],
                   "requested_snapshot_samples": report["requested_snapshot_samples"],
                   "qt_paint_event": report["qt_paint_event"], "schedule_lateness": report["schedule_lateness"],
                   "resources_after_cleanup": {**process_memory(), **process_resources()}}
            rows.append(row)
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            print(f"Pipeline cycles {index + 1}/{cycles}; committed {row['frames_committed']}", flush=True)
    measured = rows[warmup_cycles:]
    result = {"algorithm": "same-process-pipeline-cycle-diagnostics-v1", "scope": "Synthetic raw + worker + HDF5 + shown plot",
              "cycles_completed": cycles, "warmup_cycles": warmup_cycles, "measured_cycles": len(measured),
              "points": points, "frames_per_cycle": frames, "warmup_frames_per_cycle": warmup_frames,
              "requested_rate_hz": rate_hz, "elapsed_s": time.perf_counter() - started,
              "total_frames_committed": sum(row["frames_committed"] for row in rows),
              "total_lost_frames": sum(row["lost_frames"] for row in rows),
              "total_archive_bytes": sum(row["archive_bytes"] for row in rows),
              "stop_submission": latency([row["stop_submission_ms"] for row in measured]),
              "stop_archive_close": latency([row["stop_archive_close_ms"] for row in measured]),
              "resources_after_cleanup": resource_summary(measured), "cycles": rows,
              "resources_before_first_cycle": initial_resources, "disk_budget": budget,
              "resource_journal": str(journal), "laboratory_qualified": False, "soak_qualified": False,
              "leak_free_qualified": False, "gui_stop_response_qualified": False,
              "limitations": ["Short repeated cycles do not replace continuous 30-minute or two-hour runs",
                              "Resource slopes are descriptive; memory allocators/caches and OS scheduling affect samples",
                              "Stop is submitted programmatically after the final frame, not an operator click during acquisition",
                              "Archive close includes final compatibility validation; this is not physical instrument shutdown",
                              "Same-process cycles are not statistically independent replications",
                              "Warmup cycle count is fixed before data acquisition; all raw archives and cycle rows are retained",
                              "Cycle reports retain frame/publication/paint samples and platform/library/environment metadata"]}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--warmup-cycles", type=int, default=2)
    parser.add_argument("--points", type=int, default=10001)
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--warmup-frames", type=int, default=5)
    parser.add_argument("--rate", default="20 Hz")
    args = parser.parse_args()
    try:
        result = benchmark_cycles(args.output, cycles=args.cycles, warmup_cycles=args.warmup_cycles,
            points=args.points, frames=args.frames, warmup_frames=args.warmup_frames,
            rate_hz=parse_quantity(args.rate, DIMENSION_FREQUENCY).si_value)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({name: result[name] for name in ("stop_submission", "stop_archive_close", "resources_after_cleanup")}, indent=2))


if __name__ == "__main__":
    main()
