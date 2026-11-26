"""Spectrum browsing tab for the Results page."""

from __future__ import annotations

import logging
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import prod
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSignalBlocker,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    PushButton,
    ScrollArea,
    Slider,
    SpinBox,
    TogglePushButton,
    TreeView,
)

from app.domain.errors import ExecutionError
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
)
from app.spectrum.spintronic_fits import SpectrumResonanceSummary, fit_spectrum_resonance
from app.storage import (
    Hdf5RunReader,
    StoredPoint,
    StoredReference,
    StoredSpectrum,
    ThatecRun,
    ThatecRunReader,
    ThatecSpectrum,
)
from app.ui.design_system import plot_theme, tokens_for
from app.ui.results.analysis_export import processing_manifest
from app.ui.results.baseline_controls import RecordedBaselineControls, baseline_sample_label
from app.ui.results.batch_processing import SpectrumBatch, plan_spectra
from app.ui.results.data_classifier import find_scalar_rows, find_spectrum_rows
from app.ui.results.filter_choices import FilterComboBox
from app.ui.results.filter_index import FILTER_ABS_TOL, FILTER_REL_TOL, unique_signatures
from app.ui.results.peak_tools import ResultPeakTools
from app.ui.results.processing import (
    ProcessedResultSpectrum,
    RecordedBaselineEvidence,
    recorded_instrument_average,
    recorded_spectrum_label,
)
from app.ui.results.processing_controls import ResultProcessingControls
from app.ui.results.processing_overlay import ResultProcessingOverlay
from app.ui.results.read_session import ResultReadSession
from app.ui.results.series_controls import SweepSeriesControls
from app.ui.results.spectrum_views import (
    SpectrumViewControls,
    SpectrumViewPayload,
    read_baseline_view,
    read_private_view,
    read_public_view,
)
from app.ui.results.spectrum_workbench import ResultSpectrumWorkbench
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublicCheckpoint:
    """A lightweight checkpoint index for public-only THATEC files."""

    index: int
    timestamp_utc: str | None
    values: dict[str, float]
    spectrum_rows: tuple[str, ...]
    setpoints: dict[str, float] | None = None


def read_checkpoint_details(path, point):
    resolved = Hdf5RunReader.point(path, point.index)
    trace = Hdf5RunReader.spectrum(path, point.index, max_points=2_000) if resolved.has_spectrum else None
    return resolved, trace


def build_public_checkpoints(path, run, *, cancelled=None) -> tuple[PublicCheckpoint, ...]:
    """Read scalar metadata and construct public checkpoint records outside Qt."""
    if run is None:
        return ()
    spectrum_rows = find_spectrum_rows(run)
    if not spectrum_rows:
        return ()
    checkpoint_count = max(row.shape[0] for row in spectrum_rows if row.shape)
    scalar_values: dict[str, tuple[object, object]] = {}
    setpoint_labels = set()
    for row in find_scalar_rows(run):
        if cancelled is not None and cancelled():
            raise InterruptedError("Checkpoint indexing cancelled")
        label = row.control_name or row.device_name or row.id
        if label in scalar_values:
            label = f"{label} [{row.id}]"
        if dict(row.definition).get("lab control role") == "setpoint" or "control" in row.function.lower():
            setpoint_labels.add(label)
        try:
            scalar_values[label] = ThatecRunReader.scalar_series(
                path, row.id
            ) if path is not None else ((), ())
        except (ExecutionError, OSError, KeyError, ValueError, TypeError) as exc:
            # A malformed scalar row must not prevent the spectrum rows
            # from being browseable; the row remains visible in the tree.
            _LOGGER.debug("Skipping unreadable public scalar row %s: %s", row.id, exc)
            continue

    checkpoints: list[PublicCheckpoint] = []
    row_ids = tuple(row.id for row in spectrum_rows)
    for index in range(checkpoint_count):
        if index % 256 == 0 and cancelled is not None and cancelled():
            raise InterruptedError("Checkpoint indexing cancelled")
        values: dict[str, float] = {}
        timestamp_utc: str | None = None
        for label, (series, timestamps) in scalar_values.items():
            if index < len(series):
                try:
                    value = float(series[index])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    values[label] = value
            if timestamp_utc is None and index < len(timestamps):
                try:
                    timestamp = float(timestamps[index])
                    if math.isfinite(timestamp):
                        timestamp_utc = datetime.fromtimestamp(
                            timestamp, UTC
                        ).isoformat()
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
        checkpoints.append(
            PublicCheckpoint(index, timestamp_utc, values, row_ids,
                             {key: value for key, value in values.items() if key in setpoint_labels})
        )
    return tuple(checkpoints)


_ALL_PARAMETERS = "__all_parameters__"
_ALL_VALUES = "__all_values__"
_ALL_PARAMETER_SETS = "__all_parameter_sets__"


class CheckpointModel(QAbstractTableModel):
    """Format visible checkpoint cells on demand without per-point Qt items."""

    def __init__(self, tooltip, parent=None):
        super().__init__(parent)
        self.records = ()
        self._tooltip = tooltip

    def set_records(self, records):
        self.beginResetModel()
        self.records = records
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | None = None):
        return 0 if parent is not None and parent.isValid() else len(self.records)

    def columnCount(self, parent: QModelIndex | None = None):
        return 0 if parent is not None and parent.isValid() else 4

    def find_checkpoint(self, checkpoint, record_type):
        for row, record in enumerate(self.records):
            if isinstance(record, record_type) and record.index == checkpoint:
                return self.index(row, 0)
        return QModelIndex()

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ("Point", "State", "UTC time", "Data")[section] if 0 <= section < 4 else None
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.records):
            return None
        point = self.records[index.row()]
        column = index.column()
        private = isinstance(point, StoredPoint)
        state = point.status if private else "public"
        raw_time = point.timestamp_utc or "-"
        if role == Qt.ItemDataRole.UserRole:
            return raw_time if column == 2 else point
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (0, 1):
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 2:
                return f"Exact recorded UTC time:\n{raw_time}"
            if column == 3:
                return self._tooltip(point)
        if role == Qt.ItemDataRole.DecorationRole and column == 1:
            icon = FluentIcon.ACCEPT if state == "ok" else FluentIcon.CANCEL if state in {"faulted", "aborted", "error"} else FluentIcon.INFO if state == "public" else None
            return icon.icon() if icon is not None else None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if column == 0:
            return str(point.index)
        if column == 1:
            return state
        if column == 2:
            return raw_time.replace("T", " ").split(".")[0].split("+")[0]
        if column == 3:
            count = len(point.setpoints.keys() | point.measurements.keys()) if private else len(point.values)
            suffix = (" \u00b7 spectrum" if point.has_spectrum else "") if private else " \u00b7 public spectrum"
            return f"{count} parameters{suffix}"
        return None


class SpectrumResultsTab(QWidget):
    """Browse private Lab Control and public THATEC/PyThat spectra.

    Private files expose individual committed points in ``/points`` and
    ``/spectra``.  Public THATEC/PyThat files instead expose spectral rows in
    ``/measurement``.  Both are read-only and never contact an instrument.
    """

    status_changed = Signal(str)
    batch_requested = Signal(object, object, object, object)
    device_state_changed = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_path: Path | None = None
        self._session = None
        self._preferred_variant = "saved_view"
        self._run: ThatecRun | None = None
        self._stored_points: tuple[StoredPoint, ...] = ()
        self._public_checkpoints: tuple[PublicCheckpoint, ...] = ()
        self._public_spectrum: ThatecSpectrum | None = None
        self._selected_private_point: StoredPoint | None = None
        self._selected_private_spectrum: StoredSpectrum | None = None
        self._selected_reference: StoredReference | None = None
        self._read_pool = QThreadPool(self)
        self._read_pool.setMaxThreadCount(1)
        self._read_pool.setExpiryTimeout(15_000)
        self._read_request = 0
        self._active_read_request = 0
        self._read_tasks: dict[int, ResultReadTask] = {}
        self._read_context: dict[int, tuple[str, object]] = {}
        self.processing_overlay = ResultProcessingOverlay(self)
        self._filter_request_id = 0
        self._filter_pool = QThreadPool(self)
        self._filter_pool.setMaxThreadCount(1)
        self._filter_task = None
        self._filter_context = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(260)
        self._play_timer.timeout.connect(self._advance_playback)
        self._last_fit_summary: SpectrumResonanceSummary | None = None
        self._inspector_visible = True

        # --- Data-First Horizontal Split (Left: Unobstructed Spectrum Plot | Right: Scrollable Inspector) ---
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.workspace_splitter.setObjectName("resultsTabSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)

        # ==================== LEFT STAGE: TELEMETRY + SPECTRUM CANVAS + SCRUBBER ====================
        main_stage = QWidget(self.workspace_splitter)
        stage_layout = QVBoxLayout(main_stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(4)

        # Dedicated single-line command & telemetry strip ABOVE the plot (zero floating bubbles!)
        telemetry_bar = QFrame(main_stage)
        telemetry_bar.setObjectName("resultsTelemetryBar")
        telemetry_layout = QHBoxLayout(telemetry_bar)
        telemetry_layout.setContentsMargins(8, 4, 8, 4)
        telemetry_layout.setSpacing(6)

        self.fit_button = TogglePushButton("Curve Fit", telemetry_bar)
        self.fit_button.setToolTip(
            "Fit a Lorentzian or Gaussian resonance on linear power (SI W·Hz area)."
        )
        self.fit_button.toggled.connect(self._on_fit_toggled)
        telemetry_layout.addWidget(self.fit_button)

        self.fit_model_combo = ComboBox(telemetry_bar)
        self.fit_model_combo.setAccessibleName("Resonance fit profile")
        self.fit_model_combo.addItem("Lorentzian", userData="lorentzian")
        self.fit_model_combo.addItem("Gaussian", userData="gaussian")
        self.fit_model_combo.setFixedWidth(112)
        self.fit_model_combo.hide()
        self.fit_model_combo.currentIndexChanged.connect(lambda _idx: self._update_analysis_overlays())
        telemetry_layout.addWidget(self.fit_model_combo)

        self.ab_wipe_button = TogglePushButton("A/B Wipe", telemetry_bar)
        self.ab_wipe_button.setToolTip(
            "Split the trace with a draggable divider: Raw HDF5 (left) vs Processed (right)."
        )
        self.ab_wipe_button.toggled.connect(lambda _checked: self._update_analysis_overlays())
        telemetry_layout.addWidget(self.ab_wipe_button)

        self.fit_readout_label = CaptionLabel("", telemetry_bar)
        self.fit_readout_label.setObjectName("resultsTelemetryMuted")
        telemetry_layout.addWidget(self.fit_readout_label, 1)

        self.inspector_toggle_button = TogglePushButton("Inspector", telemetry_bar)
        self.inspector_toggle_button.setChecked(True)
        self.inspector_toggle_button.setToolTip("Show or hide the right analysis & filter inspector")
        self.inspector_toggle_button.toggled.connect(self.set_inspector_visible)
        telemetry_layout.addWidget(self.inspector_toggle_button)
        stage_layout.addWidget(telemetry_bar)

        # Primary Spectrum Canvas (fills viewport height above the fold!)
        self.spectrum_plot = ResultSpectrumWorkbench(main_stage)
        self.spectrum_plot.batch_button.clicked.connect(self._open_batch)
        self.peak_tools = ResultPeakTools(self)
        self.spectrum_plot.absolute_changed.connect(self.peak_tools.spectrum_changed)
        self.spectrum_plot.set_title("Select a stored or public spectrum")
        self.spectrum_plot.setMinimumHeight(200)
        self.spectrum_plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.spectrum_state = ResultsStateCard(main_stage)
        self.spectrum_view = QStackedWidget(main_stage)
        self.spectrum_view.setMinimumHeight(240)
        self.spectrum_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.spectrum_view.addWidget(self.spectrum_state)
        self.spectrum_view.addWidget(self.spectrum_plot)
        stage_layout.addWidget(self.spectrum_view, 1)

        # Compact Checkpoint Scrubber & Sweep Playback Bar
        navigation_card = QFrame(main_stage)
        navigation_card.setObjectName("resultsScrubberBar")
        navigation = QHBoxLayout(navigation_card)
        navigation.setContentsMargins(8, 4, 8, 4)
        navigation.setSpacing(6)
        self.prev_button = PushButton("Prev", navigation_card)
        self.prev_button.setIcon(FluentIcon.LEFT_ARROW)
        self.prev_button.setToolTip("Previous recorded spectrum")
        self.play_button = PushButton(FluentIcon.PLAY, "Play", navigation_card)
        self.play_button.setToolTip("Animate playback across recorded checkpoints")
        self.play_button.setEnabled(False)
        self.next_button = PushButton("Next", navigation_card)
        self.next_button.setIcon(FluentIcon.RIGHT_ARROW)
        self.next_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.next_button.setToolTip("Next recorded spectrum")
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        navigation.addWidget(self.prev_button)
        navigation.addWidget(self.play_button)
        navigation.addWidget(self.next_button)

        self.checkpoint_slider = Slider(Qt.Orientation.Horizontal, navigation_card)
        self.checkpoint_slider.setAccessibleName("Checkpoint scrubber")
        self.checkpoint_slider.setRange(0, 0)
        self.checkpoint_slider.setEnabled(False)
        navigation.addWidget(self.checkpoint_slider, 1)

        self.position_label = CaptionLabel("No spectrum selected", navigation_card)
        self.position_label.setObjectName("resultsTelemetryMuted")
        navigation.addWidget(self.position_label)
        stage_layout.addWidget(navigation_card)

        self.spectrum_info = CaptionLabel(
            "Spectra are read from HDF5 without contacting instruments.",
            main_stage,
        )
        self.spectrum_info.setObjectName("muted")
        self.spectrum_info.setWordWrap(True)
        self.spectrum_info.setFixedHeight(32)
        stage_layout.addWidget(self.spectrum_info)
        self.workspace_splitter.addWidget(main_stage)

        # ==================== RIGHT DRAWER: SCROLLABLE ANALYSIS & FILTER INSPECTOR ====================
        self.inspector_drawer = ScrollArea(self.workspace_splitter)
        self.inspector_drawer.setObjectName("resultsInspectorScroll")
        self.inspector_drawer.setWidgetResizable(True)
        self.inspector_drawer.setFrameShape(ScrollArea.Shape.NoFrame)
        self.inspector_drawer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector_drawer.setMinimumWidth(260)

        inspector_host = QWidget(self.inspector_drawer)
        inspector_host.setObjectName("resultsInspectorDrawer")
        inspector_layout = QVBoxLayout(inspector_host)
        inspector_layout.setContentsMargins(8, 6, 8, 8)
        inspector_layout.setSpacing(6)

        self.processing_controls = ResultProcessingControls(inspector_host)
        inspector_layout.addWidget(self.processing_controls)
        self.processing_controls.changed.connect(self._processing_changed)

        self.view_controls = SpectrumViewControls(inspector_host)
        inspector_layout.addWidget(self.view_controls)
        self.view_controls.changed.connect(self._processing_changed)
        self.view_controls.tools_requested.connect(self.spectrum_plot.show_tools)
        self.view_controls.floating_requested.connect(self.spectrum_plot.show_floating)
        self.view_controls.peaks_requested.connect(self.peak_tools.open)

        self.series_controls = SweepSeriesControls(inspector_host)
        self.series_controls.changed.connect(self._parameter_filter_changed)
        inspector_layout.addWidget(self.series_controls)

        self.baseline_controls = RecordedBaselineControls(inspector_host)
        self.baseline_controls.show_requested.connect(self.show_reference)
        self.baseline_controls.measurement_requested.connect(self._return_to_measurement)
        inspector_layout.addWidget(self.baseline_controls)

        # --- Fluent Controls Card (Checkpoint Filters & Public THATEC Rows) ---
        self.controls_card = CardWidget(inspector_host)
        controls_layout = QVBoxLayout(self.controls_card)
        controls_layout.setContentsMargins(10, 8, 10, 8)
        controls_layout.setSpacing(6)

        # Row 1: Checkpoint Filters & Stored View
        self.filter_selector = QGridLayout()
        filter_row = self.filter_selector
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(6)

        param_layout = QVBoxLayout()
        param_layout.setSpacing(2)
        param_label = CaptionLabel("Parameter", self.controls_card)
        param_label.setObjectName("muted")
        param_layout.addWidget(param_label)
        self.filter_parameter_combo = FilterComboBox(self.controls_card)
        self.filter_parameter_combo.setMinimumWidth(140)
        self.filter_parameter_combo.setAccessibleName("Result parameter filter")
        self.filter_parameter_combo.setToolTip(
            "Limit the checkpoint browser to one exact setpoint or result value."
        )
        param_layout.addWidget(self.filter_parameter_combo)
        filter_row.addLayout(param_layout, 0, 0)

        val_layout = QVBoxLayout()
        val_layout.setSpacing(2)
        val_label = CaptionLabel("Value", self.controls_card)
        val_label.setObjectName("muted")
        val_layout.addWidget(val_label)
        self.filter_value_combo = FilterComboBox(self.controls_card)
        self.filter_value_combo.setMinimumWidth(110)
        self.filter_value_combo.setAccessibleName("Result parameter value filter")
        val_layout.addWidget(self.filter_value_combo)
        filter_row.addLayout(val_layout, 1, 0)

        set_layout = QVBoxLayout()
        set_layout.setSpacing(2)
        set_label = CaptionLabel("Parameter set", self.controls_card)
        set_label.setObjectName("muted")
        set_layout.addWidget(set_label)
        self.parameter_set_combo = FilterComboBox(self.controls_card)
        self.parameter_set_combo.setMinimumWidth(140)
        self.parameter_set_combo.setAccessibleName("Result parameter set filter")
        self.parameter_set_combo.setToolTip(
            "Choose one exact combination of checkpoint parameters."
        )
        set_layout.addWidget(self.parameter_set_combo)
        filter_row.addLayout(set_layout, 2, 0)

        variant_layout = QVBoxLayout()
        variant_layout.setSpacing(2)
        variant_label = CaptionLabel("Spectrum view", self.controls_card)
        variant_label.setObjectName("muted")
        variant_layout.addWidget(variant_label)
        self.spectrum_variant_combo = ComboBox(self.controls_card)
        self.spectrum_variant_combo.setMinimumWidth(140)
        self.spectrum_variant_combo.setAccessibleName("Stored spectrum view")
        self.spectrum_variant_combo.setToolTip(
            "Choose raw, processed, or reference data for the selected checkpoint."
        )
        variant_layout.addWidget(self.spectrum_variant_combo)
        filter_row.addLayout(variant_layout, 3, 0)

        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(2)
        self.filter_summary = CaptionLabel("All checkpoints", self.controls_card)
        self.filter_summary.setObjectName("muted")
        btn_layout.addWidget(self.filter_summary)
        self.clear_filter_button = PushButton("Clear filter", self.controls_card)
        self.clear_filter_button.setIcon(FluentIcon.CLEAR_SELECTION)
        self.clear_filter_button.setEnabled(False)
        self.clear_filter_button.setToolTip("Reset parameter filters")
        btn_layout.addWidget(self.clear_filter_button)
        filter_row.addLayout(btn_layout, 4, 0)
        self._filter_fields = (param_layout, val_layout, set_layout, variant_layout, btn_layout)

        controls_layout.addLayout(filter_row)

        # Row 2: Public THATEC/PyThat spectral rows (only shown when rows exist)
        self.public_row_container = QWidget(self.controls_card)
        self.public_selector = QGridLayout(self.public_row_container)
        public_row = self.public_selector
        public_row.setContentsMargins(0, 4, 0, 0)
        public_row.setSpacing(6)

        pub_row_layout = QVBoxLayout()
        pub_row_layout.setSpacing(2)
        pub_row_label = CaptionLabel("Public spectrum row", self.public_row_container)
        pub_row_label.setObjectName("muted")
        pub_row_layout.addWidget(pub_row_label)
        self.thatec_row_combo = ComboBox(self.public_row_container)
        self.thatec_row_combo.setMinimumWidth(140)
        self.thatec_row_combo.setAccessibleName("Public spectrum row")
        self.thatec_row_combo.setToolTip(
            "Choose a spectral row stored in the public THATEC/PyThat result."
        )
        pub_row_layout.addWidget(self.thatec_row_combo)
        public_row.addLayout(pub_row_layout, 0, 0)

        pub_cp_layout = QVBoxLayout()
        pub_cp_layout.setSpacing(2)
        pub_cp_label = CaptionLabel("Checkpoint", self.public_row_container)
        pub_cp_label.setObjectName("muted")
        pub_cp_layout.addWidget(pub_cp_label)
        self.thatec_checkpoint = SpinBox(self.public_row_container)
        self.thatec_checkpoint.setRange(0, 0)
        self.thatec_checkpoint.setAccessibleName("Spectrum checkpoint")
        pub_cp_layout.addWidget(self.thatec_checkpoint)
        public_row.addLayout(pub_cp_layout, 1, 0)

        pub_tr_layout = QVBoxLayout()
        pub_tr_layout.setSpacing(2)
        pub_tr_label = CaptionLabel("Trace component", self.public_row_container)
        pub_tr_label.setObjectName("muted")
        pub_tr_layout.addWidget(pub_tr_label)
        self.thatec_trace_combo = ComboBox(self.public_row_container)
        self.thatec_trace_combo.setEnabled(False)
        self.thatec_trace_combo.setAccessibleName("Spectrum trace component")
        self.thatec_trace_combo.setToolTip(
            "Choose a channel from a multi-trace public spectrum."
        )
        pub_tr_layout.addWidget(self.thatec_trace_combo)
        public_row.addLayout(pub_tr_layout, 2, 0)

        pub_btn_layout = QVBoxLayout()
        pub_btn_layout.setSpacing(2)
        self.show_thatec_button = PushButton("Show spectrum", self.public_row_container)
        self.show_thatec_button.setIcon(FluentIcon.VIEW)
        self.show_thatec_button.setEnabled(False)
        self.show_thatec_button.setToolTip(
            "Read the selected public spectrum without contacting an instrument."
        )
        pub_btn_layout.addWidget(self.show_thatec_button)
        public_row.addLayout(pub_btn_layout, 3, 0)
        self._public_fields = (pub_row_layout, pub_cp_layout, pub_tr_layout, pub_btn_layout)

        controls_layout.addWidget(self.public_row_container)
        self.public_row_container.hide()
        inspector_layout.addWidget(self.controls_card)

        # Compact Checkpoint Table inside Inspector
        points_heading = CaptionLabel("Recorded checkpoints", inspector_host)
        points_heading.setObjectName("muted")
        inspector_layout.addWidget(points_heading)
        self.points = TreeView(inspector_host)
        self.points_model = CheckpointModel(self._point_tooltip, self)
        self.points.setModel(self.points_model)
        self.points.setRootIsDecorated(False)
        self.points.setUniformRowHeights(True)
        self.points.setAlternatingRowColors(False)
        self.points.setMinimumHeight(150)
        header = self.points.header()
        header.setMinimumSectionSize(50)
        header.setStretchLastSection(True)
        self.points.setColumnWidth(0, 52)
        self.points.setColumnWidth(1, 76)
        self.points.setColumnWidth(2, 130)
        inspector_layout.addWidget(self.points, 1)

        self.inspector_drawer.setWidget(inspector_host)
        self.workspace_splitter.addWidget(self.inspector_drawer)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([640, 320])
        layout.addWidget(self.workspace_splitter, 1)
        self.setMinimumHeight(240)

        self.points.selectionModel().currentRowChanged.connect(self._on_point_selected)
        self.prev_button.clicked.connect(self._go_previous)
        self.play_button.clicked.connect(self._toggle_playback)
        self.next_button.clicked.connect(self._go_next)
        self.checkpoint_slider.valueChanged.connect(self._on_slider_moved)
        self.filter_parameter_combo.currentIndexChanged.connect(
            self._parameter_key_changed
        )
        self.filter_value_combo.currentIndexChanged.connect(
            self._parameter_value_changed
        )
        self.parameter_set_combo.currentIndexChanged.connect(
            self._parameter_set_changed
        )
        self.clear_filter_button.clicked.connect(self.clear_parameter_filter)
        self.thatec_row_combo.currentIndexChanged.connect(self._thatec_row_changed)
        self.show_thatec_button.clicked.connect(self._load_selected_thatec_spectrum)
        self.thatec_trace_combo.currentIndexChanged.connect(self._render_thatec_trace)
        self.spectrum_variant_combo.currentIndexChanged.connect(
            self._render_selected_private_variant
        )
        self.spectrum_plot.status_changed.connect(self._show_plot_status)
        self._show_spectrum_state(
            "Select a spectrum",
            "Choose a public spectrum row or a stored checkpoint to inspect its trace.",
        )
        self._reset_variant_selector()
        self._reset_parameter_filters()
        self._filter_columns: int | None = None
        self._public_columns: int | None = None

    @staticmethod
    def _place_fields(grid, fields, columns):
        for field in fields:
            grid.removeItem(field)
        for index, field in enumerate(fields):
            grid.addLayout(field, index // columns, index % columns)
        for index in range(len(fields)):
            grid.setColumnStretch(index, 1 if index < columns else 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        ctrl_width = max(240, self.controls_card.width())
        filter_columns = 2 if ctrl_width >= 440 else 1
        public_columns = 2 if ctrl_width >= 440 else 1
        if filter_columns != self._filter_columns:
            self._filter_columns = filter_columns
            self._place_fields(self.filter_selector, self._filter_fields, filter_columns)
        if public_columns != self._public_columns:
            self._public_columns = public_columns
            self._place_fields(self.public_selector, self._public_fields, public_columns)

    def set_inspector_visible(self, visible: bool) -> None:
        self._inspector_visible = bool(visible)
        self.inspector_drawer.setVisible(self._inspector_visible)
        if self.inspector_toggle_button.isChecked() != self._inspector_visible:
            blocked = self.inspector_toggle_button.blockSignals(True)
            self.inspector_toggle_button.setChecked(self._inspector_visible)
            self.inspector_toggle_button.blockSignals(blocked)

    def toggle_inspector(self) -> None:
        self.set_inspector_visible(not self._inspector_visible)

    def toggle_resonance_fit(self) -> None:
        self.fit_button.setChecked(not self.fit_button.isChecked())

    def toggle_ab_wipe(self) -> None:
        self.ab_wipe_button.setChecked(not self.ab_wipe_button.isChecked())

    def _on_fit_toggled(self, checked: bool) -> None:
        self.fit_model_combo.setVisible(checked)
        self._update_analysis_overlays()

    def _toggle_playback(self) -> None:
        if self._play_timer.isActive():
            self._stop_playback()
            return
        rows, _has_spectra = self._navigation_rows()
        if len(rows) < 2:
            return
        self._play_timer.start()
        self.play_button.setText("Pause")
        self.play_button.setIcon(FluentIcon.PAUSE)

    def _stop_playback(self) -> None:
        if self._play_timer.isActive():
            self._play_timer.stop()
        self.play_button.setText("Play")
        self.play_button.setIcon(FluentIcon.PLAY)

    def _advance_playback(self) -> None:
        if self._read_tasks:
            return
        rows, _has_spectra = self._navigation_rows()
        if not rows:
            self._stop_playback()
            return
        current = self.points.currentIndex()
        cur_row = current.row() if current.isValid() else -1
        if isinstance(rows, range):
            next_row = (cur_row + 1) % len(rows)
        else:
            pos = bisect_right(rows, cur_row)
            next_row = rows[pos % len(rows)]
        self.points.setCurrentIndex(self.points_model.index(next_row, 0))

    def _on_slider_moved(self, value: int) -> None:
        rows, _has_spectra = self._navigation_rows()
        if not rows or not (0 <= value < len(rows)):
            return
        target_row = rows[value]
        current = self.points.currentIndex()
        if not current.isValid() or current.row() != target_row:
            self.points.setCurrentIndex(self.points_model.index(target_row, 0))

    def _primary_trace_data(self) -> tuple[object, object, str] | None:
        traces = getattr(self.spectrum_plot, "_traces", {})
        if not traces:
            return None
        preferred = getattr(self.spectrum_plot, "_hold_source", None)
        name = preferred if preferred in traces else next(
            (k for k in traces if k not in {"Max hold", "Min hold"}), None
        )
        if name is None:
            return None
        x, y = traces[name]
        unit = getattr(self.spectrum_plot, "amplitude_unit", "") or getattr(self.spectrum_plot, "_y_unit", "")
        return x, y, str(unit)

    def _raw_trace_data(self) -> tuple[object, object] | None:
        if self._selected_private_spectrum is not None:
            return (
                self._selected_private_spectrum.frequencies_hz,
                self._selected_private_spectrum.powers_dbm,
            )
        if self._public_spectrum is not None and self._public_spectrum.traces:
            idx = max(0, self.thatec_trace_combo.currentIndex())
            idx = min(idx, len(self._public_spectrum.traces) - 1)
            return (
                self._public_spectrum.x_values,
                self._public_spectrum.traces[idx].values,
            )
        return None

    def _update_analysis_overlays(self) -> None:
        """Update non-destructive Resonance Fit overlay & A/B Split Wipe curtain."""
        self._last_fit_summary = None
        if self.spectrum_view.currentWidget() is not self.spectrum_plot:
            self.spectrum_plot.clear_fit_overlay()
            self.spectrum_plot.clear_ab_wipe()
            self.fit_readout_label.setText("")
            return

        primary = self._primary_trace_data()
        # 1. A/B Split Wipe Curtain (Raw HDF5 left vs Processed right)
        if self.ab_wipe_button.isChecked():
            raw = self._raw_trace_data()
            if primary is None or raw is None:
                self.spectrum_plot.clear_ab_wipe()
                self.fit_readout_label.setText("A/B Wipe: load a spectrum to compare")
                return
            proc_x, proc_y, unit = primary
            if unit != "dBm":
                self.spectrum_plot.clear_ab_wipe()
                self.fit_readout_label.setText(
                    f"A/B Wipe requires matching dBm units (current: {unit})"
                )
            else:
                self.spectrum_plot.set_ab_wipe(
                    True,
                    raw_x=raw[0],
                    raw_y=raw[1],
                    proc_x=proc_x,
                    proc_y=proc_y,
                )
                self.fit_readout_label.setText(
                    "A/B Wipe active: drag divider · Raw HDF5 (left) vs Processed (right)"
                )
                self.spectrum_plot.clear_fit_overlay()
                return
        else:
            self.spectrum_plot.clear_ab_wipe()

        # 2. Offline Resonance Curve Fit (linear-power fit with SI W*Hz area)
        if not self.fit_button.isChecked() or primary is None:
            self.spectrum_plot.clear_fit_overlay()
            if not self.ab_wipe_button.isChecked():
                self.fit_readout_label.setText("")
            return

        x_hz, y_vals, unit = primary
        shape = str(self.fit_model_combo.currentData() or "lorentzian")
        window = None
        if getattr(self.spectrum_plot, "band_enabled", None) is not None and self.spectrum_plot.band_enabled.isChecked():
            lo, hi = self.spectrum_plot.band.getRegion()
            window = (float(lo), float(hi))
        try:
            summary = fit_spectrum_resonance(
                x_hz, y_vals, unit=unit, shape=shape, window_hz=window
            )
        except (ValueError, RuntimeError) as exc:
            self.spectrum_plot.clear_fit_overlay()
            self.fit_readout_label.setText(f"Fit unavailable: {exc}")
            return

        self._last_fit_summary = summary
        self.spectrum_plot.set_fit_overlay(
            summary.frequencies_hz, summary.fitted_display_values
        )
        f0_hz = summary.fit.center_hz
        fw_hz = summary.fit.fwhm_hz
        f0_txt = f"{f0_hz / 1e9:.4g} GHz" if abs(f0_hz) >= 1e9 else f"{f0_hz / 1e6:.4g} MHz" if abs(f0_hz) >= 1e6 else f"{f0_hz:.4g} Hz"
        fw_txt = f"{fw_hz / 1e9:.3g} GHz" if abs(fw_hz) >= 1e9 else f"{fw_hz / 1e6:.4g} MHz" if abs(fw_hz) >= 1e6 else f"{fw_hz:.4g} Hz"
        self.fit_readout_label.setText(
            f"f₀ = {f0_txt} · FWHM = {fw_txt} · Q = {summary.q_factor:.3g} · R² = {summary.r_squared:.3f}"
        )
        self.fit_readout_label.setToolTip(
            f"{shape.capitalize()} resonance fit · f₀ = {format_quantity_auto(f0_hz, DIMENSION_FREQUENCY)} · "
            f"FWHM = {format_quantity_auto(fw_hz, DIMENSION_FREQUENCY)} · "
            f"Q = {summary.q_factor:.3g} · ∫P df = {summary.fit.finite_window_area_w_hz:.3g} W·Hz · "
            f"R² = {summary.r_squared:.4f}"
        )

    def load(
        self,
        path: Path,
        run: ThatecRun,
        points: tuple[StoredPoint, ...],
        *, references=(),
    ) -> None:
        """Load private point data and public spectral rows from one file."""

        self.clear()
        self.processing_controls.set_references(references)
        self.view_controls.set_references(references)
        self.baseline_controls.set_references(references)
        self._selected_path = path
        try:
            stat = path.stat()
            self._batch_identity = (path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._batch_identity = (path, None, None)
        self._session = ResultReadSession(path, references=references)
        self._run = run
        self._stored_points = points
        self._public_checkpoints = ()
        if not points:
            self._show_spectrum_state("Loading checkpoints", "Reading public checkpoint metadata...", loading=True)
            self._start_read("checkpoints", run, build_public_checkpoints, path, run, cooperative_cancel=True)
            return
        self._populate_thatec_rows()
        self._populate_points(points)
        self._populate_parameter_filters()

    def show_stored_spectrum(self, index: int, variant: str = "saved_view") -> None:
        """Open a private checkpoint requested by the result tree."""

        self._preferred_variant = variant

        def select_visible() -> bool:
            item = self.points_model.find_checkpoint(index, StoredPoint)
            if item.isValid():
                record = item.data(Qt.ItemDataRole.UserRole)
                if self.points.currentIndex().siblingAtColumn(0) == item and self._selected_private_point is not record:
                    self._on_point_selected(item, None)
                self.points.setCurrentIndex(item)
                variant_index = self.spectrum_variant_combo.findData(variant)
                if variant_index >= 0:
                    self.spectrum_variant_combo.setCurrentIndex(variant_index)
                return True
            return False

        if select_visible():
            return
        # A tree action should remain reachable even when the Spectrum page is
        # filtered to another parameter set.  Clear that view-local filter and
        # then select the requested immutable checkpoint.
        if self._stored_points:
            self.clear_parameter_filter()
            select_visible()

    def show_reference(self, index: int, sweep: int | None = None) -> None:
        """Open one stored Anritsu reference from the Results tree."""

        if self._selected_path is None:
            return
        self._invalidate_pending_reads()
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = None
        self._public_spectrum = None
        self._pending_private_variant = None
        self.baseline_controls.select(index, sweep)
        self._set_baseline_context(True)
        self._reset_variant_selector()
        self.device_state_changed.emit({})
        self._show_spectrum_state("Loading reference", "Reading stored reference...", loading=True)
        if sweep is None:
            reader = self._session.reference if self._session else Hdf5RunReader.reference
            self._start_read("reference", index, reader, self._selected_path, index)
        else:
            reader = self._session.reference_sweep if self._session else Hdf5RunReader.reference_sweep
            self._start_read("reference", index, reader, self._selected_path, index, sweep)

    def _set_baseline_context(self, enabled):
        self.baseline_controls.set_viewing(enabled)
        self.processing_controls.set_baseline_view(enabled)
        self.view_controls.set_baseline_view(enabled)

    def _return_to_measurement(self):
        item = self.points.currentIndex()
        if not item.isValid() and self.points_model.rowCount():
            item = self.points_model.index(0, 0)
        if item.isValid():
            self._on_point_selected(item, None)
            self.points.setCurrentIndex(item)

    def _render_reference(self, index, reference):
        if reference is None:
            self._show_spectrum_state(
                "Reference spectrum unavailable",
                f"Reference {index} is not a complete stored spectrum.",
            )
            return
        self._public_spectrum = None
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = reference
        self.processing_controls.set_instrument_average(reference.averaging_source == "instrument")
        self._reset_variant_selector()
        self.device_state_changed.emit({})
        self.position_label.setText(f"{reference.purpose.title()} {reference.index} · {baseline_sample_label(reference)}")
        # Keep checkpoint correction choices, but preview a baseline as absolute
        # power. It already represents its explicitly selected mean or repeat.
        state = replace(self.processing_controls.state, operation="none", reference_index=None,
            reference_sweep=None, parameters=replace(self.processing_controls.state.parameters,
                                                    temporal_average_frames=1))
        from app.ui.results.spectrum_views import SpectrumView
        self._start_read("baseline_view", reference.index, read_baseline_view, reference,
            state, SpectrumView(unit=self.view_controls.state.unit), cooperative_cancel=True)

    def show_thatec_spectrum(self, row_id: str, checkpoint: int) -> None:
        """Select a public spectrum requested by the tree or heatmap."""

        if self._selected_path is None or self._run is None:
            return
        row_index = self.thatec_row_combo.findData(row_id)
        if row_index < 0:
            self.spectrum_info.setText(
                f"THATEC row {row_id} does not contain a displayable spectrum."
            )
            return
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = None
        self._set_baseline_context(False)
        self._reset_variant_selector()
        self.device_state_changed.emit({})
        item = self.points_model.find_checkpoint(checkpoint, PublicCheckpoint)
        if not item.isValid() and self._public_checkpoints:
            self.clear_parameter_filter()
            item = self.points_model.find_checkpoint(checkpoint, PublicCheckpoint)
        if item.isValid():
            self.points.setCurrentIndex(item)
        self.thatec_row_combo.setCurrentIndex(row_index)
        self.thatec_checkpoint.setValue(
            max(0, min(checkpoint, self.thatec_checkpoint.maximum()))
        )
        self._load_selected_thatec_spectrum()

    def clear(self) -> None:
        """Reset the tab to its empty state."""

        self._stop_playback()
        self._last_fit_summary = None
        self.fit_readout_label.setText("")
        self.checkpoint_slider.blockSignals(True)
        self.checkpoint_slider.setRange(0, 0)
        self.checkpoint_slider.setEnabled(False)
        self.checkpoint_slider.blockSignals(False)
        self.play_button.setEnabled(False)
        self._invalidate_pending_reads()
        self._cancel_filter_read()
        self.peak_tools.close()
        self.points_model.set_records(())
        self._clear_spectrum()
        self._stored_points = ()
        self.processing_controls.set_references(())
        self.view_controls.set_references(())
        self.baseline_controls.set_references(())
        self._set_baseline_context(False)
        self.spectrum_plot.close_windows()
        self._public_checkpoints = ()
        self._selected_path = None
        self._session = None
        self._preferred_variant = "saved_view"
        self._run = None
        self._public_spectrum = None
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = None
        self.thatec_row_combo.blockSignals(True)
        self.thatec_row_combo.clear()
        self.thatec_row_combo.blockSignals(False)
        self.thatec_checkpoint.setRange(0, 0)
        self.thatec_trace_combo.blockSignals(True)
        self.thatec_trace_combo.clear()
        self.thatec_trace_combo.blockSignals(False)
        self.thatec_trace_combo.setEnabled(False)
        self.show_thatec_button.setEnabled(False)
        self.public_row_container.setVisible(False)
        self._reset_parameter_filters()
        self.series_controls.clear()
        self._reset_variant_selector()
        self.position_label.setText("No spectrum selected")

    def _populate_points(
        self,
        points: tuple[StoredPoint | PublicCheckpoint, ...],
    ) -> None:
        selected = self.points.currentIndex().siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        selection = self.points.selectionModel()
        with QSignalBlocker(selection):
            self.points_model.set_records(points)
        if points:
            matched = next((i for i, p in enumerate(points)
                            if selected is not None and type(p) is type(selected)
                            and p.index == selected.index), None)
            target = matched if matched is not None else next(
                (i for i, p in enumerate(points) if not isinstance(p, StoredPoint) or p.has_spectrum), 0)
            current_point = self._selected_private_point
            unchanged = matched is not None and (
                (isinstance(selected, StoredPoint) and current_point is not None
                 and current_point.index == selected.index)
                or (isinstance(selected, PublicCheckpoint) and self._public_spectrum is not None
                    and self._public_spectrum.checkpoint == selected.index))
            if unchanged:
                with QSignalBlocker(selection):
                    self.points.setCurrentIndex(self.points_model.index(target, 0))
            else:
                self.points.setCurrentIndex(self.points_model.index(target, 0))
        else:
            self._invalidate_pending_reads()
            self._selected_private_point = self._selected_private_spectrum = self._public_spectrum = None
            self._show_spectrum_state("No matching checkpoints", "Choose another parameter combination.")

        self._update_nav_buttons()
        self._pending_private_variant = None
        if points:
            self.filter_summary.setText(f"Showing {len(points):,} checkpoint(s)")
        else:
            self.filter_summary.setText("No checkpoints")

    def _populate_parameter_filters(self) -> None:
        self._start_filter_read("catalogue")

    def _populate_parameter_values(self) -> None:
        key = self.filter_parameter_combo.currentData()
        self.filter_value_combo.set_options(("All values", _ALL_VALUES))
        if key not in (None, _ALL_PARAMETERS):
            self._start_filter_read("values", str(key))
        elif self._filter_context and self._filter_context[0] == "values":
            self._cancel_filter_read()

    @classmethod
    def _build_filter_options(cls, kind, records, key, *, cancelled):
        if kind == "selection":
            parameter, value, signature, *extra = key
            constraints = extra[0] if extra else ()
            filtered = []
            for record in records:
                if cancelled():
                    raise InterruptedError("Checkpoint filtering cancelled")
                matches = (
                    cls._signatures_equal(cls._record_parameter_signature(record), signature)
                    if signature not in (None, _ALL_PARAMETER_SETS)
                    else (parameter in (None, _ALL_PARAMETERS) or value in (None, _ALL_VALUES)
                          or cls._values_equal(cls._record_values(record).get(str(parameter)), value))
                )
                setpoints = dict(cls._record_parameter_signature(record))
                matches = matches and all(k in setpoints and cls._values_equal(setpoints[k], v)
                                          for k, v in constraints)
                if matches:
                    filtered.append(record)
            return tuple(filtered)
        if kind == "values":
            def signatures():
                for record in records:
                    if cancelled():
                        raise InterruptedError("Filter indexing cancelled")
                    value = cls._record_values(record).get(str(key))
                    if value is not None:
                        yield ((str(key), value),)
            values = [signature[0][1] for signature in unique_signatures(signatures(), cls._signatures_equal, cancelled=cancelled)]
            return sorted(values, key=cls._format_value)
        keys = set()
        for record in records:
            if cancelled():
                raise InterruptedError("Filter indexing cancelled")
            keys.update(cls._record_values(record))
        signatures = unique_signatures(
            (cls._record_parameter_signature(record) for record in records),
            cls._signatures_equal, cancelled=cancelled,
        )
        series = {}
        for series_key in sorted({item_key for signature in signatures for item_key, _ in signature}):
            values = unique_signatures(
                (((series_key, dict(signature)[series_key]),) for signature in signatures if series_key in dict(signature)),
                cls._signatures_equal, cancelled=cancelled)
            series[series_key] = tuple(sorted((s[0][1] for s in values), key=lambda v: (0, float(v))
                                       if isinstance(v, (int, float)) else (1, str(v))))
        return sorted(keys), sorted(signatures, key=cls._format_signature), series

    def _cancel_filter_read(self):
        self._filter_request_id += 1
        if self._filter_task is not None:
            self._filter_task.cancel()
        self._filter_pool.clear()
        self._filter_task = None
        self._filter_context = None
        for combo in (self.filter_parameter_combo, self.filter_value_combo, self.parameter_set_combo):
            combo.setEnabled(True)

    def _start_filter_read(self, kind, key=None):
        self._cancel_filter_read()
        self._filter_context = (kind, key)
        self.filter_value_combo.setEnabled(kind == "selection")
        if kind == "catalogue":
            self.filter_parameter_combo.setEnabled(False)
            self.parameter_set_combo.setEnabled(False)
        task = ResultReadTask(
            self._filter_request_id, type(self)._build_filter_options, kind,
            self._stored_points or self._public_checkpoints, key,
            cooperative_cancel=True,
        )
        self._filter_task = task
        task.signals.loaded.connect(self._filter_loaded)
        task.signals.failed.connect(self._filter_failed)
        self._filter_pool.start(task)

    def _filter_loaded(self, request_id, payload):
        if request_id != self._filter_request_id:
            return
        kind, key = self._filter_context
        self._cancel_filter_read()
        if kind == "catalogue":
            keys, signatures, *series = payload
            self.filter_parameter_combo.set_options(("All parameters", _ALL_PARAMETERS), keys)
            self.parameter_set_combo.set_options(
                ("All parameter sets", _ALL_PARAMETER_SETS), signatures,
                lambda signature: self._format_signature(signature) or "No setpoints",
            )
            if series:
                self.series_controls.set_catalogue(series[0], self._format_parameter_value)
        elif kind == "values":
            self.filter_value_combo.set_options(
                ("All values", _ALL_VALUES), payload,
                lambda value: self._format_parameter_value(key, value),
            )
        elif kind == "selection":
            self._populate_points(payload)
            records = self._stored_points or self._public_checkpoints
            self.filter_summary.setText(f"Showing {len(payload):,} of {len(records):,} checkpoint(s)")

    def _filter_failed(self, request_id, message):
        if request_id == self._filter_request_id:
            self._cancel_filter_read()
            self.filter_summary.setText(f"Cannot build filters: {message}")

    def _parameter_value_changed(self, *_args: object) -> None:
        self._select_all_parameter_sets()
        self._parameter_filter_changed()

    def _parameter_set_changed(self, *_args: object) -> None:
        selected = self.parameter_set_combo.currentData()
        if selected not in (None, _ALL_PARAMETER_SETS):
            self._cancel_filter_read()
            self.filter_parameter_combo.blockSignals(True)
            self.filter_parameter_combo.setCurrentIndex(0)
            self.filter_parameter_combo.blockSignals(False)
            self.filter_value_combo.set_options(("All values", _ALL_VALUES))
        self._parameter_filter_changed()

    def _select_all_parameter_sets(self) -> None:
        if self.parameter_set_combo.currentData() == _ALL_PARAMETER_SETS:
            return
        self.parameter_set_combo.blockSignals(True)
        self.parameter_set_combo.setCurrentIndex(0)
        self.parameter_set_combo.blockSignals(False)

    def _parameter_key_changed(self, *_args: object) -> None:
        self._select_all_parameter_sets()
        self._populate_parameter_values()
        self._parameter_filter_changed()

    def _parameter_filter_changed(self, *_args: object) -> None:
        key = self.filter_parameter_combo.currentData()
        selected_value = self.filter_value_combo.currentData()
        selected_set = self.parameter_set_combo.currentData()
        records: tuple[StoredPoint | PublicCheckpoint, ...] = (
            self._stored_points or self._public_checkpoints
        )
        active = (
            bool(self.series_controls.constraints)
            or
            selected_set not in (None, _ALL_PARAMETER_SETS)
            or (
                key not in (None, _ALL_PARAMETERS)
                and selected_value not in (None, _ALL_VALUES)
            )
        )
        self.clear_filter_button.setEnabled(active)
        if active:
            self.filter_summary.setText("Filtering checkpoints...")
            self._start_filter_read("selection", (key, selected_value, selected_set,
                                                   self.series_controls.constraints))
            return
        if self._filter_context and self._filter_context[0] == "selection":
            self._cancel_filter_read()
        self._populate_points(records)
        self.filter_summary.setText(f"All {len(records):,} checkpoint(s)")

    def clear_parameter_filter(self) -> None:
        self.series_controls.reset()
        self.parameter_set_combo.blockSignals(True)
        self.parameter_set_combo.setCurrentIndex(0)
        self.parameter_set_combo.blockSignals(False)
        self.filter_parameter_combo.blockSignals(True)
        self.filter_parameter_combo.setCurrentIndex(0)
        self.filter_parameter_combo.blockSignals(False)
        self._populate_parameter_values()
        self._parameter_filter_changed()

    def _reset_parameter_filters(self) -> None:
        self.filter_parameter_combo.set_options(("All parameters", _ALL_PARAMETERS))
        self.filter_value_combo.set_options(("All values", _ALL_VALUES))
        self.parameter_set_combo.set_options(("All parameter sets", _ALL_PARAMETER_SETS))
        self.clear_filter_button.setEnabled(False)
        self.filter_summary.setText("All checkpoints")

    @staticmethod
    def _record_values(record: StoredPoint | PublicCheckpoint) -> dict[str, object]:
        if isinstance(record, StoredPoint):
            return {**record.setpoints, **record.measurements}
        return dict(record.values)

    @staticmethod
    def _record_parameter_signature(
        record: StoredPoint | PublicCheckpoint,
    ) -> tuple[tuple[str, object], ...]:
        values = record.setpoints if record.setpoints is not None else record.values
        return tuple(sorted((str(key), value) for key, value in values.items()))

    @classmethod
    def _signatures_equal(
        cls,
        left: tuple[tuple[str, object], ...],
        right: object,
    ) -> bool:
        if not isinstance(right, tuple) or len(left) != len(right):
            return False
        return all(
            str(left_item[0]) == str(right_item[0])
            and cls._values_equal(left_item[1], right_item[1])
            for left_item, right_item in zip(left, right, strict=True)
        )

    @classmethod
    def _format_signature(cls, signature: tuple[tuple[str, object], ...]) -> str:
        return "; ".join(
            f"{key}={cls._format_parameter_value(key, value)}"
            for key, value in signature
        )

    @staticmethod
    def _format_parameter_value(key: str, value: object) -> str:
        """Format SI result values with a unit inferred from the stable key."""

        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return str(value)
        normalized = key.casefold()
        dimension = None
        if "dbm" in normalized:
            dimension = DIMENSION_DBM
        elif "db" in normalized:
            dimension = DIMENSION_DB
        elif normalized.endswith("_hz") or "frequency" in normalized or "(hz)" in normalized:
            dimension = DIMENSION_FREQUENCY
        elif normalized.endswith("_v") or "voltage" in normalized or "(v)" in normalized:
            dimension = DIMENSION_VOLTAGE
        elif normalized.endswith("_a") or "current" in normalized or "(a)" in normalized:
            dimension = DIMENSION_CURRENT
        elif normalized.endswith("_w") or "power" in normalized or "(w)" in normalized:
            dimension = DIMENSION_POWER
        elif normalized.endswith("_ohm") or "resistance" in normalized or "(ohm)" in normalized:
            dimension = DIMENSION_RESISTANCE
        elif normalized.endswith("_s") or "time" in normalized or "(s)" in normalized:
            dimension = DIMENSION_TIME
        elif normalized.endswith("_t") or "field" in normalized or "(t)" in normalized:
            dimension = DIMENSION_MAGNETIC_FIELD
        if dimension is None:
            return SpectrumResultsTab._format_value(value)
        try:
            return format_quantity_auto(float(value), dimension)
        except (TypeError, ValueError):
            return SpectrumResultsTab._format_value(value)

    @staticmethod
    def _values_equal(left: object, right: object) -> bool:
        if left is None or right is None:
            return left is right
        try:
            return math.isclose(float(left), float(right), rel_tol=FILTER_REL_TOL, abs_tol=FILTER_ABS_TOL)
        except (TypeError, ValueError):
            return str(left) == str(right)

    @staticmethod
    def _format_value(value: object) -> str:
        if isinstance(value, float):
            return f"{value:.12g}"
        return str(value)

    def _populate_thatec_rows(self) -> None:
        self._public_spectrum = None
        self.thatec_row_combo.blockSignals(True)
        self.thatec_row_combo.clear()
        if self._run is not None:
            for row in find_spectrum_rows(self._run):
                label = row.control_name or row.device_name or row.id
                checkpoints = row.shape[0] if row.shape else 0
                sample_shape = " x ".join(str(size) for size in row.shape[1:])
                role = dict(row.definition).get("lab control role", "spectrum")
                variant = "processed" if role == "spectrum_processed" else "recorded"
                self.thatec_row_combo.addItem(
                    f"{label} · {variant} ({checkpoints} checkpoints x {sample_shape})",
                    userData=row.id,
                )
        self.thatec_row_combo.blockSignals(False)
        self.public_row_container.setVisible(self.thatec_row_combo.count() > 0)
        self._thatec_row_changed()

    def _thatec_row_changed(self, *_args: object) -> None:
        self._invalidate_pending_reads()
        row_id = self.thatec_row_combo.currentData()
        row = self._run.rows.get(str(row_id)) if self._run is not None else None
        self._public_spectrum = None
        self.thatec_trace_combo.blockSignals(True)
        self.thatec_trace_combo.clear()
        self.thatec_trace_combo.blockSignals(False)
        self.thatec_trace_combo.setEnabled(False)
        if row is None or len(row.shape) < 2:
            self.thatec_checkpoint.setRange(0, 0)
            self.show_thatec_button.setEnabled(False)
            return
        self.thatec_checkpoint.setRange(0, max(0, row.shape[0] - 1))
        self.show_thatec_button.setEnabled(True)

    def _load_selected_thatec_spectrum(self) -> None:
        self._set_baseline_context(False)
        self._invalidate_pending_reads()
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = None
        self._reset_variant_selector()
        row_id = self.thatec_row_combo.currentData()
        if self._selected_path is None or row_id is None or self._run is None:
            return
        checkpoint = self.thatec_checkpoint.value()
        row = self._run.rows.get(str(row_id))
        if row is None:
            return
        sample_count = prod(row.shape[1:]) if len(row.shape) >= 2 else 0
        self.show_thatec_button.setEnabled(False)
        self._show_spectrum_state(
            "Loading spectrum",
            f"Reading {sample_count:,} samples from checkpoint {checkpoint}...",
            loading=True,
        )
        reader = self._session.public_spectrum if self._session else ThatecRunReader.spectrum_slice
        self._start_read(
            "public", (str(row_id), checkpoint), reader,
            self._selected_path, str(row_id), checkpoint,
        )

    def _accept_public_spectrum(self, spectrum: ThatecSpectrum) -> None:
        self._public_spectrum = spectrum
        self.thatec_trace_combo.blockSignals(True)
        self.thatec_trace_combo.clear()
        for index, trace in enumerate(spectrum.traces):
            self.thatec_trace_combo.addItem(trace.name, userData=index)
        self.thatec_trace_combo.setCurrentIndex(0)
        self.thatec_trace_combo.blockSignals(False)
        self.thatec_trace_combo.setEnabled(bool(spectrum.traces))
        self.show_thatec_button.setEnabled(True)
        self._render_thatec_trace()

    def _render_thatec_trace(self, *_args: object) -> None:
        spectrum = self._public_spectrum
        trace_index = self.thatec_trace_combo.currentData()
        if spectrum is None or trace_index is None:
            return
        try:
            trace = spectrum.traces[int(trace_index)]
        except (IndexError, ValueError):
            return
        point = self._public_point()
        processed_row = spectrum.role == "spectrum_processed"
        self.processing_controls.set_instrument_average(recorded_instrument_average(point) is not None)
        if self.view_controls.state.active:
            row = self._run.rows.get(spectrum.row_id) if self._run else None
            if row and dict(row.definition).get("lab control role") == "spectrum_processed":
                self._show_spectrum_error("Choose a raw spectrum", "Offline comparisons start from the raw spectrum row.")
                return
            self._invalidate_pending_reads()
            self._show_spectrum_state("Preparing spectrum views", "Reading independent recorded baselines…", loading=True)
            self._start_read("view", spectrum.checkpoint, read_public_view, self._session,
                spectrum, int(trace_index), self._stored_points, self.processing_controls.state,
                self.view_controls.state, cooperative_cancel=True)
            return
        if self.processing_controls.state.active:
            row = self._run.rows.get(spectrum.row_id) if self._run else None
            if row and dict(row.definition).get("lab control role") == "spectrum_processed":
                self._show_spectrum_error("Choose a raw spectrum", "Post-processing starts from the raw spectrum row. Select a raw row above.")
                return
            self._invalidate_pending_reads()
            self._show_spectrum_state("Processing recorded spectrum", "Applying post-acquisition filters...", loading=True)
            self._start_read("post", spectrum.checkpoint, self._session.processed_public,
                             self._selected_path, spectrum, int(trace_index),
                             self.processing_controls.state, self._stored_points)
            return
        trace_label = trace.name if processed_row else recorded_spectrum_label(point, trace.name)
        self.spectrum_plot.prepare_traces((trace_label,))
        self.spectrum_plot.set_labels(
            x=spectrum.x_label,
            x_unit=spectrum.x_unit,
            y=spectrum.y_label,
            y_unit=spectrum.y_unit,
        )
        row = self._run.rows.get(spectrum.row_id) if self._run is not None else None
        self.spectrum_plot.set_trace(
            trace_label,
            spectrum.x_values,
            trace.values,
            primary=True,
            signed_difference=spectrum.y_unit == "dB" or processed_row and spectrum.y_unit == "W",
        )
        label = row.control_name if row is not None and row.control_name else spectrum.row_id
        self.spectrum_plot.set_title(
            f"{label} - checkpoint {spectrum.checkpoint} - {trace_label}"
        )
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        row_count = (
            row.shape[0]
            if row is not None and len(row.shape) >= 2
            else spectrum.checkpoint + 1
        )
        self.position_label.setText(
            f"Public checkpoint {spectrum.checkpoint + 1} / {row_count}"
        )
        self._update_nav_buttons()
        self.spectrum_info.setText(
            "Public THATEC/PyThat spectrum: "
            f"{label}, checkpoint {spectrum.checkpoint}, {trace_label}, "
            f"{len(spectrum.x_values)} samples from {spectrum.source_shape}."
        )
        self._publish_spectrum()

    def _public_point(self) -> StoredPoint | None:
        if self._public_spectrum is None:
            return None
        index = self._public_spectrum.checkpoint
        if 0 <= index < len(self._stored_points) and self._stored_points[index].index == index:
            return self._stored_points[index]
        return next((point for point in self._stored_points if point.index == index), None)

    def _on_point_selected(
        self,
        item: QModelIndex | None,
        _previous: QModelIndex | None,
    ) -> None:
        self._pending_private_variant = None
        self._update_nav_buttons()
        self._invalidate_pending_reads()
        if item is None or not item.isValid() or self._selected_path is None:
            self._selected_private_point = None
            self._selected_private_spectrum = None
            self._reset_variant_selector()
            self.device_state_changed.emit({})
            return
        point = item.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if isinstance(point, PublicCheckpoint):
            self._set_baseline_context(False)
            self._selected_private_point = None
            self._selected_private_spectrum = None
            self._reset_variant_selector()
            self.device_state_changed.emit({})
            if self.thatec_row_combo.count():
                self.thatec_checkpoint.setValue(
                    max(0, min(point.index, self.thatec_checkpoint.maximum()))
                )
                self._load_selected_thatec_spectrum()
            return
        if not isinstance(point, StoredPoint):
            return
        self._set_baseline_context(False)
        self._selected_private_point = point
        self._selected_private_spectrum = None
        self._selected_reference = None
        self.device_state_changed.emit(point.device_states)
        if not point.details_loaded:
            self._show_spectrum_state("Loading checkpoint", "Reading checkpoint details and spectrum...", loading=True)
            reader = self._session.checkpoint if self._session else read_checkpoint_details
            self._start_read("private_details", point, reader, self._selected_path, point)
            return
        if not point.has_spectrum:
            self._show_spectrum_state(
                "No spectrum at this checkpoint",
                "This checkpoint contains scalar data only. Choose another checkpoint.",
            )
            return
        self._show_spectrum_state(
            "Loading stored spectrum",
            "Reading and reducing the stored spectrum for display...",
            loading=True,
        )
        reader = self._session.spectrum if self._session else Hdf5RunReader.spectrum
        self._start_read("private", point, reader, self._selected_path, point.index)

    def _render_stored_spectrum(
        self, point: StoredPoint, trace: StoredSpectrum
    ) -> None:
        self._selected_private_point = point
        self._selected_private_spectrum = trace
        self.processing_controls.set_instrument_average(
            recorded_instrument_average(point, trace_name=trace.trace_name) is not None
        )
        self._update_variant_selector(trace)
        pending = getattr(self, "_pending_private_variant", None)
        self._pending_private_variant = None
        if pending is not None:
            selected = self.spectrum_variant_combo.findData(pending)
            if selected >= 0:
                blocked = self.spectrum_variant_combo.blockSignals(True)
                self.spectrum_variant_combo.setCurrentIndex(selected)
                self.spectrum_variant_combo.blockSignals(blocked)
        self._render_selected_private_variant()

    def _render_selected_private_variant(self, *_args: object) -> None:
        if _args:
            self._preferred_variant = self.spectrum_variant_combo.currentData() or "raw"
        if self._read_context.get(self._active_read_request, (None,))[0] == "variant_reference":
            self._invalidate_pending_reads()
        point = self._selected_private_point
        trace = self._selected_private_spectrum
        if point is None or trace is None:
            return
        if self.view_controls.state.active:
            self._invalidate_pending_reads()
            self._show_spectrum_state("Preparing spectrum views", "Comparing complete recorded grids…", loading=True)
            self._start_read("view", point, read_private_view, self._session, point,
                self.processing_controls.state, self.view_controls.state, cooperative_cancel=True)
            return
        if self.processing_controls.state.active:
            self._invalidate_pending_reads()
            self._show_spectrum_state("Processing recorded spectrum", "Applying correction and filters to the complete raw grid...", loading=True)
            self._start_read("post", point, self._session.processed_private, self._selected_path, point, self.processing_controls.state)
            return
        variant = self.spectrum_variant_combo.currentData() or "raw"
        if variant == "saved_view" and trace.saved_display is not None:
            saved = trace.saved_display
            self._render_view(
                SpectrumViewPayload(saved.traces, ("Spectrum view saved on Anritsu.",)),
                title=f"Checkpoint {point.index} · As saved on Anritsu",
                saved_display=saved,
            )
            return
        reference: StoredReference | None = None
        if variant in {"reference", "raw_reference"}:
            reference_index = (
                trace.reference_index if trace.reference_index is not None else 0
            )
            if self._selected_reference is None or self._selected_reference.index != reference_index:
                self._invalidate_pending_reads()
                self._show_spectrum_state("Loading reference", "Reading stored reference...", loading=True)
                reader = self._session.reference if self._session else Hdf5RunReader.reference
                self._start_read("variant_reference", reference_index, reader,
                                 self._selected_path, reference_index)
                return
            reference = self._selected_reference
            if reference is None:
                self._show_spectrum_state(
                    "Reference spectrum unavailable",
                    "This checkpoint does not have a complete stored reference.",
                )
                return

        raw_label = recorded_spectrum_label(point, "Stored spectrum", trace_name=trace.trace_name)
        reference_label = (f"Reference · {baseline_sample_label(reference)}"
                           if reference is not None and reference.averaging_source == "instrument"
                           else "Reference spectrum")
        names = (["Processed spectrum"] if variant == "processed" else
                 [reference_label] if variant == "reference" else
                 [raw_label, "Processed spectrum"] if variant == "raw_processed" else
                 [raw_label, reference_label] if variant == "raw_reference" else [raw_label])
        self.spectrum_plot.prepare_traces(names)
        self.spectrum_plot.set_title("Select a stored or public spectrum")
        if variant == "processed":
            if trace.processed_values is None:
                self._show_spectrum_state(
                    "Processed spectrum unavailable",
                    "This checkpoint contains a raw trace but no processed values.",
                )
                return
            self.spectrum_plot.set_labels(
                x="Frequency", x_unit="Hz", y="Processed amplitude",
                y_unit=trace.processed_unit or "",
            )
            self.spectrum_plot.set_trace(
                "Processed spectrum",
                trace.frequencies_hz,
                trace.processed_values,
                primary=True,
                signed_difference=trace.processed_unit == "dB" or trace.processing_operation in
                {"subtract_power_signed", "subtract_reference_signed"},
            )
        elif variant == "reference":
            assert reference is not None
            self.spectrum_plot.set_labels(
                x="Frequency", x_unit="Hz", y="Power", y_unit="dBm"
            )
            self.spectrum_plot.set_trace(
                reference_label,
                reference.frequencies_hz,
                reference.powers_dbm,
                primary=True,
            )
        else:
            if variant == "raw_processed" and trace.processed_unit != "dBm":
                self._show_spectrum_error(
                    "Incompatible overlay units",
                    f"Raw dBm and processed {trace.processed_unit} require separate views. Choose Processed spectrum.")
                return
            y_label = "Raw / processed" if variant == "raw_processed" else "Power"
            y_unit = "dBm"
            self.spectrum_plot.set_labels(
                x="Frequency", x_unit="Hz", y=y_label, y_unit=y_unit
            )
            self.spectrum_plot.set_trace(
                raw_label,
                trace.frequencies_hz,
                trace.powers_dbm,
                primary=True,
            )
            if variant == "raw_processed" and trace.processed_values is not None:
                self.spectrum_plot.set_trace(
                    "Processed spectrum",
                    trace.frequencies_hz,
                    trace.processed_values,
                    primary=False,
                )
            elif variant == "raw_reference" and reference is not None:
                self.spectrum_plot.set_trace(
                    reference_label,
                    reference.frequencies_hz,
                    reference.powers_dbm,
                    primary=False,
                )
        self.spectrum_plot.set_title(f"{raw_label} at point {point.index} ({trace.trace_name})")
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        processed_note = (
            f"; processed: {trace.processing_operation} ({trace.processed_unit})"
            if trace.processed_values is not None
            else ""
        )
        self.spectrum_info.setText(
            f"{trace.source_point_count} points · {raw_label} · "
            f"{trace.acquired_at_utc or 'missing time'} · "
            f"peak {max(trace.powers_dbm):.4g} dBm{processed_note}"
        )
        self._update_nav_buttons()

        self._publish_spectrum()

    def _processing_changed(self, _state=None) -> None:
        kind = self._read_context.get(self._active_read_request, (None,))[0]
        if ((kind == "checkpoints" and not self._public_checkpoints)
                or (kind == "private_details" and self._selected_private_spectrum is None)
                or (kind == "reference" and self._selected_reference is None)):
            # The new processing state applies when the first spectrum is ready;
            # it must not cancel the prerequisite checkpoint catalogue.
            return
        pending_public = self._read_context.get(self._active_read_request, (None,))[0] == "public"
        self._invalidate_pending_reads()
        self.spectrum_variant_combo.setEnabled(not self.processing_controls.state.active
            and not self.view_controls.state.active and self._selected_private_spectrum is not None)
        if self.processing_controls.state.active or self.view_controls.state.active:
            blocked = self.spectrum_variant_combo.blockSignals(True)
            self.spectrum_variant_combo.setCurrentIndex(max(0, self.spectrum_variant_combo.findData("raw")))
            self.spectrum_variant_combo.blockSignals(blocked)
        if self._selected_private_point is not None:
            if self._selected_private_spectrum is None:
                reader = self._session.spectrum if self._session else Hdf5RunReader.spectrum
                self._start_read("private", self._selected_private_point, reader,
                                 self._selected_path, self._selected_private_point.index)
            else:
                self._render_selected_private_variant()
        elif self._public_spectrum is not None:
            self._render_thatec_trace()
        elif self._selected_reference is not None:
            self._render_reference(self._selected_reference.index, self._selected_reference)
        elif pending_public:
            self._load_selected_thatec_spectrum()

    def _render_postprocessed(self, result: ProcessedResultSpectrum) -> None:
        self.spectrum_plot.prepare_traces(("Post-processed spectrum",))
        self.spectrum_plot.set_labels(x="Frequency", x_unit="Hz", y="Post-processed amplitude", y_unit=result.unit)
        self.spectrum_plot.set_trace("Post-processed spectrum", result.frequencies_hz, result.values, primary=True,
                                     signed_difference=result.unit == "dB" or
                                     self.processing_controls.state.math_operation == "subtract_power_signed")
        self.spectrum_plot.set_title("Recorded spectrum - post-processing")
        operation_label = self.processing_controls.operation.currentText().split(" — ")[0]
        if self._selected_private_point is not None:
            self.spectrum_plot.set_title(f"Checkpoint {self._selected_private_point.index} · {operation_label}")
        elif self._public_spectrum is not None:
            self.spectrum_plot.set_title(f"Checkpoint {self._public_spectrum.checkpoint} · {operation_label}")
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        message = result.method + ("; " + "; ".join(result.notes) if result.notes else "")
        self.spectrum_info.setText(message)
        self.spectrum_info.setToolTip(message)
        self._update_nav_buttons()
        self.status_changed.emit(message)
        self._publish_spectrum(extra={"recorded_baselines": [processing_manifest(r) for r in result.baselines]})

    def _render_view(self, payload, *, title=None, saved_display=None):
        unit = payload.traces[-1].unit
        self.spectrum_plot.set_labels(x="Frequency", x_unit="Hz", y="Power / contrast", y_unit=unit)
        labels = {trace.label for trace in payload.traces}
        for name in tuple(self.spectrum_plot._traces):
            if name not in labels and name not in {"Max hold", "Min hold"}:
                self.spectrum_plot.clear_trace(name)
        theme = tokens_for(self.spectrum_plot._theme_name)
        palette = plot_theme(theme)
        colours = (palette.measurement, palette.reference, theme.success, theme.caution, theme.neutral, theme.focus)
        for index, trace in enumerate(payload.traces):
            self.spectrum_plot.set_trace(trace.label, trace.frequencies_hz, trace.values,
                color=colours[index % len(colours)],
                primary=(trace.key == saved_display.primary_key if saved_display and saved_display.primary_key else index == 0),
                legend_label=f"{trace.label} [{trace.unit}]", signed_difference=trace.is_signed_difference,
                absolute_capable=saved_display is None)
        selected_key = saved_display.primary_key if saved_display else None
        primary = next((trace for trace in payload.traces if trace.key == selected_key), payload.traces[0])
        self.spectrum_plot.set_estimated_bands(primary.estimated_bands_hz)
        checkpoint = payload.traces[0].frame_id
        self.spectrum_plot.set_title(title or f"Checkpoint {checkpoint} · recorded spectrum comparison")
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        message = "; ".join(payload.notes) or "Recorded spectrum comparison · original HDF5 unchanged."
        if primary.estimated_bands_hz:
            message += " Shaded bands show estimates from neighbouring frequencies."
        self.spectrum_info.setText(message)
        self.spectrum_info.setToolTip(message)
        self._update_nav_buttons()
        self._publish_spectrum(extra={"recorded_baselines": [processing_manifest(r) for r in payload.baselines],
                                    "traces": [{"label": trace.label, "unit": trace.unit,
                                  "provenance": list(trace.provenance), "estimated_bands_hz": trace.estimated_bands_hz}
                                 for trace in payload.traces]})

    def _open_batch(self):
        row = None
        trace_index = 0
        if self._selected_private_point is not None:
            checkpoints = [(point.index, point.setpoints) for point in self._stored_points if point.has_spectrum]
        elif self._public_spectrum is not None and self._run is not None:
            row = self._run.rows.get(self._public_spectrum.row_id)
            if row is None or self.thatec_trace_combo.currentData() is None:
                return
            trace_index = int(self.thatec_trace_combo.currentData())
            stored = {point.index: point.setpoints for point in self._stored_points or self._public_checkpoints}
            checkpoints = [(index, stored.get(index)) for index in range(row.shape[0])]
        else:
            return
        state = SpectrumBatch(self._stored_points, self.processing_controls.state, self.view_controls.state,
                              self.spectrum_plot.show_absolute.isChecked(),
                              self.spectrum_variant_combo.currentData() or "raw", row, trace_index)
        items = plan_spectra(checkpoints)
        if items:
            self.batch_requested.emit(self._batch_identity, items, state, self.spectrum_plot.plot.grab())

    def _publish_spectrum(self, extra=None):
        average = recorded_instrument_average(self._selected_private_point or self._public_point())
        self.spectrum_plot.export_metadata = {
            "source_file": str(self._selected_path),
            "checkpoint": (self._selected_private_point.index if self._selected_private_point else
                           self._public_spectrum.checkpoint if self._public_spectrum else None),
            "spectrum_processing": processing_manifest(self.processing_controls.state),
            "view": processing_manifest(self.view_controls.state),
            "series_constraints": list(self.series_controls.constraints),
            "notes": self.spectrum_info.text(), **(extra or {}),
        }
        if average is not None:
            self.spectrum_plot.export_metadata["instrument_average"] = average
        if self.baseline_controls.viewing and self._selected_reference is not None:
            reference = self._selected_reference
            effective = replace(self.processing_controls.state, operation="none", reference_index=None,
                reference_sweep=None, parameters=replace(self.processing_controls.state.parameters,
                                                        temporal_average_frames=1))
            self.spectrum_plot.export_metadata.update(
                standalone_baseline=processing_manifest(RecordedBaselineEvidence.from_reference(reference)),
                spectrum_processing_effective=processing_manifest(effective),
                checkpoint_correction_applied=False)
        identity = self.spectrum_plot.export_metadata["checkpoint"]
        self.spectrum_plot.batch_button.setEnabled(identity is not None and not self.baseline_controls.viewing)
        if identity is None and self._selected_reference is not None:
            reference = self._selected_reference
            identity = f"{reference.purpose}:{reference.index}:{reference.selected_sweep if reference.selected_sweep is not None else 'mean'}"
        self.spectrum_plot.register_frame(identity)
        self.spectrum_plot.sync_floating()
        if hasattr(self, "peak_tools"):
            self.peak_tools.spectrum_changed()
        self._update_analysis_overlays()

    def _go_previous(self) -> None:
        self._go_spectrum(-1)

    def _go_next(self) -> None:
        self._go_spectrum(1)

    def _spectrum_rows(self) -> tuple[int, ...]:
        return tuple(i for i, p in enumerate(self.points_model.records)
                     if not isinstance(p, StoredPoint) or p.has_spectrum)

    def _navigation_rows(self) -> tuple[tuple[int, ...] | range, bool]:
        rows = self._spectrum_rows()
        if rows:
            return rows, True
        return range(len(self.points_model.records)), False

    def _go_spectrum(self, direction: int) -> None:
        current = self.points.currentIndex()
        if not current.isValid():
            return
        index = current.row()
        candidates, _has_spectra = self._navigation_rows()
        if not candidates:
            return
        if isinstance(candidates, range):
            target = index + direction
            if 0 <= target < len(candidates):
                self.points.setCurrentIndex(self.points_model.index(target, 0))
            return
        pos = bisect_right(candidates, index) if direction > 0 else bisect_left(candidates, index) - 1
        if 0 <= pos < len(candidates):
            self.points.setCurrentIndex(self.points_model.index(candidates[pos], 0))

    def _update_nav_buttons(self) -> None:
        if self.baseline_controls.viewing:
            self._stop_playback()
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.play_button.setEnabled(False)
            self.checkpoint_slider.setEnabled(False)
            if self._selected_reference is not None:
                reference = self._selected_reference
                self.position_label.setText(f"{reference.purpose.title()} {reference.index} · {baseline_sample_label(reference)}")
            return
        current = self.points.currentIndex()
        if not current.isValid():
            self._stop_playback()
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.play_button.setEnabled(False)
            self.checkpoint_slider.setEnabled(False)
            self.position_label.setText("No spectrum selected")
            return
        index = current.row()
        rows, has_spectra = self._navigation_rows()
        self.prev_button.setEnabled(bool(rows) and rows[0] < index)
        self.next_button.setEnabled(bool(rows) and rows[-1] > index)
        self.play_button.setEnabled(len(rows) > 1)
        slider_pos = index if isinstance(rows, range) else bisect_left(rows, index)
        blocked = self.checkpoint_slider.blockSignals(True)
        self.checkpoint_slider.setRange(0, max(0, len(rows) - 1))
        self.checkpoint_slider.setValue(max(0, min(slider_pos, max(0, len(rows) - 1))))
        self.checkpoint_slider.setEnabled(len(rows) > 1)
        self.checkpoint_slider.blockSignals(blocked)
        record = current.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if isinstance(record, (StoredPoint, PublicCheckpoint)):
            if has_spectra:
                pos = bisect_left(rows, index)
                display_pos = pos + 1 if pos < len(rows) and rows[pos] == index else "—"
                self.position_label.setText(
                    f"Spectrum {display_pos} / {len(rows)} · checkpoint {record.index}"
                )
            else:
                self.position_label.setText(
                    f"Checkpoint {index + 1} / {len(rows)} · checkpoint {record.index}"
                )
            values = dict(self._record_parameter_signature(record))
            self.position_label.setToolTip(self._format_signature(tuple(values.items())))
            self.filter_summary.setToolTip(self._format_signature(tuple(values.items())))

    def _update_variant_selector(self, trace: StoredSpectrum | None) -> None:
        raw = recorded_spectrum_label(self._selected_private_point, "Raw spectrum",
                                      trace_name=trace.trace_name if trace is not None else "")
        options: list[tuple[str, str]] = [(f"{raw} (dBm)", "raw")]
        if trace is not None and trace.saved_display is not None:
            options.append(("As saved on Anritsu", "saved_view"))
        if trace is not None and trace.processed_values is not None:
            options.extend(
                [
                    ("Processed spectrum", "processed"),
                ]
            )
            if trace.processed_unit == "dBm":
                options.append(("Raw + processed", "raw_processed"))
        reference_index = (
            trace.reference_index if trace is not None else None
        )
        if self._selected_path is not None and reference_index is not None:
            options.extend([
                ("Reference spectrum", "reference"),
                ("Raw + reference", "raw_reference"),
            ])
        current = self._preferred_variant
        if self.processing_controls.state.active or self.view_controls.state.active:
            current = "raw"
        self.spectrum_variant_combo.blockSignals(True)
        self.spectrum_variant_combo.clear()
        for label, value in options:
            self.spectrum_variant_combo.addItem(label, userData=value)
        selected = self.spectrum_variant_combo.findData(current)
        self.spectrum_variant_combo.setCurrentIndex(max(selected, 0))
        self.spectrum_variant_combo.setEnabled(trace is not None and not self.processing_controls.state.active
                                               and not self.view_controls.state.active)
        self.spectrum_variant_combo.blockSignals(False)

    def _reset_variant_selector(self) -> None:
        self.spectrum_variant_combo.blockSignals(True)
        self.spectrum_variant_combo.clear()
        self.spectrum_variant_combo.addItem("Raw spectrum (dBm)", userData="raw")
        self.spectrum_variant_combo.setCurrentIndex(0)
        self.spectrum_variant_combo.setEnabled(False)
        self.spectrum_variant_combo.blockSignals(False)

    def _clear_spectrum(self) -> None:
        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Select a stored or public spectrum")
        self._reset_variant_selector()
        self._show_spectrum_state(
            "Select a spectrum",
            "Spectra are read from HDF5 without contacting instruments.",
        )

    def _start_read(
        self,
        kind: str,
        context: object,
        operation: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        self.spectrum_plot.batch_button.setEnabled(False)
        self._read_request += 1
        request_id = self._read_request
        self._active_read_request = request_id
        task = ResultReadTask(request_id, operation, *args, **kwargs)
        self._read_tasks[request_id] = task
        self._read_context[request_id] = (kind, context)
        task.signals.loaded.connect(self._read_loaded)
        task.signals.failed.connect(self._read_failed)
        task.signals.finished.connect(self._read_finished)
        if kind in {"post", "view", "baseline_view"}:
            filters = [box.text() for box in self.processing_controls.filters.values() if box.isChecked()]
            self.processing_overlay.begin(request_id, "Recalculating spectrum…",
                " · ".join(filters) or "Applying the selected correction and spectrum view.")
        self._read_pool.start(task)

    def _read_loaded(self, request_id: int, payload: object) -> None:
        if request_id != self._active_read_request:
            return
        kind, context = self._read_context.get(request_id, ("", None))
        if kind == "checkpoints":
            self._public_checkpoints = payload
            self._populate_thatec_rows()
            self._populate_points(payload)
            self._populate_parameter_filters()
            if not self.points_model.rowCount() and self.thatec_row_combo.count():
                self._load_selected_thatec_spectrum()
            elif not self.points_model.rowCount():
                self._clear_spectrum()
        elif kind == "private_details":
            point, trace = payload
            self._selected_private_point = point
            self.device_state_changed.emit(point.device_states)
            if trace is None:
                self._show_spectrum_state("No spectrum at this checkpoint", "This checkpoint contains scalar data only.")
            else:
                self._render_stored_spectrum(point, trace)
        elif kind == "reference":
            self._render_reference(context, payload)
        elif kind == "variant_reference":
            if payload is None:
                self._show_spectrum_state("Reference spectrum unavailable", "No complete reference is stored for this checkpoint.")
            else:
                self._selected_reference = payload
                self._render_selected_private_variant()
        elif kind == "post" and isinstance(payload, ProcessedResultSpectrum):
            self._render_postprocessed(payload)
        elif kind == "view" and isinstance(payload, SpectrumViewPayload):
            self._render_view(payload)
        elif kind == "baseline_view" and isinstance(payload, SpectrumViewPayload):
            baseline = payload.baselines[0]
            self._render_view(payload, title=f"Recorded {baseline.purpose} {baseline.index} · {baseline_sample_label(baseline)}")
        elif kind == "public" and isinstance(payload, ThatecSpectrum):
            self._accept_public_spectrum(payload)
        elif (
            kind == "private"
            and isinstance(context, StoredPoint)
            and isinstance(payload, StoredSpectrum)
        ):
            self._render_stored_spectrum(context, payload)
        elif kind == "private" and payload is None:
            self._show_spectrum_state(
                "Stored spectrum missing",
                "No complete spectrum is stored for this checkpoint.",
            )
        else:
            self._show_spectrum_state(
                "Unsupported spectrum result",
                "The reader returned data that cannot be plotted safely.",
            )

    def _read_failed(self, request_id: int, message: str) -> None:
        if request_id != self._active_read_request:
            return
        kind, _context = self._read_context.get(request_id, ("", None))
        title = (
            "Cannot read public checkpoint metadata" if kind == "checkpoints" else
            "Cannot read public THATEC/PyThat spectrum"
            if kind == "public"
            else "Cannot read stored spectrum"
        )
        self._show_spectrum_error(title, message)

    def _read_finished(self, request_id: int) -> None:
        self._read_tasks.pop(request_id, None)
        self._read_context.pop(request_id, None)
        self.processing_overlay.finish(request_id)

    def _invalidate_pending_reads(self) -> None:
        self.processing_overlay.clear()
        for task in self._read_tasks.values():
            task.cancel()
        self._read_pool.clear()
        self._read_tasks.clear()
        self._read_context.clear()
        self._read_request += 1
        self._active_read_request = self._read_request

    def _show_spectrum_state(
        self, title: str, description: str, *, loading: bool = False
    ) -> None:
        self.spectrum_plot.batch_button.setEnabled(False)
        self.spectrum_state.show_state(
            title=title,
            description=description,
            accessible_name=title,
            loading=loading,
        )
        # During a read/processing job keep the last confirmed trace visible.
        # Its title identifies the old checkpoint until the new one is ready.
        if not loading or not self.spectrum_plot._traces:
            self.spectrum_view.setCurrentWidget(self.spectrum_state)
        self.spectrum_info.setText(description)
        self.spectrum_info.setToolTip(description)

    def _show_spectrum_error(self, title: str, error: object) -> None:
        # Preserve the input so Reset can recover after a correction error.
        self.thatec_trace_combo.setEnabled(self._public_spectrum is not None)
        self.show_thatec_button.setEnabled(
            self.thatec_row_combo.currentData() is not None
        )
        message = str(error)
        self._show_spectrum_state(title, message)
        self.status_changed.emit(f"{title}: {message}")

    def _show_plot_status(self, message: str) -> None:
        self.spectrum_info.setText(message)
        self.status_changed.emit(message)

    @staticmethod
    def _point_tooltip(point: StoredPoint | PublicCheckpoint) -> str:
        import json

        if isinstance(point, PublicCheckpoint):
            return json.dumps(
                {
                    "checkpoint": point.index,
                    "timestamp_utc": point.timestamp_utc,
                    "parameters": point.values,
                    "spectrum_rows": point.spectrum_rows,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        payload = {
            "setpoints": point.setpoints,
            "measurements": point.measurements,
            "metadata": point.metadata,
            "device_states": point.device_states,
        }
        if not point.details_loaded:
            payload["details"] = "Select this checkpoint in the recorded-data tree to read full metadata and device states."
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
