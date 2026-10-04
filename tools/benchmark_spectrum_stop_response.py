"""Queued synthetic mouse click during HDF5 commit; real workspace, no VISA."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import time
from unittest.mock import patch

import numpy as np
import h5py
from PySide6.QtCore import QEvent, QObject, QSettings, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.correction_controller import CorrectionSessionRequest
from app.domain.quantities import DIMENSION_FREQUENCY, parse_quantity
from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext, SweepEvidence
from app.settings import SettingsRepository
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.hdf5_writer import Hdf5RunWriter
from tools.benchmark_spectrum_cycles import drain_deleted_objects
from tools.benchmark_spectrum_pipeline import latency, process_memory
from tools.spectrum_benchmark_resources import archive_disk_budget, process_resources


class StopProbe(QObject):
    requested = Signal(float)

    def __init__(self, workspace, state, stop_button, feedback_widget):
        super().__init__(workspace)
        self.workspace, self.state = workspace, state
        self.stop_button = stop_button
        self.requested.connect(self.click, Qt.ConnectionType.QueuedConnection)
        feedback_widget.installEventFilter(self)

    @Slot(float)
    def click(self, posted_s):
        state, workspace = self.state, self.workspace
        try:
            state["posted_s"] = posted_s
            state["mouse_dispatch_s"] = time.perf_counter()
            state["commit_active_at_mouse_dispatch"] = state["commit_active"]
            if not workspace.running or not self.stop_button.isEnabled() or not self.stop_button.isVisible():
                raise RuntimeError("Stop is unavailable during the prepared active session")
            QTest.mouseClick(self.stop_button, Qt.MouseButton.LeftButton)
            state["handler_return_s"] = time.perf_counter()
            if not workspace._stopping or workspace.stop.isEnabled():
                raise RuntimeError("Stop did not acknowledge the click synchronously")
        except Exception as exc:
            state["errors"].append(str(exc))

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        if (event.type() == QEvent.Type.Paint and self.state["handler_return_s"] is not None
                and self.state["feedback_paint_entry_s"] is None
                and watched.text().startswith("Stopping recording")):
            self.state["feedback_paint_entry_s"] = time.perf_counter()
            # A zero timer runs after the paint event returns. Measuring this
            # barrier includes painting and any intervening event-loop delay,
            # instead of qualifying mere entry into the paint handler.
            QTimer.singleShot(0, self.feedback_painted)
        return super().eventFilter(watched, event)

    @Slot()
    def feedback_painted(self):
        self.state["first_feedback_paint_s"] = time.perf_counter()


def _stop_cycle(application, settings, directory, *, index, points, frames_before_stop, rate_hz, host_mode):
    archive = directory / f"cycle-{index:03d}.h5"
    screenshot = directory / f"cycle-{index:03d}-stop.png"
    closed_screenshot = directory / f"cycle-{index:03d}-closed.png"
    base_wall_s = time.time()
    context = SpectrumAcquisitionContext(np.linspace(1e6, 6e9, points), "prepared-synthetic-stop-benchmark")
    baseline = np.full(points, 1e-9)
    profile = BackgroundProfile("synthetic-background", context.context_id, baseline, np.zeros(points),
        None, 100, base_wall_s - 10, base_wall_s - 1, "synthetic only; no laboratory qualification")
    rng = np.random.default_rng(20261015 + index)
    signal = baseline.copy()
    signal[points // 2:points // 2 + 2] += [1e-10, -1e-10]
    frequencies = tuple(context.frequencies_hz)
    isolation = ExitStack()
    if host_mode == "shell":
        from app.ui.shell import MainWindow
        from app.devices.anritsu_ms2830a.ui.page import AnritsuPageState

        cycle_directory = directory / f"cycle-{index:03d}-shell"
        isolated_settings = settings.model_copy(update={
            "storage": {**settings.storage, "output_directory": str((cycle_directory / "measurements").resolve()),
                        "catalogue_directory": str((cycle_directory / "catalogue").resolve())},
            "application": {**settings.application,
                "audit_log_directory": str((cycle_directory / "logs").resolve()), "restore_last_recipe": False},
        })
        settings_path = cycle_directory / "settings.yml"
        SettingsRepository(settings_path).save(isolated_settings)
        # Only persistence is redirected. The actual Fluent window and page
        # tree remain intact; no UI facade or controller adapter is inserted.
        isolation.enter_context(patch("app.ui.shell.main_window.QSettings",
            lambda *_args: QSettings(str(cycle_directory / "ui-state.ini"), QSettings.Format.IniFormat)))
        host = MainWindow(settings_path, simulation=True)
        page = host.anritsu_page
        workspace = page.correction_workspace
        workspace.request_device.disconnect(page._request_correction_device)
        page._set_page_state(AnritsuPageState.IDLE)
        host.resize(1500, 950)
        host._navigate_to("anritsu")
        page.analysis_tabs.setCurrentIndex(0)
        page._open_recording_setup()
        page.recording_tabs.setCurrentIndex(1)
        stop_button, feedback_widget = workspace.stop, workspace.recording_title
    else:
        workspace = SpectrumCorrectionWorkspace(settings, single_sweep_available=True, simulation_mode=True)
        host = workspace
        host.resize(1000, 800)
        stop_button, feedback_widget = workspace.stop, workspace.recording_title
    host.show()
    application.processEvents()
    state = {"started_s": None, "posted_s": None, "mouse_dispatch_s": None, "handler_return_s": None,
        "first_feedback_paint_s": None, "feedback_paint_entry_s": None, "closed_s": None, "commit_active": False,
        "commit_active_at_mouse_dispatch": None, "submitted": 0, "pending_request": False,
        "queue_max": 0, "errors": [], "feedback_screenshot": None}
    probe = StopProbe(workspace, state, stop_button, feedback_widget)
    producer = QTimer(workspace)
    producer.setTimerType(Qt.TimerType.PreciseTimer)
    producer.setInterval(1)
    workspace._context, workspace._profile = context, profile
    workspace._kind, workspace._archive_path = "signal", archive
    workspace._running = True
    workspace.set_available(False, device_idn="SYNTHETIC;NO_VISA")
    workspace.recording_activity.show()
    workspace.recording_activity.start()

    def request(operation, _payload):
        if operation != "single_sweep" or state["pending_request"]:
            state["errors"].append(f"Unexpected synthetic request: {operation}")
        else:
            state["pending_request"] = True

    def produce():
        try:
            if not state["pending_request"] or state["started_s"] is None:
                return
            frame_index = state["submitted"]
            if time.perf_counter() < state["started_s"] + frame_index / rate_hz:
                return
            if frame_index >= frames_before_stop + 1:
                raise RuntimeError("Stop did not bound the synthetic producer")
            state["pending_request"] = False
            watts = signal * rng.lognormal(mean=-0.03 ** 2 / 2, sigma=0.03, size=points)
            trace = SpectrumTrace(frequencies, tuple(10 * np.log10(watts) + 30),
                datetime.fromtimestamp(base_wall_s + frame_index / rate_hz, timezone.utc), "TRAC1",
                sweep_evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP, sweep_id=f"synthetic:{frame_index}")
            state["submitted"] += 1
            if not workspace.handle_result("single_sweep", trace):
                raise RuntimeError("Workspace rejected the synthetic instrument-response route")
        except Exception as exc:
            state["errors"].append(str(exc))

    def completed(operation, _payload):
        if operation == "start":
            state["started_s"] = time.perf_counter()
            workspace._request("single_sweep")
            producer.start()
        elif operation == "stop":
            state["closed_s"] = time.perf_counter()
            producer.stop()

    original_append = Hdf5RunWriter.append

    def append_with_probe(writer, *args, **kwargs):
        state["commit_active"] = True
        try:
            if writer.point_count + 1 == frames_before_stop:
                # This signal crosses from the archive worker to the GUI. No
                # artificial delay or nested GUI event processing is inserted.
                probe.requested.emit(time.perf_counter())
            return original_append(writer, *args, **kwargs)
        finally:
            state["commit_active"] = False

    workspace.request_device.connect(request)
    workspace._cpu.completed.connect(completed)
    workspace._cpu.failed.connect(lambda *args: state["errors"].append(str(args)))
    producer.timeout.connect(produce)
    before_resources = {**process_memory(), **process_resources()}
    try:
        with patch.object(Hdf5RunWriter, "append", append_with_probe):
            workspace._cpu.start_session(CorrectionSessionRequest(context, workspace._processor_config, archive,
                settings.model_dump_json(), "SYNTHETIC;NO_VISA", profile=profile, simulation_mode=True))
            deadline = time.monotonic() + 120 + 2 * frames_before_stop / rate_hz
            while state["closed_s"] is None and not state["errors"]:
                application.processEvents()
                state["queue_max"] = max(state["queue_max"], workspace._cpu.queued_count)
                if (state["first_feedback_paint_s"] is not None and state["feedback_screenshot"] is None
                        and feedback_widget.text().startswith("Stopping recording")):
                    if not host.grab().save(str(screenshot)):
                        raise OSError("Could not save the Stop feedback screenshot")
                    state["feedback_screenshot"] = str(screenshot)
                if time.monotonic() >= deadline:
                    raise TimeoutError("GUI Stop benchmark did not close its archive")
                time.sleep(0.0005)
        if state["errors"]:
            raise RuntimeError(f"GUI Stop benchmark failed: {state['errors']}")
        if state["first_feedback_paint_s"] is None:
            raise RuntimeError("No natural Stop feedback paint was observed before archive close")
        summary = Hdf5RunReader.summary(archive)
        if summary.status != "aborted" or summary.point_count != state["submitted"]:
            raise RuntimeError("Stop lost an accepted raw checkpoint or has inconsistent close status")
        workspace._render()
        if not host.grab().save(str(closed_screenshot)):
            raise OSError("Could not save the closed archive screenshot")
        return {"cycle": index, "host_mode": host_mode,
            "stop_button_visible": stop_button.isVisible(), "stop_button_width": stop_button.width(),
            "frames_submitted": state["submitted"], "frames_committed": summary.point_count,
            "lost_frames": state["submitted"] - summary.point_count, "status": summary.status,
            "request_posted_during_commit": True,
            "commit_active_at_mouse_dispatch": state["commit_active_at_mouse_dispatch"],
            "post_to_mouse_dispatch_ms": (state["mouse_dispatch_s"] - state["posted_s"]) * 1000,
            "mouse_handler_ms": (state["handler_return_s"] - state["mouse_dispatch_s"]) * 1000,
            "post_to_feedback_paint_ms": (state["first_feedback_paint_s"] - state["posted_s"]) * 1000,
            "post_to_feedback_paint_entry_ms": (state["feedback_paint_entry_s"] - state["posted_s"]) * 1000,
            "post_to_archive_close_ms": (state["closed_s"] - state["posted_s"]) * 1000,
            "queue_max_observed": state["queue_max"], "archive": str(archive), "archive_bytes": archive.stat().st_size,
            "feedback_screenshot": state["feedback_screenshot"], "closed_screenshot": str(closed_screenshot),
            "resources_before": before_resources}
    finally:
        producer.stop()
        workspace._render_timer.stop()
        workspace._running = False
        if not workspace._cpu.close(wait_ms=5000):
            raise RuntimeError("GUI Stop benchmark worker did not terminate")
        if not host.close():
            raise RuntimeError("GUI Stop benchmark host refused shutdown")
        host.deleteLater()
        drain_deleted_objects(application)
        isolation.close()


def benchmark_stop_response(output, *, cycles=20, warmup_cycles=2, points=10001, frames_before_stop=15, rate_hz=20.0,
                            host_mode="workspace"):
    for name, value, lower, upper in (("cycles", cycles, 1, 100), ("warmup_cycles", warmup_cycles, 0, 99),
            ("points", points, 3, 10001), ("frames_before_stop", frames_before_stop, 3, 1000)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
    if warmup_cycles >= cycles or not np.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError("Require measured cycles and a finite positive rate")
    if host_mode not in {"workspace", "shell"}:
        raise ValueError("Require workspace or shell host_mode")
    output = Path(output)
    directory, journal = output.with_suffix(".stop-cycles"), output.with_suffix(".stop-cycles.jsonl")
    if any(path.exists() for path in (output, directory, journal)):
        raise FileExistsError("All GUI Stop benchmark paths must be new")
    budget = archive_disk_budget(output.parent, points=points, frames=cycles * (frames_before_stop + 1))
    directory.mkdir(parents=True, exist_ok=False)
    application = QApplication.instance() or QApplication([])
    if Path("C:/Windows/Fonts/segoeui.ttf").is_file():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        application.setFont(QFont("Segoe UI", 10))
    settings = SettingsRepository(Path(__file__).resolve().parents[1] / "app/resources/settings.template.yml").load().settings
    rows = []
    with journal.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "incomplete_gui_stop_journal", "cycles": cycles,
            "warmup_cycles": warmup_cycles, "points": points, "frames_before_stop": frames_before_stop,
            "host_mode": host_mode,
            "rate_hz": rate_hz, "disk_budget": budget}) + "\n")
        stream.flush()
        for index in range(cycles):
            row = _stop_cycle(application, settings, directory, index=index, points=points,
                frames_before_stop=frames_before_stop, rate_hz=rate_hz, host_mode=host_mode)
            # Cycle-local Python closures still hold the host during its own
            # finally block. Collect again after that frame has returned.
            drain_deleted_objects(application)
            row["warmup_cycle"] = index < warmup_cycles
            row["qt_widgets_after_cleanup"] = len(application.allWidgets())
            row["qt_top_level_widgets_after_cleanup"] = len(application.topLevelWidgets())
            row["qt_top_level_classes_after_cleanup"] = dict(Counter(
                widget.metaObject().className() for widget in application.topLevelWidgets()))
            row["resources_after_cleanup"] = {**process_memory(), **process_resources()}
            rows.append(row)
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            print(f"GUI Stop cycles {index + 1}/{cycles}; committed {row['frames_committed']}", flush=True)
    measured = rows[warmup_cycles:]
    response = latency([row["post_to_feedback_paint_ms"] for row in measured])
    report = {"algorithm": "synthetic-workspace-stop-response-v1", "scope": "Prepared synthetic session; no VISA or hardware preflight",
        "python": platform.python_version(), "numpy": np.__version__, "platform": platform.platform(),
        "cpu_identifier": platform.processor(), "logical_cpu_count": os.cpu_count(),
        "hdf5": h5py.version.hdf5_version, "qt_platform": application.platformName(),
        "processor_config": asdict(settings.anritsu.spectrum_correction.processor_config()),
        "blas_thread_environment": {key: os.environ.get(key) for key in (
            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")},
        "cycles_completed": cycles, "warmup_cycles": warmup_cycles, "points": points, "requested_rate_hz": rate_hz,
        "host_mode": host_mode,
        "frames_before_stop": frames_before_stop, "cycles": rows, "disk_budget": budget, "journal": str(journal),
        "total_frames_committed": sum(row["frames_committed"] for row in rows),
        "total_lost_frames": sum(row["lost_frames"] for row in rows),
        "post_to_feedback_paint": response,
        "post_to_mouse_dispatch": latency([row["post_to_mouse_dispatch_ms"] for row in measured]),
        "mouse_handler": latency([row["mouse_handler_ms"] for row in measured]),
        "post_to_archive_close": latency([row["post_to_archive_close_ms"] for row in measured]),
        "synthetic_response_p95_100ms_gate": response["p95_ms"] <= 100,
        "laboratory_qualified": False, "hardware_shutdown_qualified": False, "operator_input_qualified": False,
        "limitations": ["Queued QTest mouse click is synthetic, not physical mouse/input/display latency",
            "Stop request is posted at checkpoint entry; commit may finish before GUI dispatch",
            "Prepared session bypasses instrument configuration/preflight; no hardware commands or off confirmation",
            "Paint response is a next-event-loop barrier after the observed Stop paint; includes scheduling, not physical display presentation",
            "Feedback screenshot is saved after measuring GUI response and may add overhead to the archive-close interval",
            "Short repeated cycles are not 30-minute or two-hour soak and are not independent replications"]}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--warmup-cycles", type=int, default=2)
    parser.add_argument("--points", type=int, default=10001)
    parser.add_argument("--frames-before-stop", type=int, default=15)
    parser.add_argument("--rate", default="20 Hz")
    parser.add_argument("--host", choices=("workspace", "shell"), default="workspace")
    args = parser.parse_args()
    try:
        report = benchmark_stop_response(args.output, cycles=args.cycles, warmup_cycles=args.warmup_cycles,
            points=args.points, frames_before_stop=args.frames_before_stop,
            host_mode=args.host,
            rate_hz=parse_quantity(args.rate, DIMENSION_FREQUENCY).si_value)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({key: report[key] for key in ("post_to_feedback_paint", "post_to_archive_close",
        "synthetic_response_p95_100ms_gate")}, indent=2))


if __name__ == "__main__":
    main()
