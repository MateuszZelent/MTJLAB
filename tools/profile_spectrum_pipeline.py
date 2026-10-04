"""Profile pipeline call scopes in separate runs; includes overhead and overlapping work."""

from __future__ import annotations

import argparse
import cProfile
from pathlib import Path
import pstats

from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_controller import _CorrectionWorker
from app.ui.widgets import SpectrumPlotWidget
from tools.benchmark_spectrum_pipeline import benchmark_pipeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--scope", choices=["worker", "gui"], required=True)
    args = parser.parse_args()
    profile_path = args.output.with_suffix(f".{args.scope}.prof")
    if profile_path.exists():
        raise FileExistsError("Profiler outputs must be new")
    worker_profile, gui_profile = cProfile.Profile(), cProfile.Profile()
    original_execute = _CorrectionWorker._execute
    original_trace = SpectrumPlotWidget.set_trace
    original_events = QApplication.processEvents

    def execute(worker, *arguments):
        return worker_profile.runcall(original_execute, worker, *arguments)

    def set_trace(plot, *arguments, **keywords):
        return gui_profile.runcall(original_trace, plot, *arguments, **keywords)

    def events(*arguments):
        return gui_profile.runcall(original_events, *arguments)

    # Python 3.14's cProfile monitoring slot cannot host two simultaneous
    # profiles across threads. Measure one scope per independent run.
    if args.scope == "worker":
        _CorrectionWorker._execute = execute
    else:
        SpectrumPlotWidget.set_trace = set_trace
        QApplication.processEvents = staticmethod(events)
    try:
        benchmark_pipeline(args.output, frames=args.frames, warmup=10, profiling_scope=args.scope)
    finally:
        _CorrectionWorker._execute = original_execute
        SpectrumPlotWidget.set_trace = original_trace
        QApplication.processEvents = original_events
        args.output.parent.mkdir(parents=True, exist_ok=True)
        profiler = worker_profile if args.scope == "worker" else gui_profile
        profiler.dump_stats(str(profile_path))
    print(args.scope.upper())
    pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(20)


if __name__ == "__main__":
    main()
