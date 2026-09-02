"""Execution monitoring page independent of device UI."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl, Signal
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
from app.recipes import parse_recipe_text
from app.recipes.models import RecipeNode
from app.recipes.parameter_registry import PARAMETERS_BY_TARGET
from app.domain.quantities import format_quantity_auto


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
        self.steps = TreeWidget(self)
        self.steps.setObjectName("executionSteps")
        self.steps.setHeaderLabels(
            ("Measurement sequence", "Role / expansion", "Status")
        )
        self.steps.setRootIsDecorated(True)
        self.steps.setAlternatingRowColors(True)
        self.steps.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.steps.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.steps.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.steps.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.steps.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.steps.setMinimumHeight(220)
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
        self.activity_splitter.addWidget(self.steps)
        self.activity_splitter.addWidget(self.events)
        self.activity_splitter.setStretchFactor(0, 5)
        self.activity_splitter.setStretchFactor(1, 1)
        self.activity_splitter.setSizes((230, 90))
        self.monitor_splitter.addWidget(self.activity_splitter)
        self.monitor_splitter.addWidget(self.spectrum_preview)
        self.monitor_splitter.setMinimumHeight(300)
        self.monitor_splitter.setStretchFactor(0, 5)
        self.monitor_splitter.setStretchFactor(1, 6)
        self.monitor_splitter.setSizes((600, 700))
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
        self._dry_run = False
        self._manual_stage_mode = False
        self._manual_dialog = ManualStageDialog(self)
        self._manual_dialog.next_requested.connect(self.manual_next_requested)
        self._manual_dialog.abort_requested.connect(self._request_safe_stop)
        self._step_items: dict[str, QTreeWidgetItem] = {}
        self._step_totals: dict[str, int] = {}
        self._step_completed: dict[str, int] = {}
        self._active_step: QTreeWidgetItem | None = None
        self._pending_step_visual: tuple[QTreeWidgetItem, str] | None = None
        self._last_step_visual_at = 0.0
        self._step_visual_interval_s = 0.08
        self._step_visual_timer = QTimer(self)
        self._step_visual_timer.setSingleShot(True)
        self._step_visual_timer.setInterval(80)
        self._step_visual_timer.timeout.connect(self._flush_step_visual)
        self._event_log_last_by_group: dict[tuple[str, str], float] = {}
        self._event_log_sample_interval_s = 0.35
        self._last_operation_repolish_at = 0.0
        self._operation_repolish_interval_s = 0.08
        self._output_items: dict[str, QTreeWidgetItem] = {}
        self._parameter_items: dict[str, QTreeWidgetItem] = {}
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._update_eta)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(100)
        self._preview_timer.timeout.connect(self._flush_spectrum_preview)
        self._pending_spectrum_preview: dict[str, object] | None = None
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
                available = max(760, self.monitor_splitter.width())
                left = max(480, int(available * 0.48))
                self.monitor_splitter.setSizes((left, max(520, available - left)))
            else:
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

    def run_started(
        self,
        actions: int,
        estimated_duration_s: float = 0.0,
        *,
        plan_actions: object = (),
        recipe_source: str | None = None,
        recipe_tree_items: tuple[QTreeWidgetItem, ...] = (),
        execution_mode: str = "measurement",
    ) -> None:
        self._preview_timer.stop()
        self._pending_spectrum_preview = None
        self._activity_pulse_timer.stop()
        self._step_visual_timer.stop()
        self._pending_step_visual = None
        self._last_step_visual_at = 0.0
        self._activity_pulse_on = False
        self._set_activity_indicator("○", "off")
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
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.stop_button.setText("Stop safely")
        self.stop_button.setEnabled(True)
        self.pause_button.setEnabled(not manual)
        self.resume_button.setEnabled(False)
        self.events.clear()
        self._event_log_last_by_group.clear()
        self._last_operation_repolish_at = 0.0
        self._build_step_list(
            plan_actions,
            recipe_source=recipe_source,
            recipe_tree_items=recipe_tree_items,
        )
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

    def _build_step_list(
        self,
        plan_actions: object,
        *,
        recipe_source: str | None = None,
        recipe_tree_items: tuple[QTreeWidgetItem, ...] = (),
    ) -> None:
        """Show the immutable recipe hierarchy, annotated by its action plan.

        The runner reports actions, but their flat order is not the operator's
        procedure.  Rendering from the exact compiled recipe source preserves
        sweep/repeat/conditional nesting and the guaranteed Finally branch.
        """

        self.steps.clear()
        self._step_items.clear()
        self._step_totals.clear()
        self._step_completed.clear()
        self._active_step = None
        if not isinstance(plan_actions, (tuple, list)):
            return
        ordered: list[tuple[str, str, bool]] = []
        seen_node_ids: set[str] = set()
        for action in plan_actions:
            node_id = str(getattr(action, "node_id", ""))
            kind = str(getattr(action, "kind", ""))
            if not node_id:
                continue
            self._step_totals[node_id] = self._step_totals.get(node_id, 0) + 1
            if node_id not in seen_node_ids:
                ordered.append((node_id, kind, bool(getattr(action, "is_finally", False))))
                seen_node_ids.add(node_id)
        if recipe_tree_items:
            self.steps.addTopLevelItems(
                [item.clone() for item in recipe_tree_items]
            )
            self._index_recipe_tree_items()
            self._project_generated_actions(plan_actions)
            self.steps.expandAll()
            return
        if isinstance(recipe_source, str) and recipe_source.strip():
            try:
                recipe = parse_recipe_text(recipe_source, origin="execution plan")
            except Exception:
                # The execution plan is already authoritative and immutable.
                # If the source cannot be presented, retain the legacy action
                # list rather than hiding execution state from the operator.
                recipe = None
            if recipe is not None:
                self._build_recipe_tree(recipe.root, recipe.finally_nodes, ordered)
                self._project_generated_actions(plan_actions)
                self.steps.expandAll()
                return
        for node_id, kind, is_finally in ordered:
            label = node_id.replace("-", " ").replace("_", " ")
            if is_finally:
                label = "Finally · " + label
            total = self._step_totals[node_id]
            status = "○ WAITING" if total == 1 else f"○ 0/{total} WAITING"
            item = QTreeWidgetItem([label, kind.replace("_", " "), status])
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            self.steps.addTopLevelItem(item)
            self._step_items[node_id] = item

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
        item.setText(1, normalized.upper())
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        color = {
            "on": tokens.success,
            "off": tokens.text_muted,
            "unknown": tokens.caution,
        }.get(normalized, tokens.caution)
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
                    item.setText(2, updated if state != "unknown" else "Not confirmed")
        device_states = snapshot.get("device_states")
        for target, item in self._parameter_items.items():
            applied = self._applied_parameter_value(target, device_states)
            if applied is not None:
                item.setText(2, self._format_parameter(target, applied))
                item.setText(3, "APPLIED")
                item.setForeground(3, self._step_brush("done"))

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
            item.setText(1, self._format_parameter(target, value))
            item.setText(3, "PENDING")
            item.setForeground(3, self._step_brush("running"))

    def _index_recipe_tree_items(self) -> None:
        """Map cloned Builder rows to run events without changing their text.

        The third Builder column remains the static preflight status.  Execution
        state is communicated by selection/highlighting and the live monitor,
        preserving an exact visual recipe projection rather than replacing it
        with a second, flatter execution-specific tree.
        """

        def visit(item: QTreeWidgetItem) -> None:
            node = item.data(0, Qt.ItemDataRole.UserRole)
            node_id = getattr(node, "id", None)
            if isinstance(node_id, str) and node_id:
                self._step_items[node_id] = item
                item.setData(0, int(Qt.ItemDataRole.UserRole) + 98, True)
            structural = item.data(0, int(Qt.ItemDataRole.UserRole) + 41)
            if isinstance(structural, str) and structural.startswith(
                "automatic_shutdown:"
            ):
                action = structural.removeprefix("automatic_shutdown:")
                if action:
                    shutdown_id = f"shutdown:{action}"
                    self._step_items[shutdown_id] = item
                    self._step_totals.setdefault(shutdown_id, 1)
            generated_id = item.data(0, int(Qt.ItemDataRole.UserRole) + 99)
            if isinstance(generated_id, str) and generated_id:
                self._step_items[generated_id] = item
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            for index in range(item.childCount()):
                visit(item.child(index))

        for index in range(self.steps.topLevelItemCount()):
            visit(self.steps.topLevelItem(index))

    def _project_generated_actions(self, plan_actions: object) -> None:
        """Place compiler-generated actions under their source loop owner.

        Device-module recipes intentionally generate action IDs such as
        ``keithley-...update-level`` and ``keithley-...settle`` while compiling
        each ROI. Those IDs are not editable :class:`RecipeNode` IDs, so a
        naive event handler used to create them as new top-level rows. Resolve
        the longest source-node prefix and attach each unique generated action
        to that node's execution container instead.
        """

        if not isinstance(plan_actions, (tuple, list)):
            return
        structural_role = int(Qt.ItemDataRole.UserRole) + 41
        source_marker_role = int(Qt.ItemDataRole.UserRole) + 97
        generated_marker_role = int(Qt.ItemDataRole.UserRole) + 99
        node_items: dict[str, QTreeWidgetItem] = {}
        execution_items: dict[str, QTreeWidgetItem] = {}

        def visit(item: QTreeWidgetItem, owner_id: str | None = None) -> None:
            raw_node = item.data(0, Qt.ItemDataRole.UserRole)
            current_owner = owner_id
            node_id = getattr(raw_node, "id", None)
            if node_id is None and item.data(0, source_marker_role) is True:
                node_id = raw_node
            if isinstance(node_id, str) and node_id:
                node_items[node_id] = item
                current_owner = node_id
            if item.data(0, structural_role) == "execution" and current_owner:
                execution_items[current_owner] = item
            for index in range(item.childCount()):
                visit(item.child(index), current_owner)

        for index in range(self.steps.topLevelItemCount()):
            visit(self.steps.topLevelItem(index))
        if not node_items:
            return

        candidates = tuple(sorted(node_items, key=len, reverse=True))
        generated: set[str] = set()
        unresolved: list[tuple[str, str]] = []
        unresolved_ids: set[str] = set()

        for action in plan_actions:
            node_id = str(getattr(action, "node_id", ""))
            kind = str(getattr(action, "kind", ""))
            if not node_id or node_id in self._step_items or node_id in generated:
                continue
            owner_id = next(
                (
                    candidate
                    for candidate in candidates
                    if node_id.startswith(candidate + ".")
                ),
                None,
            )
            if owner_id is None:
                if node_id not in unresolved_ids:
                    unresolved.append((node_id, kind))
                    unresolved_ids.add(node_id)
                continue
            owner_item = node_items[owner_id]
            suffix = node_id[len(owner_id) + 1 :]
            execution_item = execution_items.get(owner_id)
            inside_loop = execution_item is not None and self._generated_action_is_looped(
                kind, suffix
            )
            parent = execution_item if inside_loop else owner_item
            label = self._generated_action_label(kind)
            total = self._step_totals.get(node_id, 1)
            detail = (
                f"Per-point operation • {total} execution(s)"
                if inside_loop
                else (
                    "After ROI loop • safe output transition"
                    if suffix.endswith("output-off")
                    else f"Generated execution action • {total} execution(s)"
                )
            )
            item = QTreeWidgetItem([label, detail, self._waiting_status(total)])
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            item.setData(0, generated_marker_role, node_id)
            item.setFlags(
                item.flags()
                & ~Qt.ItemFlag.ItemIsDragEnabled
                & ~Qt.ItemFlag.ItemIsDropEnabled
                & ~Qt.ItemFlag.ItemIsEditable
            )
            item.setToolTip(
                0,
                f"Compiler-generated action {node_id}. "
                + (
                    "Runs inside the ROI loop."
                    if inside_loop
                    else "Runs at the device block boundary."
                ),
            )
            if inside_loop:
                parent.addChild(item)
            elif execution_item is not None:
                # Configuration/output-on actions precede the loop container;
                # output-off actions remain visibly after it.
                if suffix.endswith("output-off"):
                    parent.addChild(item)
                else:
                    # Re-read the execution index for each insertion. Adding
                    # at that index keeps every pre-loop action before the
                    # boundary while preserving first-seen plan order.
                    position = parent.indexOfChild(execution_item)
                    parent.insertChild(max(0, position), item)
            else:
                parent.addChild(item)
            self._step_items[node_id] = item
            generated.add(node_id)

        if unresolved:
            # Recovery/prelude actions can be synthesized without a source
            # RecipeNode. Keep them visible in one explicit engine branch
            # instead of allowing the event handler to create flat rows.
            prelude = QTreeWidgetItem(
                [
                    "Engine-generated actions",
                    "Actions without a source-node owner",
                    "FLOW",
                ]
            )
            prelude.setData(0, structural_role, "recovery")
            prelude.setFlags(prelude.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            for node_id, kind in unresolved:
                item = QTreeWidgetItem(
                    [
                        self._generated_action_label(kind),
                        "Recovery/prelude operation",
                        self._waiting_status(self._step_totals.get(node_id, 1)),
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, node_id)
                item.setData(0, generated_marker_role, node_id)
                item.setFlags(
                    item.flags()
                    & ~Qt.ItemFlag.ItemIsDragEnabled
                    & ~Qt.ItemFlag.ItemIsDropEnabled
                    & ~Qt.ItemFlag.ItemIsEditable
                )
                prelude.addChild(item)
                self._step_items[node_id] = item
            self.steps.insertTopLevelItem(0, prelude)

    @staticmethod
    def _generated_action_is_looped(kind: str, suffix: str) -> bool:
        """Classify generated action IDs by the boundary that owns them."""

        normalized_kind = kind.casefold()
        normalized_suffix = suffix.casefold()
        if normalized_suffix.startswith("configure"):
            return False
        if normalized_suffix.startswith("output") or normalized_kind.endswith(
            "_output"
        ):
            return False
        return True

    @staticmethod
    def _generated_action_label(kind: str) -> str:
        if not kind:
            return "Generated operation"
        return kind.replace("_", " ").strip().title()

    def _build_recipe_tree(
        self,
        root: RecipeNode,
        finally_nodes: tuple[RecipeNode, ...],
        ordered_actions: list[tuple[str, str, bool]],
    ) -> None:
        """Project every editable recipe node into the read-only run tree."""

        rendered: set[str] = set()

        def add_node(
            node: RecipeNode, parent: QTreeWidgetItem | None = None
        ) -> QTreeWidgetItem:
            total = self._step_totals.get(node.id, 0)
            detail = node.type.replace("_", " ")
            if total:
                detail += f" • {total} action{'s' if total != 1 else ''}"
            item = QTreeWidgetItem(
                [self._recipe_node_label(node), detail, self._waiting_status(total)]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, node.id)
            item.setData(0, int(Qt.ItemDataRole.UserRole) + 97, True)
            if parent is None:
                self.steps.addTopLevelItem(item)
            else:
                parent.addChild(item)
            self._step_items[node.id] = item
            rendered.add(node.id)
            child_parent = item
            # Keep the execution boundary explicit. A sweep's children are
            # evaluated once for every ROI point; they are not sibling steps
            # of the sweep itself. This source-only fallback mirrors the
            # Builder projection used by the normal snapshot path.
            if self._uses_execution_container(node):
                execution_label = (
                    "For each ROI point"
                    if node.type == "sweep" or node.data.get("device_module")
                    else f"Repeated steps × {node.data.get('count', '?')}"
                )
                execution = QTreeWidgetItem(
                    [execution_label, "Executable child steps", "FLOW"]
                )
                execution.setData(
                    0,
                    int(Qt.ItemDataRole.UserRole) + 41,
                    "execution",
                )
                execution.setFlags(
                    execution.flags() & ~Qt.ItemFlag.ItemIsDragEnabled
                )
                execution.setToolTip(
                    0,
                    "Only blocks placed in this branch execute for every loop point.",
                )
                item.addChild(execution)
                child_parent = execution
            for child in node.children:
                add_node(child, child_parent)
            if node.else_children:
                alternative = QTreeWidgetItem(
                    ["Else branch", "Conditional alternative", "○ WAITING"]
                )
                alternative.setData(
                    0,
                    int(Qt.ItemDataRole.UserRole) + 41,
                    "else",
                )
                alternative.setFlags(
                    alternative.flags() & ~Qt.ItemFlag.ItemIsSelectable
                )
                item.addChild(alternative)
                for child in node.else_children:
                    add_node(child, alternative)
            return item

        add_node(root)
        if finally_nodes:
            cleanup = QTreeWidgetItem(
                ["Finally — safe shutdown", "Guaranteed after success, stop or fault", "○ WAITING"]
            )
            cleanup.setData(
                0,
                int(Qt.ItemDataRole.UserRole) + 41,
                "finally",
            )
            self.steps.addTopLevelItem(cleanup)
            for node in finally_nodes:
                add_node(node, cleanup)

        # Recovery prelude actions are generated by the runner and do not
        # necessarily exist in the source tree. Keep them visible rather than
        # falsely attaching them to an unrelated recipe node.
        extras = [
            (node_id, kind, is_finally)
            for node_id, kind, is_finally in ordered_actions
            if node_id not in rendered
            and not any(node_id.startswith(source_id + ".") for source_id in rendered)
        ]
        if extras:
            prelude = QTreeWidgetItem(
                ["Recovery prelude", "Re-establishing confirmed safe boundary", "○ WAITING"]
            )
            prelude.setData(0, Qt.ItemDataRole.UserRole, "recovery")
            self.steps.insertTopLevelItem(0, prelude)
            for node_id, kind, _is_finally in extras:
                item = QTreeWidgetItem(
                    [node_id.replace("_", " "), kind.replace("_", " "), self._waiting_status(self._step_totals[node_id])]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, node_id)
                prelude.addChild(item)
                self._step_items[node_id] = item

    @staticmethod
    def _uses_execution_container(node: RecipeNode) -> bool:
        """Return whether source children execute inside a loop boundary."""

        if node.type in {"sweep", "repeat"}:
            return True
        if not node.data.get("device_module"):
            return False
        actions = node.data.get("parameter_actions", [])
        return isinstance(actions, list) and any(
            isinstance(action, dict)
            and (action.get("mode") == "sweep" or bool(action.get("segments")))
            for action in actions
        )

    @staticmethod
    def _waiting_status(total: int) -> str:
        return "○ WAITING" if total <= 1 else f"○ 0/{total} WAITING"

    @staticmethod
    def _recipe_node_label(node: RecipeNode) -> str:
        """Use concise labels while retaining every node in the source tree."""

        if node.type == "sequence":
            return "Measurement sequence"
        if node.type == "sweep":
            return f"Sweep · {node.data.get('target', 'parameter')}"
        if node.type == "repeat":
            return f"Repeat × {node.data.get('count', '?')}"
        if node.type == "if":
            return "Conditional branch"
        if node.type == "comment":
            text = str(node.data.get("text", "Comment")).strip()
            return f"Comment · {text}" if text else "Comment"
        device = node.data.get("device_module") or node.data.get("device")
        if device:
            return f"{str(device).replace('_', ' ').title()} · {node.type.replace('_', ' ')}"
        return node.type.replace("_", " ").title()

    def _step_item(self, node_id: str, kind: str) -> QTreeWidgetItem:
        item = self._step_items.get(node_id)
        if item is not None:
            return item
        item = QTreeWidgetItem(
            [node_id.replace("_", " "), kind.replace("_", " "), "○ WAITING"]
        )
        item.setData(0, Qt.ItemDataRole.UserRole, node_id)
        self.steps.addTopLevelItem(item)
        self._step_items[node_id] = item
        self._step_totals[node_id] = 1
        return item

    def _mark_step(self, node_id: str, kind: str, state: str) -> None:
        item = self._step_item(node_id, kind)
        total = self._step_totals.get(node_id, 1)
        if state == "running":
            self._active_step = item
            self._queue_step_visual(item, state)
            return
        if state == "done":
            completed = min(total, self._step_completed.get(node_id, 0) + 1)
            self._step_completed[node_id] = completed
            self._active_step = None
            # The final completion is always rendered immediately; interim
            # loop counts can be sampled at the display cadence.
            self._queue_step_visual(item, state, force=completed >= total)
            return
        self._active_step = item
        self._queue_step_visual(item, "failed", force=True)

    def _queue_step_visual(
        self,
        item: QTreeWidgetItem,
        state: str,
        *,
        force: bool = False,
    ) -> None:
        """Render tree state at a bounded cadence while keeping logic live."""

        now = time.monotonic()
        active_changed = self._active_step is item and self.steps.currentItem() is not item
        if force or active_changed or now - self._last_step_visual_at >= self._step_visual_interval_s:
            self._pending_step_visual = None
            self._step_visual_timer.stop()
            self._render_step_visual(item, state)
            self._last_step_visual_at = now
            return
        self._pending_step_visual = (item, state)
        if not self._step_visual_timer.isActive():
            self._step_visual_timer.start()

    def _flush_step_visual(self) -> None:
        pending = self._pending_step_visual
        self._pending_step_visual = None
        if pending is None:
            return
        item, state = pending
        self._render_step_visual(item, state)
        self._last_step_visual_at = time.monotonic()

    def _render_step_visual(self, item: QTreeWidgetItem, state: str) -> None:
        static_builder_row = bool(item.data(0, int(Qt.ItemDataRole.UserRole) + 98))
        if state == "running":
            if not static_builder_row:
                item.setText(2, "RUNNING")
            item.setToolTip(2, "Execution state: RUNNING")
            self._set_step_state(item, state)
            # Selecting and scrolling a QTreeWidget forces a layout pass. A
            # sweep re-enters the same action node thousands of times, so only
            # follow the row when the active node actually changes.
            if self.steps.currentItem() is not item:
                self.steps.setCurrentItem(item)
                self.steps.scrollToItem(
                    item, QAbstractItemView.ScrollHint.EnsureVisible
                )
            return
        if state == "done":
            raw_node_id = item.data(0, Qt.ItemDataRole.UserRole)
            node_id = str(getattr(raw_node_id, "id", raw_node_id))
            total = self._step_totals.get(
                node_id, 1
            )
            completed = self._step_completed.get(node_id, 0)
            if not static_builder_row:
                item.setText(2, "✓ DONE" if total == 1 else f"✓ {completed}/{total}")
            item.setToolTip(2, "Execution state: DONE")
            self._set_step_state(item, state)
            return
        if not static_builder_row:
            item.setText(2, "✕ FAILED")
        item.setToolTip(2, "Execution state: FAILED")
        self._set_step_state(item, "failed")
        if self.steps.currentItem() is not item:
            self.steps.setCurrentItem(item)
            self.steps.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)

    def event(self, event: QEvent) -> bool:
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        } and hasattr(self, "steps"):
            self._refresh_step_colours()
        return super().event(event)

    def _set_step_state(self, item: QTreeWidgetItem, state: str) -> None:
        item.setData(2, Qt.ItemDataRole.UserRole, state)
        item.setForeground(2, self._step_brush(state))

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
        terminal = device_state == "fault" or state in {"COMPLETE", "CONFIRMED", "CHECKPOINT SAVED"}
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

    def _refresh_step_colours(self) -> None:
        def visit(item: QTreeWidgetItem) -> None:
            state = item.data(2, Qt.ItemDataRole.UserRole)
            if isinstance(state, str):
                item.setForeground(2, self._step_brush(state))
            for child_index in range(item.childCount()):
                visit(item.child(child_index))

        for index in range(self.steps.topLevelItemCount()):
            visit(self.steps.topLevelItem(index))

    @staticmethod
    def _step_brush(state: str) -> QBrush:
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        color = {
            "running": tokens.accent,
            "done": tokens.success,
            "failed": tokens.danger,
        }.get(state, tokens.text_primary)
        return QBrush(QColor(color))

    def _mark_shutdown(self, action: str, state: str) -> None:
        node_id = f"shutdown:{action}"
        item = self._step_items.get(node_id)
        if item is None:
            item = QTreeWidgetItem(
                ["Emergency shutdown", action.replace(".", " · "), "○ WAITING"]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            structural_role = int(Qt.ItemDataRole.UserRole) + 41
            finally_parent: QTreeWidgetItem | None = None

            def find_finally(candidate: QTreeWidgetItem) -> None:
                nonlocal finally_parent
                if finally_parent is not None:
                    return
                if candidate.data(0, structural_role) == "finally":
                    finally_parent = candidate
                    return
                for child_index in range(candidate.childCount()):
                    find_finally(candidate.child(child_index))

            for top_index in range(self.steps.topLevelItemCount()):
                find_finally(self.steps.topLevelItem(top_index))
                if finally_parent is not None:
                    break
            if finally_parent is None:
                finally_parent = QTreeWidgetItem(
                    [
                        "Finally — safe shutdown",
                        "Guaranteed after success, stop or fault",
                        "FLOW",
                    ]
                )
                finally_parent.setData(0, structural_role, "finally")
                finally_parent.setFlags(
                    finally_parent.flags() & ~Qt.ItemFlag.ItemIsDragEnabled
                )
                self.steps.addTopLevelItem(finally_parent)
            finally_parent.addChild(item)
            self._step_items[node_id] = item
            self._step_totals[node_id] = 1
        self._mark_step(node_id, action, state)

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
        self.heartbeat.setText(
            "Heartbeat: "
            f"{data.get('kind', 'operation')} • attempt {data.get('attempt', '—')} • "
            f"elapsed {float(data.get('elapsed_s', 0.0)):.2f} s • "
            f"remaining {float(data.get('remaining_s', 0.0)):.2f} s"
        )

    def queue_spectrum_preview(self, data: dict[str, object]) -> None:
        """Schedule one latest-frame repaint instead of one repaint per point."""

        self._pending_spectrum_preview = dict(data)
        if not self._preview_timer.isActive():
            self._preview_timer.start()

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
        """Sample repetitive action rows before they reach the text document.

        The tree/current-operation card remains live for every event. The
        event log is a bounded diagnostic surface, not a durable event store;
        retaining one line per repeated loop action can otherwise make Qt spend
        seconds trimming and re-layouting its document while the runner is
        healthy.
        """

        if name not in {
            "action_started",
            "action_finished",
            "recovery_prelude_started",
            "recovery_prelude_finished",
        }:
            return True
        group = (str(data.get("node_id", "")), str(data.get("kind", name)))
        now = time.monotonic()
        last = self._event_log_last_by_group.get(group)
        if last is not None and now - last < self._event_log_sample_interval_s:
            return False
        self._event_log_last_by_group[group] = now
        return True

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name == "manual_stage_waiting":
            self.state.setText("MANUAL — WAITING FOR NEXT")
            self._mark_step(
                str(data.get("node_id", "—")),
                str(data.get("kind", "—")),
                "running",
            )
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
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "done"
            )
            self._set_current_operation_state("CONFIRMED")
            self._update_eta()
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
            self._mark_step(
                str(data.get("node_id", "—")),
                str(data.get("kind", "—")),
                "running",
            )
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
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "failed"
            )
            self._set_current_operation_state("FAILED")
        elif name == "safe_finally_started":
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "running"
            )
            self._update_current_operation(data, state="SAFE SHUTDOWN")
        elif name == "safe_finally_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "done"
            )
            self._set_current_operation_state("CONFIRMED")
            self._update_eta()
        elif name == "safe_finally_error":
            self._mark_step(
                str(data.get("node_id", "—")), str(data.get("kind", "—")), "failed"
            )
        elif name == "shutdown_action_started":
            self._mark_shutdown(str(data.get("action", "unknown")), "running")
            self._update_current_operation(
                {"kind": str(data.get("action", "shutdown")), "node_id": "shutdown"},
                state="SAFE SHUTDOWN",
            )
        elif name == "shutdown_action_finished":
            self._mark_shutdown(str(data.get("action", "unknown")), "done")
            self._set_current_operation_state("CONFIRMED")
        elif name == "shutdown_error":
            self._mark_shutdown(str(data.get("action", "unknown")), "failed")
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
            "point_stored",
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
        preview_kind = str(data.get("preview_kind", "measurement"))
        trace_label = (
            "Stored reference" if preview_kind == "reference" else "Stored spectrum"
        )
        self.spectrum_preview.set_trace(
            trace_label,
            frequencies,
            powers,
            primary=True,
        )
        title = (
            "Stored reference"
            if preview_kind == "reference"
            else f"Stored point {data.get('point_index', '—')}"
        )
        self.spectrum_preview.set_title(
            title + f" • {data.get('source_points', len(powers))} source values"
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
        self._preview_timer.stop()
        self._flush_spectrum_preview()
        self._step_visual_timer.stop()
        self._flush_step_visual()
        self._manual_dialog.finish()
        self._eta_timer.stop()
        self._activity_pulse_timer.stop()
        self._activity_pulse_on = False
        self._set_activity_indicator("○", "off")
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        run_result = result["result"]
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
        self._preview_timer.stop()
        self._flush_spectrum_preview()
        self._step_visual_timer.stop()
        self._flush_step_visual()
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
