"""Guided background acquisition using the existing durable correction workflow."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    IndeterminateProgressBar,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SubtitleLabel,
)

from app.domain.spectrum_correction import CorrectionQuality
from app.ui.dialogs import StationDialog


class BackgroundCorrectionAssistant(StationDialog):
    """Keithley-OFF background collection with read-only output verification."""

    prepare_requested = Signal()
    live_ready = Signal()

    def __init__(self, workspace, directory: Path, parent=None, *, output_controller=None):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Automatic background correction")
        self.resize(620, 620)
        self.setMinimumSize(400, 380)
        self.workspace = workspace
        self.directory = directory
        self.phase = "setup"
        self._owns_recording = False
        self.output_controller = output_controller
        self._check_operation = None
        self._check_callback = None
        self._output_timeout = QTimer(self)
        self._output_timeout.setSingleShot(True)
        self._output_timeout.setInterval(15_000)
        self._output_timeout.timeout.connect(lambda: self._output_check_failed("Keithley output readback timed out."))
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
        self.reference_path = directory / f"background_{stamp}.h5"
        self.signal_path = directory / f"corrected_live_{stamp}.h5"
        root = self.modal_content_layout(spacing=12)
        self.heading = SubtitleLabel("1 · Prepare a background", self)
        root.addWidget(self.heading)
        scroll = ScrollArea(self)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        content = QWidget()
        fields = QVBoxLayout(content)
        fields.setContentsMargins(4, 4, 4, 4)
        fields.setSpacing(10)
        explanation = BodyLabel(
            "Background is recorded only with both Keithley outputs (A and B) OFF: "
            "apparatus noise without the dynamic signal. The assistant checks the outputs automatically. "
            "Keep the same cables, frequency range and analyzer settings.", content)
        explanation.setWordWrap(True)
        fields.addWidget(explanation)
        self.output_status = BodyLabel("Keithley A: UNKNOWN · B: UNKNOWN", content)
        self.output_status.setWordWrap(True)
        fields.addWidget(self.output_status)
        self.recheck = PushButton("Check Keithley outputs again", content)
        self.recheck.clicked.connect(self._check_initial_outputs)
        fields.addWidget(self.recheck)
        fields.addWidget(BodyLabel("How long should background collection take?", content))
        self.duration = ComboBox(content)
        default = workspace.duration.text()
        for value in dict.fromkeys((default, "10 s", "30 s", "60 s")):
            self.duration.addItem(value, userData=value)
        fields.addWidget(self.duration)
        minimum = workspace._settings.anritsu.spectrum_correction.calibration_min_sweeps
        minimum_note = CaptionLabel(f"At least {minimum} complete sweeps are required; collection can take longer with slow sweeps.", content)
        minimum_note.setWordWrap(True)
        fields.addWidget(minimum_note)
        fields.addWidget(BodyLabel("How smooth should the Live preview be?", content))
        self.smoothing = ComboBox(content)
        for label, value in (("Responsive · 1 s", "1 s"), ("Balanced · 5 s", "5 s"),
                             ("Smooth · 15 s (slower response)", "15 s")):
            self.smoothing.addItem(label, userData=value)
        self.smoothing.setCurrentIndex(1)
        fields.addWidget(self.smoothing)
        note = CaptionLabel(
            "Background subtraction removes repeatable background; averaging reduces random noise. "
            "A real signal remains visible. A perfectly flat line is not guaranteed. "
            "Sources and physical setpoints are controlled by you.", content)
        note.setWordWrap(True)
        fields.addWidget(note)
        archive_note = CaptionLabel(f"Raw spectra and background will be saved automatically in:\n{directory}", content)
        archive_note.setWordWrap(True)
        fields.addWidget(archive_note)
        fields.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self.form = content
        self.activity = IndeterminateProgressBar(self)
        self.activity.hide()
        root.addWidget(self.activity)
        self.status = BodyLabel("Checking that both Keithley outputs are OFF.", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        actions = QHBoxLayout()
        self.cancel = PushButton("Cancel", self)
        self.next = PrimaryPushButton("Collect background automatically", self)
        self.next.setEnabled(False)
        actions.addWidget(self.cancel)
        actions.addWidget(self.next, 1)
        root.addLayout(actions)
        self.cancel.clicked.connect(self.reject)
        self.next.clicked.connect(self._advance)
        self._ready_timer = QTimer(self)
        self._ready_timer.setSingleShot(True)
        self._ready_timer.timeout.connect(self._maybe_start_reference)
        workspace.availability_changed.connect(self._availability_changed)
        workspace.recording_finished.connect(self._recording_finished)
        workspace.status_changed.connect(self._status_changed)
        workspace.display_changed.connect(self._display_changed)
        self.finished.connect(self._disconnect)
        self._prepare_timeout = QTimer(self)
        self._prepare_timeout.setSingleShot(True)
        self._prepare_timeout.setInterval(30_000)
        self._prepare_timeout.timeout.connect(lambda: self._fail("Analyzer did not become ready. Check its connection and status."))
        if output_controller is not None:
            output_controller.result.connect(self._output_result)
            output_controller.error.connect(self._output_error)
        QTimer.singleShot(0, self._check_initial_outputs)

    def _check_initial_outputs(self):
        if self.phase == "setup":
            self.verify_outputs(self._initial_outputs_ready)

    def _initial_outputs_ready(self):
        self.status.setText("Both Keithley outputs are OFF. Ready to record apparatus background.")
        self.next.setEnabled(True)

    def verify_outputs(self, callback):
        """Require a fresh, correlated readback before authorizing this callback."""
        if self.phase in {"cancelled", "failed", "live"} or self._check_operation is not None:
            return
        self.next.setEnabled(False)
        self.recheck.setEnabled(False)
        self.output_status.setText("Keithley A: checking… · B: checking…")
        controller = self.output_controller
        if controller is None or controller.is_connected is not True:
            self._output_check_failed("Connect Keithley and switch both outputs OFF before collecting background.")
            return
        self._check_operation = f"background_output_readback:{uuid4().hex}"
        self._check_callback = callback
        self._output_timeout.start()
        controller.call(self._check_operation)

    def _output_result(self, operation, result):
        if operation != self._check_operation:
            return
        self._output_timeout.stop()
        self._check_operation = None
        callback, self._check_callback = self._check_callback, None
        if (not isinstance(result, dict) or set(result) != {"A", "B"}
                or any(type(value) is not bool for value in result.values())):
            self._output_check_failed("Keithley returned an unknown output state; background collection is blocked.")
            return
        self.output_status.setText(f"Keithley A: {'ON' if result['A'] else 'OFF'} · B: {'ON' if result['B'] else 'OFF'}")
        if any(result.values()):
            self._output_check_failed("Switch both Keithley outputs OFF. Background collection requires A OFF and B OFF.", keep_status=True)
            return
        if self.output_controller.is_connected is not True:
            self._output_check_failed("Keithley disconnected during output verification.")
            return
        self.recheck.setEnabled(self.phase == "setup")
        if callback is not None:
            callback()

    def _output_error(self, operation, message):
        if operation == self._check_operation:
            self._output_check_failed(f"Cannot verify Keithley outputs: {message}")

    def _output_check_failed(self, message, *, keep_status=False):
        self._output_timeout.stop()
        self._check_operation = self._check_callback = None
        if not keep_status:
            self.output_status.setText("Keithley A: UNKNOWN · B: UNKNOWN")
        if self.phase == "setup":
            self.status.setText(message)
            self.next.setEnabled(False)
            self.recheck.setEnabled(True)
        elif self.phase not in {"cancelled", "failed", "live"}:
            self._fail(message)

    def _advance(self):
        if self.phase == "setup":
            self.verify_outputs(self._start_prepare)
        elif self.phase == "restore":
            self._start_signal()

    def _start_prepare(self):
        if self.phase == "setup":
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._fail(f"Cannot create the archive directory: {exc}")
                return
            self.form.setEnabled(False)
            self.next.setEnabled(False)
            self.phase = "preparing"
            self.status.setText("Waiting for the analyzer; existing Live is paused before background collection.")
            self.activity.show()
            self.activity.start()
            self._prepare_timeout.start()
            self.prepare_requested.emit()
            self._maybe_start_reference()
    def _start_signal(self):
        if self.phase == "restore":
            if not self.workspace.acquire_signal.isEnabled():
                self._fail("Analyzer is unavailable. Check the connection before starting corrected Live.")
                return
            self.phase = "starting_signal"
            self._owns_recording = True
            self.next.setEnabled(False)
            self.activity.show()
            self.activity.start()
            self.workspace.signal_state.setText("Dynamic signal restored by operator using Keithley after background acquisition")
            self.workspace.freeze.setChecked(False)
            self.workspace.start_acquisition("signal", self.signal_path)

    def _maybe_start_reference(self):
        if self.phase != "preparing" or not self.workspace.acquire_reference.isEnabled():
            return
        self.verify_outputs(self._begin_reference)

    def _begin_reference(self):
        if self.phase != "preparing":
            return
        self._prepare_timeout.stop()
        self.phase = "collecting"
        self._owns_recording = True
        self.heading.setText("2 · Collecting background")
        self.workspace.reference_state.setText(
            "Keithley A and B outputs OFF; verified by hardware readback before and after each background sweep. "
            "Apparatus noise without dynamic signal; input path and analyzer settings retained."
        )
        self.workspace.duration.setText(self.duration.currentData())
        self.workspace.mode.setCurrentIndex(self.workspace.mode.findData("ema_preview"))
        self.workspace.tau.setText(self.smoothing.currentData())
        self.workspace.freeze.setChecked(False)
        self.workspace.start_acquisition("reference", self.reference_path)

    def _availability_changed(self):
        # Device readiness can change inside the page's state update. Start on
        # the next event turn so acquisition ownership cannot be overwritten.
        if self.phase == "preparing":
            self._ready_timer.start(0)

    def _recording_finished(self, kind: str, succeeded: bool):
        if self.phase not in {"collecting", "starting_signal"}:
            return
        if not succeeded:
            self._fail(self.workspace.state_label.text())
        elif kind == "reference" and self.phase == "collecting":
            self._owns_recording = False
            self.phase = "restore"
            self.activity.stop()
            self.activity.hide()
            self.heading.setText("3 · Restore your signal")
            self.status.setText(
                "Background is saved. Restore the dynamic signal using Keithley without changing the analyzer settings or input path. "
                "When ready, click below. Correction and averaging will start automatically.")
            self.next.setText("Signal restored · Start corrected Live")
            self.next.setEnabled(True)

    def _status_changed(self, text: str):
        if self.phase in {"collecting", "starting_signal"}:
            self.status.setText(text)

    def _display_changed(self, _context, result, _description):
        if self.phase != "starting_signal" or result is None:
            return
        if result.quality not in {CorrectionQuality.READY, CorrectionQuality.UNQUALIFIED}:
            self._fail("The new spectrum could not be corrected. Check the background and analyzer settings.")
            if self.workspace.running:
                self.workspace.stop_acquisition()
            return
        self.phase = "live"
        self._owns_recording = False
        self.live_ready.emit()
        self.accept()

    def _fail(self, message: str):
        self._prepare_timeout.stop()
        self._output_timeout.stop()
        self._check_operation = self._check_callback = None
        owns_recording = self._owns_recording
        self.phase = "failed"
        if owns_recording and self.workspace.running:
            # Preserve the archive's fault status; never promote a partial or
            # contaminated background to a usable completed profile.
            if not self.workspace.handle_error("single_sweep", message):
                self.workspace.stop_acquisition()
        self._owns_recording = False
        self.activity.stop()
        self.activity.hide()
        self.heading.setText("Background correction stopped")
        self.status.setText(message)
        self.next.setEnabled(False)
        self.cancel.setText("Close")

    def _cancel_work(self):
        self._prepare_timeout.stop()
        self._output_timeout.stop()
        self._check_operation = self._check_callback = None
        self.phase = "cancelled"
        if self._owns_recording and self.workspace.running:
            self.workspace.stop_acquisition()
        self._owns_recording = False

    def reject(self):
        self._cancel_work()
        super().reject()

    def closeEvent(self, event: QCloseEvent):
        self._cancel_work()
        super().closeEvent(event)

    def _disconnect(self, _result):
        self._output_timeout.stop()
        self._check_operation = self._check_callback = None
        if self.output_controller is not None:
            self.output_controller.result.disconnect(self._output_result)
            self.output_controller.error.disconnect(self._output_error)
        for signal, slot in (
            (self.workspace.availability_changed, self._availability_changed),
            (self.workspace.recording_finished, self._recording_finished),
            (self.workspace.status_changed, self._status_changed),
            (self.workspace.display_changed, self._display_changed),
        ):
            signal.disconnect(slot)
