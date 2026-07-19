"""Execution monitoring page independent of device UI."""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, PrimaryPushButton, ProgressBar, PushButton, StrongBodyLabel

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
        self.heartbeat = QLabel("Heartbeat: —")
        self.heartbeat.setObjectName("muted")
        self.eta = QLabel("ETA: —")
        self.eta.setObjectName("muted")
        self.monitor_card = CardWidget(self)
        monitor_layout = QVBoxLayout(self.monitor_card)
        monitor_layout.setContentsMargins(20, 16, 20, 16)
        monitor_layout.setSpacing(10)
        telemetry = QGridLayout()
        self.current_path = QLabel("Current node: —")
        self.current_path.setWordWrap(True)
        self.current_setpoints = QLabel("Setpoints (SI): —")
        self.current_setpoints.setWordWrap(True)
        self.current_measurements = QLabel("Measurements (SI): —")
        self.current_measurements.setWordWrap(True)
        self.storage_rate = QLabel("Storage: —")
        self.storage_rate.setWordWrap(True)
        telemetry.addWidget(self.current_path, 0, 0)
        telemetry.addWidget(self.storage_rate, 0, 1)
        telemetry.addWidget(self.current_setpoints, 1, 0)
        telemetry.addWidget(self.current_measurements, 1, 1)
        self.progress = ProgressBar(self.monitor_card)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        controls = QHBoxLayout()
        self.pause_button = PushButton("Pause after point", self.monitor_card)
        self.resume_button = PushButton("Resume", self.monitor_card)
        self.stop_button = PrimaryPushButton("Stop safely", self.monitor_card)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.resume_button)
        controls.addStretch(1)
        controls.addWidget(self.stop_button)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setProperty("stationSurface", "raised")
        self.warnings = QPlainTextEdit()
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
        monitor_splitter.addWidget(self.events)
        monitor_splitter.addWidget(self.spectrum_preview)
        monitor_splitter.setStretchFactor(0, 1)
        monitor_splitter.setStretchFactor(1, 2)
        layout.addWidget(self.hero_card)
        monitor_layout.addWidget(self.heartbeat)
        monitor_layout.addWidget(self.eta)
        monitor_layout.addLayout(telemetry)
        monitor_layout.addWidget(self.progress)
        monitor_layout.addLayout(controls)
        layout.addWidget(self.monitor_card)
        layout.addWidget(self.warnings)
        layout.addWidget(monitor_splitter, 1)
        self.pause_button.clicked.connect(self.pause_requested)
        self.resume_button.clicked.connect(self.resume_requested)
        self.stop_button.clicked.connect(self.stop_requested)
        self._eta_started = 0.0
        self._model_duration_s = 0.0
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._update_eta)

    def run_started(self, actions: int, estimated_duration_s: float = 0.0) -> None:
        self.state.setText("RUNNING")
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.events.clear()
        self.warnings.clear()
        self.spectrum_preview.clear()
        self.current_path.setText("Current node: waiting for first action")
        self.current_setpoints.setText("Setpoints (SI): —")
        self.current_measurements.setText("Measurements (SI): —")
        self.storage_rate.setText("Storage: waiting for first checkpoint")
        self.heartbeat.setText("Heartbeat: waiting for first operation")
        self._eta_started = time.monotonic()
        self._model_duration_s = max(0.0, estimated_duration_s)
        self._eta_timer.start()
        self._update_eta()

    def _update_eta(self) -> None:
        if not self._eta_started:
            self.eta.setText("ETA: —")
            return
        elapsed = max(0.0, time.monotonic() - self._eta_started)
        completed = self.progress.value()
        total = max(1, self.progress.maximum())
        empirical_remaining = (
            elapsed / completed * max(0, total - completed)
            if completed
            else self._model_duration_s
        )
        model_remaining = max(0.0, self._model_duration_s - elapsed)
        remaining = max(empirical_remaining, model_remaining)
        self.eta.setText(
            f"Elapsed: {_human_duration(elapsed)} • "
            f"estimated remaining: {_human_duration(remaining)}"
        )

    def update_heartbeat(self, data: dict[str, object]) -> None:
        self.heartbeat.setText(
            "Heartbeat: "
            f"{data.get('kind', 'operation')} • attempt {data.get('attempt', '—')} • "
            f"elapsed {float(data.get('elapsed_s', 0.0)):.2f} s • "
            f"remaining {float(data.get('remaining_s', 0.0)):.2f} s"
        )

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name == "action_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
        elif name == "action_started":
            self.current_path.setText(
                f"Current node: {data.get('node_id', '—')} • {data.get('kind', '—')} • "
                f"action {int(data.get('action_index', 0)) + 1}/{self.progress.maximum()}"
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
        if name == "pause_pending":
            self.state.setText("PAUSED")
        elif name == "run_fault":
            self.state.setText("FAULT")
        elif name == "watchdog_timeout":
            self.state.setText("FAULT • WATCHDOG TIMEOUT")
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
        run_result = result["result"]
        self.state.setText(f"{run_result.state.value.upper()} • {run_result.stored_points} points")
        self.events.appendPlainText(f"File: {result['path']}")

    def failed(self, error: str) -> None:
        self._eta_timer.stop()
        self.state.setText("FAULT")
        self.events.appendPlainText(error)
