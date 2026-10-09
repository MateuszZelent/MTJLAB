"""Fluent quantitative spectrum workspace with explicit acquisition ownership."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    IndeterminateProgressBar,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    ScrollArea,
    SpinBox,
    SplitPushButton,
    StrongBodyLabel,
)

from app.devices.anritsu_ms2830a.acquisition_context import spectrum_configuration_fingerprint
from app.devices.anritsu_ms2830a.adapter import (
    AdvancedSpectrumSnapshot,
    AnritsuFullConfigurationReadback,
    SpectrumTrace,
)
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.domain.spectrum_correction import (
    BackgroundProfile,
    CorrectedSpectrumFrame,
    SpectrumAcquisitionContext,
    SpectrumFrameEnvelope,
    SpectrumFrameRole,
    SweepEvidence,
)
from app.domain.spectrum_decisions import SpectrumDecisionOperation, SpectrumProcessingChange
from app.domain.spectrum_finalization import (
    SpectrumFinalizationBatchRequest,
    SpectrumFinalizationResumeRequest,
)
from app.domain.spectrum_interleaved import (
    InterleavedSpectrumConfig,
    SpectrumOperatorStateConfirmation,
)
from app.settings.models import StationSettings
from app.spectrum.interleaved_acquisition import InterleavedPhase, InterleavedSpectrumAcquisition
from app.ui.dialogs import StationFileDialog
from app.ui.widgets import NotificationBanner, SpectrumPlotWidget

from .correction_controller import (
    CorrectionSessionRequest,
    CorrectionViewSnapshot,
    SpectrumCorrectionController,
    SpectrumFinalizationRequest,
    SpectrumInterferenceImportRequest,
)


class _RecordingInputError(ValueError):
    """A validation failure linked to its visible editor, without hardware work."""

    def __init__(self, message, editor):
        super().__init__(message)
        self.editor = editor


class SpectrumCorrectionWorkspace(QWidget):
    """Acquire qualified single sweeps, archive each raw frame, render at ≤20 Hz.

    The existing page mediates device requests and keeps all other mutating
    controls disabled during this workspace's acquisition. CPU and HDF5 work
    use a separate bounded worker; no automatic field or bias changes occur.
    """

    request_device = Signal(str, object)
    busy_changed = Signal(bool)
    status_changed = Signal(str)
    display_changed = Signal(object, object, str)
    availability_changed = Signal()
    recording_finished = Signal(str, bool)
    profile_load_finished = Signal(bool, str)

    def __init__(self, settings: StationSettings, *, single_sweep_available: bool,
                 simulation_mode: bool | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("spectrumCorrectionWorkspace")
        self._settings = settings
        self._start_feedback = NotificationBanner(self)
        self._single_sweep_available = single_sweep_available
        self._allowed = False
        self._device_idn = ""
        self._running = False
        self._simulation_mode = None
        self.set_simulation_mode(simulation_mode)
        self._profile_io_busy = False
        self._finalization_busy = False
        self._resonance_dialog = None
        self._difference_dialog = None
        self._training_dialog = None
        self._validation_dialog = None
        self._diagnostic_dialog = None
        self._finalization_selection_dialog = None
        self._finalization_resume_dialog = None
        self._processing_cancel_requested = False
        self._stopping = False
        self._recording_failed = False
        self._processor_active = False
        self._segment_reset_pending = False
        self._signal_segment_index = 0
        self._interleaved = None
        self._cycle_pending = None
        self._initial_confirmation = None
        self._committed_count = 0
        self._last_rejection_message = ""
        self._kind = "signal"
        self._expected_device: str | None = None
        self._sweep_requested_at = None
        self._live_timings_s = {}
        self._full: AnritsuFullConfigurationReadback | None = None
        self._advanced: AdvancedSpectrumSnapshot | None = None
        self._context: SpectrumAcquisitionContext | None = None
        self._profile: BackgroundProfile | None = None
        self._interference_calibrations = ()
        self._frame_id = 0
        self._pending_raw: SpectrumTrace | None = None
        self._latest_raw: SpectrumTrace | None = None
        self._latest_result: CorrectedSpectrumFrame | None = None
        self._result_archive_path: Path | None = None
        self._dirty_view = False
        self._reset_plot_on_render = False
        self._corrected_auto_range_pending = True
        self._final_profiles = ()
        self._started_monotonic = 0.0
        self._duration_s = 60.0
        self._archive_path: Path | None = None
        self._processor_config = settings.anritsu.spectrum_correction.processor_config()
        self._cpu = SpectrumCorrectionController(
            self, queue_frames=settings.anritsu.spectrum_correction.processing_queue_frames,
        )
        self._cpu.completed.connect(self._processed)
        self._cpu.failed.connect(self._failed)
        self._cpu.cancelled.connect(self._processing_cancelled)
        self._cpu.processing_progress.connect(self._finalization_progress)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.recording_status = CardWidget(self)
        self.recording_status.setObjectName("spectrumRecordingStatus")
        self.recording_status.setAccessibleName("Spectrum recording status")
        status_layout = QVBoxLayout(self.recording_status)
        status_layout.setContentsMargins(12, 10, 12, 10)
        status_layout.setSpacing(4)
        self.recording_title = StrongBodyLabel("Ready to record", self.recording_status)
        self.recording_title.setWordWrap(True)
        status_layout.addWidget(self.recording_title)
        self.state_label = BodyLabel(
            "Record a reference in a state excluding the target signal. Raw frames are archived.",
            self.recording_status,
        )
        self.state_label.setWordWrap(True)
        status_layout.addWidget(self.state_label)
        self.recording_activity = IndeterminateProgressBar(self.recording_status)
        self.recording_activity.setFixedHeight(4)
        self.recording_activity.hide()
        status_layout.addWidget(self.recording_activity)
        self.progress = ProgressBar(self.recording_status)
        self.progress.setRange(0, 100)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        status_layout.addWidget(self.progress)
        self.archive_label = CaptionLabel("No archive session.", self.recording_status)
        self.archive_label.setWordWrap(True)
        status_layout.addWidget(self.archive_label)
        self.stable_state = CheckBox("I confirm the requested sample state is stable", self.recording_status)
        self.stable_state.hide()
        status_layout.addWidget(self.stable_state)
        self.confirm_interleaved_state = PrimaryPushButton("Confirm REF state and record", self.recording_status)
        self.confirm_interleaved_state.setAccessibleName("Confirm a stable state and acquire the next interleaved block")
        self.confirm_interleaved_state.hide()
        status_layout.addWidget(self.confirm_interleaved_state)
        root.addWidget(self.recording_status)
        controls = CardWidget(self)
        grid = QGridLayout(controls)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setSpacing(8)
        grid.addWidget(StrongBodyLabel("Quantitative background correction", controls), 0, 0, 1, 2)
        self.reference_state = LineEdit(controls)
        self.reference_state.setPlaceholderText("Describe the reference state and input path")
        self.reference_state.setAccessibleName("Reference state description")
        grid.addWidget(self.reference_state, 1, 0, 1, 2)
        self.duration = LineEdit(controls)
        self.duration.setText(settings.anritsu.spectrum_correction.calibration_duration)
        self.duration.setAccessibleName("Background acquisition duration with unit")
        self.tau = LineEdit(controls)
        self.tau.setText(settings.anritsu.spectrum_correction.temporal_average.time_constant)
        self.tau.setAccessibleName("Temporal time constant with unit")
        grid.addWidget(BodyLabel("Reference duration", controls), 2, 0)
        grid.addWidget(self.duration, 2, 1)
        grid.addWidget(BodyLabel("Preview time constant", controls), 3, 0)
        grid.addWidget(self.tau, 3, 1)
        self.mode = ComboBox(controls)
        for label, value in (("EMA preview", "ema_preview"), ("Measurement block", "block"),
                             ("Sliding window", "window")):
            self.mode.addItem(label, userData=value)
        self.mode.setCurrentIndex(self.mode.findData(str(self._processor_config.average_mode)))
        grid.addWidget(BodyLabel("Temporal average", controls), 4, 0)
        grid.addWidget(self.mode, 4, 1)
        self.acquire_reference = PrimaryPushButton("Record background…", controls)
        self.acquire_signal = PushButton("Record corrected spectra…", controls)
        self.stop = PushButton("Stop acquisition", controls)
        self.stop.setAccessibleName("Stop quantitative spectrum acquisition")
        self.freeze = CheckBox("Freeze displayed frame", controls)
        self.load_profile = PushButton("Load background profile…", controls)
        self.save_profile = PushButton("Export background profile…", controls)
        self.finalize_button = SplitPushButton("Finalize between two references…", controls)
        self.finalize_button.setAccessibleName("Spectrum finalization")
        self.finalize_button.dropButton.setAccessibleName("Choose finalization workflow")
        self.finalize_button.setToolTip("Finalize a whole archive, or use the arrow to choose profiles and SIGNAL points.")
        finalization_menu = RoundMenu(parent=self.finalize_button)
        self.select_finalization_action = Action("Choose profiles and SIGNAL block…", self)
        self.select_finalization_action.triggered.connect(self._open_finalization_selection)
        finalization_menu.addAction(self.select_finalization_action)
        self.select_batch_action = Action("Choose multiple SIGNAL blocks…", self)
        self.select_batch_action.triggered.connect(lambda: self._open_finalization_selection(batch_mode=True))
        finalization_menu.addAction(self.select_batch_action)
        self.interleaved_finalization_action = Action("Finalize an alternating archive…", self)
        self.interleaved_finalization_action.triggered.connect(self._open_interleaved_finalization)
        finalization_menu.addAction(self.interleaved_finalization_action)
        self.resume_batch_action = Action("Resume a stopped batch…", self)
        self.resume_batch_action.triggered.connect(self._open_finalization_resume)
        finalization_menu.addAction(self.resume_batch_action)
        self.finalize_button.setFlyout(finalization_menu)
        grid.addWidget(self.acquire_reference, 5, 0)
        grid.addWidget(self.acquire_signal, 5, 1)
        self.acquire_interleaved = PushButton("Record alternating REF / SIGNAL…", controls)
        self.acquire_interleaved.setAccessibleName("Record alternating background and signal blocks")
        grid.addWidget(self.acquire_interleaved, 6, 0, 1, 2)
        self.signal_state = LineEdit(controls)
        self.signal_state.setPlaceholderText("Describe the SIGNAL state; keep the same RF input path")
        self.signal_state.setAccessibleName("Interleaved SIGNAL state description")
        grid.addWidget(self.signal_state, 7, 0, 1, 2)
        policy = settings.anritsu.spectrum_correction.reference_policy
        self.interleaved_ref_duration = LineEdit(controls)
        self.interleaved_ref_duration.setText(policy.block_duration)
        self.interleaved_ref_duration.setAccessibleName("Interleaved REF block duration with unit")
        self.interleaved_ref_duration.setToolTip(
            "Minimum acquired duration. Complete sweeps and the minimum REF count can make this block longer."
        )
        self.interleaved_signal_duration = LineEdit(controls)
        self.interleaved_signal_duration.setText(policy.signal_duration)
        self.interleaved_signal_duration.setAccessibleName("Interleaved SIGNAL block duration with unit")
        self.interleaved_minimum_sweeps = SpinBox(controls)
        self.interleaved_minimum_sweeps.setRange(2, 100_000)
        self.interleaved_minimum_sweeps.setValue(policy.minimum_sweeps)
        self.interleaved_minimum_sweeps.setAccessibleName("Minimum complete sweeps in each interleaved REF block")
        self.interleaved_minimum_sweeps.setToolTip(
            "More REF sweeps give a better estimated mean but delay the next SIGNAL block. "
            "Two sweeps are a protocol minimum, not a noise qualification."
        )
        grid.addWidget(BodyLabel("Alternating REF duration", controls), 8, 0)
        grid.addWidget(self.interleaved_ref_duration, 8, 1)
        grid.addWidget(BodyLabel("Alternating SIGNAL duration", controls), 9, 0)
        grid.addWidget(self.interleaved_signal_duration, 9, 1)
        grid.addWidget(BodyLabel("Minimum REF sweeps", controls), 10, 0)
        grid.addWidget(self.interleaved_minimum_sweeps, 10, 1)
        self.new_signal_segment = PushButton("New SIGNAL segment", controls)
        self.new_signal_segment.setAccessibleName("Restart the current SIGNAL average")
        self.new_signal_segment.setToolTip(
            "Restart averaging after the sample state changes. Keep the background and raw archive."
        )
        self.segment_status = CaptionLabel("New segments restart averaging; they do not change the instruments.", controls)
        self.segment_status.setWordWrap(True)
        grid.addWidget(self.new_signal_segment, 11, 0)
        grid.addWidget(self.segment_status, 11, 1)
        grid.addWidget(self.load_profile, 12, 0)
        grid.addWidget(self.save_profile, 12, 1)
        self.interference_mode = ComboBox(controls)
        self.interference_mode.setAccessibleName("Background correction model for the next recording")
        self.interference_mode.addItem("Mean reference subtraction", userData=None)
        self.load_interference = PushButton("Load model calibration…", controls)
        self.load_interference.setAccessibleName("Load interference calibration from a measurement archive")
        self.clear_interference = PushButton("Use mean background", controls)
        self.interference_status = CaptionLabel("Background processing: mean reference subtraction.", controls)
        self.interference_status.setWordWrap(True)
        grid.addWidget(BodyLabel("Background model", controls), 13, 0)
        grid.addWidget(self.interference_mode, 13, 1)
        grid.addWidget(self.load_interference, 14, 0)
        grid.addWidget(self.clear_interference, 14, 1)
        grid.addWidget(self.interference_status, 15, 0, 1, 2)
        self.analyze_resonance = PushButton("Analyze recorded resonance…", controls)
        grid.addWidget(self.analyze_resonance, 16, 0, 1, 2)
        self.compare_recorded = PushButton("Compare recorded REF / SIGNAL…", controls)
        grid.addWidget(self.compare_recorded, 17, 0, 1, 2)
        self.train_model = PushButton("Prepare model from recorded REF…", controls)
        grid.addWidget(self.train_model, 18, 0, 1, 2)
        self.validate_model = PushButton("Validate model on separate REF…", controls)
        grid.addWidget(self.validate_model, 19, 0, 1, 2)
        self.diagnose_reference = PushButton("Inspect recorded REF stability…", controls)
        grid.addWidget(self.diagnose_reference, 20, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        self.controls_scroll = ScrollArea(self)
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.controls_scroll.setWidget(controls)
        self.controls_scroll.setMinimumHeight(120)
        root.addWidget(self.controls_scroll)
        actions = QHBoxLayout()
        actions.addWidget(self.stop)
        actions.addWidget(self.freeze)
        actions.addStretch(1)
        actions.addWidget(self.finalize_button)
        root.addLayout(actions)
        plots = QSplitter(Qt.Orientation.Vertical, self)
        self.raw_plot = SpectrumPlotWidget(plots, compact_toolbar=True)
        self.raw_plot.set_labels(y="Raw power", y_unit="dBm")
        self.raw_plot.set_title("Raw and reference")
        self.raw_plot.setMinimumHeight(150)
        self.corrected_plot = SpectrumPlotWidget(plots, compact_toolbar=True)
        self.corrected_plot.set_labels(y="Signed residual", y_unit="W")
        self.corrected_plot.set_title("Reference correction — provisional")
        self.corrected_plot.setMinimumHeight(180)
        plots.addWidget(self.raw_plot)
        plots.addWidget(self.corrected_plot)
        plots.setChildrenCollapsible(False)
        plots.setStretchFactor(0, 1)
        plots.setStretchFactor(1, 2)
        root.addWidget(plots, 1)
        self.frame_label = CaptionLabel("No displayed spectrum.", self)
        self.frame_label.setWordWrap(True)
        root.addWidget(self.frame_label)
        self.acquire_reference.clicked.connect(lambda: self._start_dialog("reference"))
        self.acquire_signal.clicked.connect(lambda: self._start_dialog("signal"))
        self.acquire_interleaved.clicked.connect(lambda: self._start_dialog("interleaved"))
        self.confirm_interleaved_state.clicked.connect(self._confirm_cycle_state)
        self.stable_state.toggled.connect(lambda _checked: self.set_available(self._allowed))
        self.stop.clicked.connect(self.stop_acquisition)
        self.new_signal_segment.clicked.connect(self._start_signal_segment)
        self.load_profile.clicked.connect(self._load_profile)
        self.save_profile.clicked.connect(self._save_profile)
        self.finalize_button.clicked.connect(self._finalize_dialog)
        self.analyze_resonance.clicked.connect(self._open_resonance_analysis)
        self.compare_recorded.clicked.connect(self._open_difference_analysis)
        self.train_model.clicked.connect(self._open_model_training)
        self.validate_model.clicked.connect(self._open_model_validation)
        self.diagnose_reference.clicked.connect(self._open_reference_diagnostics)
        self.load_interference.clicked.connect(self._load_interference)
        self.clear_interference.clicked.connect(lambda: self.interference_mode.setCurrentIndex(0))
        self.interference_mode.currentIndexChanged.connect(self._interference_selection_changed)
        self.freeze.toggled.connect(lambda _value: self._render())
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(round(1000 * parse_quantity(
            settings.anritsu.spectrum_correction.render_interval, DIMENSION_TIME,
        ).si_value))
        self._render_timer.timeout.connect(self._tick)
        self._render_timer.start()
        self.set_available(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep both data views useful in a short/narrow page. All controls
        # remain keyboard-accessible through the Fluent scroll viewport.
        self.controls_scroll.setMaximumHeight(max(120, min(420, round(self.height() * 0.40))))

    @property
    def running(self) -> bool:
        return self._running

    @property
    def simulation_mode(self) -> bool | None:
        return self._simulation_mode

    def set_simulation_mode(self, mode: bool | None):
        """Composition-provided backend mode; never infer it from IDN or paths."""
        if mode is not None and type(mode) is not bool:
            raise ValueError("Simulation mode must be an explicit boolean or unknown.")
        if self._running:
            raise ValueError("Cannot change backend provenance during a recording.")
        self._simulation_mode = mode

    def set_measurement_directory(self, directory: Path | None) -> None:
        self._sample_measurement_directory = directory

    def set_available(self, available: bool, *, device_idn: str | None = None):
        self._allowed = available
        if device_idn is not None:
            self._device_idn = device_idn
        ready = available and self._single_sweep_available and not self._running and not self._profile_io_busy
        self.acquire_reference.setEnabled(ready)
        self.acquire_signal.setEnabled(ready and self._profile is not None)
        self.acquire_interleaved.setEnabled(ready)
        acquiring = self._interleaved is None or self._interleaved.waiting_role is None
        self.acquire_reference.setText("Recording background…" if self._running and acquiring and self._kind == "reference"
                                       else "Record background…")
        self.acquire_signal.setText("Recording corrected spectra…" if self._running and acquiring and self._kind == "signal"
                                    else "Record corrected spectra…")
        self.acquire_interleaved.setText("Alternating session active" if self._running and self._interleaved is not None
                                        else "Record alternating REF / SIGNAL…")
        self.acquire_signal.setToolTip(
            "Record or load a background profile first." if self._profile is None
            else "Selecting a new archive starts recording immediately."
        )
        self.acquire_reference.setToolTip("Selecting a new archive starts recording immediately.")
        self.stop.setText("Cancel processing" if self._finalization_busy else "Stop acquisition")
        self.stop.setAccessibleName("Cancel offline spectrum processing" if self._finalization_busy
                                    else "Stop quantitative spectrum acquisition")
        self.stop.setEnabled((self._running and not self._stopping)
                             or (self._finalization_busy and not self._processing_cancel_requested))
        self.new_signal_segment.setEnabled(self._running and self._kind == "signal"
            and self._interleaved is None and self._processor_active and not self._stopping
            and not self._segment_reset_pending)
        waiting = self._interleaved is not None and self._interleaved.waiting_role is not None
        self.stable_state.setEnabled(waiting and self._running and not self._stopping and self._cycle_pending is None)
        self.confirm_interleaved_state.setEnabled(self.stable_state.isEnabled() and self.stable_state.isChecked())
        self.new_signal_segment.setText("Starting segment…" if self._segment_reset_pending else "New SIGNAL segment")
        self.load_profile.setEnabled(not self._running and not self._profile_io_busy)
        self.save_profile.setEnabled(not self._running and not self._profile_io_busy and self._profile is not None)
        self.finalize_button.setEnabled(not self._running and not self._profile_io_busy)
        self.analyze_resonance.setEnabled(not self._running and not self._profile_io_busy)
        self.compare_recorded.setEnabled(not self._running and not self._profile_io_busy)
        self.train_model.setEnabled(not self._running and not self._profile_io_busy)
        self.validate_model.setEnabled(not self._running and not self._profile_io_busy)
        self.diagnose_reference.setEnabled(not self._running and not self._profile_io_busy)
        idle = not self._running and not self._profile_io_busy
        self.load_interference.setEnabled(idle and self._profile is not None and self._context is not None)
        self.interference_mode.setEnabled(idle and bool(self._interference_calibrations))
        self.clear_interference.setEnabled(idle and self.interference_mode.currentIndex() > 0)
        for control in (self.duration, self.tau, self.mode, self.reference_state, self.signal_state,
                        self.interleaved_ref_duration, self.interleaved_signal_duration,
                        self.interleaved_minimum_sweeps):
            control.setEnabled(not self._running)
        if not self._single_sweep_available:
            self.acquire_reference.setToolTip("Quantitative acquisition requires a qualified single-sweep protocol.")
        alternating_hint = (
            "Quantitative acquisition requires a qualified single-sweep protocol." if not self._single_sweep_available
            else "Stop the active recording before starting another session." if self._running
            else "Wait for background processing to finish." if self._profile_io_busy
            else "Connect the analyzer and finish any active workflow before recording." if not available
            else "Enter REF and SIGNAL state descriptions, choose a new archive, then confirm the stable REF state."
        )
        self.acquire_interleaved.setToolTip(alternating_hint)
        self.availability_changed.emit()

    @property
    def prefers_interleaved_acquisition(self):
        return self._settings.anritsu.spectrum_correction.reference_policy.mode == "interleaved"

    def _message(self, text):
        self.state_label.setText(text)
        self.status_changed.emit(text)

    def _show_start_issue(self, error):
        self.recording_title.setText("Cannot start recording")
        self._message(str(error))
        editor = getattr(error, "editor", None)
        if editor is not None:
            self.controls_scroll.ensureWidgetVisible(editor, 24, 24)
            editor.setFocus(Qt.FocusReason.OtherFocusReason)
            editor.selectAll()
        if self.window().isVisible():
            self._start_feedback.show_message(str(error), severity="warning")

    def _recording_duration(self, editor, label):
        try:
            duration_s = parse_quantity(editor.text(), DIMENSION_TIME).si_value
        except ValueError as exc:
            raise _RecordingInputError(f"{label}: {exc}", editor) from exc
        if duration_s <= 0:
            raise _RecordingInputError(f"{label} must be positive.", editor)
        return duration_s

    def _start_dialog(self, kind):
        if self._running or not self._allowed or self._profile_io_busy:
            self._show_start_issue(ValueError("Acquisition is unavailable while another workflow owns the instrument."))
            return
        if not self._single_sweep_available:
            self._show_start_issue(ValueError("Quantitative acquisition requires a qualified single-sweep protocol."))
            return
        try:
            self._prepare_start(kind)
        except ValueError as exc:
            self._show_start_issue(exc)
            return
        self._start_feedback.clear_message()
        directory = getattr(self, "_sample_measurement_directory", None) or Path(str(self._settings.storage.get("output_directory", "./measurements")))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        destination, _filter = StationFileDialog.getSaveFileName(
            self, "Record every raw spectrum", str(directory / f"spectrum_{kind}_{stamp}.h5"),
            "HDF5 measurement (*.h5)",
        )
        if destination:
            self.start_acquisition(kind, Path(destination))
        else:
            self.recording_title.setText("Recording canceled")
            self._message("No archive selected. Recording has not started.")

    def _prepare_start(self, kind):
        if kind not in {"reference", "signal", "interleaved"}:
            raise ValueError("Unknown spectrum acquisition role.")
        if kind in {"reference", "interleaved"} and not self.reference_state.text().strip():
            raise _RecordingInputError("Enter the reference state description above before choosing an archive.",
                                       self.reference_state)
        if kind == "signal" and self._profile is None:
            raise ValueError("Record or load a background profile first.")
        if kind == "interleaved":
            if not self.signal_state.text().strip():
                raise _RecordingInputError("Describe the SIGNAL state before starting alternating REF / SIGNAL.",
                                           self.signal_state)
            cycle = self._interleaved_config()
            duration = cycle.reference_duration_s
        else:
            duration = self._recording_duration(self.duration, "Reference duration")
        config = replace(
            self._settings.anritsu.spectrum_correction.processor_config(),
            average_mode=self.mode.currentData(),
            time_constant_s=self._recording_duration(self.tau, "Preview time constant"),
        )
        if kind == "interleaved":
            config = replace(config, minimum_reference_sweeps=cycle.minimum_reference_sweeps)
        return duration, config

    def _interleaved_config(self):
        return InterleavedSpectrumConfig(
            reference_duration_s=self._recording_duration(self.interleaved_ref_duration, "Alternating REF duration"),
            signal_duration_s=self._recording_duration(self.interleaved_signal_duration, "Alternating SIGNAL duration"),
            minimum_reference_sweeps=self.interleaved_minimum_sweeps.value(),
            maximum_reference_blocks=self._settings.anritsu.spectrum_correction.reference_policy.maximum_reference_blocks,
        )

    def start_acquisition(self, kind: str, destination: Path):
        if self._running or not self._allowed or not self._single_sweep_available or self._profile_io_busy:
            self._show_start_issue(ValueError("Acquisition is unavailable while another workflow owns the instrument."))
            return
        try:
            self._duration_s, self._processor_config = self._prepare_start(kind)
        except ValueError as exc:
            self._show_start_issue(exc)
            return
        self._start_feedback.clear_message()
        self._interleaved = (InterleavedSpectrumAcquisition(self._interleaved_config())
                             if kind == "interleaved" else None)
        self._cycle_pending = self._initial_confirmation = None
        self._kind = "reference" if kind == "interleaved" else kind
        self._signal_segment_index = 0
        self._processor_active = False
        self._segment_reset_pending = False
        self.segment_status.setText("New segments restart averaging; they do not change the instruments.")
        self._archive_path = destination
        self._full = None
        self._advanced = None
        self._context = None
        self._latest_result = None
        self._result_archive_path = None
        self._latest_raw = None
        self._final_profiles = ()
        self._sweep_requested_at = None
        self._live_timings_s.clear()
        self._dirty_view = False
        self.frame_label.setText("Waiting for the first spectrum of this recording.")
        self.display_changed.emit(None, None, self.frame_label.text())
        self.raw_plot.clear()
        self.corrected_plot.clear()
        self._running = True
        self._stopping = False
        self._recording_failed = False
        self._committed_count = 0
        self._last_rejection_message = ""
        self.recording_title.setText(
            "Starting background recording…" if kind == "reference" else "Starting corrected spectrum recording…"
        )
        self.archive_label.setText(f"Selected archive: {destination} · waiting for the first raw spectrum")
        self.recording_activity.show()
        self.recording_activity.start()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        self.busy_changed.emit(True)
        self.set_available(False)
        if self._interleaved is not None:
            self._profile = None
            self._clear_interference_calibrations()
            self.archive_label.setText(f"Selected archive: {destination} · awaiting stable REF confirmation")
            self._await_cycle_state()
            return
        self._message("Save accepted. Reading analyzer settings before recording the first sweep…")
        self._request("read_acquisition_configuration")

    def _request(self, operation):
        self._expected_device = operation
        if operation == "single_sweep":
            self._sweep_requested_at = time.perf_counter()
        self.request_device.emit(operation, "TRAC1" if operation == "single_sweep" else None)

    def handle_result(self, operation: str, result: object) -> bool:
        if operation != self._expected_device or not self._running:
            return False
        self._expected_device = None
        try:
            if operation == "read_acquisition_configuration":
                if (not isinstance(result, tuple) or len(result) != 2
                        or not isinstance(result[0], AnritsuFullConfigurationReadback)
                        or not isinstance(result[1], AdvancedSpectrumSnapshot)):
                    raise ValueError("Invalid analyzer acquisition configuration response.")
                self._full, self._advanced = result
                if self._interleaved is not None and self._context is not None and (
                        self._configuration_fingerprint() != self._context.configuration_fingerprint):
                    raise ValueError("Analyzer settings changed between blocks. Start a new archive with matching REF.")
                if self._stopping:
                    if self._context is None:
                        self._processed("stop", None)
                    else:
                        self._cpu.stop_session("aborted")
                    return True
                self._message("Starting recording: waiting for the first completed analyzer sweep…")
                self._request("single_sweep")
            elif operation == "single_sweep" and isinstance(result, SpectrumTrace):
                if self._sweep_requested_at is not None:
                    self._live_timings_s["Sweep and transfer"] = time.perf_counter() - self._sweep_requested_at
                    self._sweep_requested_at = None
                if result.sweep_evidence != SweepEvidence.QUALIFIED_SINGLE_SWEEP:
                    raise ValueError("No proof of a new, completed quantitative sweep.")
                if self._context is None:
                    self._start_processor(result)
                else:
                    self._ingest(result)
            else:
                raise ValueError("Unexpected instrument response during correction acquisition.")
        except (ValueError, BufferError) as exc:
            self._failed(operation, str(exc))
        return True

    def handle_error(self, operation: str, error: str) -> bool:
        if operation != self._expected_device or not self._running:
            return False
        self._expected_device = None
        self._failed(operation, error)
        return True

    def _start_processor(self, first_trace):
        assert self._full is not None and self._advanced is not None and self._archive_path is not None
        fingerprint = self._configuration_fingerprint()
        # Operator-provided state descriptions do not constitute experimental
        # qualification of detector statistics, trace mode, or signal absence.
        self._context = SpectrumAcquisitionContext(
            first_trace.frequencies_hz, fingerprint,
            configuration_generation=first_trace.configuration_generation,
            settings_verified=False, independent_sweeps_qualified=False,
        )
        if self._kind == "signal" and self._profile.context_id != self._context.context_id:
            raise ValueError("Actual acquisition settings differ from the recorded background profile.")
        self._pending_raw = first_trace
        self._message("First sweep received. Opening the raw spectrum archive…")
        self._cpu.start_session(CorrectionSessionRequest(
            self._context, self._processor_config, self._archive_path,
            self._settings.model_dump_json(), self._device_idn,
            profile=self._profile if self._kind == "signal" else None,
            reference_state=self.reference_state.text().strip() if self._kind == "reference" else None,
            signal_free_qualified=False,
            simulation_mode=self._simulation_mode,
            interference_calibration=(self.interference_mode.currentData() if self._kind == "signal" else None),
            interleaved_config=self._interleaved.config if self._interleaved is not None else None,
            operator_confirmation=self._initial_confirmation,
        ))

    def _configuration_fingerprint(self):
        assert self._full is not None and self._advanced is not None
        return spectrum_configuration_fingerprint(self._full, self._advanced, self._device_idn)

    def _ingest(self, trace):
        assert self._context is not None
        acquired = trace.acquisition_completed_at_utc or trace.acquired_at_utc
        envelope = SpectrumFrameEnvelope(
            self._frame_id, (f"{self._kind}-{self._archive_path.stem}"
                            + (f"-cycle-{self._interleaved.block_index}" if self._interleaved is not None else
                               f"-segment-{self._signal_segment_index}" if self._signal_segment_index else "")),
            self._context.context_id,
            trace.configuration_generation, acquired.timestamp(),
            role=SpectrumFrameRole(self._kind), evidence=trace.sweep_evidence,
            sweep_id=trace.sweep_id,
            started_at_s=(trace.acquisition_started_at_utc.timestamp()
                          if trace.acquisition_started_at_utc is not None else None),
            received_at_s=trace.acquired_at_utc.timestamp(),
        )
        self._frame_id += 1
        if self._kind == "reference":
            self._latest_raw = trace
            self._dirty_view = True
        self._cpu.ingest(envelope, trace)

    def _processed(self, operation, result):
        if self._recording_failed and operation in {
            "start", "frame", "reference", "snapshot", "operator_state_confirmation", "processing_change",
        }:
            # Drain the faulted session's close acknowledgement, but never
            # publish late results or schedule another instrument request.
            return
        if operation == "start":
            self._processor_active = True
            self.set_available(self._allowed)
            self._started_monotonic = time.monotonic()
            self.recording_title.setText(
                "Recording background" if self._kind == "reference" else "Recording corrected spectra"
            )
            self._message("Archive opened. Saving the first raw spectrum…")
            self.archive_label.setText(f"Recording raw spectra: {result}")
            pending, self._pending_raw = self._pending_raw, None
            if pending is not None:
                self._ingest(pending)
        elif operation == "frame":
            for label, key in (("Correction", "processing_duration_s"), ("Saving", "commit_duration_s")):
                if key in result:
                    self._live_timings_s[label] = result[key]
            self._committed_count = result['committed_point_count']
            view = result.get("view")
            if isinstance(view, CorrectionViewSnapshot):
                self._receive_committed_view(view)
            self.archive_label.setText(
                f"Committed {result['committed_point_count']} raw spectra: {self._archive_path}"
            )
            if self._interleaved is not None:
                self._cycle_frame_committed(result)
                return
            elapsed = time.monotonic() - self._started_monotonic
            if self._kind == "reference":
                self.progress.show()
                self.progress.setValue(min(100, round(100 * elapsed / self._duration_s)))
                self._message(f"Reference: {result['reference_count']} completed sweeps, {elapsed:.1f} s.")
                enough = result["reference_count"] >= self._processor_config.minimum_reference_sweeps
                if not self._stopping and elapsed >= self._duration_s and enough:
                    self._cpu.finish_reference()
                    return
            elif not self._stopping:
                if not result["accepted"]:
                    quality = result["quality"]
                    explanation = {
                        "stale": "Background has expired. Record a new background.",
                        "incompatible": "Acquisition settings changed. Record a matching background.",
                        "invalid_acquisition": "Sweep completion, order or identity is invalid.",
                        "invalid_model": "Spectrum is outside the calibrated background model. Use mean background or recalibrate.",
                        "raw_only": "No matching background is available.",
                    }.get(quality, "This sweep cannot be corrected.")
                    self._last_rejection_message = f"Correction rejected ({quality}): {explanation} Raw spectrum saved."
                    self._message(self._last_rejection_message)
                else:
                    self._last_rejection_message = ""
                    self._message(f"Recording: {self._committed_count} raw spectra saved, {elapsed:.1f} s. Use Stop acquisition to finish.")
            if self._stopping:
                if self._kind == "signal":
                    self._cpu.request_snapshot()
                self._cpu.stop_session("aborted")
            else:
                self._request("single_sweep")
        elif operation == "reference":
            if self._stopping:
                # A queued finalization may finish after Cancel. Retain RAW
                # but do not activate its profile or mark the run completed.
                self._cpu.stop_session("aborted")
                return
            self._profile = result
            self._clear_interference_calibrations()
            self._dirty_view = True
            self.progress.setValue(100)
            if self._interleaved is not None:
                self._interleaved.reference_committed()
                if self._stopping or self._interleaved.phase == InterleavedPhase.COMPLETE:
                    self._cpu.stop_session("aborted" if self._stopping else "completed")
                else:
                    self._await_cycle_state()
                return
            self.recording_title.setText("Finishing background recording…")
            self._message(
                f"Background: {result.sweep_count} sweeps. Signal absence and uncertainty remain unqualified."
            )
            self._cpu.stop_session("completed")
        elif operation == "snapshot" and isinstance(result, CorrectionViewSnapshot):
            self._receive_committed_view(result)
        elif operation == "operator_state_confirmation" and self._interleaved is not None:
            if self._stopping:
                self._cpu.stop_session("aborted")
                return
            self._cycle_pending = "processing_change"
            change = (SpectrumDecisionOperation.BEGIN_REFERENCE if result.role == SpectrumFrameRole.REFERENCE
                      else SpectrumDecisionOperation.RESET_SEGMENT)
            self._cpu.apply_processing_change(SpectrumProcessingChange(
                change, reference_state=result.description if change == SpectrumDecisionOperation.BEGIN_REFERENCE else None,
            ))
        elif operation == "processing_change" and self._cycle_pending == "processing_change":
            self._cycle_pending = None
            self._clear_cycle_display()
            if self._stopping:
                self._cpu.stop_session("aborted")
            else:
                self._request("read_acquisition_configuration")
        elif operation == "processing_change" and result == SpectrumDecisionOperation.RESET_SEGMENT.value:
            self._segment_reset_pending = False
            self._signal_segment_index += 1
            self._latest_result = None
            self._latest_raw = None
            self._dirty_view = False
            self.corrected_plot.clear()
            self._corrected_auto_range_pending = True
            self.frame_label.setText("New SIGNAL segment: waiting for the first corrected spectrum.")
            # A frozen old average must not be presented as the new segment.
            self.display_changed.emit(None, None, self.frame_label.text())
            self.segment_status.setText("New SIGNAL segment started. Background and raw archive retained.")
            self._message(self.segment_status.text())
            self.set_available(self._allowed)
        elif operation == "stop":
            succeeded = not self._recording_failed and not self._stopping
            self._processor_active = False
            self._segment_reset_pending = False
            self._running = False
            self._stopping = False
            self._cycle_pending = None
            self.confirm_interleaved_state.hide()
            self.stable_state.hide()
            self.recording_activity.stop()
            self.recording_activity.hide()
            if not self._recording_failed:
                self.recording_title.setText("Recording finished" if succeeded else "Recording stopped")
                qualification = " Signal absence and uncertainty remain unqualified." if self._kind == "reference" else ""
                self._message(f"Archive closed. {self._committed_count} raw spectra saved.{qualification}"
                              + (f"\n{self._last_rejection_message}" if self._last_rejection_message else ""))
            self.busy_changed.emit(False)
            self.set_available(self._allowed)
            self.archive_label.setText(f"Archive closed: {result}" if result is not None
                                       else f"No archive was opened: {self._archive_path}")
            self._dirty_view = True
            self.recording_finished.emit(self._kind, succeeded)
        elif operation == "import_profile":
            self._profile_io_busy = False
            self.recording_activity.stop()
            self.recording_activity.hide()
            self.recording_title.setText("Background profile loaded")
            self._context, self._profile = result
            self._clear_interference_calibrations()
            self._latest_result = None
            self._result_archive_path = None
            self._latest_raw = None
            self.display_changed.emit(None, None, "Profile loaded; record corrected spectra for the new profile.")
            self._final_profiles = ()
            self._reset_plot_on_render = True
            self.reference_state.setText(self._profile.reference_state)
            self._dirty_view = True
            self.set_available(self._allowed)
            self._message("Profile loaded; actual settings will be checked before signal acquisition.")
            self.profile_load_finished.emit(True, "Background profile loaded.")
        elif operation == "export_profile":
            self._profile_io_busy = False
            self.recording_activity.stop()
            self.recording_activity.hide()
            self.recording_title.setText("Background profile exported")
            self.set_available(self._allowed)
            self._message(f"Background profile exported: {result}")
        elif operation == "import_interference":
            path, calibrations, rejected = result
            self._profile_io_busy = False
            self.recording_activity.stop()
            self.recording_activity.hide()
            self._clear_interference_calibrations()
            self._interference_calibrations = calibrations
            for calibration in calibrations:
                self.interference_mode.addItem(f"Reference model: {calibration.model_id}", userData=calibration)
            self.recording_title.setText("Interference calibrations loaded")
            self.set_available(self._allowed)
            self._message(f"Loaded {len(calibrations)} compatible models from {path}. "
                          f"Choose a background model for the next recording. {len(rejected)} models excluded.")
        elif operation in {"finalize_batch", "resume_batch"}:
            journal, records, last_result = result
            self._processed("finalize", last_result)
            self.recording_title.setText(f"Batch finished: {len(records)} blocks saved")
            self.archive_label.setText(f"Batch journal: {journal}\nLast artifact: {last_result[0]}")
            self._message(f"Saved {len(records)} final blocks. Showing the last selected block. "
                          f"Batch journal: {journal}. No qualified confidence interval.")
            if operation == "resume_batch":
                self.recording_title.setText(f"Batch resumed: {len(records)} verified blocks")
        elif operation == "finalize":
            destination, block, profiles = result
            self._profile_io_busy = False
            self._finalization_busy = False
            self.recording_title.setText("Final spectrum saved")
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            by_id = {profile.profile_id: (context, profile) for context, profile in profiles}
            # The after-reference is the newest causal profile for a subsequent
            # live session; display interpolation retains both original blocks.
            self._context, self._profile = by_id[block.result.profile_weights[-1][0]]
            self._clear_interference_calibrations()
            self.reference_state.setText(self._profile.reference_state)
            self._final_profiles = tuple(by_id[identity][1] for identity, _weight in block.result.profile_weights)
            self._latest_raw = SpectrumTrace(
                tuple(self._context.frequencies_hz), tuple(10 * np.log10(block.raw_mean_w) + 30),
                datetime.fromtimestamp(block.result.completed_at_s, timezone.utc), "RAW_BLOCK_MEAN",
            )
            self._latest_result = block.result
            self._result_archive_path = destination
            self._dirty_view = self._reset_plot_on_render = True
            self.set_available(self._allowed)
            self.archive_label.setText(f"Finalized artifact: {destination}")
            self._message("Final block saved. Reference interpolation remains unqualified; no confidence interval.")

    def _start_signal_segment(self):
        if (not self._running or self._kind != "signal" or not self._processor_active
                or self._interleaved is not None or self._stopping or self._segment_reset_pending):
            return
        try:
            self._cpu.apply_processing_change(SpectrumProcessingChange(SpectrumDecisionOperation.RESET_SEGMENT))
        except (ValueError, BufferError) as exc:
            self.segment_status.setText(f"Cannot start a new segment: {exc}")
            self._message(self.segment_status.text())
            return
        self._segment_reset_pending = True
        self.segment_status.setText("Starting a new SIGNAL segment. Previously saved spectra remain in the archive.")
        self._message(self.segment_status.text())
        self.set_available(self._allowed)

    def _await_cycle_state(self):
        role = self._interleaved.waiting_role
        if role is None:
            raise ValueError("No operator state is required at this acquisition boundary.")
        self.recording_activity.stop()
        self.recording_activity.hide()
        self.progress.hide()
        self.stable_state.setChecked(False)
        self.stable_state.show()
        self.confirm_interleaved_state.show()
        self.confirm_interleaved_state.setText(f"Confirm {role.name} state and record")
        self.recording_title.setText(f"Acquisition paused — prepare {role.name}")
        description = self.reference_state.text().strip() if role == SpectrumFrameRole.REFERENCE else self.signal_state.text().strip()
        self._message(f"Prepare {role.name}: {description}. Keep the RF path and analyzer settings unchanged. "
                      "Confirm only after the sample has settled. Signal absence and drift remain unqualified.")
        if self._latest_result is not None:
            # Freeze retains data, but must not hide the acquisition boundary.
            self.corrected_plot.set_title("Previous SIGNAL block — acquisition paused")
            self.frame_label.setText(f"Previous SIGNAL block · acquisition paused · {self._latest_result.count} sweeps")
            self.display_changed.emit(self._context, self._latest_result,
                f"{self.frame_label.text()}\nCorrection archive: {self._archive_path}")
        self._dirty_view = True
        self.set_available(self._allowed)
        self._render()

    def _confirm_cycle_state(self):
        cycle = self._interleaved
        if (cycle is None or cycle.waiting_role is None or not self._running or self._stopping
                or self._cycle_pending is not None or not self.stable_state.isChecked()):
            return
        try:
            role = cycle.waiting_role
            description = self.reference_state.text().strip() if role == SpectrumFrameRole.REFERENCE else self.signal_state.text().strip()
            confirmation = SpectrumOperatorStateConfirmation(role, description, True)
            cycle.confirm_state(confirmation)
            self._kind = role.value
            self.stable_state.hide()
            self.confirm_interleaved_state.hide()
            self.recording_activity.show()
            self.recording_activity.start()
            self.recording_title.setText(f"Starting interleaved {role.name} block {cycle.block_index}…")
            self._message("Stable state reported. Verifying the acquisition boundary before the next sweep…")
            self._clear_cycle_display()
            if self._processor_active:
                self._cycle_pending = "operator_state_confirmation"
                self._cpu.confirm_operator_state(confirmation)
            else:
                self._initial_confirmation = confirmation
                self._request("read_acquisition_configuration")
            self.set_available(self._allowed)
        except (ValueError, BufferError) as exc:
            self._failed("interleaved_state", str(exc))

    def _clear_cycle_display(self):
        self._latest_result = self._latest_raw = None
        self.corrected_plot.clear()
        self._corrected_auto_range_pending = True
        self._dirty_view = False
        self.frame_label.setText(f"Interleaved {self._kind.upper()}: waiting for a committed sweep.")
        self.display_changed.emit(None, None, self.frame_label.text())

    def _cycle_frame_committed(self, result):
        try:
            if self._stopping:
                self._cpu.stop_session("aborted")
                return
            cycle = self._interleaved
            if not result["accepted"]:
                if result["quality"] == "stale" and self._kind == "signal":
                    cycle.reference_expired()
                    self._last_rejection_message = "Background expired. Raw sweep retained; prepare a new REF."
                    self._clear_cycle_display()
                    self._await_cycle_state()
                    return
                raise ValueError(f"Interleaved sweep rejected ({result['quality']}). Raw checkpoint retained.")
            done = cycle.record_committed(result["envelope"])
            duration = cycle.config.reference_duration_s if self._kind == "reference" else cycle.config.signal_duration_s
            self.progress.show()
            self.progress.setValue(round(100 * min(1.0, cycle.elapsed_s / duration)))
            self.recording_title.setText(f"Recording {self._kind.upper()} block {cycle.block_index}")
            self._message(f"Interleaved {self._kind.upper()}: {cycle.count} complete sweeps, "
                          f"{cycle.elapsed_s:.2f} s acquired. "
                          + ("Background will refresh when this REF block completes." if self._kind == "reference"
                             else f"Reference age {self._latest_result.reference_age_s:.2f} s; correction provisional."))
            if done:
                if self._kind == "reference":
                    self._cpu.finish_reference()
                else:
                    self._await_cycle_state()
            else:
                self._request("single_sweep")
        except (ValueError, BufferError) as exc:
            self._failed("interleaved_block", str(exc))

    def stop_acquisition(self):
        if self._finalization_busy:
            self._processing_cancel_requested = True
            self._cpu.cancel_processing()
            self.stop.setEnabled(False)
            self.recording_title.setText("Canceling finalization…")
            self._message("Waiting for the current processing checkpoint to finish; sources remain unchanged.")
            return
        if self._running:
            self._stopping = True
            self.stop.setEnabled(False)
            self.new_signal_segment.setEnabled(False)
            self.recording_title.setText("Stopping recording…")
            self._message("Stopping after the pending sweep and its raw checkpoint…")
            self.set_available(self._allowed)
            if self._interleaved is not None and self._interleaved.waiting_role is not None and self._cycle_pending is None:
                if self._context is None:
                    self._processed("stop", None)
                else:
                    self._cpu.stop_session("aborted")

    def _processing_cancelled(self, operation):
        if operation not in {"finalize", "finalize_batch", "resume_batch"}:
            return
        self._profile_io_busy = self._finalization_busy = False
        self.progress.setRange(0, 100)
        self.progress.hide()
        self.recording_title.setText("Finalization canceled")
        self._message("Sources unchanged. Completed blocks remain saved; inspect the batch journal for progress. "
                      "Any partially written output is marked aborted, not completed." if operation in {"finalize_batch", "resume_batch"}
                      else "Sources unchanged. Any partially written output is marked aborted, not completed.")
        self.set_available(self._allowed)

    def _failed(self, operation, error):
        if operation in {"import_profile", "export_profile", "finalize", "finalize_batch", "resume_batch", "import_interference"}:
            self._profile_io_busy = False
            self._finalization_busy = False
            self.progress.setRange(0, 100)
            action = ("Batch resume" if operation == "resume_batch" else "Finalization"
                      if operation in {"finalize", "finalize_batch"} else "Interference calibration import"
                      if operation == "import_interference" else f"Background profile {operation}")
            self.recording_title.setText(f"{action} failed")
            self.recording_activity.stop()
            self.recording_activity.hide()
            self.progress.hide()
            self._message(f"{action} failed: {error}")
            self.set_available(self._allowed)
            if operation == "import_profile":
                self.profile_load_finished.emit(False, str(error))
            return
        self._expected_device = None
        self._recording_failed = True
        self._processor_active = False
        self._segment_reset_pending = False
        self._cycle_pending = None
        self.confirm_interleaved_state.hide()
        self.stable_state.hide()
        self._running = operation != "stop"
        self._stopping = self._running
        self._pending_raw = None
        self.recording_title.setText("Recording failed" if self._committed_count == 0 else "Recording interrupted")
        self.archive_label.setText(f"Requested archive: {self._archive_path} · recording failed")
        self.recording_activity.stop()
        self.recording_activity.hide()
        self.progress.hide()
        self._message(f"Correction {operation} failed: {error}")
        try:
            if self._running:
                self._cpu.stop_session("faulted")
        except (ValueError, BufferError):
            self._running = self._stopping = False
        self.busy_changed.emit(self._running)
        self.set_available(self._allowed)
        self.recording_finished.emit(self._kind, False)

    def _receive_committed_view(self, view: CorrectionViewSnapshot):
        previous = self._latest_result
        if previous is not None and self._latest_raw is view.source_raw:
            # Status polling republishes an immutable snapshot even when no
            # new sweep has committed. Quality transitions still invalidate
            # the view, including a reference becoming stale while waiting.
            fields = ("context_id", "segment_id", "frame_id", "processing_generation",
                      "count", "completed_at_s", "quality", "final", "reference_age_s")
            if all(getattr(previous, name) == getattr(view.corrected, name) for name in fields):
                return
        first_corrected_frame = self._latest_result is None and view.corrected is not None
        self._result_archive_path = self._archive_path
        self._latest_raw = view.source_raw
        self._latest_result = view.corrected
        self._dirty_view = True
        # Publish the first committed correction even when the workspace page
        # is hidden or its preview timer is paused. Later frames retain the
        # bounded preview refresh rate; freeze still defers drawing.
        if first_corrected_frame:
            self._render()

    def live_performance_text(self):
        if not self._live_timings_s:
            return "Live timing measurements become available after the first committed sweep."
        return "Latest committed sweep:\n" + "\n".join(
            f"{label}: {duration_s * 1000:.1f} ms" for label, duration_s in self._live_timings_s.items()
        )

    def showEvent(self, event):
        super().showEvent(event)
        self._dirty_view = True
        self._render()

    def _tick(self):
        if (self._running and not self._stopping and self._kind == "signal" and self._context is not None
                and (self._interleaved is None or self._interleaved.waiting_role is None)):
            try:
                self._cpu.request_snapshot(time.time())
            except BufferError:
                pass  # Preview request can wait; raw ingestion is never dropped.
        self._render()

    def _render(self):
        if self.freeze.isChecked() or not self._dirty_view:
            return
        self._dirty_view = False
        if self._reset_plot_on_render:
            self.raw_plot.clear()
            self.corrected_plot.clear()
            self._reset_plot_on_render = False
            self._corrected_auto_range_pending = True
        raw = self._latest_raw
        if raw is not None and self.raw_plot.isVisible():
            self.raw_plot.set_trace("Raw", raw.frequencies_hz, raw.powers_dbm, primary=True)
        profile, ctx = self._profile, self._context
        if (self.raw_plot.isVisible() and profile is not None and ctx is not None
                and profile.context_id == ctx.context_id):
            references = self._final_profiles or (profile,)
            for index, reference in enumerate(references):
                name = ("Reference before" if index == 0 else "Reference after") if self._final_profiles else "Reference"
                self.raw_plot.set_trace(name, ctx.frequencies_hz, 10 * np.log10(reference.mean_w) + 30)
        if self._latest_result is not None and ctx is not None:
            result = self._latest_result
            paused = self._running and self._interleaved is not None and self._interleaved.waiting_role is not None
            if self.corrected_plot.isVisible():
                plot = self.corrected_plot.plot
                if plot.getAxis("bottom").logMode or plot.getAxis("left").logMode:
                    plot.setLogMode(x=False, y=False)
                    self._corrected_auto_range_pending = True
                self.corrected_plot.set_title("Previous SIGNAL block — acquisition paused" if paused else
                    "Reference correction — final block" if result.final else "Reference correction — provisional")
                self.corrected_plot.set_trace("Signed residual", ctx.frequencies_hz,
                                              result.values_w, primary=True)
                if self._corrected_auto_range_pending:
                    self.corrected_plot.auto_range()
                    self._corrected_auto_range_pending = False
            self.frame_label.setText(
                ("Previous SIGNAL block · acquisition paused · " if paused else "")
                + f"{result.quality.value} · frame {result.frame_id} · {result.count} sweeps"
                f" · reference age {result.reference_age_s:.1f} s"
                f" · {'final block' if result.final else 'provisional signed power'}"
                " · no qualified confidence interval"
                + (f" · model {result.interference_model_id}" if result.interference_model_id is not None else "")
            )
            self.display_changed.emit(
                ctx, result, f"{self.frame_label.text()}\nCorrection archive: {self._result_archive_path or 'unavailable'}",
            )
        elif raw is not None:
            self.frame_label.setText(
                f"Raw sweep {raw.sweep_id or 'unknown'} · {raw.acquired_at_utc.isoformat()}"
                " · no corrected result"
            )
            self.display_changed.emit(None, None, self.frame_label.text())
        else:
            self.display_changed.emit(None, None, self._last_rejection_message
                                      or "Record corrected spectra to produce Raw − background.")

    def _clear_interference_calibrations(self):
        self._interference_calibrations = ()
        self.interference_mode.blockSignals(True)
        self.interference_mode.clear()
        self.interference_mode.addItem("Mean reference subtraction", userData=None)
        self.interference_mode.blockSignals(False)
        self._interference_selection_changed()

    def _interference_selection_changed(self, *_args):
        calibration = self.interference_mode.currentData()
        if calibration is None:
            self.interference_status.setText("Background processing: mean reference subtraction.")
        else:
            self.interference_status.setText(
                f"Background processing: {calibration.model_id} · {calibration.basis_w.shape[1]} components. "
                "Fit uses qualified control regions; uncertainty remains unqualified."
            )
        self.set_available(self._allowed)

    def _load_interference(self, dialog_parent=None):
        if self._running or self._profile_io_busy or self._profile is None or self._context is None:
            return
        path, _ = StationFileDialog.getOpenFileName(
            dialog_parent or self, "Load interference calibrations from an archive", "", "HDF5 measurement (*.h5 *.hdf5)",
        )
        if not path:
            return
        self._profile_io_busy = True
        self.recording_title.setText("Loading interference calibrations…")
        self.recording_activity.show()
        self.recording_activity.start()
        self.set_available(self._allowed)
        self._message("Checking model identity, reference dependencies and protected controls…")
        try:
            self._cpu.load_interference_calibrations(SpectrumInterferenceImportRequest(
                Path(path), self._context, self._profile, self._processor_config,
            ))
        except (ValueError, BufferError) as exc:
            self._failed("import_interference", str(exc))

    def _load_profile(self):
        path, _filter = StationFileDialog.getOpenFileName(self, "Load background profile", "", "HDF5 (*.h5)")
        if not path:
            return
        self.load_background_profile(Path(path))

    def load_background_profile(self, path: Path):
        if self._running or self._profile_io_busy:
            raise RuntimeError("Stop recording and wait for the current profile operation before loading a background.")
        self._profile_io_busy = True
        self.recording_title.setText("Loading background profile…")
        self.recording_activity.show()
        self.recording_activity.start()
        self._message(f"Reading background profile: {path}")
        self.set_available(self._allowed)
        try:
            self._cpu.load_profile(Path(path))
        except (ValueError, BufferError) as exc:
            self._failed("import_profile", str(exc))

    def _finalize_dialog(self):
        if self._running or self._profile_io_busy:
            return
        sources = []
        for title in ("Select closed SIGNAL archive", "Select completed reference BEFORE signal",
                      "Select completed reference AFTER signal"):
            path, _filter = StationFileDialog.getOpenFileName(self, title, "", "HDF5 (*.h5)")
            if not path:
                return
            sources.append(Path(path))
        destination, _filter = StationFileDialog.getSaveFileName(
            self, "Save separate finalized artifact", "finalized-spectrum.h5", "HDF5 (*.h5)",
        )
        if not destination:
            return
        self._start_finalization_request(SpectrumFinalizationRequest(*sources, Path(destination)))

    def _start_finalization_request(self, request):
        if self._running or self._profile_io_busy:
            self._message("Finish recording or current processing before finalization.")
            return
        self._profile_io_busy = True
        self.recording_title.setText("Finalizing archived spectra…")
        batch = isinstance(request, SpectrumFinalizationBatchRequest)
        resume = isinstance(request, SpectrumFinalizationResumeRequest)
        self.archive_label.setText(f"Batch journal: {request.journal_path}" if batch or resume
                                  else f"Finalization destination: {request.destination}")
        self._finalization_busy = True
        self._processing_cancel_requested = False
        self.progress.setRange(0, 0)
        self.progress.show()
        self.set_available(self._allowed)
        self._message("Finalizing archived raw sweeps between two references…")
        try:
            if resume:
                self._cpu.resume_batch(request)
            elif batch:
                self._cpu.finalize_batch(request)
            else:
                self._cpu.finalize_archives(request)
        except (ValueError, BufferError) as exc:
            self._failed("resume_batch" if resume else "finalize_batch" if batch else "finalize", str(exc))

    def _finalization_progress(self, operation, done, total):
        if operation not in {"finalize_batch", "resume_batch"} or not self._finalization_busy:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.recording_title.setText(f"Finalizing batch: {done}/{total} blocks saved")

    def _open_finalization_resume(self):
        if self._running or self._profile_io_busy:
            return
        if self._finalization_resume_dialog is not None:
            self._finalization_resume_dialog.raise_()
            return
        from .finalization_resume_dialog import SpectrumFinalizationResumeDialog

        dialog = SpectrumFinalizationResumeDialog(self)
        self._finalization_resume_dialog = dialog
        dialog.selected.connect(self._start_finalization_request)

        def finished(_result):
            dialog.shutdown()
            dialog.deleteLater()
            self._finalization_resume_dialog = None

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()

    def _open_interleaved_finalization(self):
        if self._running or self._profile_io_busy:
            return
        path = self._archive_path if self._interleaved is not None else None
        if path is None or not path.exists():
            selected, _filter = StationFileDialog.getOpenFileName(
                self, "Choose a closed alternating REF / SIGNAL archive", "", "HDF5 (*.h5)")
            if not selected:
                return
            path = Path(selected)
        self._open_finalization_selection(batch_mode=True, initial_source_path=path)

    def _open_finalization_selection(self, *, batch_mode=False, initial_source_path=None):
        if self._running or self._profile_io_busy:
            return
        if self._finalization_selection_dialog is not None:
            self._finalization_selection_dialog.raise_()
            return
        from .finalization_selection_dialog import SpectrumFinalizationSelectionDialog

        dialog = SpectrumFinalizationSelectionDialog(self, batch_mode=batch_mode, initial_source_path=initial_source_path)
        self._finalization_selection_dialog = dialog
        dialog.selected.connect(self._start_finalization_request)

        def finished(_result):
            dialog.shutdown()
            dialog.deleteLater()
            self._finalization_selection_dialog = None

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()

    def _save_profile(self):
        if self._profile is None or self._context is None:
            return
        path, _filter = StationFileDialog.getSaveFileName(self, "Export background profile", "background.h5", "HDF5 (*.h5)")
        if path:
            self._profile_io_busy = True
            self.recording_title.setText("Exporting background profile…")
            self.recording_activity.show()
            self.recording_activity.start()
            self._message(f"Saving background profile: {path}")
            self.set_available(self._allowed)
            self._cpu.save_profile(self._context, self._profile, Path(path))

    def _open_resonance_analysis(self):
        if self._running or self._profile_io_busy:
            return
        if self._resonance_dialog is not None:
            self._resonance_dialog.raise_()
            return
        from .resonance_dialog import SpectrumResonanceDialog

        dialog = SpectrumResonanceDialog(self)
        self._resonance_dialog = dialog

        def finished(_result):
            dialog.shutdown()
            dialog.deleteLater()
            self._resonance_dialog = None

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()

    def _open_difference_analysis(self):
        if self._running or self._profile_io_busy:
            return
        if self._difference_dialog is not None:
            self._difference_dialog.raise_()
            return
        from .difference_dialog import SpectrumDifferenceDialog

        dialog = SpectrumDifferenceDialog(self)
        self._difference_dialog = dialog

        def finished(_result):
            dialog.shutdown()
            dialog.deleteLater()
            self._difference_dialog = None

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()

    def _open_model_training(self, dialog_parent=None):
        if self._running or self._profile_io_busy:
            return
        if self._training_dialog is not None:
            self._training_dialog.raise_()
            return
        from .training_dialog import SpectrumTrainingDialog

        dialog = SpectrumTrainingDialog(dialog_parent or self)
        self._training_dialog = dialog

        def finished(_result):
            dialog.shutdown()
            dialog.deleteLater()
            self._training_dialog = None

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()

    def _open_model_validation(self, dialog_parent=None):
        if self._running or self._profile_io_busy:
            return
        if self._validation_dialog is not None:
            self._validation_dialog.raise_()
            return
        from .validation_dialog import SpectrumValidationDialog

        dialog = SpectrumValidationDialog(dialog_parent or self)
        self._validation_dialog = dialog

        def finished(_result):
            dialog.shutdown()
            dialog.deleteLater()
            self._validation_dialog = None

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()

    def _open_reference_diagnostics(self, dialog_parent=None):
        if self._running or self._profile_io_busy:
            return
        if self._diagnostic_dialog is not None:
            self._diagnostic_dialog.raise_()
            return
        from .diagnostic_dialog import SpectrumDiagnosticDialog

        dialog = SpectrumDiagnosticDialog(dialog_parent or self)
        if self._archive_path is not None and self._profile is not None and self._kind == "reference":
            dialog.reference.setText(str(self._archive_path))
        self._diagnostic_dialog = dialog

        def finished(_result):
            dialog.shutdown()
            dialog.deleteLater()
            self._diagnostic_dialog = None

        dialog.finished.connect(finished)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()

    def shutdown(self):
        self._start_feedback.clear_message()
        analyses_closed = True
        for dialog in (self._diagnostic_dialog, self._validation_dialog,
                       self._training_dialog, self._difference_dialog,
                       self._resonance_dialog, self._finalization_selection_dialog, self._finalization_resume_dialog):
            if dialog is not None:
                # Cancel every analysis even when another worker is still
                # draining; do not short-circuit cancellation on the first.
                closed = dialog.shutdown()
                analyses_closed = closed and analyses_closed
        self._render_timer.stop()
        self._running = False
        self._expected_device = None
        # The page/window rejects close until this returns True. Keep ownership
        # stable so a later close can inspect and wait for the same controller.
        acquisition_closed = self._cpu.close(wait_ms=50)
        return acquisition_closed and analyses_closed
