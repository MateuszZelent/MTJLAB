"""Manual-control UI for the Anritsu MS2830A module."""

# ruff: noqa: F401
from __future__ import annotations

import math
import time
from dataclasses import replace
from enum import StrEnum
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter, QSpinBox, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from app.devices.anritsu import (
    ANRITSU_PREAMPLIFIER_OPTIONS, AdvancedSpectrumConfig, AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot, ReferenceSpectrum, SignalGeneratorConfig,
    SignalGeneratorSnapshot, SpectrumConfig, SpectrumTrace, frequency_option_for,
)
from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT, DIMENSION_DBM, DIMENSION_FREQUENCY, DIMENSION_TIME,
    DIMENSION_VOLTAGE, format_quantity_auto, parse_quantity,
)
from app.recipes.parameter_registry import SWEEPABLE_PARAMETERS, sweep_default
from app.safety.anritsu import ANRITSU_SWEEP_POINT_COUNTS
from app.settings.models import StationSettings
from app.spectrum import LinearPowerAverager, apply_reference_operation, frequency_grids_match
from app.storage import ReferenceHdf5Store
from app.ui.common import line_edit as _line
from app.ui.widgets import LimitField, NotificationBanner, SpectrumPlotWidget
from app.ui.workers import DeviceController


class AnritsuPageState(StrEnum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    STARTING_LIVE = "starting_live"
    LIVE = "live"
    AVERAGING_SIGNAL = "averaging_signal"
    AVERAGING_REFERENCE = "averaging_reference"
    ACQUIRING_REFERENCE = "acquiring_reference"
    CONFIGURING = "configuring"
    STOPPING = "stopping"
    ERROR = "error"


class AnritsuSpectrumConfigurationPanel(QFrame):
    """Shared, hardware-neutral spectrum setup for manual and plan hosts."""

    def __init__(
        self,
        settings: StationSettings,
        parent: QWidget | None = None,
        *,
        plan_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("anritsuSpectrumConfigurationPanel")
        self._settings = settings
        self.plan_mode = plan_mode
        self.limit_fields: dict[str, LimitField] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(7)
        self.frequency_representation = QComboBox()
        self.frequency_representation.addItem("Start / Stop", "start_stop")
        self.frequency_representation.addItem("Center / Span", "center_span")
        self.start = _line("1 MHz")
        self.stop = _line("10 MHz")
        self.reference = _line("0 dBm")
        self.points = QComboBox()
        self.frequency_label_a = QLabel("Start")
        self.frequency_label_b = QLabel("Stop")
        form.addRow("Frequency representation", self.frequency_representation)
        form.addRow(
            self.frequency_label_a, self._bounded("frequency", self.start)
        )
        form.addRow(
            self.frequency_label_b, self._bounded("frequency", self.stop)
        )
        form.addRow(
            "Reference level", self._bounded("reference_level", self.reference)
        )
        form.addRow("Points", self.points)
        layout.addLayout(form)
        if plan_mode:
            note = QLabel(
                "Plan editing is offline. Only selected overrides are stored; "
                "no VISA command is sent to Anritsu."
            )
            note.setObjectName("recipeHint")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.frequency_representation.currentIndexChanged.connect(
            self._change_frequency_representation
        )
        self.set_settings(settings)

    def _limit_values(self, key: str) -> tuple[object, object]:
        value = getattr(self._settings.anritsu.safety, key)
        return value.min, value.max

    def _bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self._limit_values(key))
        field.setProperty("limitKey", key)
        if self.plan_mode:
            field.edit_button.setVisible(False)
        self.limit_fields[key + str(len(self.limit_fields))] = field
        return field

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        minimum, maximum = self._limit_values("sweep_points")
        current = self.points.currentData()
        self.points.clear()
        for value in ANRITSU_SWEEP_POINT_COUNTS:
            if int(minimum) <= value <= int(maximum):
                self.points.addItem(str(value), value)
        index = self.points.findData(current if current is not None else 1001)
        self.points.setCurrentIndex(index if index >= 0 else 0)
        for field in self.limit_fields.values():
            key = str(field.property("limitKey"))
            field.set_limits(*self._limit_values(key))

    def frequency_bounds(self) -> tuple[float, float]:
        first = parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value
        second = parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value
        if self.frequency_representation.currentData() == "center_span":
            if second <= 0:
                raise ConfigurationError("Frequency span must be positive.")
            return first - second / 2, first + second / 2
        return first, second

    def configuration_snapshot(self) -> AnritsuConfigurationSnapshot:
        start_hz, stop_hz = self.frequency_bounds()
        return AnritsuConfigurationSnapshot(
            start_hz=start_hz,
            stop_hz=stop_hz,
            reference_level_dbm=parse_quantity(
                self.reference.text(), DIMENSION_DBM
            ).si_value,
            points=int(self.points.currentData()),
            instrument_mode="PLAN_EDIT" if self.plan_mode else "MANUAL",
        )

    def load_snapshot(self, snapshot: AnritsuConfigurationSnapshot) -> None:
        self.frequency_representation.setCurrentIndex(
            self.frequency_representation.findData("start_stop")
        )
        self.start.setText(
            format_quantity_auto(snapshot.start_hz, DIMENSION_FREQUENCY)
        )
        self.stop.setText(
            format_quantity_auto(snapshot.stop_hz, DIMENSION_FREQUENCY)
        )
        self.reference.setText(f"{snapshot.reference_level_dbm:.9g} dBm")
        index = self.points.findData(snapshot.points)
        if index >= 0:
            self.points.setCurrentIndex(index)

    def _change_frequency_representation(self) -> None:
        try:
            first = parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value
            second = parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value
            if self.frequency_representation.currentData() == "center_span":
                self.frequency_label_a.setText("Center")
                self.frequency_label_b.setText("Span")
                self.start.setText(
                    format_quantity_auto((first + second) / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(second - first, DIMENSION_FREQUENCY)
                )
            else:
                self.frequency_label_a.setText("Start")
                self.frequency_label_b.setText("Stop")
                self.start.setText(
                    format_quantity_auto(first - second / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(first + second / 2, DIMENSION_FREQUENCY)
                )
        except Exception:
            return


class AnritsuAdvancedSpectrumPanel(QFrame):
    """Shared hardware-neutral RBW/VBW/input-path configuration panel."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hardware_options: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("anritsuAdvancedSpectrumPanel")
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.rbw_mode = QComboBox()
        self.rbw_mode.addItem("Automatic", "auto")
        self.rbw_mode.addItem("Manual", "manual")
        self.rbw = _line("1 kHz")
        form.addRow("RBW mode", self.rbw_mode)
        form.addRow("Resolution bandwidth", self.rbw)

        self.vbw_mode = QComboBox()
        self.vbw_mode.addItem("Automatic", "auto")
        self.vbw_mode.addItem("Manual", "manual")
        self.vbw_mode.addItem("Off", "off")
        self.vbw = _line("1 kHz")
        form.addRow("VBW mode", self.vbw_mode)
        form.addRow("Video bandwidth", self.vbw)

        self.detector = QComboBox()
        form.addRow("Detector", self.detector)
        self.attenuation_mode = QComboBox()
        self.attenuation_mode.addItem("Automatic", "auto")
        self.attenuation_mode.addItem("Manual", "manual")
        self.attenuation = QSpinBox()
        self.attenuation.setRange(0, 60)
        self.attenuation.setSingleStep(2)
        self.attenuation.setSuffix(" dB")
        form.addRow("RF attenuation mode", self.attenuation_mode)
        form.addRow("RF attenuation", self.attenuation)
        self.preamplifier = QCheckBox("Enable preamplifier")
        form.addRow("Input gain", self.preamplifier)

        self.sweep_time_mode = QComboBox()
        self.sweep_time_mode.addItem("Automatic", "auto")
        self.sweep_time_mode.addItem("Manual", "manual")
        self.sweep_time = _line("100 ms")
        form.addRow("Sweep-time mode", self.sweep_time_mode)
        form.addRow("Sweep time", self.sweep_time)

        self.refresh_detector_choices(hardware_options)
        self.set_hardware_options(hardware_options)
        for control in (
            self.rbw_mode,
            self.vbw_mode,
            self.attenuation_mode,
            self.sweep_time_mode,
        ):
            control.currentIndexChanged.connect(self.sync_editors)
        self.sync_editors()

    def refresh_detector_choices(self, options: tuple[str, ...]) -> None:
        current = self.detector.currentData()
        detectors = [
            ("Normal peak", "NORM"),
            ("Positive peak", "POS"),
            ("Sample", "SAMP"),
            ("Negative peak", "NEG"),
            ("RMS", "RMS"),
        ]
        if {"016", "116"}.intersection(options):
            detectors.extend(
                [
                    ("Quasi-peak", "QPE"),
                    ("CISPR average", "CAV"),
                    ("CISPR RMS", "CRMS"),
                ]
            )
        self.detector.clear()
        for label, value in detectors:
            self.detector.addItem(label, value)
        index = self.detector.findData(current or "NORM")
        self.detector.setCurrentIndex(max(index, 0))

    def set_hardware_options(self, options: tuple[str, ...]) -> None:
        self.refresh_detector_choices(options)
        has_preamp = bool(ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options))
        self.preamplifier.setEnabled(has_preamp)
        self.preamplifier.setToolTip(
            "Available because a supported preamplifier option was detected."
            if has_preamp
            else "No supported preamplifier option is currently detected."
        )

    def sync_editors(self, *_args: object) -> None:
        self.rbw.setEnabled(self.rbw_mode.currentData() == "manual")
        self.vbw.setEnabled(self.vbw_mode.currentData() == "manual")
        self.attenuation.setEnabled(
            self.attenuation_mode.currentData() == "manual"
        )
        self.sweep_time.setEnabled(
            self.sweep_time_mode.currentData() == "manual"
        )

    def configuration(self) -> AdvancedSpectrumConfig:
        return AdvancedSpectrumConfig(
            rbw_auto=self.rbw_mode.currentData() == "auto",
            rbw_hz=(
                parse_quantity(self.rbw.text(), DIMENSION_FREQUENCY).si_value
                if self.rbw_mode.currentData() == "manual"
                else None
            ),
            vbw_mode=str(self.vbw_mode.currentData()),
            vbw_hz=(
                parse_quantity(self.vbw.text(), DIMENSION_FREQUENCY).si_value
                if self.vbw_mode.currentData() == "manual"
                else None
            ),
            detector=str(self.detector.currentData()),
            attenuation_auto=self.attenuation_mode.currentData() == "auto",
            attenuation_db=(
                float(self.attenuation.value())
                if self.attenuation_mode.currentData() == "manual"
                else None
            ),
            preamplifier_enabled=self.preamplifier.isChecked(),
            sweep_time_auto=self.sweep_time_mode.currentData() == "auto",
            sweep_time_s=(
                parse_quantity(self.sweep_time.text(), DIMENSION_TIME).si_value
                if self.sweep_time_mode.currentData() == "manual"
                else None
            ),
        )

    def load_snapshot(self, snapshot: AdvancedSpectrumSnapshot) -> None:
        for combo, value in (
            (self.rbw_mode, "auto" if snapshot.rbw_auto else "manual"),
            (self.vbw_mode, snapshot.vbw_mode),
            (
                self.attenuation_mode,
                "auto" if snapshot.attenuation_auto else "manual",
            ),
            (
                self.sweep_time_mode,
                "auto" if snapshot.sweep_time_auto else "manual",
            ),
        ):
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
        self.rbw.setText(format_quantity_auto(snapshot.rbw_hz, DIMENSION_FREQUENCY))
        if snapshot.vbw_hz is not None:
            self.vbw.setText(
                format_quantity_auto(snapshot.vbw_hz, DIMENSION_FREQUENCY)
            )
        detector_index = self.detector.findData(snapshot.detector)
        if detector_index >= 0:
            self.detector.setCurrentIndex(detector_index)
        self.attenuation.setValue(round(snapshot.attenuation_db))
        self.preamplifier.setChecked(snapshot.preamplifier_enabled)
        self.sweep_time.setText(
            format_quantity_auto(snapshot.sweep_time_s, DIMENSION_TIME)
        )
        self.sync_editors()


class AnritsuPage(QWidget):
    status = Signal(str)

    def __init__(
        self,
        controller: DeviceController,
        settings: StationSettings,
        *,
        single_sweep_available: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[str, LimitField] = {}
        self._single_sweep_configured = single_sweep_available
        self._trace_supported = True
        self._fetch_pending = False
        self._live_transition_pending = False
        self._latest_trace: SpectrumTrace | None = None
        self._averaged_trace: SpectrumTrace | None = None
        self._reference_trace: SpectrumTrace | None = None
        self._reference_spectrum: ReferenceSpectrum | None = None
        self._pending_reference_kind: str | None = None
        self._device_idn = ""
        self._last_configuration: AnritsuConfigurationSnapshot | None = None
        self._last_advanced_configuration: AdvancedSpectrumSnapshot | None = None
        self._page_state = AnritsuPageState.IDLE
        self._capabilities: object | None = None
        self._averager = LinearPowerAverager()
        self._averaging_active = False
        self._averaging_destination: str | None = None
        self._resume_live_after_averaging = False
        self._live_frame_count = 0
        self._fetch_started_monotonic: float | None = None
        self._last_frame_monotonic: float | None = None
        self._frame_intervals_s: list[float] = []
        self._transfer_durations_s: list[float] = []
        self._stale_frame_count = 0
        self._coalesced_timer_ticks = 0
        self._identical_live_frames = 0
        self._last_live_signature: int | None = None
        self._reconnect_pending = False
        self._sg_supported = False
        self._sg_output_enabled = False
        self._sg_armed = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.fetch_live)
        layout = QVBoxLayout(self)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)
        title_row = QHBoxLayout()
        title = QLabel("Anritsu MS2830A — Spectrum / Live")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.live_indicator = QLabel("●  LIVE OFF")
        self.live_indicator.setObjectName("anritsuLiveIndicator")
        self.live_indicator.setProperty("liveState", "off")
        self.live_indicator.setToolTip(
            "Confirmed Live acquisition state. The indicator changes to ON only after the "
            "instrument accepts Live startup."
        )
        title_row.addWidget(self.live_indicator)
        layout.addLayout(title_row)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("anritsuWorkspaceSplitter")
        left_panel = QWidget()
        left_panel.setObjectName("anritsuControlPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(10)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)
        setup_header = QHBoxLayout()
        setup_title = QLabel("Acquisition setup")
        setup_title.setObjectName("sectionTitle")
        setup_header.addWidget(setup_title)
        setup_header.addStretch(1)
        self.advanced_spectrum_button = QPushButton("Advanced…")
        self.advanced_spectrum_button.setToolTip(
            "Open qualified RBW, VBW, detector, attenuation, preamplifier and sweep-time controls."
        )
        self.advanced_spectrum_button.clicked.connect(self._show_advanced_spectrum_dialog)
        setup_header.addWidget(self.advanced_spectrum_button)
        self.hardware_info_button = QPushButton("ⓘ")
        self.hardware_info_button.setObjectName("infoButton")
        self.hardware_info_button.setFixedSize(28, 28)
        self.hardware_info_button.setToolTip(
            "Show detected Anritsu hardware options and documented operating limits."
        )
        self.hardware_info_button.clicked.connect(self._show_anritsu_hardware_info)
        setup_header.addWidget(self.hardware_info_button)
        self._advanced_dialog = self._build_advanced_spectrum_dialog()
        left_layout.addLayout(setup_header)
        self.configuration_panel = AnritsuSpectrumConfigurationPanel(
            settings, self
        )
        self.start = self.configuration_panel.start
        self.stop = self.configuration_panel.stop
        self.frequency_representation = (
            self.configuration_panel.frequency_representation
        )
        self.reference = self.configuration_panel.reference
        self.points = self.configuration_panel.points
        self.frequency_label_a = self.configuration_panel.frequency_label_a
        self.frequency_label_b = self.configuration_panel.frequency_label_b
        self._limit_fields = self.configuration_panel.limit_fields
        left_layout.addWidget(self.configuration_panel)
        self.refresh = QSpinBox()
        self.refresh.setRange(10, 5000)
        self.refresh.setValue(500)
        self.refresh.setSuffix(" ms")
        self.refresh.setToolTip(
            "Requested Live polling interval: 10 ms to 5 s. The effective frame rate is "
            "limited by the analyser sweep, VISA transfer and complete TRAC1 processing."
        )
        refresh_form = QFormLayout()
        refresh_form.addRow("Live refresh interval", self.refresh)
        left_layout.addLayout(refresh_form)
        self.hardware_option_info = QLabel()
        self.hardware_range_info = QLabel()
        self.hardware_option_info.hide()
        self.hardware_range_info.hide()
        self._hardware_details_text = ""
        self._update_anritsu_hardware_limits(())
        controls = QGridLayout()
        controls.setSpacing(6)
        self.read_configuration = QPushButton("Read from instrument")
        self.configure_button = QPushButton("Apply configuration")
        self.single = QPushButton("Read current spectrum")
        self.live = QPushButton("Start Live")
        self.abort_button = QPushButton("Abort acquisition")
        self.configure_button.setObjectName("primaryButton")
        self.abort_button.setObjectName("warningButton")
        for button in (
            self.read_configuration,
            self.configure_button,
            self.single,
            self.live,
            self.abort_button,
        ):
            button.setProperty("compact", True)
        controls.addWidget(self.read_configuration, 0, 0, 1, 2)
        controls.addWidget(self.configure_button, 1, 0)
        controls.addWidget(self.single, 1, 1)
        controls.addWidget(self.live, 2, 0)
        controls.addWidget(self.abort_button, 2, 1)
        left_layout.addLayout(controls)
        processing = QFrame()
        processing.setObjectName("anritsuProcessingCard")
        processing_layout = QGridLayout(processing)
        processing_title = QLabel("Averaging and reference processing")
        processing_title.setObjectName("sectionTitle")
        processing_layout.setHorizontalSpacing(6)
        processing_layout.setVerticalSpacing(7)
        processing_layout.addWidget(processing_title, 0, 0, 1, 2)
        self.average_count = QSpinBox()
        self.average_count.setRange(1, 9999)
        self.average_count.setValue(self._station_settings.anritsu.acquisition.application_average_count)
        self.acquire_average = QPushButton("Acquire averaged spectrum")
        self.cancel_average = QPushButton("Cancel averaging")
        self.acquire_average.setProperty("compact", True)
        self.cancel_average.setProperty("compact", True)
        self.cancel_average.setEnabled(False)
        self.average_progress = QProgressBar()
        initial_average_count = self.average_count.value()
        self.average_progress.setRange(0, initial_average_count)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {initial_average_count}")
        processing_layout.addWidget(QLabel("Average count"), 1, 0)
        processing_layout.addWidget(self.average_count, 1, 1)
        processing_layout.addWidget(self.acquire_average, 2, 0)
        processing_layout.addWidget(self.cancel_average, 2, 1)
        processing_layout.addWidget(self.average_progress, 3, 0, 1, 2)
        reference_title = QLabel("Reference")
        reference_title.setObjectName("subsectionTitle")
        processing_layout.addWidget(reference_title, 4, 0, 1, 2)
        self.reference_status = QLabel("No reference")
        self.reference_status.setObjectName("muted")
        self.reference_status.setWordWrap(True)
        processing_layout.addWidget(self.reference_status, 5, 0, 1, 2)
        self.acquire_single_reference = QPushButton("Acquire 1× reference")
        self.use_current_reference = QPushButton("Use current trace")
        self.capture_reference = QPushButton("Acquire N× reference")
        self.clear_reference = QPushButton("Clear reference")
        self.load_reference = QPushButton("Load reference…")
        self.save_reference = QPushButton("Save reference…")
        for button in (
            self.acquire_single_reference,
            self.use_current_reference,
            self.capture_reference,
            self.clear_reference,
            self.load_reference,
            self.save_reference,
        ):
            button.setProperty("compact", True)
        self.use_current_reference.setEnabled(False)
        self.clear_reference.setEnabled(False)
        self.reference_operation = QComboBox()
        self.reference_operation.addItem("No processing", "none")
        self.reference_operation.addItem("Signal − reference [dB]", "difference_db")
        self.reference_operation.addItem("Signal ÷ reference [linear ratio]", "ratio_linear")
        self.reference_operation.addItem("Signal + reference [linear power]", "add_power")
        self.reference_operation.addItem("Signal − reference [linear power]", "subtract_power")
        self.reference_operation.addItem("Signal × reference [linear mW²]", "multiply_linear")
        processing_layout.addWidget(self.acquire_single_reference, 6, 0)
        processing_layout.addWidget(self.use_current_reference, 6, 1)
        processing_layout.addWidget(self.capture_reference, 7, 0)
        processing_layout.addWidget(self.clear_reference, 7, 1)
        processing_layout.addWidget(self.load_reference, 8, 0)
        processing_layout.addWidget(self.save_reference, 8, 1)
        processing_layout.addWidget(QLabel("Reference operation"), 9, 0)
        processing_layout.addWidget(self.reference_operation, 9, 1)
        self.show_raw = QCheckBox("Raw")
        self.show_raw.setChecked(True)
        self.show_average = QCheckBox("Averaged")
        self.show_reference = QCheckBox("Reference")
        self.show_processed = QCheckBox("Processed")
        trace_toggles = QHBoxLayout()
        trace_toggles.setSpacing(10)
        for checkbox in (self.show_raw, self.show_average, self.show_reference, self.show_processed):
            trace_toggles.addWidget(checkbox)
        trace_toggles.addStretch(1)
        processing_layout.addLayout(trace_toggles, 10, 0, 1, 2)
        left_layout.addWidget(processing)
        left_layout.addStretch(1)
        self.spectrum_plot = SpectrumPlotWidget(legend=True)
        self.spectrum_plot.set_title("Current spectrum")
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit="dBm"
        )
        self.spectrum_plot.setMinimumHeight(300)
        self.spectrum_plot.status_changed.connect(self.status.emit)
        right_layout.addWidget(self.spectrum_plot, 1)
        self.info = QLabel("Live stopped. Each frame is a complete trace, not a push stream.")
        self.info.setObjectName("muted")
        right_layout.addWidget(self.info)
        left_scroll = QScrollArea()
        left_scroll.setObjectName("anritsuControlScroll")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_panel)
        left_scroll.setMinimumWidth(320)
        self.workspace_splitter.addWidget(left_scroll)
        self.workspace_splitter.addWidget(right_panel)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([680, 1100])
        self.mode_tabs = QTabWidget()
        self.mode_tabs.setObjectName("anritsuModeTabs")
        spectrum_tab = QWidget()
        spectrum_tab_layout = QVBoxLayout(spectrum_tab)
        spectrum_tab_layout.setContentsMargins(0, 0, 0, 0)
        spectrum_tab_layout.addWidget(self.workspace_splitter)
        self.mode_tabs.addTab(spectrum_tab, "Spectrum analyser")
        self.signal_generator_tab = self._build_signal_generator_tab()
        self.signal_generator_tab_index = self.mode_tabs.addTab(
            self.signal_generator_tab, "Signal generator"
        )
        self.mode_tabs.setTabVisible(self.signal_generator_tab_index, False)
        layout.addWidget(self.mode_tabs, 1)
        self.read_configuration.clicked.connect(self.read_configuration_from_instrument)
        self.configure_button.clicked.connect(self.configure)
        self.single.clicked.connect(self.read_once)
        self.live.clicked.connect(self.toggle_live)
        self.abort_button.clicked.connect(lambda: self._controller.call("emergency_off"))
        self.acquire_average.clicked.connect(self.start_averaging)
        self.cancel_average.clicked.connect(self.cancel_averaging)
        self.acquire_single_reference.clicked.connect(self.acquire_reference_once)
        self.use_current_reference.clicked.connect(self.capture_current_reference)
        self.capture_reference.clicked.connect(self.start_reference_averaging)
        self.clear_reference.clicked.connect(self.remove_reference)
        self.load_reference.clicked.connect(self.load_reference_file)
        self.save_reference.clicked.connect(self.save_reference_file)
        self.reference_operation.currentIndexChanged.connect(self._refresh_spectrum_display)
        for checkbox in (self.show_raw, self.show_average, self.show_reference, self.show_processed):
            checkbox.toggled.connect(self._refresh_spectrum_display)
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._device_state_changed)
        help_items = {
            self.read_configuration: "Read Start, Stop, Reference level, and Points from the connected analyser. This sends query commands only and never changes the instrument or approved safety limits.",
            self.single: "Read the currently displayed TRAC1 spectrum using SCPI queries only. This does not configure or trigger the analyser and does not require an approved safety profile.",
            self.average_count: "Number of complete spectra to average. 200 is common in the Thatec workflow. Averaging is performed in linear mW, not directly in dBm.",
            self.acquire_average: "Passively read N traces at the Live refresh interval and average power in linear mW. No analyser setting or trigger mode is changed.",
            self.cancel_average: "Stop temporal averaging. Already collected temporary frames are discarded; completed raw/reference data are unchanged.",
            self.acquire_single_reference: "Passively fetch one new TRAC1 frame and store that completed frame as the reference. No analyser setting is changed.",
            self.use_current_reference: "Use the latest already acquired trace as the reference without sending a VISA command.",
            self.capture_reference: "Passively acquire and average N traces, then store that completed average as the in-memory reference spectrum.",
            self.clear_reference: "Remove the in-memory reference and all derived display results. It does not delete raw measurements from HDF5.",
            self.load_reference: "Load a Lab Control reference HDF5 artefact. The current analyser is not queried or configured.",
            self.save_reference: "Save the complete reference trace and provenance as a thaTEC/PyThat-compatible HDF5 artefact.",
            self.reference_operation: "Choose point-wise reference mathematics. Difference in dB equals a power ratio expressed logarithmically; linear operations first convert dBm to mW.",
            self.show_raw: "Show the latest untouched trace returned by Anritsu.",
            self.show_average: "Show the application-side linear-power average.",
            self.show_reference: "Overlay the captured reference spectrum.",
            self.show_processed: "Show the selected reference operation result. Non-dBm results use their own Y-axis unit and hide incompatible overlays.",
        }
        for widget, description in help_items.items():
            widget.setToolTip(description)
            widget.setToolTipDuration(25_000)
        self._apply_page_state()

    def _build_signal_generator_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(18, 14, 18, 14)
        heading = QLabel("Optional vector signal generator")
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)
        explanation = QLabel(
            "This panel is shown only when *OPT? reports option 020/120/021/121. "
            "Configuration explicitly enters SG mode and proves RF OUTPUT OFF. "
            "RF ON additionally requires a qualified protocol, approved limits, profile approval "
            "and a fresh one-shot ARM."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        outer.addWidget(explanation)

        card = QFrame()
        card.setObjectName("anritsuProcessingCard")
        grid = QGridLayout(card)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        self.sg_status = QLabel("●  RF OUTPUT UNKNOWN")
        self.sg_status.setObjectName("anritsuSgIndicator")
        self.sg_status.setProperty("liveState", "off")
        grid.addWidget(self.sg_status, 0, 0, 1, 4)
        generator = self._station_settings.anritsu.signal_generator
        default_frequency = generator.frequency.min or "1 GHz"
        default_power = generator.power.min or "-30 dBm"
        self.sg_frequency = QLineEdit(str(default_frequency))
        self.sg_power = QLineEdit(str(default_power))
        grid.addWidget(QLabel("Frequency"), 1, 0)
        grid.addWidget(self.sg_frequency, 1, 1, 1, 3)
        grid.addWidget(QLabel("RF power"), 2, 0)
        grid.addWidget(self.sg_power, 2, 1, 1, 3)
        self.sg_read = QPushButton("Read current SG state")
        self.sg_configure = QPushButton("Configure while RF OFF")
        self.sg_arm = QPushButton("ARM RF output")
        self.sg_on = QPushButton("RF OUTPUT ON")
        self.sg_off = QPushButton("RF OUTPUT OFF")
        self.sg_configure.setObjectName("primaryButton")
        self.sg_on.setObjectName("outputOnButton")
        self.sg_off.setObjectName("outputOffButton")
        for button in (
            self.sg_read,
            self.sg_configure,
            self.sg_arm,
            self.sg_on,
            self.sg_off,
        ):
            button.setProperty("compact", True)
        grid.addWidget(self.sg_read, 3, 0, 1, 2)
        grid.addWidget(self.sg_configure, 3, 2, 1, 2)
        grid.addWidget(self.sg_arm, 4, 0)
        grid.addWidget(self.sg_on, 4, 1)
        grid.addWidget(self.sg_off, 4, 2, 1, 2)
        self.sg_limits = QLabel()
        self.sg_limits.setWordWrap(True)
        self.sg_limits.setObjectName("muted")
        grid.addWidget(self.sg_limits, 5, 0, 1, 4)
        outer.addWidget(card)
        outer.addStretch(1)
        self.sg_read.clicked.connect(
            lambda: self._controller.call("read_signal_generator")
        )
        self.sg_configure.clicked.connect(self.configure_signal_generator)
        self.sg_arm.clicked.connect(self.arm_signal_generator)
        self.sg_on.clicked.connect(self.enable_signal_generator)
        self.sg_off.clicked.connect(
            lambda: self._controller.call("set_signal_generator_output", False)
        )
        self._update_signal_generator_limits()
        return tab

    def _build_advanced_spectrum_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("Anritsu advanced Spectrum settings")
        dialog.setModal(False)
        dialog.resize(620, 470)
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "These controls change bandwidth, detector and the RF input path. Readback is "
            "always available as an explicit diagnostic action. Apply remains locked until "
            "the exact firmware is qualified in the station profile."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.advanced_protocol_status = QLabel()
        self.advanced_protocol_status.setWordWrap(True)
        layout.addWidget(self.advanced_protocol_status)

        self.advanced_configuration_panel = AnritsuAdvancedSpectrumPanel(dialog)
        self.advanced_rbw_mode = self.advanced_configuration_panel.rbw_mode
        self.advanced_rbw = self.advanced_configuration_panel.rbw
        self.advanced_vbw_mode = self.advanced_configuration_panel.vbw_mode
        self.advanced_vbw = self.advanced_configuration_panel.vbw
        self.advanced_detector = self.advanced_configuration_panel.detector
        self.advanced_attenuation_mode = (
            self.advanced_configuration_panel.attenuation_mode
        )
        self.advanced_attenuation = self.advanced_configuration_panel.attenuation
        self.advanced_preamplifier = self.advanced_configuration_panel.preamplifier
        self.advanced_sweep_mode = (
            self.advanced_configuration_panel.sweep_time_mode
        )
        self.advanced_sweep_time = self.advanced_configuration_panel.sweep_time
        layout.addWidget(self.advanced_configuration_panel)

        help_text = QLabel(
            "Documented limits: RBW 1 Hz–31.25 MHz; VBW 1 Hz–10 MHz or Off; "
            "attenuation 0–60 dB in 2 dB steps; frequency-domain sweep 1 ms–1000 s. "
            "Automatic attenuation is blocked when the safety profile defines a minimum."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        actions = QHBoxLayout()
        self.advanced_read_button = QPushButton("Read from instrument")
        self.advanced_apply_button = QPushButton("Apply and verify")
        self.advanced_apply_button.setObjectName("primaryButton")
        close_button = QPushButton("Close")
        actions.addWidget(self.advanced_read_button)
        actions.addWidget(self.advanced_apply_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.advanced_read_button.clicked.connect(self.read_advanced_spectrum)
        self.advanced_apply_button.clicked.connect(self.configure_advanced_spectrum)
        close_button.clicked.connect(dialog.hide)
        self._sync_advanced_editors()
        self._update_advanced_availability()
        return dialog

    def _refresh_advanced_detector_choices(self, options: tuple[str, ...]) -> None:
        if hasattr(self, "advanced_configuration_panel"):
            self.advanced_configuration_panel.set_hardware_options(options)

    def _sync_advanced_editors(self) -> None:
        self.advanced_configuration_panel.sync_editors()

    def _advanced_firmware_qualified(self) -> bool:
        protocol = self._station_settings.anritsu.advanced_spectrum
        firmware = str(getattr(self._capabilities, "firmware", "") or "")
        return (
            protocol.control_protocol == "standard_scpi"
            and firmware in protocol.qualified_firmware
        )

    def _update_advanced_availability(self) -> None:
        if not hasattr(self, "advanced_protocol_status"):
            return
        protocol = self._station_settings.anritsu.advanced_spectrum
        firmware = str(getattr(self._capabilities, "firmware", "") or "unknown")
        qualified = self._advanced_firmware_qualified()
        if qualified:
            text = f"Qualified standard SCPI control for firmware {firmware}."
        else:
            versions = ", ".join(protocol.qualified_firmware) or "none"
            text = (
                f"WRITE LOCKED — protocol={protocol.control_protocol}, connected firmware={firmware}, "
                f"qualified firmware={versions}. Read-only queries remain available."
            )
        self.advanced_protocol_status.setText(text)
        connected = self._page_state != AnritsuPageState.DISCONNECTED
        idle = self._page_state in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}
        self.advanced_read_button.setEnabled(connected and idle)
        self.advanced_apply_button.setEnabled(connected and idle and qualified)

    def _show_advanced_spectrum_dialog(self) -> None:
        self._update_advanced_availability()
        self._advanced_dialog.show()
        self._advanced_dialog.raise_()
        self._advanced_dialog.activateWindow()

    def read_advanced_spectrum(self) -> None:
        self._set_page_state(AnritsuPageState.CONFIGURING)
        self.status.emit("Anritsu advanced Spectrum readback requested")
        self._controller.call("read_advanced_spectrum")

    def configure_advanced_spectrum(self) -> None:
        try:
            config = self.advanced_configuration_panel.configuration()
        except Exception as exc:
            self.banner.show_message(f"Invalid advanced Spectrum settings: {exc}", severity="error")
            return
        if config.preamplifier_enabled:
            answer = QMessageBox.warning(
                self,
                "Enable Anritsu preamplifier",
                "The preamplifier changes the RF input path and may overload at high input power. "
                "Confirm that the approved expected input and attenuation are correct.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return
        self._set_page_state(AnritsuPageState.CONFIGURING)
        self._controller.call("configure_advanced_spectrum", config)

    def _show_advanced_snapshot(self, snapshot: AdvancedSpectrumSnapshot) -> None:
        self._last_advanced_configuration = snapshot
        self.advanced_configuration_panel.load_snapshot(snapshot)

    def _anritsu_limit_values(self, key: str) -> tuple[object, object]:
        safety = self._station_settings.anritsu.safety
        value = getattr(safety, key)
        return value.min, value.max

    def _spectrum_frequency_bounds(self) -> tuple[float, float]:
        first = parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value
        second = parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value
        if self.frequency_representation.currentData() == "center_span":
            center, span = first, second
            if not math.isfinite(span) or span <= 0:
                raise ValueError("Frequency span must be finite and positive.")
            return center - span / 2, center + span / 2
        return first, second

    def _set_frequency_bounds(self, start_hz: float, stop_hz: float) -> None:
        if self.frequency_representation.currentData() == "center_span":
            self.start.setText(
                format_quantity_auto((start_hz + stop_hz) / 2, DIMENSION_FREQUENCY)
            )
            self.stop.setText(
                format_quantity_auto(stop_hz - start_hz, DIMENSION_FREQUENCY)
            )
        else:
            self.start.setText(format_quantity_auto(start_hz, DIMENSION_FREQUENCY))
            self.stop.setText(format_quantity_auto(stop_hz, DIMENSION_FREQUENCY))

    def _change_frequency_representation(self) -> None:
        try:
            if self.frequency_representation.currentData() == "center_span":
                start_hz = parse_quantity(
                    self.start.text(), DIMENSION_FREQUENCY
                ).si_value
                stop_hz = parse_quantity(
                    self.stop.text(), DIMENSION_FREQUENCY
                ).si_value
                self.frequency_label_a.setText("Center")
                self.frequency_label_b.setText("Span")
                self.start.setText(
                    format_quantity_auto((start_hz + stop_hz) / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(stop_hz - start_hz, DIMENSION_FREQUENCY)
                )
            else:
                center_hz = parse_quantity(
                    self.start.text(), DIMENSION_FREQUENCY
                ).si_value
                span_hz = parse_quantity(
                    self.stop.text(), DIMENSION_FREQUENCY
                ).si_value
                self.frequency_label_a.setText("Start")
                self.frequency_label_b.setText("Stop")
                self.start.setText(
                    format_quantity_auto(center_hz - span_hz / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(center_hz + span_hz / 2, DIMENSION_FREQUENCY)
                )
        except Exception as exc:
            self.banner.show_message(
                f"Cannot change frequency representation: {exc}", severity="error"
            )

    def _refresh_point_choices(self, preferred: int | None = None) -> None:
        minimum, maximum = self._anritsu_limit_values("sweep_points")
        current = preferred if preferred is not None else self.points.currentData()
        self.points.clear()
        for value in ANRITSU_SWEEP_POINT_COUNTS:
            if int(minimum) <= value <= int(maximum):
                self.points.addItem(str(value), value)
        index = self.points.findData(current)
        self.points.setCurrentIndex(index if index >= 0 else 0)

    def _anritsu_bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self._anritsu_limit_values(key))
        self._limit_fields[key + str(len(self._limit_fields))] = field
        field.setProperty("limitKey", key)
        return field

    def set_settings(self, settings: StationSettings) -> None:
        self._station_settings = settings
        self.configuration_panel.set_settings(settings)
        self._update_signal_generator_limits()
        self._update_advanced_availability()
        self._apply_page_state()

    def set_capabilities(self, capabilities: object) -> None:
        supports = getattr(capabilities, "supports", lambda _feature: False)
        self._capabilities = capabilities
        self._trace_supported = bool(supports("spectrum_trace"))
        self._sg_supported = bool(supports("signal_generator"))
        options = tuple(getattr(capabilities, "hardware_options", ()) or ())
        self._update_anritsu_hardware_limits(options)
        self._refresh_advanced_detector_choices(options)
        self.advanced_preamplifier.setEnabled(
            bool(ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options))
        )
        self.mode_tabs.setTabVisible(self.signal_generator_tab_index, self._sg_supported)
        if not self._sg_supported and self.mode_tabs.currentIndex() == self.signal_generator_tab_index:
            self.mode_tabs.setCurrentIndex(0)
        self._update_advanced_availability()
        self._apply_page_state()

    def _device_state_changed(self, state: str) -> None:
        if state == "disconnected":
            self._sg_armed = False
            self._sg_output_enabled = False
            self._last_advanced_configuration = None
            self.sg_status.setText("●  RF OUTPUT UNKNOWN")
            self.sg_status.setProperty("liveState", "off")
            self._set_page_state(AnritsuPageState.DISCONNECTED)
        elif state in {"fault", "unknown"}:
            self._set_page_state(AnritsuPageState.ERROR)
        elif self._page_state in {
            AnritsuPageState.DISCONNECTED,
            AnritsuPageState.ERROR,
        }:
            self._set_page_state(AnritsuPageState.IDLE)

    def _set_page_state(self, state: AnritsuPageState) -> None:
        self._page_state = state
        self._apply_page_state()

    def _apply_page_state(self) -> None:
        idle = self._page_state in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}
        live = self._page_state == AnritsuPageState.LIVE
        averaging = self._page_state in {
            AnritsuPageState.AVERAGING_SIGNAL,
            AnritsuPageState.AVERAGING_REFERENCE,
        }
        connected = self._page_state != AnritsuPageState.DISCONNECTED
        self.read_configuration.setEnabled(idle)
        self.configure_button.setEnabled(idle)
        self.single.setEnabled(idle and self._trace_supported)
        self.live.setEnabled((idle or live) and connected and not self._live_transition_pending)
        self.abort_button.setEnabled(connected)
        self.advanced_spectrum_button.setEnabled(connected and idle)
        self.average_count.setEnabled(idle and not averaging)
        self.acquire_average.setEnabled(idle and self._trace_supported)
        self.acquire_single_reference.setEnabled(idle and self._trace_supported)
        self.use_current_reference.setEnabled(idle and self._latest_trace is not None)
        self.capture_reference.setEnabled(idle and self._trace_supported)
        self.load_reference.setEnabled(idle)
        self.save_reference.setEnabled(idle and self._reference_spectrum is not None)
        self.cancel_average.setEnabled(averaging)
        self.clear_reference.setEnabled(
            self._reference_spectrum is not None
            and self._page_state not in {
                AnritsuPageState.STARTING_LIVE,
                AnritsuPageState.STOPPING,
                AnritsuPageState.ACQUIRING_REFERENCE,
            }
        )
        protocol_qualified = (
            self._station_settings.anritsu.signal_generator.control_protocol
            == "basic_scpi"
        )
        self.sg_read.setEnabled(connected and idle and self._sg_supported)
        self.sg_configure.setEnabled(
            connected and idle and self._sg_supported and protocol_qualified
        )
        self.sg_arm.setEnabled(
            connected
            and idle
            and self._sg_supported
            and protocol_qualified
            and not self._sg_output_enabled
        )
        self.sg_on.setEnabled(
            connected
            and idle
            and self._sg_supported
            and protocol_qualified
            and self._sg_armed
            and not self._sg_output_enabled
        )
        self.sg_off.setEnabled(connected and self._sg_supported)
        self._update_advanced_availability()

    def _update_signal_generator_limits(self) -> None:
        generator = self._station_settings.anritsu.signal_generator
        protocol = generator.control_protocol
        frequency = (
            f"{generator.frequency.min} … {generator.frequency.max}"
            if generator.frequency.min is not None
            else "not defined"
        )
        power = (
            f"{generator.power.min} … {generator.power.max}"
            if generator.power.min is not None
            else "not defined"
        )
        permission = self._station_settings.anritsu.safety.signal_generator_output_allowed
        self.sg_limits.setText(
            f"Protocol: {protocol} | Approved frequency: {frequency} | Approved RF power: "
            f"{power} | RF output permission: {'enabled' if permission else 'disabled'}"
        )

    def configure_signal_generator(self) -> None:
        try:
            config = SignalGeneratorConfig(
                frequency_hz=parse_quantity(
                    self.sg_frequency.text(), DIMENSION_FREQUENCY
                ).si_value,
                power_dbm=parse_quantity(self.sg_power.text(), DIMENSION_DBM).si_value,
            )
        except Exception as exc:
            self.banner.show_message(f"Invalid signal-generator settings: {exc}")
            return
        self._sg_armed = False
        self._controller.call("configure_signal_generator", config)

    def arm_signal_generator(self) -> None:
        answer = QMessageBox.warning(
            self,
            "ARM Anritsu RF output",
            "ARM permits one RF OUTPUT ON action for a short time. Confirm the RF cable, "
            "dummy load/DUT power rating, attenuation and emergency stop before continuing.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Ok:
            self._controller.call("arm_signal_generator")

    def enable_signal_generator(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Enable Anritsu RF output?",
            f"Enable RF at {self.sg_frequency.text()} and {self.sg_power.text()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._controller.call("set_signal_generator_output", True)

    def _show_signal_generator_snapshot(self, result: SignalGeneratorSnapshot) -> None:
        self.sg_frequency.setText(
            format_quantity_auto(result.frequency_hz, DIMENSION_FREQUENCY)
        )
        self.sg_power.setText(f"{result.power_dbm:.9g} dBm")
        self._sg_output_enabled = result.output_enabled
        self._sg_armed = False
        state = "on" if result.output_enabled else "off"
        self.sg_status.setText(
            "●  RF OUTPUT ON" if result.output_enabled else "●  RF OUTPUT OFF"
        )
        self.sg_status.setProperty("liveState", state)
        self.sg_status.style().unpolish(self.sg_status)
        self.sg_status.style().polish(self.sg_status)
        self._apply_page_state()

    def signal_generator_snapshot(self) -> SignalGeneratorSnapshot:
        """Expose the manual SG form as a read-only seed for the sweep editor."""

        return SignalGeneratorSnapshot(
            frequency_hz=parse_quantity(
                self.sg_frequency.text(), DIMENSION_FREQUENCY
            ).si_value,
            power_dbm=parse_quantity(self.sg_power.text(), DIMENSION_DBM).si_value,
            output_enabled=self._sg_output_enabled,
            instrument_mode="PLAN_EDIT",
        )

    def _update_anritsu_hardware_limits(self, options: tuple[str, ...]) -> None:
        frequency_option = frequency_option_for(options)
        if options:
            option_text = ", ".join(options)
            preamplifier = (
                "installed option detected"
                if ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options)
                else "no preamplifier option reported"
            )
            self.hardware_option_info.setText(
                f"Auto-detected by *OPT?: {option_text} | Preamplifier: {preamplifier}."
            )
        else:
            self.hardware_option_info.setText(
                "Hardware options: waiting for connection, or *OPT? was not supported/reported."
            )
        if frequency_option is None:
            frequency_text = (
                "Frequency: option dependent (040: 3.7 GHz, 041: 6.1 GHz, "
                "043: 13.6 GHz, 044: 26.6 GHz, 045: 43.1 GHz)."
            )
            default_sweep_text = "option-dependent"
        else:
            frequency_text = (
                f"Frequency option {frequency_option.code}: documented displayed range "
                f"-100 MHz to {frequency_option.maximum_stop_hz / 1e9:g} GHz."
            )
            default_sweep_text = f"{frequency_option.default_sweep_time_s * 1e3:g} ms"
        self.hardware_range_info.setText(
            f"{frequency_text}\n"
            "Reference level: -120 to +50 dBm (0.01 dB resolution) | "
            "RF attenuation: 0 to 60 dB (2 dB steps).\n"
            "RBW: 1 Hz to 31.25 MHz | VBW: 1 Hz to 10 MHz or OFF | "
            "Input impedance: 50 or 75 ohm.\n"
            f"Sweep time: 1 ms to 1000 s in frequency mode; default for this option: "
            f"{default_sweep_text}. Zero Span: 1 us to 1000 s.\n"
            "Trace points: 11, 21, 41, 51, 101, 201, 251, 401, 501, 1001, 2001, "
            "5001, 10001 | Device averaging: 2 to 9999.\n"
            "Application polling: 10 ms to 5 s | Application averaging: 1 to 9999. "
            "Approved safety badges above may intentionally be stricter."
        )
        self._hardware_details_text = (
            "Detected hardware options\n"
            f"{self.hardware_option_info.text()}\n\n"
            "Documented instrument limits\n"
            f"{self.hardware_range_info.text()}"
        )

    def _show_anritsu_hardware_info(self) -> None:
        QMessageBox.information(
            self,
            "Anritsu hardware information",
            self._hardware_details_text or "Hardware information is not available yet.",
        )

    def configure(self) -> None:
        try:
            start_hz, stop_hz = self._spectrum_frequency_bounds()
            config = SpectrumConfig(
                start_hz=start_hz,
                stop_hz=stop_hz,
                reference_level_dbm=parse_quantity(self.reference.text(), DIMENSION_DBM).si_value,
                points=int(self.points.currentData()),
            )
        except Exception as exc:
            self.banner.show_message(f"Invalid spectrum settings: {exc}")
            return
        self._controller.call("configure", config)

    def read_configuration_from_instrument(self) -> None:
        self.status.emit("Anritsu current-configuration read requested")
        self._controller.call("read_configuration")

    def read_once(self) -> None:
        if self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
            return
        self._request_trace()

    def _request_trace(self) -> bool:
        if self._fetch_pending:
            self._coalesced_timer_ticks += 1
            return False
        self._fetch_pending = True
        self._fetch_started_monotonic = time.monotonic()
        self._controller.call("fetch_current_trace", "TRAC1")
        return True

    def toggle_live(self) -> None:
        if self._live_transition_pending:
            return
        if self._timer.isActive():
            self._timer.stop()
            self._live_transition_pending = True
            self.live.setText("Stopping…")
            self._set_live_indicator("stopping")
            self._set_page_state(AnritsuPageState.STOPPING)
            self._controller.call("stop_live")
            return
        self._live_transition_pending = True
        self.live.setText("Starting…")
        self._set_live_indicator("starting")
        self._set_page_state(AnritsuPageState.STARTING_LIVE)
        self._timer.setInterval(self.refresh.value())
        # Live is intentionally passive: do not alter sweep or trace modes.
        # This is compatible with the same current-trace path used by the
        # working one-shot read and avoids unsupported mode probes.
        self._controller.call("start_live", False)

    def _set_live_indicator(self, state: str, frame: int | None = None) -> None:
        labels = {
            "off": "●  LIVE OFF",
            "starting": "●  LIVE STARTING…",
            "on": "●  LIVE ON",
            "paused": "●  LIVE PAUSED",
            "stopping": "●  LIVE STOPPING…",
        }
        text = labels.get(state, labels["off"])
        if state == "on" and frame is not None:
            text += f"  •  FRAME {frame}"
            if self._frame_intervals_s:
                mean_interval = sum(self._frame_intervals_s[-20:]) / len(
                    self._frame_intervals_s[-20:]
                )
                if mean_interval > 0:
                    text += f"  •  {1.0 / mean_interval:.2f} FPS"
            if self._transfer_durations_s:
                text += f"  •  {self._transfer_durations_s[-1] * 1e3:.0f} ms VISA"
        self.live_indicator.setText(text)
        self.live_indicator.setProperty("liveState", state)
        self.live_indicator.style().unpolish(self.live_indicator)
        self.live_indicator.style().polish(self.live_indicator)

    def fetch_live(self) -> None:
        self._request_trace()

    def start_averaging(self) -> None:
        self._start_temporal_averaging("spectrum")

    def start_reference_averaging(self) -> None:
        if not self._confirm_reference_replacement("averaged"):
            return
        self._start_temporal_averaging("reference")

    def _start_temporal_averaging(self, destination: str) -> None:
        if self._averaging_active:
            return
        target = self.average_count.value()
        self._resume_live_after_averaging = self._timer.isActive()
        if self._resume_live_after_averaging:
            self._timer.stop()
            self._set_live_indicator("paused")
        self._averager.reset()
        self._averaging_active = True
        self._averaging_destination = destination
        self._set_page_state(
            AnritsuPageState.AVERAGING_REFERENCE
            if destination == "reference"
            else AnritsuPageState.AVERAGING_SIGNAL
        )
        self.average_progress.setRange(0, target)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {target}")
        label = "reference" if destination == "reference" else "spectrum"
        self.info.setText(f"Averaging {label}: 0 / {target} temporal frames...")
        self.status.emit(
            f"Anritsu passive temporal averaging started: {label}, 0 / {target}"
        )
        # Reuse an already pending Live frame instead of queuing a duplicate
        # VISA query against the same session.
        self._request_trace()

    def cancel_averaging(self) -> None:
        self._finish_temporal_averaging(resume_live=True)
        self.info.setText("Averaging cancelled; completed spectra were not modified.")
        self.status.emit("Anritsu temporal averaging cancelled")

    def _finish_temporal_averaging(self, *, resume_live: bool) -> None:
        was_live = self._resume_live_after_averaging
        should_resume_live = was_live and resume_live
        self._averaging_active = False
        self._averaging_destination = None
        self._resume_live_after_averaging = False
        self._averager.reset()
        if should_resume_live:
            self._timer.setInterval(self.refresh.value())
            self._timer.start()
            self.live.setText("Stop Live")
            self._set_live_indicator("on", self._live_frame_count)
            self._set_page_state(AnritsuPageState.LIVE)
        elif was_live:
            self.live.setText("Start Live")
            self.single.setEnabled(True)
            self._set_live_indicator("off")
            self._set_page_state(AnritsuPageState.IDLE)
        else:
            self._set_page_state(AnritsuPageState.IDLE)

    def _request_next_average_frame(self) -> None:
        if not self._averaging_active or self._fetch_pending:
            return
        self._request_trace()

    def capture_current_reference(self) -> None:
        """Use the latest completed frame locally without issuing a VISA query."""

        if self._latest_trace is None:
            QMessageBox.information(self, "Reference spectrum", "Acquire a spectrum before capturing a reference.")
            return
        if not self._confirm_reference_replacement("single"):
            return
        self._set_reference(self._build_reference(self._latest_trace, kind="single", count=1))
        self.status.emit("Anritsu current trace stored as a single reference")

    def acquire_reference_once(self) -> None:
        """Passively fetch one fresh trace and commit it only after success."""

        if self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
            return
        if not self._confirm_reference_replacement("single"):
            return
        self._pending_reference_kind = "single"
        self._set_page_state(AnritsuPageState.ACQUIRING_REFERENCE)
        self.info.setText("Acquiring one fresh reference frame…")
        self.status.emit("Anritsu single-reference acquisition started")
        if not self._request_trace():
            self._pending_reference_kind = None
            self._set_page_state(AnritsuPageState.IDLE)

    def _confirm_reference_replacement(self, new_kind: str) -> bool:
        current = self._reference_spectrum
        if current is None:
            return True
        existing = (
            f"{current.kind}, {current.average_count} frame(s), "
            f"{current.acquired_at_utc.isoformat()}, {current.points} points"
        )
        answer = QMessageBox.question(
            self,
            "Replace reference?",
            f"Existing reference: {existing}.\n\nReplace it with a new {new_kind} reference?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _build_reference(
        self,
        trace: SpectrumTrace,
        *,
        kind: str,
        count: int,
    ) -> ReferenceSpectrum:
        capabilities = self._capabilities
        firmware = str(getattr(capabilities, "firmware", "") or "")
        options = tuple(getattr(capabilities, "hardware_options", ()) or ())
        reference_level: float | None = None
        if self._last_configuration is not None:
            reference_level = self._last_configuration.reference_level_dbm
        else:
            try:
                reference_level = parse_quantity(self.reference.text(), DIMENSION_DBM).si_value
            except Exception:
                pass
        advanced = self._last_advanced_configuration
        return ReferenceSpectrum(
            trace=trace,
            kind=kind,
            average_count=count,
            acquired_at_utc=trace.acquired_at_utc,
            source_device_idn=self._device_idn,
            firmware=firmware,
            hardware_options=options,
            reference_level_dbm=reference_level,
            advanced_configuration_known=advanced is not None,
            rbw_auto=advanced.rbw_auto if advanced is not None else None,
            rbw_hz=advanced.rbw_hz if advanced is not None else None,
            vbw_mode=advanced.vbw_mode if advanced is not None else "",
            vbw_hz=advanced.vbw_hz if advanced is not None else None,
            detector=advanced.detector if advanced is not None else "",
            attenuation_auto=advanced.attenuation_auto if advanced is not None else None,
            attenuation_db=advanced.attenuation_db if advanced is not None else None,
            preamplifier_enabled=(
                advanced.preamplifier_enabled if advanced is not None else None
            ),
            sweep_time_auto=advanced.sweep_time_auto if advanced is not None else None,
            sweep_time_s=advanced.sweep_time_s if advanced is not None else None,
        )

    def _validate_reference_acquisition_compatibility(
        self, reference: ReferenceSpectrum
    ) -> None:
        """Reject processing when known acquisition conditions are not equivalent."""

        current = self._last_advanced_configuration
        if reference.advanced_configuration_known != (current is not None):
            raise ValueError(
                "Advanced acquisition configuration is known for only one spectrum. "
                "Read the instrument settings and acquire a new reference."
            )
        if not reference.advanced_configuration_known or current is None:
            return
        mismatches: list[str] = []
        if reference.rbw_auto != current.rbw_auto or not math.isclose(
            float(reference.rbw_hz), current.rbw_hz, rel_tol=1e-6, abs_tol=1.0
        ):
            mismatches.append("RBW")
        if reference.vbw_mode != current.vbw_mode:
            mismatches.append("VBW mode")
        elif reference.vbw_mode != "off" and (
            reference.vbw_hz is None
            or current.vbw_hz is None
            or not math.isclose(reference.vbw_hz, current.vbw_hz, rel_tol=1e-6, abs_tol=1.0)
        ):
            mismatches.append("VBW")
        if reference.detector != current.detector:
            mismatches.append("detector")
        if reference.attenuation_auto != current.attenuation_auto or not math.isclose(
            float(reference.attenuation_db),
            current.attenuation_db,
            rel_tol=0.0,
            abs_tol=0.01,
        ):
            mismatches.append("input attenuation")
        if reference.preamplifier_enabled != current.preamplifier_enabled:
            mismatches.append("preamplifier")
        if reference.sweep_time_auto != current.sweep_time_auto or not math.isclose(
            float(reference.sweep_time_s),
            current.sweep_time_s,
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            mismatches.append("sweep time")
        if mismatches:
            raise ValueError(
                "Acquisition settings differ from the reference: " + ", ".join(mismatches) + "."
            )

    def _set_reference(self, reference: ReferenceSpectrum) -> None:
        self._reference_spectrum = reference
        self._reference_trace = reference.trace
        self.show_reference.setChecked(True)
        self._update_reference_status()
        self._apply_page_state()
        self._refresh_spectrum_display()

    def _update_reference_status(self) -> None:
        reference = self._reference_spectrum
        if reference is None:
            self.reference_status.setText("No reference")
            return
        kind = "averaged" if reference.kind == "averaged" else reference.kind
        start = format_quantity_auto(reference.start_hz, DIMENSION_FREQUENCY)
        stop = format_quantity_auto(reference.stop_hz, DIMENSION_FREQUENCY)
        stored = "saved" if reference.saved_to_file else "memory only"
        self.reference_status.setText(
            f"{kind} · {reference.average_count} frame(s) · {reference.points} points · "
            f"{start}–{stop} · {reference.acquired_at_utc.isoformat()} · {stored}"
        )

    def remove_reference(self) -> None:
        self._reference_trace = None
        self._reference_spectrum = None
        self._pending_reference_kind = None
        self.spectrum_plot.clear_trace("Reference")
        self.spectrum_plot.clear_trace("Processed")
        self.show_reference.setChecked(False)
        self.show_processed.setChecked(False)
        self.reference_operation.setCurrentIndex(0)
        self._update_reference_status()
        self._apply_page_state()
        self._refresh_spectrum_display()
        self.status.emit("Anritsu reference spectrum removed")

    def save_reference_file(self) -> None:
        reference = self._reference_spectrum
        if reference is None:
            self.banner.show_message("There is no reference spectrum to save.")
            return
        directory = str(self._station_settings.storage.get("output_directory", "./measurements"))
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save Anritsu reference",
            str(Path(directory) / "anritsu_reference.h5"),
            "HDF5 measurement (*.h5)",
        )
        if not selected:
            return
        self._save_reference_to(Path(selected))

    def _save_reference_to(self, path: Path) -> None:
        reference = self._reference_spectrum
        if reference is None:
            raise ValueError("There is no reference spectrum to save.")
        try:
            saved = ReferenceHdf5Store.save(path, reference)
        except Exception as exc:
            self.banner.show_message(f"Reference save failed: {exc}", severity="error", timeout_ms=0)
            self.status.emit(f"Anritsu reference save failed: {exc}")
            return
        self._set_reference(saved)
        self.banner.show_message(
            f"Reference saved to {path.name} and verified as a completed HDF5 artefact.",
            severity="success",
        )
        self.status.emit(f"Anritsu reference saved: {path}")

    def load_reference_file(self) -> None:
        directory = str(self._station_settings.storage.get("output_directory", "./measurements"))
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Load Anritsu reference",
            directory,
            "HDF5 measurement (*.h5)",
        )
        if not selected:
            return
        self._load_reference_from(Path(selected))

    def _load_reference_from(self, path: Path) -> None:
        try:
            loaded = ReferenceHdf5Store.load(path)
        except Exception as exc:
            self.banner.show_message(f"Reference load failed: {exc}", severity="error", timeout_ms=0)
            self.status.emit(f"Anritsu reference load failed: {exc}")
            return
        if not self._confirm_reference_replacement("imported"):
            return
        imported = replace(loaded, kind="imported")
        self._set_reference(imported)
        self.banner.show_message(
            f"Reference loaded from {path.name}; the analyser configuration was not changed.",
            severity="success",
        )
        self.status.emit(f"Anritsu reference loaded: {path}")

    def _result(self, operation: str, result: object) -> None:
        if operation == "connect":
            self._device_idn = str(getattr(result, "idn", "") or "")
            self._set_page_state(AnritsuPageState.IDLE)
        if operation == "read_configuration" and isinstance(result, AnritsuConfigurationSnapshot):
            self._last_configuration = result
            self._set_frequency_bounds(result.start_hz, result.stop_hz)
            self.reference.setText(f"{result.reference_level_dbm:.9g} dBm")
            point_index = self.points.findData(result.points)
            if point_index < 0:
                raise ValueError(
                    f"Instrument returned {result.points} points outside the approved UI choices."
                )
            self.points.setCurrentIndex(point_index)
            self.banner.show_message(
                f"Current analyser settings loaded into the form (mode: "
                f"{result.instrument_mode or 'unknown'}). "
                "The instrument and safety limits were not changed.",
                severity="success",
            )
            self.status.emit("Anritsu current configuration read from instrument")
        elif operation in {
            "read_advanced_spectrum",
            "configure_advanced_spectrum",
        } and isinstance(result, AdvancedSpectrumSnapshot):
            self._show_advanced_snapshot(result)
            self._set_page_state(AnritsuPageState.IDLE)
            verb = (
                "configured and verified"
                if operation == "configure_advanced_spectrum"
                else "read without changing the instrument"
            )
            self.banner.show_message(
                f"Advanced Spectrum settings {verb}.", severity="success"
            )
            self.status.emit(f"Anritsu advanced Spectrum settings {verb}")
        elif operation in {
            "read_signal_generator",
            "configure_signal_generator",
        } and isinstance(result, SignalGeneratorSnapshot):
            self._show_signal_generator_snapshot(result)
            verb = "configured and verified" if operation == "configure_signal_generator" else "read"
            self.status.emit(f"Anritsu signal generator {verb}; RF state confirmed")
        elif operation == "arm_signal_generator":
            self._sg_armed = True
            self.sg_status.setText("●  RF ARMED — one enable permitted")
            self.sg_status.setProperty("liveState", "starting")
            self.sg_status.style().unpolish(self.sg_status)
            self.sg_status.style().polish(self.sg_status)
            self._apply_page_state()
            self.status.emit("Anritsu signal generator armed for one RF enable")
        elif operation == "set_signal_generator_output":
            self._sg_output_enabled = bool(result)
            self._sg_armed = False
            state = "on" if self._sg_output_enabled else "off"
            self.sg_status.setText(
                "●  RF OUTPUT ON" if self._sg_output_enabled else "●  RF OUTPUT OFF"
            )
            self.sg_status.setProperty("liveState", state)
            self.sg_status.style().unpolish(self.sg_status)
            self.sg_status.style().polish(self.sg_status)
            self._apply_page_state()
            self.status.emit(
                "Anritsu signal generator RF OUTPUT "
                + ("ON" if self._sg_output_enabled else "OFF")
            )
        elif operation == "configure" and isinstance(result, AnritsuConfigurationSnapshot):
            self._result("read_configuration", result)
            self.status.emit("Anritsu configured and verified by SCPI readback")
        elif operation == "start_live" and isinstance(result, AnritsuConfigurationSnapshot):
            self._live_transition_pending = False
            self._result("read_configuration", result)
            self._live_frame_count = 0
            self._identical_live_frames = 0
            self._last_live_signature = None
            self._last_frame_monotonic = None
            self._frame_intervals_s.clear()
            self._transfer_durations_s.clear()
            self._stale_frame_count = 0
            self._coalesced_timer_ticks = 0
            self._timer.start()
            self.live.setText("Stop Live")
            self._set_live_indicator("on", 0)
            self._set_page_state(AnritsuPageState.LIVE)
            mode = "passive current-trace polling"
            self.info.setText(f"Live started; {mode}. Waiting for first frame...")
            self.status.emit(f"Anritsu Live started: {mode}")
        elif operation == "stop_live":
            self._live_transition_pending = False
            self.live.setText("Start Live")
            self._set_live_indicator("off")
            self._set_page_state(AnritsuPageState.IDLE)
            self.info.setText("Live stopped.")
            self.status.emit("Anritsu Live stopped")
        elif operation in {"fetch_trace", "fetch_current_trace", "single_sweep"} and isinstance(result, SpectrumTrace):
            self._fetch_pending = False
            finished = time.monotonic()
            if self._fetch_started_monotonic is not None:
                self._transfer_durations_s.append(finished - self._fetch_started_monotonic)
                self._transfer_durations_s = self._transfer_durations_s[-100:]
            self._fetch_started_monotonic = None
            if self._pending_reference_kind == "single":
                self._pending_reference_kind = None
                self._latest_trace = result
                self._set_reference(self._build_reference(result, kind="single", count=1))
                self._set_page_state(AnritsuPageState.IDLE)
                self.info.setText(
                    f"Single reference acquired: {len(result.powers_dbm)} points · "
                    f"{result.acquired_at_utc.isoformat()}"
                )
                self.status.emit("Anritsu single-reference acquisition completed")
                return
            if self._averaging_active:
                try:
                    completed = self._averager.add(result.powers_dbm)
                except ValueError as exc:
                    self._finish_temporal_averaging(resume_live=False)
                    self.info.setText(f"Averaging stopped: {exc}")
                    return
                target = self.average_count.value()
                self.average_progress.setValue(completed)
                self.average_progress.setFormat(f"{completed} / {target}")
                label = (
                    "reference" if self._averaging_destination == "reference" else "spectrum"
                )
                self.info.setText(
                    f"Averaging {label}: {completed} / {target} temporal frames..."
                )
                self.status.emit(
                    f"Anritsu temporal averaging progress: {label} {completed} / {target}"
                )
                if completed >= target:
                    averaged = self._averager.result()
                    averaged_trace = SpectrumTrace(
                        frequencies_hz=result.frequencies_hz,
                        powers_dbm=averaged,
                        acquired_at_utc=result.acquired_at_utc,
                        trace_name=(
                            f"{result.trace_name}_REFAVG{target}"
                            if self._averaging_destination == "reference"
                            else f"{result.trace_name}_AVG{target}"
                        ),
                    )
                    self._latest_trace = result
                    if self._averaging_destination == "reference":
                        self._set_reference(
                            self._build_reference(
                                averaged_trace,
                                kind="averaged",
                                count=target,
                            )
                        )
                        completion = f"Averaged reference completed: {target} / {target}"
                    else:
                        self._averaged_trace = averaged_trace
                        self.show_average.setChecked(True)
                        completion = f"Averaged spectrum completed: {target} / {target}"
                    self._finish_temporal_averaging(resume_live=True)
                    self.info.setText(completion)
                    self.status.emit(f"Anritsu {completion.lower()}")
                    self._refresh_spectrum_display()
                else:
                    QTimer.singleShot(self.refresh.value(), self._request_next_average_frame)
            else:
                self._show_trace(result)

    def _show_trace(self, trace: SpectrumTrace) -> None:
        self._latest_trace = trace
        self._apply_page_state()
        self._refresh_spectrum_display()
        live_detail = ""
        if self._timer.isActive():
            now = time.monotonic()
            if self._last_frame_monotonic is not None:
                self._frame_intervals_s.append(now - self._last_frame_monotonic)
                self._frame_intervals_s = self._frame_intervals_s[-100:]
            self._last_frame_monotonic = now
            self._live_frame_count += 1
            signature = hash(trace.powers_dbm)
            if signature == self._last_live_signature:
                self._identical_live_frames += 1
                self._stale_frame_count += 1
                live_detail = f" • unchanged ×{self._identical_live_frames}"
                if self._identical_live_frames == 3:
                    self.banner.show_message(
                        "Live received three identical traces. Verify that Trace A is in Write "
                        "mode, Continuous Sweep is active, and the analyser sweep time is not "
                        "longer than the observation interval.",
                        timeout_ms=15_000,
                    )
            else:
                self._identical_live_frames = 0
                self._stale_frame_count = 0
                live_detail = " • new data"
            self._last_live_signature = signature
            self._set_live_indicator("on", self._live_frame_count)
            live_detail = f" • Live frame {self._live_frame_count}{live_detail}"
            if self._frame_intervals_s:
                effective_ms = (
                    sum(self._frame_intervals_s[-20:])
                    / len(self._frame_intervals_s[-20:])
                    * 1e3
                )
                live_detail += (
                    f" • requested {self.refresh.value()} ms"
                    f" • effective {effective_ms:.0f} ms"
                    f" • coalesced {self._coalesced_timer_ticks}"
                )
        self.info.setText(
            f"{len(trace.powers_dbm)} points • {trace.acquired_at_utc.isoformat()} • "
            f"max {max(trace.powers_dbm):.4g} dBm{live_detail}"
        )

    def _refresh_spectrum_display(self, *_args: object) -> None:
        traces: list[tuple[str, SpectrumTrace, tuple[float, ...], str, str]] = []
        if self._latest_trace is not None and self.show_raw.isChecked():
            traces.append(("Raw", self._latest_trace, self._latest_trace.powers_dbm, "dBm", "#2196f3"))
        if self._averaged_trace is not None and self.show_average.isChecked():
            traces.append(("Averaged", self._averaged_trace, self._averaged_trace.powers_dbm, "dBm", "#00a67d"))
        if self._reference_trace is not None and self.show_reference.isChecked():
            traces.append(("Reference", self._reference_trace, self._reference_trace.powers_dbm, "dBm", "#ffb300"))

        operation = str(self.reference_operation.currentData() or "none")
        processed: tuple[float, ...] | None = None
        processed_unit = "dBm"
        signal = self._averaged_trace or self._latest_trace
        if operation != "none" and signal is not None and self._reference_trace is not None:
            try:
                if not frequency_grids_match(signal.frequencies_hz, self._reference_trace.frequencies_hz):
                    raise ValueError("Reference frequency grid differs from the current spectrum.")
                reference_level = (
                    self._reference_spectrum.reference_level_dbm
                    if self._reference_spectrum is not None
                    else None
                )
                current_level = (
                    self._last_configuration.reference_level_dbm
                    if self._last_configuration is not None
                    else None
                )
                if (
                    reference_level is not None
                    and current_level is not None
                    and not math.isclose(reference_level, current_level, abs_tol=0.005)
                ):
                    raise ValueError(
                        "Reference Level differs from the reference acquisition "
                        f"({current_level:g} dBm current, {reference_level:g} dBm reference)."
                    )
                if self._reference_spectrum is not None:
                    self._validate_reference_acquisition_compatibility(
                        self._reference_spectrum
                    )
                processed, processed_unit = apply_reference_operation(
                    signal.powers_dbm, self._reference_trace.powers_dbm, operation
                )
            except ValueError as exc:
                self.info.setText(f"Reference processing unavailable: {exc}")
            else:
                self.show_processed.setChecked(True)
                if self.show_processed.isChecked():
                    traces.append(("Processed", signal, processed, processed_unit, "#ab47bc"))

        for name in ("Raw", "Averaged", "Reference", "Processed"):
            self.spectrum_plot.clear_trace(name)
        if not traces:
            return
        if processed is not None and processed_unit != "dBm" and self.show_processed.isChecked():
            traces = [item for item in traces if item[0] == "Processed"]
        displayed = 0
        for name, trace, values, _unit, color in traces:
            self.spectrum_plot.set_trace(
                name,
                trace.frequencies_hz,
                values,
                color=color,
                primary=name in {"Processed", "Averaged", "Raw"},
            )
            displayed += sum(
                math.isfinite(frequency) and math.isfinite(value)
                for frequency, value in zip(trace.frequencies_hz, values, strict=True)
            )
        if displayed == 0:
            self.info.setText("No finite spectrum points are available for display.")
            return
        active_unit = traces[-1][3]
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit=active_unit
        )

    def _error(self, operation: str, error: str) -> None:
        if operation in {"read_advanced_spectrum", "configure_advanced_spectrum"}:
            self._set_page_state(AnritsuPageState.ERROR)
            self.banner.show_message(
                f"Anritsu advanced Spectrum operation failed: {error}",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu {operation} failed: {error}")
            return
        if operation in {
            "read_signal_generator",
            "configure_signal_generator",
            "arm_signal_generator",
            "set_signal_generator_output",
        }:
            self._sg_armed = False
            if operation == "set_signal_generator_output":
                self.sg_status.setText("●  RF OUTPUT UNKNOWN — use RF OFF or E-STOP")
                self.sg_status.setProperty("liveState", "off")
            self._apply_page_state()
            self.banner.show_message(
                f"Anritsu signal-generator operation {operation!r} failed: {error}",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu {operation} failed: {error}")
            return
        if operation in {"fetch_trace", "fetch_current_trace", "single_sweep"}:
            self._fetch_pending = False
            if self._averaging_active:
                self._finish_temporal_averaging(resume_live=False)
                self.info.setText(f"Averaging stopped: {error}")
        if operation in {
            "read_configuration", "configure", "start_live", "fetch_trace", "fetch_current_trace",
            "single_sweep", "emergency_off",
        }:
            self._live_transition_pending = False
            self._timer.stop()
            self.live.setText("Start Live")
            self._set_live_indicator("off")
            self._pending_reference_kind = None
            self._set_page_state(AnritsuPageState.ERROR)
            self.banner.show_message(
                f"Anritsu operation {operation!r} failed: {error}. "
                "The last valid spectrum remains visible; retry when communication is stable.",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu {operation} failed: {error}")


_SWEEPABLE_PARAMETERS = SWEEPABLE_PARAMETERS
_sweep_default = sweep_default

