"""Execution monitoring page independent of device UI."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QSplitter,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    TreeWidget,
)

from app.ui.common import human_duration as _human_duration
from app.ui.widgets import SpectrumPlotWidget


class RunMonitorPage(QWidget):
    stop_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        self.hero_card = CardWidget(self)
        hero_layout = QHBoxLayout(self.hero_card)
        hero_layout.setContentsMargins(20, 16, 20, 16)
        title = StrongBodyLabel("Recipe execution", self.hero_card)
        title.setObjectName("pageTitle")
        hero_layout.addWidget(title, 1)
        self.state = StrongBodyLabel("IDLE", self.hero_card)
        self.state.setObjectName("readout")
        self.state.setProperty("deviceState", "compliance")
        hero_layout.addWidget(self.state)
        self.heartbeat = BodyLabel("Heartbeat: —")
        self.heartbeat.setObjectName("muted")
        self.eta = BodyLabel("ETA: —")
        self.eta.setObjectName("muted")
        self.total_estimate = BodyLabel("Plan estimate: —")
        self.total_estimate.setObjectName("muted")
        self.monitor_card = CardWidget(self)
        monitor_layout = QVBoxLayout(self.monitor_card)
        monitor_layout.setContentsMargins(20, 16, 20, 16)
        monitor_layout.setSpacing(10)
        telemetry = QGridLayout()
        self.current_path = BodyLabel("Current node: —")
        self.current_path.setWordWrap(True)
        self.current_setpoints = BodyLabel("Setpoints (SI): —")
        self.current_setpoints.setWordWrap(True)
        self.current_measurements = BodyLabel("Measurements (SI): —")
        self.current_measurements.setWordWrap(True)
        self.storage_rate = BodyLabel("Storage: —")
        self.storage_rate.setWordWrap(True)
        telemetry.addWidget(self.current_path, 0, 0)
        telemetry.addWidget(self.storage_rate, 0, 1)
        telemetry.addWidget(self.current_setpoints, 1, 0)
        telemetry.addWidget(self.current_measurements, 1, 1)
        self.progress = ProgressBar(self.monitor_card)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        progress_header = QHBoxLayout()
        self.progress_summary = StrongBodyLabel("0 of 0 actions • 0%", self.monitor_card)
        self.progress_summary.setObjectName("progressSummary")
        progress_header.addWidget(self.progress_summary)
        progress_header.addStretch(1)
        controls = QHBoxLayout()
        self.pause_button = PushButton("Pause after point", self.monitor_card)
        self.resume_button = PushButton("Resume", self.monitor_card)
        self.stop_button = PrimaryPushButton("Stop safely", self.monitor_card)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.resume_button)
        controls.addStretch(1)
        controls.addWidget(self.stop_button)
        self.events = PlainTextEdit(self)
        self.events.setReadOnly(True)
        self.events.setProperty("stationSurface", "raised")
        self.steps = TreeWidget(self)
        self.steps.setObjectName("executionSteps")
        self.steps.setHeaderLabels(("Procedure step", "Action", "Status"))
        self.steps.setRootIsDecorated(False)
        self.steps.setAlternatingRowColors(True)
        self.steps.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.steps.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.steps.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.steps.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.steps.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.steps.setMinimumHeight(170)
        self.warnings = PlainTextEdit(self)
        self.warnings.setReadOnly(True)
        self.warnings.setProperty("stationSurface", "raised")
        self.warnings.setMaximumHeight(95)
        self.warnings.setPlaceholderText("No run warnings.")
        self.spectrum_preview = SpectrumPlotWidget(legend=False)
        self.spectrum_preview.set_labels(
            x="Frequency",
            x_unit="Hz",
            y="Amplitude",
            y_unit="dBm",
        )
        self.spectrum_preview.set_title("Latest stored spectrum checkpoint")
        monitor_splitter = QSplitter(Qt.Orientation.Horizontal)
        activity_splitter = QSplitter(Qt.Orientation.Vertical)
        activity_splitter.addWidget(self.steps)
        activity_splitter.addWidget(self.events)
        activity_splitter.setStretchFactor(0, 2)
        activity_splitter.setStretchFactor(1, 1)
        monitor_splitter.addWidget(activity_splitter)
        monitor_splitter.addWidget(self.spectrum_preview)
        monitor_splitter.setStretchFactor(0, 1)
        monitor_splitter.setStretchFactor(1, 2)
        layout.addWidget(self.hero_card)
        monitor_layout.addWidget(self.heartbeat)
        monitor_layout.addWidget(self.eta)
        monitor_layout.addWidget(self.total_estimate)
        monitor_layout.addLayout(telemetry)
        monitor_layout.addLayout(progress_header)
        monitor_layout.addWidget(self.progress)
        monitor_layout.addLayout(controls)
        layout.addWidget(self.monitor_card)
        layout.addWidget(self.warnings)
        layout.addWidget(monitor_splitter, 1)
        self.pause_button.clicked.connect(self.pause_requested)
        self.resume_button.clicked.connect(self.resume_requested)
        self.stop_button.clicked.connect(self._request_safe_stop)
        self._eta_started = 0.0
        self._model_duration_s = 0.0
        self._planned_actions = 0
        self._step_items: dict[str, QTreeWidgetItem] = {}
        self._step_totals: dict[str, int] = {}
        self._step_completed: dict[str, int] = {}
        self._active_step: QTreeWidgetItem | None = None
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._update_eta)

    def run_started(
        self,
        actions: int,
        estimated_duration_s: float = 0.0,
        *,
        plan_actions: object = (),
        execution_mode: str = "measurement",
    ) -> None:
        demo = execution_mode == "demo_outputs_off"
        self.state.setText("DEMO — OUTPUTS OFF" if demo else "RUNNING")
        self.state.setToolTip(
            (
                "Configurations, setpoints and acquisitions are executing while "
                "every source output is forced OFF."
            )
            if demo
            else "Normal measurement execution; recipe OUTPUT actions are active."
        )
        self._planned_actions = max(0, actions)
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.stop_button.setText("Stop safely")
        self.stop_button.setEnabled(True)
        self.pause_button.setEnabled(True)
        self.resume_button.setEnabled(False)
        self.events.clear()
        self._build_step_list(plan_actions)
        self.warnings.clear()
        self.spectrum_preview.clear()
        self.current_path.setText("Current node: waiting for first action")
        self.current_setpoints.setText("Setpoints (SI): —")
        self.current_measurements.setText("Measurements (SI): —")
        self.storage_rate.setText("Storage: waiting for first checkpoint")
        self.heartbeat.setText("Heartbeat: waiting for first operation")
        self._eta_started = time.monotonic()
        self._model_duration_s = max(0.0, estimated_duration_s)
        expected_finish = datetime.now().astimezone() + timedelta(
            seconds=self._model_duration_s
        )
        self.total_estimate.setText(
            f"Plan estimate: {_human_duration(self._model_duration_s)} • "
            f"expected finish: {expected_finish:%H:%M:%S}"
        )
        self._eta_timer.start()
        self._update_eta()

    def _request_safe_stop(self) -> None:
        if not self.stop_button.isEnabled():
            return
        self.state.setText("STOPPING")
        self.stop_button.setText("Stopping safely…")
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.stop_requested.emit()

    def _build_step_list(self, plan_actions: object) -> None:
        self.steps.clear()
        self._step_items.clear()
        self._step_totals.clear()
        self._step_completed.clear()
        self._active_step = None
        if not isinstance(plan_actions, (tuple, list)):
            return
        ordered: list[tuple[str, str, bool]] = []
        for action in plan_actions:
            node_id = str(getattr(action, "node_id", ""))
            kind = str(getattr(action, "kind", ""))
            if not node_id:
                continue
            self._step_totals[node_id] = self._step_totals.get(node_id, 0) + 1
            if node_id not in self._step_items:
                ordered.append((node_id, kind, bool(getattr(action, "is_finally", False))))
                item = QTreeWidgetItem()
                self._step_items[node_id] = item
        for node_id, kind, is_finally in ordered:
            label = node_id.replace("-", " ").replace("_", " ")
            if is_finally:
                label = "Finally · " + label
            total = self._step_totals[node_id]
            status = "○ WAITING" if total == 1 else f"○ 0/{total} WAITING"
            item = self._step_items[node_id]
            item.setText(0, label)
            item.setText(1, kind.replace("_", " "))
            item.setText(2, status)
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            self.steps.addTopLevelItem(item)

    def _step_item(self, node_id: str, kind: str) -> QTreeWidgetItem:
        item = self._step_items.get(node_id)
        if item is not None:
            return item
        item = QTreeWidgetItem(
            [node_id.replace("_", " "), kind.replace("_", " "), "○ WAITING"]
        )
        item.setData(0, Qt.ItemDataRole.UserRole, node_id)
        self.steps.addTopLevelItem(item)
        self._step_items[node_id] = item
        self._step_totals[node_id] = 1
        return item

    def _mark_step(self, node_id: str, kind: str, state: str) -> None:
        item = self._step_item(node_id, kind)
        total = self._step_totals.get(node_id, 1)
        if state == "running":
            item.setText(2, "● RUNNING")
            item.setForeground(2, QBrush(QColor("#1769aa")))
            self.steps.setCurrentItem(item)
            self.steps.scrollToItem(item)
            self._active_step = item
            return
        if state == "done":
            completed = min(total, self._step_completed.get(node_id, 0) + 1)
            self._step_completed[node_id] = completed
            item.setText(2, "✓ DONE" if total == 1 else f"✓ {completed}/{total}")
            item.setForeground(2, QBrush(QColor("#18844c")))
            self._active_step = None
            return
        item.setText(2, "✕ FAILED")
        item.setForeground(2, QBrush(QColor("#b4233a")))
        self.steps.setCurrentItem(item)
        self.steps.scrollToItem(item)
        self._active_step = item

    def _mark_shutdown(self, action: str, state: str) -> None:
        node_id = f"shutdown:{action}"
        item = self._step_items.get(node_id)
        if item is None:
            item = QTreeWidgetItem(
                ["Emergency shutdown", action.replace(".", " · "), "○ WAITING"]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            self.steps.addTopLevelItem(item)
            self._step_items[node_id] = item
            self._step_totals[node_id] = 1
        self._mark_step(node_id, action, state)

    def _update_eta(self) -> None:
        if not self._eta_started:
            self.eta.setText("ETA: —")
            self.progress_summary.setText("0 of 0 actions • 0%")
            return
        elapsed = max(0.0, time.monotonic() - self._eta_started)
        completed = self.progress.value()
        total = max(1, self.progress.maximum())
        visible_total = self._planned_actions
        percentage = (
            min(100, round(completed / visible_total * 100))
            if visible_total
            else 0
        )
        self.progress_summary.setText(
            f"{min(completed, visible_total)} of {visible_total} actions • {percentage}%"
        )
        empirical_remaining = (
            elapsed / completed * max(0, total - completed)
            if completed
            else 0.0
        )
        model_remaining = max(0.0, self._model_duration_s - elapsed)
        remaining = (
            0.0
            if visible_total and completed >= visible_total
            else max(empirical_remaining, model_remaining)
        )
        estimated_total = elapsed + remaining
        expected_finish = datetime.now().astimezone() + timedelta(seconds=remaining)
        self.eta.setText(
            f"Elapsed: {_human_duration(elapsed)} • "
            f"remaining: {_human_duration(remaining)} • "
            f"estimated total: {_human_duration(estimated_total)}"
        )
        self.total_estimate.setText(
            f"Plan estimate: {_human_duration(self._model_duration_s)} • "
            f"expected finish: {expected_finish:%H:%M:%S}"
        )

    def update_heartbeat(self, data: dict[str, object]) -> None:
        self.heartbeat.setText(
            "Heartbeat: "
            f"{data.get('kind', 'operation')} • attempt {data.get('attempt', '—')} • "
            f"elapsed {float(data.get('elapsed_s', 0.0)):.2f} s • "
            f"remaining {float(data.get('remaining_s', 0.0)):.2f} s"
        )

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name in {"action_finished", "recovery_prelude_finished"}:
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "done"
            )
            self._update_eta()
        elif name in {"action_started", "recovery_prelude_started"}:
            self._mark_step(
                str(data.get("node_id", "—")),
                str(data.get("kind", "—")),
                "running",
            )
            action_number = min(
                self.progress.value() + 1,
                self.progress.maximum(),
            )
            self.current_path.setText(
                f"Current node: {data.get('node_id', '—')} • {data.get('kind', '—')} • "
                f"action {action_number}/{self.progress.maximum()}"
            )
            self.current_setpoints.setText(
                "Setpoints (SI): " + self._format_scalars(data.get("setpoints_si"))
            )
        elif name == "point_stored":
            self.current_measurements.setText(
                "Measurements (SI): " + self._format_scalars(data.get("measurements_si"))
            )
            self.storage_rate.setText(
                f"Storage: point {data.get('stored_points', '—')} • "
                f"write {float(data.get('write_elapsed_s', 0.0)) * 1000:.1f} ms • "
                f"average {float(data.get('average_write_rate_points_per_s', 0.0)):.2f} point/s • "
                f"spectrum {data.get('spectrum_points', 0)} values"
            )
        if name == "action_failed":
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "failed"
            )
        elif name == "safe_finally_started":
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "running"
            )
        elif name == "safe_finally_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "done"
            )
            self._update_eta()
        elif name == "safe_finally_error":
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "failed"
            )
        elif name == "shutdown_action_started":
            self._mark_shutdown(str(data.get("action", "unknown")), "running")
        elif name == "shutdown_action_finished":
            self._mark_shutdown(str(data.get("action", "unknown")), "done")
        elif name == "shutdown_error":
            self._mark_shutdown(str(data.get("action", "unknown")), "failed")
        if name == "pause_pending":
            self.state.setText("PAUSED")
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(True)
        elif name in {"run_aborting", "shutdown_action_started"}:
            self.state.setText("STOPPING")
            self.stop_button.setText("Stopping safely…")
            self.stop_button.setEnabled(False)
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(False)
        elif name == "run_fault":
            self.state.setText("FAULT")
        elif name == "watchdog_timeout":
            self.state.setText("FAULT • WATCHDOG TIMEOUT")
        elif name == "run_completed":
            self.progress.setValue(self.progress.maximum())
            self._update_eta()
        if name in {
            "action_retry",
            "compliance_detected",
            "run_fault",
            "watchdog_timeout",
            "safe_finally_error",
        }:
            self.warnings.appendPlainText(f"{name}: {data}")
        self.events.appendPlainText(f"{name}: {data}")

    def update_spectrum_preview(self, data: dict[str, object]) -> None:
        frequencies = data.get("frequency_hz")
        powers = data.get("power_dbm")
        if not isinstance(frequencies, (tuple, list)) or not isinstance(powers, (tuple, list)):
            return
        self.spectrum_preview.set_trace(
            "Stored spectrum",
            frequencies,
            powers,
            primary=True,
        )
        self.spectrum_preview.set_title(
            f"Stored point {data.get('point_index', '—')} • "
            f"{data.get('source_points', len(powers))} source values"
        )

    @staticmethod
    def _format_scalars(value: object) -> str:
        if not isinstance(value, dict) or not value:
            return "—"
        return " • ".join(
            f"{key}={float(number):.6g}" for key, number in sorted(value.items())
        )

    def complete(self, result: object) -> None:
        self._eta_timer.stop()
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        run_result = result["result"]
        self.state.setText(f"{run_result.state.value.upper()} • {run_result.stored_points} points")
        self.events.appendPlainText(f"File: {result['path']}")

    def failed(self, error: str) -> None:
        self._eta_timer.stop()
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.state.setText("FAULT")
        self.events.appendPlainText(error)
