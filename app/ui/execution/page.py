"""Execution monitoring page independent of device UI."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path

import numpy as np

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    TreeWidget,
    isDarkTheme,
)

from app.ui.common import human_duration as _human_duration
from app.ui.dialogs import StationDialog
from app.ui.design_system import tokens_for
from app.ui.widgets import SpectrumPlotWidget
from app.recipes.parameter_registry import PARAMETERS_BY_TARGET
from app.recipes.semantic_tree import AxisPointContext, SemanticMeasurementTree, SemanticNodeKind
from app.domain.quantities import DIMENSION_TIME, format_quantity_auto
from app.domain.execution_state import SemanticOperationState
from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeView, TreeInteractionMode


@dataclass(slots=True)
class ExecutionUiMetrics:
    """Counters used to prove bounded presentation work during long runs."""

    semantic_events_received: int = 0
    model_flushes: int = 0
    tree_rebuilds: int = 0
    preview_flushes: int = 0
    log_rows_rendered: int = 0
    max_pending_semantic: int = 0
    semantic_events_coalesced: int = 0
    max_tree_update_duration_s: float = 0.0
    max_preview_update_duration_s: float = 0.0


@dataclass(slots=True)
class ExecutionPresentationBuffer:
    """Latest-state buffer for non-safety execution presentation streams."""

    latest_semantic: dict[str, SemanticOperationState] = field(default_factory=dict)
    latest_device_snapshot: dict[str, object] | None = None
    latest_preview: dict[str, object] | None = None
    log_events: deque[tuple[str, dict[str, object]]] = field(default_factory=deque)
    metrics: ExecutionUiMetrics = field(default_factory=ExecutionUiMetrics)

    def submit(self, name: str, payload: object) -> None:
        if name.startswith("semantic_operation_"):
            state = payload
            if isinstance(state, SemanticOperationState):
                if state.semantic_id in self.latest_semantic:
                    self.metrics.semantic_events_coalesced += 1
                self.latest_semantic[state.semantic_id] = state
                self.metrics.semantic_events_received += 1
                self.metrics.max_pending_semantic = max(
                    self.metrics.max_pending_semantic, len(self.latest_semantic)
                )
        elif name in {"spectrum_preview", "reference_preview"} and isinstance(payload, dict):
            self.latest_preview = dict(payload)
        elif isinstance(payload, dict):
            self.log_events.append((name, dict(payload)))

    def pop_semantic(self) -> tuple[SemanticOperationState, ...]:
        values = tuple(self.latest_semantic.values())
        self.latest_semantic.clear()
        if values:
            self.metrics.model_flushes += 1
        return values


class ManualStageDialog(StationDialog):
    """Non-modal operator gate for one recipe action in manual-stage mode."""

    next_requested = Signal()
    abort_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manual sweep stage")
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setModal(False)
        self.setMinimumWidth(470)
        self._allow_close = False
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=10)
        title = StrongBodyLabel("Manual execution", surface)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.stage = StrongBodyLabel("Preparing next stage…", surface)
        self.stage.setWordWrap(True)
        layout.addWidget(self.stage)
        self.details = BodyLabel("The runner waits for your confirmation.", surface)
        self.details.setObjectName("muted")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        hint = BodyLabel(
            "The device operation itself still waits for its readback/completion. "
            "Finally and emergency shutdown do not wait for this dialog.",
            surface,
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        self.abort_button = PushButton("Abort safely", surface)
        self.next_button = PrimaryPushButton("Next stage", surface)
        buttons.addWidget(self.abort_button)
        buttons.addStretch(1)
        buttons.addWidget(self.next_button)
        layout.addLayout(buttons)
        self.next_button.clicked.connect(self._next)
        self.abort_button.clicked.connect(self._abort)

    def waiting(self, data: dict[str, object]) -> None:
        position = int(data.get("action_index", 0)) + 1
        total = int(data.get("total_actions", 0))
        self.stage.setText(f"Stage {position}/{total}: {data.get('kind', 'operation')}")
        self.details.setText(
            f"Node: {data.get('node_id', '—')}\n"
            f"Setpoints: {RunMonitorPage._format_setpoints(data.get('setpoints_si'))}"
        )
        self.next_button.setText("Execute this stage")
        self.next_button.setEnabled(True)
        self.show()
        self.raise_()
        self.activateWindow()

    def confirmed(self) -> None:
        self.next_button.setEnabled(False)
        self.next_button.setText("Executing…")

    def finish(self) -> None:
        self._allow_close = True
        self.hide()
        self._allow_close = False

    def _next(self) -> None:
        self.confirmed()
        self.next_requested.emit()

    def _abort(self) -> None:
        self.next_button.setEnabled(False)
        self.abort_button.setEnabled(False)
        self.details.setText("Stopping safely and running final cleanup…")
        self.abort_requested.emit()

    def closeEvent(self, event: object) -> None:
        if self._allow_close:
            event.accept()  # type: ignore[union-attr]
            return
        self._abort()
        event.ignore()  # type: ignore[union-attr]


class RunMonitorPage(QWidget):
    stop_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()
    manual_next_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # The page is hosted by a vertical Fluent scroll area.  Ignore the
        # desktop width hint while retaining a preferred content height so the
        # host can provide a real scrollbar instead of forcing child widgets
        # to paint outside the viewport.
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(9)
        self.hero_card = CardWidget(self)
        hero_layout = QHBoxLayout(self.hero_card)
        hero_layout.setContentsMargins(18, 11, 18, 11)
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
        # Keep the active device and setpoint in a stable, high-salience row.
        # The tree and event stream are valuable context, but neither should
        # be required to answer "what is the instrument doing right now?".
        self.current_operation_card = CardWidget(self)
        self.current_operation_card.setObjectName("executionCurrentOperationCard")
        self.current_operation_card.setProperty("stationSurface", "surface")
        operation_layout = QGridLayout(self.current_operation_card)
        operation_layout.setContentsMargins(16, 9, 16, 9)
        operation_layout.setHorizontalSpacing(12)
        operation_layout.setVerticalSpacing(1)
        self.activity_indicator = StrongBodyLabel("○", self.current_operation_card)
        self.activity_indicator.setObjectName("executionActivityIndicator")
        self.activity_indicator.setProperty("activityPulse", "off")
        self.activity_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.activity_indicator.setMinimumWidth(28)
        self.activity_indicator.setToolTip(
            "The highlighted row in the measurement tree follows this operation."
        )
        operation_layout.addWidget(self.activity_indicator, 0, 0, 2, 1)
        self.current_operation_phase = CaptionLabel(
            "WAITING FOR FIRST ACTION", self.current_operation_card
        )
        self.current_operation_phase.setObjectName("executionOperationPhase")
        operation_layout.addWidget(self.current_operation_phase, 0, 1)
        self.current_operation_device = StrongBodyLabel(
            "Waiting for first action", self.current_operation_card
        )
        self.current_operation_device.setObjectName("executionOperationDevice")
        self.current_operation_device.setMinimumWidth(0)
        self.current_operation_device.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        operation_layout.addWidget(self.current_operation_device, 1, 1)
        self.current_operation_parameter = BodyLabel(
            "No parameter selected", self.current_operation_card
        )
        self.current_operation_parameter.setObjectName("executionOperationParameter")
        self.current_operation_parameter.setMinimumWidth(0)
        self.current_operation_parameter.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        operation_layout.addWidget(self.current_operation_parameter, 0, 2)
        self.current_operation_detail = CaptionLabel(
            "The active device and setpoint will appear here.",
            self.current_operation_card,
        )
        self.current_operation_detail.setObjectName("muted")
        self.current_operation_detail.setMinimumWidth(0)
        self.current_operation_detail.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        operation_layout.addWidget(self.current_operation_detail, 1, 2)
        self.current_operation_value = StrongBodyLabel("—", self.current_operation_card)
        self.current_operation_value.setObjectName("executionOperationValue")
        self.current_operation_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.current_operation_value.setMinimumWidth(130)
        operation_layout.addWidget(self.current_operation_value, 0, 3)
        self.current_operation_si = CaptionLabel("SI —", self.current_operation_card)
        self.current_operation_si.setObjectName("muted")
        self.current_operation_si.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        operation_layout.addWidget(self.current_operation_si, 1, 3)
        self.current_operation_state = BodyLabel("WAITING", self.current_operation_card)
        self.current_operation_state.setObjectName("executionOperationState")
        self.current_operation_state.setProperty("deviceState", "active")
        self.current_operation_state.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        operation_layout.addWidget(self.current_operation_state, 0, 4, 2, 1)
        operation_layout.setColumnStretch(1, 2)
        operation_layout.setColumnStretch(2, 3)

        self.monitor_card = CardWidget(self)
        self.monitor_card.setObjectName("executionControlCard")
        monitor_layout = QVBoxLayout(self.monitor_card)
        monitor_layout.setContentsMargins(18, 10, 18, 10)
        monitor_layout.setSpacing(6)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(14)
        for label in (self.heartbeat, self.eta, self.total_estimate):
            label.setMinimumWidth(0)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            meta_row.addWidget(label, 1)
        telemetry = QGridLayout()
        telemetry.setHorizontalSpacing(14)
        telemetry.setVerticalSpacing(2)
        self.current_path = BodyLabel("Current node: —")
        self.current_path.setWordWrap(True)
        self.current_setpoints = BodyLabel("Setpoints (SI): —")
        self.current_setpoints.setWordWrap(True)
        self.current_measurements = BodyLabel("Measurements (SI): —")
        self.current_measurements.setWordWrap(True)
        self.storage_rate = BodyLabel("Storage: —")
        self.storage_rate.setWordWrap(True)
        for label in (
            self.current_path,
            self.current_setpoints,
            self.current_measurements,
            self.storage_rate,
        ):
            label.setMinimumWidth(0)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
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
        self.events.setMaximumBlockCount(500)
        self.events.setUndoRedoEnabled(False)
        self.events.setMinimumHeight(86)
        self.events.setMaximumHeight(132)
        # The semantic Fluent model/view is the sole execution procedure tree.
        # Small item-based tables below remain flat state manifests; they do
        # not reconstruct recipe structure or own execution progress.
        self.tree_model = MeasurementTreeModel()
        self.tree_model.unknown_semantic_state.connect(
            self._report_unknown_semantic_operation
        )
        self.measurement_tree = MeasurementTreeView(self)
        self.measurement_tree.setObjectName("executionMeasurementTree")
        self.measurement_tree.setModel(self.tree_model)
        self.measurement_tree.set_interaction_mode(TreeInteractionMode.READ_ONLY)
        self.measurement_tree.setMinimumHeight(260)
        self._semantic_tree: SemanticMeasurementTree | None = None
        self.ui_metrics = ExecutionUiMetrics()
        # The buffer owns the cadence, while the page exposes one shared
        # metrics object for diagnostics and qualification tests.  Keeping a
        # single counter source prevents a queued state from being counted
        # once on ingress and again when it is painted.
        self.presentation_buffer = ExecutionPresentationBuffer(metrics=self.ui_metrics)
        self._semantic_state_by_id: dict[str, SemanticOperationState] = {}
        self.warnings = PlainTextEdit(self)
        self.warnings.setReadOnly(True)
        self.warnings.setProperty("stationSurface", "raised")
        self.warnings.setMaximumBlockCount(200)
        self.warnings.setUndoRedoEnabled(False)
        self.warnings.setMinimumHeight(70)
        self.warnings.setMaximumHeight(88)
        self.warnings.setPlaceholderText("No run warnings.")
        self.warnings.hide()
        self.spectrum_preview = SpectrumPlotWidget(
            legend=False, compact_toolbar=True
        )
        self.spectrum_preview.setMinimumWidth(0)
        self.spectrum_preview.setMinimumHeight(240)
        self.spectrum_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.spectrum_preview.set_labels(
            x="Frequency",
            x_unit="Hz",
            y="Amplitude",
            y_unit="dBm",
        )
        self.spectrum_preview.set_title("Latest stored spectrum checkpoint")
        self.monitor_splitter = QSplitter(Qt.Orientation.Vertical)
        self.monitor_splitter.setObjectName("executionMonitorSplitter")
        self.monitor_splitter.setChildrenCollapsible(False)
        self.activity_splitter = QSplitter(Qt.Orientation.Vertical)
        self.activity_splitter.setMinimumWidth(0)
        self.activity_splitter.setMinimumHeight(300)
        self.activity_splitter.addWidget(self.measurement_tree)
        self.activity_splitter.addWidget(self.events)
        self.activity_splitter.setStretchFactor(0, 5)
        self.activity_splitter.setStretchFactor(1, 1)
        self.activity_splitter.setSizes((230, 90))
        self.monitor_splitter.addWidget(self.activity_splitter)
        self.monitor_splitter.addWidget(self.spectrum_preview)
        self.monitor_splitter.setMinimumHeight(300)
        self.monitor_splitter.setStretchFactor(0, 6)
        self.monitor_splitter.setStretchFactor(1, 5)
        # Keep the construction-time vertical layout modest.  QSplitter uses
        # its last sizes when calculating the page size hint; the old 600/700
        # seed made a freshly-created page reserve more than a 900 px desktop
        # viewport before the responsive orientation pass ran.
        self.monitor_splitter.setSizes((300, 300))
        self.workspace_card = CardWidget(self)
        self.workspace_card.setObjectName("executionWorkspaceCard")
        self.workspace_card.setProperty("stationSurface", "surface")
        workspace_layout = QVBoxLayout(self.workspace_card)
        workspace_layout.setContentsMargins(12, 8, 12, 10)
        workspace_layout.setSpacing(5)
        workspace_header = QHBoxLayout()
        workspace_header.setSpacing(8)
        workspace_title = StrongBodyLabel("Measurement workspace", self.workspace_card)
        workspace_title.setObjectName("executionSectionTitle")
        workspace_header.addWidget(workspace_title)
        workspace_hint = BodyLabel(
            "The highlighted tree row is the operation shown above.",
            self.workspace_card,
        )
        workspace_hint.setObjectName("muted")
        workspace_hint.setMinimumWidth(0)
        workspace_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        workspace_header.addWidget(workspace_hint, 1)
        workspace_layout.addLayout(workspace_header)
        workspace_layout.addWidget(self.monitor_splitter, 1)
        self.completion_card = CardWidget(self)
        self.completion_card.setObjectName("executionCompletionCard")
        self.completion_card.setProperty("deviceState", "verified")
        completion_layout = QHBoxLayout(self.completion_card)
        completion_layout.setContentsMargins(20, 14, 20, 14)
        completion_copy = QVBoxLayout()
        completion_copy.setSpacing(4)
        self.completion_title = StrongBodyLabel("Measurement completed", self.completion_card)
        self.completion_title.setObjectName("pageTitle")
        completion_copy.addWidget(self.completion_title)
        self.completion_summary = BodyLabel("", self.completion_card)
        self.completion_summary.setWordWrap(True)
        completion_copy.addWidget(self.completion_summary)
        self.completion_path = BodyLabel("", self.completion_card)
        self.completion_path.setObjectName("muted")
        self.completion_path.setWordWrap(True)
        self.completion_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        completion_copy.addWidget(self.completion_path)
        completion_layout.addLayout(completion_copy, 1)
        self.open_result_folder_button = PushButton("Open result folder", self.completion_card)
        self.open_result_folder_button.setEnabled(False)
        self.open_result_folder_button.clicked.connect(self._open_result_folder)
        completion_layout.addWidget(self.open_result_folder_button)
        self.completion_card.hide()
        layout.addWidget(self.hero_card)
        layout.addWidget(self.completion_card)
        layout.addWidget(self.current_operation_card)
        monitor_layout.addLayout(meta_row)
        monitor_layout.addLayout(telemetry)
        monitor_layout.addLayout(progress_header)
        monitor_layout.addWidget(self.progress)
        monitor_layout.addLayout(controls)
        self.live_state_card = CardWidget(self)
        live_layout = QVBoxLayout(self.live_state_card)
        live_layout.setContentsMargins(20, 14, 20, 14)
        live_layout.setSpacing(8)
        live_layout.addWidget(StrongBodyLabel("Live execution state", self.live_state_card))
        live_copy = BodyLabel(
            "Requested values are shown immediately; Applied and OUTPUT states "
            "change only after an engine-confirmed adapter result.",
            self.live_state_card,
        )
        live_copy.setObjectName("muted")
        live_copy.setWordWrap(True)
        live_layout.addWidget(live_copy)
        self.live_tables = QSplitter(Qt.Orientation.Horizontal, self.live_state_card)
        self.live_tables.setChildrenCollapsible(False)
        self.live_tables.setMinimumHeight(82)
        self.output_states = TreeWidget(self.live_tables)
        self.output_states.setObjectName("executionOutputStates")
        self.output_states.setHeaderLabels(("Used output", "Confirmed state", "Updated"))
        self.output_states.setRootIsDecorated(False)
        self.output_states.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.output_states.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.output_states.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.output_states.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.active_parameters = TreeWidget(self.live_tables)
        self.active_parameters.setObjectName("executionActiveParameters")
        self.active_parameters.setHeaderLabels(("Changing parameter", "Requested", "Applied", "State"))
        self.active_parameters.setRootIsDecorated(False)
        self.active_parameters.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.active_parameters.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.active_parameters.header().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.live_tables.setStretchFactor(0, 1)
        self.live_tables.setStretchFactor(1, 2)
        self.live_tables.setSizes((360, 680))
        live_layout.addWidget(self.live_tables)
        self.live_state_card.setMinimumHeight(145)
        self.live_state_card.setMaximumHeight(180)
        layout.addWidget(self.monitor_card)
        layout.addWidget(self.workspace_card, 1)
        layout.addWidget(self.live_state_card)
        layout.addWidget(self.warnings)
        self.pause_button.clicked.connect(self._request_pause)
        self.resume_button.clicked.connect(self._request_resume)
        self.stop_button.clicked.connect(self._request_safe_stop)
        self._eta_started = 0.0
        self._paused_started = 0.0
        self._paused_total_s = 0.0
        self._model_duration_s = 0.0
        self._planned_actions = 0
        self._stored_points = 0
        self._run_active = False
        self._active_wait_node_id: str | None = None
        self._active_wait_duration_s: float | None = None
        self._active_wait_position = ""
        self._dry_run = False
        self._manual_stage_mode = False
        self._manual_dialog = ManualStageDialog(self)
        self._manual_dialog.next_requested.connect(self.manual_next_requested)
        self._manual_dialog.abort_requested.connect(self._request_safe_stop)
        self._last_operation_repolish_at = 0.0
        self._operation_repolish_interval_s = 0.08
        self._output_items: dict[str, QTreeWidgetItem] = {}
        self._parameter_items: dict[str, QTreeWidgetItem] = {}
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._update_eta)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(200)
        self._preview_timer.timeout.connect(self._flush_spectrum_preview)
        self._pending_spectrum_preview: dict[str, object] | None = None
        self._semantic_flush_timer = QTimer(self)
        self._semantic_flush_timer.setSingleShot(True)
        # A 10 Hz presentation cadence is fast enough for a live active-row
        # animation, while avoiding a model/dataChanged pass for every tiny
        # burst of worker events.  The durable Runner event stream is not
        # throttled; only this non-safety projection is coalesced.
        self._semantic_flush_timer.setInterval(100)
        self._semantic_flush_timer.timeout.connect(self._flush_semantic_states)
        self._pending_semantic_states: dict[str, SemanticOperationState] = {}
        self._activity_pulse_on = False
        self._activity_pulse_timer = QTimer(self)
        self._activity_pulse_timer.setInterval(550)
        self._activity_pulse_timer.timeout.connect(self._pulse_activity_indicator)
        self._last_layout_orientation: Qt.Orientation | None = None
        self._update_monitor_layout(force=True)

    def _update_monitor_layout(self, *, force: bool = False) -> None:
        """Keep the tree, plot and confirmed-state tables usable at each width."""

        orientation = (
            Qt.Orientation.Horizontal
            if self.width() >= 900
            else Qt.Orientation.Vertical
        )
        orientation_changed = force or orientation != self._last_layout_orientation
        if orientation_changed:
            self._last_layout_orientation = orientation
            self.monitor_splitter.setOrientation(orientation)
            if orientation == Qt.Orientation.Horizontal:
                # The plot and tree sit side by side at desktop widths.  Keep
                # the workspace card bounded so the current-operation card and
                # active tree remain in the first viewport; the tree/plot
                # retain their own scroll/zoom affordances for deeper inspection.
                self.workspace_card.setMaximumHeight(440)
                available = max(760, self.monitor_splitter.width())
                # The tree carries four semantic columns.  Give it a modest
                # majority of the workspace so operation and active-value
                # labels remain readable while the plot keeps a useful
                # 400+ px canvas.
                left = max(560, int(available * 0.62))
                self.monitor_splitter.setSizes((left, max(400, available - left)))
            else:
                # At narrow widths the plot follows the tree vertically.  A
                # larger cap leaves both surfaces useful while still avoiding
                # the old construction-time 600/700 splitter reservation.
                self.workspace_card.setMaximumHeight(760)
                available = max(560, self.monitor_splitter.height())
                upper = max(300, int(available * 0.52))
                self.monitor_splitter.setSizes((upper, max(260, available - upper)))
        live_orientation = (
            Qt.Orientation.Horizontal
            if self.width() >= 900
            else Qt.Orientation.Vertical
        )
        if orientation_changed:
            self.live_tables.setOrientation(live_orientation)
            if live_orientation == Qt.Orientation.Horizontal:
                self.live_state_card.setMinimumHeight(145)
                self.live_state_card.setMaximumHeight(180)
                self.live_tables.setMinimumHeight(82)
                self.live_tables.setSizes((360, max(440, self.live_tables.width() - 380)))
            else:
                self.live_state_card.setMinimumHeight(210)
                self.live_state_card.setMaximumHeight(245)
                self.live_tables.setMinimumHeight(150)
                self.live_tables.setSizes((110, 110))
            if self.activity_splitter.height() > 0:
                event_height = min(112, max(86, self.activity_splitter.height() // 4))
                self.activity_splitter.setSizes(
                    (max(220, self.activity_splitter.height() - event_height), event_height)
                )

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._update_monitor_layout()

    @staticmethod
    def _axis_context_from_event(value: object) -> AxisPointContext | None:
        if isinstance(value, AxisPointContext):
            return value
        if not isinstance(value, dict):
            return None
        try:
            active = value.get("active_setpoints_si", {})
            if not isinstance(active, dict):
                active = {}
            loop_path = value.get("loop_path", ())
            if not isinstance(loop_path, (list, tuple)):
                loop_path = ()
            return AxisPointContext(
                str(value["axis_id"]),
                int(value.get("point_index", 0)),
                int(value.get("point_count", 0)),
                int(value.get("stage_index", 0)),
                float(value.get("value_si", 0.0)),
                dict(active),
                tuple(str(item) for item in loop_path),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _semantic_target(self, semantic_id: str) -> str | None:
        tree = self._semantic_tree
        if tree is None:
            return None
        node = tree.by_id.get(semantic_id)
        if node is None:
            return None
        if node.axis is not None:
            return node.axis.target
        parent_id = tree.parent_by_id.get(semantic_id)
        while parent_id:
            parent = tree.by_id.get(parent_id)
            if parent is not None and parent.axis is not None:
                return parent.axis.target
            parent_id = tree.parent_by_id.get(parent_id)
        raw_target = node.data.get("target")
        return str(raw_target) if raw_target else None

    def _semantic_state_from_event(
        self,
        data: dict[str, object],
        *,
        phase: str,
    ) -> SemanticOperationState | None:
        semantic_id = data.get("semantic_id")
        if not isinstance(semantic_id, str) or not semantic_id:
            return None
        try:
            normalized_phase = phase  # Literal is enforced by the event source.
            if normalized_phase not in {"waiting", "running", "applied", "failed", "skipped"}:
                return None
            requested = data.get("requested_si")
            applied = data.get("applied_si")
            readback = data.get("readback_si")
            return SemanticOperationState(
                semantic_id,
                normalized_phase,  # type: ignore[arg-type]
                float(requested) if isinstance(requested, (int, float)) else None,
                float(applied) if isinstance(applied, (int, float)) else None,
                float(readback) if isinstance(readback, (int, float)) else None,
                str(data["verification"]) if data.get("verification") else None,  # type: ignore[arg-type]
                int(data.get("action_index", 0)),
                int(data.get("total_actions", self._planned_actions)),
                self._axis_context_from_event(data.get("axis_context")),
                str(data["kind"]) if data.get("kind") is not None else None,
                str(data["device"]) if data.get("device") is not None else None,
                data.get("channel") if isinstance(data.get("channel"), (str, int)) else None,
                float(data["duration_s"])
                if isinstance(data.get("duration_s"), (int, float))
                and not isinstance(data.get("duration_s"), bool)
                else None,
                str(data["trace"]) if data.get("trace") is not None else None,
                (
                    str(data["reference_operation"])
                    if data.get("reference_operation") is not None
                    else None
                ),
            )
        except (TypeError, ValueError):
            return None

    def _apply_semantic_event(
        self,
        name: str,
        data: dict[str, object],
        *,
        submit_buffer: bool = True,
        update_focus: bool = True,
        apply_model: bool = True,
    ) -> None:
        phases = {
            "semantic_operation_started": "running",
            "semantic_operation_applied": "applied",
            "semantic_operation_failed": "failed",
        }
        phase = phases.get(name)
        if phase is None:
            return
        state = self._semantic_state_from_event(data, phase=phase)
        if state is None:
            return
        if submit_buffer:
            self.presentation_buffer.submit(name, state)
        self._semantic_state_by_id[state.semantic_id] = state
        tree_update_started = time.perf_counter()
        if apply_model:
            self.tree_model.apply_state(state)
        if not update_focus:
            self.ui_metrics.max_tree_update_duration_s = max(
                self.ui_metrics.max_tree_update_duration_s,
                time.perf_counter() - tree_update_started,
            )
            return
        if state.phase == "applied":
            self.progress.setValue(
                min(self.progress.maximum(), max(self.progress.value(), state.action_index + 1))
            )
        if phase in {"running", "failed"}:
            self.measurement_tree.follow_semantic_id(
                state.semantic_id, force=phase == "failed"
            )
        self.ui_metrics.max_tree_update_duration_s = max(
            self.ui_metrics.max_tree_update_duration_s,
            time.perf_counter() - tree_update_started,
        )
        node = (
            self._semantic_tree.by_id.get(state.semantic_id)
            if self._semantic_tree is not None
            else None
        )
        operation_label = node.label if node is not None else state.kind or "operation"
        self.current_path.setText(
            f"Current node: {operation_label} · action "
            f"{state.action_index + 1}/{max(1, state.total_actions)}"
        )
        context = state.axis_context
        if context is not None:
            self.current_setpoints.setText(
                "Setpoints (SI): " + self._format_scalars(context.active_setpoints_si)
            )
        target = self._semantic_target(state.semantic_id)
        is_setpoint = node is not None and node.kind is SemanticNodeKind.SET_ROI_VALUE
        if is_setpoint and target:
            value = (
                state.applied_si
                if state.phase == "applied" and state.applied_si is not None
                else state.requested_si
            )
            descriptor = PARAMETERS_BY_TARGET.get(target)
            self.current_operation_device.setText(self._operation_device(target, data))
            self.current_operation_parameter.setText(
                descriptor.ui_label if descriptor is not None else target
            )
            self.current_operation_value.setText(self._format_parameter(target, value))
            self.current_operation_si.setText(self._format_si_value(target, value))
        elif node is not None or data.get("kind"):
            # Configuration, acquisition and wait rows are operations in their
            # own right. They replace the previous setpoint in the prominent
            # card instead of leaving a stale value on screen.
            kind = str(data.get("kind", "operation"))
            display_target = (
                target
                if target and context is not None and not data.get("device")
                else ""
            )
            self.current_operation_device.setText(
                self._operation_device(display_target, data)
            )
            self.current_operation_parameter.setText(
                node.label if node is not None else kind.replace("_", " ").title()
            )
            duration = data.get("duration_s")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                self.current_operation_value.setText(
                    format_quantity_auto(float(duration), DIMENSION_TIME)
                )
                self.current_operation_si.setText(f"SI {float(duration):.6g} s")
            else:
                self.current_operation_value.setText("—")
                self.current_operation_si.setText("SI —")
        if context is not None:
            self.current_operation_detail.setText(
                f"Point {context.point_index + 1}/{context.point_count} · "
                f"stage {context.stage_index + 1} · {data.get('kind', 'operation')}"
            )
        elif node is not None:
            self.current_operation_detail.setText(str(data.get("kind", node.kind.value)))
        self.current_operation_phase.setText(
            self._operation_phase(str(data.get("kind", "set point")))
        )
        operation_kind = str(data.get("kind", "")).lower()
        if operation_kind == "wait" and phase == "running":
            duration = data.get("duration_s")
            self._active_wait_node_id = (
                str(data["node_id"]) if data.get("node_id") is not None else None
            )
            self._active_wait_duration_s = (
                float(duration)
                if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                else None
            )
            if self._active_wait_duration_s is not None:
                self._active_wait_position = (
                    f"Point {context.point_index + 1}/{context.point_count} · "
                    if context is not None
                    else ""
                )
                self.current_operation_detail.setText(
                    f"{self._active_wait_position}WAIT in progress · "
                    f"{format_quantity_auto(self._active_wait_duration_s, DIMENSION_TIME)} remaining"
                )
        elif operation_kind == "wait" and phase == "applied":
            duration = self._active_wait_duration_s
            self._active_wait_node_id = None
            self._active_wait_duration_s = None
            self._active_wait_position = ""
            if duration is not None:
                prefix = (
                    f"Point {context.point_index + 1}/{context.point_count} · "
                    if context is not None
                    else ""
                )
                self.current_operation_detail.setText(
                    f"{prefix}WAIT completed · "
                    f"{format_quantity_auto(duration, DIMENSION_TIME)} elapsed"
                )
        elif phase == "running":
            self._active_wait_node_id = None
            self._active_wait_duration_s = None
            self._active_wait_position = ""
        running_state = (
            "WAITING"
            if "wait" in operation_kind
            else "MEASURING"
            if any(word in operation_kind for word in ("acquire", "measure", "spectrum"))
            else "SETTING"
        )
        self._set_current_operation_state(
            running_state
            if phase == "running"
            else "CONFIRMED"
            if phase == "applied"
            else "FAILED"
        )

    def value_for(self, semantic_id: str) -> str:
        return self.tree_model.value_for(semantic_id)

    def _report_unknown_semantic_operation(self, semantic_id: str) -> None:
        """Expose a bounded diagnostic instead of guessing a tree parent."""

        self.warnings.show()
        self.warnings.appendPlainText(
            "Engine-generated operation is absent from the accepted semantic "
            f"snapshot: {semantic_id}. The technical event remains durable."
        )

    def run_started(
        self,
        actions: int,
        estimated_duration_s: float = 0.0,
        *,
        plan_actions: object = (),
        recipe_source: str | None = None,
        execution_mode: str = "measurement",
        semantic_tree: SemanticMeasurementTree | None = None,
    ) -> None:
        self._preview_timer.stop()
        self._pending_spectrum_preview = None
        self._semantic_flush_timer.stop()
        self._pending_semantic_states.clear()
        self._activity_pulse_timer.stop()
        self._activity_pulse_on = False
        self._set_activity_indicator("○", "off")
        self._semantic_state_by_id.clear()
        self.ui_metrics = ExecutionUiMetrics()
        self.presentation_buffer = ExecutionPresentationBuffer(metrics=self.ui_metrics)
        # MainWindow supplies the exact accepted preflight snapshot. Direct UI
        # diagnostics may omit it, but they still receive the same empty
        # semantic model rather than a second item-based interpretation.
        self._semantic_tree = semantic_tree or SemanticMeasurementTree(
            (), {}, source_text=recipe_source or ""
        )
        self.tree_model.replace_tree(self._semantic_tree)
        self.tree_model.set_read_only(True)
        self._run_active = True
        self.completion_card.hide()
        self.completion_summary.clear()
        self.completion_path.clear()
        self.open_result_folder_button.setEnabled(False)
        dry_run = execution_mode == "dry_run"
        manual = execution_mode == "manual_step"
        self._dry_run = dry_run
        self._manual_stage_mode = manual
        if manual:
            self.state.setText("MANUAL — PREPARING")
            self.state.setToolTip(
                "The runner pauses before every normal recipe action until the "
                "operator explicitly confirms the next stage."
            )
        else:
            self.state.setText("DRY RUN — OUTPUTS OFF" if dry_run else "RUNNING")
            self.state.setToolTip(
                (
                    "Configurations, setpoints and acquisitions are executing while "
                    "every source output is forced OFF."
                )
                if dry_run
                else "Normal measurement execution; recipe OUTPUT actions are active."
            )
        self._planned_actions = max(0, actions)
        self._stored_points = 0
        self._active_wait_node_id = None
        self._active_wait_duration_s = None
        self._active_wait_position = ""
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.stop_button.setText("Stop safely")
        self.stop_button.setEnabled(True)
        self.pause_button.setEnabled(not manual)
        self.resume_button.setEnabled(False)
        self.events.clear()
        self._last_operation_repolish_at = 0.0
        self._build_live_manifest(plan_actions)
        if manual:
            self._manual_dialog.abort_button.setEnabled(True)
            self._manual_dialog.next_button.setEnabled(False)
            self._manual_dialog.show()
        self.warnings.clear()
        self.warnings.hide()
        self.spectrum_preview.clear()
        self.current_operation_phase.setText("WAITING FOR FIRST ACTION")
        self.current_operation_device.setText("Waiting for first action")
        self.current_operation_parameter.setText("No parameter selected")
        self.current_operation_detail.setText(
            "The active device and setpoint will appear here."
        )
        self.current_operation_value.setText("—")
        self.current_operation_si.setText("SI —")
        self._set_current_operation_state("WAITING")
        self.current_path.setText("Current node: waiting for first action")
        self.current_setpoints.setText("Setpoints (SI): —")
        self.current_measurements.setText("Measurements (SI): —")
        self.storage_rate.setText("Storage: waiting for first checkpoint")
        self.heartbeat.setText("Heartbeat: waiting for first operation")
        self._eta_started = time.monotonic()
        self._paused_started = 0.0
        self._paused_total_s = 0.0
        self._model_duration_s = max(0.0, estimated_duration_s)
        expected_finish = datetime.now().astimezone() + timedelta(
            seconds=self._model_duration_s
        )
        self.total_estimate.setText(
            f"Plan estimate: {_human_duration(self._model_duration_s)} • "
            f"expected finish: {expected_finish:%H:%M:%S}"
        )
        self._eta_timer.start()
        self._activity_pulse_timer.start()
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

    def _open_result_folder(self) -> None:
        path_text = self.completion_path.text().strip()
        if not path_text:
            return
        output_path = Path(path_text)
        if output_path.parent.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_path.parent)))

    def _request_pause(self) -> None:
        if not self.pause_button.isEnabled():
            return
        self.state.setText("PAUSE REQUESTED")
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.pause_requested.emit()

    def _request_resume(self) -> None:
        if not self.resume_button.isEnabled():
            return
        self.state.setText(
            "DRY RUN — OUTPUTS OFF" if self._dry_run else "RUNNING"
        )
        self.pause_button.setEnabled(True)
        self.resume_button.setEnabled(False)
        self._end_pause()
        self.resume_requested.emit()

    def _begin_pause(self) -> None:
        if not self._paused_started:
            self._paused_started = time.monotonic()

    def _end_pause(self) -> None:
        if self._paused_started:
            self._paused_total_s += max(
                0.0, time.monotonic() - self._paused_started
            )
            self._paused_started = 0.0

    @staticmethod
    def _action_channel(action: object, attribute: str) -> object | None:
        payload = getattr(action, "payload", None)
        if not isinstance(payload, dict):
            return None
        if attribute in payload:
            return payload[attribute]
        for nested_key in ("config", "request"):
            nested = payload.get(nested_key)
            value = getattr(nested, attribute, None)
            if value is not None:
                return value
        return None

    def _build_live_manifest(self, plan_actions: object) -> None:
        """Show only outputs/parameters used by the immutable execution plan."""

        self.output_states.clear()
        self.active_parameters.clear()
        self._output_items.clear()
        self._parameter_items.clear()
        if not isinstance(plan_actions, (tuple, list)):
            return
        endpoints: set[str] = set()
        targets: set[str] = set()
        for action in plan_actions:
            kind = str(getattr(action, "kind", ""))
            channel = self._action_channel(action, "channel")
            if "rigol" in kind and channel in {1, 2}:
                endpoints.add(f"rigol.{channel}")
            if "keithley" in kind and channel in {"A", "B"}:
                endpoints.add(f"keithley.{channel}")
            if "anritsu_sg" in kind:
                endpoints.add("anritsu.sg")
            setpoints = getattr(action, "setpoints_si", None)
            if isinstance(setpoints, dict):
                targets.update(str(target) for target in setpoints)
        for endpoint in sorted(endpoints):
            item = QTreeWidgetItem([self._endpoint_label(endpoint), "UNKNOWN", "Not confirmed"])
            item.setData(0, Qt.ItemDataRole.UserRole, endpoint)
            self.output_states.addTopLevelItem(item)
            self._output_items[endpoint] = item
            self._set_output_item_state(item, "unknown")
        for target in sorted(targets):
            descriptor = PARAMETERS_BY_TARGET.get(target)
            label = descriptor.ui_label if descriptor is not None else target
            item = QTreeWidgetItem([label, "—", "—", "Waiting"])
            item.setData(0, Qt.ItemDataRole.UserRole, target)
            self.active_parameters.addTopLevelItem(item)
            self._parameter_items[target] = item

    @staticmethod
    def _endpoint_label(endpoint: str) -> str:
        return {
            "rigol.1": "Rigol CH1 OUTPUT",
            "rigol.2": "Rigol CH2 OUTPUT",
            "keithley.A": "Keithley Channel A OUTPUT",
            "keithley.B": "Keithley Channel B OUTPUT",
            "anritsu.sg": "Anritsu SG RF OUTPUT",
        }.get(endpoint, endpoint)

    def _set_output_item_state(self, item: QTreeWidgetItem, state: str) -> None:
        normalized = state.lower()
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        color = {
            "on": tokens.success,
            "off": tokens.text_muted,
            "unknown": tokens.caution,
        }.get(normalized, tokens.caution)
        if item.text(1).casefold() != normalized:
            item.setText(1, normalized.upper())
        if item.foreground(1).color().name().casefold() != color.casefold():
            item.setForeground(1, QBrush(QColor(color)))

    @staticmethod
    def _format_parameter(target: str, value: object) -> str:
        descriptor = PARAMETERS_BY_TARGET.get(target)
        if descriptor is None or not isinstance(value, (int, float)):
            return "—" if value is None else str(value)
        return format_quantity_auto(float(value), descriptor.dimension)

    @staticmethod
    def _applied_parameter_value(target: str, device_states: object) -> object | None:
        if not isinstance(device_states, dict):
            return None
        parts = target.split(".")
        if len(parts) < 3:
            return None
        device, channel, parameter = parts[0], parts[1], parts[2]
        section = f"channel_{channel}"
        device_state = device_states.get(device)
        if not isinstance(device_state, dict):
            return None
        record = device_state.get(section)
        if not isinstance(record, dict):
            return None
        actual = record.get("actual")
        if not isinstance(actual, dict):
            return None
        fields = {
            "frequency": "frequency_hz",
            "high_level": "high_level_v",
            "low_level": "low_level_v",
            "current": "source_level_si",
            "voltage": "source_level_si",
            "compliance_current": "compliance_si",
            "compliance_voltage": "compliance_si",
        }
        return actual.get(fields.get(parameter, parameter))

    def _apply_live_snapshot(self, data: dict[str, object]) -> None:
        snapshot = data.get("state_snapshot")
        if not isinstance(snapshot, dict):
            return
        timestamp = str(data.get("timestamp_utc", ""))
        updated = timestamp[11:19] if len(timestamp) >= 19 else "Confirmed"
        statuses = snapshot.get("output_status")
        if isinstance(statuses, dict):
            for endpoint, item in self._output_items.items():
                state = statuses.get(endpoint)
                if isinstance(state, str):
                    self._set_output_item_state(item, state)
                    timestamp = updated if state != "unknown" else "Not confirmed"
                    if item.text(2) != timestamp:
                        item.setText(2, timestamp)
        device_states = snapshot.get("device_states")
        for target, item in self._parameter_items.items():
            applied = self._applied_parameter_value(target, device_states)
            if applied is not None:
                item.setText(2, self._format_parameter(target, applied))
                item.setText(3, "APPLIED")
                item.setForeground(3, self._state_brush("done"))

    def _set_requested_parameters(self, values: object) -> None:
        if not isinstance(values, dict):
            return
        for raw_target, value in values.items():
            target = str(raw_target)
            item = self._parameter_items.get(target)
            if item is None:
                descriptor = PARAMETERS_BY_TARGET.get(target)
                label = descriptor.ui_label if descriptor is not None else target
                item = QTreeWidgetItem([label, "—", "—", "Waiting"])
                item.setData(0, Qt.ItemDataRole.UserRole, target)
                self.active_parameters.addTopLevelItem(item)
                self._parameter_items[target] = item
            requested = self._format_parameter(target, value)
            if item.text(1) != requested:
                item.setText(1, requested)
            if item.text(3) != "PENDING":
                item.setText(3, "PENDING")
                item.setForeground(3, self._state_brush("running"))

    @staticmethod
    def _operation_phase(kind: str) -> str:
        normalized = kind.lower().replace("_", " ")
        if "wait" in normalized:
            return "WAITING"
        if any(word in normalized for word in ("acquire", "measure", "spectrum")):
            return "MEASURING"
        if any(word in normalized for word in ("shutdown", "output off", "off")):
            return "SAFE SHUTDOWN"
        if any(word in normalized for word in ("set", "update", "configure", "ramp")):
            return "SETTING PARAMETER"
        return "RUNNING"

    @staticmethod
    def _operation_device(target: str, data: dict[str, object]) -> str:
        descriptor = PARAMETERS_BY_TARGET.get(target)
        if descriptor is not None:
            parts = target.split(".")
            channel = parts[1] if len(parts) > 1 else ""
            if channel in {"A", "B", "1", "2"}:
                return f"{descriptor.device_name} / Channel {channel}"
            if channel:
                return f"{descriptor.device_name} / {channel.upper()}"
            return descriptor.device_name
        device = str(data.get("device") or target.split(".", 1)[0] or "")
        display_names = {
            "keithley": "Keithley 2602A",
            "rigol": "Rigol DG1032Z",
            "anritsu": "Anritsu MS2830A",
            "moke_box": "MOKE Box",
            "lakeshore_gaussmeter": "Lake Shore 475",
        }
        if not device:
            kind = str(data.get("kind", "")).lower()
            device = next(
                (candidate for candidate in display_names if candidate in kind),
                "device",
            )
        display = display_names.get(device.lower(), device.replace("_", " ").title())
        channel = data.get("channel")
        return f"{display} / Channel {channel}" if channel else display

    @staticmethod
    def _format_si_value(target: str, value: object) -> str:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "SI —" if value is None else f"SI {value}"
        descriptor = PARAMETERS_BY_TARGET.get(target)
        unit = descriptor.unit if descriptor is not None else "SI"
        return f"SI {float(value):.6g} {unit}"

    def _update_current_operation(
        self,
        data: dict[str, object],
        *,
        state: str | None = None,
    ) -> None:
        if data.get("semantic_id"):
            return
        values = data.get("setpoints_si")
        target: str | None = None
        value: object | None = None
        if isinstance(values, dict):
            for raw_target, raw_value in sorted(values.items(), key=lambda pair: str(pair[0])):
                target = str(raw_target)
                value = raw_value
                break
        if target is not None:
            descriptor = PARAMETERS_BY_TARGET.get(target)
            parameter = descriptor.ui_label if descriptor is not None else target
            self.current_operation_device.setText(self._operation_device(target, data))
            self.current_operation_parameter.setText(parameter)
            self.current_operation_value.setText(self._format_parameter(target, value))
            self.current_operation_si.setText(self._format_si_value(target, value))
            extra = max(0, len(values) - 1) if isinstance(values, dict) else 0
            suffix = f" / +{extra} more setpoint" if extra == 1 else (
                f" / +{extra} more setpoints" if extra > 1 else ""
            )
            self.current_operation_detail.setText(
                f"{target} / {str(data.get('kind', 'operation')).replace('_', ' ')}{suffix}"
            )
        elif data.get("kind"):
            kind = str(data.get("kind", "operation")).replace("_", " ")
            self.current_operation_device.setText(
                self._operation_device("", data)
            )
            self.current_operation_parameter.setText(kind.title())
            self.current_operation_detail.setText(
                f"Node {data.get('node_id', '—')}"
            )
            self.current_operation_value.setText("—")
            self.current_operation_si.setText("SI —")
        kind = str(data.get("kind", "operation"))
        self.current_operation_phase.setText(self._operation_phase(kind))
        if state is not None:
            self._set_current_operation_state(state)

    def _set_current_operation_state(self, state: str) -> None:
        if self.current_operation_state.text() != state:
            self.current_operation_state.setText(state)
        normalized = state.lower()
        device_state = (
            "fault"
            if any(word in normalized for word in ("fault", "failed", "timeout"))
            else "verified"
            if any(word in normalized for word in ("complete", "confirmed", "saved"))
            else "caution"
            if any(word in normalized for word in ("waiting", "stopping", "pause"))
            else "active"
        )
        now = time.monotonic()
        current_state = self.current_operation_state.property("deviceState")
        if current_state == device_state:
            return
        # Dynamic-property repolishing is comparatively expensive in
        # QFluentWidgets. Keep the text live for every action but repaint its
        # semantic colour at a bounded cadence; terminal/fault states bypass
        # the cadence so safety feedback is immediate.
        # A confirmed *action* is not a terminal safety boundary: it occurs
        # thousands of times in a long sweep.  Keep the text immediate, but
        # throttle the expensive Fluent dynamic-property repolish just like a
        # running action.  Only run completion and fault states bypass the
        # cadence.
        terminal = device_state == "fault" or state == "COMPLETE"
        if not terminal and now - self._last_operation_repolish_at < self._operation_repolish_interval_s:
            return
        self.current_operation_state.setProperty("deviceState", device_state)
        style = self.current_operation_state.style()
        style.unpolish(self.current_operation_state)
        style.polish(self.current_operation_state)
        self.current_operation_state.update()
        self._last_operation_repolish_at = now

    def _set_activity_indicator(self, marker: str, pulse: str) -> None:
        self.activity_indicator.setText(marker)
        self.activity_indicator.setProperty("activityPulse", pulse)
        style = self.activity_indicator.style()
        style.unpolish(self.activity_indicator)
        style.polish(self.activity_indicator)
        self.activity_indicator.update()

    def _pulse_activity_indicator(self) -> None:
        self._activity_pulse_on = not self._activity_pulse_on
        self._set_activity_indicator(
            "●" if self._activity_pulse_on else "○",
            "on" if self._activity_pulse_on else "off",
        )

    @staticmethod
    def _state_brush(state: str) -> QBrush:
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        color = {
            "running": tokens.accent,
            "done": tokens.success,
            "failed": tokens.danger,
        }.get(state, tokens.text_primary)
        return QBrush(QColor(color))

    def _update_eta(self) -> None:
        if not self._eta_started:
            self.eta.setText("ETA: —")
            self.progress_summary.setText("0 of 0 actions • 0%")
            return
        now = time.monotonic()
        elapsed = max(0.0, now - self._eta_started)
        current_pause_s = (
            max(0.0, now - self._paused_started)
            if self._paused_started
            else 0.0
        )
        active_elapsed = max(
            0.0, elapsed - self._paused_total_s - current_pause_s
        )
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
            active_elapsed / completed * max(0, total - completed)
            if completed
            else 0.0
        )
        model_remaining = max(0.0, self._model_duration_s - active_elapsed)
        remaining = (
            0.0
            if visible_total and completed >= visible_total
            else max(empirical_remaining, model_remaining)
        )
        estimated_total = active_elapsed + remaining
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
        elapsed_s = float(data.get("elapsed_s", 0.0))
        self.heartbeat.setText(
            "Heartbeat: "
            f"{data.get('kind', 'operation')} • attempt {data.get('attempt', '—')} • "
            f"elapsed {elapsed_s:.2f} s • "
            f"remaining {float(data.get('remaining_s', 0.0)):.2f} s"
        )
        if (
            str(data.get("kind", "")).lower() == "wait"
            and self._active_wait_duration_s is not None
            and (
                self._active_wait_node_id is None
                or str(data.get("node_id", "")) == self._active_wait_node_id
            )
        ):
            remaining_s = max(0.0, self._active_wait_duration_s - elapsed_s)
            self.current_operation_detail.setText(
                f"{self._active_wait_position}WAIT in progress · "
                f"{format_quantity_auto(remaining_s, DIMENSION_TIME)} remaining of "
                f"{format_quantity_auto(self._active_wait_duration_s, DIMENSION_TIME)}"
            )

    def queue_spectrum_preview(self, data: dict[str, object]) -> None:
        """Schedule one latest-frame repaint instead of one repaint per point."""

        self._pending_spectrum_preview = dict(data)
        if not self._preview_timer.isActive():
            self._preview_timer.start()

    def queue_semantic_state(self, state: SemanticOperationState) -> None:
        """Coalesce high-rate semantic states to one model flush per cadence."""

        self._queue_semantic_state("semantic_operation_applied", state)

    def queue_semantic_event(self, name: str, data: dict[str, object]) -> None:
        """Queue a typed Runner event in the page-owned presentation buffer."""

        phases = {
            "semantic_operation_started": "running",
            "semantic_operation_applied": "applied",
            "semantic_operation_failed": "failed",
        }
        phase = phases.get(name)
        if phase is None:
            return
        state = self._semantic_state_from_event(data, phase=phase)
        if state is None:
            return
        raw_count = data.get("_coalesced_count", 1)
        try:
            coalesced_count = max(1, int(raw_count))
        except (TypeError, ValueError):
            coalesced_count = 1
        if phase == "failed":
            # Fault feedback is a safety boundary and must not wait behind a
            # visual cadence timer.
            self._apply_semantic_event(name, data)
            if coalesced_count > 1:
                self.ui_metrics.semantic_events_received += coalesced_count - 1
                self.ui_metrics.semantic_events_coalesced += coalesced_count - 1
            return
        self._queue_semantic_state(name, state)
        if coalesced_count > 1:
            self.ui_metrics.semantic_events_received += coalesced_count - 1
            self.ui_metrics.semantic_events_coalesced += coalesced_count - 1

    def _queue_semantic_state(self, name: str, state: SemanticOperationState) -> None:
        self.presentation_buffer.submit(name, state)
        self._pending_semantic_states[state.semantic_id] = state
        self.ui_metrics.max_pending_semantic = max(
            self.ui_metrics.max_pending_semantic,
            len(self._pending_semantic_states),
        )
        if not self._semantic_flush_timer.isActive():
            self._semantic_flush_timer.start()

    def flush_semantic_states(self) -> None:
        """Paint the latest semantic states before a terminal run boundary."""

        self._semantic_flush_timer.stop()
        self._flush_semantic_states()

    def discard_pending_semantic(self) -> None:
        """Drop only stale visual states while retaining durable event metrics."""

        self._semantic_flush_timer.stop()
        self._pending_semantic_states.clear()
        self.presentation_buffer.latest_semantic.clear()

    def _flush_semantic_states(self) -> None:
        # The typed buffer is the source of truth.  The secondary dictionary
        # only tracks pending IDs for diagnostics; it is cleared together
        # with the buffer so one state cannot be painted twice.
        pending = self.presentation_buffer.pop_semantic()
        self._pending_semantic_states.clear()
        if not pending:
            return
        # Several semantic IDs can become ready in the same 80 ms window
        # (setpoint, acquisition, wait, and their loop ancestors).  Suppress
        # intermediate viewport paints while their model roles are updated.
        # The guard must cover ``apply_states`` itself: Qt can synchronously
        # repaint a view for each dataChanged signal otherwise.  One explicit
        # viewport update after the batch keeps the active-row animation
        # smooth without sacrificing per-ID state.
        self.measurement_tree.setUpdatesEnabled(False)
        try:
            # Apply all states before painting the focused operation.  The
            # model emits one dataChanged notification per affected row instead
            # of one notification for every state in this GUI turn.
            model_update_started = time.perf_counter()
            self.tree_model.apply_states(pending)
            self.ui_metrics.max_tree_update_duration_s = max(
                self.ui_metrics.max_tree_update_duration_s,
                time.perf_counter() - model_update_started,
            )
            for state in pending:
                self._semantic_state_by_id[state.semantic_id] = state
            focused = max(
                pending,
                key=lambda state: (state.action_index, state.phase == "failed"),
                default=None,
            )
            if focused is None:
                return
            state = focused
            event = {
                "semantic_id": state.semantic_id,
                "requested_si": state.requested_si,
                "applied_si": state.applied_si,
                "readback_si": state.readback_si,
                "verification": state.verification,
                "action_index": state.action_index,
                "total_actions": state.total_actions,
                "axis_context": (
                    {
                        "axis_id": state.axis_context.axis_id,
                        "point_index": state.axis_context.point_index,
                        "point_count": state.axis_context.point_count,
                        "stage_index": state.axis_context.stage_index,
                        "value_si": state.axis_context.value_si,
                        "active_setpoints_si": dict(state.axis_context.active_setpoints_si),
                        "loop_path": list(state.axis_context.loop_path),
                    }
                    if state.axis_context is not None
                    else None
                ),
                "kind": state.kind or "set point",
            }
            if state.device is not None:
                event["device"] = state.device
            if state.channel is not None:
                event["channel"] = state.channel
            if state.duration_s is not None:
                event["duration_s"] = state.duration_s
            if state.trace is not None:
                event["trace"] = state.trace
            if state.reference_operation is not None:
                event["reference_operation"] = state.reference_operation
            name = {
                "running": "semantic_operation_started",
                "applied": "semantic_operation_applied",
                "failed": "semantic_operation_failed",
            }.get(state.phase, "semantic_operation_applied")
            self._apply_semantic_event(
                name,
                event,
                submit_buffer=False,
                update_focus=True,
                apply_model=False,
            )
        finally:
            self.measurement_tree.setUpdatesEnabled(True)
            self.measurement_tree.viewport().update()

    def _flush_spectrum_preview(self) -> None:
        pending = self._pending_spectrum_preview
        self._pending_spectrum_preview = None
        if pending is not None:
            self.update_spectrum_preview(pending)

    @staticmethod
    def _event_summary(name: str, data: dict[str, object]) -> str:
        """Render a bounded diagnostic line without dumping state arrays."""

        fields: list[str] = []
        for key, value in data.items():
            if key == "state_snapshot":
                rendered = "<confirmed>"
            elif key in {"frequency_hz", "power_dbm"} and isinstance(
                value, (tuple, list)
            ):
                rendered = f"<{len(value)} values>"
            else:
                rendered = repr(value)
                if len(rendered) > 220:
                    rendered = rendered[:217] + "..."
            fields.append(f"{key}={rendered}")
        return f"{name}: " + ", ".join(fields)

    def _should_append_event_log(self, name: str, data: dict[str, object]) -> bool:
        """Keep repetitive action rows out of the bounded text document.

        The tree/current-operation card remains live for every event. The
        event log is a bounded diagnostic surface, not a durable event store;
        retaining one line per repeated loop action can otherwise make Qt spend
        seconds trimming and re-layouting its document while the runner is
        healthy.
        """

        if name == "point_stored" and self._run_active:
            return False
        if name in {
            "action_started",
            "action_finished",
            "recovery_prelude_started",
            "recovery_prelude_finished",
            "safe_resume_boundary",
        }:
            # These high-rate boundaries are already losslessly persisted by
            # the runner.  Rebuilding a QTextDocument line for every point can
            # monopolize the GUI thread (especially when the document trims
            # its maximum block count).  The live tree, storage card and
            # safety/fault lines remain visible; detailed action telemetry is
            # available in the run HDF5 event stream.
            return False
        return True

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name in {
            "semantic_operation_started",
            "semantic_operation_applied",
            "semantic_operation_failed",
        }:
            self._apply_semantic_event(name, data)
            if self._should_append_event_log(name, data):
                self.events.appendPlainText(self._event_summary(name, data))
            return
        if name == "manual_stage_waiting":
            self.state.setText("MANUAL — WAITING FOR NEXT")
            self.current_path.setText(
                f"Manual stage: {data.get('node_id', '—')} • {data.get('kind', '—')}"
            )
            self.current_setpoints.setText(
                "Setpoints (SI): " + self._format_scalars(data.get("setpoints_si"))
            )
            self._set_requested_parameters(data.get("setpoints_si"))
            self._update_current_operation(data, state="AWAITING CONFIRMATION")
            self._manual_dialog.waiting(data)
        elif name == "manual_stage_confirmed":
            self.state.setText("MANUAL — EXECUTING")
            self._manual_dialog.confirmed()
        if name in {"action_finished", "recovery_prelude_finished"}:
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
            self._set_current_operation_state("CONFIRMED")
        elif name in {"action_started", "recovery_prelude_started"}:
            self._end_pause()
            if self._manual_stage_mode:
                self.state.setText("MANUAL — EXECUTING")
            self.state.setText(
                "DRY RUN — OUTPUTS OFF"
                if self._dry_run
                else "RUNNING"
            )
            self.pause_button.setEnabled(not self._manual_stage_mode)
            self.resume_button.setEnabled(False)
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
            self._set_requested_parameters(data.get("setpoints_si"))
            self._update_current_operation(data, state="SETTING")
        elif name == "point_stored":
            raw_stored = data.get("stored_points")
            if isinstance(raw_stored, int) and not isinstance(raw_stored, bool):
                self._stored_points = max(self._stored_points, raw_stored)
            self.current_measurements.setText(
                "Measurements (SI): " + self._format_scalars(data.get("measurements_si"))
            )
            self.storage_rate.setText(
                f"Storage: point {data.get('stored_points', '—')} • "
                f"write {float(data.get('write_elapsed_s', 0.0)) * 1000:.1f} ms • "
                f"average {float(data.get('average_write_rate_points_per_s', 0.0)):.2f} point/s • "
                f"spectrum {data.get('spectrum_points', 0)} values"
            )
            self._set_current_operation_state("CHECKPOINT SAVED")
        if name == "action_failed":
            self._set_current_operation_state("FAILED")
        elif name == "safe_finally_started":
            self._update_current_operation(data, state="SAFE SHUTDOWN")
        elif name == "safe_finally_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
            self._set_current_operation_state("CONFIRMED")
        elif name == "safe_finally_error":
            self._set_current_operation_state("FAILED")
        elif name == "shutdown_action_started":
            self._update_current_operation(
                {"kind": str(data.get("action", "shutdown")), "node_id": "shutdown"},
                state="SAFE SHUTDOWN",
            )
        elif name == "shutdown_action_finished":
            self._set_current_operation_state("CONFIRMED")
        elif name == "shutdown_error":
            self._set_current_operation_state("FAILED")
        if name == "pause_pending":
            self._begin_pause()
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
            self._set_current_operation_state("FAULT")
        elif name == "watchdog_timeout":
            self.state.setText("FAULT • WATCHDOG TIMEOUT")
            self._set_current_operation_state("WATCHDOG TIMEOUT")
        elif name == "run_completed":
            self.progress.setValue(self.progress.maximum())
            self._set_current_operation_state("COMPLETE")
            self._update_eta()
        if name in {
            "action_retry",
            "compliance_detected",
            "run_fault",
            "watchdog_timeout",
            "safe_finally_error",
        }:
            self.warnings.show()
            self.warnings.appendPlainText(self._event_summary(name, data))
        # Action-start snapshots describe the state before the operation and
        # are intentionally skipped here. Confirmed/completed boundaries carry
        # the state that can change the read-only manifest.
        if name in {
            "action_finished",
            "recovery_prelude_finished",
            "safe_finally_finished",
            "shutdown_action_finished",
            "run_completed",
            "run_aborted",
            "run_fault",
        }:
            self._apply_live_snapshot(data)
        if self._should_append_event_log(name, data):
            self.events.appendPlainText(self._event_summary(name, data))

    def update_spectrum_preview(self, data: dict[str, object]) -> None:
        frequencies = data.get("frequency_hz")
        powers = data.get("power_dbm")
        if not isinstance(frequencies, (tuple, list)) or not isinstance(powers, (tuple, list)):
            return
        preview_started = time.perf_counter()
        source_point_count = len(powers)
        if len(frequencies) != source_point_count:
            return
        display_limit = max(512, self.spectrum_preview.width() * 2)
        if source_point_count > display_limit:
            indexes = np.linspace(
                0,
                source_point_count - 1,
                num=display_limit,
                dtype=np.intp,
            )
            frequency_values = np.asarray(frequencies, dtype=float)[indexes]
            power_values = np.asarray(powers, dtype=float)[indexes]
        else:
            frequency_values = frequencies
            power_values = powers
        preview_kind = str(data.get("preview_kind", "measurement"))
        trace_label = (
            "Stored reference" if preview_kind == "reference" else "Stored spectrum"
        )
        self.spectrum_preview.set_trace(
            trace_label,
            frequency_values,
            power_values,
            primary=True,
        )
        title = (
            "Stored reference"
            if preview_kind == "reference"
            else f"Stored point {data.get('point_index', '—')}"
        )
        self.spectrum_preview.set_title(
            title + f" • {data.get('source_points', source_point_count)} source values"
        )
        self.ui_metrics.preview_flushes += 1
        self.ui_metrics.max_preview_update_duration_s = max(
            self.ui_metrics.max_preview_update_duration_s,
            time.perf_counter() - preview_started,
        )

    @staticmethod
    def _format_scalars(value: object) -> str:
        if not isinstance(value, dict) or not value:
            return "—"
        return " • ".join(
            f"{key}={float(number):.6g}" for key, number in sorted(value.items())
        )

    @staticmethod
    def _format_setpoints(value: object) -> str:
        """Format recipe SI values as the same unit-bearing values users edit."""
        if not isinstance(value, dict) or not value:
            return "—"
        rendered: list[str] = []
        for target, number in sorted(value.items()):
            descriptor = PARAMETERS_BY_TARGET.get(str(target))
            if descriptor is None:
                rendered.append(f"{target}: {float(number):.6g}")
                continue
            rendered.append(
                f"{descriptor.ui_label}: "
                f"{format_quantity_auto(float(number), descriptor.dimension)}"
            )
        return " • ".join(rendered)

    def complete(self, result: object) -> None:
        self._run_active = False
        self._preview_timer.stop()
        self._flush_spectrum_preview()
        self._semantic_flush_timer.stop()
        self._flush_semantic_states()
        self._manual_dialog.finish()
        self._eta_timer.stop()
        self._activity_pulse_timer.stop()
        self._activity_pulse_on = False
        self._set_activity_indicator("○", "off")
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        run_result = result["result"]
        self._stored_points = int(getattr(run_result, "stored_points", self._stored_points))
        state = run_result.state.value.upper()
        if run_result.error and state == "SAFE":
            state = "STOPPED SAFELY"
        path = str(result["path"])
        completed = state == "SAFE" and not run_result.error
        self.completion_title.setText(
            "Measurement completed — data saved"
            if completed
            else "Run stopped safely — confirmed data saved"
        )
        self.completion_summary.setText(
            f"{run_result.stored_points} committed point(s). "
            "The measurement file was closed and is ready to open."
            if completed
            else f"{run_result.stored_points} committed point(s) were retained; "
            "the run ended before normal completion."
        )
        self.completion_path.setText(path)
        self.open_result_folder_button.setEnabled(Path(path).parent.exists())
        self.completion_card.show()
        self.state.setText(f"{state} • {run_result.stored_points} points")
        self._set_current_operation_state("COMPLETE" if completed else state)
        self.events.appendPlainText(f"File: {result['path']}")

    def failed(self, error: str) -> None:
        self._run_active = False
        self._preview_timer.stop()
        self._flush_spectrum_preview()
        self._semantic_flush_timer.stop()
        self._flush_semantic_states()
        self._manual_dialog.finish()
        self._eta_timer.stop()
        self._activity_pulse_timer.stop()
        self._activity_pulse_on = False
        self._set_activity_indicator("!", "off")
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.state.setText("FAULT")
        self._set_current_operation_state("FAULT")
        self.events.appendPlainText(error)
