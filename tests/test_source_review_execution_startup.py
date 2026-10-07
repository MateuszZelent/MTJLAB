"""Profile the real GUI start path separately from a long measurement run."""
import time
import json
import threading
from functools import wraps

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from app.ui.shell import MainWindow

from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence
from tests.shell_test_isolation import shell_qt_application as shell_qt_application
from tests import test_execution_ui_responsiveness as responsiveness


def test_profile_execution_startup(shell_qt_application, tmp_path, monkeypatch):
    import pyqtgraph as pg
    from app.ui.measurement_tree.view import MeasurementTreeView
    from app.ui.execution.page import RunMonitorPage
    from app.ui.execution.plan_timeline import ExecutionPlanTimeline
    from app.ui.shell.main_window import MainWindow as WindowType

    timings = []
    gui_thread = threading.get_ident()
    measuring = False

    def instrument(owner, name):
        original = getattr(owner, name)
        @wraps(original)
        def measured(*args, **kwargs):
            if not measuring or threading.get_ident() != gui_thread:
                return original(*args, **kwargs)
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timings.append({"operation": owner.__name__ + "." + name,
                                "seconds": time.perf_counter() - started})
        monkeypatch.setattr(owner, name, measured)

    for owner, names in (
        (WindowType, ("_set_run_ui_locked", "_set_device_pages_execution_read_only", "_run_event", "_audit_record")),
        (RunMonitorPage, ("run_started", "_build_live_manifest")),
        (ExecutionPlanTimeline, ("set_plan",)),
        (pg.GraphicsView, ("paintEvent",)),
        (pg.ScatterPlotItem, ("paint",)),
        (pg.AxisItem, ("paint",)),
        (MeasurementTreeView, ("paintEvent",)),
    ):
        for name in names:
            instrument(owner, name)
    monkeypatch.setattr("app.ui.shell.main_window.QMessageBox.critical", lambda *_args: None)
    monkeypatch.setattr(responsiveness, "MainWindow", MainWindow)
    window = responsiveness.build_simulated_window(
        responsiveness.SIMULATED_10_BY_100_SOURCE, seed=17, spectrum_points=10001
    )
    controller = window._run_controller
    try:
        window.show()
        window._navigate_to("execution")
        for _ in range(8):
            shell_qt_application.processEvents()
            time.sleep(.01)
        plan = window._characterization_plan
        # perf_counter wrappers deliberately collect only GUI-thread work;
        # the interpreter's cProfile output mixed in worker callbacks.
        measuring = True
        shell_qt_application.execution_profile_active = True
        start_path = time.perf_counter()
        tree = window.recipe_page.semantic_tree_snapshot(plan.recipe_source, plan)
        window.run_monitor.run_started(len(plan.actions), 1., plan_actions=plan.actions,
                                       recipe_source=plan.recipe_source, semantic_tree=tree)
        controller.start(window._settings, window._repository.path, plan, simulation=True,
                         execution_mode="dry_run", output_dir_override=str(tmp_path / "startup"))
        timings.append({"operation": "synchronous_start_path", "seconds": time.perf_counter() - start_path})
        # Include initialization and the first queued engine events. An
        # immediate Stop previously bypassed the startup phase that stalls.
        QTimer.singleShot(1000, controller.request_stop)
        deadline = time.monotonic() + 30
        while controller.running and time.monotonic() < deadline:
            event_start = time.perf_counter()
            QApplication.processEvents()
            timings.append({"operation": "processEvents", "seconds": time.perf_counter() - event_start})
            time.sleep(.005)
        measuring = False
        shell_qt_application.execution_profile_active = False
        timings.sort(key=lambda entry: entry["seconds"], reverse=True)
        (tmp_path / "startup-gui-timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
        print(json.dumps(timings[:20], indent=2))
        deadline = time.monotonic() + 30
        while controller.running and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(.005)
        assert not controller.running
    finally:
        shell_qt_application.execution_profile_active = False
        window.close()
