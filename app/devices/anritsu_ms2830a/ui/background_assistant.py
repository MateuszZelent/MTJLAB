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
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SubtitleLabel,
)

from app.ui.dialogs import StationDialog, StationFileDialog


class BackgroundCorrectionAssistant(StationDialog):
    """Background collection at an operator-selected source operating point."""

    prepare_requested = Signal()
    filter_ready = Signal()

    def __init__(self, workspace, directory: Path, parent=None, *, record_available=True):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Configure background")
        self.resize(620, 620)
        self.setMinimumSize(400, 380)
        self.workspace = workspace
        self.directory = directory
        self.record_available = record_available
        self.phase = "setup"
        self._owns_recording = False
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
        self.reference_path = directory / f"background_{stamp}.h5"
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
            "Set the background operating point, for example a low Keithley current. "
            "Keithley outputs may remain ON. Keep the same cables, frequency range and analyzer settings. "
            "The assistant does not change source outputs or setpoints.", content)
        explanation.setWordWrap(True)
        fields.addWidget(explanation)
        self.explanation = explanation
        self.background_source = ComboBox(content)
        self.background_source.setAccessibleName("Background source")
        self.background_source.addItem("Record a new background", userData="record")
        self.background_source.addItem("Load a saved background", userData="load")
        fields.addWidget(BodyLabel("Background source", content))
        fields.addWidget(self.background_source)
        self.load_fields = QWidget(content)
        load_layout = QVBoxLayout(self.load_fields)
        load_layout.setContentsMargins(0, 0, 0, 0)
        load_layout.addWidget(BodyLabel("Saved background file (HDF5)", self.load_fields))
        file_row = QHBoxLayout()
        self.profile_path = LineEdit(self.load_fields)
        self.profile_path.setReadOnly(True)
        self.profile_path.setPlaceholderText("Choose a previously saved background…")
        self.choose_profile = PushButton("Browse…", self.load_fields)
        file_row.addWidget(self.profile_path, 1)
        file_row.addWidget(self.choose_profile)
        load_layout.addLayout(file_row)
        self.load_fields.hide()
        fields.addWidget(self.load_fields)
        self.record_fields = QWidget(content)
        record_layout = QVBoxLayout(self.record_fields)
        record_layout.setContentsMargins(0, 0, 0, 0)
        record_layout.setSpacing(10)
        fields.addWidget(self.record_fields)
        record_layout.addWidget(BodyLabel("Minimum background collection time", self.record_fields))
        self.duration = ComboBox(content)
        default = workspace.duration.text()
        for value in dict.fromkeys((default, "10 s", "30 s", "60 s")):
            self.duration.addItem(value, userData=value)
        record_layout.addWidget(self.duration)
        minimum = workspace._settings.anritsu.spectrum_correction.calibration_min_sweeps
        minimum_note = CaptionLabel(f"At least {minimum} complete sweeps are required; collection can take longer with slow sweeps.", content)
        minimum_note.setWordWrap(True)
        record_layout.addWidget(minimum_note)
        note = CaptionLabel(
            "When the background is ready, this window closes and Background correction is enabled. "
            "Restore your measurement operating point, then press Start Live in the main window. "
            "Combine Background with Narrow peaks and Denoise as needed.", content)
        note.setWordWrap(True)
        fields.addWidget(note)
        archive_note = CaptionLabel(f"Raw spectra and background will be saved automatically in:\n{directory}", content)
        archive_note.setWordWrap(True)
        fields.addWidget(archive_note)
        self.archive_note = archive_note
        fields.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self.form = content
        self.activity = IndeterminateProgressBar(self)
        self.activity.hide()
        root.addWidget(self.activity)
        self.status = BodyLabel("Ready to record background at your selected operating point.", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        actions = QHBoxLayout()
        self.cancel = PushButton("Cancel", self)
        self.next = PrimaryPushButton("Record background", self)
        actions.addWidget(self.cancel)
        actions.addWidget(self.next, 1)
        root.addLayout(actions)
        self.cancel.clicked.connect(self.reject)
        self.next.clicked.connect(self._advance)
        self.background_source.currentIndexChanged.connect(self._source_changed)
        self.choose_profile.clicked.connect(self._choose_profile)
        self.profile_path.textChanged.connect(self._source_changed)
        self._ready_timer = QTimer(self)
        self._ready_timer.setSingleShot(True)
        self._ready_timer.timeout.connect(self._maybe_start_reference)
        workspace.availability_changed.connect(self._availability_changed)
        workspace.recording_finished.connect(self._recording_finished)
        workspace.profile_load_finished.connect(self._profile_loaded)
        workspace.status_changed.connect(self._status_changed)
        self.finished.connect(self._disconnect)
        self._prepare_timeout = QTimer(self)
        self._prepare_timeout.setSingleShot(True)
        self._prepare_timeout.setInterval(30_000)
        self._prepare_timeout.timeout.connect(lambda: self._fail("Analyzer did not become ready. Check its connection and status."))
        if not record_available:
            self.background_source.setCurrentIndex(self.background_source.findData("load"))

    def _advance(self):
        if self.phase == "setup":
            if self.background_source.currentData() == "load":
                self._start_load()
            else:
                self._start_prepare()

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

    def _maybe_start_reference(self):
        if self.phase != "preparing" or not self.workspace.acquire_reference.isEnabled():
            return
        self._begin_reference()

    def _begin_reference(self):
        if self.phase != "preparing":
            return
        self._prepare_timeout.stop()
        self.phase = "collecting"
        self._owns_recording = True
        self.heading.setText("2 · Collecting background")
        self.workspace.reference_state.setText(
            "Operator-selected background operating point; source output states not checked. "
            "Input path and analyzer settings retained."
        )
        self.workspace.duration.setText(self.duration.currentData())
        self.workspace.freeze.setChecked(False)
        self.workspace.start_acquisition("reference", self.reference_path)

    def _availability_changed(self):
        # Device readiness can change inside the page's state update. Start on
        # the next event turn so acquisition ownership cannot be overwritten.
        if self.phase == "preparing":
            self._ready_timer.start(0)

    def _recording_finished(self, kind: str, succeeded: bool):
        if self.phase != "collecting":
            return
        if not succeeded:
            self._fail(self.workspace.state_label.text())
        elif kind == "reference" and self.phase == "collecting":
            self._background_ready()

    def _source_changed(self, *_args):
        if self.phase != "setup":
            return
        loading = self.background_source.currentData() == "load"
        self.load_fields.setVisible(loading)
        self.record_fields.setVisible(not loading)
        self.explanation.setText(
            "Load a background acquired with the same cables, frequency range and analyzer settings. "
            "The saved file is read without changing it. Sources and setpoints remain under your control."
            if loading else
            "Set the background operating point, for example a low Keithley current. "
            "Keithley outputs may remain ON. Keep the same cables, frequency range and analyzer settings. "
            "The assistant does not change source outputs or setpoints.")
        self.archive_note.setText(
            "The saved background is used as a display filter. Start Live from the main window." if loading else
            f"Raw spectra and background will be saved automatically in:\n{self.directory}")
        self.next.setText("Load background" if loading else "Record background")
        self.next.setEnabled(bool(self.profile_path.text().strip()) if loading else self.record_available)
        self.status.setText("Choose a saved background. Analyzer settings and reference age are checked before correction."
                            if loading else "Ready to record background at your selected operating point." if self.record_available
                            else "Connect the analyzer and qualify acquisition to record a new background.")

    def _choose_profile(self):
        path, _selected = StationFileDialog.getOpenFileName(
            self, "Load saved background", str(self.directory), "HDF5 (*.h5)")
        if path:
            self.profile_path.setText(path)

    def _start_load(self):
        path = self.profile_path.text().strip()
        if not path:
            return
        self.phase = "loading"
        self.form.setEnabled(False)
        self.next.setEnabled(False)
        self.activity.show()
        self.activity.start()
        self.status.setText("Loading and validating the saved background…")
        self.prepare_requested.emit()
        try:
            self.workspace.load_background_profile(Path(path))
        except (RuntimeError, ValueError, OSError) as exc:
            self._profile_loaded(False, str(exc))

    def _profile_loaded(self, succeeded: bool, message: str):
        if self.phase != "loading":
            return
        if succeeded:
            self._background_ready()
        else:
            self.phase = "setup"
            self.activity.stop()
            self.activity.hide()
            self.form.setEnabled(True)
            self.next.setEnabled(True)
            self.status.setText(f"Could not load this background: {message}. Choose another file or record a new background.")

    def _background_ready(self):
        self._owns_recording = False
        self.phase = "ready"
        self._prepare_timeout.stop()
        self._ready_timer.stop()
        self.activity.stop()
        self.activity.hide()
        self.filter_ready.emit()
        self.accept()

    def _status_changed(self, text: str):
        if self.phase == "collecting":
            self.status.setText(text)

    def _fail(self, message: str):
        self._prepare_timeout.stop()
        owns_recording = self._owns_recording
        self.phase = "failed"
        if owns_recording and self.workspace.running and not self.workspace.handle_error("single_sweep", message):
            # Preserve the archive's fault status; never promote a partial or
            # contaminated background to a usable completed profile.
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
        for signal, slot in (
            (self.workspace.availability_changed, self._availability_changed),
            (self.workspace.recording_finished, self._recording_finished),
            (self.workspace.profile_load_finished, self._profile_loaded),
            (self.workspace.status_changed, self._status_changed),
        ):
            signal.disconnect(slot)
