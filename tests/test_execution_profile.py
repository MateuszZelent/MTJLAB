"""Short, isolated end-to-end GUI workload with profiling evidence."""
import cProfile
import pstats
import json
import time
import os
import threading
import pytest
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence, shell_qt_application  # noqa: F401
from tests import test_execution_ui_responsiveness as workload


@pytest.mark.qualification
def test_averaged_reference_and_spectrum_via_device_threads(tmp_path, monkeypatch):
    """Exercise the connected-controller path absent from direct-adapter stress tests."""
    from app.devices.anritsu_ms2830a.adapter import AnritsuAdapter
    import h5py

    monkeypatch.setattr(workload, "MainWindow", MainWindow)
    source = tmp_path / "averaged.yml"
    text = workload.SIMULATED_10_BY_100_SOURCE.read_text(encoding="utf-8")
    text = text.replace("      points: 10\n", "      points: 2\n").replace("          points: 100\n", "          points: 2\n")
    text = text.replace("type: acquire_reference\n", "type: acquire_reference\n      average_count: 32\n")
    text = text.replace("type: acquire_spectrum\n", "type: acquire_spectrum\n              average_count: 32\n")
    source.write_text(text, encoding="utf-8")
    original_acquire = AnritsuAdapter.acquire_single_sweep
    owners = []
    def delayed_acquire(adapter, *args, **kwargs):
        owners.append(threading.get_ident())
        time.sleep(.02)  # deterministic transport wait; never in Qt's GUI thread
        return original_acquire(adapter, *args, **kwargs)
    monkeypatch.setattr(AnritsuAdapter, "acquire_single_sweep", delayed_acquire)
    window = workload.build_simulated_window(source, seed=17, spectrum_points=10001)
    original_start = window._run_controller.start
    def start(*args, **kwargs):
        kwargs["device_controllers"] = {name: window._controllers[name] for name in ("anritsu", "rigol", "keithley")}
        return original_start(*args, **kwargs)
    monkeypatch.setattr(window._run_controller, "start", start)
    phase = ""
    gaps = {"acquire_reference": [], "acquire_spectrum": []}
    previous = time.perf_counter()
    def event(name, data):
        nonlocal phase
        if name == "semantic_operation_started":
            phase = data.get("kind", "")
    window._run_controller.event.connect(event)
    def tick():
        nonlocal previous
        now = time.perf_counter()
        if phase in gaps:
            gaps[phase].append(now - previous)
        previous = now
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(tick)
    try:
        window.show()
        window._navigate_to("execution")
        QApplication.processEvents()
        timer.start()
        result = workload.start_and_wait_for_run(window, expected_points=4)
        timer.stop()
        evidence = {kind: {"ticks": len(values), "max_gap_s": max(values, default=0)} for kind, values in gaps.items()}
        output = Path("scratch/execution-profile")
        output.mkdir(parents=True, exist_ok=True)
        (output / "averaged-acquisition.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        assert len(owners) == 160
        assert all(owner != threading.get_ident() for owner in owners)
        with h5py.File(result, "r") as run:
            assert len(run["spectra"]) == 4
            assert len(run["recipe_raw_sweeps_v1"]) == 160
            assert len(run["reference/power_dbm"]) == 10001
            assert all(len(run[f"spectra/{i}/power_dbm"]) == 10001 for i in range(4))
        for values in gaps.values():
            assert len(values) > 20
            assert max(values) < .350
    finally:
        timer.stop()
        window.close()


@pytest.mark.qualification
def test_full_plan_startup_profile(monkeypatch):
    monkeypatch.setattr(workload, "MainWindow", MainWindow)
    window = workload.build_simulated_window(workload.SIMULATED_10_BY_100_SOURCE, seed=17)
    window.show()
    window._navigate_to("execution")
    QApplication.processEvents()
    profiler = cProfile.Profile()
    try:
        plan = window._characterization_plan
        profiler.enable()
        tree = window.recipe_page.semantic_tree_snapshot(plan.recipe_source, plan)
        window.run_monitor.run_started(len(plan.actions), plan_actions=plan.actions, semantic_tree=tree)
        QApplication.processEvents()
        profiler.disable()
        output = Path("scratch/execution-profile")
        output.mkdir(parents=True, exist_ok=True)
        assert window.run_monitor.plan_timeline.isVisibleTo(window)
        assert len(window.run_monitor.plan_timeline.actions) == len(plan.actions)
        assert window.grab().save(str(output / "execution.png"))
        with (output / "startup.txt").open("w", encoding="utf-8") as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(50)
    finally:
        profiler.disable()
        window.close()


@pytest.mark.qualification
def test_execution_gui_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(workload, "MainWindow", MainWindow)
    source = tmp_path / "profile.yml"
    source.write_text(workload.SIMULATED_10_BY_100_SOURCE.read_text(encoding="utf-8").replace("      points: 10\n", "      points: 2\n").replace("          points: 100\n", "          points: 10\n"), encoding="utf-8")
    window = workload.build_simulated_window(source, seed=17, spectrum_points=10001)
    window.show()
    window._navigate_to("execution")
    QApplication.processEvents()
    probe = workload.GuiGapProbe()
    window._run_controller.started.connect(probe.start)
    timings = []
    original = window._run_event
    window._run_controller.event.disconnect(original)
    def measured(name, data):
        started = time.perf_counter()
        original(name, data)
        timings.append((time.perf_counter() - started, name))
    window._run_controller.event.connect(measured)
    profiler = cProfile.Profile()
    profiling = os.environ.get("PYLAB_PROFILE_EXECUTION") == "1"
    if profiling:
        profiler.enable()
    try:
        workload.start_and_wait_for_run(window, expected_points=20)
    finally:
        profiler.disable()
        output = Path("scratch/execution-profile")
        output.mkdir(parents=True, exist_ok=True)
        if profiling:
            profiler.dump_stats(str(output / "gui.prof"))
            with (output / "gui.txt").open("w", encoding="utf-8") as stream:
                pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(55)
        window.close()
        probe.timer.stop()
        (output / "latency.json").write_text(json.dumps({
            "max_gui_gap_s": probe.maximum_gap_s, "ticks": probe.ticks,
            "slowest_events": sorted(timings, reverse=True)[:15],
        }, indent=2), encoding="utf-8")
