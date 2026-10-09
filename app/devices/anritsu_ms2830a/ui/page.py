"""Manual-control UI for the Anritsu MS2830A module."""

# ruff: noqa: F401
from __future__ import annotations

import hashlib
import math
import time
from copy import deepcopy
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QThread, QTimer, Signal
from app.devices.anritsu_ms2830a.ui.manual_archive_worker import ManualArchiveWorker
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FlowLayout,
    Flyout,
    FlyoutAnimationType,
    FlyoutViewBase,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    SpinBox,
    StrongBodyLabel,
    TitleLabel,
    ToggleButton,
    TransparentPushButton,
    isDarkTheme,
)

from app.devices.anritsu_ms2830a import (
    ANRITSU_PREAMPLIFIER_OPTIONS,
    AdvancedSpectrumConfig,
    AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot,
    AnritsuFullConfigurationReadback,
    ReferenceSpectrum,
    SignalGeneratorConfig,
    SignalGeneratorSnapshot,
    SpectrumConfig,
    SpectrumTrace,
    frequency_option_for,
)
from app.devices.anritsu_ms2830a.acquisition_context import spectrum_configuration_fingerprint
from app.devices.anritsu_ms2830a.ui.readback_dialog import AnritsuReadbackDialog
from app.domain.errors import ConfigurationError
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
    parse_quantity,
)
from app.recipes.parameter_registry import SWEEPABLE_PARAMETERS, sweep_default
from app.safety.anritsu import (
    ANRITSU_REFERENCE_LEVEL_MAX_DBM,
    ANRITSU_REFERENCE_LEVEL_MIN_DBM,
    ANRITSU_SWEEP_POINT_COUNTS,
    normalize_anritsu_detector,
)
from app.settings.models import StationSettings
from app.spectrum import (
    LinearPowerAverager,
    SpectrumAnalysisParameters,
    SpectrumCleanupResult,
    SpectrumDisplayState,
    SpectrumDisplayTrace,
    SpectrumPeak,
    apply_reference_operation,
    build_display_state,
    frequency_grids_match,
)
from app.spectrum.analysis import SPECTRUM_FILTER_LABELS, SPECTRUM_FILTER_ORDER
from app.spectrum.comparison_view import compare_power, convert_power_view
from app.storage import (
    ManualSpectrumArchive,
    ManualSpectrumSaveMode,
    ReferenceHdf5Store,
)
from app.ui.common import line_edit as _line
from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationDialog
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.dialogs import StationMessageBox as QMessageBox
from app.ui.widgets import FluentTabView, LimitField, NotificationBanner, SpectrumPlotWidget
from app.ui.widgets.plot_ownership import create_plot_widget, own_plot_item_menus
from app.ui.workers import DeviceController

from .analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from .analysis_worker import (
    SpectrogramAnalysisOutcome,
    SpectrogramAnalysisRequest,
    SpectrumAnalysisController,
    SpectrumAnalysisOutcome,
    SpectrumAnalysisRequest,
    spectrogram_color_levels,
)
from .background_assistant import BackgroundCorrectionAssistant
from .correction_card import SpectrumCorrectionWorkspace
from .manual_save import ManualSpectrumSaveDialog, ManualSpectrumSaveOptions
from .peak_analysis import PeakTableDialog, PeakTrackingWindow
from .spectrum_workbench import SpectrumWorkbench
from .spectrum_controls import PlotRangeController, PlotScaleDialog, SpectrumCorrectionControls


class AnritsuPageState(StrEnum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    STARTING_LIVE = "starting_live"
    LIVE = "live"
    AVERAGING_SIGNAL = "averaging_signal"
    AVERAGING_REFERENCE = "averaging_reference"
    ACQUIRING_SPECTRUM = "acquiring_spectrum"
    ACQUIRING_REFERENCE = "acquiring_reference"
    CONFIGURING = "configuring"
    STOPPING = "stopping"
    ERROR = "error"


def _finish_spectrum_form(form: QFormLayout) -> None:
    """Keep Fluent editors at their natural height, including in scroll hosts."""
    form.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    form.setVerticalSpacing(10)
    form.setHorizontalSpacing(16)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        label = item.widget() if item else None
        if isinstance(label, QLabel) and not isinstance(label, BodyLabel):
            fluent_label = BodyLabel(label.text(), form.parentWidget())
            fluent_label.setMinimumWidth(fluent_label.sizeHint().width())
            form.replaceWidget(label, fluent_label)
            label.hide()
            label.deleteLater()
        field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        if field and field.widget():
            widget = field.widget()
            widget.setMinimumHeight(widget.sizeHint().height())


class AnritsuSpectrumConfigurationPanel(CardWidget):
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
        self.frequency_representation = ComboBox(self)
        self.frequency_representation.addItem("Start / Stop", userData="start_stop")
        self.frequency_representation.addItem("Center / Span", userData="center_span")
        self.start = _line("1 MHz")
        self.stop = _line("10 MHz")
        self.reference = _line("0 dBm")
        self.points = ComboBox(self)
        self.frequency_label_a = BodyLabel("Start")
        self.frequency_label_b = BodyLabel("Stop")
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

        self.rbw_mode = ComboBox(self)
        self.rbw_mode.addItem("Auto", userData="auto")
        self.rbw_mode.addItem("Manual", userData="manual")
        self.rbw = _line("3 MHz")
        self.rbw.setEnabled(False)
        self.rbw_mode.currentIndexChanged.connect(
            lambda: self.rbw.setEnabled(self.rbw_mode.currentData() == "manual")
        )
        rbw_layout = QHBoxLayout()
        rbw_layout.setSpacing(6)
        rbw_layout.addWidget(self.rbw_mode)
        rbw_layout.addWidget(self.rbw, 1)
        form.addRow("RBW", rbw_layout)

        self.vbw_auto = ComboBox(self)
        self.vbw_auto.addItem("Auto", userData="auto")
        self.vbw_auto.addItem("Manual", userData="manual")
        self.vbw_mode = ComboBox(self)
        self.vbw_mode.addItem("Video", userData="VID")
        self.vbw_mode.addItem("Power", userData="POW")
        self.vbw = _line("300 kHz")
        self.vbw.setEnabled(False)
        self.vbw_auto.currentIndexChanged.connect(
            lambda: self.vbw.setEnabled(self.vbw_auto.currentData() == "manual")
        )
        vbw_layout = QHBoxLayout()
        vbw_layout.setSpacing(6)
        vbw_layout.addWidget(self.vbw_auto)
        vbw_layout.addWidget(self.vbw_mode)
        vbw_layout.addWidget(self.vbw, 1)
        form.addRow("VBW", vbw_layout)

        _finish_spectrum_form(form)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.addLayout(form)
        if plan_mode:
            note = BodyLabel(
                "Plan editing is offline. The visible core spectrum snapshot is stored; "
                "no VISA command is sent to Anritsu from this window."
            )
            note.setObjectName("recipeHint")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.frequency_representation.currentIndexChanged.connect(
            self._change_frequency_representation
        )
        self.set_settings(settings)
        self.load_settings_defaults()

    def _limit_values(self, key: str) -> tuple[object, object]:
        if key == "reference_level":
            return (
                f"{ANRITSU_REFERENCE_LEVEL_MIN_DBM:g} dBm",
                f"{ANRITSU_REFERENCE_LEVEL_MAX_DBM:g} dBm",
            )
        value = getattr(self._settings.anritsu.safety, key)
        return value.min, value.max

    def _bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self._limit_values(key), range_mode=True)
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
                self.points.addItem(str(value), userData=value)
        index = self.points.findData(current if current is not None else 1001)
        self.points.setCurrentIndex(index if index >= 0 else 0)
        for field in self.limit_fields.values():
            key = str(field.property("limitKey"))
            field.set_limits(*self._limit_values(key))

    def load_settings_defaults(self) -> None:
        """Restore the persisted acquisition snapshot into the visible form."""

        defaults = self._settings.anritsu.safety.defaults
        self.frequency_representation.setCurrentIndex(
            self.frequency_representation.findData("start_stop")
        )
        self.start.setText(str(defaults["start_frequency"]))
        self.stop.setText(str(defaults["stop_frequency"]))
        self.reference.setText(str(defaults["reference_level"]))
        point_index = self.points.findData(int(defaults["sweep_points"]))
        if point_index >= 0:
            self.points.setCurrentIndex(point_index)
        if "rbw" in defaults:
            self.rbw.setText(str(defaults["rbw"]))
        if "rbw_auto" in defaults:
            idx = self.rbw_mode.findData("auto" if defaults["rbw_auto"] else "manual")
            if idx >= 0:
                self.rbw_mode.setCurrentIndex(idx)
        if defaults.get("vbw") is not None:
            self.vbw.setText(str(defaults["vbw"]))
        if "vbw_mode" in defaults:
            mode_val = str(defaults["vbw_mode"]).upper()
            if mode_val in {"AUTO", "MANUAL", "OFF"}:
                if mode_val == "OFF" and self.vbw_auto.findData("off") < 0:
                    self.vbw_auto.addItem("Off", userData="off")
                idx = self.vbw_auto.findData(mode_val.lower())
                if idx >= 0:
                    self.vbw_auto.setCurrentIndex(idx)
            else:
                idx = self.vbw_mode.findData(mode_val)
                if idx >= 0:
                    self.vbw_mode.setCurrentIndex(idx)

        if defaults.get("vbw_filter_mode") in {"VID", "POW"}:
            self.vbw_mode.setCurrentIndex(self.vbw_mode.findData(defaults["vbw_filter_mode"]))

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

    def load_snapshot(
        self,
        snapshot: AnritsuConfigurationSnapshot | AnritsuFullConfigurationReadback,
    ) -> None:
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
        if hasattr(snapshot, "rbw_auto"):
            rbw_idx = self.rbw_mode.findData("auto" if snapshot.rbw_auto else "manual")
            if rbw_idx >= 0:
                self.rbw_mode.setCurrentIndex(rbw_idx)
        if hasattr(snapshot, "rbw_hz") and snapshot.rbw_hz is not None:
            self.rbw.setText(format_quantity_auto(snapshot.rbw_hz, DIMENSION_FREQUENCY))
        if hasattr(snapshot, "vbw_auto"):
            vbw_state = "auto" if snapshot.vbw_auto else "off" if snapshot.vbw_hz is None else "manual"
            if vbw_state == "off" and self.vbw_auto.findData("off") < 0:
                self.vbw_auto.addItem("Off", userData="off")
            vbw_auto_idx = self.vbw_auto.findData(vbw_state)
            if vbw_auto_idx >= 0:
                self.vbw_auto.setCurrentIndex(vbw_auto_idx)
        if hasattr(snapshot, "vbw_mode") and snapshot.vbw_mode:
            vbw_mode_idx = self.vbw_mode.findData(snapshot.vbw_mode)
            if vbw_mode_idx >= 0:
                self.vbw_mode.setCurrentIndex(vbw_mode_idx)
        if hasattr(snapshot, "vbw_hz") and snapshot.vbw_hz is not None:
            self.vbw.setText(format_quantity_auto(snapshot.vbw_hz, DIMENSION_FREQUENCY))

    def spectrum_config(self, trace: str = "TRAC1") -> SpectrumConfig:
        start_hz, stop_hz = self.frequency_bounds()
        rbw_auto = self.rbw_mode.currentData() == "auto"
        rbw_hz = (
            parse_quantity(self.rbw.text(), DIMENSION_FREQUENCY).si_value
            if not rbw_auto
            else None
        )
        vbw_auto = self.vbw_auto.currentData() == "auto"
        vbw_mode = str(self.vbw_mode.currentData() or "VID")
        vbw_hz = (
            parse_quantity(self.vbw.text(), DIMENSION_FREQUENCY).si_value
            if self.vbw_auto.currentData() == "manual"
            else None
        )
        return SpectrumConfig(
            start_hz=start_hz,
            stop_hz=stop_hz,
            reference_level_dbm=parse_quantity(
                self.reference.text(), DIMENSION_DBM
            ).si_value,
            points=int(self.points.currentData()),
            trace=trace,
            rbw_auto=rbw_auto,
            rbw_hz=rbw_hz,
            vbw_auto=vbw_auto,
            vbw_mode=vbw_mode,
            vbw_hz=vbw_hz,
        )

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


class AnritsuAdvancedSpectrumPanel(CardWidget):
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

        self.rbw_mode = ComboBox(self)
        self.rbw_mode.addItem("Automatic", userData="auto")
        self.rbw_mode.addItem("Manual", userData="manual")
        self.rbw = _line("1 kHz")
        form.addRow("RBW mode", self.rbw_mode)
        form.addRow("Resolution bandwidth", self.rbw)

        self.vbw_mode = ComboBox(self)
        self.vbw_mode.addItem("Automatic", userData="auto")
        self.vbw_mode.addItem("Manual", userData="manual")
        self.vbw_mode.addItem("Off", userData="off")
        self.vbw = _line("1 kHz")
        form.addRow("VBW mode", self.vbw_mode)
        form.addRow("Video bandwidth", self.vbw)

        self.detector = ComboBox(self)
        form.addRow("Detector", self.detector)
        self.attenuation_mode = ComboBox(self)
        self.attenuation_mode.addItem("Automatic", userData="auto")
        self.attenuation_mode.addItem("Manual", userData="manual")
        self.attenuation = SpinBox(self)
        self.attenuation.setRange(0, 60)
        self.attenuation.setSingleStep(2)
        self.attenuation.setSuffix(" dB")
        form.addRow("RF attenuation mode", self.attenuation_mode)
        form.addRow("RF attenuation", self.attenuation)
        self.preamplifier = CheckBox("Enable preamplifier")
        form.addRow("Input gain", self.preamplifier)

        self.sweep_time_mode = ComboBox(self)
        self.sweep_time_mode.addItem("Automatic", userData="auto")
        self.sweep_time_mode.addItem("Manual", userData="manual")
        self.sweep_time = _line("100 ms")
        form.addRow("Sweep-time mode", self.sweep_time_mode)
        form.addRow("Sweep time", self.sweep_time)

        _finish_spectrum_form(form)

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
            self.detector.addItem(label, userData=value)
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
        detector_index = self.detector.findData(
            normalize_anritsu_detector(snapshot.detector)
        )
        if detector_index >= 0:
            self.detector.setCurrentIndex(detector_index)
        self.attenuation.setValue(round(snapshot.attenuation_db))
        self.preamplifier.setChecked(snapshot.preamplifier_enabled)
        self.sweep_time.setText(
            format_quantity_auto(snapshot.sweep_time_s, DIMENSION_TIME)
        )
        self.sync_editors()

    def settings_snapshot(self) -> AdvancedSpectrumSnapshot:
        """Capture all visible defaults, including values behind AUTO modes."""

        return AdvancedSpectrumSnapshot(
            rbw_auto=self.rbw_mode.currentData() == "auto",
            rbw_hz=parse_quantity(self.rbw.text(), DIMENSION_FREQUENCY).si_value,
            vbw_mode=str(self.vbw_mode.currentData()),
            vbw_hz=parse_quantity(self.vbw.text(), DIMENSION_FREQUENCY).si_value,
            detector=str(self.detector.currentData()),
            attenuation_auto=self.attenuation_mode.currentData() == "auto",
            attenuation_db=float(self.attenuation.value()),
            preamplifier_enabled=self.preamplifier.isChecked(),
            sweep_time_auto=self.sweep_time_mode.currentData() == "auto",
            sweep_time_s=parse_quantity(
                self.sweep_time.text(), DIMENSION_TIME
            ).si_value,
            instrument_mode="PLAN_EDIT",
        )

    def load_settings_defaults(self, settings: StationSettings) -> None:
        defaults = settings.anritsu.safety.defaults
        rbw_value = defaults.get("rbw", "1 kHz")
        rbw_hz = parse_quantity(rbw_value, DIMENSION_FREQUENCY).si_value
        vbw_value = defaults.get("vbw") or rbw_value
        vbw_hz = parse_quantity(vbw_value, DIMENSION_FREQUENCY).si_value
        sweep_time_s = parse_quantity(
            defaults.get("sweep_time", "100 ms"), DIMENSION_TIME
        ).si_value
        self.load_snapshot(
            AdvancedSpectrumSnapshot(
                rbw_auto=bool(defaults.get("rbw_auto", True)),
                rbw_hz=rbw_hz,
                vbw_mode=str(defaults.get("vbw_mode", "auto")),
                vbw_hz=vbw_hz,
                detector=str(defaults.get("detector", "NORM")),
                attenuation_auto=bool(defaults.get("attenuation_auto", True)),
                attenuation_db=parse_quantity(
                    defaults.get("attenuation", "0 dB"), DIMENSION_DB
                ).si_value,
                preamplifier_enabled=bool(
                    defaults.get("preamplifier_enabled", False)
                ),
                sweep_time_auto=bool(defaults.get("sweep_time_auto", True)),
                sweep_time_s=sweep_time_s,
                instrument_mode="PLAN_EDIT",
            )
        )


class _SpectrogramBuffer:
    """Memory-bounded rolling store for already completed passive trace frames."""

    MAX_WINDOW_S = 120.0
    MIN_ROW_INTERVAL_S = 0.1
    MAX_ROWS = int(MAX_WINDOW_S / MIN_ROW_INTERVAL_S) + 2

    def __init__(self) -> None:
        self._frequencies_hz: np.ndarray | None = None
        self._rows: deque[tuple[float, np.ndarray]] = deque(maxlen=self.MAX_ROWS)
        self._processing_rows: deque[tuple[float, np.ndarray]] = deque(maxlen=self.MAX_ROWS)
        self._last_input = None
        self.configuration_generation = None

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def clear(self) -> None:
        self._frequencies_hz = None
        self._rows.clear()
        self._processing_rows.clear()
        self._last_input = None
        self.configuration_generation = None

    def append(self, trace: SpectrumTrace, *, now: float | None = None) -> None:
        if trace is self._last_input:
            return
        timestamp = time.monotonic() if now is None else float(now)
        frequencies = np.asarray(trace.frequencies_hz, dtype=np.float64)
        powers = np.asarray(trace.powers_dbm, dtype=np.float64)
        if (
            frequencies.ndim != 1
            or powers.ndim != 1
            or frequencies.size == 0
            or frequencies.size != powers.size
        ):
            return
        if (
            self._frequencies_hz is None
            or self.configuration_generation != trace.configuration_generation
            or self._frequencies_hz.shape != frequencies.shape
            or not np.array_equal(self._frequencies_hz, frequencies)
        ):
            self.clear()
            self._frequencies_hz = frequencies.copy()
            self.configuration_generation = trace.configuration_generation
        powers = np.frombuffer(powers.tobytes(), dtype=np.float64)
        # Keep every received frame for temporal processing, independently of
        # heatmap display decimation. Immutable doubles preserve subtraction.
        exact = powers
        self._processing_rows.append((timestamp, exact))
        self._last_input = trace
        limit = max(1, min(self.MAX_ROWS, (64 * 1024 * 1024) // max(1, exact.nbytes)))
        while len(self._processing_rows) > limit:
            self._processing_rows.popleft()
        if self._rows and timestamp - self._rows[-1][0] < self.MIN_ROW_INTERVAL_S:
            self._rows[-1] = (timestamp, powers)
        else:
            self._rows.append((timestamp, powers))
        cutoff = timestamp - self.MAX_WINDOW_S
        while self._rows and self._rows[0][0] < cutoff:
            self._rows.popleft()
        while len(self._rows) > limit:
            self._rows.popleft()
        while len(self._processing_rows) > 64 and self._processing_rows[0][0] < cutoff:
            self._processing_rows.popleft()

    def reset_processing(self):
        self._processing_rows.clear()
        self._last_input = None

    def processing_tail(self, count):
        selected = tuple(self._processing_rows)[-count:]
        return tuple(stamp for stamp, _ in selected), tuple(row for _, row in selected)

    def snapshot(
        self, window_s: int, *, now: float | None = None
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        if self._frequencies_hz is None or not self._rows:
            return None
        timestamp = self._rows[-1][0] if now is None else float(now)
        cutoff = timestamp - float(window_s)
        selected = [(stamp, row) for stamp, row in self._rows if stamp >= cutoff]
        if not selected:
            return None
        elapsed = np.asarray([stamp - timestamp for stamp, _row in selected], dtype=float)
        matrix = np.stack([row for _stamp, row in selected])
        return self._frequencies_hz.copy(), elapsed, matrix

    def recent_power_rows(self, max_rows: int = 24) -> tuple[tuple[float, ...], ...]:
        """Return a bounded temporal sample for EMI classification.

        The full 120-second buffer belongs to the spectrogram.  Re-copying it
        for every analysis frame scales poorly and gives the stationary-line
        classifier little additional value over a recent representative tail.
        """

        count = max(0, int(max_rows))
        if count == 0:
            return ()
        return tuple(
            tuple(row.tolist())
            for _stamp, row in tuple(self._rows)[-count:]
        )

    def frame_snapshot(self, window_s: int, *, processing=False, warmup_frames=0):
        """Share immutable rows with the CPU worker without copying the full history."""
        if self._frequencies_hz is None or not self._rows:
            return None
        cutoff = self._rows[-1][0] - window_s
        source = self._processing_rows if processing else self._rows
        source = tuple(source)
        first = next((i for i, (stamp, _row) in enumerate(source) if stamp >= cutoff), len(source))
        rows = source[max(0, first - warmup_frames):]
        if not rows:
            return None
        return tuple(self._frequencies_hz), tuple(stamp for stamp, _ in rows), tuple(row for _, row in rows)


class _AnritsuSpectrogramWidget(QWidget):
    """Theme-aware rolling frequency/time heatmap."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.plot = create_plot_widget(self)
        self.plot.setObjectName("anritsuSpectrogramPlot")
        self.plot.setMenuEnabled(True)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Time before latest frame", units="s")
        self.image = pg.ImageItem(axisOrder="row-major")
        self.plot.addItem(self.image)
        self.colormap = pg.colormap.get("viridis")
        self._amplitude_unit = None
        self.color_bar = pg.ColorBarItem(
            interactive=False,
            values=(-120.0, 0.0),
            colorMap=self.colormap,
            label=None,
        )
        self.color_bar.setImageItem(self.image, insert_in=self.plot.getPlotItem())
        self.color_bar.axis.setLabel("Amplitude", units="dBm")
        own_plot_item_menus(self.color_bar, self.plot)
        layout.addWidget(self.plot, 1)
        self._apply_theme()

    def set_data(
        self,
        frequencies_hz: np.ndarray,
        elapsed_s: np.ndarray,
        matrix: np.ndarray,
        *,
        unit: str,
        color_levels: tuple[float, float] | None = None,
    ) -> None:
        levels = color_levels if color_levels is not None else spectrogram_color_levels(matrix, unit)
        if levels is None:
            self.clear()
            return
        low, high = levels
        if unit != self._amplitude_unit:
            self.colormap = pg.colormap.get("CET-D1" if unit == "W" else "viridis")
            self.color_bar.setColorMap(self.colormap)
            self._amplitude_unit = unit
        x_min = float(frequencies_hz[0])
        x_max = float(frequencies_hz[-1])
        x_width = max(abs(x_max - x_min), 1.0)
        y_min = float(elapsed_s[0]) if elapsed_s.size > 1 else -1.0
        y_max = max(float(elapsed_s[-1]), 0.0)
        y_height = max(y_max - y_min, 1.0)
        self.image.setImage(matrix, autoLevels=False, levels=(float(low), float(high)))
        self.image.setRect(QRectF(min(x_min, x_max), y_min, x_width, y_height))
        self.color_bar.setLevels((float(low), float(high)))
        self.color_bar.axis.setLabel("Signed residual" if unit == "W" else "Amplitude", units=unit)

    def clear(self) -> None:
        self.image.clear()

    def reset_view(self) -> None:
        self.plot.getViewBox().autoRange()

    def event(self, event: QEvent) -> bool:
        if event.type() in {
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        }:
            self._apply_theme()
        return super().event(event)

    def _apply_theme(self) -> None:
        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(palette.background)
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen(palette.axes))
            axis.setTextPen(pg.mkPen(palette.axes))
        self.color_bar.axis.setPen(pg.mkPen(palette.axes))
        self.color_bar.axis.setTextPen(pg.mkPen(palette.axes))


class _AnritsuSpectrogramWindow(StationDialog):
    """Always-on-top view sharing the page's rolling spectrogram buffer."""

    source_changed = Signal(str)
    window_changed = Signal(int)
    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Anritsu MS2830A — floating spectrogram")
        self.setObjectName("anritsuSpectrogramWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(820, 560)
        self.setMinimumSize(520, 380)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=8)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Live spectrogram", surface))
        header.addStretch(1)
        layout.addLayout(header)
        controls = QGridLayout()
        controls.setHorizontalSpacing(8)
        controls.setVerticalSpacing(6)
        self.source = ComboBox(surface)
        self.source.addItem("Current correction and filters", userData="current")
        self.source.addItem("Raw input + filters", userData="raw")
        self.source.setToolTip(
            "Follow the main correction pipeline, or inspect the raw input through the selected filters."
        )
        self.window_span = ComboBox(surface)
        for seconds in (30, 60, 90, 120):
            self.window_span.addItem(f"{seconds} s", userData=seconds)
        self.window_span.setToolTip(
            "Choose the rolling time window retained in the spectrogram."
        )
        self.reset_view = PushButton("Reset view", surface)
        self.reset_view.setToolTip("Show the complete frequency and time range.")
        controls.addWidget(BodyLabel("Trace", surface), 0, 0)
        controls.addWidget(self.source, 0, 1, 1, 2)
        controls.addWidget(BodyLabel("Window", surface), 1, 0)
        controls.addWidget(self.window_span, 1, 1)
        controls.addWidget(self.reset_view, 1, 2)
        controls.setColumnStretch(1, 1)
        layout.addLayout(controls)
        self.spectrogram = _AnritsuSpectrogramWidget(surface)
        layout.addWidget(self.spectrogram, 1)
        self.status = CaptionLabel("Waiting for completed Live frames.", surface)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.source.currentIndexChanged.connect(
            lambda: self.source_changed.emit(str(self.source.currentData() or "raw"))
        )
        self.window_span.currentIndexChanged.connect(
            lambda: self.window_changed.emit(int(self.window_span.currentData() or 30))
        )
        self.reset_view.clicked.connect(self.spectrogram.reset_view)

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()


class _AnritsuSpectrumWindow(StationDialog):
    """Always-on-top mirror of the current, already acquired spectrum."""

    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Anritsu MS2830A — floating spectrum")
        self.setObjectName("anritsuSpectrumWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(1280, 820)
        self.setMinimumSize(860, 580)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=8)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Current spectrum", surface))
        self.start_live = PrimaryPushButton("Start Live", surface)
        self.stop_live = PushButton("Stop Live", surface)
        self.peak_table = PushButton("Add / select peaks", surface)
        header.addWidget(self.start_live)
        header.addWidget(self.stop_live)
        header.addWidget(self.peak_table)
        header.addStretch(1)
        self.tools_toggle = CheckBox("Analysis tools", surface)
        self.tools_toggle.setChecked(True)
        header.addWidget(self.tools_toggle)
        layout.addLayout(header)
        self.spectrum = SpectrumWorkbench(parent=surface)
        self.spectrum.set_title("Waiting for a completed spectrum")
        self.spectrum.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit="dBm"
        )
        self.workspace = QSplitter(Qt.Orientation.Horizontal, surface)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.addWidget(self.spectrum)
        self.workspace.addWidget(self.spectrum.tools)
        self.workspace.setStretchFactor(0, 1)
        self.workspace.setStretchFactor(1, 0)
        self.workspace.setSizes([880, 340])
        self.tools_toggle.toggled.connect(self.spectrum.tools.setVisible)
        layout.addWidget(self.workspace, 1)
        self.status = CaptionLabel(
            "This window mirrors completed traces; it does not start acquisition.",
            self,
        )
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()


class _AnritsuTraceDiagnosticsDialog(StationDialog):
    """Read-only view of the exact completed trace delivered to the page."""

    closed = Signal()
    preview_points = 64

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Anritsu MS2830A — trace diagnostics")
        self.setObjectName("anritsuTraceDiagnosticsDialog")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(780, 560)
        self.setMinimumSize(540, 400)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=8)

        title = StrongBodyLabel("Anritsu read-only diagnostics", surface)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        description = BodyLabel(
            "This window mirrors completed TRAC1 data already returned by the "
            "instrument. Opening it never starts or configures a measurement.",
            surface,
        )
        description.setWordWrap(True)
        description.setObjectName("muted")
        layout.addWidget(description)

        self.tabs = FluentTabView(surface)
        raw_page = QWidget(self.tabs)
        raw_layout = QVBoxLayout(raw_page)
        raw_layout.setContentsMargins(0, 0, 0, 0)
        raw_layout.setSpacing(8)
        self.raw_status = CaptionLabel(
            "Waiting for the first completed current spectrum.", raw_page
        )
        self.raw_status.setObjectName("muted")
        self.raw_status.setWordWrap(True)
        raw_layout.addWidget(self.raw_status)
        self.raw_text = PlainTextEdit(raw_page)
        self.raw_text.setObjectName("anritsuRawTracePreview")
        self.raw_text.setReadOnly(True)
        self.raw_text.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        self.raw_text.setAccessibleName("Raw Anritsu TRAC1 point preview")
        self.raw_text.setAccessibleDescription(
            "The first parsed frequency and dBm values from the most recently "
            "completed unprocessed TRAC1 frame."
        )
        self.raw_text.setPlainText(
            "No completed TRAC1 frame has been received in this application session."
        )
        raw_layout.addWidget(self.raw_text, 1)
        self.tabs.addTab(raw_page, "Raw TRAC1")

        hardware_page = QWidget(self.tabs)
        hardware_layout = QVBoxLayout(hardware_page)
        hardware_layout.setContentsMargins(0, 0, 0, 0)
        self.hardware_text = PlainTextEdit(hardware_page)
        self.hardware_text.setObjectName("anritsuHardwareDiagnostics")
        self.hardware_text.setReadOnly(True)
        self.hardware_text.setLineWrapMode(PlainTextEdit.LineWrapMode.WidgetWidth)
        self.hardware_text.setAccessibleName("Detected Anritsu hardware information")
        hardware_layout.addWidget(self.hardware_text)
        self.tabs.addTab(hardware_page, "Hardware")
        layout.addWidget(self.tabs, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def set_hardware_details(self, details: str) -> None:
        self.hardware_text.setPlainText(
            details or "Hardware information is not available yet."
        )

    def set_trace(self, trace: SpectrumTrace, *, received_frame: int) -> None:
        point_count = len(trace.powers_dbm)
        shown = min(self.preview_points, point_count)
        fingerprint = hashlib.sha256(
            np.asarray(trace.powers_dbm, dtype=np.float64).tobytes()
        ).hexdigest()
        start_hz = trace.frequencies_hz[0]
        stop_hz = trace.frequencies_hz[-1]
        lines = [
            f"received_frame: {received_frame}",
            f"acquired_at_utc: {trace.acquired_at_utc.isoformat()}",
            f"trace_name: {trace.trace_name}",
            f"point_count: {point_count}",
            f"start_frequency_hz: {start_hz:.12g}",
            f"stop_frequency_hz: {stop_hz:.12g}",
            f"power_values_sha256: {fingerprint}",
            "processing: none (before cleanup, reference subtraction, and averaging)",
            "",
            f"first {shown} parsed TRAC1 points:",
            "index\tfrequency_hz\tpower_dbm",
        ]
        lines.extend(
            f"{index}\t{frequency_hz:.12g}\t{power_dbm:.12g}"
            for index, (frequency_hz, power_dbm) in enumerate(
                zip(
                    trace.frequencies_hz[:shown],
                    trace.powers_dbm[:shown],
                    strict=True,
                )
            )
        )
        self.raw_text.setPlainText("\n".join(lines))
        self.raw_status.setText(
            f"Received frame {received_frame} · {point_count} points · "
            f"SHA-256 {fingerprint[:16]}…"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()


class AnritsuPage(QWidget):
    owns_viewport = True
    status = Signal(str)
    settings_readback_requested = Signal(object, object)
    quick_controls_requested = Signal()

    def __init__(
        self,
        controller: DeviceController,
        settings: StationSettings,
        *,
        single_sweep_available: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[str, LimitField] = {}
        self._single_sweep_configured = single_sweep_available
        self._trace_supported = True
        self._fetch_pending = False
        self._discard_cancelled_average_frame = False
        self._manual_trace_deadline_monotonic: float | None = None
        self._live_transition_pending = False
        self._execution_controlled = False
        self._pending_after_spectrum_configuration: str | None = None
        self._latest_trace: SpectrumTrace | None = None
        self._averaged_trace: SpectrumTrace | None = None
        self._reference_trace: SpectrumTrace | None = None
        self._reference_spectrum: ReferenceSpectrum | None = None
        self._spectrogram_buffer = _SpectrogramBuffer()
        self._spectrogram_window: _AnritsuSpectrogramWindow | None = None
        self._spectrum_window: _AnritsuSpectrumWindow | None = None
        self._trace_diagnostics_dialog: _AnritsuTraceDiagnosticsDialog | None = None
        self._received_trace_count = 0
        self._active_spectrum_unit = "dBm"
        self._cleanup_result: SpectrumCleanupResult | None = None
        self._analysis_error: str | None = None
        self._analysis_wait_reason: str | None = None
        self._detected_peaks: tuple[SpectrumPeak, ...] = ()
        self._analysis_generation = 0
        self._applied_analysis_generation = 0
        self._invalidated_before_generation = 0
        self._switching_trace_checkboxes = False
        self._candidate_traces: Mapping[str, SpectrumDisplayTrace] = {}
        self._display_revision = 0
        self._analysis_source_key: str | None = None
        self._analysis_source_snapshot = None
        self._analysis_raw_snapshot = None
        self._analysis_source_selection = "auto"
        self._display_state = build_display_state(
            raw=None,
            averaged=None,
            reference=None,
            reference_operation="none",
            visible={},
            frame_id=0,
        )
        self._analysis_controller = SpectrumAnalysisController(self)
        self._spectrogram_analysis_controller = SpectrumAnalysisController(self)
        self._spectrogram_analysis_controller.result.connect(self._spectrogram_filters_completed)
        self._spectrogram_analysis_controller.error.connect(self._spectrogram_filters_failed)
        self._spectrogram_filter_generation = 0
        self._spectrogram_filter_invalidated = 0
        self._spectrogram_filter_key = None
        self._spectrogram_filter_outcome = None
        self._spectrogram_filter_error = None
        self._shared_background_key = None
        self._background_config_pending = None
        self._background_config_snapshot = None
        self._background_preview_context = None
        self._background_config_error = None
        self._background_filter_enabled = False
        self._background_config_timer = QTimer(self)
        self._background_config_timer.setSingleShot(True)
        self._background_config_timer.setInterval(15_000)
        self._background_config_timer.timeout.connect(self._background_configuration_timed_out)
        self._analysis_controller.result.connect(self._analysis_completed)
        self._analysis_controller.error.connect(self._analysis_failed)
        self._analysis_parameters = settings.anritsu.preview.analysis_parameters()
        self._preview_statistics = None
        self._processing_quality_dialog = None
        self._analysis_settings_dialog: SpectrumAnalysisSettingsDialog | None = None
        self._peak_table_dialog: PeakTableDialog | None = None
        self._peak_tracking_window: PeakTrackingWindow | None = None
        self._additional_peak_trackers = {}
        self._tracked_peak_target_hz: float | None = None
        self._tracked_peak_gate_hz: float | None = None
        self._tracking_started_monotonic: float | None = None
        self._pending_reference_kind: str | None = None
        self._device_idn = ""
        self._last_configuration: AnritsuConfigurationSnapshot | None = None
        self._last_advanced_configuration: AdvancedSpectrumSnapshot | None = None
        self._last_signal_generator_snapshot: SignalGeneratorSnapshot | None = None
        self._save_readback_pending = False
        self._page_state = AnritsuPageState.IDLE
        self._capabilities: object | None = None
        self._averager = LinearPowerAverager()
        self._averaging_source_trace: SpectrumTrace | None = None
        self._averaging_active = False
        self._averaging_destination: str | None = None
        self._averaging_start_monotonic: float | None = None
        self._averaging_target_count = 0
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
        self._sg_output_known = False
        self._sg_configured = False
        self._manual_metadata_provider: Callable[[], tuple[ManualMetadataValue, ...]] | None = None
        self._manual_device_idn_provider: Callable[[], dict[str, str]] | None = None
        self._manual_settings_source_provider: Callable[[], str] | None = None
        self._manual_operator_context_provider: Callable[[], dict[str, object]] | None = None
        self._manual_elab_config_provider: Callable[[], tuple[bool, bool, str]] | None = None
        self._manual_elab_upload_callback: Callable[[Path], None] | None = None
        self._manual_archive: ManualSpectrumArchive | None = None
        self._manual_archive_thread = None
        self._manual_archive_worker = None
        self._manual_archive_result = None
        self._manual_archive_job = None
        self._manual_simulation = False
        self._latest_trace_is_execution_preview = False
        self._manual_archive_last_path: Path | None = None
        self._manual_last_mode: ManualSpectrumSaveMode | None = None
        self._manual_save_options: ManualSpectrumSaveOptions | None = None
        self._last_displayed_trace_names: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.fetch_live)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)
        self.hero_card = CardWidget(self)
        self.hero_card.setObjectName("anritsuHeroCard")
        self.hero_card.setProperty("stationSurface", "card")
        title_row = QHBoxLayout(self.hero_card)
        self._hero_title_row = title_row
        title_row.setContentsMargins(16, 10, 16, 10)
        title = StrongBodyLabel("Anritsu MS2830A · Spectrum")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.execution_badge = CaptionLabel("SWEEP CONTROLLED", self.hero_card)
        self.execution_badge.setObjectName("executionControlBadge")
        self.execution_badge.setProperty("deviceState", "verified")
        self.execution_badge.hide()
        title_row.addWidget(self.execution_badge)
        self.quick_controls_button = PushButton("Quick controls...", self.hero_card)
        self.quick_controls_button.setToolTip(
            "Open always-on-top Rigol and Keithley setpoint controls beside Live Spectrum."
        )
        self.quick_controls_button.clicked.connect(self.quick_controls_requested)
        title_row.addWidget(self.quick_controls_button)
        self.live_indicator = BodyLabel("●  LIVE OFF")
        self.live_indicator.setWordWrap(True)
        self.live_indicator.setMaximumWidth(420)
        self.live_indicator.setObjectName("anritsuLiveIndicator")
        self.live_indicator.setProperty("liveState", "off")
        self.live_indicator.setToolTip(
            "Confirmed Live acquisition state. The indicator changes to ON only after the "
            "instrument accepts Live startup."
        )
        title_row.addWidget(self.live_indicator)
        layout.addWidget(self.hero_card)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("anritsuWorkspaceSplitter")
        self.workspace_splitter.setProperty("stationSurface", "page")
        left_panel = QWidget()
        left_panel.setObjectName("anritsuControlPanel")
        left_panel.setProperty("stationSurface", "page")
        left_panel.setMinimumWidth(0)
        left_panel.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(10)
        left_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.setup_card = CardWidget(left_panel)
        self.setup_card.setObjectName("anritsuSetupCard")
        self.setup_card.setProperty("stationSurface", "card")
        setup_layout = QVBoxLayout(self.setup_card)
        setup_layout.setContentsMargins(20, 16, 20, 16)
        setup_layout.setSpacing(10)
        left_layout.addWidget(self.setup_card)
        right_panel = QWidget()
        right_panel.setObjectName("anritsuPlotPanel")
        right_panel.setProperty("stationSurface", "page")
        right_layout = QVBoxLayout(right_panel)
        self._plot_controls_layout = right_layout
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)
        setup_header = QHBoxLayout()
        setup_title = StrongBodyLabel("Acquisition setup")
        setup_title.setObjectName("sectionTitle")
        setup_header.addWidget(setup_title)
        setup_header.addStretch(1)
        self.advanced_spectrum_button = PushButton("Advanced…")
        self.advanced_spectrum_button.setToolTip(
            "Open qualified RBW, VBW, detector, attenuation, preamplifier and sweep-time controls."
        )
        self.advanced_spectrum_button.clicked.connect(self._show_advanced_spectrum_dialog)
        setup_header.addWidget(self.advanced_spectrum_button)
        self.hardware_info_button = PushButton("ⓘ")
        self.hardware_info_button.setObjectName("infoButton")
        self.hardware_info_button.setFixedSize(28, 28)
        self.hardware_info_button.setToolTip(
            "Show the latest raw TRAC1 values, frame fingerprint, hardware options "
            "and documented operating limits."
        )
        self.hardware_info_button.setAccessibleName("Open Anritsu trace diagnostics")
        self.hardware_info_button.clicked.connect(self._show_anritsu_hardware_info)
        setup_header.addWidget(self.hardware_info_button)
        self._advanced_dialog = self._build_advanced_spectrum_dialog()
        setup_layout.addLayout(setup_header)
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
        self.rbw_mode = self.configuration_panel.rbw_mode
        self.rbw = self.configuration_panel.rbw
        self.vbw_auto = self.configuration_panel.vbw_auto
        self.vbw_mode = self.configuration_panel.vbw_mode
        self.vbw = self.configuration_panel.vbw
        self._limit_fields = self.configuration_panel.limit_fields
        self._readback_dialog: AnritsuReadbackDialog | None = None
        self._last_full_readback: AnritsuFullConfigurationReadback | None = None
        setup_layout.addWidget(self.configuration_panel)
        self.refresh = SpinBox(self)
        self.refresh.setRange(10, 5000)
        self.refresh.setValue(
            round(
                parse_quantity(
                    self._station_settings.anritsu.acquisition.live_refresh_interval,
                    DIMENSION_TIME,
                ).si_value
                * 1000
            )
        )
        self.refresh.setSuffix(" ms")
        self.refresh.setToolTip(
            "Requested Live polling interval: 10 ms to 5 s. The effective frame rate is "
            "limited by the analyser sweep, VISA transfer and complete TRAC1 processing."
        )
        refresh_form = QFormLayout()
        refresh_form.addRow("Live refresh interval", self.refresh)
        _finish_spectrum_form(refresh_form)
        setup_layout.addLayout(refresh_form)
        self.hardware_option_info = BodyLabel(self)
        self.hardware_range_info = BodyLabel(self)
        self.hardware_option_info.hide()
        self.hardware_range_info.hide()
        self._hardware_details_text = ""
        self._update_anritsu_hardware_limits(())
        controls = QGridLayout()
        controls.setSpacing(6)
        self.read_configuration = PushButton("Read from instrument")
        self.read_and_save_configuration = PushButton("Read all & save defaults")
        self.configure_button = PrimaryPushButton("Apply configuration")
        self.single = PushButton("Acquire new spectrum")
        self.live = PrimaryPushButton("Start Live")
        self.abort_button = PushButton("Abort acquisition")
        self.abort_button.setObjectName("warningButton")
        for button in (
            self.read_configuration,
            self.read_and_save_configuration,
            self.configure_button,
            self.single,
            self.live,
            self.abort_button,
        ):
            button.setProperty("compact", True)
        controls.addWidget(self.read_configuration, 0, 0)
        controls.addWidget(self.read_and_save_configuration, 0, 1)
        controls.addWidget(self.configure_button, 1, 0, 1, 2)
        setup_layout.addLayout(controls)
        # Acquisition remains reachable while the settings drawer is closed.
        self.acquisition_commands = QWidget(self)
        command_layout = FlowLayout(self.acquisition_commands, isTight=True)
        self._acquisition_command_layout = command_layout
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setHorizontalSpacing(8)
        command_layout.setVerticalSpacing(6)
        self.toggle_acquisition_controls = ToggleButton("Instrument settings", self)
        self.toggle_acquisition_controls.setAccessibleName("Show analyser configuration and averaging")
        self.toggle_acquisition_controls.setToolTip("Configure frequency range, bandwidth, sweep and acquisition averaging.")
        self.single.setText("Acquire once")
        for button in (self.live, self.single, self.abort_button, self.toggle_acquisition_controls):
            command_layout.addWidget(button)
        layout.addWidget(self.acquisition_commands)
        self.processing_card = CardWidget(left_panel)
        self.processing_card.setObjectName("anritsuProcessingCard")
        self.processing_card.setProperty("stationSurface", "card")
        processing_layout = QGridLayout(self.processing_card)
        processing_layout.setContentsMargins(20, 16, 20, 16)
        processing_title = StrongBodyLabel("Averaging")
        processing_title.setObjectName("sectionTitle")
        processing_layout.setHorizontalSpacing(6)
        processing_layout.setVerticalSpacing(7)
        processing_layout.addWidget(processing_title, 0, 0, 1, 2)
        self.average_count = SpinBox(self)
        self.average_count.setRange(1, 9999)
        self.average_count.setValue(self._station_settings.anritsu.acquisition.application_average_count)
        self.acquire_average = PrimaryPushButton("Acquire averaged spectrum")
        self.cancel_average = PushButton("Cancel averaging")
        self.acquire_average.setProperty("compact", True)
        self.cancel_average.setProperty("compact", True)
        self.cancel_average.setEnabled(False)
        self.average_progress = ProgressBar(self)
        initial_average_count = self.average_count.value()
        self.average_progress.setRange(0, initial_average_count)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {initial_average_count}")
        processing_layout.addWidget(BodyLabel("Average count"), 1, 0)
        processing_layout.addWidget(self.average_count, 1, 1)
        processing_layout.addWidget(self.acquire_average, 2, 0)
        processing_layout.addWidget(self.cancel_average, 2, 1)
        processing_layout.addWidget(self.average_progress, 3, 0, 1, 2)
        self.average_stats_label = CaptionLabel("No averaging performed yet", self.processing_card)
        self.average_stats_label.setObjectName("averageStatsLabel")
        self.average_stats_label.setWordWrap(True)
        processing_layout.addWidget(self.average_stats_label, 4, 0, 1, 2)
        self.reference_dialog = self._create_workflow_dialog("Configure reference", "spectrumReferenceDialog")
        reference_surface = self.reference_dialog.use_modal_shell_content().surface
        reference_layout = self.reference_dialog.modal_content_layout(spacing=10)
        reference_layout.addWidget(StrongBodyLabel("Reference spectrum", reference_surface))
        reference_hint = CaptionLabel("Use a completed trace, acquire a new reference or load an HDF5 reference. Choose the operation below.", reference_surface)
        reference_hint.setWordWrap(True)
        reference_layout.addWidget(reference_hint)
        self.reference_status = CaptionLabel("No reference", reference_surface)
        self.reference_status.setWordWrap(True)
        reference_layout.addWidget(self.reference_status)
        self.acquire_single_reference = PushButton("Acquire reference once", reference_surface)
        self.acquire_single_reference.setAccessibleName("Acquire reference once")
        self._background_assistant = None
        self.correction_controls = SpectrumCorrectionControls(self)
        self.correction_controls.configure_background.clicked.connect(self._open_background_setup)
        self.reference_operation = self.correction_controls.operation
        self._reference_operation_selection = "difference_db"
        operation_row = QHBoxLayout()
        operation_row.addWidget(BodyLabel("Operation", reference_surface))
        operation_row.addWidget(self.reference_operation, 1)
        self.reference_operation.setMinimumWidth(194)
        self.reference_operation.setMaximumWidth(16777215)
        operation_row.addStretch(1)
        reference_layout.addLayout(operation_row)
        self._changing_correction = False
        self._pending_correction = None
        self.use_current_reference = PushButton("Use current trace")
        self.capture_reference = PrimaryPushButton("Acquire N× reference")
        self.clear_reference = PushButton("Clear reference")
        self.load_reference = PushButton("Load reference…")
        self.save_reference = PushButton("Save reference…")
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
        reference_actions = QWidget(reference_surface)
        reference_flow = FlowLayout(reference_actions, isTight=True)
        reference_flow.setContentsMargins(0, 0, 0, 0)
        reference_flow.setHorizontalSpacing(8)
        reference_flow.setVerticalSpacing(8)
        for control in (self.use_current_reference, self.acquire_single_reference, self.load_reference,
                        self.save_reference, self.clear_reference):
            reference_flow.addWidget(control)
        reference_layout.addWidget(reference_actions)
        reference_average = QHBoxLayout()
        self.reference_average_count = SpinBox(reference_surface)
        self.reference_average_count.setRange(1, 9999)
        self.reference_average_count.setValue(self.average_count.value())
        self.reference_average_count.setAccessibleName("Reference averaging sweep count")
        reference_average.addWidget(BodyLabel("Complete traces to average", reference_surface))
        reference_average.addWidget(self.reference_average_count)
        reference_average.addWidget(self.capture_reference)
        reference_layout.addLayout(reference_average)
        self.reference_average_status = CaptionLabel("No reference averaging in progress.", reference_surface)
        reference_layout.addWidget(self.reference_average_status)
        self.reference_cancel_average = PushButton("Cancel reference averaging", reference_surface)
        self.reference_cancel_average.clicked.connect(self.cancel_averaging)
        reference_layout.addWidget(self.reference_cancel_average)
        reference_layout.addStretch(1)
        self.average_progress.valueChanged.connect(
            lambda value: self.reference_average_status.setText(
                f"{value} / {self._averaging_target_count} complete reference traces"
                if self._averaging_destination == "reference" else "No reference averaging in progress."))
        reference_close = PushButton("Done", reference_surface)
        reference_close.clicked.connect(self.reference_dialog.accept)
        reference_layout.addWidget(reference_close)
        self.correction_controls.configure_reference.clicked.connect(self._open_reference_setup)
        self.correction_controls.reference.toggled.connect(self._reference_enabled_changed)
        self.reference_dialog.finished.connect(self._reference_setup_closed)
        self.show_raw = CheckBox("Raw")
        self.show_raw.setChecked(True)
        self.show_average = CheckBox("Averaged")
        self.show_reference = CheckBox("Reference")
        self.show_processed = CheckBox("Processed (Ref op)")
        self.show_analysis = CheckBox("Filtered")
        self.show_analysis.setChecked(True)
        trace_toggles = QHBoxLayout()
        trace_toggles.setSpacing(10)
        for checkbox in (
            self.show_raw,
            self.show_average,
            self.show_reference,
            self.show_processed,
            self.show_analysis,
        ):
            trace_toggles.addWidget(checkbox)
        trace_toggles.addStretch(1)
        self.clear_all_spectra_button = PushButton(
            "Clear all spectra…", self.processing_card
        )
        self.clear_all_spectra_button.setObjectName("clearAllSpectraButton")
        self.clear_all_spectra_button.setProperty("compact", True)
        left_layout.addWidget(self.processing_card)
        self.manual_save_card = CardWidget(left_panel)
        self.manual_save_card.setObjectName("anritsuManualSaveCard")
        self.manual_save_card.setProperty("stationSurface", "card")
        manual_layout = QVBoxLayout(self.manual_save_card)
        manual_layout.setContentsMargins(20, 16, 20, 16)
        manual_layout.setSpacing(7)
        manual_title = StrongBodyLabel("Manual spectrum archive")
        manual_title.setObjectName("sectionTitle")
        manual_layout.addWidget(manual_title)
        manual_hint = CaptionLabel(
            "Save the completed trace visible above. The archive can collect several "
            "spectra and selected last-confirmed device values without another hardware query."
        )
        manual_hint.setObjectName("muted")
        manual_hint.setWordWrap(True)
        manual_layout.addWidget(manual_hint)
        self.manual_save_status = BodyLabel("No completed spectrum ready to save.")
        self.manual_save_status.setObjectName("anritsuManualSaveStatus")
        self.manual_save_status.setWordWrap(True)
        manual_layout.addWidget(self.manual_save_status)
        self.manual_save_target = CaptionLabel("No append archive selected.")
        self.manual_save_target.setObjectName("muted")
        self.manual_save_target.setWordWrap(True)
        manual_layout.addWidget(self.manual_save_target)
        manual_buttons = QVBoxLayout()
        manual_buttons.setSpacing(6)
        self.configure_manual_spectrum = PushButton("Configure archive…")
        self.configure_manual_spectrum.setProperty("compact", True)
        self.save_manual_spectrum = PrimaryPushButton("Save current spectrum")
        self.save_manual_spectrum.setProperty("compact", True)
        self.close_manual_archive = PushButton("Close append session")
        self.close_manual_archive.setProperty("compact", True)
        self.close_manual_archive.setEnabled(False)
        manual_buttons.addWidget(self.configure_manual_spectrum)
        manual_buttons.addWidget(self.save_manual_spectrum)
        manual_buttons.addWidget(self.close_manual_archive)
        manual_layout.addLayout(manual_buttons)
        left_layout.addStretch(1)
        self.spectrum_plot = SpectrumPlotWidget(legend=True, responsive_toolbar=True)
        self.spectrum_plot.setProperty("stationSurface", "raised")
        self.spectrum_plot.set_title("Current spectrum")
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit="dBm"
        )
        self.spectrum_plot.setMinimumHeight(300)
        self.spectrum_plot.status_changed.connect(self.status.emit)
        self.info = CaptionLabel("Live stopped. Each frame is a complete trace, not a push stream.")
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.info.setObjectName("muted")
        self.analysis_tabs = FluentTabView(self)
        self.analysis_tabs.setObjectName("anritsuAnalysisTabs")
        # Hidden workspaces must not dictate the current plot's height.
        self.analysis_tabs.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        current_spectrum_tab = QWidget(self.analysis_tabs)
        current_spectrum_layout = QVBoxLayout(current_spectrum_tab)
        current_spectrum_layout.setContentsMargins(0, 0, 0, 0)
        current_spectrum_layout.setSpacing(4)
        self.open_floating_spectrum = PushButton("Floating window", current_spectrum_tab)
        self.open_floating_spectrum.setAccessibleName("Open floating spectrum")
        self.open_floating_spectrum.setToolTip("Open an always-on-top mirror without starting another acquisition.")
        self.spectrum_plot.toolbar_layout.insertWidget(0, self.open_floating_spectrum)
        self.signal_analysis_card = CardWidget(current_spectrum_tab)
        self.signal_analysis_card.setObjectName("anritsuSignalAnalysisCard")
        self.signal_analysis_card.setProperty("stationSurface", "card")
        analysis_controls = QVBoxLayout(self.signal_analysis_card)
        analysis_controls.setContentsMargins(12, 6, 12, 6)
        analysis_controls.setSpacing(6)
        self.filter_strip = QWidget(self.signal_analysis_card)
        filter_layout = FlowLayout(self.filter_strip)
        self._filter_strip_layout = filter_layout
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setHorizontalSpacing(8)
        filter_layout.setVerticalSpacing(4)
        filter_layout.addWidget(StrongBodyLabel("Filters", self.filter_strip))
        self.cleanup_filters: dict[str, CheckBox] = {"background": self.correction_controls.background}
        for key, short_title, tooltip in (
            ("narrow_reject", "Narrow peaks",
             "Remove narrow extrema of both signs before denoising. Protect the desired signal band in Parameters."),
            ("emi_reject", "EMI lines",
             "Reject stationary-line candidates using the displayed source history. Requires several Live frames; a desired carrier can also be stationary."),
            ("denoise", "Denoise",
             "Smooth noise after the selected rejection filters, preserving signal edges."),
        ):
            checkbox = CheckBox(short_title, self.filter_strip)
            checkbox.setObjectName(f"spectrumFilter_{key}")
            checkbox.setAccessibleName(SPECTRUM_FILTER_LABELS[key])
            checkbox.setToolTip(tooltip)
            self.cleanup_filters[key] = checkbox
            filter_layout.addWidget(checkbox)
        self.configure_analysis = TransparentPushButton("Filter settings…", self.filter_strip)
        self.configure_analysis.setAccessibleName("Digital filter parameters")
        self.configure_analysis.setToolTip("Configure Narrow peaks, EMI lines and Denoise. Changes apply immediately.")
        filter_layout.addWidget(self.configure_analysis)
        self.quick_comparison_label = StrongBodyLabel("Compare", self.filter_strip)
        filter_layout.addWidget(self.quick_comparison_label)
        self.quick_curves = {}
        for key, label in (("raw", "Raw"), ("background", "Raw − BG"), ("reference", "Raw − Ref")):
            checkbox = CheckBox(label, self.filter_strip)
            checkbox.setAccessibleName(f"Compare {label}")
            checkbox.setToolTip("Compare the same raw frame in power units. Uncheck all to return to the current filtered/averaged view.")
            checkbox.toggled.connect(lambda *_: self._refresh_spectrum_display(auto_range=True))
            self.quick_curves[key] = checkbox
            filter_layout.addWidget(checkbox)
        self.quick_power_unit = ComboBox(self.filter_strip)
        self.quick_power_unit.setAccessibleName("Spectrum power display units")
        for label, unit in (("Auto units", "auto"), ("Linear: W", "W"), ("Log: dBm", "dBm")):
            self.quick_power_unit.addItem(label, userData=unit)
        self.quick_power_unit.currentIndexChanged.connect(lambda *_: self._refresh_spectrum_display(auto_range=True))
        filter_layout.addWidget(self.quick_power_unit)
        self.open_peak_table = PrimaryPushButton("Peaks…", self.filter_strip)
        self.toggle_analysis_details = TransparentPushButton("...", self.filter_strip)
        self.toggle_analysis_details.setToolTip("More analysis and overlay options")
        self.toggle_analysis_details.setAccessibleName("Show analysis source, overlays and peak options")
        analysis_controls.addWidget(self.filter_strip)
        self.analysis_summary = CaptionLabel("Waiting for a completed spectrum.", self.signal_analysis_card)
        self.analysis_summary.setObjectName("muted")
        self.analysis_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        analysis_summary_row = QHBoxLayout()
        analysis_summary_row.setContentsMargins(0, 0, 0, 0)
        analysis_summary_row.setSpacing(12)
        analysis_controls.addLayout(analysis_summary_row)
        self.analysis_details = FlyoutViewBase(self)
        details_layout = QVBoxLayout(self.analysis_details)
        self._analysis_details_layout = details_layout
        details_layout.setContentsMargins(16, 16, 16, 16)
        details_layout.setSpacing(8)
        source_row = QHBoxLayout()
        source_row.addWidget(BodyLabel("Analyze trace", self.analysis_details))
        self.analysis_source = ComboBox(self.analysis_details)
        self.analysis_source.setToolTip("Automatically filter the current displayed trace, or choose one of the visible curves. The result uses the same units.")
        source_row.addWidget(self.analysis_source, 1)
        details_layout.addLayout(source_row)
        option_row = FlowLayout()
        option_row.setContentsMargins(0, 0, 0, 0)
        option_row.setHorizontalSpacing(16)
        self.overlay_analysis_source = CheckBox("Compare input", self.analysis_details)
        self.overlay_analysis_source.setAccessibleName("Compare spectrum before and after digital filters")
        self.overlay_analysis_source.setChecked(True)
        self.overlay_analysis_source.setToolTip("Show the input before digital filters alongside the filtered result, in the same units.")
        self.auto_peak_detection = CheckBox("Auto peaks", self.signal_analysis_card)
        self.auto_peak_detection.setChecked(True)
        self.highlight_peaks = CheckBox("Show peaks", self.analysis_details)
        self.highlight_peaks.setChecked(True)
        self.highlight_replacements = CheckBox("Show replacements", self.analysis_details)
        self.highlight_replacements.toggled.connect(self._sync_peak_markers)
        analysis_summary_row.addWidget(self.analysis_summary, 1)
        self.analyze_peaks = TransparentPushButton("Analyze now", self.analysis_details)
        self.clear_spectra_plot_button = TransparentPushButton("Clear spectra…", self.analysis_details)
        self.clear_spectra_plot_button.setObjectName("clearSpectraPlotButton")
        for widget in (self.highlight_replacements,
                       self.analyze_peaks, self.clear_spectra_plot_button):
            option_row.addWidget(widget)
        details_layout.addLayout(option_row)
        details_layout.addLayout(trace_toggles)
        details_layout.addWidget(self.clear_all_spectra_button)
        self.analysis_status = CaptionLabel("Waiting for a completed spectrum.", self.analysis_details)
        self.analysis_status.setObjectName("muted")
        self.analysis_status.setWordWrap(True)
        details_layout.addWidget(self.analysis_status)
        self.analysis_details_flyout = Flyout(self.analysis_details, self, isDeleteOnClose=False)
        self.analysis_details_flyout.hide()
        self.toggle_analysis_details.clicked.connect(self._open_analysis_details)
        self.spectrum_host = QStackedWidget(current_spectrum_tab)
        self.spectrum_host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.spectrum_host.addWidget(self.spectrum_plot)
        self.spectrum_empty = CardWidget(self.spectrum_host)
        empty_layout = QVBoxLayout(self.spectrum_empty)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        empty_layout.addStretch(1)
        self.spectrum_empty_heading = StrongBodyLabel("Ready for a spectrum", self.spectrum_empty)
        self.spectrum_empty_heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.spectrum_empty_heading)
        self.spectrum_empty_text = BodyLabel(
            "Acquire once or Start Live. Then choose a correction and configure the filters above.", self.spectrum_empty)
        self.spectrum_empty_text.setWordWrap(True)
        self.spectrum_empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.spectrum_empty_text)
        empty_layout.addStretch(1)
        self.spectrum_host.addWidget(self.spectrum_empty)
        self.spectrum_host.setCurrentWidget(self.spectrum_empty)
        current_spectrum_layout.addWidget(self.spectrum_host, 1)
        current_spectrum_layout.addWidget(self.info)
        self.analysis_tabs.addTab(current_spectrum_tab, "Current spectrum")

        self.correction_workspace = SpectrumCorrectionWorkspace(
            settings, single_sweep_available=single_sweep_available, parent=self,
        )
        self.correction_workspace.request_device.connect(self._request_correction_device)
        self.correction_workspace.busy_changed.connect(self._correction_busy_changed)
        self.correction_workspace.status_changed.connect(self.status.emit)
        self.correction_workspace.display_changed.connect(self._background_display_changed)
        self.correction_workspace.availability_changed.connect(self._shared_background_changed)
        spectrogram_tab = QWidget(self.analysis_tabs)
        spectrogram_layout = QVBoxLayout(spectrogram_tab)
        spectrogram_layout.setContentsMargins(0, 0, 0, 0)
        spectrogram_layout.setSpacing(6)
        spectrogram_controls = QGridLayout()
        spectrogram_controls.setHorizontalSpacing(6)
        spectrogram_controls.setVerticalSpacing(6)
        self.spectrogram_source = ComboBox(self)
        self.spectrogram_source.addItem("Current correction and filters", userData="current")
        self.spectrogram_source.addItem(
            "Raw input + filters", userData="raw"
        )
        self.spectrogram_source.setToolTip(
            "Spectrum and Spectrogram share the same correction, reference operation and filters."
        )
        self.spectrogram_window_span = ComboBox(self)
        for seconds in (30, 60, 90, 120):
            self.spectrogram_window_span.addItem(f"{seconds} s", userData=seconds)
        self.spectrogram_window_span.setToolTip(
            "Rolling spectrogram history: 30, 60, 90 or 120 seconds."
        )
        self.spectrogram_reset_view = PushButton("Reset view", self)
        self.spectrogram_reset_view.setToolTip(
            "Reset zoom and show the complete rolling spectrogram."
        )
        self.open_spectrogram_window = PushButton("Open floating window", self)
        self.open_spectrogram_window.setToolTip(
            "Open an always-on-top spectrogram that shares this buffer and Live session."
        )
        spectrogram_controls.addWidget(BodyLabel("Trace"), 0, 0)
        spectrogram_controls.addWidget(self.spectrogram_source, 0, 1)
        spectrogram_controls.addWidget(
            self.open_spectrogram_window, 0, 2
        )
        spectrogram_controls.addWidget(BodyLabel("Window"), 1, 0)
        spectrogram_controls.addWidget(self.spectrogram_window_span, 1, 1)
        spectrogram_controls.addWidget(self.spectrogram_reset_view, 1, 2)
        spectrogram_controls.setColumnStretch(1, 1)
        spectrogram_layout.addLayout(spectrogram_controls)
        self.spectrogram_plot = _AnritsuSpectrogramWidget(self)
        self.spectrogram_plot.setMinimumHeight(180)
        spectrogram_layout.addWidget(self.spectrogram_plot, 1)
        self.spectrogram_status = CaptionLabel(
            "Start Live to accumulate a rolling spectrogram.", self
        )
        self.spectrogram_status.setObjectName("muted")
        self.spectrogram_status.setWordWrap(True)
        spectrogram_layout.addWidget(self.spectrogram_status)
        self.analysis_tabs.addTab(spectrogram_tab, "Spectrogram")
        self.recording_dialog = self._create_workflow_dialog("Save and record spectra", "spectrumRecordingDialog")
        recording_surface = self.recording_dialog.use_modal_shell_content().surface
        recording_layout = self.recording_dialog.modal_content_layout(spacing=10)
        recording_tabs = FluentTabView(recording_surface)
        self.recording_tabs = recording_tabs
        save_scroll = ScrollArea(recording_tabs)
        save_scroll.setWidgetResizable(True)
        save_scroll.setWidget(self.manual_save_card)
        recording_tabs.addTab(save_scroll, "Save current spectrum")
        recording_tabs.addTab(self.correction_workspace, "Record a series")
        recording_layout.addWidget(recording_tabs, 1)
        close_recording = PushButton("Close", recording_surface)
        close_recording.clicked.connect(self.recording_dialog.accept)
        recording_layout.addWidget(close_recording)
        self.record_spectra = PushButton("Save / record...", self.acquisition_commands)
        self.record_spectra.clicked.connect(self._open_recording_setup)
        command_layout.addWidget(self.record_spectra)
        self.presentation_controls = self._build_presentation_controls(right_panel)
        self.compact_plot_settings = PushButton("Settings…", self.acquisition_commands)
        self.compact_plot_settings.setAccessibleName("Correction, filters, instrument, recording and plot settings")
        self.compact_plot_settings.hide()
        command_layout.addWidget(self.compact_plot_settings)
        self._presentation_popup_view = FlyoutViewBase(self)
        self._presentation_popup_layout = QVBoxLayout(self._presentation_popup_view)
        self._presentation_popup_layout.setContentsMargins(8, 8, 8, 8)
        self._compact_acquisition_actions = QWidget(self._presentation_popup_view)
        self._compact_acquisition_layout = QHBoxLayout(self._compact_acquisition_actions)
        self._compact_acquisition_layout.setContentsMargins(0, 0, 0, 0)
        self._presentation_popup_layout.addWidget(self._compact_acquisition_actions)
        self._presentation_popup = Flyout(self._presentation_popup_view, self, isDeleteOnClose=False)
        self._presentation_popup.hide()
        self._presentation_in_popup = False
        self._control_height_timer = QTimer(self)
        self._control_height_timer.setSingleShot(True)
        self._control_height_timer.timeout.connect(self._update_control_card_heights)
        for card in (self.correction_controls, self.signal_analysis_card, self.presentation_controls):
            card.installEventFilter(self)
        self.compact_plot_settings.clicked.connect(self._open_compact_plot_settings)
        self.spectrum_ranges = PlotRangeController(self.spectrum_plot.plot.getViewBox(), self, fit=self.spectrum_plot.auto_range)
        self.spectrogram_ranges = PlotRangeController(self.spectrogram_plot.plot.getViewBox(), self,
                                                     unit="s", fit=self.spectrogram_plot.reset_view)
        self.spectrum_plot.toolbar_buttons[0].clicked.connect(self.spectrum_ranges.fit)
        self._scale_dialog = None
        right_layout.addWidget(self.correction_controls)
        right_layout.addWidget(self.signal_analysis_card)
        right_layout.addWidget(self.presentation_controls)
        right_layout.addWidget(self.analysis_tabs, 1)
        self.analysis_tabs.currentChanged.connect(self._analysis_tab_changed)
        self.control_scroll = ScrollArea()
        self.control_scroll.setObjectName("anritsuControlScroll")
        self.control_scroll.setProperty("stationSurface", "page")
        self.control_scroll.viewport().setProperty("stationSurface", "page")
        self.control_scroll.setWidgetResizable(True)
        self.control_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.control_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.control_scroll.setWidget(left_panel)
        self.control_scroll.setMinimumWidth(320)
        self.workspace_splitter.addWidget(self.control_scroll)
        self.workspace_splitter.addWidget(right_panel)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([560, 1100])
        self.control_scroll.hide()
        self.toggle_acquisition_controls.toggled.connect(self._toggle_acquisition_panel)
        self._workspace_compact: bool | None = None
        self._short_workspace: bool | None = None
        self.mode_tabs = FluentTabView(self)
        self.mode_tabs.setObjectName("anritsuModeTabs")
        self.mode_tabs.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.mode_tabs.setProperty("stationSurface", "page")
        spectrum_tab = QWidget()
        spectrum_tab.setProperty("stationSurface", "page")
        spectrum_tab_layout = QVBoxLayout(spectrum_tab)
        spectrum_tab_layout.setContentsMargins(0, 0, 0, 0)
        spectrum_tab_layout.addWidget(self.workspace_splitter)
        self.mode_tabs.addTab(spectrum_tab, "Spectrum analyser")
        self.signal_generator_tab = self._build_signal_generator_tab()
        self.signal_generator_tab_index = self.mode_tabs.addTab(
            self.signal_generator_tab, "Signal generator"
        )
        self.mode_tabs.setTabVisible(self.signal_generator_tab_index, False)
        self.mode_tabs.navigation.hide()
        self.mode_tabs.currentChanged.connect(
            lambda index: self.acquisition_commands.setVisible(index == 0)
        )
        layout.addWidget(self.mode_tabs, 1)
        self.read_configuration.clicked.connect(self.read_configuration_from_instrument)
        self.read_and_save_configuration.clicked.connect(
            self.read_and_save_configuration_from_instrument
        )
        self.configure_button.clicked.connect(self.configure)
        self.single.clicked.connect(self.read_once)
        self.live.clicked.connect(self.toggle_live)
        self.refresh.valueChanged.connect(self._on_refresh_interval_changed)
        self.abort_button.clicked.connect(lambda: self._controller.call("abort_acquisition"))
        self.acquire_average.clicked.connect(self.start_averaging)
        self.cancel_average.clicked.connect(self.cancel_averaging)
        self.acquire_single_reference.clicked.connect(self.acquire_reference_once)
        self.use_current_reference.clicked.connect(self.capture_current_reference)
        self.capture_reference.clicked.connect(self.start_reference_averaging)
        self.clear_reference.clicked.connect(self.remove_reference)
        self.clear_all_spectra_button.clicked.connect(
            lambda: self.clear_all_spectra(confirm=True)
        )
        self.clear_spectra_plot_button.clicked.connect(
            lambda: self.clear_all_spectra(confirm=True)
        )
        self.load_reference.clicked.connect(self.load_reference_file)
        self.save_reference.clicked.connect(self.save_reference_file)
        self.configure_manual_spectrum.clicked.connect(self._show_manual_save_dialog)
        self.save_manual_spectrum.clicked.connect(self._save_configured_manual_spectrum)
        self.close_manual_archive.clicked.connect(self.close_manual_archive_session)
        self.reference_operation.currentIndexChanged.connect(
            self._reference_operation_changed
        )
        for checkbox in (
            self.show_raw,
            self.show_average,
            self.show_reference,
            self.show_processed,
            self.show_analysis,
        ):
            checkbox.toggled.connect(
                lambda checked, cb=checkbox: self._on_trace_checkbox_toggled(cb, checked)
            )
        self.configure_analysis.clicked.connect(self._open_analysis_settings)
        self.spectrogram_source.currentIndexChanged.connect(
            self._spectrogram_controls_changed
        )
        self.spectrogram_window_span.currentIndexChanged.connect(
            self._spectrogram_controls_changed
        )
        self.spectrogram_reset_view.clicked.connect(self.spectrogram_plot.reset_view)
        self.spectrogram_reset_view.clicked.connect(self.spectrogram_ranges.fit)
        self.open_spectrogram_window.clicked.connect(
            self._open_spectrogram_window
        )
        self.open_floating_spectrum.clicked.connect(self._open_spectrum_window)
        for checkbox in self.cleanup_filters.values():
            checkbox.toggled.connect(self._signal_analysis_controls_changed)
        self.correction_controls.power_average.currentIndexChanged.connect(self._power_average_changed)
        self.correction_controls.background_tools.clicked.connect(self._open_processing_quality)
        self.correction_workspace.interference_mode.currentIndexChanged.connect(self._preview_model_changed)
        self.overlay_analysis_source.toggled.connect(self._display_controls_changed)
        self.analysis_source.currentIndexChanged.connect(
            self._analysis_source_changed
        )
        self.auto_peak_detection.toggled.connect(
            self._signal_analysis_controls_changed
        )
        self.highlight_peaks.toggled.connect(self._sync_peak_markers)
        self.analyze_peaks.clicked.connect(
            lambda: self._analyze_current_spectrum(force=True)
        )
        self.open_peak_table.clicked.connect(self._open_peak_table)
        self.spectrum_plot.peak_selected.connect(self._plot_peak_selected)
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._device_state_changed)
        help_items = {
            self.read_configuration: "Read Start, Stop, Reference level, and Points from the connected analyser. This sends query commands only and never changes the instrument or configured safety limits.",
            self.read_and_save_configuration: "Read the current basic and advanced Spectrum settings using query commands, preview them, then save them as settings.yml defaults. No instrument setting or safety limit is changed.",
            self.single: "Start one fresh, qualified Anritsu sweep, wait for completion, then read TRAC1. It does not change spectrum settings or enable RF output.",
            self.average_count: "Number of complete spectra to average. 200 is common in the Thatec workflow. Averaging is performed in linear mW, not directly in dBm.",
            self.acquire_average: "Acquire N complete single sweeps using the qualified protocol and average power in linear mW.",
            self.cancel_average: "Stop temporal averaging. Already collected temporary frames are discarded; completed raw/reference data are unchanged.",
            self.acquire_single_reference: "Passively fetch one new TRAC1 frame and store that completed frame as the reference. No analyser setting is changed.",
            self.use_current_reference: "Use the latest already acquired trace as the reference without sending a VISA command.",
            self.capture_reference: "Passively acquire and average N traces, then store that completed average as the in-memory reference spectrum.",
            self.clear_reference: "Remove the in-memory reference and all derived display results. It does not delete raw measurements from HDF5.",
            self.clear_all_spectra_button: "Clear all captured in-memory spectra, temporal average, reference spectrum, and display curves to start a fresh acquisition. Saved HDF5 files on disk are not affected.",
            self.clear_spectra_plot_button: "Clear all in-memory traces, reference spectrum, and display plots to start from scratch. Saved HDF5 files on disk are not affected.",
            self.load_reference: "Load a Lab Control reference HDF5 artefact. The current analyser is not queried or configured.",
            self.save_reference: "Save the complete reference trace and provenance as a thaTEC/PyThat-compatible HDF5 artefact.",
            self.configure_manual_spectrum: "Choose the manual archive destination, file policy, trace variant and confirmed metadata. This does not create a file or query the instrument.",
            self.save_manual_spectrum: "Save the latest completed spectrum using the accepted manual archive configuration. This does not open the configuration dialog or query the instrument.",
            self.close_manual_archive: "Close the current append session as a valid resumable HDF5 file. It does not delete data.",
            self.reference_operation: "Choose point-wise reference mathematics. Difference in dB equals a power ratio expressed logarithmically; linear operations first convert dBm to mW.",
            self.analysis_source: "Select the currently displayed Raw, Averaged, Reference, or Processed trace used by live analysis.",
            self.show_raw: "Show the latest untouched trace returned by Anritsu.",
            self.show_average: "Show the application-side linear-power average.",
            self.show_reference: "Overlay the captured reference spectrum.",
            self.show_processed: "Show the selected reference operation result. Non-dBm results use their own Y-axis unit and hide incompatible overlays.",
            self.show_analysis: "Show the background signal cleanup/analysis trace derived from the selected source.",
            self.configure_analysis: "Configure Narrow peaks, EMI lines and Denoise. Changes apply immediately.",
        }
        for widget, description in help_items.items():
            widget.setToolTip(description)
            widget.setToolTipDuration(25_000)
        self._apply_page_state()
        self._update_correction_summary()
        self.analysis_summary.hide()
        self._analysis_parameters_applied(self._analysis_parameters)

    def _create_workflow_dialog(self, title: str, name: str) -> StationDialog:
        # FramelessDialog creates its native window immediately. This page is
        # still standalone here; embedding it later destroys its old HWND and
        # Windows destroys dialogs owned by that HWND as well. Qt then keeps
        # a stale window ID despite reporting the dialog as visible. Give these
        # eager dialogs their real owner when opened, after shell insertion.
        dialog = StationDialog(resizable=True)
        self.destroyed.connect(dialog.deleteLater)
        dialog.setWindowTitle(title)
        dialog.setObjectName(name)
        dialog.resize(760, 600)
        return dialog

    def _build_presentation_controls(self, parent: QWidget) -> CardWidget:
        card = CardWidget(parent)
        card.setObjectName("spectrumPresentationControls")
        card.setProperty("stationSurface", "card")
        flow = FlowLayout(card, isTight=True)
        flow.setContentsMargins(12, 8, 12, 8)
        flow.setHorizontalSpacing(10)
        flow.setVerticalSpacing(6)
        flow.addWidget(StrongBodyLabel("Plot", card))
        flow.addWidget(self.auto_peak_detection)
        self.highlight_peaks.setText("Markers")
        self._peak_marker_title = BodyLabel("Max. peaks", card)
        self.peak_count = SpinBox(card)
        self.peak_count.setRange(1, 100)
        self.peak_count.setValue(self._analysis_parameters.peak_max_count)
        self.peak_count.setFixedWidth(128)
        self.peak_count.setAccessibleName("Maximum detected peak count")
        self.peak_count.valueChanged.connect(
            lambda value: self._analysis_parameters_applied(replace(self._analysis_parameters, peak_max_count=value)))
        self.peak_settings = TransparentPushButton("Peak settings…", card)
        self.peak_settings.clicked.connect(lambda: self._open_analysis_settings(focus_peaks=True))
        self.open_peak_table.setText("Peak table…")
        self.plot_scales = PushButton("Axes…", card)
        self.plot_scales.setAccessibleName("Set horizontal and vertical plot limits")
        self.plot_scales.clicked.connect(self._open_plot_scales)
        self.toggle_analysis_details.setText("View options…")
        for widget in (self.highlight_peaks, self.overlay_analysis_source, self._peak_marker_title, self.peak_count, self.peak_settings,
                       self.open_peak_table, self.plot_scales, self.toggle_analysis_details):
            flow.addWidget(widget)
        return card

    def _show_workflow_dialog(self, dialog: StationDialog) -> None:
        if self._presentation_popup.isVisible():
            # Close the Qt Popup before activating another top-level window.
            # Otherwise its mouse grab/focus can hide the newly opened dialog.
            self._presentation_popup.hide()
            QTimer.singleShot(0, lambda: self._show_workflow_dialog(dialog))
            return
        host = self.window()
        # Reference/recording dialogs are built before this page is inserted
        # into the Fluent shell. Their eagerly created native HWND must be
        # owned by the current top-level window, not the formerly standalone
        # (now hidden child) page. Background is built on demand and avoids it.
        if dialog.parentWidget() is not host:
            dialog.setParent(host, dialog.windowFlags())
        preferred = (720, 540) if dialog is self.reference_dialog else (
            (1000, 700) if dialog is self.recording_dialog else (620, 340))
        if dialog.isMinimized():
            dialog.showNormal()
        dialog.resize(min(preferred[0], max(420, self.window().width() - 60)),
                      min(preferred[1], max(340, self.window().height() - 80)))
        screen = host.screen()
        if screen is not None:
            available = screen.availableGeometry()
            target = host.frameGeometry().center() - dialog.rect().center()
            dialog.move(
                max(available.left(), min(target.x(), available.right() - dialog.width() + 1)),
                max(available.top(), min(target.y(), available.bottom() - dialog.height() + 1)),
            )
        if dialog.windowHandle() is not None and host.windowHandle() is not None:
            dialog.windowHandle().setTransientParent(host.windowHandle())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if dialog is self.reference_dialog:
            self.status.emit("Anritsu: reference configuration window opened")

    def _open_recording_setup(self) -> None:
        self._show_workflow_dialog(self.recording_dialog)

    def _open_reference_setup(self) -> None:
        self.status.emit("Anritsu: opening reference configuration")
        try:
            self._show_workflow_dialog(self.reference_dialog)
        except Exception as exc:
            message = f"Could not open reference configuration: {exc}"
            self.status.emit(message)
            self.banner.show_message(message, severity="error")

    def _reference_setup_closed(self, _result: int) -> None:
        if self._pending_correction == "reference":
            self._pending_correction = None
        self._update_correction_summary()

    def _open_plot_scales(self) -> None:
        if self._scale_dialog is not None:
            self._scale_dialog.close()
        spectrogram = self.analysis_tabs.currentIndex() == 1
        controller = self.spectrogram_ranges if spectrogram else self.spectrum_ranges
        dialog = PlotScaleDialog(controller, self, spectrogram=spectrogram)
        self._scale_dialog = dialog
        dialog.finished.connect(self._plot_scales_closed)
        self._show_workflow_dialog(dialog)

    def _open_compact_plot_settings(self) -> None:
        self._presentation_popup_view.setFixedWidth(min(680, max(360, self.width() - 40)))
        self._update_control_card_heights()
        self._presentation_popup.adjustSize()
        self._presentation_popup.exec(self.compact_plot_settings, FlyoutAnimationType.NONE)

    def _update_control_card_heights(self) -> None:
        """Reserve actual wrapped-row height instead of FlowLayout's one-row minimum."""
        if self._presentation_in_popup:
            margins = self._presentation_popup_layout.contentsMargins()
            width = self._presentation_popup_view.width() - margins.left() - margins.right()
        else:
            parent = self._plot_controls_layout.parentWidget()
            margins = self._plot_controls_layout.contentsMargins()
            width = parent.width() - margins.left() - margins.right()
        for card in (self.correction_controls, self.signal_analysis_card, self.presentation_controls):
            layout = card.layout()
            height = layout.totalHeightForWidth(max(1, width))
            if height < 0:
                height = layout.sizeHint().height()
            height = max(height, layout.minimumSize().height())
            if card.minimumHeight() != height:
                card.setMinimumHeight(height)

    def eventFilter(self, watched, event) -> bool:
        if event.type() in {QEvent.Type.Resize, QEvent.Type.LayoutRequest, QEvent.Type.FontChange}:
            # Coalesce wrapping, font and visibility changes into one layout pass.
            self._control_height_timer.start(0)
        return super().eventFilter(watched, event)

    def _plot_scales_closed(self, _result: int) -> None:
        dialog, self._scale_dialog = self._scale_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _reference_enabled_changed(self, enabled: bool) -> None:
        if self._changing_correction:
            return
        if enabled and self._reference_trace is None:
            self.correction_controls.reference.blockSignals(True)
            self.correction_controls.reference.setChecked(False)
            self.correction_controls.reference.blockSignals(False)
            self._pending_correction = "reference"
            self._open_reference_setup()
            self._update_correction_summary()
            return
        if enabled:
            self.cleanup_filters["background"].setChecked(False)
            if self.reference_operation.currentData() == "none":
                self.reference_operation.setCurrentIndex(self.reference_operation.findData(self._reference_operation_selection))
        else:
            self.reference_operation.setCurrentIndex(self.reference_operation.findData("none"))
        self._update_correction_summary()

    def _update_correction_summary(self) -> None:
        if self._pending_correction:
            text = f"Configure {self._pending_correction} to enable this correction."
        elif self.cleanup_filters["background"].isChecked():
            profile = self.correction_workspace._profile
            problem = self._background_config_error or (
                self._spectrogram_filter_error if self.analysis_tabs.currentIndex() == 1 else self._analysis_error)
            active = (self._spectrogram_filter_outcome is not None and self._spectrogram_filter_outcome.unit == "W"
                      if self.analysis_tabs.currentIndex() == 1 else
                      self._cleanup_result is not None and "background" in self._cleanup_result.applied_modes)
            if problem:
                text = f"Background unavailable · {problem}"
            elif self._background_config_pending is not None:
                text = "Background pending · checking current analyzer settings…"
            elif profile is None:
                text = "Background unavailable · configure a matching background."
            else:
                text = (f"Background {'active' if active else 'configured'} · {profile.sweep_count} sweeps"
                        " · signed power [W]")
            if profile is not None and not profile.signal_free_qualified:
                text += " · unqualified background · display preview"
        elif self.reference_operation.currentData() != "none":
            reference = self._reference_spectrum
            detail = (f"{reference.average_count} trace(s) · {reference.points} points"
                      if reference is not None else "reference unavailable")
            text = f"Reference · {self.reference_operation.currentText()} · {detail}"
        else:
            text = "No correction · raw spectrum [dBm]"
        if self._analysis_parameters.temporal_average_frames > 1:
            stats = self._preview_statistics
            count = stats.frame_count if stats is not None else 0
            text = f"Power average {count}/{self._analysis_parameters.temporal_average_frames} frames · {text}"
        model = self.correction_workspace.interference_mode.currentData()
        if model is not None and self.cleanup_filters["background"].isChecked():
            text += f" · drift model {model.model_id}"
        self.correction_controls.summary.setText(text)
        self.correction_controls.summary.setVisible(bool(self._pending_correction)
            or self.cleanup_filters["background"].isChecked() or self.reference_operation.currentData() != "none"
            or self._analysis_parameters.temporal_average_frames > 1)

    def _toggle_acquisition_panel(self, visible: bool) -> None:
        self.control_scroll.setVisible(visible)
        if visible:
            self.workspace_splitter.setSizes(
                [120, max(180, self.workspace_splitter.height() - 120)]
                if self._workspace_compact else [560, max(440, self.workspace_splitter.width() - 560)]
            )

    def _open_background_setup(self, *_args):
        if self._background_assistant is not None:
            self._background_assistant.show()
            self._background_assistant.raise_()
            return
        if not self.correction_controls.configure_background.isEnabled():
            return
        directory = Path(str(self._station_settings.storage.get("output_directory", "./measurements")))
        dialog = BackgroundCorrectionAssistant(
            self.correction_workspace, directory, self, record_available=self._can_collect_background(),
        )
        dialog.resize(min(620, max(400, self.window().width() - 40)),
                      min(620, max(380, self.window().height() - 60)))
        self._background_assistant = dialog
        dialog.prepare_requested.connect(self._prepare_guided_background)
        dialog.filter_ready.connect(self._guided_background_filter_ready)
        dialog.finished.connect(self._background_assistant_closed)
        dialog.show()
        if not self._can_collect_background():
            dialog.background_source.setCurrentIndex(dialog.background_source.findData("load"))

    def _can_collect_background(self) -> bool:
        return (self._page_state in {AnritsuPageState.IDLE, AnritsuPageState.ERROR, AnritsuPageState.LIVE}
                and self._trace_supported and self._single_sweep_configured and not self._live_transition_pending)

    def _request_correction_device(self, operation, payload=None):
        self._controller.call(operation, payload)

    def _analysis_tab_changed(self, *_args):
        index = self.analysis_tabs.currentIndex()
        for widget in (self.auto_peak_detection, self.highlight_peaks, self._peak_marker_title,
                       self.peak_count, self.peak_settings, self.open_peak_table, self.open_floating_spectrum,
                       self.overlay_analysis_source):
            widget.setVisible(index == 0)
        self._peak_marker_title.setVisible(index == 0 and self.width() >= 1300)
        self.peak_count.setVisible(index == 0 and self.width() >= 1300)
        if self._scale_dialog is not None:
            self._scale_dialog.close()
        if index == 1:
            self._refresh_spectrogram_display()
        elif index == 0:
            self._invalidate_analysis_results()
            self._refresh_spectrum_display()
            if self._latest_trace is not None:
                self._update_signal_analysis(self._latest_trace)

    def _preview_page_active(self):
        # Unshown pages remain inspectable by local callers and tests. In the
        # visible application, inactive routes accumulate frames only.
        window = self.window()
        return not window.isVisible() or self.isVisibleTo(window)

    def _ordinary_preview_active(self):
        return ((self.analysis_tabs.currentIndex() == 0 and self._preview_page_active())
                or (self._spectrum_window is not None and self._spectrum_window.isVisible())
                or (self._peak_tracking_window is not None and self._peak_tracking_window.isVisible())
                or any(window.isVisible() for window in self._additional_peak_trackers))

    def _spectrogram_preview_active(self):
        return ((self.analysis_tabs.currentIndex() == 1 and self._preview_page_active())
                or (self._spectrogram_window is not None and self._spectrogram_window.isVisible()))

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._resume_visible_preview)

    def _resume_visible_preview(self):
        self._analysis_tab_changed()

    def _invalidate_spectrogram_filters(self):
        self._spectrogram_filter_invalidated = self._spectrogram_filter_generation
        self._spectrogram_filter_key = None
        self._spectrogram_filter_outcome = None
        self._spectrogram_filter_error = None

    def _filtered_spectrogram_matrix(self, source, window_s, *, raw_unprocessed=False):
        snapshot = self._spectrogram_buffer.frame_snapshot(window_s, processing=not raw_unprocessed,
            warmup_frames=0 if raw_unprocessed else max(23, self._analysis_parameters.temporal_average_frames - 1))
        if snapshot is None:
            return None
        frequencies, timestamps, rows = snapshot
        modes = () if raw_unprocessed else tuple(mode for mode in self._selected_cleanup_modes() if source != "raw" or mode != "background")
        context, profile = None, None
        unit = "dBm"
        reference, operation = None, "none"
        if "background" in modes:
            context, profile = self._background_for_filter(frequencies)
        elif source == "current" and self.reference_operation.currentData() != "none":
            self._validate_display_reference(frequencies)
            reference = self._reference_trace.powers_dbm
            operation = str(self.reference_operation.currentData() or "none")
        model = self.correction_workspace.interference_mode.currentData() if "background" in modes else None
        cache_key = (source, modes, self._analysis_parameters, frequencies, window_s, raw_unprocessed,
                     self._spectrogram_buffer.configuration_generation,
                     profile.content_hash if profile is not None else None,
                     id(self._reference_trace) if reference is not None else None, operation,
                     model.content_hash if model is not None else None)
        request_key = (cache_key, timestamps)
        if request_key != self._spectrogram_filter_key:
            self._spectrogram_filter_key = request_key
            self._spectrogram_filter_error = None
            self._spectrogram_filter_generation += 1
            self._spectrogram_analysis_controller.submit(SpectrogramAnalysisRequest(
                self._spectrogram_filter_generation, frequencies, timestamps, rows, unit,
                modes, self._analysis_parameters, cache_key, profile, context, reference, operation, model,
                timestamps[-1] - window_s))
        outcome = self._spectrogram_filter_outcome
        if self._spectrogram_filter_error is not None:
            raise ValueError(self._spectrogram_filter_error)
        if outcome is None or outcome.cache_key != cache_key:
            return None
        elapsed = np.asarray(outcome.timestamps_s) - outcome.timestamps_s[-1]
        return np.asarray(outcome.frequencies_hz), elapsed, outcome.matrix, outcome.unit, outcome.method

    def _spectrogram_filters_completed(self, outcome):
        if (not isinstance(outcome, SpectrogramAnalysisOutcome)
                or outcome.generation <= self._spectrogram_filter_invalidated
                or self._spectrogram_filter_key is None
                or outcome.cache_key != self._spectrogram_filter_key[0]):
            return
        self._spectrogram_filter_outcome = outcome
        self._preview_statistics = outcome.statistics
        self._spectrogram_filter_error = None
        self._refresh_spectrogram_display()
        self._update_correction_summary()

    def _spectrogram_filters_failed(self, generation, message):
        if generation <= self._spectrogram_filter_invalidated:
            return
        self._spectrogram_filter_outcome = None
        self._spectrogram_filter_error = message
        self.spectrogram_plot.clear()
        self.spectrogram_status.setText(f"Filters unavailable: {message}")
        self._update_correction_summary()

    def _shared_background_changed(self):
        workspace = self.correction_workspace
        profile, context = workspace._profile, workspace._context
        key = (id(profile), context.context_id) if profile is not None and context is not None else None
        if key == self._shared_background_key:
            return
        self._shared_background_key = key
        self._spectrogram_buffer.reset_processing()
        self._preview_statistics = None
        self._update_correction_summary()
        self._background_config_error = None
        self._background_config_snapshot = None
        self._invalidate_spectrogram_filters()
        if self.cleanup_filters["background"].isChecked():
            self._invalidate_analysis_results()
            self._refresh_spectrum_display()
            if self._latest_trace is not None:
                self._update_signal_analysis(self._latest_trace)
            self._refresh_spectrogram_display()

    def _background_for_filter(self, frequencies_hz):
        workspace = self.correction_workspace
        profile, context = workspace._profile, workspace._context
        if profile is None or context is None:
            raise ValueError("Record or load a background in Background setup.")
        if profile.context_id != context.context_id or not frequency_grids_match(frequencies_hz, context.frequencies_hz):
            raise ValueError("Background frequency grid or acquisition context differs.")
        maximum_age = self._station_settings.anritsu.spectrum_correction.processor_config().maximum_reference_age_s
        if maximum_age is not None and datetime.now(UTC).timestamp() - profile.completed_at_s > maximum_age:
            raise ValueError("Background is stale. Record a new background.")
        generation = self._latest_trace.configuration_generation if self._latest_trace is not None else None
        snapshot = self._background_config_snapshot
        if snapshot is None or snapshot[0] != generation:
            if self._execution_controlled:
                raise ValueError("Background preview verification is paused while the Run Engine owns acquisition.")
            if self._background_config_error is not None:
                raise ValueError(self._background_config_error)
            if self._background_config_pending is None:
                operation = f"read_background_filter_configuration:{uuid4().hex}"
                self._background_config_pending = (operation, generation)
                self._background_config_timer.start()
                self._controller.call(operation)
            raise ValueError("Checking analyzer settings for Background…")
        if snapshot[1] != context.configuration_fingerprint:
            raise ValueError("Analyzer settings differ from the background. Record or load a matching background.")
        # Archives deliberately retain their original qualification evidence.
        # A matching readback verifies the current *preview* settings; it does
        # not qualify signal absence, independent samples or uncertainty.
        verified = self._background_preview_context
        if (verified is None or verified.context_id != context.context_id
                or verified.configuration_generation != generation):
            verified = replace(context, settings_verified=True,
                independent_sweeps_qualified=False,
                configuration_generation=generation if generation is not None else context.configuration_generation)
            self._background_preview_context = verified
        return verified, profile

    def _background_configuration_timed_out(self):
        pending = self._background_config_pending
        if pending is not None:
            self._error(pending[0], "Background configuration readback timed out.")

    def _background_configuration_received(self, operation, result):
        pending = self._background_config_pending
        if pending is None or operation != pending[0]:
            return
        self._background_config_timer.stop()
        self._background_config_pending = None
        try:
            full, advanced = result
            if not isinstance(full, AnritsuFullConfigurationReadback) or not isinstance(advanced, AdvancedSpectrumSnapshot):
                raise TypeError("Invalid analyzer configuration readback.")
            self._background_config_snapshot = (
                pending[1], spectrum_configuration_fingerprint(full, advanced, self._device_idn))
            self._background_config_error = None
        except (TypeError, ValueError) as exc:
            self._background_config_error = str(exc)
        self._invalidate_analysis_results()
        self._invalidate_spectrogram_filters()
        self._refresh_spectrum_display()
        if self._latest_trace is not None:
            self._update_signal_analysis(self._latest_trace)
        self._refresh_spectrogram_display()

    def _revalidate_background_preview(self):
        if (self._analysis_controller._closed or self.correction_workspace.running
                or not self.cleanup_filters["background"].isChecked() or self._latest_trace is None):
            return
        self._invalidate_analysis_results()
        self._refresh_spectrum_display()
        self._update_signal_analysis(self._latest_trace)
        self._refresh_spectrogram_display()

    def _prepare_guided_background(self):
        if self._timer.isActive():
            self.toggle_live()

    def _guided_background_filter_ready(self):
        self.analysis_tabs.setCurrentIndex(0)
        self._pending_correction = None
        if self.cleanup_filters["background"].isChecked():
            self._signal_analysis_controls_changed()
        else:
            self.cleanup_filters["background"].setChecked(True)
        message = "Background enabled. Restore your measurement operating point, then press Start Live."
        self.info.setText(message)
        self.status.emit(message)
        self._update_correction_summary()

    def _background_assistant_closed(self, _result):
        dialog, self._background_assistant = self._background_assistant, None
        if dialog is not None:
            dialog.deleteLater()
        if self._pending_correction == "background":
            self._pending_correction = None
        self._update_correction_summary()

    def _open_analysis_details(self) -> None:
        self.analysis_details.setFixedWidth(min(560, max(300, self.width() - 64)))
        self.analysis_details_flyout.adjustSize()
        target = self.toggle_analysis_details.mapToGlobal(QPoint(0, self.toggle_analysis_details.height()))
        window = self.window()
        bounds = window.rect().translated(window.mapToGlobal(QPoint(0, 0)))
        popup = self.analysis_details_flyout
        target.setX(max(bounds.left(), min(target.x(), bounds.right() - popup.width())))
        target.setY(max(bounds.top(), min(target.y(), bounds.bottom() - popup.height())))
        popup.exec(target, FlyoutAnimationType.NONE)

    def _set_analysis_status(self, text: str) -> None:
        self.analysis_status.setText(text)
        self.analysis_summary.setToolTip(text)
        cleanup = self._cleanup_result
        if self._analysis_error is not None or cleanup is None:
            summary = text.replace(" on the background CPU worker", "")
        else:
            source = self._candidate_traces.get(self._analysis_source_key)
            source_label = f"{source.label} [{source.unit}]" if source is not None else "Spectrum"
            if source is not None and source.unit != cleanup.unit:
                source_label += f" → signed {cleanup.unit}"
            count = len([mode for mode in self._selected_cleanup_modes() if mode != "background"])
            summary = f"{source_label} · {count} filter(s) · {len(cleanup.modified_bin_indices)} bins changed"
            if cleanup.notes:
                summary += " · " + " ".join(cleanup.notes)
        self.analysis_summary.setText(summary if len(summary) <= 120 else summary[:117] + "…")
        self.analysis_summary.setVisible(any(mode != "background" for mode in self._selected_cleanup_modes())
                                        or self._analysis_error is not None)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        narrow = event.size().width() < 1000
        self.quick_comparison_label.setVisible(not narrow)
        self._filter_strip_layout.setHorizontalSpacing(4 if narrow else 8)
        self.cleanup_filters["narrow_reject"].setText("Peaks" if narrow else "Narrow peaks")
        self.cleanup_filters["emi_reject"].setText("EMI" if narrow else "EMI lines")
        self.configure_analysis.setText("Filters…" if narrow else "Filter settings…")
        self.quick_curves["background"].setText("− BG" if narrow else "Raw − BG")
        self.quick_curves["reference"].setText("− Ref" if narrow else "Raw − Ref")
        self.correction_controls.configure_background.setText("Setup…" if narrow else "Configure background…")
        self.correction_controls.configure_reference.setText("Setup…" if narrow else "Configure reference…")
        self.correction_controls.strip_layout.setHorizontalSpacing(6 if narrow else 10)
        self.presentation_controls.layout().setHorizontalSpacing(6 if narrow else 10)
        self.overlay_analysis_source.setText("Compare" if narrow else "Compare input")
        self.toggle_analysis_details.setText("View…" if narrow else "View options…")
        short = event.size().height() < 720
        if short != self._short_workspace:
            self._short_workspace = short
            self.layout().setSpacing(6 if short else 10)
            self.layout().setContentsMargins(12, 8, 12, 8)
            # The Live indicator always remains beside the acquisition buttons.
            self.hero_card.setVisible(not short)
            if short:
                self._hero_title_row.removeWidget(self.live_indicator)
                self._acquisition_command_layout.addWidget(self.live_indicator)
                self._hero_title_row.removeWidget(self.quick_controls_button)
                self._analysis_details_layout.addWidget(self.quick_controls_button)
            else:
                self._acquisition_command_layout.removeWidget(self.live_indicator)
                self._hero_title_row.addWidget(self.live_indicator)
                self._analysis_details_layout.removeWidget(self.quick_controls_button)
                self._hero_title_row.addWidget(self.quick_controls_button)
            self.live_indicator.show()
            self.quick_controls_button.show()
        wide = event.size().width() >= 1300 and self.analysis_tabs.currentIndex() == 0
        self._peak_marker_title.setVisible(wide)
        self.peak_count.setVisible(wide)
        popup = event.size().height() < 500
        if popup != self._presentation_in_popup:
            self._presentation_in_popup = popup
            settings_cards = (self.correction_controls, self.signal_analysis_card, self.presentation_controls)
            if popup:
                for card in settings_cards:
                    self._plot_controls_layout.removeWidget(card)
                    self._presentation_popup_layout.addWidget(card)
                for widget in (self.toggle_acquisition_controls, self.record_spectra):
                    self._acquisition_command_layout.removeWidget(widget)
                    self._compact_acquisition_layout.addWidget(widget)
                    widget.show()
            else:
                self._presentation_popup.hide()
                for index, card in enumerate(settings_cards):
                    self._presentation_popup_layout.removeWidget(card)
                    self._plot_controls_layout.insertWidget(index, card)
                for index, widget in enumerate((self.toggle_acquisition_controls, self.record_spectra), 3):
                    self._compact_acquisition_layout.removeWidget(widget)
                    self._acquisition_command_layout.insertWidget(index, widget)
                    widget.show()
            for card in settings_cards:
                card.show()
            self.compact_plot_settings.setVisible(popup)
        compact = event.size().width() < 1050
        self.control_scroll.setMinimumHeight(80 if compact else 0)
        self.spectrum_plot.setMinimumHeight(100)
        self.spectrogram_plot.setMinimumHeight(100)
        if self._workspace_compact != compact:
            self._workspace_compact = compact
            self.workspace_splitter.setOrientation(
                Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
            )
        # Nested splitters receive their final width after the page resize.
        self._control_height_timer.start(0)

    def _build_signal_generator_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(18, 14, 18, 14)
        heading = StrongBodyLabel("Optional vector signal generator")
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)
        explanation = BodyLabel(
            "This panel is shown only when the hardware catalogue reports option 020/120/021/121. "
            "Configuration explicitly enters SG mode and proves RF OUTPUT OFF. "
            "RF ON additionally requires a qualified protocol, configured limits and "
            "a successful hardware readback."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        outer.addWidget(explanation)

        card = CardWidget(tab)
        card.setObjectName("anritsuSignalGeneratorCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(20, 16, 20, 16)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        self.sg_status = BodyLabel("●  RF OUTPUT UNKNOWN")
        self.sg_status.setObjectName("anritsuSgIndicator")
        self.sg_status.setProperty("liveState", "off")
        self.sg_status.setProperty("outputState", "neutral")
        grid.addWidget(self.sg_status, 0, 0, 1, 4)
        generator = self._station_settings.anritsu.signal_generator
        default_frequency = generator.default_frequency
        default_power = generator.default_power
        self.sg_frequency = LineEdit(self)
        self.sg_frequency.setText(str(default_frequency))
        self.sg_power = LineEdit(self)
        self.sg_power.setText(str(default_power))
        grid.addWidget(BodyLabel("Frequency"), 1, 0)
        grid.addWidget(self.sg_frequency, 1, 1, 1, 3)
        grid.addWidget(BodyLabel("RF power"), 2, 0)
        grid.addWidget(self.sg_power, 2, 1, 1, 3)
        self.sg_read = PushButton("Read current SG state")
        self.sg_configure = PrimaryPushButton("Configure while RF OFF")
        self.sg_on = PushButton("RF OUTPUT ON")
        self.sg_on.setCheckable(True)
        self.sg_off = PushButton("RF OUTPUT OFF")
        self.sg_on.setObjectName("outputOnButton")
        self.sg_off.setObjectName("outputOffButton")
        for button in (
            self.sg_read,
            self.sg_configure,
            self.sg_on,
            self.sg_off,
        ):
            button.setProperty("compact", True)
        grid.addWidget(self.sg_read, 3, 0, 1, 2)
        grid.addWidget(self.sg_configure, 3, 2, 1, 2)
        grid.addWidget(self.sg_on, 4, 0, 1, 2)
        grid.addWidget(self.sg_off, 4, 2, 1, 2)
        self.sg_limits = BodyLabel()
        self.sg_limits.setWordWrap(True)
        self.sg_limits.setObjectName("muted")
        grid.addWidget(self.sg_limits, 5, 0, 1, 4)
        outer.addWidget(card)
        outer.addStretch(1)
        self.sg_read.clicked.connect(
            lambda: self._controller.call("read_signal_generator")
        )
        self.sg_configure.clicked.connect(self.configure_signal_generator)
        self.sg_on.clicked.connect(self.enable_signal_generator)
        self.sg_off.clicked.connect(
            lambda: self._controller.call("set_signal_generator_output", False)
        )
        self._update_signal_generator_limits()
        return tab

    def _build_advanced_spectrum_dialog(self) -> QDialog:
        dialog = StationDialog(self, resizable=True)
        dialog.setWindowTitle("Anritsu advanced Spectrum settings")
        dialog.setModal(False)
        dialog.resize(720, 780)
        dialog.setMinimumSize(540, 460)
        surface = dialog.use_modal_shell_content().surface
        outer = dialog.modal_content_layout(spacing=12)
        scroll = ScrollArea(surface)
        scroll.setObjectName("anritsuAdvancedSettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 12, 4)
        layout.setSpacing(16)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        explanation = BodyLabel(
            "These controls change bandwidth, detector and the RF input path. Readback is "
            "always available as an explicit diagnostic action. Apply remains locked until "
            "the exact firmware is qualified in the station profile."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.advanced_protocol_status = BodyLabel()
        self.advanced_protocol_status.setWordWrap(True)
        layout.addWidget(self.advanced_protocol_status)

        self.advanced_configuration_panel = AnritsuAdvancedSpectrumPanel(surface)
        self.advanced_configuration_panel.load_settings_defaults(
            self._station_settings
        )
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

        help_text = BodyLabel(
            "Documented limits: RBW 1 Hz–31.25 MHz; VBW 1 Hz–10 MHz or Off; "
            "attenuation 0–60 dB in 2 dB steps; frequency-domain sweep 1 ms–1000 s. "
            "Automatic attenuation is blocked when the safety profile defines a minimum."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        actions = QHBoxLayout()
        self.advanced_read_button = PushButton("Read from instrument", surface)
        self.advanced_apply_button = PrimaryPushButton("Apply and verify", surface)
        close_button = PushButton("Close", surface)
        actions.addWidget(self.advanced_read_button)
        actions.addWidget(self.advanced_apply_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        outer.addLayout(actions)
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
                "Confirm that the configured expected input and attenuation are correct.",
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
        panel = self.configuration_panel
        panel.rbw_mode.setCurrentIndex(panel.rbw_mode.findData("auto" if snapshot.rbw_auto else "manual"))
        panel.rbw.setText(format_quantity_auto(snapshot.rbw_hz, DIMENSION_FREQUENCY))
        if snapshot.vbw_mode == "off" and panel.vbw_auto.findData("off") < 0:
            panel.vbw_auto.addItem("Off", userData="off")
        panel.vbw_auto.setCurrentIndex(panel.vbw_auto.findData(snapshot.vbw_mode))
        if snapshot.vbw_hz is not None:
            panel.vbw.setText(format_quantity_auto(snapshot.vbw_hz, DIMENSION_FREQUENCY))
        if snapshot.vbw_filter_mode in {"VID", "POW"}:
            panel.vbw_mode.setCurrentIndex(panel.vbw_mode.findData(snapshot.vbw_filter_mode))

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
        field = LimitField(editor, *self._anritsu_limit_values(key), range_mode=True)
        self._limit_fields[key + str(len(self._limit_fields))] = field
        field.setProperty("limitKey", key)
        return field

    def set_settings(self, settings: StationSettings) -> None:
        self._station_settings = settings
        self.configuration_panel.set_settings(settings)
        saved = settings.anritsu.preview.analysis_parameters()
        preview_parameters = replace(self._analysis_parameters,
            temporal_average_frames=saved.temporal_average_frames, temporal_max_gap_s=saved.temporal_max_gap_s,
            narrow_protected_regions_hz=saved.narrow_protected_regions_hz,
            peak_measure_filtered=saved.peak_measure_filtered)
        if preview_parameters != self._analysis_parameters:
            self._analysis_parameters_applied(preview_parameters)
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
        self.mode_tabs.navigation.setVisible(self._sg_supported)
        if not self._sg_supported and self.mode_tabs.currentIndex() == self.signal_generator_tab_index:
            self.mode_tabs.setCurrentIndex(0)
        self._update_advanced_availability()
        self._apply_page_state()

    def set_manual_archive_context(
        self,
        *,
        metadata_provider: Callable[[], tuple[ManualMetadataValue, ...]] | None = None,
        device_idn_provider: Callable[[], dict[str, str]] | None = None,
        settings_source_provider: Callable[[], str] | None = None,
        operator_context_provider: Callable[[], dict[str, object]] | None = None,
        simulation: bool = False,
    ) -> None:
        """Bind station-wide, already-confirmed values to the manual saver."""

        self._manual_metadata_provider = metadata_provider
        self._manual_device_idn_provider = device_idn_provider
        self._manual_settings_source_provider = settings_source_provider
        self._manual_operator_context_provider = operator_context_provider
        self._manual_simulation = simulation

    def set_manual_elab_context(
        self,
        *,
        configuration_provider: Callable[[], tuple[bool, bool, str]] | None = None,
        upload_callback: Callable[[Path], None] | None = None,
    ) -> None:
        """Bind the optional per-save eLab action without importing eLab into storage."""

        self._manual_elab_config_provider = configuration_provider
        self._manual_elab_upload_callback = upload_callback

    def _manual_elab_context(self) -> tuple[bool, bool, str]:
        provider = self._manual_elab_config_provider
        if provider is None:
            return False, False, "Configure a template in the eLabFTW tab first."
        try:
            available, default_enabled, hint = provider()
            return bool(available), bool(default_enabled), str(hint)
        except Exception as exc:
            self.status.emit(f"eLab save option unavailable: {exc}")
            return False, False, "The eLab save option is currently unavailable."

    def manual_metadata_values(self) -> tuple[ManualMetadataValue, ...]:
        """Expose confirmed Anritsu settings for the manual metadata picker."""

        values: list[ManualMetadataValue] = []

        def add(
            key: str,
            label: str,
            dimension: str | None,
            unit: str,
            value: float | int | None,
            *,
            source: str = "Anritsu SCPI readback",
        ) -> None:
            if value is not None:
                values.append(
                    ManualMetadataValue(
                        key=key,
                        device="Anritsu Spectrum Analyzer",
                        label=label,
                        dimension=dimension,
                        unit=unit,
                        value_si=float(value),
                        source=source,
                    )
                )

        basic = self._last_configuration
        if basic is not None:
            add(
                "anritsu.spectrum.start_frequency_hz",
                "Anritsu · start frequency",
                DIMENSION_FREQUENCY,
                "Hz",
                basic.start_hz,
            )
            add(
                "anritsu.spectrum.stop_frequency_hz",
                "Anritsu · stop frequency",
                DIMENSION_FREQUENCY,
                "Hz",
                basic.stop_hz,
            )
            add(
                "anritsu.spectrum.reference_level_dbm",
                "Anritsu · reference level",
                DIMENSION_DBM,
                "dBm",
                basic.reference_level_dbm,
            )
            add(
                "anritsu.spectrum.points_count",
                "Anritsu · sweep points",
                None,
                "count",
                basic.points,
            )
        advanced = self._last_advanced_configuration
        if advanced is not None:
            add(
                "anritsu.spectrum.rbw_hz",
                "Anritsu · RBW",
                DIMENSION_FREQUENCY,
                "Hz",
                advanced.rbw_hz if not advanced.rbw_auto else None,
                source="Anritsu advanced SCPI readback",
            )
            add(
                "anritsu.spectrum.vbw_hz",
                "Anritsu · VBW",
                DIMENSION_FREQUENCY,
                "Hz",
                advanced.vbw_hz,
                source="Anritsu advanced SCPI readback",
            )
            add(
                "anritsu.spectrum.attenuation_db",
                "Anritsu · attenuation",
                DIMENSION_DB,
                "dB",
                advanced.attenuation_db if not advanced.attenuation_auto else None,
                source="Anritsu advanced SCPI readback",
            )
            add(
                "anritsu.spectrum.sweep_time_s",
                "Anritsu · sweep time",
                DIMENSION_TIME,
                "s",
                advanced.sweep_time_s if not advanced.sweep_time_auto else None,
                source="Anritsu advanced SCPI readback",
            )
        generator = self._last_signal_generator_snapshot
        if generator is not None:
            add(
                "anritsu.sg.frequency_hz",
                "Anritsu SG · frequency",
                DIMENSION_FREQUENCY,
                "Hz",
                generator.frequency_hz,
                source="Anritsu signal-generator readback",
            )
            add(
                "anritsu.sg.power_dbm",
                "Anritsu SG · power",
                DIMENSION_DBM,
                "dBm",
                generator.power_dbm,
                source="Anritsu signal-generator readback",
            )
        return tuple(values)

    def _device_state_changed(self, state: str) -> None:
        if state == "disconnected":
            self._sg_configured = False
            self._set_sg_output_state(None)
            self._last_advanced_configuration = None
            self._set_page_state(AnritsuPageState.DISCONNECTED)
        elif state in {"fault", "unknown"}:
            self._set_sg_output_state(None)
            self._set_page_state(AnritsuPageState.ERROR)
        elif state in {"verified", "output_off"}:
            # Qualified connect and explicit aggregate OUTPUT OFF both prove
            # the optional SG output is de-energised.
            self._set_sg_output_state(False)
            if self._page_state in {
                AnritsuPageState.DISCONNECTED,
                AnritsuPageState.ERROR,
            }:
                self._set_page_state(AnritsuPageState.IDLE)
        elif state == "output_on":
            self._set_sg_output_state(True)
            if self._page_state in {
                AnritsuPageState.DISCONNECTED,
                AnritsuPageState.ERROR,
            }:
                self._set_page_state(AnritsuPageState.IDLE)
        elif self._page_state in {
            AnritsuPageState.DISCONNECTED,
            AnritsuPageState.ERROR,
        }:
            self._set_page_state(AnritsuPageState.IDLE)
        self._apply_page_state()

    def _set_page_state(self, state: AnritsuPageState) -> None:
        self._page_state = state
        self._apply_page_state()

    def _apply_page_state(self) -> None:
        idle = self._page_state in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}
        if self.correction_workspace.running:
            idle = False
        live = self._page_state == AnritsuPageState.LIVE
        averaging = self._page_state in {
            AnritsuPageState.AVERAGING_SIGNAL,
            AnritsuPageState.AVERAGING_REFERENCE,
        }
        connected = self._page_state != AnritsuPageState.DISCONNECTED
        self.correction_workspace.set_available(idle and connected, device_idn=self._device_idn)
        self.read_configuration.setEnabled(idle)
        self.read_and_save_configuration.setEnabled(idle)
        self.configuration_panel.setEnabled(idle)
        self.configure_button.setEnabled(idle)
        self.single.setEnabled(
            idle and self._trace_supported and self._single_sweep_configured
        )
        self.live.setEnabled((idle or live) and connected and not self._live_transition_pending)
        self.correction_controls.configure_background.setEnabled(not self.correction_workspace.running
            and not self.correction_workspace._profile_io_busy and not self._live_transition_pending)
        self.abort_button.setEnabled(connected)
        self.advanced_spectrum_button.setEnabled(connected and idle)
        self.average_count.setEnabled(idle and not averaging)
        self.acquire_average.setEnabled(idle and self._trace_supported)
        self.acquire_single_reference.setEnabled(idle and self._trace_supported)
        self.use_current_reference.setEnabled((idle or live) and self._latest_trace is not None)
        self.capture_reference.setEnabled((idle or live) and self._trace_supported)
        self.reference_average_count.setEnabled(idle or live)
        self.reference_cancel_average.setEnabled(self._page_state == AnritsuPageState.AVERAGING_REFERENCE)
        self.reference_cancel_average.setVisible(self._page_state == AnritsuPageState.AVERAGING_REFERENCE)
        self.load_reference.setEnabled(idle or live)
        self.save_reference.setEnabled((idle or live) and self._reference_spectrum is not None)
        self.reference_operation.setEnabled(self._reference_trace is not None)
        self.cancel_average.setEnabled(averaging)
        self.clear_reference.setEnabled(
            self._reference_spectrum is not None
            and self._page_state not in {
                AnritsuPageState.STARTING_LIVE,
                AnritsuPageState.STOPPING,
                AnritsuPageState.ACQUIRING_REFERENCE,
            }
        )
        has_any_spectra = (
            self._latest_trace is not None
            or self._averaged_trace is not None
            or self._reference_spectrum is not None
            or self._reference_trace is not None
            or self._spectrogram_buffer.row_count > 0
            or self._cleanup_result is not None
            or bool(self._detected_peaks)
        )
        can_clear = has_any_spectra and self._page_state not in {
            AnritsuPageState.STARTING_LIVE,
            AnritsuPageState.STOPPING,
        }
        self.clear_all_spectra_button.setEnabled(can_clear)
        self.clear_spectra_plot_button.setEnabled(can_clear)
        protocol_qualified = (
            self._station_settings.anritsu.signal_generator.control_protocol
            == "basic_scpi"
        )
        self.sg_read.setEnabled(connected and idle and self._sg_supported)
        self.sg_configure.setEnabled(
            connected and idle and self._sg_supported and protocol_qualified
        )
        self.sg_on.setEnabled(
            connected
            and idle
            and self._sg_supported
            and protocol_qualified
            and self._sg_configured
            and self._sg_output_known
            and not self._sg_output_enabled
        )
        self.sg_off.setEnabled(
            connected
            and self._sg_supported
            and (self._sg_output_enabled or not self._sg_output_known)
        )
        self.sg_on.setChecked(self._sg_output_enabled and self._sg_output_known)
        self.sg_on.setProperty(
            "controlState",
            "energized"
            if self._sg_output_enabled and self._sg_output_known
            else "available",
        )
        self.sg_on.setToolTip(
            "RF OUTPUT is confirmed ON."
            if self._sg_output_enabled and self._sg_output_known
            else "Enable RF only after configuration and hardware readback."
        )
        self.sg_off.setToolTip(
            "Disable RF OUTPUT and confirm hardware readback."
            if self.sg_off.isEnabled()
            else "RF OUTPUT is already confirmed OFF."
        )
        self.sg_on.style().unpolish(self.sg_on)
        self.sg_on.style().polish(self.sg_on)
        self._update_advanced_availability()
        self._update_manual_save_controls()

        self._sync_floating_live_controls()

    def _sync_floating_live_controls(self):
        if self._spectrum_window is not None:
            live = self._page_state == AnritsuPageState.LIVE
            enabled = self.live.isEnabled() and not self._execution_controlled and not self._live_transition_pending
            self._spectrum_window.start_live.setEnabled(enabled and not live)
            self._spectrum_window.stop_live.setEnabled(enabled and live)

    def _update_manual_save_controls(self) -> None:
        if self._manual_archive_thread is not None:
            self.configure_manual_spectrum.setEnabled(False)
            self.save_manual_spectrum.setEnabled(False)
            self.close_manual_archive.setEnabled(False)
            return
        configured = self._manual_save_options is not None
        ready = self._latest_trace is not None and configured and not self._latest_trace_is_execution_preview
        self.configure_manual_spectrum.setEnabled(True)
        self.save_manual_spectrum.setEnabled(ready)
        self.close_manual_archive.setEnabled(
            self._manual_archive is not None
            and self._manual_archive.active_path is not None
        )
        if not configured:
            self.manual_save_status.setText(
                "Configure the archive before saving a completed spectrum."
            )
        elif self._latest_trace is None:
            self.manual_save_status.setText(
                "Archive configured. Acquire a completed spectrum before saving."
            )
        elif self._latest_trace_is_execution_preview:
            self.manual_save_status.setText("Execution preview only. Full spectra are stored in the sweep archive.")
        elif self._manual_archive is None:
            self.manual_save_status.setText(
                "Completed spectrum ready — press Save current spectrum."
            )

    def _set_sg_output_state(self, enabled: bool | None) -> None:
        self._sg_output_known = enabled is not None
        self._sg_output_enabled = bool(enabled) if enabled is not None else False
        if enabled is True:
            text = "●  RF OUTPUT ON"
            state = "active"
        elif enabled is False:
            text = "●  RF OUTPUT OFF"
            state = "neutral"
        else:
            text = "●  RF OUTPUT UNKNOWN — use RF OFF or E-STOP"
            state = "neutral"
        self.sg_status.setText(text)
        self.sg_status.setProperty("outputState", state)
        self.sg_status.setProperty(
            "liveState", "on" if enabled is True else "off"
        )
        self.sg_status.style().unpolish(self.sg_status)
        self.sg_status.style().polish(self.sg_status)

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
            f"Protocol: {protocol} | Configured frequency: {frequency} | Configured RF power: "
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
        self._sg_configured = False
        self._apply_page_state()
        self._controller.call("configure_signal_generator", config)

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
        self._set_sg_output_state(result.output_enabled)
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

    def apply_execution_event(
        self,
        event_name: str,
        event: Mapping[str, object],
        device_state: Mapping[str, object],
        output_status: Mapping[str, str],
    ) -> None:
        """Project confirmed analyser/SG state and committed spectrum previews."""
        spectrum = self._execution_actual(device_state, "spectrum")
        if spectrum:
            start = self._execution_number(spectrum.get("start_hz"))
            stop = self._execution_number(spectrum.get("stop_hz"))
            reference = self._execution_number(spectrum.get("reference_level_dbm"))
            points = spectrum.get("points")
            if (
                start is not None
                and stop is not None
                and reference is not None
                and isinstance(points, int)
            ):
                snapshot = AnritsuConfigurationSnapshot(
                    start_hz=start,
                    stop_hz=stop,
                    reference_level_dbm=reference,
                    points=points,
                    instrument_mode=str(spectrum.get("instrument_mode", "RUN ENGINE")),
                )
                self._last_configuration = snapshot
                self.configuration_panel.load_snapshot(snapshot)

        advanced = self._execution_actual(device_state, "advanced_spectrum")
        advanced_snapshot = self._execution_advanced_snapshot(advanced)
        if advanced_snapshot is not None:
            self._show_advanced_snapshot(advanced_snapshot)

        generator = self._execution_actual(device_state, "signal_generator")
        frequency = self._execution_number(generator.get("frequency_hz"))
        power = self._execution_number(generator.get("power_dbm"))
        sg_state = output_status.get("anritsu.sg")
        output_enabled = sg_state == "on" if sg_state in {"on", "off"} else None
        if frequency is not None and power is not None:
            self.sg_frequency.setText(
                format_quantity_auto(frequency, DIMENSION_FREQUENCY)
            )
            self.sg_power.setText(f"{power:.9g} dBm")
        self._set_sg_output_state(output_enabled)

        kind = str(event.get("kind", ""))
        if event_name == "action_started" and kind in {
            "acquire_spectrum",
            "acquire_reference",
        }:
            label = "REFERENCE" if kind == "acquire_reference" else "SPECTRUM"
            self.live_indicator.setText(f"●  SWEEP ACQUIRING {label}")
            self.live_indicator.setProperty("liveState", "starting")
            self.info.setText(
                "Run Engine started a synchronized single sweep and is waiting "
                "for instrument completion readback."
            )
            self._repolish_execution_indicator()
        elif event_name in {"spectrum_preview", "reference_preview"}:
            self._show_execution_trace(event)

    @staticmethod
    def _execution_actual(
        device_state: Mapping[str, object], section: str
    ) -> Mapping[str, object]:
        record = device_state.get(section)
        actual = record.get("actual") if isinstance(record, Mapping) else None
        return actual if isinstance(actual, Mapping) else {}

    @staticmethod
    def _execution_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    @classmethod
    def _execution_advanced_snapshot(
        cls, actual: Mapping[str, object]
    ) -> AdvancedSpectrumSnapshot | None:
        rbw = cls._execution_number(actual.get("rbw_hz"))
        attenuation = cls._execution_number(actual.get("attenuation_db"))
        sweep_time = cls._execution_number(actual.get("sweep_time_s"))
        if rbw is None or attenuation is None or sweep_time is None:
            return None
        vbw = cls._execution_number(actual.get("vbw_hz"))
        return AdvancedSpectrumSnapshot(
            rbw_auto=bool(actual.get("rbw_auto", False)),
            rbw_hz=rbw,
            vbw_mode=str(actual.get("vbw_mode", "auto")),
            vbw_hz=vbw,
            detector=str(actual.get("detector", "NORM")),
            attenuation_auto=bool(actual.get("attenuation_auto", False)),
            attenuation_db=attenuation,
            preamplifier_enabled=bool(
                actual.get("preamplifier_enabled", False)
            ),
            sweep_time_auto=bool(actual.get("sweep_time_auto", False)),
            sweep_time_s=sweep_time,
            instrument_mode=str(actual.get("instrument_mode", "RUN ENGINE")),
            vbw_filter_mode=actual.get("vbw_filter_mode"),
        )

    def _show_execution_trace(self, event: Mapping[str, object]) -> None:
        frequencies = event.get("frequency_hz")
        powers = event.get("power_dbm")
        if not isinstance(frequencies, (tuple, list)) or not isinstance(
            powers, (tuple, list)
        ):
            return
        frequency_values = tuple(float(value) for value in frequencies)
        power_values = tuple(float(value) for value in powers)
        if (
            len(frequency_values) != len(power_values)
            or len(frequency_values) < 2
            or not all(math.isfinite(value) for value in (*frequency_values, *power_values))
        ):
            return
        timestamp_text = str(event.get("timestamp_utc", ""))
        try:
            acquired_at = datetime.fromisoformat(timestamp_text)
        except ValueError:
            acquired_at = datetime.now(timezone.utc)
        trace = SpectrumTrace(
            frequencies_hz=frequency_values,
            powers_dbm=power_values,
            acquired_at_utc=acquired_at,
            trace_name=str(event.get("trace_name", "TRAC1")),
        )
        self._show_trace(trace, update_controls=False, execution_preview=True)
        source_points = int(event.get("source_points", len(power_values)))
        kind = str(event.get("preview_kind", "measurement"))
        label = "REFERENCE STORED" if kind == "reference" else "SPECTRUM STORED"
        self.live_indicator.setText(f"●  {label}")
        self.live_indicator.setProperty("liveState", "on")
        self.info.setText(
            f"Run Engine confirmed {label.lower()} • displaying "
            f"{len(power_values)} of {source_points} points • {acquired_at.isoformat()}"
        )
        self._repolish_execution_indicator()

    def _repolish_execution_indicator(self) -> None:
        self.live_indicator.style().unpolish(self.live_indicator)
        self.live_indicator.style().polish(self.live_indicator)

    def set_execution_controlled(self, controlled: bool) -> None:
        self._execution_controlled = controlled
        if controlled:
            # Stop application polling, without issuing a sweep/abort command.
            # The Run Engine owns acquisition; its telemetry supplies the view.
            self._timer.stop()
            self._background_config_timer.stop()
            if self._averaging_active:
                self._finish_temporal_averaging(resume_live=False)
            self.live.setText("Start Live")
        self.execution_badge.setVisible(controlled)
        self._sync_floating_live_controls()
        if not controlled and not self._timer.isActive():
            self._set_live_indicator("off")

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
                f"Detected in hardware catalogue: {option_text} | Preamplifier: {preamplifier}."
            )
        else:
            self.hardware_option_info.setText(
                "Hardware options: waiting for connection, or the hardware catalogue is unavailable."
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
            "Configured safety badges above may intentionally be stricter."
        )
        self._hardware_details_text = (
            "Detected hardware options\n"
            f"{self.hardware_option_info.text()}\n\n"
            "Documented instrument limits\n"
            f"{self.hardware_range_info.text()}"
        )
        if self._trace_diagnostics_dialog is not None:
            self._trace_diagnostics_dialog.set_hardware_details(
                self._hardware_details_text
            )

    def _show_anritsu_hardware_info(self) -> None:
        dialog = self._trace_diagnostics_dialog
        if dialog is None:
            dialog = _AnritsuTraceDiagnosticsDialog(self)
            dialog.closed.connect(self._trace_diagnostics_closed)
            self._trace_diagnostics_dialog = dialog
        dialog.set_hardware_details(self._hardware_details_text)
        if self._latest_trace is not None:
            dialog.set_trace(
                self._latest_trace,
                received_frame=self._received_trace_count,
            )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _trace_diagnostics_closed(self) -> None:
        self._trace_diagnostics_dialog = None

    def _spectrum_config_from_form(self) -> SpectrumConfig:
        return self.configuration_panel.spectrum_config("TRAC1")

    def _configure_from_form(self, *, then: str | None = None) -> bool:
        try:
            config = self._spectrum_config_from_form()
        except Exception as exc:
            self.banner.show_message(f"Invalid spectrum settings: {exc}")
            return False
        self._pending_after_spectrum_configuration = then
        self._controller.call("configure", config)
        return True

    def configure(self) -> None:
        self._set_page_state(AnritsuPageState.CONFIGURING)
        if not self._configure_from_form():
            self._set_page_state(AnritsuPageState.ERROR)

    def read_configuration_from_instrument(self) -> None:
        self.status.emit("Anritsu current-configuration read requested")
        self._controller.call("read_configuration")

    def read_and_save_configuration_from_instrument(self) -> None:
        self._save_readback_pending = True
        self.status.emit("Anritsu read-only settings import requested")
        self._controller.call("read_configuration")

    def _show_full_readback_dialog(
        self, readback: AnritsuFullConfigurationReadback
    ) -> None:
        form_values = self._current_form_comparison_values()
        dialog = AnritsuReadbackDialog(readback, form_values, self)
        def apply_and_compare(callback, *args):
            try:
                callback(*args)
                actual = self._current_form_comparison_values()
            except (ValueError, TypeError) as exc:
                self.status.emit(f"Hardware values could not be copied to the form: {exc}")
                actual = {}
            dialog.refresh_form_values(actual)

        dialog.assign_requested.connect(
            lambda key, value: apply_and_compare(self._apply_readback_parameter, key, value)
        )
        dialog.assign_all_requested.connect(
            lambda value: apply_and_compare(self._apply_all_readback_parameters, value)
        )
        self._readback_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _current_form_comparison_values(self) -> dict[str, object]:
        start_hz, stop_hz = self._spectrum_frequency_bounds()
        ref_dbm = parse_quantity(self.reference.text(), DIMENSION_DBM).si_value
        points = int(self.points.currentData() or 1001)
        rbw_auto = self.rbw_mode.currentData() == "auto"
        rbw_hz = (
            parse_quantity(self.rbw.text(), DIMENSION_FREQUENCY).si_value
            if not rbw_auto
            else None
        )
        vbw_auto = self.vbw_auto.currentData() == "auto"
        vbw_mode = str(self.vbw_mode.currentData() or "VID")
        vbw_hz = (
            parse_quantity(self.vbw.text(), DIMENSION_FREQUENCY).si_value
            if self.vbw_auto.currentData() == "manual"
            else None
        )
        avg_count = self.average_count.value()
        return {
            "start_hz": start_hz,
            "stop_hz": stop_hz,
            "center_hz": (start_hz + stop_hz) / 2.0,
            "span_hz": stop_hz - start_hz,
            "reference_level_dbm": ref_dbm,
            "points": points,
            "rbw_auto": rbw_auto,
            "rbw_hz": rbw_hz,
            "vbw_auto": vbw_auto,
            "vbw_mode": vbw_mode,
            "vbw_hz": vbw_hz,
            "detector": (
                normalize_anritsu_detector(str(self.advanced_detector.currentData()))
                if hasattr(self, "advanced_detector") and self.advanced_detector.currentData()
                else normalize_anritsu_detector(
                    str(self.configuration_panel._settings.anritsu.safety.defaults.get("detector", "NORM"))
                )
            ),
            "average_count": avg_count,
        }

    def _apply_readback_parameter(self, parameter: str, value: object) -> None:
        if parameter in {"start_hz", "stop_hz", "center_hz", "span_hz"}:
            start, stop = self._spectrum_frequency_bounds()
            requested = float(value)
            if parameter == "start_hz":
                start = requested
            elif parameter == "stop_hz":
                stop = requested
            elif parameter == "center_hz":
                half_span = (stop - start) / 2
                start, stop = requested - half_span, requested + half_span
            else:
                center = (start + stop) / 2
                start, stop = center - requested / 2, center + requested / 2
            if not (math.isfinite(start) and math.isfinite(stop) and 0 <= start < stop):
                raise ValueError("Copied frequency must leave finite, non-negative, increasing bounds.")
            self._set_frequency_bounds(start, stop)
        elif parameter == "reference_level_dbm":
            self.reference.setText(f"{float(value):.9g} dBm")
        elif parameter == "points":
            index = self.points.findData(int(value))
            if index >= 0:
                self.points.setCurrentIndex(index)
        elif parameter == "rbw_auto":
            idx = self.rbw_mode.findData("auto" if value else "manual")
            if idx >= 0:
                self.rbw_mode.setCurrentIndex(idx)
        elif parameter == "rbw_hz" and value is not None:
            self.rbw.setText(format_quantity_auto(float(value), DIMENSION_FREQUENCY))
        elif parameter == "vbw_auto":
            idx = self.vbw_auto.findData("auto" if value else "manual")
            if idx >= 0:
                self.vbw_auto.setCurrentIndex(idx)
        elif parameter == "vbw_mode":
            idx = self.vbw_mode.findData(str(value).upper())
            if idx >= 0:
                self.vbw_mode.setCurrentIndex(idx)
        elif parameter == "vbw_hz":
            if value is None:
                if self.vbw_auto.findData("off") < 0:
                    self.vbw_auto.addItem("Off", userData="off")
                self.vbw_auto.setCurrentIndex(self.vbw_auto.findData("off"))
            else:
                self.vbw.setText(format_quantity_auto(float(value), DIMENSION_FREQUENCY))
        elif parameter == "detector":
            if hasattr(self, "advanced_detector"):
                idx = self.advanced_detector.findData(normalize_anritsu_detector(str(value)))
                if idx >= 0:
                    self.advanced_detector.setCurrentIndex(idx)
        elif parameter == "average_count":
            self.average_count.setValue(int(value))

    def _apply_all_readback_parameters(
        self, readback: AnritsuFullConfigurationReadback
    ) -> None:
        self.configuration_panel.load_snapshot(readback)
        if hasattr(self, "advanced_detector"):
            idx = self.advanced_detector.findData(normalize_anritsu_detector(readback.detector))
            if idx >= 0:
                self.advanced_detector.setCurrentIndex(idx)
        if readback.average_count > 0:
            self.average_count.setValue(readback.average_count)
        self.banner.show_message(
            "All compatible hardware parameters copied to the form.",
            severity="success",
        )

    def _confirm_full_settings_readback(
        self, readback: AnritsuFullConfigurationReadback
    ) -> None:
        basic = AnritsuConfigurationSnapshot(
            start_hz=readback.start_hz,
            stop_hz=readback.stop_hz,
            reference_level_dbm=readback.reference_level_dbm,
            points=readback.points,
            instrument_mode=readback.instrument_mode,
        )
        advanced = AdvancedSpectrumSnapshot(
            rbw_auto=readback.rbw_auto,
            rbw_hz=readback.rbw_hz,
            vbw_mode="auto" if readback.vbw_auto else "off" if readback.vbw_hz is None else "manual",
            vbw_filter_mode=readback.vbw_mode,
            vbw_hz=readback.vbw_hz,
            detector=readback.detector,
            attenuation_auto=readback.attenuation_auto,
            attenuation_db=readback.attenuation_db,
            preamplifier_enabled=readback.preamplifier_enabled,
            sweep_time_auto=readback.sweep_time_auto,
            sweep_time_s=readback.sweep_time_s,
            instrument_mode=readback.instrument_mode,
        )
        self._last_configuration = basic
        self._last_advanced_configuration = advanced
        self._confirm_settings_readback()

    def _confirm_settings_readback(self) -> None:
        basic = self._last_configuration
        if basic is None:
            return
        advanced = self._last_advanced_configuration
        lines = [
            f"Start: {format_quantity_auto(basic.start_hz, DIMENSION_FREQUENCY)}",
            f"Stop: {format_quantity_auto(basic.stop_hz, DIMENSION_FREQUENCY)}",
            f"Reference level: {basic.reference_level_dbm:.9g} dBm",
            f"Sweep points: {basic.points}",
        ]
        if advanced is not None:
            lines.extend(
                (
                    f"RBW: {'AUTO' if advanced.rbw_auto else format_quantity_auto(advanced.rbw_hz, DIMENSION_FREQUENCY)}",
                    f"VBW: {advanced.vbw_mode.upper() if advanced.vbw_hz is None else format_quantity_auto(advanced.vbw_hz, DIMENSION_FREQUENCY)}",
                    f"Detector: {advanced.detector}",
                    f"Attenuation: {'AUTO' if advanced.attenuation_auto else f'{advanced.attenuation_db:.9g} dB'}",
                    f"Preamplifier: {'ON' if advanced.preamplifier_enabled else 'OFF'}",
                    f"Sweep time: {'AUTO' if advanced.sweep_time_auto else format_quantity_auto(advanced.sweep_time_s, DIMENSION_TIME)}",
                )
            )
        answer = QMessageBox.question(
            self,
            "Save Anritsu readback",
            "The following values were read using SCPI queries only:\n\n"
            + "\n".join(lines)
            + "\n\nSave them as acquisition defaults in settings.yml? "
            "Safety limits and the instrument will not be changed.",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            self.settings_readback_requested.emit(basic, advanced)
        else:
            self.status.emit("Anritsu settings import cancelled; settings.yml unchanged")

    def read_once(self) -> None:
        if self._execution_controlled or self._fetch_pending or self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
            return
        self._set_page_state(AnritsuPageState.ACQUIRING_SPECTRUM)
        self.info.setText("Acquiring fresh spectrum…")
        self.status.emit("Anritsu fresh-spectrum acquisition started")
        self._fetch_pending = True
        self._fetch_started_monotonic = time.monotonic()
        self._manual_trace_deadline_monotonic = time.monotonic() + 5.0
        self._controller.call("single_sweep", "TRAC1")

    def _retry_manual_current_trace(self) -> None:
        if self._manual_trace_deadline_monotonic is None or self._timer.isActive():
            return
        self._request_trace()

    def _request_trace(self, *, fast: bool = True) -> bool:
        if self._execution_controlled:
            return False
        if self._fetch_pending:
            self._coalesced_timer_ticks += 1
            return False
        self._fetch_pending = True
        self._fetch_started_monotonic = time.monotonic()
        operation = "fetch_current_trace_fast" if fast else "fetch_current_trace"
        self._controller.call(operation, "TRAC1")
        return True

    def toggle_live(self) -> None:
        if self._execution_controlled:
            return
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
        self._manual_trace_deadline_monotonic = None
        self.live.setText("Starting…")
        self._spectrogram_buffer.reset_processing()
        self._preview_statistics = None
        self._set_live_indicator("starting")
        self._set_page_state(AnritsuPageState.STARTING_LIVE)
        self._timer.setInterval(self.refresh.value())
        # Live does not start one application-owned sweep per frame.  It only
        # ensures that the analyser itself is free-running, then periodically
        # reads the displayed TRAC1 buffer.
        self._controller.call("start_live", True)

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

    def _on_refresh_interval_changed(self, value: int) -> None:
        if self._timer.isActive():
            self._timer.setInterval(value)

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
        target = self.reference_average_count.value() if destination == "reference" else self.average_count.value()
        self._resume_live_after_averaging = self._timer.isActive()
        if self._resume_live_after_averaging:
            self._timer.stop()
            self._set_live_indicator("paused")
        self._averager.reset()
        self._averaging_active = True
        self._averaging_source_trace = None
        self._averaging_destination = destination
        self._averaging_start_monotonic = time.monotonic()
        self._averaging_target_count = target
        self._set_page_state(
            AnritsuPageState.AVERAGING_REFERENCE
            if destination == "reference"
            else AnritsuPageState.AVERAGING_SIGNAL
        )
        self.average_progress.setRange(0, target)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {target}")
        label = "reference" if destination == "reference" else "spectrum"
        self.average_stats_label.setText(f"Averaging {label}: 0 / {target} spectra…")
        self.info.setText(f"Averaging {label}: 0 / {target} temporal frames...")
        self.status.emit(
            f"Anritsu temporal averaging started: {label}, 0 / {target}"
        )
        self._request_next_average_frame()

    def cancel_averaging(self) -> None:
        self._finish_temporal_averaging(resume_live=True)
        self.info.setText("Averaging cancelled; completed spectra were not modified.")
        self.status.emit("Anritsu temporal averaging cancelled")

    def _finish_temporal_averaging(self, *, resume_live: bool) -> None:
        if self._averaging_active and self._fetch_pending:
            # The worker may still deliver this request after Cancel/Start.
            # Keep the fetch slot occupied until its terminal response arrives.
            self._discard_cancelled_average_frame = True
        was_live = self._resume_live_after_averaging
        should_resume_live = was_live and resume_live
        if self._averaging_active and self._averaging_start_monotonic is not None:
            completed = self.average_progress.value()
            target = self._averaging_target_count
            if completed < target:
                elapsed = max(1e-3, time.monotonic() - self._averaging_start_monotonic)
                self.average_stats_label.setText(
                    f"Averaging cancelled at {completed} / {target} spectra ({elapsed:.1f} s)"
                )
        self._averaging_active = False
        self._averaging_destination = None
        self._averaging_start_monotonic = None
        self._resume_live_after_averaging = False
        self._averager.reset()
        self._averaging_source_trace = None
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
        if self._execution_controlled or not self._averaging_active or self._fetch_pending:
            return
        if not self._single_sweep_configured:
            self._finish_temporal_averaging(resume_live=False)
            QMessageBox.warning(
                self,
                "Temporal averaging",
                "Temporal averaging requires the qualified single-sweep protocol.",
            )
            return
        self._fetch_pending = True
        self._fetch_started_monotonic = time.monotonic()
        self._controller.call("single_sweep", "TRAC1")

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
        """Acquire one new, completed sweep before storing a reference."""

        if self._fetch_pending or self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
            return
        if not self._confirm_reference_replacement("single"):
            return
        if not self._single_sweep_configured:
            QMessageBox.warning(
                self,
                "Reference spectrum",
                "A fresh reference requires the qualified single-sweep protocol. "
                "Use Start Live and acquire a new frame, or use the current trace explicitly.",
            )
            return
        self._pending_reference_kind = "single"
        self._set_page_state(AnritsuPageState.ACQUIRING_REFERENCE)
        self.info.setText("Acquiring one fresh reference frame…")
        self.status.emit("Anritsu single-reference acquisition started")
        self._fetch_pending = True
        self._fetch_started_monotonic = time.monotonic()
        self._controller.call("single_sweep", "TRAC1")

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
            vbw_filter_mode=advanced.vbw_filter_mode if advanced is not None else None,
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

    def advanced_settings_snapshot(self) -> AdvancedSpectrumSnapshot:
        return replace(
            self.advanced_configuration_panel.settings_snapshot(),
            vbw_filter_mode=str(self.vbw_mode.currentData()),
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
        if reference.vbw_filter_mode != current.vbw_filter_mode:
            mismatches.append("VBW filter VID/POW")
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
        self._display_revision += 1
        self._invalidate_analysis_results()
        self.show_reference.setChecked(True)
        self._update_reference_status()
        if self._pending_correction == "reference":
            self._pending_correction = None
            self.correction_controls.reference.setChecked(True)
        self._update_correction_summary()
        self._apply_page_state()
        self._refresh_spectrum_display()
        self._refresh_spectrogram_display()

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
        self._display_revision += 1
        self._invalidate_analysis_results()
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
        self._refresh_spectrogram_display()
        self.status.emit("Anritsu reference spectrum removed")

    def clear_all_spectra(self, *, confirm: bool = True) -> None:
        """Clear all in-memory spectra, averages, reference, and display buffers."""

        if confirm:
            answer = QMessageBox.question(
                self,
                "Clear all spectra",
                "Clear all in-memory traces, averaged spectrum, reference spectrum, and display plots?\n\n"
                "Saved HDF5 files on disk will not be affected.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        if self._averaging_active:
            self._finish_temporal_averaging(resume_live=False)

        if self._timer.isActive():
            self._timer.stop()
            self._live_transition_pending = True
            self.live.setText("Stopping…")
            self._set_live_indicator("stopping")
            self._set_page_state(AnritsuPageState.STOPPING)
            self._controller.call("stop_live")

        self._latest_trace = None
        self._averaged_trace = None
        self._reference_trace = None
        self._reference_spectrum = None
        self._pending_reference_kind = None
        self._spectrogram_buffer.clear()
        self._received_trace_count = 0
        self._live_frame_count = 0
        self._identical_live_frames = 0
        self._last_live_signature = None
        self._last_frame_monotonic = None
        self._frame_intervals_s.clear()
        self._transfer_durations_s.clear()
        self._stale_frame_count = 0
        self._coalesced_timer_ticks = 0
        self._candidate_traces = {}
        self._last_displayed_trace_names.clear()

        self._analysis_generation += 1
        self._invalidated_before_generation = self._analysis_generation
        self._applied_analysis_generation = self._analysis_generation
        self._cleanup_result = None
        self._analysis_error = None
        self._analysis_wait_reason = None
        self._detected_peaks = ()
        self._analysis_source_key = None
        self._analysis_source_selection = "auto"
        self._tracked_peak_target_hz = None
        self._tracked_peak_gate_hz = None
        self._tracking_started_monotonic = None

        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Current spectrum")
        self.spectrogram_plot.clear()

        if self._spectrum_window is not None:
            self._spectrum_window.spectrum.clear()
            self._spectrum_window.spectrum.set_title(
                "Waiting for a completed spectrum"
            )
            self._spectrum_window.status.setText(
                "Acquire a spectrum to update this read-only display."
            )
        if self._spectrogram_window is not None:
            self._spectrogram_window.spectrogram.clear()
            self._spectrogram_window.status.setText(
                "Start Live to accumulate a rolling spectrogram."
            )
        if self._trace_diagnostics_dialog is not None:
            self._trace_diagnostics_dialog.raw_text.setPlainText(
                "No completed TRAC1 frame has been received in this application session."
            )
            self._trace_diagnostics_dialog.raw_status.setText(
                "Waiting for the first completed current spectrum."
            )
        if self._peak_table_dialog is not None:
            self._peak_table_dialog.set_peaks((), method="none")
        if self._peak_tracking_window is not None:
            self._peak_tracking_window.clear()
        for window in tuple(self._additional_peak_trackers):
            window.close()

        self._switching_trace_checkboxes = True
        try:
            self.show_raw.setChecked(True)
            self.show_average.setChecked(False)
            self.show_reference.setChecked(False)
            self.show_processed.setChecked(False)
        finally:
            self._switching_trace_checkboxes = False

        self.reference_operation.setCurrentIndex(0)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {self.average_count.value()}")

        self._display_revision += 1
        self._display_state = build_display_state(
            raw=None,
            averaged=None,
            reference=None,
            reference_operation="none",
            visible={
                "raw": True,
                "averaged": False,
                "reference": False,
                "processed": False,
            },
            frame_id=self._display_revision,
        )

        self._update_reference_status()
        self.info.setText("Spectra cleared. Waiting for acquisition.")
        self._set_analysis_status("Waiting for a completed spectrum.")
        self.spectrogram_status.setText(
            "Start Live to accumulate a rolling spectrogram."
        )
        self.manual_save_status.setText("No completed spectrum ready to save.")

        self._apply_page_state()
        self._refresh_spectrum_display()
        self._refresh_spectrogram_display()

        self.status.emit("Anritsu in-memory spectra, reference, and buffers cleared")
        self.banner.show_message(
            "Cleared all captured spectra, reference spectrum, and display buffers.",
            severity="info",
        )

    def save_reference_file(self) -> None:
        reference = self._reference_spectrum
        if reference is None:
            self.banner.show_message("There is no reference spectrum to save.")
            return
        directory = str(getattr(self, "_manual_sample_directory", None) / "baselines"
                        if getattr(self, "_manual_sample_directory", None) is not None
                        else self._station_settings.storage.get("output_directory", "./measurements"))
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
        directory = str(getattr(self, "_manual_sample_directory", None) / "baselines"
                        if getattr(self, "_manual_sample_directory", None) is not None
                        else self._station_settings.storage.get("output_directory", "./measurements"))
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

    def _manual_trace_choices(self) -> tuple[tuple[str, str], ...]:
        choices: list[tuple[str, str]] = []
        for trace in self._display_state.traces:
            if trace.key.startswith("analysis:"):
                label = f"{trace.label} [{trace.unit}]"
            else:
                label = f"{trace.label} [{trace.unit}]"
            choices.append((trace.key, label))
        if not choices:
            choices.append(("raw", "Latest raw trace [dBm]"))
        return tuple(choices)

    def _manual_trace_payload(
        self, variant: str
    ) -> tuple[SpectrumTrace, tuple[float, ...] | None, str | None, str]:
        if self._latest_trace_is_execution_preview:
            raise ValueError("Execution previews cannot be saved as full RAW. Use the sweep archive or acquire a full manual spectrum.")
        source = self._display_state.by_key.get(variant)
        if source is None and variant != "raw":
            raise ValueError(f"Selected spectrum variant {variant!r} is unavailable; select an available trace.")
        if source is not None and self._latest_trace is not None:
            canonical_raw = self._latest_trace
            snapshot = self._analysis_source_snapshot
            if (snapshot is not None and self._analysis_raw_snapshot is not None
                    and source.key not in {"background_difference", "reference_difference"}
                    and source.frame_id == snapshot.frame_id):
                canonical_raw = self._analysis_raw_snapshot
            if source.key in {"raw", "averaged", "reference"} and source.unit == "dBm":
                source_trace = {
                    "raw": canonical_raw,
                    "averaged": self._averaged_trace,
                    "reference": self._reference_trace,
                }.get(source.key)
                if source_trace is not None:
                    return source_trace, None, None, "none"
            # HDF5 keeps the acquired dBm trace as the canonical source and
            # stores any relative/cleaned values as an explicitly unit-tagged
            # derived dataset.  This makes the archive match the exact display
            # snapshot without relabelling dB/ratio values as dBm.
            return (
                canonical_raw,
                source.values,
                source.unit,
                "display_" + "_".join(source.provenance),
            )
        if self._latest_trace is not None:
            return self._latest_trace, None, None, "none"
        raise ValueError("Acquire a completed spectrum before saving it.")

    def _manual_metadata_for_dialog(self) -> tuple[ManualMetadataValue, ...]:
        provider = self._manual_metadata_provider
        if provider is None:
            return self.manual_metadata_values()
        try:
            values = tuple(provider())
        except Exception as exc:
            self.status.emit(f"Manual metadata provider unavailable: {exc}")
            return self.manual_metadata_values()
        unique: dict[str, ManualMetadataValue] = {}
        for value in values:
            unique.setdefault(value.key, value)
        return tuple(unique.values())

    def _manual_default_destination(self) -> Path:
        if self._manual_save_options is not None:
            return self._manual_save_options.destination
        if self._manual_last_mode is ManualSpectrumSaveMode.APPEND and self._manual_archive_last_path:
            return self._manual_archive_last_path
        directory = getattr(self, "_manual_sample_directory", None) or Path(
            str(self._station_settings.storage.get("output_directory", "./measurements"))
        ).expanduser()
        return directory / (f"manual_{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.h5"
                            if getattr(self, "_manual_sample_directory", None) is not None else "manual_spectrum.h5")

    def set_sample_measurement_context(self, directory: Path | None, target: dict) -> None:
        """Detach an old append policy when the physical DUT changes."""
        if self._manual_archive_thread is not None:
            self._pending_manual_sample_context = (directory, deepcopy(target))
            return
        previous = getattr(self, "_manual_sample_target", {})
        if (directory == getattr(self, "_manual_sample_directory", None)
                and all(target.get(key) == previous.get(key) for key in ("sample_id", "row", "col"))):
            self._manual_sample_target = deepcopy(target)
            return
        self.close_manual_archive_session()
        self._manual_sample_directory = directory
        self._manual_sample_target = deepcopy(target)
        self.correction_workspace.set_measurement_directory(directory / "baselines" if directory is not None else None)
        self._manual_save_options = None
        self._manual_archive_last_path = None
        self._manual_last_mode = None
        self.manual_save_status.setText("DUT changed. Configure a new archive for this device before saving.")

    def _show_manual_save_dialog(self) -> None:
        choices = self._manual_trace_choices()
        previous_options = self._manual_save_options
        elab_available, default_upload_to_elab, elab_hint = self._manual_elab_context()
        default_mode = (
            previous_options.mode
            if previous_options is not None
            else self._manual_last_mode or ManualSpectrumSaveMode.APPEND
        )
        dialog = ManualSpectrumSaveDialog(
            self,
            trace_choices=choices,
            metadata_values=self._manual_metadata_for_dialog(),
            default_destination=self._manual_default_destination(),
            default_mode=default_mode,
            default_trace_variant=(
                previous_options.trace_variant if previous_options is not None else "raw"
            ),
            default_metadata_scope=(
                previous_options.metadata_scope
                if previous_options is not None
                else "all"
            ),
            default_metadata_keys=(
                tuple(value.key for value in previous_options.metadata_values)
                if previous_options is not None
                else ()
            ),
            default_upload_to_elab=(
                previous_options.upload_to_elab
                if previous_options is not None
                else default_upload_to_elab
            ),
            elab_upload_available=elab_available,
            elab_upload_hint=elab_hint,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_manual_save_options(dialog.options())

    def _apply_manual_save_options(self, options: ManualSpectrumSaveOptions) -> None:
        """Store a validated archive policy without creating a file or writer."""

        self._manual_save_options = options
        policy = (
            "append session"
            if options.mode is ManualSpectrumSaveMode.APPEND
            else "new timestamped file"
        )
        self.manual_save_target.setText(
            f"Configured archive: {options.destination} · {policy}"
        )
        self._update_manual_save_controls()

    def _save_configured_manual_spectrum(self) -> None:
        if self._manual_archive_thread is not None:
            return
        options = self._manual_save_options
        if options is None:
            self.manual_save_status.setText(
                "Configure the archive before saving a completed spectrum."
            )
            return
        if self._latest_trace is None:
            self.manual_save_status.setText(
                "Acquire a completed spectrum before saving it."
            )
            return
        try:
            trace, processed, processed_unit, operation = self._manual_trace_payload(
                options.trace_variant
            )
            if options.metadata_scope == "none":
                metadata_values = ()
            else:
                provider = self._manual_metadata_provider or self.manual_metadata_values
                available = {value.key: value for value in provider()}
                if options.metadata_scope == "all":
                    metadata_values = tuple(available.values())
                else:
                    keys = tuple(value.key for value in options.metadata_values)
                    missing = set(keys) - available.keys()
                    if missing:
                        raise ValueError(f"Selected device metadata is no longer available: {', '.join(sorted(missing))}")
                    metadata_values = tuple(available[key] for key in keys)
            settings_source = (
                self._manual_settings_source_provider()
                if self._manual_settings_source_provider is not None else ""
            )
            device_idn = (
                self._manual_device_idn_provider()
                if self._manual_device_idn_provider is not None else {}
            )
            if self._device_idn:
                device_idn = {**device_idn, "anritsu": self._device_idn}
            operator_context = (
                self._manual_operator_context_provider()
                if self._manual_operator_context_provider is not None else {}
            )
            if self._manual_archive is None:
                self._manual_archive = ManualSpectrumArchive(
                    settings_source=settings_source,
                    device_idn=device_idn,
                    operator_context=operator_context,
                    simulation=self._manual_simulation,
                    isolate_validation=True,
                )
            payload = dict(
                trace=trace,
                destination=options.destination,
                mode=options.mode,
                metadata_values=metadata_values,
                metadata_scope=options.metadata_scope,
                trace_variant=options.trace_variant,
                processed_values=processed,
                processed_unit=processed_unit,
                processing_operation=operation,
                capture_context={
                    "schema_version": 1,
                    "settings_source": settings_source,
                    "device_idn": deepcopy(device_idn),
                    "operator_context": deepcopy(operator_context),
                    "simulation": self._manual_simulation,
                    "sample_target": deepcopy(getattr(self, "_manual_sample_target", {})),
                },
            )
            self._start_manual_archive_job("save", payload, options=options, metadata_count=len(metadata_values))
        except Exception as exc:
            self.banner.show_message(
                f"Manual spectrum save failed: {exc}",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu manual spectrum save failed: {exc}")
            self._update_manual_save_controls()
            self.manual_save_status.setText(f"Manual spectrum save failed: {exc}")
            return
        return

    def _manual_save_completed(self, result, options, metadata_count):
        self._manual_archive_last_path = result.path
        self._manual_last_mode = result.mode
        self.manual_save_status.setText(
            f"Saved spectrum #{result.point_index + 1} · {result.path.name} · "
            f"{metadata_count} device value(s)."
        )
        if result.mode is ManualSpectrumSaveMode.APPEND:
            self.manual_save_target.setText(
                f"Append session: {result.path} · {result.point_count} spectrum(s)"
            )
        else:
            self.manual_save_target.setText(f"Last timestamped file: {result.path}")
        self.banner.show_message(
            f"Manual spectrum saved to {result.path.name}.", severity="success"
        )
        self.status.emit(f"Anritsu manual spectrum saved: {result.path}")
        if options.upload_to_elab:
            callback = self._manual_elab_upload_callback
            if callback is None:
                self.banner.show_message(
                    "The result was saved locally, but no eLab upload service is configured.",
                    severity="warning",
                    timeout_ms=0,
                )
            elif result.mode is not ManualSpectrumSaveMode.TIMESTAMPED:
                self.banner.show_message(
                    "The result was saved locally. eLab upload requires a closed timestamped file.",
                    severity="warning",
                    timeout_ms=0,
                )
            else:
                callback(result.path)
        self._update_manual_save_controls()

    def close_manual_archive_session(self) -> None:
        if self._manual_archive_thread is not None:
            return
        archive = self._manual_archive
        if archive is None or archive.active_path is None:
            return
        self._start_manual_archive_job("close", {}, path=archive.active_path)

    def _manual_close_completed(self, path):
        self.manual_save_status.setText("Append session closed; committed spectra remain available.")
        self.manual_save_target.setText(
            f"Append session closed: {path.name} · it can be resumed later."
        )
        self.close_manual_archive.setEnabled(False)
        self.status.emit(f"Anritsu manual archive closed: {path}")

    def _start_manual_archive_job(self, operation, payload, **context):
        if self._manual_archive_thread is not None:
            raise RuntimeError("A manual archive operation is already running.")
        self._manual_archive_job = (operation, context)
        self._manual_archive_result = None
        thread = self._manual_archive_thread = QThread(self)
        worker = self._manual_archive_worker = ManualArchiveWorker(self._manual_archive, operation, payload)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._manual_archive_completed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._manual_archive_finished)
        self.manual_save_status.setText("Saving spectrum…" if operation == "save" else "Closing append session…")
        self._update_manual_save_controls()
        thread.start()

    def _manual_archive_completed(self, result, error):
        self._manual_archive_result = (result, error)

    def _manual_archive_finished(self):
        thread = self._manual_archive_thread
        self._manual_archive_thread = self._manual_archive_worker = None
        operation, context = self._manual_archive_job
        result, error = self._manual_archive_result
        self._manual_archive_job = self._manual_archive_result = None
        thread.deleteLater()
        self._update_manual_save_controls()
        if error is not None:
            message = f"Manual archive {operation} failed: {error}"
            self.banner.show_message(message, severity="error", timeout_ms=0)
            self.status.emit(message)
            self.manual_save_status.setText(message)
        elif operation == "save":
            self._manual_save_completed(result, **context)
        else:
            self._manual_close_completed(context["path"])
        pending = getattr(self, "_pending_manual_sample_context", None)
        if pending is not None:
            self._pending_manual_sample_context = None
            self.set_sample_measurement_context(*pending)

    def prepare_manual_archive_shutdown(self) -> bool:
        if self._manual_archive_thread is not None:
            return False
        if self._manual_archive is not None and self._manual_archive.active_path is not None:
            self.close_manual_archive_session()
            return False
        return True

    def _correction_busy_changed(self, busy: bool) -> None:
        if busy:
            self._timer.stop()
            self._set_page_state(AnritsuPageState.ACQUIRING_SPECTRUM)
        elif self._page_state not in {AnritsuPageState.DISCONNECTED, AnritsuPageState.ERROR}:
            self._set_page_state(AnritsuPageState.IDLE)
        else:
            self._apply_page_state()

    def _result(self, operation: str, result: object) -> None:
        if operation.startswith("read_background_filter_configuration:"):
            self._background_configuration_received(operation, result)
            return
        settings_changed = operation in {"configure", "configure_advanced_spectrum", "read_configuration", "read_full_configuration", "read_acquisition_configuration", "read_advanced_spectrum", "connect", "disconnect"}
        if settings_changed:
            self._background_config_snapshot = None
            self._background_config_error = None
            self._background_config_pending = None
            self._background_config_timer.stop()
            self._invalidate_spectrogram_filters()
        if self.correction_workspace.handle_result(operation, result):
            return
        if settings_changed and operation != "disconnect":
            QTimer.singleShot(0, self._revalidate_background_preview)
        if operation in {"abort", "abort_acquisition", "emergency_off", "disconnect"} and self.correction_workspace.running:
            self.correction_workspace.stop_acquisition()
        if operation == "connect":
            self._device_idn = str(getattr(result, "idn", "") or "")
            self._spectrogram_buffer.clear()
            self._refresh_spectrogram_display()
            self._set_page_state(AnritsuPageState.IDLE)
        if operation in {"read_configuration", "read_full_configuration"} and isinstance(
            result, AnritsuFullConfigurationReadback
        ):
            self._last_full_readback = result
            self.status.emit("Anritsu full configuration read from instrument")
            if self._save_readback_pending:
                self._save_readback_pending = False
                self._confirm_full_settings_readback(result)
            else:
                self._show_full_readback_dialog(result)
        elif operation in {"read_configuration", "read_full_configuration"} and isinstance(
            result, AnritsuConfigurationSnapshot
        ):
            self._last_configuration = result
            self._set_frequency_bounds(result.start_hz, result.stop_hz)
            self.reference.setText(f"{result.reference_level_dbm:.9g} dBm")
            point_index = self.points.findData(result.points)
            if point_index < 0:
                raise ValueError(
                    f"Instrument returned {result.points} points outside the configured UI choices."
                )
            self.points.setCurrentIndex(point_index)
            self.banner.show_message(
                f"Current analyser settings loaded into the form (mode: "
                f"{result.instrument_mode or 'unknown'}). "
                "The instrument and safety limits were not changed.",
                severity="success",
            )
            self.status.emit("Anritsu current configuration read from instrument")
            if self._save_readback_pending:
                self._controller.call("read_advanced_spectrum")
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
            if operation == "read_advanced_spectrum" and self._save_readback_pending:
                self._save_readback_pending = False
                self._confirm_settings_readback()
        elif operation in {
            "read_signal_generator",
            "configure_signal_generator",
        } and isinstance(result, SignalGeneratorSnapshot):
            self._last_signal_generator_snapshot = result
            self._show_signal_generator_snapshot(result)
            self._sg_configured = operation == "configure_signal_generator"
            self._apply_page_state()
            verb = "configured and verified" if operation == "configure_signal_generator" else "read"
            self.status.emit(f"Anritsu signal generator {verb}; RF state confirmed")
        elif operation == "set_signal_generator_output":
            self._set_sg_output_state(bool(result))
            self._apply_page_state()
            self.status.emit(
                "Anritsu signal generator RF OUTPUT "
                + ("ON" if self._sg_output_enabled else "OFF")
            )
        elif operation == "configure" and isinstance(result, AnritsuConfigurationSnapshot):
            self._pending_after_spectrum_configuration = None
            self._result("read_configuration", result)
            self.banner.show_message(
                "Spectrum settings applied to Anritsu and verified by SCPI readback.",
                severity="success",
            )
            self.status.emit("Anritsu configured and verified by SCPI readback")
            self._set_page_state(AnritsuPageState.IDLE)
        elif operation == "start_live" and isinstance(result, AnritsuConfigurationSnapshot):
            self._live_transition_pending = False
            if self._execution_controlled:
                # An acknowledgement queued before the run cannot restart Live.
                return
            self._result("read_configuration", result)
            self._spectrogram_buffer.clear()
            self._refresh_spectrogram_display()
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
            mode = "fast binary current-trace polling from the analyser's continuous measurement"
            self.info.setText(f"Live started; {mode}. Waiting for first frame...")
            self.status.emit(f"Anritsu Live started: {mode}")
        elif operation == "stop_live":
            self._live_transition_pending = False
            self.live.setText("Start Live")
            self._set_live_indicator("off")
            self._set_page_state(AnritsuPageState.IDLE)
            self.info.setText("Live stopped.")
            self.status.emit("Anritsu Live stopped")
        elif operation in {"fetch_trace", "fetch_current_trace", "fetch_current_trace_fast", "single_sweep", "acquire_fresh_trace"} and isinstance(result, SpectrumTrace):
            self._fetch_pending = False
            self._manual_trace_deadline_monotonic = None
            if self._discard_cancelled_average_frame:
                self._discard_cancelled_average_frame = False
                self._fetch_started_monotonic = None
                self._request_next_average_frame()
                return
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
                if operation not in {"single_sweep", "acquire_fresh_trace"}:
                    # A Live polling request may already have been in flight
                    # when averaging started. It cannot prove a new sweep.
                    self._request_next_average_frame()
                    return
                try:
                    anchor = self._averaging_source_trace
                    frequencies = result.frequencies_hz
                    if (len(frequencies) != len(result.powers_dbm)
                            or not all(math.isfinite(value) for value in frequencies)
                            or any(right <= left for left, right in zip(frequencies, frequencies[1:]))):
                        raise ValueError("Spectrum frequency grid is invalid.")
                    if anchor is not None and (
                        result.configuration_generation != anchor.configuration_generation
                        or result.trace_name != anchor.trace_name
                        or not frequency_grids_match(anchor.frequencies_hz, frequencies)
                    ):
                        raise ValueError("Analyser configuration or frequency grid changed during averaging; acquire a new series.")
                    completed = self._averager.add(result.powers_dbm)
                    if anchor is None:
                        self._averaging_source_trace = result
                except ValueError as exc:
                    self._finish_temporal_averaging(resume_live=False)
                    self.info.setText(f"Averaging stopped: {exc}")
                    self.status.emit(f"Anritsu averaging stopped: {exc}")
                    return
                target = self._averaging_target_count
                self.average_progress.setValue(completed)
                self.average_progress.setFormat(f"{completed} / {target}")
                label = (
                    "reference" if self._averaging_destination == "reference" else "spectrum"
                )
                now = time.monotonic()
                start = self._averaging_start_monotonic or now
                elapsed = max(1e-3, now - start)
                rate = completed / elapsed
                if completed >= target:
                    stats_str = f"{target} spectra taken in {elapsed:.1f} s ({rate:.1f}/s)"
                    self.average_stats_label.setText(f"Completed: {stats_str}")
                    averaged = self._averager.result()
                    averaged_trace = replace(
                        result,
                        powers_dbm=averaged,
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
                        completion = f"Averaged reference completed: {stats_str}"
                    else:
                        self._averaged_trace = averaged_trace
                        self._display_revision += 1
                        self._invalidate_analysis_results()
                        self.show_average.setChecked(True)
                        completion = f"Averaged spectrum completed: {stats_str}"
                    self._finish_temporal_averaging(resume_live=True)
                    self.info.setText(completion)
                    self.status.emit(f"Anritsu {completion.lower()}")
                    self._refresh_spectrum_display()
                else:
                    self.average_stats_label.setText(
                        f"Progress: {completed} / {target} spectra · {elapsed:.1f} s ({rate:.1f}/s)"
                    )
                    self.info.setText(
                        f"Averaging {label}: {completed} / {target} spectra · {elapsed:.1f} s ({rate:.1f}/s)..."
                    )
                    self.status.emit(
                        f"Anritsu temporal averaging progress: {label} {completed} / {target} ({rate:.1f}/s)"
                    )
                    QTimer.singleShot(0, self._request_next_average_frame)
            else:
                if not self._timer.isActive():
                    self._set_page_state(AnritsuPageState.IDLE)
                self._show_trace(result)

    def _show_trace(
        self, trace: SpectrumTrace, *, update_controls: bool = True, execution_preview: bool = False
    ) -> None:
        provenance_changed = self._latest_trace_is_execution_preview != execution_preview
        self._latest_trace_is_execution_preview = execution_preview
        if provenance_changed:
            self._spectrogram_buffer.clear()
        if self._latest_trace is not None and (
            provenance_changed
            or self._latest_trace.configuration_generation != trace.configuration_generation
            or not frequency_grids_match(self._latest_trace.frequencies_hz, trace.frequencies_hz)
        ):
            self._invalidate_analysis_results()
            self._invalidate_spectrogram_filters()
        self._latest_trace = trace
        if provenance_changed and not update_controls:
            self._update_manual_save_controls()
        self._display_revision += 1
        self._received_trace_count += 1
        if self._trace_diagnostics_dialog is not None:
            self._trace_diagnostics_dialog.set_trace(
                trace,
                received_frame=self._received_trace_count,
            )
        self._spectrogram_buffer.append(trace)
        # Raw Live must repaint immediately.  CPU cleanup/peak analysis is
        # deliberately asynchronous and may coalesce frames; making repaint
        # wait for that worker can leave the visible trace frozen indefinitely
        # while newer analysis requests keep superseding older generations.
        first_trace = self._received_trace_count <= 1
        self._refresh_spectrum_display(auto_range=first_trace)
        if update_controls:
            self._apply_page_state()
        self._update_signal_analysis(trace)
        self._refresh_spectrogram_display()
        live_detail = ""
        if self._timer.isActive():
            now = time.monotonic()
            if self._last_frame_monotonic is not None:
                self._frame_intervals_s.append(now - self._last_frame_monotonic)
                self._frame_intervals_s = self._frame_intervals_s[-100:]
            self._last_frame_monotonic = now
            self._live_frame_count += 1
            n_pts = len(trace.powers_dbm)
            mid = n_pts // 2
            signature = (
                n_pts,
                trace.powers_dbm[0],
                trace.powers_dbm[-1],
                trace.powers_dbm[mid],
                trace.powers_dbm[mid // 2],
                trace.powers_dbm[min(n_pts - 1, 3 * mid // 2)],
            )
            if signature == self._last_live_signature:
                self._identical_live_frames += 1
                self._stale_frame_count += 1
                live_detail = f" • unchanged ×{self._identical_live_frames}"
                # Identical numeric frames are valid for a stable input and do
                # not prove stale acquisition.  Continuous sweep is enforced
                # at Live startup, so do not raise a false warning from data
                # equality alone.
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
                effective_fps = 1000.0 / effective_ms if effective_ms > 0 else 0
                live_detail += (
                    f" • requested {self.refresh.value()} ms"
                    f" • effective {effective_ms:.0f} ms ({effective_fps:.1f} FPS)"
                    f" • coalesced {self._coalesced_timer_ticks}"
                )
        self.info.setText(
            f"Acquired raw: {len(trace.powers_dbm)} points • {trace.acquired_at_utc.isoformat()} • "
            f"max {max(trace.powers_dbm):.4g} dBm{live_detail}"
        )

    def _on_trace_checkbox_toggled(self, sender: CheckBox, checked: bool) -> None:
        if self._switching_trace_checkboxes:
            return

        proc = self._candidate_traces.get("processed")
        has_relative_processed = proc is not None and proc.unit != "dBm"

        if has_relative_processed:
            self._switching_trace_checkboxes = True
            try:
                if sender is self.show_processed:
                    if checked:
                        self.show_raw.setChecked(False)
                        self.show_average.setChecked(False)
                        self.show_reference.setChecked(False)
                    else:
                        if not any((
                            self.show_raw.isChecked(),
                            self.show_average.isChecked(),
                            self.show_reference.isChecked(),
                            self.show_analysis.isChecked(),
                        )):
                            self.show_raw.setChecked(True)
                elif sender in (self.show_raw, self.show_average, self.show_reference):
                    if checked:
                        self.show_processed.setChecked(False)
                    else:
                        if not any((
                            self.show_raw.isChecked(),
                            self.show_average.isChecked(),
                            self.show_reference.isChecked(),
                            self.show_processed.isChecked(),
                            self.show_analysis.isChecked(),
                        )):
                            self.show_raw.setChecked(True)
            finally:
                self._switching_trace_checkboxes = False

        self._display_controls_changed(auto_range=True)

    def _invalidate_analysis_results(self) -> None:
        self._invalidated_before_generation = self._analysis_generation
        self._analysis_generation += 1
        self._cleanup_result = None
        self._analysis_source_snapshot = None
        self._analysis_raw_snapshot = None
        self._analysis_error = None
        self._analysis_wait_reason = None
        self._detected_peaks = ()
        self._sync_peak_markers()

    def _display_controls_changed(self, *_args: object, auto_range: bool = False) -> None:
        self._display_revision += 1
        self._refresh_spectrum_display(auto_range=auto_range)
        if (
            self.show_analysis.isChecked()
            and self._cleanup_result is None
            and self._latest_trace is not None
            and (self._selected_cleanup_modes() or self._analysis_parameters.temporal_average_frames > 1)
        ):
            self._update_signal_analysis(self._latest_trace)

    def _analysis_source_changed(self, *_args: object) -> None:
        selected = self.analysis_source.currentData()
        self._analysis_source_selection = str(selected or "auto")
        self._invalidate_analysis_results()
        self._refresh_spectrum_display()
        if self._latest_trace is not None:
            self._update_signal_analysis(self._latest_trace)

    def _cleanup_history(self, source_key: str = "raw") -> tuple[tuple[float, ...] | np.ndarray, ...]:
        # Use every received frame, as in the spectrogram worker. Heatmap
        # decimation must not change the evidence for stationary-line removal.
        _times, rows = self._spectrogram_buffer.processing_tail(24)
        if source_key == "raw":
            return rows
        source = self._candidate_traces.get(source_key)
        if source is None:
            return ()
        if source.key != "processed" or self._reference_trace is None:
            return ()
        if source.unit not in {"dBm", "dB"}:
            return ()
        operation = str(self.reference_operation.currentData() or "none")
        return tuple(
            apply_reference_operation(row, self._reference_trace.powers_dbm, operation)[0]
            for row in rows
        )

    def _selected_cleanup_modes(self) -> tuple[str, ...]:
        background = self.cleanup_filters["background"].isChecked()
        return tuple(key for key in SPECTRUM_FILTER_ORDER if self.cleanup_filters[key].isChecked()
                     and not (background and key == "emi_reject"))

    def _signal_analysis_controls_changed(self, *_args: object) -> None:
        if self._changing_correction:
            return
        self._background_config_error = None
        enabled = self.cleanup_filters["background"].isChecked()
        if enabled and self.correction_workspace._profile is None:
            self.cleanup_filters["background"].blockSignals(True)
            self.cleanup_filters["background"].setChecked(False)
            self.cleanup_filters["background"].blockSignals(False)
            self._pending_correction = "background"
            self._open_background_setup()
            self._update_correction_summary()
            return
        if enabled and self.reference_operation.currentData() != "none":
            self._changing_correction = True
            self.correction_controls.reference.setChecked(False)
            self.reference_operation.setCurrentIndex(self.reference_operation.findData("none"))
            self._changing_correction = False
            self.show_processed.setChecked(False)
            self.show_raw.setChecked(True)
        if enabled and not self._background_filter_enabled:
            self._background_config_snapshot = None
        self._background_filter_enabled = enabled
        unit = "W" if enabled else getattr(self._candidate_traces.get(self._analysis_source_key), "unit", "dBm")
        logarithmic = unit in {"dBm", "dB"}
        self.cleanup_filters["emi_reject"].setEnabled(logarithmic)
        self.cleanup_filters["emi_reject"].setToolTip(
            "EMI uses dB thresholds and is unavailable for the signed W background residual. Use Narrow peaks."
            if not logarithmic else
            "Reject stationary-line candidates using recent Live frames. A desired carrier can also be stationary.")
        self._invalidate_spectrogram_filters()
        self._update_correction_summary()
        self._refresh_spectrogram_display()
        self.overlay_analysis_source.setToolTip(
            "Compare the corrected input with the filtered result in the same units.")
        if self._selected_cleanup_modes() and not self.show_analysis.isChecked():
            self.show_analysis.setChecked(True)
        if self._latest_trace is None:
            self._invalidate_analysis_results()
            self._cleanup_result = None
            self._detected_peaks = ()
            self._sync_peak_markers()
            return
        self._invalidate_analysis_results()
        self._refresh_spectrum_display()
        self._update_signal_analysis(self._latest_trace)

    def _update_signal_analysis(
        self, trace: SpectrumTrace, *, detect_peaks: bool | None = None
    ) -> None:
        del trace  # the immutable display snapshot below is the source of truth
        if not self._ordinary_preview_active():
            return
        source_key = self._analysis_source_key
        source = self._candidate_traces.get(source_key)
        if source is None:
            self._set_analysis_status(
                "Show a spectrum trace before starting analysis."
            )
            return
        mode = self._selected_cleanup_modes()
        context, profile = None, None
        if "background" in mode:
            try:
                context, profile = self._background_for_filter(source.frequencies_hz)
            except ValueError as exc:
                self._cleanup_result = None
                self._detected_peaks = ()
                pending = self._background_config_pending is not None
                self._analysis_wait_reason = str(exc) if pending else None
                self._analysis_error = None if pending else str(exc)
                self._set_analysis_status(str(exc) if pending else f"Background unavailable: {exc}")
                self._update_correction_summary()
                self._refresh_spectrum_display()
                return
        self._analysis_wait_reason = None
        self._analysis_generation += 1
        parameters = self._analysis_parameters
        times, rows = self._spectrogram_buffer.processing_tail(parameters.temporal_average_frames)
        reference_values, reference_operation = None, "none"
        values = source.values
        if parameters.temporal_average_frames > 1 and source.key == "processed" and self._latest_trace is not None:
            values = self._latest_trace.powers_dbm
            reference_values = self._reference_trace.powers_dbm if self._reference_trace is not None else None
            reference_operation = str(self.reference_operation.currentData() or "none")
        if source.key not in {"raw", "processed"}:
            parameters = replace(parameters, temporal_average_frames=1)
        request = SpectrumAnalysisRequest(
            generation=self._analysis_generation,
            frequencies_hz=source.frequencies_hz,
            powers_dbm=values,
            mode=mode,
            history_dbm=(self._cleanup_history(source.key) if "emi_reject" in mode else ()),
            detect_peaks=self.auto_peak_detection.isChecked() if detect_peaks is None else detect_peaks,
            source_key=source.key,
            frame_id=self._display_revision,
            source_unit=source.unit,
            provenance=source.provenance,
            parameters=parameters,
            background_context=context,
            background_profile=profile,
            source_snapshot=source,
            allow_gaps=source.key == "processed",
            raw_snapshot=self._latest_trace,
            power_rows=rows, timestamps_s=times,
            reference_values_dbm=reference_values, reference_operation=reference_operation,
            interference_calibration=(self.correction_workspace.interference_mode.currentData()
                                      if "background" in mode else None),
            tracking_context=self._tracking_context(),
            additional_tracking_contexts=tuple(state["context"] for window, state in self._additional_peak_trackers.items()
                                              if not window.paused.isChecked()),
        )
        if self._cleanup_result is None and self._analysis_error is None:
            self._set_analysis_status(
                f"Analyzing {source.label} ({source.unit}) on the background CPU worker..."
            )
        self._analysis_controller.submit(request)

    def _analysis_completed(self, result: object) -> None:
        if not isinstance(result, SpectrumAnalysisOutcome):
            return
        if result.generation <= self._invalidated_before_generation:
            return
        if result.generation < self._applied_analysis_generation:
            return
        if result.source_key != self._analysis_source_key:
            return
        source = self._candidate_traces.get(result.source_key)
        if source is None or source.unit != result.source_unit:
            return
        if len(result.cleanup.values) != len(source.frequencies_hz):
            return
        if result.frequencies_hz and not frequency_grids_match(result.frequencies_hz, source.frequencies_hz):
            return
        self._applied_analysis_generation = result.generation
        self._analysis_error = None
        self._analysis_wait_reason = None
        self._cleanup_result = result.cleanup
        self._preview_statistics = result.statistics
        self._analysis_source_snapshot = result.source_snapshot
        self._analysis_raw_snapshot = result.raw_snapshot
        if result.peaks is not None:
            self._detected_peaks = result.peaks
        else:
            # A one-off analysis with Auto peaks off belongs to its own frame.
            self._detected_peaks = ()
        self._refresh_spectrum_display()
        self._sync_peak_markers()
        self._update_analysis_status()
        self._update_correction_summary()
        if self._peak_table_dialog is not None:
            self._peak_table_dialog.set_peaks(
                self._detected_peaks, method=self._peak_measurement_method()
            )
        self._update_peak_tracking(time.monotonic(), result)

    def _analysis_failed(self, generation: int, message: str) -> None:
        if generation <= self._invalidated_before_generation or generation < self._applied_analysis_generation:
            return
        self._applied_analysis_generation = generation
        self._analysis_error = message
        self._cleanup_result = None
        self._detected_peaks = ()
        self._set_analysis_status(f"Signal analysis unavailable: {message}")
        self._update_correction_summary()
        self._sync_peak_markers()
        self._refresh_spectrum_display()

    def _analysis_values(self) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
        cleanup = self._cleanup_result
        if cleanup is None or self._analysis_source_key is None:
            return None
        source = self._candidate_traces.get(self._analysis_source_key)
        if (
            source is None
            or len(source.frequencies_hz) != len(cleanup.values)
        ):
            return None
        values = (cleanup.values if self._analysis_parameters.peak_measure_filtered or cleanup.input_values is None
                  else cleanup.input_values)
        return source.frequencies_hz, values

    def _analyze_current_spectrum(self, *, force: bool = False) -> None:
        trace = self._latest_trace
        if trace is None:
            self._set_analysis_status(
                "Acquire a completed spectrum before detecting peaks."
            )
            return
        self._update_signal_analysis(trace, detect_peaks=True if force else None)

    def _update_analysis_status(self) -> None:
        cleanup = self._cleanup_result
        if cleanup is None:
            return
        interference = len(cleanup.stationary_interference_indices)
        source = self._candidate_traces.get(self._analysis_source_key)
        source_label = f"{source.label} [{source.unit}] · " if source is not None else ""
        notes = " · " + " ".join(cleanup.notes) if cleanup.notes else ""
        notes += " · " + ("Peaks measured on filtered preview." if self._analysis_parameters.peak_measure_filtered
                            else "Peaks measured before display filters.")
        if self.cleanup_filters["narrow_reject"].isChecked():
            self._set_analysis_status(
                f"{source_label}{cleanup.method} · {len(cleanup.removed_peak_indices)} narrow extrema · "
                f"{len(cleanup.modified_bin_indices)} bins replaced · "
                f"width ≤ {format_quantity_auto(self._analysis_parameters.narrow_max_width_hz, DIMENSION_FREQUENCY)}"
                + notes
            )
            return
        self._set_analysis_status(
            f"{source_label}{cleanup.method} · heuristic noise scale {cleanup.noise_sigma_db:.3g} "
            f"{'dB' if cleanup.unit in {'dBm', 'dB'} else cleanup.unit} · "
            f"{len(self._detected_peaks)} peak(s) · "
            f"{interference} stationary-line candidate bin(s){notes}"
        )

    def _peak_measurement_method(self):
        if self._cleanup_result is None:
            return "unavailable"
        source = "filtered preview" if self._analysis_parameters.peak_measure_filtered else "before display filters"
        return f"Peak source: {source} [{self._cleanup_result.unit}] · {self._cleanup_result.method}"

    def _sync_peak_markers(self, *_args: object) -> None:
        plots = [self.spectrum_plot]
        if self._spectrum_window is not None and not self._spectrum_window.spectrum.frozen:
            plots.append(self._spectrum_window.spectrum)
        for plot in plots:
            plot.clear_replacement_markers()
        if (self.highlight_replacements.isChecked() and self.cleanup_filters["narrow_reject"].isChecked()
                and self._cleanup_result is not None):
            source = self._candidate_traces.get(self._analysis_source_key)
            if source is not None:
                indices = self._cleanup_result.removed_peak_indices
                for plot in plots:
                    plot.set_replacement_markers([source.frequencies_hz[i] for i in indices],
                                                [self._cleanup_result.values[i] for i in indices])
        if not self.highlight_peaks.isChecked() or not self._detected_peaks:
            for plot in plots:
                plot.clear_peak_markers()
            return
        for plot in plots:
            plot.set_peak_markers(
                [peak.frequency_hz for peak in self._detected_peaks],
                [peak.amplitude_dbm for peak in self._detected_peaks],
            )

    def _open_analysis_settings(self, *, focus_peaks: bool = False) -> None:
        section = "peaks" if focus_peaks else "filters"
        if self._analysis_settings_dialog is not None and self._analysis_settings_dialog.section != section:
            self._analysis_settings_dialog.close()
        if self._analysis_settings_dialog is None:
            dialog = SpectrumAnalysisSettingsDialog(
                self, current_parameters=self._analysis_parameters,
                source_unit="W" if self.cleanup_filters["background"].isChecked()
                else getattr(self._candidate_traces.get(self._analysis_source_key), "unit", "dBm"),
                section=section,
            )
            dialog.parameters_applied.connect(self._analysis_parameters_applied)
            dialog.finished.connect(self._analysis_settings_closed)
            self._analysis_settings_dialog = dialog
        dialog = self._analysis_settings_dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if focus_peaks:
            QTimer.singleShot(0, dialog.focus_peaks)

    def _analysis_parameters_applied(self, parameters: object) -> None:
        if isinstance(parameters, SpectrumAnalysisParameters):
            self._analysis_parameters = parameters
            average = self.correction_controls.power_average
            average.blockSignals(True)
            index = average.findData(parameters.temporal_average_frames)
            if index < 0:
                average.addItem(f"Avg: {parameters.temporal_average_frames}", userData=parameters.temporal_average_frames)
                index = average.count() - 1
            average.setCurrentIndex(index)
            average.blockSignals(False)
            if parameters.temporal_average_frames > 1:
                self.show_analysis.setChecked(True)
            self.peak_count.blockSignals(True)
            self.peak_count.setValue(parameters.peak_max_count)
            self.peak_count.blockSignals(False)
            self._invalidate_spectrogram_filters()
            self._refresh_spectrogram_display()
            self._invalidate_analysis_results()
            self._refresh_spectrum_display()
            if self._latest_trace is not None:
                self._update_signal_analysis(self._latest_trace)
            self._update_correction_summary()

    def _power_average_changed(self, *_args):
        count = self.correction_controls.power_average.currentData()
        if count is not None:
            self._analysis_parameters_applied(replace(self._analysis_parameters, temporal_average_frames=int(count)))

    def preview_settings_snapshot(self):
        parameters = self._analysis_parameters
        return {"average_frames": parameters.temporal_average_frames,
                "reset_gap": format_quantity_auto(parameters.temporal_max_gap_s, DIMENSION_TIME, precision=17),
                "protected_bands": [[format_quantity_auto(value, DIMENSION_FREQUENCY, precision=17) for value in band]
                                    for band in parameters.narrow_protected_regions_hz],
                "measure_filtered_peaks": parameters.peak_measure_filtered}

    def _reset_preview_average(self):
        self._spectrogram_buffer.reset_processing()
        self._preview_statistics = None
        if self._latest_trace is not None:
            self._spectrogram_buffer.append(self._latest_trace)
        self._analysis_parameters_applied(self._analysis_parameters)

    def _preview_model_changed(self, *_args):
        self._analysis_parameters_applied(self._analysis_parameters)

    def _open_processing_quality(self):
        if self._processing_quality_dialog is None:
            from .processing_quality_dialog import ProcessingQualityDialog

            self._processing_quality_dialog = ProcessingQualityDialog(self)
            self._processing_quality_dialog.finished.connect(self._processing_quality_closed)
        self._processing_quality_dialog.show()
        self._processing_quality_dialog.raise_()

    def _processing_quality_closed(self, *_args):
        dialog, self._processing_quality_dialog = self._processing_quality_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _analysis_settings_closed(self, _result: int = 0) -> None:
        dialog = self._analysis_settings_dialog
        self._analysis_settings_dialog = None
        if dialog is not None:
            dialog.deleteLater()

    def _open_peak_table(self) -> None:
        self._analyze_current_spectrum(force=True)
        if self._peak_table_dialog is None:
            dialog = PeakTableDialog(self)
            dialog.peak_selected.connect(self.spectrum_plot.select_peak_marker)
            dialog.track_requested.connect(self._start_peak_tracking)
            dialog.closed.connect(self._peak_table_closed)
            self._peak_table_dialog = dialog
        dialog = self._peak_table_dialog
        dialog.set_peaks(
            self._detected_peaks,
            method=self._peak_measurement_method(),
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _plot_peak_selected(self, index: int) -> None:
        if self._peak_table_dialog is None:
            self._open_peak_table()
        dialog = self._peak_table_dialog
        if dialog is not None and 0 <= index < dialog.table.rowCount():
            dialog.table.selectRow(index)

    def _peak_table_closed(self) -> None:
        dialog = self._peak_table_dialog
        self._peak_table_dialog = None
        if dialog is not None:
            dialog.deleteLater()

    def _start_peak_tracking(self, index: int) -> None:
        if not 0 <= index < len(self._detected_peaks):
            return
        peak = self._detected_peaks[index]
        data = self._analysis_values()
        if data is None:
            return
        frequencies_hz = np.asarray(data[0], dtype=float)
        if len(frequencies_hz) < 2:
            return
        if self._peak_tracking_window is not None and self._tracked_peak_target_hz is None:
            self._peak_tracking_window.close()
        if self._peak_tracking_window is not None and self._tracked_peak_target_hz is not None:
            self._additional_peak_trackers[self._peak_tracking_window] = {
                "context": (self._tracking_session, self._tracked_peak_target_hz, self._tracked_peak_gate_hz), "revision": self._tracked_peak_revision,
                "generation": self._tracked_peak_generation, "started": self._tracking_started_monotonic,
            }
        self._peak_tracking_window = None
        self._tracking_session = getattr(self, "_tracking_session", 0) + 1
        spacing_hz = float(np.median(np.abs(np.diff(frequencies_hz))))
        width_hz = peak.fit_fwhm_hz or peak.fwhm_hz
        self._tracked_peak_target_hz = peak.frequency_hz
        self._tracked_peak_gate_hz = max(
            spacing_hz * 5.0,
            (width_hz * 2.0 if width_hz is not None else 0.0),
        )
        self._tracked_peak_generation = getattr(self, "_applied_analysis_generation", -1)
        self._tracked_peak_revision = self._display_revision
        self._tracking_started_monotonic = time.monotonic()
        if self._peak_tracking_window is None:
            window = PeakTrackingWindow(self)
            window.closed.connect(lambda current=window: self._peak_tracking_closed(current))
            window.history_cleared.connect(lambda current=window: self._peak_tracking_history_cleared(current))
            window.gate_changed.connect(lambda gate, current=window: self._peak_tracking_gate_changed(current, gate))
            window.paused.toggled.connect(lambda paused, current=window: self._peak_tracking_paused(current, paused))
            self._peak_tracking_window = window
        tracking = self._peak_tracking_window
        tracking.setWindowTitle(f"Anritsu — track {self._tracking_session}: {format_quantity_auto(peak.frequency_hz, DIMENSION_FREQUENCY)}")
        tracking.gate.setText(format_quantity_auto(self._tracked_peak_gate_hz, DIMENSION_FREQUENCY))
        tracking.gate_hz = self._tracked_peak_gate_hz
        tracking.clear()
        tracking.append(
            0.0,
            peak,
            source=self._peak_measurement_method(),
        )
        tracking.show()
        tracking.raise_()
        tracking.activateWindow()
        self.spectrum_plot.select_peak_marker(index)
        self.status.emit(
            f"Anritsu local peak tracking started at {peak.frequency_hz:.12g} Hz"
        )

    def _tracking_context(self):
        if (self._peak_tracking_window is None or self._tracked_peak_target_hz is None
                or self._tracked_peak_gate_hz is None
                or self._peak_tracking_window.paused.isChecked()):
            return None
        return (getattr(self, "_tracking_session", 0), self._tracked_peak_target_hz,
                self._tracked_peak_gate_hz)

    def _update_peak_tracking(self, now: float, result: SpectrumAnalysisOutcome) -> None:
        for context, peak in getattr(result, "additional_tracked_peaks", ()):
            for window, state in tuple(getattr(self, "_additional_peak_trackers", {}).items()):
                if (window.paused.isChecked() or state["context"] != context
                        or result.frame_id <= state["revision"]
                        or self._applied_analysis_generation <= state["generation"]):
                    continue
                state["revision"] = result.frame_id
                state["generation"] = self._applied_analysis_generation
                elapsed = max(0., now - state["started"])
                if peak is None:
                    window.mark_lost(target_hz=context[1], gate_hz=context[2], elapsed_s=elapsed)
                else:
                    state["context"] = (context[0], peak.frequency_hz, context[2])
                    window.append(elapsed, peak, source=self._peak_measurement_method())
        target_hz = self._tracked_peak_target_hz
        gate_hz = self._tracked_peak_gate_hz
        tracking = self._peak_tracking_window
        current_gen = getattr(self, "_applied_analysis_generation", -1)
        if (
            target_hz is None
            or gate_hz is None
            or tracking is None
            or result.tracking_context is None
            or result.tracking_context != self._tracking_context()
            or result.frame_id <= getattr(self, "_tracked_peak_revision", -1)
            or current_gen <= getattr(self, "_tracked_peak_generation", -1)
        ):
            return
        self._tracked_peak_revision = result.frame_id
        self._tracked_peak_generation = current_gen
        nearest = result.tracked_peak
        if nearest is None or abs(nearest.frequency_hz - target_hz) > gate_hz:
            tracking.mark_lost(target_hz=target_hz, gate_hz=gate_hz,
                               elapsed_s=max(0., now - (self._tracking_started_monotonic or now)))
            return
        self._tracked_peak_target_hz = nearest.frequency_hz
        started = self._tracking_started_monotonic or now
        tracking.append(
            max(0.0, now - started),
            nearest,
            source=self._peak_measurement_method(),
        )

    def _peak_tracking_closed(self, window=None) -> None:
        if window is not None and window is not self._peak_tracking_window:
            self._additional_peak_trackers.pop(window, None)
            window.deleteLater()
            return
        tracking = self._peak_tracking_window
        self._peak_tracking_window = None
        self._tracked_peak_target_hz = None
        self._tracked_peak_gate_hz = None
        self._tracked_peak_generation = -1
        self._tracked_peak_revision = -1
        self._tracking_started_monotonic = None
        if tracking is not None:
            tracking.deleteLater()
        self.status.emit("Anritsu local peak tracking stopped")

    def _peak_tracking_history_cleared(self, window=None) -> None:
        self._tracking_session = getattr(self, "_tracking_session", 0) + 1
        if window is not None and window is not self._peak_tracking_window:
            state = self._additional_peak_trackers.get(window)
            if state is not None:
                _, target, gate = state["context"]
                state["context"] = (self._tracking_session, target, gate)
                state["started"] = time.monotonic()
            return
        self._tracking_started_monotonic = time.monotonic()

    def _peak_tracking_gate_changed(self, window, gate_hz):
        if window is self._peak_tracking_window:
            self._tracked_peak_gate_hz = gate_hz
            self._tracking_session = getattr(self, "_tracking_session", 0) + 1
        elif window in self._additional_peak_trackers:
            state = self._additional_peak_trackers[window]
            self._tracking_session = getattr(self, "_tracking_session", 0) + 1
            state["context"] = (self._tracking_session, state["context"][1], gate_hz)
        self._analyze_current_spectrum(force=True)

    def _peak_tracking_paused(self, window, paused):
        self._tracking_session = getattr(self, "_tracking_session", 0) + 1
        state = self._additional_peak_trackers.get(window)
        if state is not None:
            state["context"] = (self._tracking_session, state["context"][1], state["context"][2])
        if paused:
            target = state["context"][1] if state else self._tracked_peak_target_hz
            gate = state["context"][2] if state else self._tracked_peak_gate_hz
            started = state["started"] if state else self._tracking_started_monotonic
            if target is not None and started is not None:
                window.mark_lost(target_hz=target, gate_hz=gate, elapsed_s=time.monotonic() - started)
            window.status.setText("Peak tracking paused. Spectrum acquisition is unchanged.")
        self._analyze_current_spectrum(force=True)

    @staticmethod
    def _set_combo_data(combo: ComboBox, value: object) -> None:
        index = combo.findData(value)
        if index < 0 or index == combo.currentIndex():
            return
        previous = combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(previous)

    def _spectrogram_controls_changed(self, *_args: object) -> None:
        source = str(self.spectrogram_source.currentData() or "raw")
        window_s = int(self.spectrogram_window_span.currentData() or 30)
        floating = self._spectrogram_window
        if floating is not None:
            self._set_combo_data(floating.source, source)
            self._set_combo_data(floating.window_span, window_s)
        self._refresh_spectrogram_display()

    def _floating_spectrogram_source_changed(self, source: str) -> None:
        self._set_combo_data(self.spectrogram_source, source)
        self._spectrogram_controls_changed()

    def _floating_spectrogram_window_changed(self, window_s: int) -> None:
        self._set_combo_data(self.spectrogram_window_span, window_s)
        self._spectrogram_controls_changed()

    def _open_spectrogram_window(self) -> None:
        if self._spectrogram_window is None:
            floating = _AnritsuSpectrogramWindow(self)
            floating.source_changed.connect(
                self._floating_spectrogram_source_changed
            )
            floating.window_changed.connect(
                self._floating_spectrogram_window_changed
            )
            floating.closed.connect(self._spectrogram_window_closed)
            self._spectrogram_window = floating
        floating = self._spectrogram_window
        self._set_combo_data(
            floating.source, self.spectrogram_source.currentData() or "raw"
        )
        self._set_combo_data(
            floating.window_span,
            int(self.spectrogram_window_span.currentData() or 30),
        )
        floating.show()
        self._refresh_spectrogram_display()
        floating.raise_()
        floating.activateWindow()

    def _spectrogram_window_closed(self) -> None:
        floating = self._spectrogram_window
        self._spectrogram_window = None
        if floating is not None:
            floating.deleteLater()

    def _open_spectrum_window(self) -> None:
        """Mirror completed traces with controls for the same page-owned Live session."""

        if self._spectrum_window is None:
            floating = _AnritsuSpectrumWindow(self)
            floating.closed.connect(self._spectrum_window_closed)
            floating.start_live.clicked.connect(lambda: self.toggle_live() if not self._timer.isActive() else None)
            floating.stop_live.clicked.connect(lambda: self.toggle_live() if self._timer.isActive() else None)
            floating.peak_table.clicked.connect(self._open_peak_table)
            floating.spectrum.status_changed.connect(self.status.emit)
            floating.spectrum.display_resumed.connect(self._refresh_spectrum_display)
            floating.spectrum.display_resumed.connect(self._sync_peak_markers)
            self._spectrum_window = floating
        floating = self._spectrum_window
        floating.show()
        self._apply_page_state()
        self._refresh_spectrum_display()
        if self._latest_trace is not None:
            self._update_signal_analysis(self._latest_trace)
        floating.raise_()
        floating.activateWindow()

    def _spectrum_window_closed(self) -> None:
        floating = self._spectrum_window
        self._spectrum_window = None
        if floating is not None:
            floating.deleteLater()

    def _spectrogram_matrix(
        self, *, source: str, window_s: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, str] | None:
        if source not in {"current", "raw"}:
            raise ValueError("Choose the current correction or the raw input for the spectrogram.")
        if (self._selected_cleanup_modes() or self._analysis_parameters.temporal_average_frames > 1
                or (source == "current" and self.reference_operation.currentData() != "none")):
            return self._filtered_spectrogram_matrix(source, window_s)
        return self._raw_spectrogram_matrix(window_s)

    def _validate_display_reference(self, frequencies) -> None:
        reference = self._reference_trace
        if reference is None:
            raise ValueError("Configure a captured or loaded reference first.")
        if not frequency_grids_match(frequencies, reference.frequencies_hz):
            raise ValueError("Reference frequency grid differs from the current spectrum.")
        if self._reference_spectrum is not None:
            level = self._reference_spectrum.reference_level_dbm
            current = self._last_configuration.reference_level_dbm if self._last_configuration is not None else None
            if level is not None and current is not None and not math.isclose(level, current, abs_tol=.005):
                raise ValueError(f"Reference level differs ({current:g} dBm current, {level:g} dBm reference).")
            self._validate_reference_acquisition_compatibility(self._reference_spectrum)

    def _raw_spectrogram_matrix(self, window_s):
        # Matrix assembly and contrast estimation belong to the CPU worker
        # even when no filters are selected. Share immutable display rows.
        return self._filtered_spectrogram_matrix("raw", window_s, raw_unprocessed=True)

    def _refresh_spectrogram_display(self) -> None:
        if not self._spectrogram_preview_active():
            return
        source = str(self.spectrogram_source.currentData() or "raw")
        window_s = int(self.spectrogram_window_span.currentData() or 30)
        try:
            data = self._spectrogram_matrix(source=source, window_s=window_s)
        except ValueError as exc:
            self.spectrogram_plot.clear()
            message = str(exc)
            self.spectrogram_status.setText(message)
            if self._spectrogram_window is not None:
                self._spectrogram_window.spectrogram.clear()
                self._spectrogram_window.status.setText(message)
            return
        if data is None:
            message = ("Filtering completed spectrogram frames…" if self._selected_cleanup_modes()
                       and self._spectrogram_buffer.row_count else "Start Live to accumulate a rolling spectrogram.")
            self.spectrogram_plot.clear()
            self.spectrogram_status.setText(message)
            if self._spectrogram_window is not None:
                self._spectrogram_window.spectrogram.clear()
                self._spectrogram_window.status.setText(message)
            return
        frequencies, elapsed, matrix, unit, label = data
        outcome = self._spectrogram_filter_outcome
        levels = outcome.color_levels if outcome is not None and matrix is outcome.matrix else None
        self.spectrogram_plot.set_data(
            frequencies, elapsed, matrix, unit=unit, color_levels=levels
        )
        message = (
            f"{label} · {matrix.shape[0]} completed frame(s) · "
            f"{matrix.shape[1]} frequency point(s) · rolling {window_s} s"
        )
        self.spectrogram_status.setText(message)
        if self._spectrogram_window is not None:
            self._spectrogram_window.spectrogram.set_data(
                frequencies, elapsed, matrix, unit=unit, color_levels=levels
            )
            self._spectrogram_window.status.setText(message)

    def _background_display_changed(self, context, result, description):
        # Recording and ordinary Live publish into the same preview pipeline.
        # The archive's quantitative/averaged result remains in its workspace.
        raw = self.correction_workspace._latest_raw
        if raw is not None and raw is not self._latest_trace:
            self._show_trace(raw, update_controls=False)
        self._update_correction_summary()

    def _reference_operation_changed(self, *_args: object) -> None:
        if self._changing_correction:
            return
        operation = str(self.reference_operation.currentData() or "none")
        should_show = operation != "none"
        if should_show:
            self._reference_operation_selection = operation
        self._changing_correction = True
        self.correction_controls.reference.setChecked(should_show)
        if should_show:
            self.cleanup_filters["background"].setChecked(False)
            self._background_filter_enabled = False
        self._changing_correction = False
        if should_show:
            self.show_processed.setChecked(True)
            self.show_raw.setChecked(False)
            self.show_average.setChecked(False)
            self.show_reference.setChecked(False)
        else:
            self.show_processed.setChecked(False)
            if not any((self.show_raw.isChecked(), self.show_average.isChecked(), self.show_reference.isChecked())):
                self.show_raw.setChecked(True)
        if should_show and self._reference_trace is None:
            self._set_analysis_status(
                "Capture or load a reference before displaying Raw − reference."
            )
        self._display_revision += 1
        self._analysis_source_selection = "auto"
        self._invalidate_analysis_results()
        self._invalidate_spectrogram_filters()
        self._refresh_spectrum_display(auto_range=True)
        unit = getattr(self._candidate_traces.get(self._analysis_source_key), "unit", "dBm")
        self.cleanup_filters["emi_reject"].setEnabled(unit in {"dBm", "dB"})
        self._refresh_spectrogram_display()
        self._update_correction_summary()
        if self._latest_trace is not None:
            self._update_signal_analysis(self._latest_trace)

    def _sync_analysis_source_combo(
        self, candidates: SpectrumDisplayState, visible: Mapping[str, bool]
    ) -> None:
        choices = tuple(trace for trace in candidates.traces if visible.get(trace.key, False))
        if self.cleanup_filters["background"].isChecked():
            raw = candidates.by_key.get("raw")
            choices = (raw,) if raw is not None else ()
            self._analysis_source_selection = "raw"
        # With only Analysis visible, retain the source that produced it.
        if not choices and self.show_analysis.isChecked():
            retained = candidates.by_key.get(self._analysis_source_key)
            if retained is not None:
                choices = (retained,)
        keys = {trace.key for trace in choices}
        selection = self._analysis_source_selection
        if selection not in keys:
            selection = "auto"
        automatic_key = next(
            (key for key in ("processed", "averaged", "reference", "raw") if key in keys), None
        )
        source_key = automatic_key if selection == "auto" else selection
        automatic_label = (
            f"Automatic · {candidates.by_key[automatic_key].label} [{candidates.by_key[automatic_key].unit}]"
            if automatic_key is not None else "Automatic · no visible spectrum"
        )
        current_items = [
            (self.analysis_source.itemData(i), self.analysis_source.itemText(i))
            for i in range(self.analysis_source.count())
        ]
        desired_items = [("auto", automatic_label)] + [
            (trace.key, f"{trace.label} [{trace.unit}]") for trace in choices
        ]
        self._analysis_source_selection = selection
        if source_key != self._analysis_source_key:
            self._invalidate_analysis_results()
            self._analysis_source_key = source_key
        if current_items != desired_items:
            blocked = self.analysis_source.blockSignals(True)
            self.analysis_source.clear()
            for key, text in desired_items:
                self.analysis_source.addItem(text, userData=key)
            index = self.analysis_source.findData(selection)
            self.analysis_source.setCurrentIndex(max(index, 0))
            self.analysis_source.blockSignals(blocked)
        else:
            idx = self.analysis_source.findData(selection)
            if idx >= 0 and self.analysis_source.currentIndex() != idx:
                blocked = self.analysis_source.blockSignals(True)
                self.analysis_source.setCurrentIndex(idx)
                self.analysis_source.blockSignals(blocked)

    def _refresh_spectrum_display(self, *_args: object, auto_range: bool = False) -> None:
        if not self._ordinary_preview_active():
            return
        operation = str(self.reference_operation.currentData() or "none")
        effective_operation = operation
        if operation != "none" and self._latest_trace is not None:
            try:
                self._validate_display_reference(self._latest_trace.frequencies_hz)
                if self._latest_trace is self._reference_trace:
                    raise ValueError("Reference captured: acquire the next spectrum to display the corrected result.")
            except ValueError as exc:
                message = f"Reference processing unavailable: {exc}"
                self.info.setText(message)
                self._set_analysis_status(message)
                self.correction_controls.summary.setText(message)
                effective_operation = "none"

        # Build candidate state containing all available traces regardless of visibility
        all_candidates_state = build_display_state(
            raw=self._latest_trace,
            averaged=self._averaged_trace,
            reference=self._reference_trace,
            reference_operation=effective_operation,
            visible={"raw": True, "averaged": True, "reference": True, "processed": True},
            frame_id=self._display_revision,
            preferred_key=self._analysis_source_key,
        )
        self._candidate_traces = all_candidates_state.by_key

        visible = {
            "raw": self.show_raw.isChecked(),
            "averaged": self.show_average.isChecked(),
            "reference": self.show_reference.isChecked(),
            "processed": self.show_processed.isChecked(),
        }
        background_enabled = self.cleanup_filters["background"].isChecked()
        if background_enabled:
            visible = {"raw": True, "averaged": False, "reference": False, "processed": False}
        processed = all_candidates_state.by_key.get("processed")
        if processed is not None and processed.unit != "dBm" and self.show_processed.isChecked():
            # Do not put incompatible dBm overlays on a relative/linear axis.
            visible.update({"raw": False, "averaged": False, "reference": False})

        self._sync_analysis_source_combo(all_candidates_state, visible)

        has_processed = "processed" in all_candidates_state.by_key
        self.show_processed.setEnabled(has_processed)
        if not has_processed:
            self.show_processed.setToolTip(
                "Processed trace is unavailable. Capture/load a Reference and select a Reference operation (e.g. Signal − reference)."
            )
        else:
            self.show_processed.setToolTip(
                f"Show the reference operation result ({self.reference_operation.currentText()})."
            )

        has_analysis = bool(self._selected_cleanup_modes()) or self._analysis_parameters.temporal_average_frames > 1
        self.show_analysis.setEnabled(has_analysis and not background_enabled)
        self.overlay_analysis_source.setEnabled(any(mode != "background" for mode in self._selected_cleanup_modes())
                                               or self._analysis_parameters.temporal_average_frames > 1)
        if not has_analysis:
            self.show_analysis.setToolTip(
                "Select one or more filters to show the cleaned trace. With all filters off, the source curve is unchanged."
            )
        elif background_enabled:
            self.show_analysis.setToolTip("The Background result is always displayed while correction is enabled.")
        else:
            self.show_analysis.setToolTip(
                "Show the background signal cleanup/analysis trace derived from the selected source."
            )

        analysis_values: tuple[float, ...] | None = None
        analysis_source_key: str | None = None
        analysis_method: str | None = None
        cleanup = self._cleanup_result
        if cleanup is not None and has_analysis:
            source_key = self._analysis_source_key or all_candidates_state.selected_key
            if source_key in all_candidates_state.by_key:
                source_trace = all_candidates_state.by_key[source_key]
                if len(cleanup.values) == len(source_trace.frequencies_hz):
                    base_is_relative = self.show_processed.isChecked() and processed is not None and processed.unit != "dBm"
                    analysis_is_relative = cleanup.unit != "dBm"
                    has_base_visible = any(visible.values())
                    if background_enabled or not has_base_visible or (base_is_relative == analysis_is_relative):
                        analysis_values = cleanup.values
                        analysis_source_key = source_key
                        analysis_method = cleanup.method
                        display_analysis = background_enabled or self.show_analysis.isChecked()
                        visible[f"analysis:{analysis_source_key}"] = display_analysis
                        if background_enabled:
                            visible.update({key: False for key in ("raw", "averaged", "reference", "processed")})
                            visible[f"correction:{analysis_source_key}"] = self.overlay_analysis_source.isChecked()
                        if display_analysis and not self.overlay_analysis_source.isChecked():
                            visible[source_key] = False
        processing_pending = (has_analysis and (background_enabled or self.show_analysis.isChecked())
                              and analysis_values is None and self._latest_trace is not None)
        if processing_pending:
            # Never substitute an uncorrected trace for a selected processing
            # result. Keep raw available for explicit raw exports and analysis.
            visible = {key: False for key in visible}
        state = build_display_state(
            raw=self._analysis_raw_snapshot if analysis_values is not None and self._analysis_raw_snapshot is not None else self._latest_trace,
            averaged=self._averaged_trace,
            reference=self._reference_trace,
            reference_operation=effective_operation,
            visible=visible,
            frame_id=(self._analysis_source_snapshot.frame_id if analysis_values is not None
                      and self._analysis_source_snapshot is not None else self._display_revision),
            preferred_key=self._analysis_source_key,
            analysis_values=analysis_values,
            analysis_unit=cleanup.unit if cleanup is not None else "dBm",
            analysis_source_key=analysis_source_key,
            analysis_method=analysis_method,
            analysis_input_values=cleanup.input_values if cleanup is not None else None,
            analysis_source_snapshot=self._analysis_source_snapshot,
            analysis_input_provenance=cleanup.input_provenance if cleanup is not None else (),
            analysis_modes=cleanup.applied_modes if cleanup is not None else (),
        )
        view_notes = []
        if any(checkbox.isChecked() for checkbox in self.quick_curves.values()):
            background_w, reference, background_provenance = None, None, ()
            if self._latest_trace is not None:
                if self.quick_curves["background"].isChecked():
                    try:
                        _, profile = self._background_for_filter(self._latest_trace.frequencies_hz)
                        background_w = profile.mean_w
                        background_provenance = (profile.profile_id, profile.content_hash, profile.context_id)
                    except ValueError as exc:
                        view_notes.append(f"Background unavailable: {exc}")
                if self.quick_curves["reference"].isChecked():
                    try:
                        self._validate_display_reference(self._latest_trace.frequencies_hz)
                        if self._latest_trace is self._reference_trace:
                            raise ValueError("Acquire a new spectrum after capturing the reference.")
                        reference = self._reference_trace
                    except ValueError as exc:
                        view_notes.append(f"Reference unavailable: {exc}")
            state = compare_power(self._latest_trace, frame_id=self._display_revision,
                show_raw=self.quick_curves["raw"].isChecked(), background_w=background_w,
                reference=reference, background_provenance=background_provenance)
        state, unit_note = convert_power_view(state, str(self.quick_power_unit.currentData() or "auto"))
        if unit_note:
            view_notes.append(unit_note)
        previous_note = getattr(self, "_comparison_view_note", "")
        self._comparison_view_note = " ".join(view_notes)
        if view_notes or (previous_note and self.info.text() == previous_note):
            self.info.setText(self._comparison_view_note)
        self._display_state = state
        traces = list(state.traces)

        plots = [self.spectrum_plot]
        floating = self._spectrum_window
        if floating is not None and floating.spectrum.frozen:
            floating = None
        if floating is not None:
            plots.append(floating.spectrum)
        active_names = {
            ("Analysis" if trace.key.startswith("analysis:") else "Corrected" if trace.key.startswith("correction:")
             else ("Processed" if trace.key == "processed" else trace.key.capitalize()))
            for trace in traces
        }
        previous_names = self._last_displayed_trace_names
        traces_changed = active_names != previous_names
        self._last_displayed_trace_names = active_names
        for plot in plots:
            for name in previous_names | {"Raw", "Analysis", "Averaged", "Reference", "Processed", "Corrected"}:
                if name not in active_names:
                    plot.clear_trace(name)
        if not traces:
            self.spectrum_host.setCurrentWidget(self.spectrum_empty)
            if processing_pending:
                problem = self._analysis_error or self._background_config_error
                title = "Processing unavailable" if problem else "Preparing processed spectrum"
                detail = problem or self._analysis_wait_reason or "Applying the selected correction and filters to the latest spectrum…"
            else:
                title = "Ready for a spectrum"
                detail = "Acquire once or Start Live. Then choose a correction and configure the filters above."
            if view_notes:
                title, detail = "Comparison unavailable", " ".join(view_notes)
            self.spectrum_empty_heading.setText(title)
            self.spectrum_empty_text.setText(detail)
            for plot in plots:
                plot.clear_holds()
                plot.set_title(title)
            if floating is not None:
                floating.status.setText(detail)
            return
        self.spectrum_host.setCurrentWidget(self.spectrum_plot)
        displayed = 0
        legend_order = []
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        palette = plot_theme(tokens)
        colors = {
            "raw": palette.measurement,
            "averaged": tokens.success,
            "reference": palette.reference,
            "processed": tokens.accent,
            "background_difference": tokens.success,
            "reference_difference": palette.reference,
        }
        if traces[-1].unit != self._active_spectrum_unit:
            for plot in plots:
                plot.clear_holds()
                plot.delta_marker.hide()
        if traces[-1].unit in {"W", "dBm"}:
            for plot in plots:
                plot.plot.setLogMode(x=False, y=False)
        for trace in traces:
            name = ("Analysis" if trace.key.startswith("analysis:") else "Corrected" if trace.key.startswith("correction:")
                    else trace.key.capitalize())
            if trace.key == "processed":
                name = "Processed"
            legend_order.append(name)
            values = trace.values
            color = (palette.measurement if trace.key.startswith("analysis:") else
                     palette.reference if trace.key.startswith("correction:") or
                     (analysis_values is not None and trace.key == analysis_source_key) else
                     colors.get(trace.key, palette.measurement))
            for plot in plots:
                if isinstance(plot, SpectrumWorkbench):
                    plot.set_trace_unit(name, trace.unit)
                plot.set_trace(
                    name,
                    trace.frequencies_hz,
                    values,
                    color=color,
                    legend_label=f"{trace.label} [{trace.unit}]",
                    primary=trace.key == state.primary_key,
                )
            displayed += sum(
                math.isfinite(frequency) and math.isfinite(value)
                for frequency, value in zip(trace.frequencies_hz, values, strict=True)
            )
        if displayed == 0:
            self.spectrum_empty_heading.setText("No finite spectrum points")
            self.spectrum_empty_text.setText("No finite spectrum points. A non-positive linear power difference is undefined in dBm.")
            self.spectrum_host.setCurrentWidget(self.spectrum_empty)
            self.spectrum_ranges.set_unit(traces[-1].unit)
            self._active_spectrum_unit = traces[-1].unit
            self.info.setText("No finite spectrum points are available for display.")
            if floating is not None:
                floating.status.setText("No finite spectrum points are available.")
            return
        active_unit = traces[-1].unit
        primary = state.by_key.get(state.primary_key)
        title = primary.label if primary is not None else "Current spectrum"
        if view_notes:
            title += " · " + " ".join(view_notes)
        unit_changed = active_unit != self._active_spectrum_unit
        self.spectrum_ranges.set_unit(active_unit)
        for plot in plots:
            plot.set_legend_order(legend_order)
            plot.set_title(title)
            plot.set_labels(
                x="Frequency",
                x_unit="Hz",
                y=("Power / residual" if "raw" in state.by_key else "Signed residual") if active_unit == "W" else "Relative power" if active_unit in {"dB", "linear ratio"} else "Amplitude",
                y_unit=active_unit,
            )
            plot._csv_value_column = "signed_power_w" if active_unit == "W" else "value"
            if unit_changed or auto_range or traces_changed:
                plot.auto_range()
        self._active_spectrum_unit = active_unit
        if floating is not None:
            floating.status.setText(
                f"Mirroring {displayed:,} finite value(s) from the completed trace."
            )

    def _error(self, operation: str, error: str) -> None:
        if operation in {"fetch_trace", "fetch_current_trace", "fetch_current_trace_fast", "single_sweep", "acquire_fresh_trace"}:
            self._discard_cancelled_average_frame = False
        if operation.startswith("read_background_filter_configuration:"):
            pending = self._background_config_pending
            if pending is not None and operation == pending[0]:
                self._background_config_timer.stop()
                self._background_config_pending = None
                self._background_config_snapshot = None
                self._background_config_error = error
                self._invalidate_analysis_results()
                self._invalidate_spectrogram_filters()
                self._analysis_error = error
                self._set_analysis_status(f"Background unavailable: {error}")
                self._update_correction_summary()
                self._refresh_spectrum_display()
                self._refresh_spectrogram_display()
            return
        if self.correction_workspace.handle_error(operation, error):
            self._set_page_state(AnritsuPageState.ERROR)
            self.banner.show_message(f"Quantitative acquisition failed: {error}", severity="error")
            return
        if (
            operation in {"fetch_current_trace", "fetch_current_trace_fast", "acquire_fresh_trace", "single_sweep"}
            and "-999.0 unmeasured/error sentinel" in error
        ):
            # A Continuous measurement can briefly expose Anritsu's
            # documented "not measured yet" marker after reconfiguration.
            # Retry only the passive TRAC1 read; never repaint the previous
            # frame as though it were newly acquired.
            self._fetch_pending = False
            self._fetch_started_monotonic = None
            self._stale_frame_count += 1
            if self._timer.isActive():
                self.info.setText(
                    "Anritsu has not completed a valid current spectrum yet; "
                    "Live will retry on the next interval."
                )
                self.status.emit("Anritsu Live skipped an unmeasured -999 trace")
                return
            deadline = self._manual_trace_deadline_monotonic
            if deadline is not None and time.monotonic() < deadline:
                self.info.setText(
                    "The new Anritsu spectrum is still being measured; "
                    "waiting for a complete current trace..."
                )
                self.status.emit(
                    "Anritsu current trace is not measured yet; retry queued"
                )
                self._set_page_state(AnritsuPageState.IDLE)
                QTimer.singleShot(
                    self.refresh.value(), self._retry_manual_current_trace
                )
                return
            self._manual_trace_deadline_monotonic = None
        if operation == "configure":
            self._pending_after_spectrum_configuration = None
            self._fetch_pending = False
            self._fetch_started_monotonic = None
        if operation in {"read_advanced_spectrum", "configure_advanced_spectrum"}:
            if operation == "read_advanced_spectrum" and self._save_readback_pending:
                self._save_readback_pending = False
                self._set_page_state(AnritsuPageState.ERROR)
                self.banner.show_message(
                    "Advanced readback was unavailable; the basic Start/Stop, "
                    "reference-level and point-count values can still be saved.",
                    severity="warning",
                )
                self._confirm_settings_readback()
                return
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
            "set_signal_generator_output",
        }:
            if operation in {"configure_signal_generator", "set_signal_generator_output"}:
                self._sg_configured = False
            if operation == "set_signal_generator_output":
                self._set_sg_output_state(None)
            self._apply_page_state()
            self.banner.show_message(
                f"Anritsu signal-generator operation {operation!r} failed: {error}",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu {operation} failed: {error}")
            return
        if operation in {"fetch_trace", "fetch_current_trace", "fetch_current_trace_fast", "single_sweep", "acquire_fresh_trace"}:
            self._fetch_pending = False
            self._manual_trace_deadline_monotonic = None
            if self._averaging_active:
                self._finish_temporal_averaging(resume_live=False)
                self.info.setText(f"Averaging stopped: {error}")
        if operation in {
            "read_configuration", "configure", "start_live", "stop_live", "fetch_trace", "fetch_current_trace",
            "fetch_current_trace_fast", "acquire_fresh_trace", "single_sweep", "emergency_off",
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

    def shutdown_analysis(self) -> bool:
        """Keep ownership until both analysis threads have actually stopped."""
        self._timer.stop()
        self._background_config_timer.stop()
        stopped = (
            self._analysis_controller.close(),
            self._spectrogram_analysis_controller.close(),
        )
        return all(stopped)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Release background workers and HDF5 handles before page teardown."""

        if not self.prepare_manual_archive_shutdown():
            self.status.emit("Waiting for manual spectrum archive I/O to finish. Retry close when saving ends.")
            event.ignore()
            return
        if not self.shutdown_analysis():
            self.status.emit("Spectrum analysis is still stopping. Retry close when processing ends.")
            event.ignore()
            return
        if self._background_assistant is not None:
            self._background_assistant.reject()
        self._background_config_timer.stop()
        self._background_config_pending = None
        self._timer.stop()
        self.reference_dialog.close()
        self.recording_dialog.close()
        if self._processing_quality_dialog is not None:
            self._processing_quality_dialog.close()
        if self._scale_dialog is not None:
            self._scale_dialog.close()
        if not self.correction_workspace.shutdown():
            self.status.emit("Waiting for spectrum archives and analysis workers to finish. Retry close when processing ends.")
            event.ignore()
            return
        for window in tuple(self._additional_peak_trackers):
            window.close()
        if self._peak_tracking_window is not None:
            self._peak_tracking_window.close()
        super().closeEvent(event)


_SWEEPABLE_PARAMETERS = SWEEPABLE_PARAMETERS
_sweep_default = sweep_default
