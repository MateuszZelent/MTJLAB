"""Manual-control UI for the Anritsu MS2830A module."""

# ruff: noqa: F401
from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QSplitter, QSpinBox,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel, CardWidget, CheckBox, ComboBox, LineEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, SpinBox, StrongBodyLabel, TitleLabel, isDarkTheme,
)

from app.devices.anritsu_ms2830a import (
    ANRITSU_PREAMPLIFIER_OPTIONS, AdvancedSpectrumConfig, AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot, ReferenceSpectrum, SignalGeneratorConfig,
    SignalGeneratorSnapshot, SpectrumConfig, SpectrumTrace, frequency_option_for,
)
from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT, DIMENSION_DB, DIMENSION_DBM, DIMENSION_FREQUENCY, DIMENSION_TIME,
    DIMENSION_VOLTAGE, format_quantity_auto, parse_quantity,
)
from app.recipes.parameter_registry import SWEEPABLE_PARAMETERS, sweep_default
from app.safety.anritsu import (
    ANRITSU_REFERENCE_LEVEL_MAX_DBM,
    ANRITSU_REFERENCE_LEVEL_MIN_DBM,
    ANRITSU_SWEEP_POINT_COUNTS,
)
from app.settings.models import StationSettings
from app.spectrum import (
    LinearPowerAverager,
    SpectrumCleanupResult,
    SpectrumPeak,
    apply_reference_operation,
    detect_spectrum_peaks,
    frequency_grids_match,
)
from app.storage import ReferenceHdf5Store
from app.ui.common import line_edit as _line
from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.dialogs import StationDialog, StationMessageBox as QMessageBox
from app.ui.widgets import FluentTabView, LimitField, NotificationBanner, SpectrumPlotWidget
from app.ui.workers import DeviceController

from .peak_analysis import PeakTableDialog, PeakTrackingWindow
from .analysis_worker import (
    SpectrumAnalysisController,
    SpectrumAnalysisOutcome,
    SpectrumAnalysisRequest,
)


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
        detector_index = self.detector.findData(snapshot.detector)
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

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def clear(self) -> None:
        self._frequencies_hz = None
        self._rows.clear()

    def append(self, trace: SpectrumTrace, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else float(now)
        frequencies = np.asarray(trace.frequencies_hz, dtype=np.float64)
        powers = np.asarray(trace.powers_dbm, dtype=np.float32)
        if (
            frequencies.ndim != 1
            or powers.ndim != 1
            or frequencies.size == 0
            or frequencies.size != powers.size
        ):
            return
        if (
            self._frequencies_hz is None
            or self._frequencies_hz.shape != frequencies.shape
            or not np.array_equal(self._frequencies_hz, frequencies)
        ):
            self.clear()
            self._frequencies_hz = frequencies.copy()
        if self._rows and timestamp - self._rows[-1][0] < self.MIN_ROW_INTERVAL_S:
            self._rows[-1] = (timestamp, powers.copy())
        else:
            self._rows.append((timestamp, powers.copy()))
        cutoff = timestamp - self.MAX_WINDOW_S
        while self._rows and self._rows[0][0] < cutoff:
            self._rows.popleft()

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
            tuple(float(value) for value in row)
            for _stamp, row in tuple(self._rows)[-count:]
        )


class _AnritsuSpectrogramWidget(QWidget):
    """Theme-aware rolling frequency/time heatmap."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.plot = pg.PlotWidget(self)
        self.plot.setObjectName("anritsuSpectrogramPlot")
        self.plot.setMenuEnabled(True)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Time before latest frame", units="s")
        self.image = pg.ImageItem(axisOrder="row-major")
        self.plot.addItem(self.image)
        self.colormap = pg.colormap.get("viridis")
        self.color_bar = pg.ColorBarItem(
            interactive=False,
            values=(-120.0, 0.0),
            colorMap=self.colormap,
            label="Amplitude (dBm)",
        )
        self.color_bar.setImageItem(self.image, insert_in=self.plot.getPlotItem())
        layout.addWidget(self.plot, 1)
        self._apply_theme()

    def set_data(
        self,
        frequencies_hz: np.ndarray,
        elapsed_s: np.ndarray,
        matrix: np.ndarray,
        *,
        unit: str,
    ) -> None:
        finite = matrix[np.isfinite(matrix)]
        if finite.size == 0:
            self.clear()
            return
        low, high = np.nanpercentile(finite, (2.0, 98.0))
        if not math.isfinite(float(low)) or not math.isfinite(float(high)):
            self.clear()
            return
        if math.isclose(float(low), float(high), rel_tol=0.0, abs_tol=1e-12):
            low, high = float(low) - 1.0, float(high) + 1.0
        x_min = float(frequencies_hz[0])
        x_max = float(frequencies_hz[-1])
        x_width = max(abs(x_max - x_min), 1.0)
        y_min = float(elapsed_s[0]) if elapsed_s.size > 1 else -1.0
        y_max = max(float(elapsed_s[-1]), 0.0)
        y_height = max(y_max - y_min, 1.0)
        self.image.setImage(matrix, autoLevels=False, levels=(float(low), float(high)))
        self.image.setRect(QRectF(min(x_min, x_max), y_min, x_width, y_height))
        self.color_bar.setLevels((float(low), float(high)))
        self.color_bar.axis.setLabel(f"Amplitude ({unit})")

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
        super().__init__(parent)
        self.setWindowTitle("Anritsu MS2830A — floating spectrogram")
        self.setObjectName("anritsuSpectrogramWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(820, 560)
        self.setMinimumSize(520, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Live spectrogram"))
        header.addStretch(1)
        layout.addLayout(header)
        controls = QGridLayout()
        controls.setHorizontalSpacing(8)
        controls.setVerticalSpacing(6)
        self.source = ComboBox(self)
        self.source.addItem("Raw", userData="raw")
        self.source.addItem("Processed (Raw − reference)", userData="processed")
        self.source.setToolTip(
            "Switch between untouched TRAC1 frames and local Raw − reference processing."
        )
        self.window_span = ComboBox(self)
        for seconds in (30, 60, 90, 120):
            self.window_span.addItem(f"{seconds} s", userData=seconds)
        self.window_span.setToolTip(
            "Choose the rolling time window retained in the spectrogram."
        )
        self.reset_view = PushButton("Reset view", self)
        self.reset_view.setToolTip("Show the complete frequency and time range.")
        controls.addWidget(BodyLabel("Trace"), 0, 0)
        controls.addWidget(self.source, 0, 1, 1, 2)
        controls.addWidget(BodyLabel("Window"), 1, 0)
        controls.addWidget(self.window_span, 1, 1)
        controls.addWidget(self.reset_view, 1, 2)
        controls.setColumnStretch(1, 1)
        layout.addLayout(controls)
        self.spectrogram = _AnritsuSpectrogramWidget(self)
        layout.addWidget(self.spectrogram, 1)
        self.status = CaptionLabel("Waiting for completed Live frames.", self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
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
        super().__init__(parent)
        self.setWindowTitle("Anritsu MS2830A — floating spectrum")
        self.setObjectName("anritsuSpectrumWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(920, 620)
        self.setMinimumSize(580, 400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Current spectrum", self))
        header.addStretch(1)
        layout.addLayout(header)
        self.spectrum = SpectrumPlotWidget(legend=True, parent=self)
        self.spectrum.set_title("Waiting for a completed spectrum")
        self.spectrum.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit="dBm"
        )
        layout.addWidget(self.spectrum, 1)
        self.status = CaptionLabel(
            "This window mirrors completed traces; it does not start acquisition.",
            self,
        )
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()


class AnritsuPage(QWidget):
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
        self._live_transition_pending = False
        self._pending_after_spectrum_configuration: str | None = None
        self._latest_trace: SpectrumTrace | None = None
        self._averaged_trace: SpectrumTrace | None = None
        self._reference_trace: SpectrumTrace | None = None
        self._reference_spectrum: ReferenceSpectrum | None = None
        self._spectrogram_buffer = _SpectrogramBuffer()
        self._spectrogram_window: _AnritsuSpectrogramWindow | None = None
        self._spectrum_window: _AnritsuSpectrumWindow | None = None
        self._cleanup_result: SpectrumCleanupResult | None = None
        self._detected_peaks: tuple[SpectrumPeak, ...] = ()
        self._last_peak_analysis_monotonic: float | None = None
        self._analysis_generation = 0
        self._analysis_controller = SpectrumAnalysisController(self)
        self._analysis_controller.result.connect(self._analysis_completed)
        self._analysis_controller.error.connect(self._analysis_failed)
        self._peak_table_dialog: PeakTableDialog | None = None
        self._peak_tracking_window: PeakTrackingWindow | None = None
        self._tracked_peak_target_hz: float | None = None
        self._tracked_peak_gate_hz: float | None = None
        self._tracking_started_monotonic: float | None = None
        self._pending_reference_kind: str | None = None
        self._device_idn = ""
        self._last_configuration: AnritsuConfigurationSnapshot | None = None
        self._last_advanced_configuration: AdvancedSpectrumSnapshot | None = None
        self._save_readback_pending = False
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
        self._sg_output_known = False
        self._sg_configured = False
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
        title_row.setContentsMargins(20, 16, 20, 16)
        title = TitleLabel("Anritsu MS2830A — Spectrum / Live")
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
            "Show detected Anritsu hardware options and documented operating limits."
        )
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
        self._limit_fields = self.configuration_panel.limit_fields
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
        setup_layout.addLayout(refresh_form)
        self.hardware_option_info = BodyLabel()
        self.hardware_range_info = BodyLabel()
        self.hardware_option_info.hide()
        self.hardware_range_info.hide()
        self._hardware_details_text = ""
        self._update_anritsu_hardware_limits(())
        controls = QGridLayout()
        controls.setSpacing(6)
        self.read_configuration = PushButton("Read from instrument")
        self.read_and_save_configuration = PushButton("Read all & save defaults")
        self.configure_button = PrimaryPushButton("Apply configuration")
        self.single = PushButton("Acquire fresh spectrum")
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
        controls.addWidget(self.configure_button, 1, 0)
        controls.addWidget(self.single, 1, 1)
        controls.addWidget(self.live, 2, 0)
        controls.addWidget(self.abort_button, 2, 1)
        setup_layout.addLayout(controls)
        self.processing_card = CardWidget(left_panel)
        self.processing_card.setObjectName("anritsuProcessingCard")
        self.processing_card.setProperty("stationSurface", "card")
        processing_layout = QGridLayout(self.processing_card)
        processing_layout.setContentsMargins(20, 16, 20, 16)
        processing_title = StrongBodyLabel("Averaging and reference processing")
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
        reference_title = StrongBodyLabel("Reference")
        reference_title.setObjectName("subsectionTitle")
        processing_layout.addWidget(reference_title, 4, 0, 1, 2)
        self.reference_status = CaptionLabel("No reference")
        self.reference_status.setObjectName("muted")
        self.reference_status.setWordWrap(True)
        processing_layout.addWidget(self.reference_status, 5, 0, 1, 2)
        self.acquire_single_reference = PushButton("Acquire 1× reference")
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
        self.reference_operation = ComboBox(self)
        self.reference_operation.addItem("No processing", userData="none")
        self.reference_operation.addItem("Signal − reference [dB]", userData="difference_db")
        self.reference_operation.addItem("Signal ÷ reference [linear ratio]", userData="ratio_linear")
        self.reference_operation.addItem("Signal + reference [linear power]", userData="add_power")
        self.reference_operation.addItem("Signal − reference [linear power]", userData="subtract_power")
        self.reference_operation.addItem("Signal × reference [linear mW²]", userData="multiply_linear")
        processing_layout.addWidget(self.acquire_single_reference, 6, 0)
        processing_layout.addWidget(self.use_current_reference, 6, 1)
        processing_layout.addWidget(self.capture_reference, 7, 0)
        processing_layout.addWidget(self.clear_reference, 7, 1)
        processing_layout.addWidget(self.load_reference, 8, 0)
        processing_layout.addWidget(self.save_reference, 8, 1)
        processing_layout.addWidget(BodyLabel("Reference operation"), 9, 0)
        processing_layout.addWidget(self.reference_operation, 9, 1)
        self.show_raw = CheckBox("Raw")
        self.show_raw.setChecked(True)
        self.show_average = CheckBox("Averaged")
        self.show_reference = CheckBox("Reference")
        self.show_processed = CheckBox("Processed")
        trace_toggles = QHBoxLayout()
        trace_toggles.setSpacing(10)
        for checkbox in (self.show_raw, self.show_average, self.show_reference, self.show_processed):
            trace_toggles.addWidget(checkbox)
        trace_toggles.addStretch(1)
        processing_layout.addLayout(trace_toggles, 10, 0, 1, 2)
        left_layout.addWidget(self.processing_card)
        left_layout.addStretch(1)
        self.spectrum_plot = SpectrumPlotWidget(legend=True)
        self.spectrum_plot.setProperty("stationSurface", "raised")
        self.spectrum_plot.set_title("Current spectrum")
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit="dBm"
        )
        self.spectrum_plot.setMinimumHeight(300)
        self.spectrum_plot.status_changed.connect(self.status.emit)
        self.info = CaptionLabel("Live stopped. Each frame is a complete trace, not a push stream.")
        self.info.setObjectName("muted")
        self.analysis_tabs = FluentTabView(self)
        self.analysis_tabs.setObjectName("anritsuAnalysisTabs")
        current_spectrum_tab = QWidget(self.analysis_tabs)
        current_spectrum_layout = QVBoxLayout(current_spectrum_tab)
        current_spectrum_layout.setContentsMargins(0, 0, 0, 0)
        current_spectrum_layout.setSpacing(4)
        self.signal_analysis_card = CardWidget(current_spectrum_tab)
        self.signal_analysis_card.setObjectName("anritsuSignalAnalysisCard")
        self.signal_analysis_card.setProperty("stationSurface", "card")
        analysis_controls = QGridLayout(self.signal_analysis_card)
        analysis_controls.setContentsMargins(12, 8, 12, 8)
        analysis_controls.setHorizontalSpacing(8)
        analysis_controls.setVerticalSpacing(6)
        analysis_title = StrongBodyLabel("Automatic signal analysis")
        analysis_title.setObjectName("sectionTitle")
        analysis_controls.addWidget(analysis_title, 0, 0)
        self.cleanup_mode = ComboBox(self.signal_analysis_card)
        self.cleanup_mode.addItem("Raw · no cleanup", userData="raw")
        self.cleanup_mode.addItem(
            "Edge-preserving denoise", userData="denoise"
        )
        self.cleanup_mode.addItem(
            "Stationary-line rejection", userData="emi_reject"
        )
        self.cleanup_mode.addItem(
            "Auto clean · denoise + line rejection", userData="auto_clean"
        )
        self.cleanup_mode.setToolTip(
            "Choose local display processing. Raw remains untouched. Stationary-line "
            "rejection is conservative and cannot prove that a stable carrier is EMI."
        )
        analysis_controls.addWidget(self.cleanup_mode, 0, 1, 1, 2)
        self.auto_peak_detection = CheckBox("Auto-detect peaks")
        self.auto_peak_detection.setChecked(True)
        self.highlight_peaks = CheckBox("Highlight peaks")
        self.highlight_peaks.setChecked(True)
        self.analyze_peaks = PushButton("Analyze now", self.signal_analysis_card)
        self.open_peak_table = PrimaryPushButton(
            "Peak table…", self.signal_analysis_card
        )
        self.open_floating_spectrum = PushButton(
            "Open floating spectrum", self.signal_analysis_card
        )
        self.open_floating_spectrum.setToolTip(
            "Open an always-on-top mirror of completed spectrum traces. "
            "It never starts acquisition or changes analyser settings."
        )
        self.open_floating_spectrum.setAccessibleName("Open floating spectrum")
        analysis_controls.addWidget(self.auto_peak_detection, 1, 0)
        analysis_controls.addWidget(self.highlight_peaks, 1, 1)
        analysis_controls.addWidget(self.analyze_peaks, 1, 2)
        analysis_controls.addWidget(self.open_peak_table, 1, 3)
        analysis_controls.addWidget(self.open_floating_spectrum, 0, 3)
        self.analysis_status = CaptionLabel(
            "Waiting for a completed spectrum.", self.signal_analysis_card
        )
        self.analysis_status.setObjectName("muted")
        self.analysis_status.setWordWrap(True)
        analysis_controls.addWidget(self.analysis_status, 2, 0, 1, 4)
        analysis_controls.setColumnStretch(1, 1)
        current_spectrum_layout.addWidget(self.signal_analysis_card)
        current_spectrum_layout.addWidget(self.spectrum_plot, 1)
        current_spectrum_layout.addWidget(self.info)
        self.analysis_tabs.addTab(current_spectrum_tab, "Current spectrum")

        spectrogram_tab = QWidget(self.analysis_tabs)
        spectrogram_layout = QVBoxLayout(spectrogram_tab)
        spectrogram_layout.setContentsMargins(0, 0, 0, 0)
        spectrogram_layout.setSpacing(6)
        spectrogram_controls = QGridLayout()
        spectrogram_controls.setHorizontalSpacing(6)
        spectrogram_controls.setVerticalSpacing(6)
        self.spectrogram_source = ComboBox(self)
        self.spectrogram_source.addItem("Raw", userData="raw")
        self.spectrogram_source.addItem(
            "Processed (Raw − reference)", userData="processed"
        )
        self.spectrogram_source.setToolTip(
            "Raw shows untouched completed TRAC1 frames. Processed subtracts "
            "the compatible captured reference locally without another VISA request."
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
        self.spectrogram_plot.setMinimumHeight(300)
        spectrogram_layout.addWidget(self.spectrogram_plot, 1)
        self.spectrogram_status = CaptionLabel(
            "Start Live to accumulate a rolling spectrogram.", self
        )
        self.spectrogram_status.setObjectName("muted")
        self.spectrogram_status.setWordWrap(True)
        spectrogram_layout.addWidget(self.spectrogram_status)
        self.analysis_tabs.addTab(spectrogram_tab, "Spectrogram")
        right_layout.addWidget(self.analysis_tabs, 1)
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
        self.workspace_splitter.setSizes([680, 1100])
        self._workspace_compact: bool | None = None
        self.mode_tabs = FluentTabView(self)
        self.mode_tabs.setObjectName("anritsuModeTabs")
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
        layout.addWidget(self.mode_tabs, 1)
        self.read_configuration.clicked.connect(self.read_configuration_from_instrument)
        self.read_and_save_configuration.clicked.connect(
            self.read_and_save_configuration_from_instrument
        )
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
        self.spectrogram_source.currentIndexChanged.connect(
            self._spectrogram_controls_changed
        )
        self.spectrogram_window_span.currentIndexChanged.connect(
            self._spectrogram_controls_changed
        )
        self.spectrogram_reset_view.clicked.connect(self.spectrogram_plot.reset_view)
        self.open_spectrogram_window.clicked.connect(
            self._open_spectrogram_window
        )
        self.open_floating_spectrum.clicked.connect(self._open_spectrum_window)
        self.cleanup_mode.currentIndexChanged.connect(
            self._signal_analysis_controls_changed
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
            self.single: "Trigger one qualified analyser sweep, wait for completion, then read a fresh TRAC1 spectrum directly from the instrument.",
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

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        compact = event.size().width() < 900
        if self._workspace_compact == compact:
            return
        self._workspace_compact = compact
        self.workspace_splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        self.workspace_splitter.setSizes(
            [1_050, 620] if compact else [680, 1_100]
        )

    def _build_signal_generator_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(18, 14, 18, 14)
        heading = StrongBodyLabel("Optional vector signal generator")
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)
        explanation = BodyLabel(
            "This panel is shown only when *OPT? reports option 020/120/021/121. "
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
        dialog = StationDialog(self)
        dialog.setWindowTitle("Anritsu advanced Spectrum settings")
        dialog.setModal(False)
        dialog.resize(620, 470)
        layout = QVBoxLayout(dialog)
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

        self.advanced_configuration_panel = AnritsuAdvancedSpectrumPanel(dialog)
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
        self.advanced_read_button = PushButton("Read from instrument", dialog)
        self.advanced_apply_button = PrimaryPushButton("Apply and verify", dialog)
        close_button = PushButton("Close", dialog)
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
        live = self._page_state == AnritsuPageState.LIVE
        averaging = self._page_state in {
            AnritsuPageState.AVERAGING_SIGNAL,
            AnritsuPageState.AVERAGING_REFERENCE,
        }
        connected = self._page_state != AnritsuPageState.DISCONNECTED
        self.read_configuration.setEnabled(idle)
        self.read_and_save_configuration.setEnabled(idle)
        self.configuration_panel.setEnabled(idle)
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
        self._show_trace(trace, update_controls=False)
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
        self.execution_badge.setVisible(controlled)
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
            "Configured safety badges above may intentionally be stricter."
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

    def _spectrum_config_from_form(self) -> SpectrumConfig:
        start_hz, stop_hz = self._spectrum_frequency_bounds()
        return SpectrumConfig(
            start_hz=start_hz,
            stop_hz=stop_hz,
            reference_level_dbm=parse_quantity(
                self.reference.text(), DIMENSION_DBM
            ).si_value,
            points=int(self.points.currentData()),
        )

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
        if self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
            return
        if not self._single_sweep_configured:
            QMessageBox.warning(
                self,
                "Fresh spectrum",
                "A fresh spectrum requires the qualified single-sweep protocol. "
                "Enable the qualified protocol in the Anritsu acquisition profile "
                "or use Live acquisition.",
            )
            return
        if self._fetch_pending:
            return
        self._fetch_pending = True
        self._fetch_started_monotonic = time.monotonic()
        self.info.setText("Applying and verifying spectrum settings...")
        self.status.emit("Anritsu spectrum configuration requested before acquisition")
        self._set_page_state(AnritsuPageState.CONFIGURING)
        if not self._configure_from_form(then="single_sweep"):
            self._fetch_pending = False
            self._fetch_started_monotonic = None
            self._set_page_state(AnritsuPageState.ERROR)

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
        self._set_page_state(AnritsuPageState.CONFIGURING)
        self._timer.setInterval(self.refresh.value())
        # Live owns continuous acquisition for its lifetime.  The adapter
        # remembers the previous INIT:CONT state and restores it on Stop.
        # Without this, polling can repeatedly read a frozen TRAC1 buffer.
        if not self._configure_from_form(then="start_live"):
            self._live_transition_pending = False
            self.live.setText("Start Live")
            self._set_live_indicator("off")
            self._set_page_state(AnritsuPageState.ERROR)

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
            f"Anritsu temporal averaging started: {label}, 0 / {target}"
        )
        self._request_next_average_frame()

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
        if self._resume_live_after_averaging:
            # Continuous Live acquisition remains active while the timer is
            # paused, so this reads the next completed hardware frame.
            self._request_trace()
            return
        if not self._single_sweep_configured:
            self._finish_temporal_averaging(resume_live=False)
            QMessageBox.warning(
                self,
                "Temporal averaging",
                "Averaging outside Live requires the qualified single-sweep protocol.",
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

        if self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
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
            self._spectrogram_buffer.clear()
            self._refresh_spectrogram_display()
            self._set_page_state(AnritsuPageState.IDLE)
        if operation == "read_configuration" and isinstance(result, AnritsuConfigurationSnapshot):
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
            pending = self._pending_after_spectrum_configuration
            self._pending_after_spectrum_configuration = None
            self._result("read_configuration", result)
            self.banner.show_message(
                "Spectrum settings applied to Anritsu and verified by SCPI readback.",
                severity="success",
            )
            self.status.emit("Anritsu configured and verified by SCPI readback")
            if pending == "single_sweep":
                self.info.setText("Acquiring one fresh spectrum from the instrument...")
                self.status.emit("Anritsu fresh single-sweep acquisition started")
                self._controller.call("single_sweep", "TRAC1")
            elif pending == "start_live":
                self.live.setText("Starting...")
                self._set_page_state(AnritsuPageState.STARTING_LIVE)
                # Live owns continuous acquisition for its lifetime. The adapter
                # restores the previous INIT:CONT state when Live stops.
                self._controller.call("start_live", True)
            else:
                self._set_page_state(AnritsuPageState.IDLE)
        elif operation == "start_live" and isinstance(result, AnritsuConfigurationSnapshot):
            self._live_transition_pending = False
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
            mode = "continuous sweep with completed-trace polling"
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
                if not self._timer.isActive():
                    self._set_page_state(AnritsuPageState.IDLE)
                self._show_trace(result)

    def _show_trace(
        self, trace: SpectrumTrace, *, update_controls: bool = True
    ) -> None:
        self._latest_trace = trace
        self._spectrogram_buffer.append(trace)
        # Raw Live must repaint immediately.  CPU cleanup/peak analysis is
        # deliberately asynchronous and may coalesce frames; making repaint
        # wait for that worker can leave the visible trace frozen indefinitely
        # while newer analysis requests keep superseding older generations.
        self._cleanup_result = None
        self._refresh_spectrum_display()
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
            signature = hash(trace.powers_dbm)
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
                live_detail += (
                    f" • requested {self.refresh.value()} ms"
                    f" • effective {effective_ms:.0f} ms"
                    f" • coalesced {self._coalesced_timer_ticks}"
                )
        self.info.setText(
            f"{len(trace.powers_dbm)} points • {trace.acquired_at_utc.isoformat()} • "
            f"max {max(trace.powers_dbm):.4g} dBm{live_detail}"
        )

    def _cleanup_history(self) -> tuple[tuple[float, ...], ...]:
        return self._spectrogram_buffer.recent_power_rows(24)

    def _signal_analysis_controls_changed(self, *_args: object) -> None:
        if self._latest_trace is None:
            self._cleanup_result = None
            self._detected_peaks = ()
            self._sync_peak_markers()
            return
        self._update_signal_analysis(self._latest_trace, force=True)

    def _update_signal_analysis(
        self, trace: SpectrumTrace, *, force: bool = False
    ) -> None:
        mode = str(self.cleanup_mode.currentData() or "raw")
        now = time.monotonic()
        peak_analysis_due = (
            force
            or self._last_peak_analysis_monotonic is None
            or now - self._last_peak_analysis_monotonic >= 1.0
        )
        self._analysis_generation += 1
        request = SpectrumAnalysisRequest(
            generation=self._analysis_generation,
            frequencies_hz=trace.frequencies_hz,
            powers_dbm=trace.powers_dbm,
            mode=mode,
            history_dbm=self._cleanup_history(),
            detect_peaks=(
                self.auto_peak_detection.isChecked() and peak_analysis_due
            ),
        )
        self.analysis_status.setText(
            "Analyzing newest completed frame on the background CPU worker..."
        )
        self._analysis_controller.submit(request)

    def _analysis_completed(self, result: object) -> None:
        if not isinstance(result, SpectrumAnalysisOutcome):
            return
        if result.generation != self._analysis_generation:
            return
        self._cleanup_result = result.cleanup
        if result.peaks is not None:
            self._detected_peaks = result.peaks
            self._last_peak_analysis_monotonic = time.monotonic()
        self._refresh_spectrum_display()
        self._sync_peak_markers()
        self._update_analysis_status()
        if self._peak_table_dialog is not None:
            self._peak_table_dialog.set_peaks(
                self._detected_peaks, method=result.cleanup.method
            )
        self._update_peak_tracking(time.monotonic())

    def _analysis_failed(self, generation: int, message: str) -> None:
        if generation != self._analysis_generation:
            return
        self._cleanup_result = None
        self._detected_peaks = ()
        self.analysis_status.setText(f"Signal analysis unavailable: {message}")
        self._sync_peak_markers()
        self._refresh_spectrum_display()

    def _analysis_values(self) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
        trace = self._latest_trace
        cleanup = self._cleanup_result
        if trace is None or cleanup is None:
            return None
        return trace.frequencies_hz, cleanup.values_dbm

    def _analyze_current_spectrum(self, *, force: bool = False) -> None:
        trace = self._latest_trace
        if trace is None:
            self.analysis_status.setText(
                "Acquire a completed spectrum before detecting peaks."
            )
            return
        self._update_signal_analysis(trace, force=force)

    def _update_analysis_status(self) -> None:
        cleanup = self._cleanup_result
        if cleanup is None:
            return
        interference = len(cleanup.stationary_interference_indices)
        self.analysis_status.setText(
            f"{cleanup.method} · noise σ {cleanup.noise_sigma_db:.3g} dB · "
            f"{len(self._detected_peaks)} peak(s) · "
            f"{interference} stationary-line candidate bin(s)"
        )

    def _sync_peak_markers(self, *_args: object) -> None:
        plots = [self.spectrum_plot]
        if self._spectrum_window is not None:
            plots.append(self._spectrum_window.spectrum)
        if not self.highlight_peaks.isChecked() or not self._detected_peaks:
            for plot in plots:
                plot.clear_peak_markers()
            return
        for plot in plots:
            plot.set_peak_markers(
                [peak.frequency_hz for peak in self._detected_peaks],
                [peak.amplitude_dbm for peak in self._detected_peaks],
            )

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
            method=self._cleanup_result.method if self._cleanup_result else "unavailable",
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
        spacing_hz = float(np.median(np.abs(np.diff(frequencies_hz))))
        width_hz = peak.fit_fwhm_hz or peak.fwhm_hz
        self._tracked_peak_target_hz = peak.frequency_hz
        self._tracked_peak_gate_hz = max(
            spacing_hz * 5.0,
            (width_hz * 2.0 if width_hz is not None else 0.0),
        )
        self._tracking_started_monotonic = time.monotonic()
        if self._peak_tracking_window is None:
            window = PeakTrackingWindow(self)
            window.closed.connect(self._peak_tracking_closed)
            window.history_cleared.connect(self._peak_tracking_history_cleared)
            self._peak_tracking_window = window
        tracking = self._peak_tracking_window
        tracking.clear()
        tracking.append(
            0.0,
            peak,
            source=self._cleanup_result.method if self._cleanup_result else "Raw",
        )
        tracking.show()
        tracking.raise_()
        tracking.activateWindow()
        self.spectrum_plot.select_peak_marker(index)
        self.status.emit(
            f"Anritsu local peak tracking started at {peak.frequency_hz:.12g} Hz"
        )

    def _update_peak_tracking(self, now: float) -> None:
        target_hz = self._tracked_peak_target_hz
        gate_hz = self._tracked_peak_gate_hz
        tracking = self._peak_tracking_window
        data = self._analysis_values()
        if target_hz is None or gate_hz is None or tracking is None or data is None:
            return
        frequencies = np.asarray(data[0], dtype=float)
        values = np.asarray(data[1], dtype=float)
        local = np.abs(frequencies - target_hz) <= gate_hz * 4.0
        if int(np.count_nonzero(local)) >= 5:
            candidate_frequencies = frequencies[local]
            candidate_values = values[local]
        else:
            candidate_frequencies = frequencies
            candidate_values = values
        try:
            candidates = detect_spectrum_peaks(
                candidate_frequencies,
                candidate_values,
                min_snr_db=4.0,
                min_prominence_db=2.0,
                max_peaks=40,
                fit=False,
            )
        except ValueError:
            candidates = ()
        nearest = min(
            candidates,
            key=lambda peak: abs(peak.frequency_hz - target_hz),
            default=None,
        )
        if nearest is None or abs(nearest.frequency_hz - target_hz) > gate_hz:
            tracking.mark_lost(target_hz=target_hz, gate_hz=gate_hz)
            return
        self._tracked_peak_target_hz = nearest.frequency_hz
        started = self._tracking_started_monotonic or now
        tracking.append(
            max(0.0, now - started),
            nearest,
            source=self._cleanup_result.method if self._cleanup_result else "Raw",
        )

    def _peak_tracking_closed(self) -> None:
        tracking = self._peak_tracking_window
        self._peak_tracking_window = None
        self._tracked_peak_target_hz = None
        self._tracked_peak_gate_hz = None
        self._tracking_started_monotonic = None
        if tracking is not None:
            tracking.deleteLater()
        self.status.emit("Anritsu local peak tracking stopped")

    def _peak_tracking_history_cleared(self) -> None:
        self._tracking_started_monotonic = time.monotonic()

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
        self._refresh_spectrogram_display()
        floating.show()
        floating.raise_()
        floating.activateWindow()

    def _spectrogram_window_closed(self) -> None:
        floating = self._spectrogram_window
        self._spectrogram_window = None
        if floating is not None:
            floating.deleteLater()

    def _open_spectrum_window(self) -> None:
        """Show a non-controlling mirror of the current spectrum display."""

        if self._spectrum_window is None:
            floating = _AnritsuSpectrumWindow(self)
            floating.closed.connect(self._spectrum_window_closed)
            floating.spectrum.status_changed.connect(self.status.emit)
            self._spectrum_window = floating
        self._refresh_spectrum_display()
        floating = self._spectrum_window
        floating.show()
        floating.raise_()
        floating.activateWindow()

    def _spectrum_window_closed(self) -> None:
        floating = self._spectrum_window
        self._spectrum_window = None
        if floating is not None:
            floating.deleteLater()

    def _spectrogram_matrix(
        self,
        *,
        source: str,
        window_s: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, str] | None:
        reference = self._reference_trace
        if source == "processed" and reference is None:
            raise ValueError(
                "Processed spectrogram requires a captured or loaded reference."
            )
        snapshot = self._spectrogram_buffer.snapshot(window_s)
        if snapshot is None:
            return None
        frequencies, elapsed, raw = snapshot
        if source == "raw":
            return frequencies, elapsed, raw, "dBm", "Raw"
        assert reference is not None
        if not frequency_grids_match(
            tuple(float(value) for value in frequencies),
            reference.frequencies_hz,
        ):
            raise ValueError(
                "Processed spectrogram unavailable: reference frequency grid differs."
            )
        if self._reference_spectrum is not None:
            reference_level = self._reference_spectrum.reference_level_dbm
            current_level = (
                self._last_configuration.reference_level_dbm
                if self._last_configuration is not None
                else None
            )
            if (
                reference_level is not None
                and current_level is not None
                and not math.isclose(
                    reference_level, current_level, abs_tol=0.005
                )
            ):
                raise ValueError(
                    "Processed spectrogram unavailable: Reference Level differs "
                    f"({current_level:g} dBm current, "
                    f"{reference_level:g} dBm reference)."
                )
            self._validate_reference_acquisition_compatibility(
                self._reference_spectrum
            )
        reference_values = np.asarray(reference.powers_dbm, dtype=np.float32)
        processed = raw - reference_values[np.newaxis, :]
        return frequencies, elapsed, processed, "dB", "Processed (Raw − reference)"

    def _refresh_spectrogram_display(self) -> None:
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
            message = "Start Live to accumulate a rolling spectrogram."
            self.spectrogram_plot.clear()
            self.spectrogram_status.setText(message)
            if self._spectrogram_window is not None:
                self._spectrogram_window.spectrogram.clear()
                self._spectrogram_window.status.setText(message)
            return
        frequencies, elapsed, matrix, unit, label = data
        self.spectrogram_plot.set_data(
            frequencies, elapsed, matrix, unit=unit
        )
        message = (
            f"{label} · {matrix.shape[0]} completed frame(s) · "
            f"{matrix.shape[1]} frequency point(s) · rolling {window_s} s"
        )
        self.spectrogram_status.setText(message)
        if self._spectrogram_window is not None:
            self._spectrogram_window.spectrogram.set_data(
                frequencies, elapsed, matrix, unit=unit
            )
            self._spectrogram_window.status.setText(message)

    def _refresh_spectrum_display(self, *_args: object) -> None:
        traces: list[tuple[str, SpectrumTrace, tuple[float, ...], str, str]] = []
        if self._latest_trace is not None and self.show_raw.isChecked():
            traces.append(("Raw", self._latest_trace, self._latest_trace.powers_dbm, "dBm", "#2196f3"))
        if (
            self._latest_trace is not None
            and self._cleanup_result is not None
            and str(self.cleanup_mode.currentData() or "raw") != "raw"
        ):
            traces.append(
                (
                    "Analysis",
                    self._latest_trace,
                    self._cleanup_result.values_dbm,
                    "dBm",
                    "#00b7c3",
                )
            )
        if self._averaged_trace is not None and self.show_average.isChecked():
            traces.append(("Averaged", self._averaged_trace, self._averaged_trace.powers_dbm, "dBm", "#00a67d"))
        if self._reference_trace is not None and self.show_reference.isChecked():
            traces.append(("Reference", self._reference_trace, self._reference_trace.powers_dbm, "dBm", "#ffb300"))

        operation = str(self.reference_operation.currentData() or "none")
        processed: tuple[float, ...] | None = None
        processed_unit = "dBm"
        signal = self._averaged_trace or self._latest_trace
        if operation != "none" and signal is not None and self._reference_trace is not None:
            if signal is self._reference_trace:
                self.analysis_status.setText(
                    "Reference captured — acquire the next spectrum before displaying Signal − reference."
                )
                operation = "none"
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
                if self.show_processed.isChecked():
                    traces.append(("Processed", signal, processed, processed_unit, "#ab47bc"))

        plots = [self.spectrum_plot]
        floating = self._spectrum_window
        if floating is not None:
            plots.append(floating.spectrum)
        for plot in plots:
            for name in ("Raw", "Analysis", "Averaged", "Reference", "Processed"):
                plot.clear_trace(name)
        if not traces:
            if floating is not None:
                floating.spectrum.set_title("Waiting for a completed spectrum")
                floating.status.setText(
                    "Acquire a spectrum to update this read-only display."
                )
            return
        if processed is not None and processed_unit != "dBm" and self.show_processed.isChecked():
            traces = [item for item in traces if item[0] == "Processed"]
        displayed = 0
        for name, trace, values, _unit, color in traces:
            for plot in plots:
                plot.set_trace(
                    name,
                    trace.frequencies_hz,
                    values,
                    color=color,
                    primary=name in {"Processed", "Analysis", "Averaged", "Raw"},
                )
            displayed += sum(
                math.isfinite(frequency) and math.isfinite(value)
                for frequency, value in zip(trace.frequencies_hz, values, strict=True)
            )
        if displayed == 0:
            self.info.setText("No finite spectrum points are available for display.")
            if floating is not None:
                floating.status.setText("No finite spectrum points are available.")
            return
        active_unit = traces[-1][3]
        for plot in plots:
            plot.set_labels(
                x="Frequency", x_unit="Hz", y="Amplitude", y_unit=active_unit
            )
        if floating is not None:
            floating.spectrum.set_title("Current spectrum")
            floating.status.setText(
                f"Mirroring {displayed:,} finite value(s) from the completed trace."
            )

    def _error(self, operation: str, error: str) -> None:
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
