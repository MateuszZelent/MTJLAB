"""Guided background acquisition using the existing durable correction workflow."""

from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
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

from app.devices.anritsu_ms2830a import InstrumentAverageTarget
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.ui.dialogs import StationDialog, StationFileDialog

from .spectrum_controls import AveragingTargetControls


class BackgroundAveragingControls(QWidget):
    """Choose a raw PC recording or the instrument's completed linear-power mean."""

    def __init__(self, duration, parent=None, *, allow_instrument, minimum_sweeps, instrument_timeout):
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setColumnStretch(1, 1)
        self.backend = ComboBox(self)
        self.backend.setAccessibleName("Background averaging source")
        self.backend.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.backend.setMinimumWidth(110)
        self.backend.addItem("PC · record individual sweeps", userData="pc")
        self.target = None
        if allow_instrument:
            self.backend.addItem("Instrument · save mean only", userData="instrument")
            layout.addWidget(BodyLabel("Averaging", self), 0, 0)
            layout.addWidget(self.backend, 0, 1)
            self.target = AveragingTargetControls(self, initial_frames=minimum_sweeps, allow_instrument=True)
            self.target.backend.setCurrentIndex(self.target.backend.findData("instrument"))
            self.target.backend.hide()
            timeout_s = parse_quantity(instrument_timeout, DIMENSION_TIME).si_value
            self.target.seconds.setMaximum(max(1, min(self.target.seconds.maximum(), ceil(timeout_s) - 1)))
            self.target.seconds.setValue(1)
            self.target.setToolTip("A duration is converted to a whole sweep count using the analyzer sweep time.")
            self.target_label = BodyLabel("Target", self)
            layout.addWidget(self.target_label, 1, 0)
            layout.addWidget(self.target, 1, 1)
        else:
            self.backend.hide()
        self.duration = duration
        self.duration.setAccessibleName("Minimum background collection time")
        self.duration.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.duration.setMinimumWidth(90)
        self.duration_label = BodyLabel("Duration", self)
        layout.addWidget(self.duration_label, 2, 0)
        layout.addWidget(self.duration, 2, 1)
        self.minimum_sweeps = minimum_sweeps
        self.detail = CaptionLabel(self)
        self.detail.setWordWrap(True)
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.detail, 3, 0, 1, 2)
        self.backend.currentIndexChanged.connect(self._update_fields)
        self._update_fields()

    @property
    def uses_instrument(self):
        return self.backend.currentData() == "instrument"

    def instrument_target(self) -> InstrumentAverageTarget | None:
        if not self.uses_instrument:
            return None
        frames, seconds = self.target.target()
        return InstrumentAverageTarget(frame_count=frames) if seconds is None else InstrumentAverageTarget(duration_s=seconds)

    def _update_fields(self):
        instrument = self.uses_instrument
        self.duration.setVisible(not instrument)
        self.duration_label.setVisible(not instrument)
        if self.target is not None:
            self.target.setVisible(instrument)
            self.target_label.setVisible(instrument)
        self.detail.setText("Saves the linear-power mean. Individual sweeps and variance are unavailable." if instrument else
                            f"Collects at least {self.minimum_sweeps} complete sweeps. Slow sweeps can extend the recording.")


class BackgroundCorrectionAssistant(StationDialog):
    """Background collection at an operator-selected source operating point."""

    prepare_requested = Signal()
    filter_ready = Signal()

    def __init__(self, workspace, directory: Path, parent=None, *, record_available=True, for_stripes=False):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Stripes background" if for_stripes else "Configure background")
        self.resize(620, 620)
        self.setMinimumSize(400, 380)
        self.workspace = workspace
        self.directory = directory
        self.record_available = record_available
        self.for_stripes = for_stripes
        self.phase = "setup"
        self._owns_recording = False
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
        self.reference_path = directory / f"background_{stamp}.h5"
        root = self.modal_content_layout(spacing=12)
        self.heading = SubtitleLabel("Choose a background for Stripes" if for_stripes else "1 · Prepare a background", self)
        root.addWidget(self.heading)
        scroll = ScrollArea(self)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        fields = QVBoxLayout(content)
        fields.setContentsMargins(4, 4, 12, 4)
        fields.setSpacing(10)
        explanation = BodyLabel(self._record_instructions(), content)
        explanation.setWordWrap(True)
        explanation.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.explanation = explanation
        self.background_source = ComboBox(content)
        self.background_source.setAccessibleName("Background source")
        self.background_source.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.background_source.setMinimumWidth(120)
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
        self.duration = ComboBox(self.record_fields)
        default = workspace.duration.text()
        for value in dict.fromkeys((default, "10 s", "30 s", "60 s", "120 s", "300 s")):
            self.duration.addItem(value, userData=value)
        self.background_averaging = BackgroundAveragingControls(
            self.duration, self.record_fields, allow_instrument=record_available and not for_stripes,
            minimum_sweeps=workspace._settings.anritsu.spectrum_correction.calibration_min_sweeps,
            instrument_timeout=workspace._settings.anritsu.acquisition.operation_complete_timeout,
        )
        record_layout.addWidget(self.background_averaging)
        fields.addWidget(explanation)
        self.record_note = CaptionLabel(
            ("Automatic Stripes finds the affected frequencies in this background. "
             "After recording, restore your measurement operating point and press Start Live."
             if for_stripes else
             "When the background is ready, this window closes and Background correction is enabled. "
             "Restore your measurement operating point, then press Start Live in the main window. "
             "Use Avg: 16 or 32 for the measurement as well. Longer background collection reduces "
             "background uncertainty, but cannot remove noise in a single measurement frame. "
             "Averaging does not remove drifting interference lines."), content)
        self.record_note.setWordWrap(True)
        self.record_note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        fields.addWidget(self.record_note)
        archive_folder = directory.name or str(directory)
        archive_note = CaptionLabel(
            f"Raw spectra and background will be saved automatically in folder: {archive_folder}",
            content,
        )
        archive_note.setToolTip(str(directory))
        archive_note.setWordWrap(True)
        archive_note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        fields.addWidget(archive_note)
        self.archive_note = archive_note
        fields.addStretch()
        scroll.setWidget(content)
        scroll.enableTransparentBackground()
        root.addWidget(scroll, 1)
        self.form = content
        self.activity = IndeterminateProgressBar(self)
        self.activity.hide()
        root.addWidget(self.activity)
        self.status = BodyLabel("Set the background operating point, then record.", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        self.cancel = PushButton("Cancel", self)
        self.next = PrimaryPushButton("Record background", self)
        actions.addWidget(self.cancel)
        actions.addWidget(self.next)
        root.addLayout(actions)
        self.cancel.clicked.connect(self.reject)
        self.next.clicked.connect(self._advance)
        self.background_source.currentIndexChanged.connect(self._source_changed)
        self.background_averaging.backend.currentIndexChanged.connect(self._source_changed)
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
        if for_stripes or not record_available:
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
        target = self.background_averaging.instrument_target()
        if target is None:
            self.workspace.duration.setText(self.duration.currentData())
        self.workspace.freeze.setChecked(False)
        self.workspace.start_acquisition("reference", self.reference_path, instrument_target=target)
        if self.phase == "collecting" and not self.workspace.running:
            self._fail(self.workspace.state_label.text())

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

    def _record_instructions(self) -> str:
        if self.for_stripes:
            return ("Use a background operating point where the signal of interest is absent. "
                    "Keep the same cables, frequency range and analyzer settings.")
        return ("Set the background operating point, for example a low Keithley current. "
                "Keithley outputs may remain ON. Keep the same cables, frequency range and analyzer settings. "
                "The assistant does not change source outputs or setpoints.")

    def _source_changed(self, *_args):
        if self.phase != "setup":
            return
        loading = self.background_source.currentData() == "load"
        self.load_fields.setVisible(loading)
        self.record_fields.setVisible(not loading)
        self.record_note.setVisible(not loading)
        self.explanation.setText(
            ("Choose a saved background acquired without the signal of interest, with the same cables, "
             "frequency range and analyzer settings. Stripes uses it to find interference bands. "
             "Reference sets the dB baseline." if self.for_stripes else
             "Load a background acquired with the same cables, frequency range and analyzer settings. "
             "The saved file is read without changing it. Sources and setpoints remain under your control.")
            if loading else
            self._record_instructions())
        if loading:
            archive_note = ("Stripes applies the saved background once the analyzer settings match." if self.for_stripes
                            else "The saved background is used as a display filter. Start Live from the main window.")
        else:
            contents = "The instrument mean" if self.background_averaging.uses_instrument else "Raw spectra and background"
            archive_folder = self.directory.name or str(self.directory)
            archive_note = f"{contents} will be saved automatically in folder: {archive_folder}"
        self.archive_note.setText(archive_note)
        self.archive_note.setToolTip("" if loading else str(self.directory))
        self.next.setText("Load background" if loading else "Record background")
        self.next.setEnabled(bool(self.profile_path.text().strip()) if loading else self.record_available)
        self.status.setText("Choose a saved background. Its age and analyzer settings are checked before correction."
                            if loading else "Set the background operating point, then record." if self.record_available
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
        if not self.for_stripes:
            self.prepare_requested.emit()
        try:
            self.workspace.load_background_profile(Path(path), require_raw_sweeps=self.for_stripes)
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
        self._ready_timer.stop()
        self.phase = "canceled"
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
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
