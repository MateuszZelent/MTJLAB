"""Count GC-visible wrappers after real shell teardown; no timing qualification."""

import argparse
from collections import Counter
import gc
import json
import os
from pathlib import Path
from types import CellType, FrameType, FunctionType, MethodType
from unittest.mock import patch

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from tools import benchmark_spectrum_stop_response as stop_benchmark


def bounded_reference_paths(target, *, excluded_ids=(), max_depth=5, max_nodes=300):
    """Describe GC-visible backward paths; terminal does not prove a GC root."""
    excluded = set(excluded_ids)
    queue = [(target, [])]
    containers = [queue, queue[0]]
    excluded.add(id(containers))
    excluded.add(id(queue))
    excluded.add(id(queue[0]))
    seen, paths = set(), []
    visited = 0
    while queue and visited < max_nodes:
        node = queue.pop(0)
        current, path = node
        seen.add(id(current))
        visited += 1
        references = gc.get_referrers(current)
        containers.append(references)
        excluded.add(id(references))
        eligible = [ref for ref in references
                    if id(ref) not in excluded and id(ref) not in seen
                    and not isinstance(ref, FrameType)]
        containers.append(eligible)
        excluded.add(id(eligible))
        if not eligible:
            paths.append({"path": path, "terminal": "no eligible GC-visible referrers"})
        for ref in eligible:
            description = {"type": f"{type(ref).__module__}.{type(ref).__qualname__}"}
            if isinstance(ref, FunctionType):
                description["function"] = f"{ref.__module__}.{ref.__qualname__}:{ref.__code__.co_firstlineno}"
            elif isinstance(ref, MethodType):
                description["method"] = ref.__func__.__qualname__
            elif isinstance(ref, dict):
                description["keys"] = [str(key) for key, value in ref.items() if value is current][:10]
                if isinstance(ref.get("__name__"), str):
                    description["module"] = ref["__name__"]
            next_path = path + [description]
            if len(next_path) >= max_depth or "module" in description:
                paths.append({"path": next_path, "terminal": "module" if "module" in description else "depth limit"})
            else:
                next_node = (ref, next_path)
                containers.append(next_node)
                excluded.add(id(next_node))
                queue.append(next_node)
    return {"paths": paths[:100], "visited_nodes": visited,
            "paths_truncated": len(paths) > 100,
            "node_limit_reached": bool(queue), "max_depth": max_depth,
            "limitations": "Only GC-visible references; diagnostic containers excluded; terminal paths are not proof of an external root"}


def qt_lifetime_census():
    """Return counts only, retaining neither widgets nor the GC object list."""
    python_counts, relevant, valid, invalid = (Counter() for _ in range(4))
    objects = gc.get_objects()
    windows = []
    for obj in objects:
        kind = type(obj)
        name = f"{kind.__module__}.{kind.__qualname__}"
        python_counts[name] += 1
        if name.startswith(("app.", "pyqtgraph.", "qfluentwidgets.", "PySide6.")):
            relevant[name] += 1
        if isinstance(obj, QObject):
            (valid if isValid(obj) else invalid)[name] += 1
        if name == "app.ui.shell.main_window.MainWindow":
            windows.append(obj)
    roots = []
    for window in windows:
        descriptions = []
        for ref in gc.get_referrers(window):
            if ref is objects or ref is windows:
                continue
            entry = {"type": f"{type(ref).__module__}.{type(ref).__qualname__}"}
            if isinstance(ref, dict):
                entry["keys"] = [str(key) for key, value in ref.items() if value is window][:10]
            elif isinstance(ref, MethodType):
                entry["method"] = ref.__func__.__qualname__
            elif isinstance(ref, CellType):
                functions = []
                for closure in gc.get_referrers(ref):
                    if isinstance(closure, tuple):
                        for function in gc.get_referrers(closure):
                            if isinstance(function, FunctionType) and function.__closure__ is closure:
                                functions.append(f"{function.__module__}.{function.__qualname__}:{function.__code__.co_firstlineno}")
                entry["closure_functions"] = sorted(set(functions))
            descriptions.append(entry)
        roots.append({"native_valid": isValid(window), "referrers": descriptions,
            "backward_paths": bounded_reference_paths(window,
                excluded_ids=(id(objects), id(windows)))})
    return {"python_top_counts": dict(python_counts.most_common(40)),
        "relevant_python_counts": dict(sorted(relevant.items())),
        "qobject_valid_counts": dict(sorted(valid.items())),
        "qobject_invalid_counts": dict(sorted(invalid.items())),
        "window_referrers": roots,
        "limitations": ["GC-visible wrappers only; not a census of all native allocations",
            "Numeric NumPy arrays and many native Qt caches are not GC-tracked",
            "Counts identify retained classes, not their ownership roots or total bytes"]}


def diagnose_shell_lifetimes(output, *, cycles=3, points=101, frames=5,
                             experimental_overrides=()):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Lifetime diagnostic report must be new")
    benchmark_path = output.with_suffix(".benchmark.json")
    census_path = output.with_suffix(".census.jsonl")
    reserved_paths = (census_path, benchmark_path,
                      benchmark_path.with_suffix(".stop-cycles"),
                      benchmark_path.with_suffix(".stop-cycles.jsonl"))
    if any(path.exists() for path in reserved_paths):
        raise FileExistsError("Lifetime diagnostic artifacts must be new")
    snapshots = []
    memory_snapshot = stop_benchmark.process_memory

    def observe_memory():
        memory = memory_snapshot()
        application = QApplication.instance()
        # Before-cycle snapshots have an active host. Only observe the outer
        # post-cycle snapshot, after local closures and DeferredDelete/GC drain.
        if application is not None and not application.allWidgets():
            snapshot = {"cycle": len(snapshots), "memory": memory,
                        "census": qt_lifetime_census()}
            census_stream.write(json.dumps(snapshot, allow_nan=False) + "\n")
            census_stream.flush()
            os.fsync(census_stream.fileno())
            snapshots.append(snapshot)
        return memory

    output.parent.mkdir(parents=True, exist_ok=True)
    with census_path.open("x", encoding="utf-8") as census_stream:
        census_stream.write(json.dumps({"type": "incomplete_qt_lifetime_census",
            "requested_cycles": cycles,
            "experimental_overrides": list(experimental_overrides)}, allow_nan=False) + "\n")
        census_stream.flush()
        os.fsync(census_stream.fileno())
        with patch.object(stop_benchmark, "process_memory", observe_memory):
            benchmark = stop_benchmark.benchmark_stop_response(benchmark_path,
                cycles=cycles, warmup_cycles=0, points=points, frames_before_stop=frames,
                host_mode="shell")
    if len(snapshots) != cycles:
        raise RuntimeError("Expected exactly one census after every empty-shell cleanup")
    report = {"algorithm": "qt-wrapper-lifetime-census-v1", "benchmark": str(benchmark_path),
        "census_journal": str(census_path),
        "scope": "Same-process synthetic shell lifecycles, with post-cleanup GC instrumentation",
        "timing_qualified": False, "laboratory_qualified": False,
        "experimental_overrides": list(experimental_overrides),
        "cycles": snapshots, "frames_committed": benchmark["total_frames_committed"],
        "lost_frames": benchmark["total_lost_frames"]}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--points", type=int, default=101)
    parser.add_argument("--frames", type=int, default=5)
    args = parser.parse_args()
    try:
        report = diagnose_shell_lifetimes(args.output, cycles=args.cycles,
                                        points=args.points, frames=args.frames)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"cycles": len(report["cycles"]), "lost_frames": report["lost_frames"],
        "invalid_qobjects": [sum(row["census"]["qobject_invalid_counts"].values())
                             for row in report["cycles"]]}, indent=2))


if __name__ == "__main__":
    main()
