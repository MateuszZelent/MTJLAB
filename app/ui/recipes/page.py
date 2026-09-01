"""Recipe editing workspace and its supporting Qt widgets."""

# ruff: noqa: F401
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import QMimeData, QSize, QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QCloseEvent, QColor, QDrag, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import QAbstractItemView, QApplication, QComboBox, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QSpinBox, QStackedWidget, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CommandBar,
    ComboBox,
    FlowLayout,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    ScrollArea,
    SegmentedWidget,
    StrongBodyLabel,
    SubtitleLabel,
)

from app.audit import AuditLogger
from app.contracts import DeviceModuleRegistry
from app.domain.errors import AuthorizationError, ConfigurationError
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_DBM, DIMENSION_FREQUENCY, DIMENSION_TIME, DIMENSION_VOLTAGE, format_quantity_auto, parse_quantity
from app.engine.compiler import ExecutionPlan, RecipeCompiler
from app.engine.estimation import PlanEstimate, PlanEstimator
from app.recipes import (
    RecipeNode,
    RecipeRepository,
    add_recipe_node,
    delete_recipe_node,
    generate_sweep_points,
    generate_sweep_stage_points,
    move_recipe_node,
    parse_recipe_text,
    replace_recipe_node,
    wrap_recipe_nodes_in_repeat,
)
from app.recipes.parameter_registry import SWEEP_DIMENSIONS
from app.recipes.parameter_registry import SWEEPABLE_PARAMETERS as _SWEEPABLE_PARAMETERS
from app.recipes.parameter_registry import sweep_default as _sweep_default
from app.security import AccessPolicy, Permission
from app.settings.models import StationSettings
from app.storage import Hdf5RunReader, ThatecDevice, ThatecRow, ThatecRun, ThatecRunReader, ThatecTreeNode
from app.ui.common import human_bytes as _human_bytes
from app.ui.dialogs import StationDialog, StationFileDialog as QFileDialog
from app.ui.dialogs import StationMessageBox as QMessageBox
from app.ui.settings_guidance import SettingsIssue, settings_issue_for_error
from app.ui.common import human_duration as _human_duration
from app.ui.common import line_edit as _line
from app.ui.design_system import ThemeTokens, effective_theme, tokens_for
from app.ui.recipes.device_parameters import DeviceParameterDialog
from app.ui.recipes.common_dialogs import (
    ActionNodeEditorDialog, AnritsuAcquisitionEditorDialog, CommentEditorDialog, FixedValueDialog,
    KeithleySweepBuilderDialog, RecipeTreeMoveRequest, RecipeTreeWidget,
    OutputPolicyDialog, RepeatCountDialog, SweepLibraryButton,
)
from app.ui.recipes.device_extensions import (
    AnritsuAdvancedSpectrumPanel,
    AnritsuConfigurationSnapshot,
    AnritsuNodeEditorDialog,
    AnritsuPage,
    AnritsuPageState,
    AnritsuSignalGeneratorNodeEditorDialog,
    AnritsuSpectrumConfigurationPanel,
    KeithleyConfigurationPanel,
    KeithleyConfigurationSnapshot,
    KeithleyNodeEditorDialog,
    KeithleyPage,
    RigolConfigurationSnapshot,
    RigolNodeEditorDialog,
    RigolPage,
    SignalGeneratorSnapshot,
    _keithley_roi_definition,
)
from app.ui.recipes.sweep_editor import SweepGeneratorDialog
from app.ui.run_worker import planned_run_paths
from app.ui.widgets import LimitEditDialog, LimitField, SpectrumPlotWidget
from app.ui.workers import RecipePreflightWorker


_KEITHLEY_OPTIONAL_SHUTDOWN_RAMP_IDS = {
    "A": "optional-keithley-a-ramp-before-auto-off",
    "B": "optional-keithley-b-ramp-before-auto-off",
}


class KeithleyShutdownMethodDialog(StationDialog):
    """Choose optional operator cleanup before the compiler's final OFF."""

    def __init__(
        self,
        *,
        ramp_channels: set[str],
        deadline: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keithley shutdown method")
        self.setMinimumWidth(520)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=12)

        title = StrongBodyLabel("Keithley shutdown before automatic OFF", surface)
        layout.addWidget(title)
        explanation = BodyLabel(
            "Automatic final OUTPUT OFF always remains active. Ramp to zero is "
            "only added when you explicitly select it here.",
            surface,
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        layout.addWidget(explanation)

        form = QFormLayout()
        self.channel_a = self._method_combo("A" in ramp_channels)
        self.channel_b = self._method_combo("B" in ramp_channels)
        self.deadline = LineEdit(surface)
        self.deadline.setText(deadline or "30 s")
        self.deadline.setPlaceholderText("30 s")
        self.deadline.setAccessibleName("Ramp deadline")
        form.addRow("Channel A", self.channel_a)
        form.addRow("Channel B", self.channel_b)
        form.addRow("Ramp deadline", self.deadline)
        layout.addLayout(form)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", surface)
        apply = PrimaryPushButton("Apply shutdown choice", surface)
        footer.addWidget(cancel)
        footer.addWidget(apply)
        layout.addLayout(footer)
        cancel.clicked.connect(self.reject)
        apply.clicked.connect(self._accept_if_valid)

    def selected_ramp_channels(self) -> set[str]:
        selected: set[str] = set()
        if self.channel_a.currentData() == "ramp":
            selected.add("A")
        if self.channel_b.currentData() == "ramp":
            selected.add("B")
        return selected

    def ramp_deadline(self) -> str:
        return self.deadline.text().strip() or "30 s"

    def _method_combo(self, ramp_enabled: bool) -> ComboBox:
        combo = ComboBox(self.modal_shell.surface)
        combo.addItem("Immediate OUTPUT OFF", userData="off")
        combo.addItem("Ramp to zero, then OUTPUT OFF", userData="ramp")
        combo.setCurrentIndex(1 if ramp_enabled else 0)
        return combo

    def _accept_if_valid(self) -> None:
        try:
            quantity = parse_quantity(self.ramp_deadline(), DIMENSION_TIME)
            if quantity.si_value <= 0:
                raise ConfigurationError("Ramp deadline must be greater than 0 s.")
        except Exception as exc:
            QMessageBox.warning(self, "Keithley shutdown method", str(exc))
            return
        self.accept()


def set_keithley_shutdown_ramps_in_recipe(
    source: str,
    *,
    ramp_channels: set[str],
    deadline: str,
) -> tuple[str, str | None]:
    invalid = ramp_channels - {"A", "B"}
    if invalid:
        raise ConfigurationError(
            f"Keithley shutdown ramp supports only channels A and B, not {sorted(invalid)!r}."
        )
    quantity = parse_quantity(deadline, DIMENSION_TIME)
    if quantity.si_value <= 0:
        raise ConfigurationError("Ramp deadline must be greater than 0 s.")
    recipe = parse_recipe_text(source, origin="tree-builder")
    managed_ids = set(_KEITHLEY_OPTIONAL_SHUTDOWN_RAMP_IDS.values())
    next_source = source
    for node in recipe.finally_nodes:
        if node.id not in managed_ids:
            continue
        channel = str(node.data.get("channel", "")).upper()
        if (
            node.type != "ramp_keithley_to_zero"
            or _KEITHLEY_OPTIONAL_SHUTDOWN_RAMP_IDS.get(channel) != node.id
        ):
            raise ConfigurationError(
                f"{node.id}: reserved shutdown node id is not a managed Keithley ramp."
            )
        next_source = delete_recipe_node(next_source, node_id=node.id)
    selected_id: str | None = None
    for channel in ("A", "B"):
        if channel not in ramp_channels:
            continue
        node = {
            "id": _KEITHLEY_OPTIONAL_SHUTDOWN_RAMP_IDS[channel],
            "type": "ramp_keithley_to_zero",
            "channel": channel,
            "deadline": deadline,
        }
        next_source = add_recipe_node(
            next_source,
            parent_id="__finally__",
            branch="children",
            node=node,
        )
        selected_id = str(node["id"])
    return next_source, selected_id


class RecipePage(QWidget):
    status = Signal(str)
    run_requested = Signal(object, bool, str, str, str)
    plan_preflight_changed = Signal(object)
    settings_issue_requested = Signal(object)
    operator_row_role = int(Qt.ItemDataRole.UserRole) + 17
    _FINALLY_ACTION_TYPES = {
        "ramp_keithley_to_zero",
        "set_keithley_output",
        "set_rigol_output",
        "set_anritsu_sg_output",
    }

    def __init__(
        self,
        settings: StationSettings,
        parent: QWidget | None = None,
        *,
        device_registry: DeviceModuleRegistry | None = None,
    ) -> None:
        super().__init__(parent)
        self._historical_sweep_active = False
        self._execution_controlled = False
        self._execution_widget_states: dict[QWidget, bool] = {}
        self._execution_action_states: dict[QAction, bool] = {}
        self._execution_editor_read_only = False
        self._recipe_parameter_definitions = (
            device_registry.recipe_parameter_definitions()
            if device_registry is not None
            else _SWEEPABLE_PARAMETERS
        )
        if settings.moke_box.enabled:
            self._recipe_parameter_definitions = tuple(
                (*self._recipe_parameter_definitions,)
                + tuple(
                    item for item in _SWEEPABLE_PARAMETERS
                    if item["target"].startswith("moke_box.")
                )
            )
        self._settings = settings
        self._keithley_snapshot_provider = None
        self._rigol_snapshot_provider = None
        self._anritsu_snapshot_provider = None
        self._anritsu_sg_snapshot_provider = None
        self._plan = None
        self._preflight_thread: QThread | None = None
        self._preflight_worker: RecipePreflightWorker | None = None
        self._preflight_source: str | None = None
        self._preflight_outputs_forced_off: bool | None = None
        self._repository = RecipeRepository()
        self._loading_source = False
        # ``_tree_source`` is the only source represented by the visible tree.
        # Manual YAML can diverge while it is being typed, but tree mutations
        # remain locked until that draft has parsed and rendered atomically.
        self._tree_source = ""
        self._saved_source: str | None = None
        self._saved_path: str | None = None
        self._close_discard_confirmed = False
        self._tree_undo: list[str] = []
        self._tree_redo: list[str] = []
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(750)
        self._autosave_timer.timeout.connect(self._autosave)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        self.setProperty("stationSurface", "page")
        self.hero_card = CardWidget(self)
        hero = self.hero_card
        hero.setObjectName("recipeHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(18, 14, 18, 14)
        hero_layout.setSpacing(16)
        hero_copy = QVBoxLayout()
        hero_copy.setSpacing(3)
        title = SubtitleLabel("Sweep builder", hero)
        title.setObjectName("recipePageTitle")
        hero_copy.addWidget(title)
        subtitle = BodyLabel(
            "Build a qualified measurement from instrument blocks, then validate it before execution.",
            hero,
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        subtitle.setMinimumWidth(0)
        subtitle.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        hero_copy.addWidget(subtitle)
        hero_layout.addLayout(hero_copy, 1)
        self.recipe_profile_badge = StrongBodyLabel(
            "LIMITS + READBACK ACTIVE",
            hero,
        )
        self.recipe_profile_badge.setObjectName("recipeProfileBadge")
        self.recipe_profile_badge.setProperty("safetyState", "verified")
        self.recipe_profile_badge.setToolTip(
            "Device permissions, configured station limits, explicit OUTPUT "
            "actions and hardware readback govern output operations."
        )
        hero_layout.addWidget(self.recipe_profile_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(hero)
        self.document_card = CardWidget(self)
        self.document_card.setObjectName("recipeDocumentCard")
        document_layout = QVBoxLayout(self.document_card)
        document_layout.setContentsMargins(12, 10, 12, 12)
        document_layout.setSpacing(8)
        self.recipe_command_bar = CommandBar()
        self.recipe_command_bar.setObjectName("recipeCommandBar")
        self.recipe_command_bar.setIconSize(QSize(18, 18))
        self.recipe_command_bar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        path_line = QHBoxLayout()
        path_line.setSpacing(8)
        self.path = LineEdit(self.document_card)
        self.path.setText("recipes/example_nested_sweep.yml")
        self.path.setClearButtonEnabled(True)
        self.path.setAccessibleName("Recipe file path")
        self.path.setProperty("precisionArrowStepping", False)
        self.restore_button = PushButton("Restore autosave")
        self.restore_button.setEnabled(False)
        self.execution_mode = ComboBox(self.document_card)
        self.execution_mode.setObjectName("recipeExecutionMode")
        self.execution_mode.setAccessibleName("Sweep execution mode")
        self.execution_mode.addItem(
            "Measurement — recipe controls outputs",
            userData="measurement",
        )
        self.execution_mode.addItem(
            "Dry run — outputs forced OFF",
            userData="dry_run",
        )
        self.execution_mode.addItem(
            "Manual stages — operator advances each step",
            userData="manual_step",
        )
        self.execution_mode.setMinimumWidth(260)
        self.execution_mode.setToolTip(
            "Dry run sends configurations and every sweep setpoint to real devices, "
            "acquires and stores Anritsu spectra, but replaces every OUTPUT ON with "
            "confirmed OUTPUT OFF."
        )
        self.run_button = PrimaryPushButton("Run plan")
        self.run_button.setEnabled(False)

        def command_action(
            text: str,
            icon: QStyle.StandardPixmap,
            callback: Callable[[], None],
            *,
            shortcut: QKeySequence | QKeySequence.StandardKey | None = None,
        ) -> QAction:
            action = QAction(self.style().standardIcon(icon), text, self)
            action.setToolTip(text)
            action.setStatusTip(text)
            if shortcut is not None:
                action.setShortcut(shortcut)
            # QAction.triggered emits ``checked: bool``. Adapt that signal once
            # here so commands keep their natural no-argument API, including
            # methods with keyword-only options such as ``load_editor``.
            action.triggered.connect(
                lambda _checked=False, command=callback: command()
            )
            self.recipe_command_bar.addAction(action)
            return action

        self.new_recipe_action = command_action(
            "New",
            QStyle.StandardPixmap.SP_FileIcon,
            self.new_recipe,
            shortcut=QKeySequence.StandardKey.New,
        )
        self.load_recipe_action = command_action(
            "Load recipe",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            self.browse_recipe,
            shortcut=QKeySequence.StandardKey.Open,
        )
        self.open_hdf5_action = command_action(
            "Open HDF5 result",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            self.browse_hdf5_result,
        )
        self.save_recipe_action = command_action(
            "Save recipe",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            self.save_recipe,
            shortcut=QKeySequence.StandardKey.Save,
        )
        self.apply_yaml_action = command_action(
            "Apply YAML to tree",
            QStyle.StandardPixmap.SP_DialogApplyButton,
            self.apply_yaml_to_tree,
            shortcut=QKeySequence("Ctrl+Shift+Return"),
        )
        self.recipe_command_bar.addSeparator()
        self.undo_tree_action = command_action(
            "Undo",
            QStyle.StandardPixmap.SP_ArrowBack,
            self.undo_tree_edit,
            shortcut=QKeySequence.StandardKey.Undo,
        )
        self.redo_tree_action = command_action(
            "Redo",
            QStyle.StandardPixmap.SP_ArrowForward,
            self.redo_tree_edit,
            shortcut=QKeySequence.StandardKey.Redo,
        )
        self.recipe_command_bar.addSeparator()
        self.compile_recipe_action = command_action(
            "Validate & preview",
            QStyle.StandardPixmap.SP_DialogApplyButton,
            self.compile_recipe_async,
            shortcut=QKeySequence("Ctrl+Return"),
        )
        self.recipe_command_bar.addSeparator()
        self.library_visibility_action = QAction("Library", self)
        self.library_visibility_action.setCheckable(True)
        self.library_visibility_action.setChecked(True)
        self.library_visibility_action.setToolTip("Show or hide the node library")
        self.library_visibility_action.toggled.connect(
            lambda visible: self._set_workspace_panel_visible(
                "library", visible
            )
        )
        self.recipe_command_bar.addAction(self.library_visibility_action)
        self.inspector_visibility_action = QAction("Inspector", self)
        self.inspector_visibility_action.setCheckable(True)
        self.inspector_visibility_action.setChecked(True)
        self.inspector_visibility_action.setToolTip(
            "Show or hide the selected-node inspector"
        )
        self.inspector_visibility_action.toggled.connect(
            lambda visible: self._set_workspace_panel_visible(
                "inspector", visible
            )
        )
        self.recipe_command_bar.addAction(self.inspector_visibility_action)
        document_layout.addWidget(self.recipe_command_bar)
        self.execution_lock_banner = CardWidget(self.document_card)
        self.execution_lock_banner.setObjectName("recipeExecutionLockBanner")
        self.execution_lock_banner.setProperty("stationSurface", "raised")
        lock_layout = QHBoxLayout(self.execution_lock_banner)
        lock_layout.setContentsMargins(10, 7, 10, 7)
        lock_layout.setSpacing(10)
        lock_title = StrongBodyLabel(
            "RUN IN PROGRESS · READ-ONLY", self.execution_lock_banner
        )
        lock_title.setObjectName("recipeExecutionLockTitle")
        lock_title.setProperty("safetyState", "caution")
        lock_layout.addWidget(lock_title)
        lock_copy = CaptionLabel(
            "The Run Engine owns device I/O. Inspect the recipe and live readback; editing is paused until the run ends.",
            self.execution_lock_banner,
        )
        lock_copy.setObjectName("muted")
        lock_copy.setWordWrap(True)
        lock_layout.addWidget(lock_copy, 1)
        self.execution_lock_banner.hide()
        document_layout.addWidget(self.execution_lock_banner)
        path_label = CaptionLabel("Recipe file", self.document_card)
        path_label.setObjectName("recipePathLabel")
        path_line.addWidget(path_label)
        path_line.addWidget(self.path, 1)
        self.document_state_badge = StrongBodyLabel("DRAFT", self.document_card)
        self.document_state_badge.setObjectName("recipeDocumentState")
        self.document_state_badge.setProperty("safetyState", "caution")
        self.document_state_badge.setAccessibleName("Recipe document state")
        path_line.addWidget(
            self.document_state_badge, 0, Qt.AlignmentFlag.AlignVCenter
        )
        path_line.addWidget(self.restore_button)
        document_layout.addLayout(path_line)
        output_line = QGridLayout()
        output_line.setSpacing(8)
        output_directory_label = CaptionLabel("Result directory", self.document_card)
        output_line.addWidget(output_directory_label, 0, 0)
        self.output_directory = LineEdit(self.document_card)
        self.output_directory.setText(self._default_output_directory())
        self.output_directory.setClearButtonEnabled(True)
        self.output_directory.setAccessibleName("Sweep result directory")
        self.output_directory.setProperty("precisionArrowStepping", False)
        output_line.addWidget(self.output_directory, 0, 1)
        self.output_directory_button = PushButton("Browse...", self.document_card)
        output_line.addWidget(self.output_directory_button, 0, 2)
        output_file_label = CaptionLabel("Result file name", self.document_card)
        output_line.addWidget(output_file_label, 1, 0)
        self.output_file_stem = LineEdit(self.document_card)
        self.output_file_stem.setPlaceholderText("Auto from recipe name")
        self.output_file_stem.setClearButtonEnabled(True)
        self.output_file_stem.setAccessibleName("Sweep result file name")
        self.output_file_stem.setProperty("precisionArrowStepping", False)
        self.output_file_stem.setToolTip(
            "The run keeps its automatic UTC timestamp prefix to avoid accidental overwrites."
        )
        output_line.addWidget(self.output_file_stem, 1, 1, 1, 2)
        self.output_file_preview = CaptionLabel(self.document_card)
        self.output_file_preview.setObjectName("muted")
        self.output_file_preview.setWordWrap(True)
        self.output_file_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        output_line.addWidget(self.output_file_preview, 2, 0, 1, 3)
        output_line.setColumnStretch(1, 1)
        document_layout.addLayout(output_line)
        execution_line = QGridLayout()
        execution_line.setSpacing(8)
        execution_label = CaptionLabel("Execution mode", self.document_card)
        execution_line.addWidget(execution_label, 0, 0)
        execution_line.addWidget(self.execution_mode, 0, 1)
        self.execution_mode_hint = CaptionLabel(
            "Normal measurement: OUTPUT actions in the recipe are executed.",
            self.document_card,
        )
        self.execution_mode_hint.setObjectName("muted")
        self.execution_mode_hint.setWordWrap(True)
        self.execution_mode_hint.setMinimumWidth(0)
        self.execution_mode_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        execution_line.addWidget(self.execution_mode_hint, 1, 0, 1, 3)
        execution_line.addWidget(self.run_button, 0, 2)
        execution_line.setColumnStretch(1, 1)
        document_layout.addLayout(execution_line)
        layout.addWidget(self.document_card)
        self.selection_card = CardWidget(self)
        self.selection_card.setObjectName("recipeSelectionCard")
        builder_actions = FlowLayout(
            self.selection_card, needAni=False, isTight=True
        )
        builder_actions.setContentsMargins(12, 8, 12, 8)
        builder_actions.setHorizontalSpacing(8)
        builder_actions.setVerticalSpacing(8)
        selection_summary = QWidget(self.selection_card)
        selection_summary.setMinimumWidth(180)
        selection_summary.setMaximumWidth(220)
        selection_summary.setMinimumHeight(40)
        selection_copy = QVBoxLayout(selection_summary)
        selection_copy.setContentsMargins(0, 0, 0, 0)
        selection_copy.setSpacing(2)
        self.selection_title = StrongBodyLabel("Selected block", self.selection_card)
        self.selection_title.setFixedHeight(18)
        self.selection_context = CaptionLabel("Select a block in the measurement tree", self.selection_card)
        self.selection_context.setObjectName("muted")
        self.selection_context.setFixedHeight(16)
        selection_copy.addWidget(self.selection_title)
        selection_copy.addWidget(self.selection_context)
        builder_actions.addWidget(selection_summary)
        def tool_button(
            text: str, tooltip: str, icon: QStyle.StandardPixmap, *, primary: bool = False
        ) -> PushButton:
            button = PrimaryPushButton() if primary else PushButton()
            button.setText(text)
            button.setToolTip(tooltip)
            button.setIcon(self.style().standardIcon(icon))
            return button

        self.edit_device_button = tool_button(
            "Device settings",
            "Open a separate configuration window for the selected instrument",
            QStyle.StandardPixmap.SP_ComputerIcon,
            primary=True,
        )
        self.edit_generator_button = tool_button(
            "Edit ROI",
            "Open only the point/interval editor for the selected sweep",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
        )
        self.delete_node_button = tool_button(
            "Delete", "Delete the selected node (Delete)", QStyle.StandardPixmap.SP_TrashIcon
        )
        self.duplicate_node_button = tool_button(
            "Duplicate", "Duplicate the selected leaf node (Ctrl+D)", QStyle.StandardPixmap.SP_FileDialogContentsView
        )
        self.move_up_button = tool_button(
            "Up", "Move the selected node up (Alt+Up)", QStyle.StandardPixmap.SP_ArrowUp
        )
        self.move_down_button = tool_button(
            "Down", "Move the selected node down (Alt+Down)", QStyle.StandardPixmap.SP_ArrowDown
        )
        self.wrap_repeat_button = tool_button(
            "Wrap in Repeat...",
            "Repeat the selected block, or all root steps when the root is selected",
            QStyle.StandardPixmap.SP_BrowserReload,
        )
        builder_actions.addWidget(self.edit_device_button)
        builder_actions.addWidget(self.edit_generator_button)
        builder_actions.addWidget(self.delete_node_button)
        builder_actions.addWidget(self.duplicate_node_button)
        builder_actions.addWidget(self.move_up_button)
        builder_actions.addWidget(self.move_down_button)
        builder_actions.addWidget(self.wrap_repeat_button)
        layout.addWidget(self.selection_card)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("recipeWorkspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        self._workspace_layout_updating = False
        self._workspace_user_resized = False
        self._last_workspace_layout_width = -1
        self._workspace_visibility_override: dict[str, bool | None] = {
            "library": None,
            "inspector": None,
        }
        self.editor = PlainTextEdit(self)
        self.editor.setPlaceholderText("Declarative YAML recipe — no Python code and no raw SCPI.")
        self.editor.setMinimumWidth(320)
        self.tree = RecipeTreeWidget()
        self.tree.setObjectName("recipeTree")
        self.tree.setHeaderLabels(["Measurement sequence", "Role / expansion", "Status"])
        tree_header = self.tree.header()
        tree_header.setStretchLastSection(False)
        tree_header.setMinimumSectionSize(72)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        tree_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 190)
        self.tree.setColumnWidth(2, 92)
        self.tree.setAlternatingRowColors(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setToolTip(
            "Drop on a horizontal gap to reorder. Drop on a highlighted flow "
            "container to add inside it. Actions and ROI rows never accept children."
        )
        self.builder_container = CardWidget()
        self.builder_container.setObjectName("recipeBuilderPanel")
        self.builder_container.setProperty("stationSurface", "surface")
        self.builder_container.setMinimumHeight(430)
        builder_layout = QVBoxLayout(self.builder_container)
        builder_layout.setContentsMargins(12, 10, 12, 12)
        builder_layout.setSpacing(8)
        self.workflow_tabs = SegmentedWidget(self.builder_container)
        self.workflow_tabs.addItem("tree", "Measurement tree")
        self.workflow_tabs.addItem("yaml", "YAML source")
        builder_layout.addWidget(self.workflow_tabs)
        self.drag_feedback = CaptionLabel(
            "A line inserts between steps; a highlighted container inserts inside it.",
            self.builder_container,
        )
        self.drag_feedback.setObjectName("muted")
        self.drag_feedback.setWordWrap(True)
        builder_layout.addWidget(self.drag_feedback)
        self.builder_stack = QStackedWidget()
        self.builder_stack.setMinimumWidth(390)
        self.builder_stack.addWidget(self.tree)
        self.builder_stack.addWidget(self.editor)
        self.workflow_tabs.setCurrentItem("tree")
        self.workflow_tabs.currentItemChanged.connect(
            lambda route: self.builder_stack.setCurrentIndex(0 if route == "tree" else 1)
        )
        self.builder_stack.currentChanged.connect(
            lambda index: self.workflow_tabs.setCurrentItem("tree" if index == 0 else "yaml")
        )
        builder_layout.addWidget(self.builder_stack, 1)
        self.library_panel = self._build_device_library()
        self.library_panel.setMinimumHeight(430)
        self.workspace_splitter.addWidget(self.library_panel)
        self.workspace_splitter.addWidget(self.builder_container)
        self.inspector_panel = QWidget()
        self.inspector_panel.setObjectName("recipeInspectorPanel")
        self.inspector_panel.setProperty("stationSurface", "surface")
        self.inspector_panel.setMinimumHeight(430)
        self.inspector_panel.setMinimumWidth(280)
        self.inspector_panel.setMaximumWidth(620)
        inspector_layout = QVBoxLayout(self.inspector_panel)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_card = CardWidget()
        inspector_card.setObjectName("recipeInspector")
        inspector_card_layout = QVBoxLayout(inspector_card)
        inspector_card_layout.setContentsMargins(12, 12, 12, 12)
        inspector_title = BodyLabel("Inspector")
        inspector_title.setObjectName("sectionTitle")
        self.inspector_summary = BodyLabel("Select a node to see its measurement role and configuration.")
        self.inspector_summary.setWordWrap(True)
        self.inspector_summary.setObjectName("muted")
        self.open_editor_button = PrimaryPushButton("Open parameter editor")
        self.open_editor_button.setEnabled(False)
        self.inspector = PlainTextEdit(self.inspector_panel)
        self.inspector.setReadOnly(True)
        self.inspector.setPlaceholderText("Select a recipe node to inspect its fields and expansion.")
        inspector_card_layout.addWidget(inspector_title)
        inspector_card_layout.addWidget(self.inspector_summary)
        inspector_card_layout.addWidget(self.open_editor_button)
        inspector_card_layout.addWidget(self.inspector, 1)
        inspector_layout.addWidget(inspector_card, 1)
        self.workspace_splitter.addWidget(self.inspector_panel)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 4)
        self.workspace_splitter.setStretchFactor(2, 2)
        self.workspace_splitter.setMinimumHeight(430)
        self.workspace_splitter.setProperty("stationSurface", "surface")
        self.workspace_splitter.splitterMoved.connect(self._workspace_splitter_moved)
        self.workspace_card = CardWidget(self)
        self.workspace_card.setObjectName("recipeWorkspaceCard")
        self.workspace_card.setProperty("stationSurface", "surface")
        self.workspace_card.setMinimumHeight(500)
        workspace_layout = QVBoxLayout(self.workspace_card)
        workspace_layout.setContentsMargins(12, 10, 12, 12)
        workspace_layout.setSpacing(8)
        workspace_heading = QHBoxLayout()
        workspace_title = StrongBodyLabel(
            "Measurement workspace", self.workspace_card
        )
        workspace_title.setObjectName("recipeWorkspaceTitle")
        workspace_heading.addWidget(workspace_title)
        workspace_hint = CaptionLabel(
            "Library · measurement tree · inspector", self.workspace_card
        )
        workspace_hint.setObjectName("muted")
        workspace_heading.addWidget(workspace_hint)
        workspace_heading.addStretch(1)
        self.workspace_state = CaptionLabel(
            "READY TO EDIT", self.workspace_card
        )
        self.workspace_state.setObjectName("recipeWorkspaceState")
        self.workspace_state.setProperty("safetyState", "verified")
        workspace_heading.addWidget(self.workspace_state)
        workspace_layout.addLayout(workspace_heading)
        workspace_layout.addWidget(self.workspace_splitter, 1)
        layout.addWidget(self.workspace_card, 1)
        self._update_workspace_layout(force=True)
        self.status_card = CardWidget(self)
        self.status_card.setObjectName("recipeStatusCard")
        status_layout = QVBoxLayout(self.status_card)
        status_layout.setContentsMargins(12, 8, 12, 8)
        status_layout.setSpacing(2)
        self.version_label = CaptionLabel("No saved version history.", self.status_card)
        self.version_label.setObjectName("muted")
        self.version_label.setWordWrap(True)
        self.version_label.setMinimumWidth(0)
        self.version_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        status_layout.addWidget(self.version_label)
        self.summary = BodyLabel("The recipe has not been compiled.", self.status_card)
        self.summary.setWordWrap(True)
        self.summary.setMinimumWidth(0)
        self.summary.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        status_layout.addWidget(self.summary)
        layout.addWidget(self.status_card)
        self.restore_button.clicked.connect(self.restore_autosave)
        self.run_button.clicked.connect(self.request_run)
        self.output_directory.textChanged.connect(self._refresh_output_preview)
        self.output_file_stem.textChanged.connect(self._refresh_output_preview)
        self.output_directory_button.clicked.connect(self.browse_output_directory)
        self.execution_mode.currentIndexChanged.connect(
            self._execution_mode_changed
        )
        self.editor.textChanged.connect(self._source_changed)
        self.tree.currentItemChanged.connect(self._node_selected)
        self.tree.itemClicked.connect(self._operator_row_clicked)
        self.tree.itemDoubleClicked.connect(self._open_node_editor)
        self.tree.move_requested.connect(self._handle_tree_move_request)
        self.tree.library_drop_requested.connect(self._drop_library_block)
        self.tree.drop_rejected.connect(self._tree_drop_rejected)
        self.tree.drag_status_changed.connect(self._tree_drag_status_changed)
        self.edit_device_button.clicked.connect(
            self._edit_selected_device_settings
        )
        self.edit_generator_button.clicked.connect(self._edit_selected_roi)
        self.delete_node_button.clicked.connect(self._delete_selected_node)
        self.duplicate_node_button.clicked.connect(self._duplicate_selected_node)
        self.move_up_button.clicked.connect(lambda: self._move_selected_sibling(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected_sibling(1))
        self.wrap_repeat_button.clicked.connect(self._wrap_selected_in_repeat)
        self.open_editor_button.clicked.connect(self._open_current_node_editor)
        self.path.textChanged.connect(self._path_changed)
        self.path.editingFinished.connect(self._update_repository_state)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self._tree_shortcuts = (
            QShortcut(QKeySequence("Delete"), self.tree, activated=self._delete_selected_node),
            QShortcut(QKeySequence("Ctrl+D"), self.tree, activated=self._duplicate_selected_node),
            QShortcut(QKeySequence("Alt+Up"), self.tree, activated=lambda: self._move_selected_sibling(-1)),
            QShortcut(QKeySequence("Alt+Down"), self.tree, activated=lambda: self._move_selected_sibling(1)),
            QShortcut(QKeySequence("Ctrl+Shift+G"), self.tree, activated=self._edit_selected_node),
            QShortcut(QKeySequence("Return"), self.tree, activated=self._edit_selected_node),
            QShortcut(QKeySequence("Enter"), self.tree, activated=self._edit_selected_node),
        )
        self._execution_edit_widgets: tuple[QWidget, ...] = (
            self.path,
            self.restore_button,
            self.execution_mode,
            self.output_directory,
            self.output_directory_button,
            self.output_file_stem,
            self.run_button,
            self.edit_device_button,
            self.edit_generator_button,
            self.delete_node_button,
            self.duplicate_node_button,
            self.move_up_button,
            self.move_down_button,
            self.wrap_repeat_button,
            self.open_editor_button,
            *self._library_action_buttons,
        )
        self._execution_edit_actions: tuple[QAction, ...] = (
            self.new_recipe_action,
            self.load_recipe_action,
            self.open_hdf5_action,
            self.save_recipe_action,
            self.apply_yaml_action,
            self.undo_tree_action,
            self.redo_tree_action,
            self.compile_recipe_action,
        )
        self.load_editor(show_error=False)

    def _default_output_directory(self) -> str:
        return str(self._settings.storage.get("output_directory", "./measurements"))

    def _requested_output_directory(self) -> str:
        text = self.output_directory.text().strip()
        return str(Path(text or self._default_output_directory()).expanduser())

    def _requested_output_file_stem(self) -> str:
        return self.output_file_stem.text().strip()

    def _suggested_recipe_name(self) -> str:
        if self._plan is not None:
            name = getattr(self._plan, "recipe_name", None)
            if name:
                return str(name)
        for source in (self.editor.toPlainText(), self._tree_source):
            match = re.search(r"(?m)^name:\s*(.+?)\s*$", source)
            if not match:
                continue
            raw_name = match.group(1).strip()
            if raw_name.startswith(("'", '"')) and raw_name.endswith(("'", '"')):
                raw_name = raw_name[1:-1].strip()
            if raw_name:
                return raw_name
        current_path = self.path.text().strip()
        if current_path:
            stem = Path(current_path).stem.strip()
            if stem:
                return stem
        return "run"

    def _refresh_output_preview(self, _text: str = "") -> None:
        if not hasattr(self, "output_file_preview"):
            return
        try:
            result_path, csv_summary_path = planned_run_paths(
                self._settings,
                self._suggested_recipe_name(),
                output_dir_override=self._requested_output_directory(),
                file_stem_override=self._requested_output_file_stem() or None,
            )
        except Exception as exc:
            self.output_file_preview.setText(f"Run output preview unavailable: {exc}")
            return
        preview_lines = [f"Next run file: {result_path}"]
        if csv_summary_path is not None:
            preview_lines.append(f"CSV summary: {csv_summary_path}")
        self.output_file_preview.setText("\n".join(preview_lines))

    def browse_output_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose sweep result directory",
            self._requested_output_directory(),
        )
        if selected:
            self.output_directory.setText(str(Path(selected)))

    def _workspace_splitter_moved(self, _position: int, _index: int) -> None:
        if not self._workspace_layout_updating:
            self._workspace_user_resized = True

    def _set_workspace_panel_visible(self, panel: str, visible: bool) -> None:
        self._workspace_visibility_override[panel] = visible
        widget = (
            getattr(self, "library_panel", None)
            if panel == "library"
            else getattr(self, "inspector_panel", None)
        )
        if isinstance(widget, QWidget):
            widget.setVisible(visible)
            self._workspace_user_resized = False
            self._update_workspace_layout(force=True)

    def _update_workspace_layout(self, *, force: bool = False) -> None:
        """Keep Library, measurement tree and Inspector useful at every width."""

        if not hasattr(self, "workspace_splitter"):
            return
        available = max(480, self.workspace_splitter.width() or self.width() - 28)
        if not force and (
            self._workspace_user_resized
            or abs(available - self._last_workspace_layout_width) < 24
        ):
            return
        self._last_workspace_layout_width = available
        inspector_override = self._workspace_visibility_override["inspector"]
        library_override = self._workspace_visibility_override["library"]
        show_inspector = (
            available >= 1_200
            if inspector_override is None
            else inspector_override
        )
        show_library = (
            available >= 760 if library_override is None else library_override
        )
        for action, widget, visible in (
            (
                self.library_visibility_action,
                self.library_panel,
                show_library,
            ),
            (
                self.inspector_visibility_action,
                self.inspector_panel,
                show_inspector,
            ),
        ):
            action.blockSignals(True)
            action.setChecked(visible)
            action.blockSignals(False)
            widget.setVisible(visible)
        if not show_library:
            sizes = [0, available, 0]
            self._workspace_layout_updating = True
            try:
                self.workspace_splitter.setSizes(sizes)
            finally:
                self._workspace_layout_updating = False
            return
        if not show_inspector:
            library = max(210, min(260, round(available * 0.24)))
            sizes = [library, max(390, available - library), 0]
            self._workspace_layout_updating = True
            try:
                self.workspace_splitter.setSizes(sizes)
            finally:
                self._workspace_layout_updating = False
            return
        if available >= 1600:
            library = max(270, min(340, round(available * 0.17)))
            inspector = max(420, min(560, round(available * 0.25)))
        elif available >= 1250:
            library = max(235, min(300, round(available * 0.19)))
            inspector = max(340, min(450, round(available * 0.27)))
        else:
            library = max(210, min(245, round(available * 0.21)))
            inspector = max(280, min(330, round(available * 0.28)))
        tree = max(390, available - library - inspector)
        self._workspace_layout_updating = True
        try:
            self.workspace_splitter.setSizes([library, tree, inspector])
        finally:
            self._workspace_layout_updating = False

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._update_workspace_layout()

    def _build_device_library(self) -> QFrame:
        """Build a dense, searchable node library modelled after the operator workspace."""

        scroll = ScrollArea()
        scroll.setObjectName("recipeLibraryScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumWidth(210)
        scroll.setMaximumWidth(400)
        panel = CardWidget(self)
        panel.setObjectName("recipeLibrary")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)
        heading = BodyLabel("NODE LIBRARY")
        heading.setObjectName("recipeLibraryHeading")
        layout.addWidget(heading)
        self.library_search = _line("", 24)
        self.library_search.setPlaceholderText("Search nodes and actions…")
        self.library_search.setClearButtonEnabled(True)
        layout.addWidget(self.library_search)
        self._library_action_buttons: list[QWidget] = []
        self._library_force_inside = False

        def group(title: str, badge: str) -> QVBoxLayout:
            group_frame = CardWidget(panel)
            group_frame.setObjectName("recipeLibraryGroup")
            group_layout = QVBoxLayout(group_frame)
            group_layout.setContentsMargins(7, 6, 7, 7)
            group_layout.setSpacing(2)
            caption = QHBoxLayout()
            label = BodyLabel(title.upper())
            label.setObjectName("recipeLibraryGroupTitle")
            count = BodyLabel(badge)
            count.setObjectName("recipeLibraryBadge")
            caption.addWidget(label)
            caption.addStretch(1)
            caption.addWidget(count)
            group_layout.addLayout(caption)
            layout.addWidget(group_frame)
            return group_layout

        def action(
            target_layout: QVBoxLayout,
            text: str,
            description: str,
            kind: str,
            icon: QStyle.StandardPixmap,
            callback: object,
            *,
            drag_kind: str | None,
        ) -> None:
            button = (
                SweepLibraryButton(drag_kind)
                if drag_kind is not None
                else PushButton()
            )
            button.setObjectName("recipeLibraryAction")
            button.setProperty("deviceKind", kind)
            button.setProperty("libraryDescription", description)
            button.setText(text)
            button.setToolTip(description)
            button.setIcon(self.style().standardIcon(icon))
            button.setIconSize(QSize(18, 18))
            button.setMinimumHeight(34)
            button.setMinimumWidth(0)
            button.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
            )
            button.clicked.connect(
                lambda _checked=False, command=callback, label=text: (
                    self._invoke_library_action(label, command)
                )
            )
            if drag_kind is not None and not drag_kind.startswith("safety:"):
                button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                button.customContextMenuRequested.connect(
                    lambda position, widget=button, command=callback, label=text: (
                        self._show_library_action_menu(widget, position, label, command)
                    )
                )
            target_layout.addWidget(button)
            self._library_action_buttons.append(button)

        devices = group("Devices", "4")
        action(
            devices, "Keithley 2600", "Source-measure unit module", "keithley",
            QStyle.StandardPixmap.SP_DriveHDIcon,
            lambda: self._library_add_device("keithley"),
            drag_kind="device:keithley",
        )
        action(
            devices, "Rigol DG1032Z", "Function generator module", "rigol",
            QStyle.StandardPixmap.SP_DriveFDIcon,
            lambda: self._library_add_device("rigol"),
            drag_kind="device:rigol",
        )
        action(
            devices, "Anritsu configuration", "Configure Spectrum Analyzer settings once", "anritsu",
            QStyle.StandardPixmap.SP_ComputerIcon,
            lambda: self._library_add_device("anritsu"),
            drag_kind="device:anritsu",
        )
        action(
            devices,
            "Anritsu signal generator",
            "Configure RF frequency, power, and a plan-owned output lifecycle; safe default is RF OFF",
            "anritsu",
            QStyle.StandardPixmap.SP_ComputerIcon,
            lambda: self._library_add_device("anritsu_sg"),
            drag_kind="device:anritsu_sg",
        )

        outputs = group("Advanced output transitions", "5")
        action(
            outputs,
            "Advanced · Keithley A OUTPUT ON",
            "Expert transition only: enable channel A after an earlier plan-owned, validated configuration",
            "keithley",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_output_on("keithley", "A"),
            drag_kind="output:keithley_a",
        )
        action(
            outputs,
            "Advanced · Keithley B OUTPUT ON",
            "Expert transition only: enable channel B after an earlier plan-owned, validated configuration",
            "keithley",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_output_on("keithley", "B"),
            drag_kind="output:keithley_b",
        )
        action(
            outputs,
            "Advanced · Rigol CH1 OUTPUT ON",
            "Expert transition only: enable channel 1 after an earlier plan-owned configuration",
            "rigol",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_output_on("rigol", 1),
            drag_kind="output:rigol_1",
        )
        action(
            outputs,
            "Advanced · Rigol CH2 OUTPUT ON",
            "Expert transition only: enable channel 2 after an earlier plan-owned configuration",
            "rigol",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_output_on("rigol", 2),
            drag_kind="output:rigol_2",
        )
        action(
            outputs,
            "Advanced · Anritsu SG RF OUTPUT ON",
            "Expert transition only: enable RF after an earlier plan-owned SG configuration",
            "anritsu",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_output_on("anritsu_sg", None),
            drag_kind="output:anritsu_sg",
        )

        acquisition = group("Acquisition", "4")
        action(
            acquisition,
            "MOKE Hall (V + field)",
            "Read Hall 1 voltage and store the derived base-polynomial field at this sweep point. Read-only; no VOUT or gain command.",
            "moke_box",
            QStyle.StandardPixmap.SP_DialogApplyButton,
            lambda: self._library_add_basic("measure_moke_hall"),
            drag_kind="flow:measure_moke_hall",
        )
        action(
            acquisition,
            "Measure Lake Shore field",
            "Store a read-only Lake Shore 475 DC, RMS, or peak field checkpoint.",
            "lakeshore_gaussmeter",
            QStyle.StandardPixmap.SP_DialogApplyButton,
            lambda: self._library_add_basic("measure_lakeshore_field"),
            drag_kind="flow:measure_lakeshore_field",
        )
        action(
            acquisition,
            "Acquire reference",
            "Acquire and freeze the Anritsu reference used by processed checkpoints",
            "anritsu",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            lambda: self._library_add_basic("acquire_reference"),
            drag_kind="flow:acquire_reference",
        )
        action(
            acquisition,
            "Acquire spectrum once",
            "Read one qualified TRAC1 spectrum at the current loop point",
            "anritsu",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_basic("acquire_spectrum"),
            drag_kind="flow:acquire_spectrum",
        )

        safety = group("Safe shutdown", "7")
        action(
            safety,
            "Keithley A OUTPUT OFF",
            "Add an immediate channel A OUTPUT OFF to Finally (no ramp)",
            "keithley",
            QStyle.StandardPixmap.SP_MediaStop,
            lambda: self._library_add_output_off(
                "set_keithley_output", channel="A"
            ),
            drag_kind="safety:keithley_a_off",
        )
        action(
            safety,
            "Keithley B OUTPUT OFF",
            "Add an immediate channel B OUTPUT OFF to Finally (no ramp)",
            "keithley",
            QStyle.StandardPixmap.SP_MediaStop,
            lambda: self._library_add_output_off(
                "set_keithley_output", channel="B"
            ),
            drag_kind="safety:keithley_b_off",
        )
        action(
            safety, "Keithley A RAMP TO ZERO + OFF (optional)",
            "Add a bounded channel A ramp followed by OUTPUT OFF to Finally", "keithley",
            QStyle.StandardPixmap.SP_MediaStop,
            lambda: self._library_add_keithley_shutdown("A"),
            drag_kind="safety:keithley_a",
        )
        action(
            safety, "Keithley B RAMP TO ZERO + OFF (optional)",
            "Add a bounded channel B ramp followed by OUTPUT OFF to Finally", "keithley",
            QStyle.StandardPixmap.SP_MediaStop,
            lambda: self._library_add_keithley_shutdown("B"),
            drag_kind="safety:keithley_b",
        )
        action(
            safety, "Rigol CH1 OFF", "Add Rigol channel 1 OUTPUT OFF to Finally", "rigol",
            QStyle.StandardPixmap.SP_MediaStop,
            lambda: self._library_add_output_off("set_rigol_output", channel=1),
            drag_kind="safety:rigol_1",
        )
        action(
            safety, "Rigol CH2 OFF", "Add Rigol channel 2 OUTPUT OFF to Finally", "rigol",
            QStyle.StandardPixmap.SP_MediaStop,
            lambda: self._library_add_output_off("set_rigol_output", channel=2),
            drag_kind="safety:rigol_2",
        )
        action(
            safety, "Anritsu SG OFF",
            "Add signal-generator RF OFF to Finally; the spectrum analyzer input has no source output", "anritsu",
            QStyle.StandardPixmap.SP_MediaStop,
            lambda: self._library_add_output_off("set_anritsu_sg_output"),
            drag_kind="safety:anritsu_sg",
        )

        flow = group("Flow", "4")
        action(
            flow, "Wait", "Add a settling delay", "timing",
            QStyle.StandardPixmap.SP_BrowserReload,
            lambda: self._library_add_basic("wait"),
            drag_kind="flow:wait",
        )
        action(
            flow, "Sequence / group", "Add a sequential container", "structure",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            lambda: self._library_add_basic("sequence"),
            drag_kind="flow:sequence",
        )
        action(
            flow,
            "Wrap in Repeat...",
            "Wrap the selected block, or every root step, in a non-empty Repeat",
            "structure",
            QStyle.StandardPixmap.SP_DirIcon,
            self._wrap_selected_in_repeat,
            drag_kind=None,
        )
        action(
            flow, "Comment", "Document this part of the measurement", "structure",
            QStyle.StandardPixmap.SP_MessageBoxInformation,
            lambda: self._library_add_basic("comment"),
            drag_kind="flow:comment",
        )
        layout.addStretch(1)
        hint = BodyLabel(
            "Click to add after the selected step, or drag to an exact gap. "
            "Drop inside only when a flow container is highlighted. "
            "Every device block must be configured before validation; incomplete "
            "blocks are rejected without executing their children."
        )
        hint.setObjectName("recipeHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._update_lakeshore_library_availability()
        self.library_search.textChanged.connect(self._filter_library_actions)
        scroll.setWidget(panel)
        return scroll

    def _show_library_action_menu(
        self, button: QWidget, position: object, label: str, callback: object
    ) -> None:
        menu = RoundMenu(parent=self)
        add_after = QAction("Add after selected step", menu)
        add_inside = QAction("Add inside selected flow container", menu)
        current = self.tree.currentItem()
        current_node = (
            current.data(0, Qt.ItemDataRole.UserRole)
            if current is not None
            else None
        )
        add_inside.setEnabled(
            isinstance(current_node, RecipeNode)
            and RecipeTreeWidget.node_accepts_children(current_node)
        )
        add_after.triggered.connect(
            lambda: self._invoke_library_action(label, callback)
        )
        add_inside.triggered.connect(
            lambda: self._invoke_library_action_inside(label, callback)
        )
        menu.addAction(add_after)
        menu.addAction(add_inside)
        menu.exec(button.mapToGlobal(position))  # type: ignore[arg-type]

    def _invoke_library_action_inside(self, label: str, callback: object) -> None:
        self._library_force_inside = True
        try:
            self._invoke_library_action(label, callback)
        finally:
            self._library_force_inside = False

    def _invoke_library_action(self, label: str, callback: object) -> None:
        """Keep every library click inside a controlled UI error boundary."""

        try:
            if not callable(callback):
                raise ConfigurationError(
                    f"Library action {label!r} is not available."
                )
            callback()
        except Exception as exc:
            self.status.emit(
                f"Library action rejected without changing the plan: {exc}"
            )
            QMessageBox.warning(
                self,
                "Cannot add block",
                f"{label} was not added. The measurement tree is unchanged.\n\n{exc}",
            )

    def _tree_drop_rejected(self, message: str) -> None:
        self.summary.setText(f"Drag-and-drop rejected: {message}")
        self._tree_drag_status_changed(message, False)
        self.status.emit(f"TREE_DROP_REJECTED | {message}")

    def _tree_drag_status_changed(self, message: str, valid: bool) -> None:
        text = message.strip()
        if not text:
            text = (
                "Apply the YAML draft before moving blocks."
                if self._yaml_draft_pending()
                else "A line inserts between steps; a highlighted container inserts inside it."
            )
        self.drag_feedback.setText(text)
        self.drag_feedback.setProperty(
            "safetyState", "" if valid else "caution"
        )
        self.drag_feedback.style().unpolish(self.drag_feedback)
        self.drag_feedback.style().polish(self.drag_feedback)

    def _filter_library_actions(self, query: str) -> None:
        needle = query.strip().casefold()
        for button in self._library_action_buttons:
            haystack = f"{button.text()} {button.toolTip()}".casefold()
            button.setVisible(not needle or needle in haystack)

    def _update_lakeshore_library_availability(self) -> None:
        lakeshore_enabled = self._settings.lakeshore_gaussmeter.enabled and bool(
            self._settings.lakeshore_gaussmeter.resource
        )
        moke_enabled = self._settings.moke_box.enabled and bool(
            self._settings.moke_box.endpoint
        )
        for button in self._library_action_buttons:
            if button.property("deviceKind") == "lakeshore_gaussmeter":
                button.setEnabled(lakeshore_enabled)
                if not lakeshore_enabled:
                    button.setToolTip(
                        "Configure enabled=true and a VISA resource for Lake Shore 475 in Settings before adding this read-only checkpoint."
                    )
                else:
                    button.setToolTip(str(button.property("libraryDescription")))
            elif button.property("deviceKind") == "moke_box":
                button.setEnabled(moke_enabled)
                if not moke_enabled:
                    button.setToolTip(
                        "Configure enabled=true and a verified endpoint for MOKE Box in Settings before adding this read-only checkpoint."
                    )
                else:
                    button.setToolTip(str(button.property("libraryDescription")))

    def _library_add_basic(
        self,
        kind: str,
        *,
        parent_id: str | None = None,
        branch: str | None = None,
        index: int | None = None,
    ) -> None:
        if parent_id is None or branch is None:
            parent_id, branch, index = self._library_default_destination()
        self._add_basic_node(
            kind,
            parent_id=parent_id,
            branch=branch,
            insert_index=index,
        )

    def _library_add_keithley_shutdown(self, channel: str) -> None:
        self._add_finally_nodes(
            [
                {
                    "id": self._new_node_id("ramp-keithley-zero"),
                    "type": "ramp_keithley_to_zero",
                    "channel": channel,
                    "deadline": "30 s",
                },
                {
                    "id": self._new_node_id("keithley-output-off"),
                    "type": "set_keithley_output",
                    "channel": channel,
                    "enabled": False,
                },
            ],
            f"Added Keithley {channel} safe shutdown",
        )

    def _current_keithley_shutdown_choice(self) -> tuple[set[str], str]:
        recipe = parse_recipe_text(self._builder_source(), origin="tree-builder")
        channels: set[str] = set()
        deadline = "30 s"
        for node in recipe.finally_nodes:
            if node.id not in _KEITHLEY_OPTIONAL_SHUTDOWN_RAMP_IDS.values():
                continue
            channel = str(node.data.get("channel", "")).upper()
            expected_id = _KEITHLEY_OPTIONAL_SHUTDOWN_RAMP_IDS.get(channel)
            if node.type != "ramp_keithley_to_zero" or node.id != expected_id:
                raise ConfigurationError(
                    f"{node.id}: reserved shutdown node id is not a managed Keithley ramp."
                )
            channels.add(channel)
            deadline = str(node.data.get("deadline", deadline))
        return channels, deadline

    def _set_keithley_shutdown_choice(
        self,
        ramp_channels: set[str],
        deadline: str,
    ) -> None:
        source, selected_id = set_keithley_shutdown_ramps_in_recipe(
            self._builder_source(),
            ramp_channels=ramp_channels,
            deadline=deadline,
        )
        self._apply_builder_source(
            source,
            "Updated Keithley shutdown method",
            selected_node_id=selected_id,
        )

    def _edit_automatic_shutdown(self, action: str) -> None:
        if action == "keithley.outputs_off":
            try:
                channels, deadline = self._current_keithley_shutdown_choice()
            except Exception as exc:
                QMessageBox.warning(self, "Keithley shutdown method", str(exc))
                return
            dialog = KeithleyShutdownMethodDialog(
                ramp_channels=channels,
                deadline=deadline,
                parent=self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                self._set_keithley_shutdown_choice(
                    dialog.selected_ramp_channels(),
                    dialog.ramp_deadline(),
                )
            except Exception as exc:
                QMessageBox.warning(self, "Keithley shutdown method", str(exc))
            return
        QMessageBox.information(
            self,
            "Automatic shutdown",
            "This automatic shutdown action is always immediate and cannot be changed here.",
        )

    def _library_add_output_on(
        self,
        device: str,
        channel: str | int | None,
        *,
        parent_id: str | None = None,
        branch: str | None = None,
        index: int | None = None,
    ) -> None:
        try:
            if parent_id is None or branch is None:
                parent_id, branch, index = self._library_default_destination()
            if parent_id == "__finally__":
                raise ConfigurationError("OUTPUT ON cannot be added to Finally.")
            if device == "keithley":
                if channel not in {"A", "B"}:
                    raise ConfigurationError("Keithley OUTPUT ON requires channel A or B.")
                node = {
                    "id": self._new_node_id("keithley-output-on"),
                    "type": "set_keithley_output",
                    "channel": channel,
                    "enabled": True,
                }
            elif device == "rigol":
                if channel not in {1, 2}:
                    raise ConfigurationError("Rigol OUTPUT ON requires channel 1 or 2.")
                node = {
                    "id": self._new_node_id("rigol-output-on"),
                    "type": "enable_rigol_output",
                    "channel": channel,
                }
            elif device == "anritsu_sg":
                node = {
                    "id": self._new_node_id("anritsu-sg-output-on"),
                    "type": "enable_anritsu_sg_output",
                }
            else:
                raise ConfigurationError(f"Unknown output device {device!r}.")
            source = add_recipe_node(
                self._builder_source(),
                parent_id=parent_id,
                branch=branch,
                index=index,
                node=node,
            )
            self._apply_builder_source(
                source,
                f"Added {device} channel {channel} OUTPUT ON",
                selected_node_id=str(node["id"]),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Add OUTPUT ON", str(exc))

    def _library_add_output_off(
        self, kind: str, *, channel: str | int | None = None
    ) -> None:
        node: dict[str, object] = {
            "id": self._new_node_id("output-off"),
            "type": kind,
            "enabled": False,
        }
        if channel is not None:
            node["channel"] = channel
        self._add_finally_nodes([node], "Added output OFF to safe shutdown")

    def _add_finally_nodes(
        self, nodes: list[dict[str, object]], status: str
    ) -> None:
        source = self._builder_source()
        try:
            for node in nodes:
                source = add_recipe_node(
                    source,
                    parent_id="__finally__",
                    branch="children",
                    node=node,
                )
            self._apply_builder_source(
                source,
                status,
                selected_node_id=str(nodes[-1]["id"]),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Add safe shutdown", str(exc))

    def _library_add_device(
        self,
        device: str,
        *,
        parent_id: str | None = None,
        branch: str | None = None,
        index: int | None = None,
    ) -> None:
        labels = {
            "keithley": "Keithley 2600",
            "rigol": "Rigol DG1032Z",
            "anritsu": "Anritsu MS2830A",
            "anritsu_sg": "Anritsu MS2830A Signal Generator",
        }
        if device not in labels:
            raise ConfigurationError(f"Unknown device module {device!r}.")
        if parent_id is None or branch is None:
            parent_id, branch, index = self._library_default_destination()
        if parent_id == "__finally__":
            QMessageBox.warning(
                self,
                "Cannot add device",
                "Finally accepts only ramp-to-zero and OUTPUT OFF safety actions.",
            )
            return
        if device == "anritsu_sg":
            node = {
                "id": self._new_node_id("anritsu-sg"),
                "type": "sequence",
                "text": "Anritsu SG — configuration required · RF OFF",
                "device_module": "anritsu_sg",
                "label": labels[device],
                "operation": "configure_selected_parameters",
                "configuration_required": True,
                "parameter_actions": [],
                "children": [],
            }
        elif device == "anritsu":
            node_id = self._new_node_id("anritsu-spectrum")
            node = self._configured_anritsu_node(
                RecipeNode(
                    id=node_id,
                    type="sequence",
                    data={"device_module": "anritsu"},
                ),
                snapshot=self._current_anritsu_snapshot(),
                parameter_actions=[],
                acquire_single=False,
                trace="TRAC1",
            )
        else:
            node = {
                "id": self._new_node_id(device),
                "type": "sequence",
                "text": f"{labels[device]} — configuration required",
                "device_module": device,
                "label": labels[device],
                "configuration_required": True,
                "children": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Drop nested devices or flow actions here",
                    }
                ],
            }
        source = add_recipe_node(
            self._builder_source(),
            parent_id=parent_id,
            branch=branch,
            index=index,
            node=node,
        )
        if device == "anritsu_sg":
            self._apply_builder_source(
                source,
                "Added editable Anritsu signal-generator module",
                selected_node_id=str(node["id"]),
            )
            self.summary.setText(
                "Anritsu SG added. Double-click it to configure frequency, power, "
                "and the RF output lifecycle. The safe default keeps RF OFF."
            )
        elif device == "anritsu":
            self._apply_builder_source(
                source,
                "Added editable Anritsu spectrum module",
                selected_node_id=str(node["id"]),
            )
            self.summary.setText(
                "Anritsu configuration added from the current Spectrum settings. "
                "Double-click it to change the snapshot or append a clearly "
                "separated spectrum/reference acquisition step."
            )
        else:
            self._apply_builder_source(
                source,
                f"Added {labels[device]} module placeholder",
                selected_node_id=str(node["id"]),
            )
            self.summary.setText(
                f"{labels[device]} added — configuration required. "
                "This placeholder cannot execute instrument commands."
            )

    def _drop_library_block(
        self,
        drag_kind: str,
        parent_id: str,
        branch: str,
        index: int,
    ) -> None:
        try:
            category, separator, kind = drag_kind.partition(":")
            if not separator:
                raise ConfigurationError(
                    f"Malformed library block identifier {drag_kind!r}."
                )
            if parent_id == "__finally__" and category in {"device", "flow"}:
                raise ConfigurationError(
                    "Finally accepts only ramp-to-zero and OUTPUT OFF safety actions."
                )
            if category == "device":
                self._library_add_device(
                    kind, parent_id=parent_id, branch=branch, index=index
                )
                return
            if category == "safety":
                if kind in {"keithley_a_off", "keithley_b_off"}:
                    self._library_add_output_off(
                        "set_keithley_output",
                        channel=kind.removesuffix("_off")[-1].upper(),
                    )
                elif kind in {"keithley_a", "keithley_b"}:
                    self._library_add_keithley_shutdown(kind[-1].upper())
                elif kind in {"rigol_1", "rigol_2"}:
                    self._library_add_output_off(
                        "set_rigol_output", channel=int(kind[-1])
                    )
                elif kind == "anritsu_sg":
                    self._library_add_output_off("set_anritsu_sg_output")
                else:
                    raise ConfigurationError(f"Unknown safe-shutdown block {kind!r}.")
                return
            if category == "output":
                if kind == "anritsu_sg":
                    self._library_add_output_on(
                        "anritsu_sg",
                        None,
                        parent_id=parent_id,
                        branch=branch,
                        index=index,
                    )
                    return
                device, separator, channel_text = kind.partition("_")
                if not separator:
                    raise ConfigurationError(f"Unknown OUTPUT block {kind!r}.")
                channel: str | int = (
                    channel_text.upper()
                    if device == "keithley"
                    else int(channel_text)
                )
                self._library_add_output_on(
                    device,
                    channel,
                    parent_id=parent_id,
                    branch=branch,
                    index=index,
                )
                return
            if category != "flow":
                raise ConfigurationError(
                    f"Unknown library block category {category!r}."
                )
            self._library_add_basic(
                kind,
                parent_id=parent_id,
                branch=branch,
                index=index,
            )
        except Exception as exc:
            self.status.emit(f"Library drop rejected without changing the plan: {exc}")
            QMessageBox.warning(
                self,
                "Cannot add block",
                f"The block was not added. The existing measurement tree is unchanged.\n\n{exc}",
            )

    def _library_add_fixed_keithley(self, mode: str) -> None:
        definition = next(
            item
            for item in _SWEEPABLE_PARAMETERS
            if item["target"] == f"keithley.B.{mode}"
        )
        dialog = FixedValueDialog(definition, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            parent_id, branch = self._builder_parent()
            source = add_recipe_node(
                self._builder_source(),
                parent_id=parent_id,
                branch=branch,
                node=self._fixed_node_from_dialog(definition, dialog),
            )
            self._apply_builder_source(source, f"Added fixed Keithley {mode} setting")
        except Exception as exc:
            QMessageBox.warning(self, "Add Keithley setting", str(exc))

    def set_settings(self, settings: StationSettings) -> None:
        previous_default_output_directory = self._default_output_directory()
        self._settings = settings
        current_output_directory = self.output_directory.text().strip()
        if not current_output_directory or current_output_directory == previous_default_output_directory:
            self.output_directory.setText(self._default_output_directory())
        self._update_lakeshore_library_availability()
        self.recipe_profile_badge.setText("LIMITS + READBACK ACTIVE")
        self.recipe_profile_badge.setProperty("safetyState", "verified")
        self.recipe_profile_badge.setToolTip(
            "Device permissions, configured station limits, explicit OUTPUT "
            "actions and hardware readback govern output operations."
        )
        self.recipe_profile_badge.style().unpolish(self.recipe_profile_badge)
        self.recipe_profile_badge.style().polish(self.recipe_profile_badge)
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self._refresh_document_state()

    def _recipe_tokens(self) -> ThemeTokens:
        mode = str(self._settings.ui.get("theme", "system"))
        return tokens_for(effective_theme(mode))

    def set_keithley_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Keithley form state."""

        self._keithley_snapshot_provider = provider

    def set_rigol_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Rigol carrier form state."""

        self._rigol_snapshot_provider = provider

    def set_anritsu_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Anritsu spectrum form state."""

        self._anritsu_snapshot_provider = provider

    def set_anritsu_sg_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Anritsu SG form state."""

        self._anritsu_sg_snapshot_provider = provider

    def _source_changed(self) -> None:
        if self._loading_source:
            return
        self._close_discard_confirmed = False
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self.summary.setText(
            "YAML draft changed. Apply it to the tree before using Tree Builder; "
            "then validate it again before running."
        )
        self._autosave_timer.start()
        self._refresh_document_state()

    def _path_changed(self, _text: str = "") -> None:
        self._close_discard_confirmed = False
        self._refresh_document_state()

    def _yaml_draft_pending(self) -> bool:
        return self.editor.toPlainText() != self._tree_source

    def _is_document_dirty(self) -> bool:
        if self._historical_sweep_active:
            return False
        source = self.editor.toPlainText()
        if self._saved_source is None:
            return bool(source.strip())
        return (
            source != self._saved_source
            or self.path.text().strip() != (self._saved_path or "")
        )

    def _tree_editing_allowed(self) -> bool:
        return (
            not self._historical_sweep_active
            and not self._execution_controlled
            and not self._yaml_draft_pending()
        )

    def _set_tree_editing_enabled(self, enabled: bool) -> None:
        effective = bool(enabled) and not self._execution_controlled
        self.tree.setDragEnabled(effective)
        self.tree.setAcceptDrops(effective)
        self.tree.setDropIndicatorShown(effective)
        # Keep the library surface enabled while a run owns the recipe so its
        # scrollbar and search remain usable.  Only its mutating buttons are
        # disabled by ``set_execution_controlled``.
        self.library_panel.setEnabled(
            True if self._execution_controlled else bool(enabled)
        )
        for shortcut in getattr(self, "_tree_shortcuts", ()):
            shortcut.setEnabled(effective)
        if not self._historical_sweep_active:
            current = self.tree.currentItem()
            self._node_selected(current, None)
        if effective:
            self.tree.setToolTip(
                "Drag a non-root node to reorder it or place it inside Sequence, "
                "Sweep, Repeat or If. The complete YAML is validated before the "
                "view changes; nodes cannot cross the Finally boundary."
            )
        elif self._execution_controlled:
            self.tree.setToolTip(
                "The Run Engine owns this recipe during execution. The tree is "
                "read-only until the run finishes."
            )
        else:
            self.tree.setToolTip(
                "Tree Builder is locked because the YAML draft has not been applied. "
                "Correct the YAML and choose Apply YAML to tree."
            )

    def set_execution_controlled(self, controlled: bool) -> None:
        """Make the recipe inspectable, but immutable, while a run is active.

        The route itself remains enabled so operators can inspect the exact
        recipe and switch to device readback pages.  We snapshot each
        control's pre-run state and restore it after completion instead of
        guessing whether it was disabled by validation, permissions or an
        incomplete selection.
        """

        controlled = bool(controlled)
        if controlled == self._execution_controlled:
            self.execution_lock_banner.setVisible(controlled)
            self.workspace_state.setText(
                "RUNNING · READ-ONLY" if controlled else "READY TO EDIT"
            )
            return
        if controlled:
            self._execution_controlled = True
            self._execution_widget_states = {
                widget: widget.isEnabled()
                for widget in self._execution_edit_widgets
            }
            self._execution_action_states = {
                action: action.isEnabled()
                for action in self._execution_edit_actions
            }
            self._execution_editor_read_only = self.editor.isReadOnly()
            for widget in self._execution_edit_widgets:
                widget.setEnabled(False)
            for action in self._execution_edit_actions:
                action.setEnabled(False)
            self.editor.setReadOnly(True)
            self._set_tree_editing_enabled(False)
            self.execution_lock_banner.show()
            self.document_state_badge.setText("RUNNING · READ-ONLY")
            self.document_state_badge.setToolTip(
                "The Run Engine owns this recipe while the measurement is active."
            )
            self.document_state_badge.setProperty("safetyState", "caution")
            self.workspace_state.setText("RUNNING · READ-ONLY")
            self.workspace_state.setProperty("safetyState", "caution")
        else:
            self._execution_controlled = False
            for widget, enabled in self._execution_widget_states.items():
                if widget is not None:
                    widget.setEnabled(enabled)
            for action, enabled in self._execution_action_states.items():
                if action is not None:
                    action.setEnabled(enabled)
            self.editor.setReadOnly(self._execution_editor_read_only)
            self._execution_widget_states.clear()
            self._execution_action_states.clear()
            self._set_tree_editing_enabled(self._tree_editing_allowed())
            self._refresh_document_state()
            self.execution_lock_banner.hide()
            self.workspace_state.setText("READY TO EDIT")
            self.workspace_state.setProperty("safetyState", "verified")
        for label in (self.workspace_state,):
            label.style().unpolish(label)
            label.style().polish(label)
        self.document_state_badge.style().unpolish(self.document_state_badge)
        self.document_state_badge.style().polish(self.document_state_badge)

    def _refresh_document_state(self) -> None:
        if not hasattr(self, "document_state_badge"):
            return
        pending = self._yaml_draft_pending()
        dirty = self._is_document_dirty()
        if self._execution_controlled:
            text = "RUNNING · READ-ONLY"
            safety_state = "caution"
            tooltip = (
                "The Run Engine owns this recipe while the measurement is active. "
                "Editing resumes after the run finishes."
            )
        elif self._historical_sweep_active:
            text = "RECORDED"
            safety_state = "verified"
            tooltip = "This is an immutable historical execution."
        elif pending:
            text = "YAML DRAFT - APPLY"
            safety_state = "caution"
            tooltip = (
                "The editor differs from the visible tree. Tree commands stay "
                "locked until the complete YAML draft is valid and applied."
            )
        elif self._saved_source is None:
            text = "DRAFT - UNSAVED"
            safety_state = "caution"
            tooltip = "This recipe draft does not have a saved YAML version yet."
        elif dirty:
            text = "DRAFT - UNSAVED"
            safety_state = "caution"
            tooltip = "The tree is current, but these recipe changes are not saved."
        elif self._plan is None:
            text = "SAVED - VALIDATION STALE"
            safety_state = "caution"
            tooltip = "The recipe is saved; validate it against the current station profile."
        else:
            text = "READY"
            safety_state = "verified"
            tooltip = "The saved recipe matches the visible tree and validated plan."
        self.document_state_badge.setText(text)
        self.document_state_badge.setToolTip(tooltip)
        self.document_state_badge.setProperty("safetyState", safety_state)
        self.document_state_badge.style().unpolish(self.document_state_badge)
        self.document_state_badge.style().polish(self.document_state_badge)
        self.apply_yaml_action.setEnabled(
            pending
            and not self._historical_sweep_active
            and not self._execution_controlled
        )
        self.apply_yaml_action.setToolTip(
            "Parse the complete YAML draft and replace the measurement tree atomically"
            if pending
            else "The YAML editor and measurement tree already match"
        )
        self.save_recipe_action.setEnabled(dirty and not self._execution_controlled)
        self._set_tree_editing_enabled(self._tree_editing_allowed())
        self._update_tree_history_controls()
        self._tree_drag_status_changed("", True)
        self._refresh_output_preview()

    def _builder_source(self) -> str:
        if not self._tree_editing_allowed():
            raise ConfigurationError(
                "Tree Builder is locked while the YAML draft differs from the tree. "
                "Apply a valid YAML draft first."
            )
        return self._tree_source

    def apply_yaml_to_tree(self, *, show_error: bool = True) -> bool:
        """Commit the YAML editor draft as one validated document transaction."""

        if self._historical_sweep_active:
            return False
        source = self.editor.toPlainText()
        if source == self._tree_source:
            self._refresh_document_state()
            return True
        try:
            self._apply_builder_source(
                source,
                "Applied YAML draft to the measurement tree",
            )
        except Exception as exc:
            self.summary.setText(
                "YAML draft was not applied. The previous tree is still active and "
                f"locked while you correct the source. {exc}"
            )
            self.status.emit(f"YAML draft rejected without changing the tree: {exc}")
            self._refresh_document_state()
            if show_error:
                QMessageBox.warning(
                    self,
                    "Cannot apply YAML",
                    "The complete YAML draft is invalid. The previous measurement "
                    f"tree was retained.\n\n{exc}",
                )
            return False
        return True

    def _confirm_discard_unsaved(self, action: str) -> bool:
        if not self._is_document_dirty():
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved sweep",
            f"{action} will discard changes that have not been saved. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def load_editor(self, *, show_error: bool = True) -> None:
        self._leave_historical_sweep_mode()
        target = Path(self.path.text()).expanduser()
        if target.suffix.lower() in {".h5", ".hdf5"}:
            self.load_hdf5_result(target, show_error=show_error)
            return
        try:
            source = target.read_text(encoding="utf-8")
        except Exception as exc:
            if show_error:
                QMessageBox.warning(self, "Recipe", f"Cannot load YAML: {exc}")
            else:
                self.summary.setText(f"Example not loaded: {exc}")
            return
        self._loading_source = True
        self.editor.setPlainText(source)
        self._loading_source = False
        self._autosave_timer.stop()
        self._close_discard_confirmed = False
        self._saved_source = source
        self._saved_path = self.path.text().strip()
        self._tree_undo.clear()
        self._tree_redo.clear()
        self._update_tree_history_controls()
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        tree_rendered = False
        try:
            recipe = parse_recipe_text(source, origin=self.path.text())
            self._populate_recipe_tree(recipe.root, recipe.finally_nodes, None)
            self._tree_source = source
            self.summary.setText("Recipe loaded. Compile it before running.")
            tree_rendered = True
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_LOAD_REJECTED",
                path=self.path.text(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            self.summary.setText(
                "Recipe could not be rendered. The previous tree remains visible; "
                "see Event log for diagnostics."
            )
        self._update_repository_state()
        self._refresh_document_state()
        self.status.emit(
            "Recipe loaded into the editor and measurement tree"
            if tree_rendered
            else "Recipe source loaded; invalid measurement tree was not applied"
        )

    @property
    def historical_sweep_active(self) -> bool:
        """Whether the tree represents an immutable, recorded THATEC execution."""
        return self._historical_sweep_active

    def load_historical_thatec_sweep(
        self, run: ThatecRun, tree: tuple[ThatecTreeNode, ...]
    ) -> None:
        """Render the public THATEC execution hierarchy in the Sweep workspace.

        A standard THATEC result stores what was executed, but not necessarily the
        application's original safety policy or declarative YAML.  It is therefore
        deliberately presented as a non-runnable historical sweep rather than a
        guessed recipe.
        """
        if not self.cancel_preflight():
            QMessageBox.warning(
                self,
                "Validation still stopping",
                "The historical result cannot replace the current workspace while "
                "recipe validation is still active. Try again after cancellation finishes.",
            )
            return
        self._historical_sweep_active = True
        self._plan = None
        self.plan_preflight_changed.emit(None)
        self.run_button.setEnabled(False)
        self.compile_recipe_action.setEnabled(False)
        self.editor.setReadOnly(True)
        self.tree.setDragEnabled(False)
        self.tree.setAcceptDrops(False)
        self.tree.setDropIndicatorShown(False)
        self.library_panel.setEnabled(False)
        for button in (
            self.edit_device_button,
            self.edit_generator_button, self.delete_node_button,
            self.duplicate_node_button, self.move_up_button, self.move_down_button,
            self.wrap_repeat_button,
            self.open_editor_button,
        ):
            button.setEnabled(False)
        self.path.setText(str(run.path))
        self._loading_source = True
        self.editor.setPlainText(
            "# Historical THATEC Sweep (read-only)\n"
            f"# Source result: {run.path.name}\n"
            "# The executed tree and recorded device settings are shown in the tree.\n"
            "# A standard THATEC result does not contain enough information to\n"
            "# reconstruct unrecorded safety policy as an executable YAML recipe.\n"
        )
        self._loading_source = False
        historical_source = self.editor.toPlainText()
        self._tree_source = historical_source
        self._saved_source = historical_source
        self._saved_path = self.path.text().strip()
        self._autosave_timer.stop()
        self._tree_undo.clear()
        self._tree_redo.clear()

        root = QTreeWidgetItem([
            f"Historical THATEC Sweep — {run.path.name}",
            "Recorded execution tree",
            "READ-ONLY",
        ])
        root.setToolTip(0, str(run.path))
        root.setData(0, Qt.ItemDataRole.UserRole, "historical-thatec-root")

        def add_node(parent: QTreeWidgetItem, node: ThatecTreeNode) -> None:
            row = run.rows.get(node.id)
            detail = node.kind or (row.function if row is not None else "recorded")
            item = QTreeWidgetItem([node.label, detail, "RECORDED"])
            item.setData(0, Qt.ItemDataRole.UserRole, row)
            item.setData(1, Qt.ItemDataRole.UserRole, node.id)
            if row is not None:
                item.setToolTip(
                    0,
                    "\n".join((
                        f"THATEC row: {row.id}",
                        f"Device: {row.device_name or 'internal'}",
                        f"Control: {row.control_name or 'internal'}",
                        f"Function: {row.function or node.kind}",
                    )),
                )
            parent.addChild(item)
            for child in node.children:
                add_node(item, child)

        for node in tree:
            add_node(root, node)
        if run.devices:
            devices_item = QTreeWidgetItem([
                "Recorded device configuration", "THATEC /devices", "RECORDED",
            ])
            devices_item.setData(0, Qt.ItemDataRole.UserRole, "historical-thatec-devices")
            root.addChild(devices_item)
            for device in run.devices:
                device_item = QTreeWidgetItem([device.name, "device settings", "RECORDED"])
                device_item.setData(0, Qt.ItemDataRole.UserRole, device)
                device_item.setToolTip(0, "All public THATEC settings recorded for this device.")
                devices_item.addChild(device_item)
        self.tree.clear()
        self.tree.addTopLevelItem(root)
        self.tree.expandAll()
        self.tree.setCurrentItem(root)
        self.inspector_summary.setText(
            "<b>Historical THATEC Sweep</b><br>Read-only execution tree reconstructed from the HDF5 result."
        )
        self.inspector.setPlainText(
            f"Result: {run.path}\nRows: {len(run.rows)}\nDevices: {len(run.devices)}\n\n"
            "Select a recorded node to inspect its public definition, metadata and recorded dimensions."
        )
        self.summary.setText(
            "Historical THATEC Sweep loaded. It is read-only because the source result "
            "does not prove every safety and recipe field needed for re-execution."
        )
        self._refresh_document_state()
        self.status.emit("Historical THATEC Sweep reconstructed")

    def load_reconstructed_thatec_sweep(
        self, run: ThatecRun, tree: tuple[ThatecTreeNode, ...]
    ) -> None:
        """Restore an editable Sweep when its public THATEC labbook has YAML.

        External THATEC results normally only contain an execution tree; those
        remain available through the historical renderer.  Files written here
        carry their validated source in ``labbook/parameter`` and can therefore
        reopen as an ordinary Sweep without guessing action semantics.
        """
        source = run.recipe_source.strip()
        if not source:
            self.load_historical_thatec_sweep(run, tree)
            return
        self._leave_historical_sweep_mode()
        self.path.setText(str(run.path))
        self._apply_builder_source(source, "Restored Sweep from THATEC labbook")
        self._saved_source = None
        self._saved_path = None
        self._refresh_document_state()
        self.summary.setText(
            "Sweep restored from public THATEC labbook. Validate it against the current "
            "station profile before running."
        )
        self.status.emit("Editable Sweep restored from THATEC labbook")

    def _leave_historical_sweep_mode(self) -> None:
        if not self._historical_sweep_active:
            return
        self._historical_sweep_active = False
        self.editor.setReadOnly(False)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.library_panel.setEnabled(True)
        self.compile_recipe_action.setEnabled(True)
        for button in (
            self.edit_device_button,
            self.edit_generator_button, self.delete_node_button,
            self.duplicate_node_button, self.move_up_button, self.move_down_button,
            self.wrap_repeat_button,
        ):
            button.setEnabled(True)

    def new_recipe(self, *, confirm: bool = True) -> None:
        """Start an empty, valid plan without touching the currently saved file."""

        if confirm and not self._confirm_discard_unsaved("Starting a new sweep"):
            return
        self._leave_historical_sweep_mode()
        source = (
            "schema_version: 1\n"
            "name: Untitled sweep\n"
            "root:\n"
            "  id: sequence-main\n"
            "  type: sequence\n"
            "  children: []\n"
            "finally: []\n"
        )
        self.path.setText(str(Path("recipes") / "untitled_sweep.yml"))
        self._apply_builder_source(source, "Created a new empty sweep")
        self._saved_source = None
        self._saved_path = None
        self._tree_undo.clear()
        self._tree_redo.clear()
        self._update_tree_history_controls()
        self._refresh_document_state()
        self.status.emit("New empty sweep ready")

    def browse_recipe(self) -> None:
        """Choose a YAML recipe with the native file explorer and load it."""

        current_text = self.path.text().strip()
        current = Path(current_text).expanduser() if current_text else Path("recipes")
        if not current.is_absolute():
            current = (Path.cwd() / current).resolve()
        initial_location = current if current.exists() else current.parent
        selected, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open sweep recipe",
            str(initial_location),
            "YAML recipes (*.yml *.yaml);;All files (*)",
        )
        if not selected:
            return
        if not self._confirm_discard_unsaved("Loading another recipe"):
            return
        self.path.setText(str(Path(selected)))
        self.load_editor()

    def browse_hdf5_result(self) -> None:
        """Choose a THATEC result and rebuild its tree in the Sweep workspace."""
        current_text = self.path.text().strip()
        current = Path(current_text).expanduser() if current_text else Path("measurements")
        initial_location = current if current.is_dir() else current.parent
        selected, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open THATEC HDF5 result",
            str(initial_location),
            "HDF5 results (*.h5 *.hdf5);;All files (*)",
        )
        if selected and self._confirm_discard_unsaved("Loading a result"):
            self.load_hdf5_result(Path(selected))

    def load_hdf5_result(
        self, path: str | Path, *, show_error: bool = True
    ) -> None:
        """Load a THATEC HDF5 result without passing binary data to the YAML parser."""
        target = Path(path).expanduser()
        try:
            run = ThatecRunReader.describe(target)
            tree = ThatecRunReader.tree(target)
            self.load_reconstructed_thatec_sweep(run, tree)
        except Exception as exc:
            if show_error:
                QMessageBox.warning(self, "THATEC HDF5", f"Cannot load HDF5 result: {exc}")
            else:
                self.summary.setText(f"HDF5 result could not be loaded: {exc}")

    def save_recipe(self) -> bool:
        if self._historical_sweep_active:
            QMessageBox.information(
                self,
                "Recorded sweep",
                "Historical results are read-only and cannot replace a recipe YAML file.",
            )
            return False
        if not self.apply_yaml_to_tree():
            return False
        source = self._tree_source
        try:
            result = self._repository.save(self.path.text(), source)
        except Exception as exc:
            QMessageBox.warning(self, "Recipe", f"YAML was not saved: {exc}")
            return False
        self._saved_source = source
        self._saved_path = self.path.text().strip()
        self._autosave_timer.stop()
        self.restore_button.setEnabled(False)
        self._update_repository_state()
        suffix = f"; previous version: {result.backup_path}" if result.backup_path else ""
        self.status.emit(f"Recipe saved atomically: {result.path}{suffix}")
        self._refresh_document_state()
        return True

    def restore_autosave(self) -> None:
        try:
            source = self._repository.load_recovery(self.path.text())
        except Exception as exc:
            QMessageBox.warning(
                self, "Restore autosave", f"Cannot load autosave recovery: {exc}"
            )
            return
        if source is None:
            self.restore_button.setEnabled(False)
            return
        try:
            self._apply_builder_source(source, "Unsaved recipe recovery restored")
        except Exception as exc:
            # Recovery deliberately also preserves temporarily invalid YAML.
            # Keep the last valid tree visible, but make the source/restoration
            # state explicit and keep Run disabled until validation succeeds.
            self._loading_source = True
            self.editor.setPlainText(source)
            self._loading_source = False
            self._close_discard_confirmed = False
            self._plan = None
            self.run_button.setEnabled(False)
            self.plan_preflight_changed.emit(None)
            self.summary.setText(
                "Autosave source restored, but its YAML is invalid; the previous "
                f"tree remains visible until the source is corrected. {exc}"
            )
            self.status.emit("Invalid autosave source restored for manual repair")
            self._update_tree_history_controls()
            self.restore_button.setEnabled(False)
            self._refresh_document_state()
            return
        self.restore_button.setEnabled(False)
        self._refresh_document_state()
        self.status.emit("Unsaved recipe recovery restored into the editor")

    def _autosave(self) -> None:
        source = self.editor.toPlainText()
        if (
            not self._is_document_dirty()
            or not source.strip()
            or not self.path.text().strip()
        ):
            return
        try:
            recovery = self._repository.autosave(self.path.text(), source)
        except Exception as exc:
            self.status.emit(f"Recipe autosave failed: {exc}")
            return
        self.restore_button.setEnabled(True)
        self.restore_button.setToolTip(f"Unsaved editor recovery: {recovery}")

    def _update_repository_state(self) -> None:
        try:
            versions = self._repository.versions(self.path.text())
            recovery = self._repository.has_newer_recovery(self.path.text())
        except Exception as exc:
            self.restore_button.setEnabled(False)
            self.version_label.setText(f"Version history unavailable: {exc}")
            self.status.emit(f"Recipe repository state unavailable: {exc}")
            return
        self.restore_button.setEnabled(recovery)
        self.version_label.setText(
            f"Immutable previous versions: {len(versions)}"
            + (" • newer autosave recovery available" if recovery else "")
        )

    def compile_recipe(self) -> None:
        """Synchronous preflight API used by deterministic tests and automation."""

        if not self.apply_yaml_to_tree():
            return
        try:
            self._repair_missing_anritsu_snapshots()
            recipe = parse_recipe_text(self._tree_source, origin=self.path.text())
            outputs_forced_off = (
                self.execution_mode.currentData() == "dry_run"
            )
            plan = RecipeCompiler(
                self._settings, outputs_forced_off=outputs_forced_off
            ).compile(recipe)
            estimate = PlanEstimator(self._settings).estimate(plan)
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_COMPILE_REJECTED",
                path=self.path.text(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            self._show_recipe_error("Recipe", str(exc))
            return
        self._accept_preflight(recipe, plan, estimate)

    def compile_recipe_async(self) -> None:
        """Run operator-triggered validation without blocking the Qt event loop."""

        if (
            self._preflight_thread is not None
            and self._preflight_thread.isRunning()
        ):
            self._preflight_thread.requestInterruption()
            self.compile_recipe_action.setEnabled(False)
            self.compile_recipe_action.setText("Cancelling…")
            self.summary.setText("Cancelling recipe validation…")
            return
        if not self.apply_yaml_to_tree():
            return
        try:
            self._repair_missing_anritsu_snapshots()
        except Exception as exc:
            self._show_recipe_error("Recipe", str(exc))
            return
        source = self._tree_source
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self._preflight_source = source
        outputs_forced_off = self.execution_mode.currentData() == "dry_run"
        self._preflight_outputs_forced_off = outputs_forced_off
        self.compile_recipe_action.setText("Cancel validation")
        self.summary.setText(
            "Validating recipe and estimating time/data size in the background…"
        )
        self.status.emit("Recipe validation started")
        thread = QThread(self)
        worker = RecipePreflightWorker(
            self._settings,
            source,
            self.path.text(),
            outputs_forced_off=outputs_forced_off,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._preflight_succeeded)
        worker.failed.connect(self._preflight_failed)
        worker.cancelled.connect(self._preflight_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._preflight_finished)
        thread.finished.connect(thread.deleteLater)
        self._preflight_thread = thread
        self._preflight_worker = worker
        thread.start()

    def _repair_missing_anritsu_snapshots(self) -> bool:
        """Upgrade legacy Anritsu placeholders from the current visible form."""

        recipe = parse_recipe_text(self._tree_source, origin=self.path.text())

        def visit(node: RecipeNode) -> list[RecipeNode]:
            found = [node]
            for child in (*node.children, *node.else_children):
                found.extend(visit(child))
            return found

        candidates = [
            node
            for root in (recipe.root, *recipe.finally_nodes)
            for node in visit(root)
            if node.data.get("device_module") == "anritsu"
            and node.data.get("operation") == "configure_selected_parameters"
            and not isinstance(node.data.get("configuration"), dict)
        ]
        if not candidates:
            return False
        snapshot = self._current_anritsu_snapshot()
        source = self._tree_source
        for node in candidates:
            raw_actions = node.data.get("parameter_actions", [])
            actions = (
                [dict(action) for action in raw_actions if isinstance(action, dict)]
                if isinstance(raw_actions, list)
                else []
            )
            replacement = self._configured_anritsu_node(
                node,
                snapshot=snapshot,
                parameter_actions=actions,
                acquire_single=bool(node.data.get("acquire_single", False)),
                trace=str(node.data.get("trace", "TRAC1")),
                post_configuration_operation=str(
                    node.data.get("post_configuration_operation", "configure")
                ),
                acquisition_average_count=int(
                    node.data.get("acquisition_average_count", 1)
                ),
                acquisition_reference_operation=str(
                    node.data.get("acquisition_reference_operation", "none")
                ),
            )
            source = replace_recipe_node(source, node_id=node.id, node=replacement)
        self._apply_builder_source(
            source,
            "Upgraded legacy Anritsu node(s) from the current Spectrum form",
            selected_node_id=candidates[0].id,
        )
        return True

    def _preflight_succeeded(
        self, recipe: object, plan: object, estimate: object
    ) -> None:
        current_outputs_forced_off = (
            self.execution_mode.currentData() == "dry_run"
        )
        if (
            self._preflight_source != self.editor.toPlainText()
            or self._preflight_outputs_forced_off != current_outputs_forced_off
        ):
            self.summary.setText(
                "Recipe changed during validation; the stale result was discarded. "
                "Validate the current source again."
            )
            self._refresh_document_state()
            self.status.emit("Stale recipe validation discarded")
            return
        if not isinstance(plan, ExecutionPlan) or not isinstance(
            estimate, PlanEstimate
        ):
            self._preflight_failed("Preflight returned an invalid result.")
            return
        if not hasattr(recipe, "root") or not hasattr(recipe, "finally_nodes"):
            self._preflight_failed("Preflight returned an invalid recipe snapshot.")
            return
        self._accept_preflight(recipe, plan, estimate)

    def _preflight_failed(self, error: str) -> None:
        self._emit_tree_diagnostic(
            "TREE_COMPILE_REJECTED",
            path=self.path.text(),
            error_type="BackgroundPreflightError",
            error=error,
        )
        self.summary.setText(f"Validation blocked: {error}")
        self._refresh_document_state()
        self._show_recipe_error("Recipe", error)

    def _show_recipe_error(self, title: str, error: str) -> None:
        """Offer the one editor that can actually resolve a validation error."""

        incomplete_node = re.match(
            r"^(?P<node_id>[^:]+): (?:device configuration is incomplete\.|"
            r"Anritsu provider requires a complete configuration snapshot\.|"
            r"incomplete Anritsu spectrum snapshot\.)",
            error,
        )
        if incomplete_node is not None:
            if QMessageBox.action_guidance(
                self, title, error, "Go to configuration..."
            ):
                self._open_incomplete_node_configuration(
                    incomplete_node.group("node_id")
                )
            return
        settings_issue = settings_issue_for_error(error)
        if settings_issue is not None:
            if QMessageBox.settings_guidance(self, title, error):
                self.settings_issue_requested.emit(settings_issue)
            return
        QMessageBox.warning(self, title, error)

    def _open_incomplete_node_configuration(self, node_id: str) -> None:
        """Select the rejected recipe node and open its resolving editor."""

        item = self._find_tree_item(node_id)
        if item is None:
            QMessageBox.warning(
                self,
                "Recipe",
                f"The incomplete recipe node {node_id!r} is no longer present.",
            )
            return
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(
            item, QAbstractItemView.ScrollHint.PositionAtCenter
        )
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(node, RecipeNode):
            return
        if node.data.get("device_module") == "anritsu":
            self._edit_anritsu_module_node(node, highlight_required=True)
            return
        self._edit_selected_node()

    def _preflight_cancelled(self) -> None:
        self.summary.setText("Recipe validation cancelled; Run remains disabled.")
        self._refresh_document_state()
        self.status.emit("Recipe validation cancelled")

    def _preflight_finished(self) -> None:
        self.compile_recipe_action.setText("Validate & preview")
        self.compile_recipe_action.setEnabled(not self._historical_sweep_active)
        self._preflight_thread = None
        self._preflight_worker = None
        self._preflight_source = None
        self._preflight_outputs_forced_off = None
        self._refresh_document_state()

    def cancel_preflight(self, *, wait_ms: int = 3_000) -> bool:
        """Stop an in-flight validation before the owning window is destroyed."""

        thread = self._preflight_thread
        if thread is None or not thread.isRunning():
            return True
        thread.requestInterruption()
        thread.quit()
        return thread.wait(wait_ms)

    def confirm_close(self) -> bool:
        """Return whether the workspace may close without losing recipe edits."""

        if self._close_discard_confirmed or not self._is_document_dirty():
            return True
        self._close_discard_confirmed = self._confirm_discard_unsaved(
            "Closing the application"
        )
        return self._close_discard_confirmed

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.confirm_close():
            event.ignore()
            return
        if not self.cancel_preflight():
            event.ignore()
            return
        super().closeEvent(event)

    def _accept_preflight(
        self, recipe: object, plan: ExecutionPlan, estimate: PlanEstimate
    ) -> None:
        try:
            self._populate_recipe_tree(
                recipe.root, recipe.finally_nodes, plan  # type: ignore[attr-defined]
            )
        except Exception as exc:
            self._plan = None
            self.run_button.setEnabled(False)
            self.plan_preflight_changed.emit(None)
            self._emit_tree_diagnostic(
                "TREE_PREFLIGHT_RENDER_REJECTED",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            self.summary.setText(
                "Validation succeeded, but the operator tree could not be rendered. "
                "Run remains disabled."
            )
            self._refresh_document_state()
            QMessageBox.warning(
                self,
                "Recipe preview",
                f"The validated plan could not be displayed safely:\n\n{exc}",
            )
            return
        self._plan = plan
        self.run_button.setEnabled(True)
        self._refresh_document_state()
        self.summary.setText(
            f"Plan: {len(plan.actions)} actions • {plan.total_points} checkpoints • "
            f"{plan.total_spectra} spectra • hash {plan.sha256}\n"
            f"Estimated nominal duration: {_human_duration(estimate.nominal_duration_s)} • "
            f"retry upper model: {_human_duration(estimate.retry_upper_duration_s)}\n"
            f"Uncompressed data upper estimate: {_human_bytes(estimate.total_upper_bytes)} • "
            f"spectrum values: {estimate.spectrum_values:,}\n"
            + (
                "Warnings: " + " | ".join(estimate.warnings) + "\n"
                if estimate.warnings
                else "Warnings: none from static preflight\n"
            )
            + "Compilation sends no instrument commands. Execution uses Run Engine and revalidates station and hardware limits."
        )
        self.status.emit("Recipe compiled")
        self.plan_preflight_changed.emit((plan, estimate))

    def _populate_recipe_tree(
        self,
        root: RecipeNode,
        finally_nodes: tuple[RecipeNode, ...],
        plan: object | None,
        selected_id: str | None = None,
    ) -> None:
        tokens = self._recipe_tokens()
        current = self.tree.currentItem()
        current_node = (
            current.data(0, Qt.ItemDataRole.UserRole)
            if current is not None
            else None
        )
        if selected_id is None and isinstance(current_node, RecipeNode):
            selected_id = current_node.id
        expanded_ids: set[str] = set()

        def remember_expansion(item: QTreeWidgetItem) -> None:
            item_node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(item_node, RecipeNode) and item.isExpanded():
                expanded_ids.add(item_node.id)
            for child_index in range(item.childCount()):
                remember_expansion(item.child(child_index))

        for top_index in range(self.tree.topLevelItemCount()):
            remember_expansion(self.tree.topLevelItem(top_index))
        previous_scroll = self.tree.verticalScrollBar().value()
        top_level_items: list[QTreeWidgetItem] = []
        occurrences: dict[str, int] = {}
        if plan is not None:
            for action in plan.actions:  # type: ignore[union-attr]
                occurrences[action.node_id] = occurrences.get(action.node_id, 0) + 1

        def add_node(node: RecipeNode, parent: QTreeWidgetItem | None = None) -> None:
            count = occurrences.get(node.id, 0)
            detail = node.type + (f" • {count} action(s)" if plan is not None else "")
            label, detail, icon = self._tree_presentation(node, count, plan is not None)
            if parent is None and node.type == "sequence":
                label = "Measurement sequence"
                detail = "Runs top-level steps in order"
            if node.data.get("disabled") is True:
                label = f"Disabled — {label}"
                detail = "Skipped with all children"
            status_text, status_color = self._tree_status(node, detail)
            item = QTreeWidgetItem([label, detail, status_text])
            item.setIcon(0, self._tree_node_icon(node, icon))
            item.setToolTip(0, f"{node.id}\nDouble-click to edit; right-click for actions.")
            item.setToolTip(2, status_text.title())
            item.setData(2, Qt.ItemDataRole.ForegroundRole, QBrush(QColor(status_color)))
            status_font = item.font(2)
            status_font.setBold(True)
            status_font.setPointSize(8)
            item.setFont(2, status_font)
            item.setData(0, Qt.ItemDataRole.UserRole, node)
            if parent is None:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                top_level_items.append(item)
            else:
                parent.addChild(item)
            self._add_operator_control_rows(node, item)
            self._add_native_sweep_roi_rows(node, item)
            child_parent = item
            if node.type in {"sweep", "repeat"} or (
                node.data.get("device_module")
                and RecipeTreeWidget.node_accepts_children(node)
            ):
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
                    RecipeTreeWidget.structural_role,
                    RecipeTreeWidget.execution_container,
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
            if node.type == "if":
                else_item = QTreeWidgetItem(["Else branch", "Conditional alternative", "●"])
                else_item.setData(
                    0,
                    RecipeTreeWidget.structural_role,
                    RecipeTreeWidget.else_container,
                )
                else_item.setFlags(
                    else_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled
                )
                item.addChild(else_item)
                for child in node.else_children:
                    add_node(child, else_item)

        add_node(root)
        automatic_shutdown = self._automatic_shutdown_projection(plan, finally_nodes)
        cleanup_detail = (
            f"Automatic shutdown • {len(automatic_shutdown)} guaranteed action(s)"
            + (
                f" • {len(finally_nodes)} operator cleanup action(s)"
                if finally_nodes
                else ""
            )
        )
        cleanup = QTreeWidgetItem(["Finally — safe shutdown", cleanup_detail, "●"])
        cleanup.setIcon(0, self._tree_badge_icon("Safety", tokens.neutral, "OFF"))
        cleanup.setData(0, Qt.ItemDataRole.UserRole, None)
        cleanup.setData(
            0,
            RecipeTreeWidget.structural_role,
            RecipeTreeWidget.finally_container,
        )
        cleanup.setFlags(cleanup.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
        cleanup.setToolTip(
            0,
            "System-generated OUTPUT OFF actions always run after success, stop "
            "and fault. Optional operator cleanup, such as a Keithley ramp, runs "
            "before the independent automatic shutdown.",
        )
        top_level_items.append(cleanup)
        for node in finally_nodes:
            add_node(node, cleanup)
        for action, label, detail in automatic_shutdown:
            item = QTreeWidgetItem([label, detail, "AUTO"])
            item.setIcon(0, self._tree_badge_icon("Safety", tokens.neutral, "OFF"))
            item.setFlags(
                item.flags()
                & ~Qt.ItemFlag.ItemIsDragEnabled
                & ~Qt.ItemFlag.ItemIsDropEnabled
                & ~Qt.ItemFlag.ItemIsEditable
            )
            item.setData(0, Qt.ItemDataRole.UserRole, None)
            item.setData(
                0,
                RecipeTreeWidget.structural_role,
                f"automatic_shutdown:{action}",
            )
            item.setToolTip(
                0,
                "Generated by the compiler and executed by the Run Engine. "
                "It cannot be removed or disabled from the recipe tree.",
            )
            item.setForeground(2, QBrush(QColor(tokens.success)))
            status_font = item.font(2)
            status_font.setBold(True)
            status_font.setPointSize(8)
            item.setFont(2, status_font)
            cleanup.addChild(item)
        # Commit only after every row, synthetic control and icon was built.
        # A presentation exception must never erase the operator's current tree.
        self.tree.clear()
        self.tree.addTopLevelItems(top_level_items)
        if expanded_ids:
            for node_id in expanded_ids:
                item = self._find_tree_item(node_id)
                if item is not None:
                    item.setExpanded(True)
        else:
            self.tree.expandAll()
        selected = (
            self._find_tree_item(selected_id) if selected_id is not None else None
        )
        if selected is not None:
            ancestor = selected.parent()
            while ancestor is not None:
                ancestor.setExpanded(True)
                ancestor = ancestor.parent()
            self.tree.setCurrentItem(selected)
            self.tree.scrollToItem(
                selected,
                QAbstractItemView.ScrollHint.EnsureVisible,
            )
        elif self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.tree.verticalScrollBar().setValue(previous_scroll)

    @staticmethod
    def _automatic_shutdown_projection(
        plan: object | None,
        finally_nodes: tuple[RecipeNode, ...] = (),
    ) -> tuple[tuple[str, str, str], ...]:
        """Describe the immutable shutdown manifest rendered below Finally."""

        actions = getattr(plan, "safe_shutdown_actions", None)
        if not isinstance(actions, (tuple, list)) or not actions:
            actions = (
                "keithley.outputs_off",
                "rigol.outputs_off",
                "anritsu.rf_off_and_abort",
                "storage.flush_checkpoint",
            )
        ramp_channels = tuple(
            str(node.data.get("channel", "")).upper()
            for node in finally_nodes
            if node.type == "ramp_keithley_to_zero"
            and str(node.data.get("channel", "")).upper() in {"A", "B"}
        )
        keithley_detail = "Automatic - both SMU channels - confirmed safe state"
        if ramp_channels:
            keithley_detail += (
                f" - optional ramp first: {', '.join(sorted(set(ramp_channels)))}"
            )
        presentation = {
            "keithley.outputs_off": (
                "Keithley A + B OUTPUT OFF",
                "Automatic • both SMU channels • confirmed safe state",
            ),
            "rigol.outputs_off": (
                "Rigol CH1 + CH2 OUTPUT OFF",
                "Automatic • both generator channels • confirmed safe state",
            ),
            "anritsu.abort_acquisition": (
                "Anritsu • abort acquisition",
                "Automatic • stop active spectrum acquisition",
            ),
            "anritsu.rf_off_and_abort": (
                "Anritsu RF OUTPUT OFF + abort",
                "Automatic • RF source OFF and spectrum acquisition stopped",
            ),
            "storage.flush_checkpoint": (
                "Measurement checkpoint flush",
                "Automatic • persist the latest completed measurement boundary",
            ),
        }
        rows: list[tuple[str, str, str]] = []
        for raw_action in actions:
            action = str(raw_action)
            label, detail = presentation.get(
                action,
                (action, "Automatic safe-shutdown operation"),
            )
            rows.append((action, label, detail))
        return tuple(rows)

    def execution_tree_snapshot(
        self,
        recipe_source: str,
        plan: object | None,
    ) -> tuple[QTreeWidgetItem, ...]:
        """Return a detached, read-only projection made by the Builder renderer.

        Execute must never maintain a second interpretation of a recipe.  The
        snapshot is rendered by :meth:`_populate_recipe_tree` into a temporary
        tree and then cloned, so labels, ROI rows, structural branches, icons
        and static status cells are byte-for-byte the same projection seen in
        Sweep Builder.  The visible Builder tree is never changed.
        """

        recipe = parse_recipe_text(recipe_source, origin="execution plan")
        original_tree = self.tree
        staging_tree = RecipeTreeWidget(self)
        try:
            self.tree = staging_tree
            self._populate_recipe_tree(recipe.root, recipe.finally_nodes, plan)
            return tuple(
                staging_tree.topLevelItem(index).clone()
                for index in range(staging_tree.topLevelItemCount())
            )
        finally:
            self.tree = original_tree
            staging_tree.deleteLater()

    @staticmethod
    def _keithley_parameter_label(node: RecipeNode, parameter_id: str) -> str:
        return {
            "source.level": (
                "Source current"
                if node.data.get("source_mode") == "current"
                else "Source voltage"
            ),
            "source.compliance": "Voltage compliance"
            if node.data.get("source_mode") == "current"
            else "Current compliance",
            "measurement.nplc": "NPLC",
            "measurement.settling_time": "Settling time",
            "measurement.sense_mode": "Sense mode",
            "source.range": "Source range",
            "measurement.voltage_range": "Measure V range",
            "measurement.current_range": "Measure I range",
        }.get(parameter_id, parameter_id)

    @staticmethod
    def _keithley_parameter_dimension(
        node: RecipeNode, parameter_id: str
    ) -> str | None:
        source_is_current = node.data.get("source_mode") == "current"
        return {
            "source.level": (
                DIMENSION_CURRENT if source_is_current else DIMENSION_VOLTAGE
            ),
            "source.compliance": (
                DIMENSION_VOLTAGE if source_is_current else DIMENSION_CURRENT
            ),
            "measurement.settling_time": DIMENSION_TIME,
        }.get(parameter_id)

    def _add_operator_control_rows(
        self, node: RecipeNode, parent: QTreeWidgetItem
    ) -> None:
        """Render device overrides as read-only operator controls below the module."""

        tokens = self._recipe_tokens()
        device_module = str(node.data.get("device_module", ""))
        if device_module not in {"keithley", "anritsu", "anritsu_sg", "rigol"}:
            return
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [action for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )

        def informational_row(
            columns: list[str],
            *,
            color: str,
            badge: str,
            metadata: dict[str, object] | None = None,
            tooltip: str | None = None,
            owner: QTreeWidgetItem = parent,
        ) -> QTreeWidgetItem:
            row = QTreeWidgetItem(columns)
            if metadata is None:
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            else:
                row.setData(0, self.operator_row_role, metadata)
                row.setToolTip(
                    0,
                    tooltip or "Click to open the ROI editor directly for this sweep axis.",
                )
                link_font = row.font(0)
                link_font.setUnderline(True)
                row.setFont(0, link_font)
                row.setData(
                    0, Qt.ItemDataRole.ForegroundRole, QBrush(QColor(tokens.accent))
                )
            row.setIcon(0, self._tree_badge_icon("Operator control", color, badge))
            if metadata is None:
                row.setToolTip(
                    0,
                    "Operator control summary. Double-click the parent device node to edit it.",
                )
            row.setData(2, Qt.ItemDataRole.ForegroundRole, QBrush(QColor(color)))
            status_font = row.font(2)
            status_font.setBold(True)
            status_font.setPointSize(8)
            row.setFont(2, status_font)
            owner.addChild(row)
            return row

        for action in actions:
            parameter_id = str(action.get("parameter_id", ""))
            label = (
                self._keithley_parameter_label(node, parameter_id)
                if device_module == "keithley"
                else {
                    "spectrum.start_frequency": "Start frequency",
                    "spectrum.stop_frequency": "Stop frequency",
                    "spectrum.reference_level": "Reference level",
                    "spectrum.points": "Trace points",
                    "sg.frequency": "RF frequency",
                    "sg.power": "RF power",
                    "advanced.rbw_mode": "RBW mode",
                    "advanced.rbw": "Resolution bandwidth",
                    "advanced.vbw_mode": "VBW mode",
                    "advanced.vbw": "Video bandwidth",
                    "advanced.detector": "Detector",
                    "advanced.attenuation_mode": "RF attenuation mode",
                    "advanced.attenuation": "RF attenuation",
                    "advanced.preamplifier_enabled": "Preamplifier",
                    "advanced.sweep_time_mode": "Sweep-time mode",
                    "advanced.sweep_time": "Sweep time",
                }.get(parameter_id, parameter_id)
            )
            mode = str(action.get("mode", "set"))
            value = str(action.get("value", ""))
            if mode != "sweep":
                informational_row(
                    [label, f"Set to {value}", "SET"],
                    color=tokens.neutral,
                    badge="=",
                )
                continue
            segments = action.get("segments")
            dimension = (
                self._keithley_parameter_dimension(node, parameter_id)
                if device_module == "keithley"
                else {
                    "spectrum.start_frequency": DIMENSION_FREQUENCY,
                    "spectrum.stop_frequency": DIMENSION_FREQUENCY,
                      "spectrum.reference_level": DIMENSION_DBM,
                      "sg.frequency": DIMENSION_FREQUENCY,
                      "sg.power": DIMENSION_DBM,
                  }.get(parameter_id)
            )
            if not isinstance(segments, list) or not segments or dimension is None:
                informational_row(
                    [label, "Sweep range requires ROI definition", "ROI"],
                    color=tokens.caution,
                    badge="!",
                )
                continue
            try:
                generated = generate_sweep_points(segments, dimension)
                stages = generate_sweep_stage_points(segments, dimension)
                first_value = segments[0].get(
                    "value", segments[0].get("start", "?")
                )
                last_value = segments[-1].get(
                    "value", segments[-1].get("stop", "?")
                )
                sweep_detail = (
                    f"{first_value} → {last_value} · "
                    f"{len(generated):,} pts · {len(segments)} ROI"
                )
            except Exception:
                informational_row(
                    [label, "Invalid sweep range", "ERROR"],
                    color=tokens.danger,
                    badge="!",
                )
                continue
            sweep_row = informational_row(
                [label, sweep_detail, "SWEEP"],
                color=tokens.accent,
                badge="S",
                metadata={
                    "kind": "sweep_parameter",
                    "device_module": device_module,
                    "owner_node_id": node.id,
                    "parameter_id": parameter_id,
                    "stage_index": None,
                },
            )
            for index, (segment, stage_points) in enumerate(
                zip(segments, stages, strict=True), start=1
            ):
                single_value = segment.get("value")
                spacing = (
                    "Single value"
                    if single_value is not None
                    else str(segment.get("spacing", "linear")).title()
                )
                stage_range = (
                    str(single_value)
                    if single_value is not None
                    else f"{segment.get('start', '?')} → {segment.get('stop', '?')}"
                )
                stage = QTreeWidgetItem(
                    [
                        f"ROI {index}",
                        f"{stage_range} · {len(stage_points):,} pts · {spacing}",
                        "STAGE",
                    ]
                )
                stage.setData(
                    0,
                    self.operator_row_role,
                    {
                        "kind": "roi_stage",
                        "device_module": device_module,
                        "owner_node_id": node.id,
                        "parameter_id": parameter_id,
                        "stage_index": index - 1,
                    },
                )
                stage.setIcon(
                    0,
                    self._tree_badge_icon("ROI stage", tokens.focus, str(index)),
                )
                stage_link_font = stage.font(0)
                stage_link_font.setUnderline(True)
                stage.setFont(0, stage_link_font)
                stage.setData(
                    0, Qt.ItemDataRole.ForegroundRole, QBrush(QColor(tokens.accent))
                )
                stage.setForeground(1, QBrush(QColor(tokens.text_muted)))
                stage.setForeground(2, QBrush(QColor(tokens.text_muted)))
                stage.setToolTip(
                    0,
                    "Click to open this stage directly in the ROI editor.",
                )
                sweep_row.addChild(stage)

        if device_module == "anritsu" and node.data.get("acquire_single"):
            informational_row(
                [
                    "Single spectrum",
                    f"Acquire {node.data.get('trace', 'TRAC1')} at each parent-loop point",
                    "ACQUIRE",
                ],
                color=tokens.success,
                badge="A",
            )

        output_policy = str(node.data.get("output_policy", "unchanged"))
        if output_policy in {"unchanged", "on", "off", "on_keep", "continue"}:
            is_on = output_policy in {"on", "on_keep", "continue"}
            description, status, badge = {
                "unchanged": ("Keep OUTPUT OFF (safe default)", "OFF", "OFF"),
                "on": ("Enable safely for this node; switch OFF on exit", "ON → OFF", "ON"),
                "on_keep": ("Enable safely and keep confirmed ON after this node", "KEEP ON", "ON"),
                "continue": ("Inherit confirmed ON; apply live updates only", "CONTINUE", "↻"),
                "off": ("Force and confirm OUTPUT OFF for this node", "OFF", "OFF"),
            }[output_policy]
            informational_row(
                [
                    "Output",
                    description,
                    status,
                ],
                color=tokens.success if is_on else tokens.neutral,
                badge=badge,
                metadata={"kind": "output_policy", "owner_node_id": node.id},
                tooltip="Click to choose the output mode for this device block.",
            )

    @staticmethod
    def _native_sweep_segments(node: RecipeNode) -> list[dict[str, object]]:
        """Return one visual ROI contract for either supported sweep syntax."""

        raw_segments = node.data.get("segments")
        if isinstance(raw_segments, list) and raw_segments:
            return [
                dict(segment)
                for segment in raw_segments
                if isinstance(segment, dict)
            ]
        if node.type != "sweep":
            return []
        if not all(key in node.data for key in ("start", "stop", "points")):
            return []
        return [
            {
                "start": node.data["start"],
                "stop": node.data["stop"],
                "points": node.data["points"],
                "spacing": node.data.get("spacing", "linear"),
            }
        ]

    def _add_native_sweep_roi_rows(
        self, node: RecipeNode, parent: QTreeWidgetItem
    ) -> None:
        """Expose every native sweep interval as a direct, editable ROI row."""

        tokens = self._recipe_tokens()
        if node.type != "sweep":
            return
        target = str(node.data.get("target", ""))
        definition = next(
            (
                item
                for item in _SWEEPABLE_PARAMETERS
                if item["target"] == target
            ),
            None,
        )
        dimension = (
            definition["dimension"]
            if definition is not None
            else SWEEP_DIMENSIONS.get(target)
        )
        segments = self._native_sweep_segments(node)
        if dimension is None or not segments:
            return
        try:
            stages = generate_sweep_stage_points(segments, dimension)
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_ROI_RENDER_REJECTED",
                node_id=node.id,
                target=target,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        for index, (segment, stage_points) in enumerate(
            zip(segments, stages, strict=True), start=1
        ):
            single_value = segment.get("value")
            stage_range = (
                str(single_value)
                if single_value is not None
                else f"{segment.get('start', '?')} → {segment.get('stop', '?')}"
            )
            spacing = (
                "Single value"
                if single_value is not None
                else str(segment.get("spacing", "linear")).title()
            )
            row = QTreeWidgetItem(
                [
                    f"ROI {index}",
                    f"{stage_range} · {len(stage_points):,} pts · {spacing}",
                    "STAGE",
                ]
            )
            row.setData(
                0,
                self.operator_row_role,
                {
                    "kind": "native_sweep_roi",
                    "owner_node_id": node.id,
                    "parameter_id": target,
                    "stage_index": index - 1,
                },
            )
            row.setIcon(
                0, self._tree_badge_icon("ROI stage", tokens.focus, str(index))
            )
            link_font = row.font(0)
            link_font.setUnderline(True)
            row.setFont(0, link_font)
            row.setData(
                0,
                Qt.ItemDataRole.ForegroundRole,
                QBrush(QColor(tokens.accent)),
            )
            row.setForeground(1, QBrush(QColor(tokens.text_muted)))
            row.setForeground(2, QBrush(QColor(tokens.text_muted)))
            row.setToolTip(0, "Click to edit this ROI without changing loop children.")
            parent.addChild(row)

    def _tree_status(self, node: RecipeNode, detail: str) -> tuple[str, str]:
        tokens = self._recipe_tokens()
        if node.data.get("disabled") is True:
            return "DISABLED", tokens.text_muted
        if node.data.get("configuration_required"):
            return "SETUP", tokens.caution
        if (
            node.data.get("operation") == "configure_selected_parameters"
            and (
                node.data.get("device_module")
                not in {"keithley", "rigol", "anritsu", "anritsu_sg"}
                or not isinstance(node.data.get("configuration"), dict)
            )
        ):
            return "PREVIEW", tokens.danger
        if detail.startswith("Sweep axis"):
            return "SWEEP", tokens.accent
        if detail.startswith("Fixed setting") or detail.startswith("Fixed configuration"):
            return "FIXED", tokens.neutral
        if node.type in {"wait", "repeat", "sequence", "comment"}:
            return "FLOW", tokens.focus
        if node.type.startswith(("set_", "ramp_", "enable_")):
            return "SAFE", tokens.success
        if node.type in {
            "acquire_reference",
            "acquire_spectrum",
            "measure_moke_hall",
            "measure_lakeshore_field",
        }:
            return "ACQUIRE", tokens.success
        return "READY", tokens.neutral

    def _tree_presentation(
        self, node: RecipeNode, count: int, compiled: bool
    ) -> tuple[str, str, QStyle.StandardPixmap]:
        """Translate YAML-oriented data into concise operator-facing tree rows."""

        detail = node.type.replace("_", " ")
        icon = QStyle.StandardPixmap.SP_FileIcon
        if node.type == "sequence" and not node.data.get("device_module"):
            if node.id == "sequence-main":
                return (
                    "Measurement sequence",
                    "Runs top-level steps in order",
                    QStyle.StandardPixmap.SP_FileDialogDetailedView,
                )
            return (
                str(node.data.get("text") or "Sequence / group"),
                "Runs child steps in order",
                QStyle.StandardPixmap.SP_FileDialogDetailedView,
            )
        if node.type == "repeat":
            return (
                f"Repeat × {node.data.get('count', '?')}",
                "Runs the nested branch repeatedly",
                QStyle.StandardPixmap.SP_BrowserReload,
            )
        if node.type == "if":
            return (
                "If / Else",
                "Chooses one conditional branch",
                QStyle.StandardPixmap.SP_ArrowRight,
            )
        if node.type == "sweep":
            target = str(node.data.get("target", ""))
            definition = next((item for item in _SWEEPABLE_PARAMETERS if item["target"] == target), None)
            label = definition["label"] if definition else target
            try:
                dimension = definition["dimension"] if definition else SWEEP_DIMENSIONS[target]
                segments = node.data.get("segments")
                if isinstance(segments, list):
                    points = generate_sweep_points(segments, dimension)
                else:
                    points = generate_sweep_points(
                        [{
                            "start": node.data["start"], "stop": node.data["stop"],
                            "points": node.data["points"], "spacing": node.data.get("spacing", "linear"),
                        }], dimension
                    )
                suffix = f" • {len(points):,} pts"
            except Exception:
                suffix = " • invalid points"
            return (
                f"{label} sweep{suffix}",
                "Sweep axis" + (f" • {count} action(s)" if compiled else ""),
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "configure_keithley":
            return (
                f"Keithley {node.data.get('channel', '?')} · {node.data.get('mode', 'source')} = {node.data.get('level', '')}",
                "Fixed setting",
                QStyle.StandardPixmap.SP_DriveHDIcon,
            )
        if node.type == "configure_rigol":
            return f"Rigol CH{node.data.get('channel', '?')} configuration", "Fixed setting", QStyle.StandardPixmap.SP_MediaPlay
        if node.type == "configure_anritsu":
            return "Anritsu spectrum configuration", "Analyzer setting", QStyle.StandardPixmap.SP_ComputerIcon
        if node.type == "acquire_reference":
            return (
                f"Acquire reference spectrum · {node.data.get('trace', 'TRAC1')} · "
                f"average {int(node.data.get('average_count', 1))}",
                "Anritsu reference acquisition",
                QStyle.StandardPixmap.SP_DialogSaveButton,
            )
        if node.type == "acquire_spectrum":
            operation = str(node.data.get("reference_operation", "none"))
            if operation == "difference_db":
                return (
                    f"Acquire spectrum - {node.data.get('trace', 'TRAC1')} - "
                    "store raw + raw-reference",
                    "Anritsu processed acquisition",
                    QStyle.StandardPixmap.SP_MediaPlay,
                )
            return (
                f"Acquire spectrum · {node.data.get('trace', 'TRAC1')} · "
                f"average {int(node.data.get('average_count', 1))}",
                "Anritsu spectrum acquisition",
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "update_keithley_level":
            return (
                f"Set Keithley {node.data.get('channel', '?')} "
                f"{node.data.get('mode', 'source')} = {node.data.get('level', '')}",
                "Point update - OUTPUT unchanged",
                QStyle.StandardPixmap.SP_DriveHDIcon,
            )
        if node.type == "update_rigol_frequency":
            return (
                f"Set Rigol CH{node.data.get('channel', '?')} frequency = "
                f"{node.data.get('frequency', '')}",
                "Point update - OUTPUT unchanged",
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "enable_rigol_output":
            return (
                f"Rigol CH{node.data.get('channel', '?')} OUTPUT ON",
                "Energization · internal one-shot interlock",
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "enable_anritsu_sg_output":
            return (
                "Anritsu SG RF OUTPUT ON",
                "Energization · internal one-shot interlock",
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "set_keithley_output":
            enabled = bool(node.data.get("enabled", False))
            return (
                f"Keithley {node.data.get('channel', '?')} OUTPUT "
                f"{'ON' if enabled else 'OFF'}",
                "Energization" if enabled else "Safety action",
                QStyle.StandardPixmap.SP_MediaPlay
                if enabled
                else QStyle.StandardPixmap.SP_BrowserStop,
            )
        if node.type == "set_rigol_output":
            enabled = bool(node.data.get("enabled", False))
            return (
                f"Rigol CH{node.data.get('channel', '?')} OUTPUT "
                f"{'ON' if enabled else 'OFF'}",
                "Energization" if enabled else "Safety action",
                QStyle.StandardPixmap.SP_MediaPlay
                if enabled
                else QStyle.StandardPixmap.SP_BrowserStop,
            )
        if node.type == "ramp_keithley_to_zero":
            return (
                f"Ramp Keithley {node.data.get('channel', '?')} to zero",
                f"Safety action · deadline {node.data.get('deadline', '10 s')}",
                QStyle.StandardPixmap.SP_BrowserReload,
            )
        if node.type == "set_anritsu_sg_output":
            enabled = bool(node.data.get("enabled", False))
            return (
                f"Anritsu SG RF OUTPUT {'ON' if enabled else 'OFF'}",
                "Energization" if enabled else "Safety action",
                (
                    QStyle.StandardPixmap.SP_MediaPlay
                    if enabled
                    else QStyle.StandardPixmap.SP_BrowserStop
                ),
            )
        if node.type == "measure_keithley":
            return f"Measure Keithley {node.data.get('channel', '?')}", "Measurement", QStyle.StandardPixmap.SP_DialogApplyButton
        if node.type == "measure_moke_hall":
            return (
                "Measure MOKE Hall 1 voltage + field",
                "Read-only checkpoint · one AD7734 sample",
                QStyle.StandardPixmap.SP_DialogApplyButton,
            )
        if node.type == "measure_lakeshore_field":
            return (
                "Measure Lake Shore field",
                "Read-only gaussmeter checkpoint",
                QStyle.StandardPixmap.SP_DialogApplyButton,
            )
        if node.type == "wait":
            return f"Wait {node.data.get('duration', '')}", "Timing", QStyle.StandardPixmap.SP_BrowserReload
        if node.type == "comment":
            text = " ".join(str(node.data.get("text", "")).split())
            return (
                text or "Empty comment",
                "Comment",
                QStyle.StandardPixmap.SP_FileIcon,
            )
        if node.data.get("device_module") == "anritsu_sg":
            actions = [
                action
                for action in node.data.get("parameter_actions", [])
                if isinstance(action, dict)
            ]
            sweep = next(
                (action for action in actions if action.get("mode") == "sweep"),
                None,
            )
            output = str(node.data.get("output_policy", "unchanged"))
            output_label = {
                "on": "RF ON for block",
                "on_keep": "RF KEEP ON",
                "continue": "RF CONTINUE ON",
                "off": "RF OFF",
                "unchanged": "RF OFF",
            }.get(output, "RF invalid")
            return (
                f"Anritsu SG · {len(actions)} parameter(s) · {output_label}",
                "Sweep axis" if sweep is not None else "Fixed configuration",
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
        if node.data.get("device_module") == "anritsu":
            raw_actions = node.data.get("parameter_actions")
            actions = (
                [action for action in raw_actions if isinstance(action, dict)]
                if isinstance(raw_actions, list)
                else []
            )
            sweep = next(
                (action for action in actions if action.get("mode") == "sweep"),
                None,
            )
            role = str(
                node.data.get(
                    "post_configuration_operation",
                    "acquire_spectrum"
                    if node.data.get("acquire_single")
                    else "configure",
                )
            )
            role_label = {
                "configure": "settings only",
                "acquire_spectrum": "then acquire spectrum",
                "acquire_reference": "then acquire reference",
            }.get(role, "invalid role")
            return (
                f"Anritsu Spectrum · {role_label} · {len(actions)} explicit row(s)",
                "Sweep axis" if sweep is not None else "Fixed configuration",
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
        if node.data.get("device_module") == "rigol":
            channel = int(node.data.get("channel", 1))
            output = str(node.data.get("output_policy", "unchanged"))
            output_label = {
                "on": "OUTPUT ON for block",
                "on_keep": "OUTPUT KEEP ON",
                "continue": "OUTPUT CONTINUE ON",
                "off": "OUTPUT OFF",
                "unchanged": "OUTPUT OFF",
            }.get(output, "OUTPUT invalid")
            return (
                f"Rigol CH{channel} · {output_label}",
                "Fixed configuration",
                QStyle.StandardPixmap.SP_DriveFDIcon,
            )
        if node.data.get("device_module"):
            raw_actions = node.data.get("parameter_actions")
            actions = (
                [action for action in raw_actions if isinstance(action, dict)]
                if isinstance(raw_actions, list)
                else []
            )
            output_only = str(node.data.get("output_policy", "unchanged"))
            if (
                node.data.get("device_module") == "keithley"
                and not actions
                and output_only in {"on", "off", "on_keep", "continue"}
            ):
                return (
                    f"Keithley {node.data.get('channel', '?')} · "
                    f"OUTPUT {output_only.replace('_', ' ').upper()}",
                    "Fixed configuration",
                    QStyle.StandardPixmap.SP_BrowserStop,
                )
            if node.data.get("device_module") == "keithley" and actions:
                def action_label(action: dict[str, object]) -> str:
                    parameter_id = str(action.get("parameter_id", "parameter"))
                    return {
                        "source.level": (
                            "Source current"
                            if node.data.get("source_mode") == "current"
                            else "Source voltage"
                        ),
                        "source.compliance": "Compliance",
                        "measurement.nplc": "NPLC",
                        "measurement.settling_time": "Settling time",
                        "measurement.sense_mode": "Sense mode",
                        "source.range": "Source range",
                        "measurement.voltage_range": "Measure V range",
                        "measurement.current_range": "Measure I range",
                    }.get(parameter_id, parameter_id)

                output = str(node.data.get("output_policy", "unchanged"))
                output_suffix = (
                    {
                        "on": " · OUTPUT ON for block",
                        "on_keep": " · OUTPUT KEEP ON",
                        "continue": " · OUTPUT CONTINUE ON",
                        "off": " · OUTPUT OFF",
                    }.get(output, "")
                )
                sweep_action = next(
                    (action for action in actions if action.get("mode") == "sweep"),
                    None,
                )
                point_suffix = ""
                sweep_complete = False
                if sweep_action is not None:
                    segments = sweep_action.get("segments")
                    if isinstance(segments, list) and segments:
                        parameter_id = str(sweep_action.get("parameter_id", ""))
                        source_mode = str(node.data.get("source_mode", "current"))
                        dimension = {
                            "source.level": (
                                DIMENSION_CURRENT
                                if source_mode == "current"
                                else DIMENSION_VOLTAGE
                            ),
                            "source.compliance": (
                                DIMENSION_VOLTAGE
                                if source_mode == "current"
                                else DIMENSION_CURRENT
                            ),
                            "measurement.settling_time": DIMENSION_TIME,
                        }.get(parameter_id)
                        if dimension is not None:
                            try:
                                point_count = len(
                                    generate_sweep_points(segments, dimension)
                                )
                                point_suffix = f" · {point_count:,} pts"
                                sweep_complete = True
                            except Exception:
                                point_suffix = " · invalid ROI"
                if len(actions) == 1:
                    action = actions[0]
                    label = (
                        f"Keithley {node.data.get('channel', '?')} · "
                        f"{action_label(action)} = {action.get('value', '')}"
                        f"{point_suffix}{output_suffix}"
                    )
                else:
                    label = (
                        f"Keithley {node.data.get('channel', '?')} · "
                        f"{len(actions)} parameters{point_suffix}{output_suffix}"
                    )
                has_sweep = any(action.get("mode") == "sweep" for action in actions)
                return (
                    label,
                    (
                        "Sweep axis"
                        if has_sweep and sweep_complete
                        else "ROI required" if has_sweep else "Fixed configuration"
                    ),
                    (
                        QStyle.StandardPixmap.SP_MediaPlay
                        if has_sweep
                        else QStyle.StandardPixmap.SP_DriveHDIcon
                    ),
                )
            override = node.data.get("parameter_override")
            if (
                node.data.get("device_module") == "keithley"
                and isinstance(override, dict)
            ):
                parameter_id = str(override.get("parameter_id", "parameter"))
                parameter_label = {
                    "source.level": (
                        "Source current"
                        if node.data.get("source_mode") == "current"
                        else "Source voltage"
                    ),
                    "source.compliance": "Compliance",
                    "measurement.nplc": "NPLC",
                    "measurement.settling_time": "Settling time",
                    "measurement.sense_mode": "Sense mode",
                    "source.range": "Source range",
                    "measurement.voltage_range": "Measure V range",
                    "measurement.current_range": "Measure I range",
                }.get(parameter_id, parameter_id)
                return (
                    f"Keithley {node.data.get('channel', '?')} · "
                    f"{parameter_label} = "
                    f"{override.get('value', '')}",
                    "Fixed setting",
                    QStyle.StandardPixmap.SP_DriveHDIcon,
                )
            configuration = node.data.get("configuration")
            if (
                node.data.get("device_module") == "keithley"
                and isinstance(configuration, dict)
            ):
                axis = node.data.get("axis")
                if isinstance(axis, dict):
                    segments = axis.get("segments")
                    point_count = ""
                    if isinstance(segments, list):
                        try:
                            dimension = (
                                DIMENSION_CURRENT
                                if configuration.get("source_mode") == "current"
                                else DIMENSION_VOLTAGE
                            )
                            point_count = (
                                f" · {len(generate_sweep_points(segments, dimension)):,} pts"
                            )
                        except Exception:
                            point_count = " · invalid ROI"
                    return (
                        f"Keithley {configuration.get('channel', '?')} · "
                        f"{configuration.get('source_mode', 'source')} sweep{point_count}",
                        "Sweep axis",
                        QStyle.StandardPixmap.SP_MediaPlay,
                    )
                return (
                    f"Keithley {configuration.get('channel', '?')} · "
                    f"{configuration.get('source_mode', 'source')} = "
                    f"{configuration.get('source_level', '')}",
                    "Fixed setting",
                    QStyle.StandardPixmap.SP_DriveHDIcon,
                )
            return (
                str(node.data.get("label", "Device module")),
                "Configuration required",
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
        if node.type.startswith("set_") or node.type.startswith("ramp_"):
            return detail.title(), "Safety action", QStyle.StandardPixmap.SP_BrowserStop
        return node.id, detail + (f" • {count} action(s)" if compiled else ""), icon

    def _tree_node_icon(self, node: RecipeNode, fallback: QStyle.StandardPixmap) -> QIcon:
        tokens = self._recipe_tokens()
        target = str(node.data.get("target", ""))
        if (
            target.startswith("keithley.")
            or node.type.startswith("configure_keithley")
            or node.type in {"measure_keithley", "update_keithley_level"}
        ):
            return self._tree_badge_icon("Keithley", tokens.danger, "K")
        if (
            target.startswith("rigol.")
            or node.type.startswith("configure_rigol")
            or node.type.startswith("set_rigol")
            or node.type == "update_rigol_frequency"
        ):
            return self._tree_badge_icon("Rigol", tokens.caution, "R")
        if (
            target.startswith("anritsu.")
            or "anritsu" in node.type
            or node.type in {"acquire_reference", "acquire_spectrum"}
        ):
            return self._tree_badge_icon("Anritsu", tokens.success, "A")
        if node.type == "measure_moke_hall":
            return self._tree_badge_icon("MOKE Box", tokens.accent, "M")
        if node.type == "measure_lakeshore_field":
            return self._tree_badge_icon("Lake Shore", tokens.focus, "L")
        module = node.data.get("device_module")
        if module == "keithley":
            return self._tree_badge_icon("Keithley", tokens.danger, "K")
        if module == "rigol":
            return self._tree_badge_icon("Rigol", tokens.caution, "R")
        if module == "anritsu":
            return self._tree_badge_icon("Anritsu", tokens.success, "A")
        if node.type in {"wait", "repeat"}:
            return self._tree_badge_icon("Timing", tokens.focus, "T")
        if node.type == "sequence":
            return self._tree_badge_icon("Structure", tokens.accent, "≡")
        if node.type.startswith("set_") or node.type.startswith("ramp_"):
            return self._tree_badge_icon("Safety", tokens.neutral, "OFF")
        return self.style().standardIcon(fallback)

    def _tree_badge_icon(self, _name: str, color: str, text: str) -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(1, 1, 22, 22, 5, 5)
        painter.setPen(QColor(self._recipe_tokens().on_emergency))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(8 if len(text) == 1 else 5)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return QIcon(pixmap)

    def _node_selected(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        editable_now = self._tree_editing_allowed()
        selected_node = (
            item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        )
        movable = (
            editable_now
            and isinstance(selected_node, RecipeNode)
            and item is not None
            and item.parent() is not None
        )
        wrap_possible = bool(
            editable_now
            and item is not None
            and not self._tree_item_is_in_finally(item)
            and (
                (
                    isinstance(selected_node, RecipeNode)
                    and (item.parent() is not None or bool(selected_node.children))
                )
                or (
                    RecipeTreeWidget.structural_kind(item)
                    == RecipeTreeWidget.else_container
                    and RecipeTreeWidget._logical_child_count(item) > 0
                )
            )
        )
        self.delete_node_button.setEnabled(movable)
        self.duplicate_node_button.setEnabled(movable)
        self.move_up_button.setEnabled(movable)
        self.move_down_button.setEnabled(movable)
        self.wrap_repeat_button.setEnabled(wrap_possible)
        if item is None:
            self.selection_context.setText("Select a block in the measurement tree")
            self.inspector.clear()
            self.inspector_summary.setText("Select a node to see its measurement role and configuration.")
            self.open_editor_button.setEnabled(False)
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(False)
            return
        self.selection_context.setText(item.text(0).strip() or "Selected tree row")
        operator_row = item.data(0, self.operator_row_role)
        if isinstance(operator_row, dict):
            if operator_row.get("kind") == "output_policy":
                owner_id = str(operator_row.get("owner_node_id", ""))
                self.inspector_summary.setText(
                    "<b>Output mode</b><br>Click to choose this block's output policy"
                )
                self.open_editor_button.setEnabled(editable_now)
                self.open_editor_button.setText("Edit output mode")
                self.edit_device_button.setEnabled(False)
                self.edit_generator_button.setEnabled(False)
                self.inspector.setPlainText(
                    f"Device node: {owner_id}\n\n"
                    "Choose how output behaves for this recipe block. The selection "
                    "is validated against safety limits again before execution."
                )
                return
            stage_index = operator_row.get("stage_index")
            parameter_id = str(operator_row.get("parameter_id", ""))
            owner_id = str(operator_row.get("owner_node_id", ""))
            stage_label = (
                f"ROI {int(stage_index) + 1}"
                if isinstance(stage_index, int)
                else "Sweep axis"
            )
            self.inspector_summary.setText(
                f"<b>{stage_label}</b><br>{parameter_id} · direct ROI editor"
            )
            self.open_editor_button.setEnabled(editable_now)
            self.open_editor_button.setText("Edit ROI")
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(editable_now)
            self.inspector.setPlainText(
                f"Device node: {owner_id}\n"
                f"Parameter: {parameter_id}\n"
                f"Selected: {stage_label}\n\n"
                "Click this row or use Open parameter editor to edit ROI directly. "
                "The device configuration window will not be opened."
            )
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(node, ThatecDevice):
            self.inspector_summary.setText(
                f"<b>Recorded device configuration</b><br>{node.name}"
            )
            self.open_editor_button.setEnabled(False)
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(False)
            self.inspector.setPlainText(
                "\n".join(f"{key}: {value}" for key, value in node.values)
                or "No public device parameters were saved."
            )
            return
        if isinstance(node, ThatecRow):
            definition = "\n".join(f"{key}: {value}" for key, value in node.definition)
            metadata = "\n".join(f"{key}: {value}" for key, value in node.metadata)
            self.inspector_summary.setText(
                f"<b>Recorded {node.function or 'THATEC'} node</b><br>"
                f"{node.device_name or 'internal'} • {node.control_name or 'internal'}"
            )
            self.open_editor_button.setEnabled(False)
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(False)
            self.inspector.setPlainText(
                f"THATEC row: {node.id}\n"
                f"Recorded shape: {node.shape or 'no measurement data'}\n"
                f"Timestamps: {node.timestamp_count}\n\n"
                f"Definition\n{definition or '—'}\n\n"
                f"Measurement metadata\n{metadata or '—'}"
            )
            return
        if not isinstance(node, RecipeNode):
            self.inspector_summary.setText("Safety cleanup runs after success, operator stop and faults.")
            self.open_editor_button.setEnabled(False)
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(False)
            self.inspector.setPlainText(
                "Finally actions run during normal completion, operator stop and fault cleanup. "
                "They may only ramp Keithley to zero or disable outputs."
            )
            return
        label, detail, _icon = self._tree_presentation(node, 0, False)
        self.inspector_summary.setText(
            f"<b>{label}</b><br>{detail} • {len(node.children)} child node(s)"
        )
        has_device_settings = bool(
            node.data.get("device_module")
            or self._legacy_device_configuration_node(node) is not None
        )
        has_roi = node.type == "sweep" or (
            bool(node.data.get("device_module"))
            and any(
                isinstance(action, dict) and action.get("mode") == "sweep"
                for action in node.data.get("parameter_actions", [])
            )
        )
        is_comment = node.type == "comment"
        is_acquisition = node.type in {
            "acquire_reference",
            "acquire_spectrum",
        }
        self.open_editor_button.setText(
            "Device settings"
            if has_device_settings
            else "Edit ROI"
            if has_roi
            else "Acquisition settings"
            if is_acquisition
            else "Edit comment"
            if is_comment
            else "Action settings"
        )
        self.open_editor_button.setEnabled(editable_now)
        self.edit_device_button.setEnabled(editable_now and has_device_settings)
        self.edit_generator_button.setEnabled(editable_now and has_roi)
        actions = (
            tuple(action for action in self._plan.actions if action.node_id == node.id)
            if self._plan is not None
            else ()
        )
        setpoints: dict[str, tuple[float, float]] = {}
        for action in actions:
            for name, value in action.setpoints_si.items():
                previous = setpoints.get(name, (value, value))
                setpoints[name] = (min(previous[0], value), max(previous[1], value))
        lines = [
            f"ID: {node.id}",
            f"Type: {node.type}",
            f"Children: {len(node.children)}",
            f"Else children: {len(node.else_children)}",
            f"Expanded actions: {len(actions)}",
            "",
            "Fields:",
            json.dumps(node.data, ensure_ascii=False, indent=2, default=str),
        ]
        if setpoints:
            lines.extend(("", "Expanded setpoint ranges:"))
            lines.extend(
                f"  {name}: {minimum:.12g} .. {maximum:.12g} SI"
                for name, (minimum, maximum) in sorted(setpoints.items())
            )
        self.inspector.setPlainText("\n".join(lines))

    def _operator_row_clicked(
        self, item: QTreeWidgetItem, _column: int
    ) -> None:
        if not self._tree_editing_allowed():
            return
        metadata = item.data(0, self.operator_row_role)
        if (
            isinstance(metadata, dict)
            and metadata.get("kind")
            in {"sweep_parameter", "roi_stage", "native_sweep_roi"}
        ):
            if metadata.get("kind") == "native_sweep_roi":
                self._edit_native_sweep_roi_from_tree(dict(metadata))
            else:
                self._edit_device_roi_from_tree(dict(metadata))
        elif isinstance(metadata, dict) and metadata.get("kind") == "output_policy":
            self._edit_output_policy_from_tree(dict(metadata))

    def _move_recipe_node(
        self,
        node_id: str,
        destination_parent_id: str,
        destination_branch: str,
        destination_index: int,
    ) -> bool:
        self._emit_tree_diagnostic(
            "TREE_MOVE_REQUESTED",
            node_id=node_id,
            destination_parent_id=destination_parent_id,
            destination_branch=destination_branch,
            destination_index=destination_index,
        )
        try:
            moved_source = move_recipe_node(
                self._builder_source(),
                node_id=node_id,
                destination_parent_id=destination_parent_id,
                destination_branch=destination_branch,
                destination_index=destination_index,
            )
            parse_recipe_text(moved_source, origin=self.path.text())
            self._apply_builder_source(
                moved_source,
                f"Recipe node {node_id} moved to {destination_parent_id}.{destination_branch}",
                selected_node_id=node_id,
            )
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_MOVE_REJECTED",
                node_id=node_id,
                destination_parent_id=destination_parent_id,
                destination_branch=destination_branch,
                destination_index=destination_index,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            QMessageBox.warning(self, "Recipe move rejected", str(exc))
            return False
        return True

    def _handle_tree_move_request(self, request: RecipeTreeMoveRequest) -> None:
        """Commit a drag only when the source transaction has succeeded."""

        request.accepted = self._move_recipe_node(
            request.node_id,
            request.destination_parent_id,
            request.destination_branch,
            request.destination_index,
        )

    def _find_tree_item(self, node_id: str) -> QTreeWidgetItem | None:
        def visit(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(node, RecipeNode) and node.id == node_id:
                return item
            for child_index in range(item.childCount()):
                found = visit(item.child(child_index))
                if found is not None:
                    return found
            return None

        for top_index in range(self.tree.topLevelItemCount()):
            found = visit(self.tree.topLevelItem(top_index))
            if found is not None:
                return found
        return None

    def _builder_parent(self) -> tuple[str, str]:
        """Return the selected container, or the root as the safe default."""

        self._builder_source()
        current = self.tree.currentItem()
        item = current
        while item is not None:
            structural = RecipeTreeWidget.structural_kind(item)
            if structural == RecipeTreeWidget.finally_container:
                return "__finally__", "children"
            if structural == RecipeTreeWidget.else_container:
                owner_item = item.parent()
                owner = (
                    owner_item.data(0, Qt.ItemDataRole.UserRole)
                    if owner_item is not None
                    else None
                )
                if isinstance(owner, RecipeNode) and owner.type == "if":
                    return owner.id, "else"
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(node, RecipeNode) and node.type in {
                "sequence",
                "sweep",
                "repeat",
                "if",
            }:
                # A selected leaf belongs to the nearest container, whereas a
                # selected container is itself the insertion target.
                return node.id, "children"
            item = item.parent()
        root = self.tree.topLevelItem(0)
        node = root.data(0, Qt.ItemDataRole.UserRole) if root is not None else None
        if isinstance(node, RecipeNode):
            return node.id, "children"
        raise ConfigurationError("Load a valid recipe before editing its tree.")

    def _library_default_destination(self) -> tuple[str, str, int]:
        """Insert after the selection; never silently turn it into a child."""

        self._builder_source()
        current = self.tree.currentItem()
        node = (
            current.data(0, Qt.ItemDataRole.UserRole)
            if current is not None
            else None
        )
        root = self.tree.topLevelItem(0)
        root_node = (
            root.data(0, Qt.ItemDataRole.UserRole) if root is not None else None
        )
        if not isinstance(root_node, RecipeNode):
            raise ConfigurationError("Load a valid recipe before editing its tree.")
        if (
            current is not None
            and RecipeTreeWidget.structural_kind(current)
            == RecipeTreeWidget.finally_container
        ):
            return "__finally__", "children", RecipeTreeWidget._logical_child_count(current)
        if self._library_force_inside:
            if not isinstance(node, RecipeNode) or not RecipeTreeWidget.node_accepts_children(node):
                raise ConfigurationError(
                    "Select Sequence, Repeat, Sweep, If, or an ROI loop before adding inside."
                )
            return node.id, "children", len(node.children)
        if not isinstance(node, RecipeNode) or current is root:
            return root_node.id, "children", RecipeTreeWidget._logical_child_count(root)
        parent = current.parent()
        destination = self._tree_parent_destination(parent)
        if destination is None:
            return root_node.id, "children", RecipeTreeWidget._logical_child_count(root)
        parent_id, branch = destination
        return (
            parent_id,
            branch,
            RecipeTreeWidget._logical_index(parent, current, below=True),
        )

    @staticmethod
    def _tree_parent_destination(
        parent: QTreeWidgetItem | None,
    ) -> tuple[str, str] | None:
        if parent is None:
            return None
        structural = RecipeTreeWidget.structural_kind(parent)
        if structural == RecipeTreeWidget.finally_container:
            return "__finally__", "children"
        if structural == RecipeTreeWidget.else_container:
            owner_item = parent.parent()
            owner = (
                owner_item.data(0, Qt.ItemDataRole.UserRole)
                if owner_item is not None
                else None
            )
            if isinstance(owner, RecipeNode) and owner.type == "if":
                return owner.id, "else"
            return None
        if (
            structural == RecipeTreeWidget.execution_container
            and parent.parent() is not None
        ):
            owner = parent.parent().data(0, Qt.ItemDataRole.UserRole)
            if isinstance(owner, RecipeNode):
                return owner.id, "children"
            return None
        owner = parent.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(owner, RecipeNode):
            return owner.id, "children"
        return None

    @staticmethod
    def _new_node_id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:8]}"

    def _update_tree_history_controls(self) -> None:
        editable = self._tree_editing_allowed()
        can_undo = editable and bool(self._tree_undo)
        can_redo = editable and bool(self._tree_redo)
        self.undo_tree_action.setEnabled(can_undo)
        self.redo_tree_action.setEnabled(can_redo)
        self.undo_tree_action.setToolTip(
            "Undo the last recipe edit"
            if can_undo
            else "Apply the YAML draft first"
            if self._yaml_draft_pending()
            else "No recipe edit to undo"
        )
        self.redo_tree_action.setToolTip(
            "Redo the last recipe edit"
            if can_redo
            else "Apply the YAML draft first"
            if self._yaml_draft_pending()
            else "No recipe edit to redo"
        )

    def _restore_tree_history_source(self, source: str, status: str) -> None:
        recipe = parse_recipe_text(source, origin="tree-builder-history")
        self._loading_source = True
        self.editor.setPlainText(source)
        self._loading_source = False
        self._close_discard_confirmed = False
        self._tree_source = source
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self._populate_recipe_tree(recipe.root, recipe.finally_nodes, None)
        self.summary.setText("Recipe tree changed; compile it again before running.")
        self._autosave_timer.start()
        self.status.emit(status)
        self._refresh_document_state()

    def undo_tree_edit(self) -> None:
        if not self._tree_undo:
            return
        current = self._tree_source
        source = self._tree_undo.pop()
        self._tree_redo.append(current)
        self._restore_tree_history_source(source, "Tree Builder change undone")
        self._update_tree_history_controls()

    def redo_tree_edit(self) -> None:
        if not self._tree_redo:
            return
        current = self._tree_source
        source = self._tree_redo.pop()
        self._tree_undo.append(current)
        self._restore_tree_history_source(source, "Tree Builder change redone")
        self._update_tree_history_controls()

    def _apply_builder_source(
        self,
        source: str,
        status: str,
        *,
        selected_node_id: str | None = None,
    ) -> None:
        self._leave_historical_sweep_mode()
        previous = self._tree_source
        previous_plan = self._plan
        try:
            recipe = parse_recipe_text(source, origin="tree-builder")
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_SOURCE_REJECTED",
                operation=status,
                source_length=len(source),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        self._plan = None
        try:
            self._populate_recipe_tree(
                recipe.root,
                recipe.finally_nodes,
                None,
                selected_node_id,
            )
        except Exception as exc:
            self._plan = previous_plan
            self._emit_tree_diagnostic(
                "TREE_RENDER_REJECTED",
                operation=status,
                root_id=recipe.root.id,
                source_length=len(source),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        if source != previous and previous:
            try:
                parse_recipe_text(previous, origin="recipe-edit-history")
            except Exception:
                # Historical/result projections and a failed initial load are
                # not executable recipe snapshots and must never enter Undo.
                pass
            else:
                self._tree_undo.append(previous)
            self._tree_redo.clear()
        self._loading_source = True
        self.editor.setPlainText(source)
        self._loading_source = False
        self._close_discard_confirmed = False
        self._tree_source = source
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self.summary.setText("Recipe tree changed; compile it again before running.")
        self._autosave_timer.start()
        self.status.emit(status)
        self._refresh_document_state()
        self._update_tree_history_controls()

    def _emit_tree_diagnostic(self, event: str, **context: object) -> None:
        """Send one searchable, structured Tree Builder record to Event log."""

        payload = json.dumps(
            context, ensure_ascii=False, sort_keys=True, default=str
        )
        self.status.emit(f"{event} | {payload}")

    def _add_basic_node(
        self,
        kind: str,
        *,
        parent_id: str | None = None,
        branch: str | None = None,
        insert_index: int | None = None,
    ) -> None:
        if kind == "repeat":
            raise ConfigurationError(
                "An empty Repeat is not a valid recipe block. Select an existing "
                "block or the recipe root and use Wrap in Repeat."
            )
        if parent_id is None or branch is None:
            parent_id, branch = self._builder_parent()
        node_id = self._new_node_id(kind)
        defaults: dict[str, dict[str, object]] = {
            "sequence": {
                "id": node_id,
                "type": "sequence",
                "children": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Add actions to this sequence",
                    }
                ],
            },
            "if": {
                "id": node_id,
                "type": "if",
                "condition": True,
                "children": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Then branch",
                    }
                ],
                "else": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Else branch",
                    }
                ],
            },
            "wait": {"id": node_id, "type": "wait", "duration": "100 ms"},
            "measure_keithley": {"id": node_id, "type": "measure_keithley", "channel": "B"},
            "measure_moke_hall": {"id": node_id, "type": "measure_moke_hall"},
            "measure_lakeshore_field": {
                "id": node_id,
                "type": "measure_lakeshore_field",
                "checkpoint": True,
            },
            "acquire_reference": {
                "id": node_id,
                "type": "acquire_reference",
                "trace": "TRAC1",
                "average_count": 1,
            },
            "acquire_spectrum": {
                "id": node_id,
                "type": "acquire_spectrum",
                "trace": "TRAC1",
                "average_count": 1,
            },
            "comment": {"id": node_id, "type": "comment", "text": "Describe this step"},
            "ramp_keithley_to_zero": {
                "id": node_id,
                "type": "ramp_keithley_to_zero",
                "channel": "B",
                "deadline": "10 s",
            },
            "set_keithley_output": {
                "id": node_id,
                "type": "set_keithley_output",
                "channel": "B",
                "enabled": False,
            },
            "set_rigol_output": {
                "id": node_id,
                "type": "set_rigol_output",
                "channel": 1,
                "enabled": False,
            },
            "set_anritsu_sg_output": {
                "id": node_id,
                "type": "set_anritsu_sg_output",
                "enabled": False,
            },
            "set_anritsu_sg_output_on": {
                "id": node_id,
                "type": "set_anritsu_sg_output",
                "enabled": True,
            },
        }
        try:
            if kind not in defaults:
                raise ConfigurationError(f"Unknown recipe action {kind!r}.")
            if (
                parent_id == "__finally__"
                and kind not in self._FINALLY_ACTION_TYPES
            ):
                raise ConfigurationError(
                    "Finally accepts only ramp-to-zero and OUTPUT OFF safety actions."
                )
            source = add_recipe_node(
                self._builder_source(),
                parent_id=parent_id,
                branch=branch,
                index=insert_index,
                node=defaults[kind],
            )
            self._apply_builder_source(
                source,
                f"Added {kind} node",
                selected_node_id=node_id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Add recipe node", str(exc))

    def _wrap_selected_in_repeat(self) -> None:
        """Wrap one subtree, or every root child, without an empty draft node."""

        try:
            self._builder_source()
            item = self.tree.currentItem()
            if item is None:
                raise ConfigurationError(
                    "Select a block, or select the recipe root to repeat all root steps."
                )
            if self._tree_item_is_in_finally(item):
                raise ConfigurationError(
                    "Finally safety actions cannot be wrapped in Repeat."
                )
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(node, RecipeNode):
                if item.parent() is None:
                    node_ids = tuple(child.id for child in node.children)
                    selection_label = f"All {len(node_ids)} root step(s)"
                else:
                    node_ids = (node.id,)
                    selection_label = f'The selected block "{item.text(0)}"'
            elif RecipeTreeWidget.structural_kind(item) == RecipeTreeWidget.else_container:
                owner_item = item.parent()
                owner = (
                    owner_item.data(0, Qt.ItemDataRole.UserRole)
                    if owner_item is not None
                    else None
                )
                if not isinstance(owner, RecipeNode):
                    raise ConfigurationError("The selected Else branch is not editable.")
                node_ids = tuple(child.id for child in owner.else_children)
                selection_label = f"All {len(node_ids)} Else step(s)"
            else:
                raise ConfigurationError(
                    "Select a recipe block or the recipe root before wrapping."
                )
            if not node_ids:
                raise ConfigurationError(
                    "The selected container is empty. Add a real action before creating Repeat."
                )
            dialog = RepeatCountDialog(
                self,
                selection_label=selection_label,
                initial_count=4,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            repeat_id = self._new_node_id("repeat")
            source = wrap_recipe_nodes_in_repeat(
                self._builder_source(),
                node_ids=node_ids,
                repeat_id=repeat_id,
                count=dialog.count.value(),
            )
            self._apply_builder_source(
                source,
                f"Wrapped {len(node_ids)} recipe node(s) in Repeat",
                selected_node_id=repeat_id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Wrap in Repeat", str(exc))

    def _add_device_controls(self, device: str | None = None) -> None:
        try:
            parent_id, branch = self._builder_parent()
        except Exception as exc:
            QMessageBox.warning(self, "Add device control", str(exc))
            return
        if parent_id == "__finally__":
            QMessageBox.warning(
                self,
                "Cannot add device control",
                "Finally accepts only ramp-to-zero and OUTPUT OFF safety actions.",
            )
            return
        if device == "Keithley":
            dialog = KeithleySweepBuilderDialog(self._settings, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                node = self._sweep_node_from_generator(
                    dialog.definition,
                    dialog.segment_data(),
                    keithley_options=dialog.keithley_options(),
                )
                source = add_recipe_node(
                    self._builder_source(),
                    parent_id=parent_id,
                    branch=branch,
                    node=node,
                )
                self._apply_builder_source(source, "Added Keithley sweep node")
            except Exception as exc:
                QMessageBox.warning(self, "Add Keithley sweep", str(exc))
            return
        picker = DeviceParameterDialog(
            self,
            initial_device=device,
            definitions=self._recipe_parameter_definitions,
        )
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        definitions = picker.selected()
        operation = picker.operation_kind()
        source = self._builder_source()
        added = 0
        for definition in definitions:
            dialog: QDialog
            dialog = SweepGeneratorDialog(definition, self) if operation == "sweep" else FixedValueDialog(definition, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                continue
            try:
                node = (
                    self._sweep_node_from_generator(definition, dialog.segment_data())
                    if isinstance(dialog, SweepGeneratorDialog)
                    else self._fixed_node_from_dialog(definition, dialog)
                )
                source = add_recipe_node(
                    source,
                    parent_id=parent_id,
                    branch=branch,
                    node=node,
                )
                added += 1
            except Exception as exc:
                QMessageBox.warning(self, "Add sweep", str(exc))
                return
        if added:
            noun = "device control node(s)" if operation == "fixed" else "dynamic sweep node(s)"
            self._apply_builder_source(source, f"Added {added} {noun}")

    def _edit_selected_node(self) -> None:
        """Open the most specific editor while keeping device and ROI tasks separate."""

        if not self._tree_editing_allowed():
            return
        item = self.tree.currentItem()
        operator_row = (
            item.data(0, self.operator_row_role) if item is not None else None
        )
        if isinstance(operator_row, dict):
            if operator_row.get("kind") == "output_policy":
                self._edit_output_policy_from_tree(dict(operator_row))
            else:
                self._edit_selected_roi()
            return
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        anritsu_role = (
            self._anritsu_node_role(node) if isinstance(node, RecipeNode) else None
        )
        if isinstance(node, RecipeNode) and node.type == "comment":
            self._edit_comment_node(node)
            return
        if anritsu_role in {"spectrum_acquisition", "reference_acquisition"}:
            self._edit_anritsu_acquisition_node(node)
            return
        if isinstance(node, RecipeNode) and node.type in {
            "configure_anritsu_advanced",
            "configure_anritsu_sg",
        }:
            self._edit_action_node(node)
            return
        if isinstance(node, RecipeNode) and (
            node.data.get("device_module")
            or self._legacy_device_configuration_node(node) is not None
        ):
            self._edit_selected_device_settings()
            return
        if isinstance(node, RecipeNode) and node.type == "sweep":
            self._edit_selected_roi()
            return
        if isinstance(node, RecipeNode):
            self._edit_action_node(node)

    @staticmethod
    def _anritsu_node_role(node: RecipeNode) -> str | None:
        """Return the one editor role represented by an Anritsu tree node."""

        module = node.data.get("device_module")
        if module == "anritsu":
            return "spectrum_configuration"
        if module == "anritsu_sg":
            return "signal_generator_configuration"
        if node.type == "acquire_spectrum":
            return "spectrum_acquisition"
        if node.type == "acquire_reference":
            return "reference_acquisition"
        if node.type in {"set_anritsu_sg_output", "enable_anritsu_sg_output"}:
            return "signal_generator_output"
        if node.type == "configure_anritsu":
            return "legacy_spectrum_configuration"
        if node.type == "configure_anritsu_advanced":
            return "legacy_advanced_configuration"
        if node.type == "configure_anritsu_sg":
            return "legacy_signal_generator_configuration"
        return None

    def _edit_action_node(self, node: RecipeNode) -> None:
        item = self.tree.currentItem()
        dialog = ActionNodeEditorDialog(
            node,
            self,
            in_finally=self._tree_item_is_in_finally(item),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        for key in tuple(node.data):
            replacement.pop(key, None)
        replacement.update(dialog.node_fields())
        try:
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source,
                f"Updated action settings for {node.id}",
                selected_node_id=node.id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Action settings", str(exc))

    def _edit_output_policy_from_tree(self, metadata: dict[str, object]) -> None:
        """Edit only the output contract represented by the compact child row."""

        owner_id = str(metadata.get("owner_node_id", ""))
        owner = self._find_tree_item(owner_id)
        node = owner.data(0, Qt.ItemDataRole.UserRole) if owner is not None else None
        if not isinstance(node, RecipeNode):
            return
        dialog = OutputPolicyDialog(str(node.data.get("output_policy", "unchanged")), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        replacement["output_policy"] = dialog.selected_policy()
        try:
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source,
                f"Updated output mode for {node.id}",
                selected_node_id=node.id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Output mode", str(exc))

    def _edit_selected_roi(self) -> None:
        item = self.tree.currentItem()
        operator_row = (
            item.data(0, self.operator_row_role) if item is not None else None
        )
        if isinstance(operator_row, dict):
            if operator_row.get("kind") == "output_policy":
                self._edit_output_policy_from_tree(dict(operator_row))
            elif operator_row.get("kind") == "native_sweep_roi":
                self._edit_native_sweep_roi_from_tree(dict(operator_row))
            else:
                self._edit_device_roi_from_tree(dict(operator_row))
            return
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(node, RecipeNode) and node.type == "sweep":
            self._edit_selected_generator(node=node)
            return
        if isinstance(node, RecipeNode) and node.data.get("device_module"):
            sweep = next(
                (
                    action
                    for action in node.data.get("parameter_actions", [])
                    if isinstance(action, dict) and action.get("mode") == "sweep"
                ),
                None,
            )
            if sweep is not None:
                self._edit_device_roi_from_tree(
                    {
                        "device_module": node.data.get("device_module"),
                        "owner_node_id": node.id,
                        "parameter_id": sweep.get("parameter_id"),
                        "stage_index": None,
                    }
                )
                return
        QMessageBox.information(
            self,
            "Edit ROI",
            "Select an ROI row, a sweep axis, or a device module containing a sweep.",
        )

    @staticmethod
    def _legacy_device_configuration_node(
        node: RecipeNode,
    ) -> RecipeNode | None:
        if node.type in {
            "configure_keithley",
            "configure_rigol",
            "configure_anritsu",
            "configure_anritsu_advanced",
            "configure_anritsu_sg",
        }:
            return node
        if node.type == "sweep":
            return next(
                (
                    child
                    for child in node.children
                    if child.type
                    in {
                        "configure_keithley",
                        "configure_rigol",
                        "configure_anritsu",
                        "configure_anritsu_sg",
                    }
                ),
                None,
            )
        return None

    def _edit_selected_device_settings(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(node, RecipeNode):
            QMessageBox.information(
                self, "Device settings", "Select a device or its sweep axis first."
            )
            return
        if node.data.get("device_module") == "keithley":
            self._edit_keithley_module_node(node)
            return
        if node.data.get("device_module") == "rigol":
            self._edit_rigol_module_node(node)
            return
        if node.data.get("device_module") == "anritsu":
            self._edit_anritsu_module_node(node)
            return
        if node.data.get("device_module") == "anritsu_sg":
            self._edit_anritsu_sg_module_node(node)
            return
        configuration = self._legacy_device_configuration_node(node)
        if configuration is None:
            QMessageBox.information(
                self,
                "Device settings",
                "The selected object has no device configuration window.",
            )
            return
        if configuration.type == "configure_keithley":
            self._edit_legacy_keithley_configuration(configuration)
        elif configuration.type == "configure_rigol":
            self._edit_legacy_rigol_configuration(configuration)
        elif configuration.type == "configure_anritsu":
            self._edit_legacy_anritsu_configuration(configuration)
        elif configuration.type in {
            "configure_anritsu_advanced",
            "configure_anritsu_sg",
        }:
            self._edit_action_node(configuration)
        else:
            QMessageBox.information(
                self,
                "Device settings",
                "Select the parent Anritsu configuration node.",
            )

    def _edit_device_roi_from_tree(self, metadata: dict[str, object]) -> None:
        if metadata.get("device_module") == "anritsu_sg":
            owner = self._find_tree_item(str(metadata.get("owner_node_id", "")))
            node = (
                owner.data(0, Qt.ItemDataRole.UserRole)
                if owner is not None
                else None
            )
            if isinstance(node, RecipeNode):
                self._edit_anritsu_sg_module_node(node)
        elif metadata.get("device_module") == "anritsu":
            self._edit_anritsu_roi_from_tree(metadata)
        elif metadata.get("device_module") == "rigol":
            self._edit_rigol_roi_from_tree(metadata)
        else:
            self._edit_keithley_roi_from_tree(metadata)

    def _edit_native_sweep_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = (
            owner_item.data(0, Qt.ItemDataRole.UserRole)
            if owner_item is not None
            else None
        )
        if not isinstance(node, RecipeNode) or node.type != "sweep":
            QMessageBox.warning(
                self, "ROI editor", "The owning sweep no longer exists."
            )
            return
        stage_index = metadata.get("stage_index")
        self._edit_selected_generator(
            node=node,
            stage_index=stage_index if isinstance(stage_index, int) else None,
        )

    def _edit_keithley_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        parameter_id = str(metadata.get("parameter_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = (
            owner_item.data(0, Qt.ItemDataRole.UserRole)
            if owner_item is not None
            else None
        )
        if not isinstance(node, RecipeNode):
            QMessageBox.warning(
                self, "ROI editor", "The owning device node no longer exists."
            )
            return
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        sweep_action = next(
            (
                action
                for action in actions
                if action.get("parameter_id") == parameter_id
                and action.get("mode") == "sweep"
            ),
            None,
        )
        if sweep_action is None:
            QMessageBox.warning(
                self,
                "ROI editor",
                "The selected parameter is no longer configured as a sweep axis.",
            )
            return
        snapshot = replace(
            self._current_keithley_snapshot(),
            channel=str(node.data.get("channel", "B")),
            source_mode=str(node.data.get("source_mode", "current")),
        )
        for action in actions:
            snapshot = self._snapshot_with_override(snapshot, action)
        definition = self._keithley_roi_definition(snapshot, parameter_id)
        initial = sweep_action.get("segments")
        dialog = SweepGeneratorDialog(
            definition,
            self,
            initial_segments=(
                [dict(segment) for segment in initial if isinstance(segment, dict)]
                if isinstance(initial, list)
                else None
            ),
        )
        dialog.setWindowTitle(
            f"Keithley {snapshot.channel} — ROI · {definition['label']}"
        )
        stage_index = metadata.get("stage_index")
        dialog.select_interval(stage_index if isinstance(stage_index, int) else None)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._apply_keithley_roi_segments(
                node, parameter_id, dialog.segment_data()
            )
        except Exception as exc:
            QMessageBox.warning(self, "ROI editor", str(exc))

    def _apply_keithley_roi_segments(
        self,
        node: RecipeNode,
        parameter_id: str,
        segments: list[dict[str, object]],
    ) -> None:
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        target = next(
            (
                action
                for action in actions
                if action.get("parameter_id") == parameter_id
                and action.get("mode") == "sweep"
            ),
            None,
        )
        if target is None:
            raise ConfigurationError(
                "The selected Keithley parameter is not a sweep axis."
            )
        snapshot = replace(
            self._current_keithley_snapshot(),
            channel=str(node.data.get("channel", "B")),
            source_mode=str(node.data.get("source_mode", "current")),
        )
        for action in actions:
            snapshot = self._snapshot_with_override(snapshot, action)
        definition = self._keithley_roi_definition(snapshot, parameter_id)
        generate_sweep_points(segments, definition["dimension"])
        target["segments"] = [dict(segment) for segment in segments]
        replacement = self._configured_keithley_node(
            node,
            snapshot,
            parameter_actions=actions,
            output_policy=str(node.data.get("output_policy", "unchanged")),
        )
        source = replace_recipe_node(
            self._builder_source(), node_id=node.id, node=replacement
        )
        self._apply_builder_source(
            source, f"Updated ROI for {parameter_id} in {node.id}"
        )

    def _edit_keithley_module_node(self, node: RecipeNode) -> None:
        snapshot = self._current_keithley_snapshot()
        configuration = node.data.get("configuration")
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        legacy_override = node.data.get("parameter_override")
        if not actions and isinstance(legacy_override, dict):
            actions = [{"mode": "set", **legacy_override}]
        if isinstance(configuration, dict):
            try:
                snapshot = KeithleyConfigurationSnapshot(**configuration)
            except (TypeError, ValueError) as exc:
                QMessageBox.warning(self, "Keithley node", f"Invalid stored snapshot: {exc}")
                return
        for action in actions:
            snapshot = self._snapshot_with_override(snapshot, action)
        dialog = KeithleyNodeEditorDialog(
            self._settings,
            self,
            snapshot=snapshot,
            snapshot_resolver=self._keithley_snapshot_for,
        )
        dialog.load_plan_actions(
            actions, str(node.data.get("output_policy", "unchanged"))
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            snapshot = dialog.configuration_snapshot()
            parameter_actions = self._edit_keithley_rois(
                snapshot, dialog.planned_parameter_actions()
            )
            if parameter_actions is None:
                return
            replacement = self._configured_keithley_node(
                node,
                snapshot,
                parameter_actions=parameter_actions,
                output_policy=dialog.selected_output_policy(),
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Configured {replacement['label']}")
        except Exception as exc:
            QMessageBox.warning(self, "Keithley node", str(exc))

    def _edit_anritsu_module_node(
        self, node: RecipeNode, *, highlight_required: bool = False
    ) -> None:
        snapshot = self._current_anritsu_snapshot()
        configuration = node.data.get("configuration")
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        try:
            if isinstance(configuration, dict):
                snapshot = self._anritsu_snapshot_from_mapping(
                    configuration, fallback=snapshot
                )
            for action in actions:
                snapshot = self._anritsu_snapshot_with_override(snapshot, action)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Anritsu node",
                "The stored Anritsu snapshot is invalid and cannot be opened "
                f"safely. Correct the YAML or remove the node.\n\n{exc}",
            )
            return
        dialog = AnritsuNodeEditorDialog(
            self._settings, self, snapshot=snapshot
        )
        dialog.load_plan_actions(
            actions,
            acquire_single=bool(node.data.get("acquire_single", False)),
            trace=str(node.data.get("trace", "TRAC1")),
        )
        stored_role = str(node.data.get("post_configuration_operation", ""))
        managed_id = str(node.data.get("managed_acquisition_id", ""))
        managed_acquisition = next(
            (child for child in node.children if child.id == managed_id),
            None,
        )
        if managed_acquisition is not None and managed_acquisition.type in {
            "acquire_spectrum",
            "acquire_reference",
        }:
            stored_role = managed_acquisition.type
        if stored_role in {"configure", "acquire_spectrum", "acquire_reference"}:
            dialog.node_role.setCurrentIndex(dialog.node_role.findData(stored_role))
        acquisition_data = (
            managed_acquisition.data if managed_acquisition is not None else node.data
        )
        trace_index = dialog.trace.findText(str(acquisition_data.get("trace", "TRAC1")))
        if trace_index >= 0:
            dialog.trace.setCurrentIndex(trace_index)
        dialog.average_count.setValue(
            int(
                acquisition_data.get(
                    "average_count",
                    node.data.get("acquisition_average_count", 1),
                )
            )
        )
        reference_operation = str(
            acquisition_data.get(
                "reference_operation",
                node.data.get("acquisition_reference_operation", "none"),
            )
        )
        reference_index = dialog.reference_operation.findData(reference_operation)
        if reference_index >= 0:
            dialog.reference_operation.setCurrentIndex(reference_index)
        if highlight_required:
            dialog.highlight_required_configuration()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            snapshot = dialog.configuration_panel.configuration_snapshot()
            replacement = self._configured_anritsu_node(
                node,
                snapshot=snapshot,
                parameter_actions=dialog.planned_parameter_actions(),
                acquire_single=False,
                trace=dialog.trace.currentText(),
                post_configuration_operation=dialog.selected_node_role(),
                acquisition_average_count=dialog.average_count.value(),
                acquisition_reference_operation=str(
                    dialog.reference_operation.currentData() or "none"
                ),
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, "Configured Anritsu spectrum module")
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu node", str(exc))

    def _edit_anritsu_sg_module_node(self, node: RecipeNode) -> None:
        configuration = node.data.get("configuration")
        snapshot = self._current_anritsu_sg_snapshot()
        frequency = format_quantity_auto(
            snapshot.frequency_hz, DIMENSION_FREQUENCY
        )
        power = f"{snapshot.power_dbm:.9g} dBm"
        if isinstance(configuration, dict):
            frequency = str(configuration.get("frequency", frequency))
            power = str(configuration.get("power", power))
        actions = [
            dict(action)
            for action in node.data.get("parameter_actions", [])
            if isinstance(action, dict)
        ]
        dialog = AnritsuSignalGeneratorNodeEditorDialog(
            self,
            frequency=frequency,
            power=power,
            parameter_actions=actions,
            output_policy=str(node.data.get("output_policy", "unchanged")),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            replacement = self._configured_anritsu_sg_node(
                node,
                frequency=dialog.frequency.text().strip(),
                power=dialog.power.text().strip(),
                parameter_actions=dialog.planned_parameter_actions(),
                output_policy=dialog.selected_output_policy(),
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, "Configured Anritsu signal-generator module · RF OFF"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu SG node", str(exc))

    def _edit_rigol_module_node(self, node: RecipeNode) -> None:
        snapshot = self._current_rigol_snapshot()
        configuration = node.data.get("configuration")
        if isinstance(configuration, dict):
            try:
                snapshot = self._rigol_snapshot_from_mapping(
                    configuration, fallback=snapshot
                )
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Rigol node",
                    "The stored Rigol snapshot is invalid and cannot be opened "
                    f"safely. Correct the YAML or remove the node.\n\n{exc}",
                )
                return
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        dialog = RigolNodeEditorDialog(
            self,
            settings=self._settings,
            snapshot=snapshot,
            snapshot_resolver=self._rigol_snapshot_for,
            parameter_actions=actions,
            output_policy=str(node.data.get("output_policy", "unchanged")),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            replacement = self._configured_rigol_node(
                node,
                snapshot=dialog.configuration_snapshot(),
                parameter_actions=dialog.planned_parameter_actions(),
                output_policy=dialog.selected_output_policy(),
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source,
                f"Configured Rigol CH{replacement['channel']} OUTPUT "
                f"{str(replacement['output_policy']).upper()}",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Rigol node", str(exc))

    def _edit_rigol_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        parameter_id = str(metadata.get("parameter_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = (
            owner_item.data(0, Qt.ItemDataRole.UserRole)
            if owner_item is not None
            else None
        )
        if not isinstance(node, RecipeNode):
            QMessageBox.warning(
                self, "ROI editor", "The owning Rigol node no longer exists."
            )
            return
        actions = [
            dict(action)
            for action in node.data.get("parameter_actions", [])
            if isinstance(action, dict)
        ]
        action = next(
            (
                candidate
                for candidate in actions
                if candidate.get("parameter_id") == parameter_id
                and candidate.get("mode") == "sweep"
            ),
            None,
        )
        if action is None:
            return
        suffix = {
            "carrier.frequency": "frequency",
            "carrier.high_level": "high_level",
            "carrier.low_level": "low_level",
            "carrier.amplitude": "amplitude",
            "carrier.offset": "offset",
        }.get(parameter_id)
        if suffix is None:
            return
        dimension = (
            DIMENSION_FREQUENCY
            if parameter_id == "carrier.frequency"
            else DIMENSION_VOLTAGE
        )
        initial = action.get("segments")
        dialog = SweepGeneratorDialog(
            {
                "device": "Rigol",
                "label": f"Rigol CH{node.data.get('channel')} · {suffix}",
                "target": f"rigol.{node.data.get('channel')}.{suffix}",
                "dimension": dimension,
            },
            self,
            initial_segments=(
                [
                    dict(segment)
                    for segment in initial
                    if isinstance(segment, dict)
                ]
                if isinstance(initial, list)
                else None
            ),
        )
        stage_index = metadata.get("stage_index")
        dialog.select_interval(
            stage_index if isinstance(stage_index, int) else None
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            action["segments"] = dialog.segment_data()
            configuration = node.data.get("configuration")
            snapshot = self._current_rigol_snapshot()
            if isinstance(configuration, dict):
                snapshot = self._rigol_snapshot_from_mapping(
                    configuration, fallback=snapshot
                )
            replacement = self._configured_rigol_node(
                node,
                snapshot=snapshot,
                parameter_actions=actions,
                output_policy=str(node.data.get("output_policy", "unchanged")),
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, f"Updated Rigol ROI for {parameter_id}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Rigol ROI editor", str(exc))

    def _edit_legacy_keithley_configuration(self, node: RecipeNode) -> None:
        snapshot = replace(
            self._current_keithley_snapshot(),
            channel=str(node.data.get("channel", "B")),
            source_mode=str(node.data.get("mode", "current")),
            source_level=str(node.data.get("level", "1 mA")),
            compliance=str(node.data.get("compliance", "67 mV")),
            nplc=str(node.data.get("nplc", "1")),
            settling_time=str(node.data.get("settle_time", "100 ms")),
            sense_mode=str(node.data.get("sense_mode", "2wire")),
            source_autorange=bool(node.data.get("source_autorange", True)),
            source_range=str(node.data.get("source_range", "AUTO")),
            measure_voltage_autorange=bool(
                node.data.get("measure_voltage_autorange", True)
            ),
            measure_voltage_range=str(
                node.data.get("measure_voltage_range", "AUTO")
            ),
            measure_current_autorange=bool(
                node.data.get("measure_current_autorange", True)
            ),
            measure_current_range=str(
                node.data.get("measure_current_range", "AUTO")
            ),
        )
        dialog = KeithleyNodeEditorDialog(
            self._settings, self, snapshot=snapshot
        )
        for selector in dialog.parameter_selectors.values():
            index = selector.findData("set")
            if index >= 0:
                selector.setCurrentIndex(index)
        dialog.output_policy.setEnabled(False)
        dialog.output_policy.setToolTip(
            "Legacy configuration nodes do not change OUTPUT."
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            updated = dialog.configuration_snapshot()
            replacement = self._node_to_mapping(node)
            replacement.update(
                {
                    "channel": updated.channel,
                    "mode": updated.source_mode,
                    "level": updated.source_level,
                    "compliance": updated.compliance,
                    "nplc": float(updated.nplc.replace(",", ".")),
                    "settle_time": updated.settling_time,
                    "sense_mode": updated.sense_mode,
                    "source_autorange": updated.source_autorange,
                    "source_range": updated.source_range,
                    "measure_voltage_autorange": updated.measure_voltage_autorange,
                    "measure_voltage_range": updated.measure_voltage_range,
                    "measure_current_autorange": updated.measure_current_autorange,
                    "measure_current_range": updated.measure_current_range,
                }
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, f"Updated Keithley settings {node.id}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Keithley settings", str(exc))

    def _edit_legacy_rigol_configuration(self, node: RecipeNode) -> None:
        snapshot = RigolConfigurationSnapshot(
            channel=int(node.data.get("channel", 1)),
            waveform=str(node.data.get("waveform", "SIN")),
            frequency=str(node.data.get("frequency", "1 kHz")),
            high_level=str(node.data.get("high_level", "1 mV")),
            low_level=str(node.data.get("low_level", "-1 mV")),
            output_load=str(node.data.get("output_load", "HIGHZ")),
            phase_deg=str(node.data.get("phase_deg", "0")),
            square_duty_percent=str(
                node.data.get("square_duty_percent", "50")
            ),
            ramp_symmetry_percent=str(
                node.data.get("ramp_symmetry_percent", "50")
            ),
            pulse_width=str(node.data.get("pulse_width", "100 us")),
            pulse_leading=str(node.data.get("pulse_leading", "10 ns")),
            pulse_trailing=str(node.data.get("pulse_trailing", "10 ns")),
        )
        dialog = RigolNodeEditorDialog(
            self,
            settings=self._settings,
            snapshot=snapshot,
            output_policy="unchanged",
        )
        dialog.output_policy.setEnabled(False)
        dialog.output_policy.setToolTip(
            "Legacy configuration nodes do not change OUTPUT."
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            updated = dialog.configuration_snapshot()
            replacement = self._node_to_mapping(node)
            replacement.update(
                {
                    "channel": updated.channel,
                    "waveform": updated.waveform,
                    "frequency": updated.frequency,
                    "high_level": updated.high_level,
                    "low_level": updated.low_level,
                    "output_load": updated.output_load,
                    "phase_deg": float(updated.phase_deg.replace(",", ".")),
                }
            )
            for key, value, waveform in (
                ("square_duty_percent", updated.square_duty_percent, "SQU"),
                ("ramp_symmetry_percent", updated.ramp_symmetry_percent, "RAMP"),
                ("pulse_width", updated.pulse_width, "PULS"),
                ("pulse_leading", updated.pulse_leading, "PULS"),
                ("pulse_trailing", updated.pulse_trailing, "PULS"),
            ):
                if updated.waveform == waveform:
                    replacement[key] = value
                else:
                    replacement.pop(key, None)
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, f"Updated Rigol settings {node.id}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Rigol settings", str(exc))

    def _edit_legacy_anritsu_configuration(self, node: RecipeNode) -> None:
        current = self._current_anritsu_snapshot()
        try:
            snapshot = replace(
                current,
                start_hz=parse_quantity(
                    node.data.get("start_frequency"), DIMENSION_FREQUENCY
                ).si_value,
                stop_hz=parse_quantity(
                    node.data.get("stop_frequency"), DIMENSION_FREQUENCY
                ).si_value,
                reference_level_dbm=parse_quantity(
                    node.data.get("reference_level"), DIMENSION_DBM
                ).si_value,
                points=int(node.data.get("points", 1001)),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu settings", str(exc))
            return
        actions = [
            {
                "parameter_id": "spectrum.start_frequency",
                "mode": "set",
                "value": str(node.data.get("start_frequency")),
            },
            {
                "parameter_id": "spectrum.stop_frequency",
                "mode": "set",
                "value": str(node.data.get("stop_frequency")),
            },
            {
                "parameter_id": "spectrum.reference_level",
                "mode": "set",
                "value": str(node.data.get("reference_level")),
            },
            {
                "parameter_id": "spectrum.points",
                "mode": "set",
                "value": str(node.data.get("points", 1001)),
            },
        ]
        dialog = AnritsuNodeEditorDialog(
            self._settings, self, snapshot=snapshot
        )
        dialog.load_plan_actions(
            actions, acquire_single=False, trace=str(node.data.get("trace", "TRAC1"))
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            updated = dialog.configuration_panel.configuration_snapshot()
            replacement = self._node_to_mapping(node)
            replacement.update(
                {
                    "start_frequency": f"{updated.start_hz:.12g} Hz",
                    "stop_frequency": f"{updated.stop_hz:.12g} Hz",
                    "reference_level": f"{updated.reference_level_dbm:.12g} dBm",
                    "points": updated.points,
                }
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, f"Updated Anritsu settings {node.id}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu settings", str(exc))

    @staticmethod
    def _configured_rigol_node(
        node: RecipeNode,
        *,
        channel: int | None = None,
        snapshot: RigolConfigurationSnapshot | None = None,
        parameter_actions: list[dict[str, object]] | None = None,
        output_policy: str,
    ) -> dict[str, object]:
        snapshot = snapshot or RigolConfigurationSnapshot(channel=channel or 1)
        channel = snapshot.channel
        if channel not in {1, 2}:
            raise ConfigurationError("Rigol output channel must be 1 or 2.")
        if output_policy not in {
            "unchanged",
            "on",
            "off",
            "on_keep",
            "continue",
        }:
            raise ConfigurationError(
                "Rigol output policy must be off, on for the block, kept on, "
                "or continuous."
            )
        actions = parameter_actions or [
            {
                "parameter_id": "carrier.frequency",
                "mode": "set",
                "value": snapshot.frequency,
            },
            {
                "parameter_id": "carrier.high_level",
                "mode": "set",
                "value": snapshot.high_level,
            },
            {
                "parameter_id": "carrier.low_level",
                "mode": "set",
                "value": snapshot.low_level,
            },
        ]
        allowed = {
            "carrier.frequency",
            "carrier.high_level",
            "carrier.low_level",
            "carrier.amplitude",
            "carrier.offset",
        }
        if any(action.get("parameter_id") not in allowed for action in actions):
            raise ConfigurationError("Rigol node contains an unsupported parameter action.")
        sweeps = [action for action in actions if action.get("mode") == "sweep"]
        if len(sweeps) > 1:
            raise ConfigurationError("One Rigol node supports one local sweep axis.")
        roi_required = any(
            not isinstance(action.get("segments"), list) or not action.get("segments")
            for action in sweeps
        )
        children = [
            RecipePage._node_to_mapping(child)
            for child in node.children
            if not (
                child.type == "comment"
                and child.data.get("text")
                == "Drop nested devices or flow actions here"
            )
        ]
        return {
            "id": node.id,
            "type": "sequence",
            "text": f"Rigol CH{channel} · OUTPUT {output_policy.upper()}",
            "device_module": "rigol",
            "label": "Rigol DG1032Z",
            "operation": "configure_selected_parameters",
            "configuration_required": roi_required,
            "channel": channel,
            "parameter_actions": actions,
            "output_policy": output_policy,
            "roi_required": roi_required,
            "configuration": {
                field: getattr(snapshot, field)
                for field in snapshot.__dataclass_fields__
            },
            "children": children,
        }

    def _current_rigol_snapshot(self) -> RigolConfigurationSnapshot:
        provider = self._rigol_snapshot_provider
        if callable(provider):
            try:
                snapshot = provider()
            except Exception as exc:
                self.status.emit(
                    f"Rigol form snapshot unavailable; using safe defaults: {exc}"
                )
                snapshot = None
            if isinstance(snapshot, RigolConfigurationSnapshot):
                return snapshot
        return RigolConfigurationSnapshot()

    def _rigol_snapshot_for(self, channel: int) -> RigolConfigurationSnapshot | None:
        provider = self._rigol_snapshot_provider
        if not callable(provider):
            return None
        try:
            snapshot = provider(channel)
        except TypeError:
            snapshot = provider()
            if isinstance(snapshot, RigolConfigurationSnapshot):
                return replace(snapshot, channel=channel)
        return snapshot if isinstance(snapshot, RigolConfigurationSnapshot) else None

    @staticmethod
    def _rigol_snapshot_from_mapping(
        configuration: dict[str, object],
        *,
        fallback: RigolConfigurationSnapshot,
    ) -> RigolConfigurationSnapshot:
        values: dict[str, object] = {}
        for field in fallback.__dataclass_fields__:
            if field not in configuration:
                continue
            value = configuration[field]
            if field == "channel":
                values[field] = int(value)
            elif field == "sync_enabled":
                if isinstance(value, bool):
                    values[field] = value
                elif isinstance(value, str) and value.strip().casefold() in {
                    "true",
                    "false",
                }:
                    values[field] = value.strip().casefold() == "true"
                else:
                    raise ConfigurationError(
                        "Rigol sync_enabled must be a YAML boolean."
                    )
            else:
                values[field] = str(value)
        snapshot = replace(fallback, **values)
        if snapshot.channel not in {1, 2}:
            raise ConfigurationError("Rigol output channel must be 1 or 2.")
        return snapshot

    def _current_anritsu_snapshot(self) -> AnritsuConfigurationSnapshot:
        provider = self._anritsu_snapshot_provider
        if callable(provider):
            try:
                snapshot = provider()
            except Exception as exc:
                self.status.emit(
                    f"Anritsu form snapshot unavailable; using safe defaults: {exc}"
                )
                snapshot = None
            if isinstance(snapshot, AnritsuConfigurationSnapshot):
                return snapshot
        return AnritsuConfigurationSnapshot(
            start_hz=1e6,
            stop_hz=10e6,
            reference_level_dbm=0.0,
            points=1001,
            instrument_mode="PLAN_EDIT",
        )

    def _current_anritsu_sg_snapshot(self) -> SignalGeneratorSnapshot:
        provider = self._anritsu_sg_snapshot_provider
        if callable(provider):
            try:
                snapshot = provider()
            except Exception as exc:
                self.status.emit(
                    "Anritsu SG form snapshot unavailable; using safe defaults: "
                    f"{exc}"
                )
                snapshot = None
            if isinstance(snapshot, SignalGeneratorSnapshot):
                return snapshot
        generator = self._settings.anritsu.signal_generator
        frequency = generator.frequency.min or "1 GHz"
        power = generator.power.min or "-30 dBm"
        return SignalGeneratorSnapshot(
            frequency_hz=parse_quantity(
                frequency, DIMENSION_FREQUENCY
            ).si_value,
            power_dbm=parse_quantity(power, DIMENSION_DBM).si_value,
            output_enabled=False,
            instrument_mode="PLAN_EDIT",
        )

    @staticmethod
    def _anritsu_snapshot_from_mapping(
        configuration: dict[str, object],
        *,
        fallback: AnritsuConfigurationSnapshot,
    ) -> AnritsuConfigurationSnapshot:
        snapshot = replace(
            fallback,
            start_hz=parse_quantity(
                configuration["start_frequency"], DIMENSION_FREQUENCY
            ).si_value,
            stop_hz=parse_quantity(
                configuration["stop_frequency"], DIMENSION_FREQUENCY
            ).si_value,
            reference_level_dbm=parse_quantity(
                configuration["reference_level"], DIMENSION_DBM
            ).si_value,
            points=int(configuration["points"]),
        )
        if snapshot.start_hz >= snapshot.stop_hz:
            raise ConfigurationError(
                "Anritsu start frequency must be below stop frequency."
            )
        if snapshot.points < 2:
            raise ConfigurationError("Anritsu spectrum requires at least 2 points.")
        return snapshot

    @staticmethod
    def _anritsu_snapshot_with_override(
        snapshot: AnritsuConfigurationSnapshot,
        override: dict[str, object],
    ) -> AnritsuConfigurationSnapshot:
        if override.get("mode") != "set":
            return snapshot
        parameter_id = str(override.get("parameter_id", ""))
        value = str(override.get("value", ""))
        if parameter_id == "spectrum.start_frequency":
            return replace(
                snapshot,
                start_hz=parse_quantity(value, DIMENSION_FREQUENCY).si_value,
            )
        if parameter_id == "spectrum.stop_frequency":
            return replace(
                snapshot,
                stop_hz=parse_quantity(value, DIMENSION_FREQUENCY).si_value,
            )
        if parameter_id == "spectrum.reference_level":
            return replace(
                snapshot,
                reference_level_dbm=parse_quantity(value, DIMENSION_DBM).si_value,
            )
        if parameter_id == "spectrum.points":
            return replace(snapshot, points=int(value))
        return snapshot

    @staticmethod
    def _configured_anritsu_sg_node(
        node: RecipeNode,
        *,
        frequency: str,
        power: str,
        parameter_actions: list[dict[str, object]],
        output_policy: str = "unchanged",
    ) -> dict[str, object]:
        allowed = {"sg.frequency", "sg.power"}
        if any(
            action.get("parameter_id") not in allowed
            or action.get("mode") not in {"set", "sweep"}
            for action in parameter_actions
        ):
            raise ConfigurationError(
                "Anritsu SG node contains an unsupported parameter action."
            )
        sweeps = [
            action for action in parameter_actions if action.get("mode") == "sweep"
        ]
        if len(sweeps) > 1:
            raise ConfigurationError("One Anritsu SG node supports one sweep axis.")
        if output_policy not in {
            "unchanged",
            "on",
            "off",
            "on_keep",
            "continue",
        }:
            raise ConfigurationError("Invalid Anritsu SG output policy.")
        roi_required = any(
            not isinstance(action.get("segments"), list)
            or not action.get("segments")
            for action in sweeps
        )
        output_label = {
            "unchanged": "OFF",
            "off": "OFF",
            "on": "ON FOR BLOCK",
            "on_keep": "KEEP ON",
            "continue": "CONTINUE ON",
        }[output_policy]
        return {
            "id": node.id,
            "type": "sequence",
            "text": (
                f"Anritsu SG · {len(parameter_actions)} parameter(s) · "
                f"RF {output_label}"
            ),
            "device_module": "anritsu_sg",
            "label": "Anritsu MS2830A Signal Generator",
            "operation": "configure_selected_parameters",
            "configuration_required": roi_required,
            "roi_required": roi_required,
            "parameter_actions": parameter_actions,
            "output_policy": output_policy,
            "configuration": {
                "frequency": frequency,
                "power": power,
            },
            "children": [
                RecipePage._node_to_mapping(child) for child in node.children
            ],
        }

    @staticmethod
    def _configured_anritsu_node(
        node: RecipeNode,
        *,
        snapshot: AnritsuConfigurationSnapshot | None = None,
        parameter_actions: list[dict[str, object]],
        acquire_single: bool,
        trace: str,
        post_configuration_operation: str = "configure",
        acquisition_average_count: int = 1,
        acquisition_reference_operation: str = "none",
    ) -> dict[str, object]:
        allowed = {
            "spectrum.start_frequency",
            "spectrum.stop_frequency",
            "spectrum.reference_level",
            "spectrum.points",
            "advanced.rbw_mode",
            "advanced.rbw",
            "advanced.vbw_mode",
            "advanced.vbw",
            "advanced.detector",
            "advanced.attenuation_mode",
            "advanced.attenuation",
            "advanced.preamplifier_enabled",
            "advanced.sweep_time_mode",
            "advanced.sweep_time",
        }
        if any(action.get("parameter_id") not in allowed for action in parameter_actions):
            raise ConfigurationError("Anritsu node contains an unsupported parameter action.")
        sweeps = [action for action in parameter_actions if action.get("mode") == "sweep"]
        if len(sweeps) > 1:
            raise ConfigurationError("One Anritsu node supports one sweep axis.")
        roi_required = any(
            not isinstance(action.get("segments"), list) or not action.get("segments")
            for action in sweeps
        )
        role = post_configuration_operation
        if acquire_single and role == "configure":
            role = "acquire_spectrum"
        if role not in {"configure", "acquire_spectrum", "acquire_reference"}:
            raise ConfigurationError(f"Unsupported Anritsu node role {role!r}.")
        if not 1 <= acquisition_average_count <= 9999:
            raise ConfigurationError(
                "Anritsu acquisition average count must be between 1 and 9999."
            )
        allowed_reference_operations = {
            "none",
            "difference_db",
            "ratio_linear",
            "add_power",
            "subtract_power",
            "multiply_linear",
        }
        if acquisition_reference_operation not in allowed_reference_operations:
            raise ConfigurationError(
                "Anritsu acquisition contains an unsupported reference operation."
            )
        managed_id = str(node.data.get("managed_acquisition_id", ""))
        if not managed_id and acquire_single:
            legacy = next(
                (child for child in node.children if child.type == "acquire_spectrum"),
                None,
            )
            managed_id = legacy.id if legacy is not None else ""
        children = [
            RecipePage._node_to_mapping(child)
            for child in node.children
            if not managed_id or child.id != managed_id
        ]
        if role != "configure":
            managed_id = managed_id or f"anritsu-acquire-{uuid4().hex[:8]}"
            acquisition: dict[str, object] = {
                "id": managed_id,
                "type": role,
                "trace": trace,
                "average_count": acquisition_average_count,
            }
            if role == "acquire_spectrum":
                acquisition.update(
                    {
                        "reference_operation": acquisition_reference_operation,
                        "store_raw": True,
                        "store_processed": acquisition_reference_operation != "none",
                    }
                )
            children.append(acquisition)
        else:
            managed_id = ""
        snapshot = snapshot or AnritsuConfigurationSnapshot(
            start_hz=1e6,
            stop_hz=10e6,
            reference_level_dbm=0.0,
            points=1001,
            instrument_mode="PLAN_EDIT",
        )
        return {
            "id": node.id,
            "type": "sequence",
            "text": (
                f"Anritsu Spectrum · {len(parameter_actions)} parameter(s)"
                + (
                    " · acquire spectrum"
                    if role == "acquire_spectrum"
                    else " · acquire reference"
                    if role == "acquire_reference"
                    else " · settings only"
                )
            ),
            "device_module": "anritsu",
            "label": "Anritsu MS2830A",
            "operation": "configure_selected_parameters",
            "configuration_required": roi_required,
            "roi_required": roi_required,
            "parameter_actions": parameter_actions,
            "acquire_single": False,
            "post_configuration_operation": role,
            "managed_acquisition_id": managed_id,
            "acquisition_average_count": acquisition_average_count,
            "acquisition_reference_operation": acquisition_reference_operation,
            "trace": trace,
            "configuration": {
                "start_frequency": f"{snapshot.start_hz:.12g} Hz",
                "stop_frequency": f"{snapshot.stop_hz:.12g} Hz",
                "reference_level": f"{snapshot.reference_level_dbm:.12g} dBm",
                "points": snapshot.points,
            },
            "children": children,
        }

    def _edit_anritsu_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        parameter_id = str(metadata.get("parameter_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = owner_item.data(0, Qt.ItemDataRole.UserRole) if owner_item else None
        if not isinstance(node, RecipeNode):
            QMessageBox.warning(self, "ROI editor", "The Anritsu node no longer exists.")
            return
        actions = [
            dict(action)
            for action in node.data.get("parameter_actions", [])
            if isinstance(action, dict)
        ]
        action = next(
            (
                candidate for candidate in actions
                if candidate.get("parameter_id") == parameter_id
                and candidate.get("mode") == "sweep"
            ),
            None,
        )
        if action is None:
            return
        label = next(
            label for candidate, label, _ in AnritsuNodeEditorDialog.parameter_specs
            if candidate == parameter_id
        )
        definition = AnritsuNodeEditorDialog._roi_definition(parameter_id, label)
        initial = action.get("segments")
        dialog = SweepGeneratorDialog(
            definition,
            self,
            initial_segments=(
                [dict(segment) for segment in initial if isinstance(segment, dict)]
                if isinstance(initial, list) else None
            ),
        )
        stage_index = metadata.get("stage_index")
        dialog.select_interval(stage_index if isinstance(stage_index, int) else None)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            action["segments"] = dialog.segment_data()
            configuration = node.data.get("configuration")
            snapshot = self._current_anritsu_snapshot()
            if isinstance(configuration, dict):
                snapshot = self._anritsu_snapshot_from_mapping(
                    configuration, fallback=snapshot
                )
            replacement = self._configured_anritsu_node(
                node,
                snapshot=snapshot,
                parameter_actions=actions,
                acquire_single=bool(node.data.get("acquire_single", False)),
                trace=str(node.data.get("trace", "TRAC1")),
                post_configuration_operation=str(
                    node.data.get("post_configuration_operation", "configure")
                ),
                acquisition_average_count=int(
                    node.data.get("acquisition_average_count", 1)
                ),
                acquisition_reference_operation=str(
                    node.data.get("acquisition_reference_operation", "none")
                ),
            )
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, f"Updated Anritsu ROI for {parameter_id}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu ROI editor", str(exc))

    def _edit_comment_node(self, node: RecipeNode) -> None:
        dialog = CommentEditorDialog(str(node.data.get("text", "")), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        replacement["text"] = dialog.comment_text()
        try:
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Updated comment {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Edit comment", str(exc))

    def _edit_anritsu_acquisition_node(self, node: RecipeNode) -> None:
        dialog = AnritsuAcquisitionEditorDialog(node, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        for field in (
            "trace",
            "average_count",
            "reference_operation",
            "store_raw",
            "store_processed",
        ):
            replacement.pop(field, None)
        replacement.update(dialog.node_fields())
        try:
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, f"Updated Anritsu acquisition {node.id}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu acquisition", str(exc))

    @staticmethod
    def _configured_keithley_node(
        node: RecipeNode,
        snapshot: KeithleyConfigurationSnapshot,
        *,
        parameter_actions: list[dict[str, object]] | None = None,
        output_policy: str = "unchanged",
    ) -> dict[str, object]:
        actions = (
            [
                {
                    "parameter_id": "source.level",
                    "mode": "set",
                    "value": snapshot.source_level,
                }
            ]
            if parameter_actions is None
            else parameter_actions
        )
        allowed_parameters = {
            "source.level",
            "source.compliance",
            "measurement.nplc",
            "measurement.settling_time",
            "measurement.sense_mode",
            "source.range",
            "measurement.voltage_range",
            "measurement.current_range",
        }
        if any(action.get("parameter_id") not in allowed_parameters for action in actions):
            raise ConfigurationError("Keithley node contains an unsupported parameter action.")
        if output_policy not in {
            "unchanged",
            "on",
            "off",
            "on_keep",
            "continue",
        }:
            raise ConfigurationError(
                "Keithley output policy must be off, on for the block, kept on, "
                "or continuous."
            )
        sweep_actions = [action for action in actions if action.get("mode") == "sweep"]
        if len(sweep_actions) > 1:
            raise ConfigurationError(
                "A Keithley device node supports one sweep axis; nest another node "
                "to create another loop."
            )
        roi_required = any(
            not isinstance(action.get("segments"), list)
            or not action.get("segments")
            for action in sweep_actions
        )
        result: dict[str, object] = {
            "id": node.id,
            "type": "sequence",
            "text": f"Keithley {snapshot.channel} · {len(actions)} selected action(s)",
            "device_module": "keithley",
            "label": "Keithley 2600",
            "configuration_required": roi_required,
            "channel": snapshot.channel,
            "source_mode": snapshot.source_mode,
            "operation": "configure_selected_parameters",
            "parameter_actions": actions,
            "output_policy": output_policy,
            "roi_required": roi_required,
            "configuration": {
                "channel": snapshot.channel,
                "source_mode": snapshot.source_mode,
                "source_level": snapshot.source_level,
                "compliance": snapshot.compliance,
                "nplc": snapshot.nplc,
                "settling_time": snapshot.settling_time,
                "sense_mode": snapshot.sense_mode,
                "source_autorange": snapshot.source_autorange,
                "source_range": snapshot.source_range,
                "measure_voltage_autorange": snapshot.measure_voltage_autorange,
                "measure_voltage_range": snapshot.measure_voltage_range,
                "measure_current_autorange": snapshot.measure_current_autorange,
                "measure_current_range": snapshot.measure_current_range,
            },
        }
        if node.children:
            result["children"] = [
                RecipePage._node_to_mapping(child) for child in node.children
            ]
        return result

    @staticmethod
    def _keithley_roi_definition(
        snapshot: KeithleyConfigurationSnapshot,
        parameter_id: str,
    ) -> dict[str, str]:
        return _keithley_roi_definition(snapshot, parameter_id)

    def _edit_keithley_rois(
        self,
        snapshot: KeithleyConfigurationSnapshot,
        actions: list[dict[str, object]],
    ) -> list[dict[str, object]] | None:
        completed: list[dict[str, object]] = []
        for action in actions:
            if action.get("mode") != "sweep":
                completed.append(dict(action))
                continue
            definition = self._keithley_roi_definition(
                snapshot, str(action.get("parameter_id", ""))
            )
            initial = action.get("segments")
            if isinstance(initial, list) and initial:
                try:
                    generate_sweep_points(initial, definition["dimension"])
                except Exception:
                    pass
                else:
                    completed.append(dict(action))
                    continue
            roi_dialog = SweepGeneratorDialog(
                definition,
                self,
                initial_segments=(
                    [dict(segment) for segment in initial if isinstance(segment, dict)]
                    if isinstance(initial, list)
                    else None
                ),
            )
            roi_dialog.setWindowTitle(
                f"Keithley {snapshot.channel} — ROI · {definition['label']}"
            )
            if roi_dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            completed_action = dict(action)
            completed_action["segments"] = roi_dialog.segment_data()
            completed.append(completed_action)
        return completed

    def _current_keithley_snapshot(self) -> KeithleyConfigurationSnapshot:
        provider = self._keithley_snapshot_provider
        if callable(provider):
            try:
                snapshot = provider()
            except Exception as exc:
                self.status.emit(
                    f"Keithley form snapshot unavailable; using safe defaults: {exc}"
                )
                snapshot = None
            if isinstance(snapshot, KeithleyConfigurationSnapshot):
                return snapshot
        return KeithleyConfigurationSnapshot()

    def _keithley_snapshot_for(
        self, channel: str, mode: str
    ) -> KeithleyConfigurationSnapshot | None:
        provider = self._keithley_snapshot_provider
        if not callable(provider):
            return None
        try:
            snapshot = provider(channel, mode)
        except TypeError:
            snapshot = provider()
            if isinstance(snapshot, KeithleyConfigurationSnapshot):
                return replace(snapshot, channel=channel, source_mode=mode)
        return snapshot if isinstance(snapshot, KeithleyConfigurationSnapshot) else None

    @staticmethod
    def _snapshot_with_override(
        snapshot: KeithleyConfigurationSnapshot,
        override: dict[str, object],
    ) -> KeithleyConfigurationSnapshot:
        parameter_id = str(override.get("parameter_id", ""))
        value = str(override.get("value", ""))
        field_by_parameter = {
            "source.level": "source_level",
            "source.compliance": "compliance",
            "measurement.nplc": "nplc",
            "measurement.settling_time": "settling_time",
            "measurement.sense_mode": "sense_mode",
            "source.range": "source_range",
            "measurement.voltage_range": "measure_voltage_range",
            "measurement.current_range": "measure_current_range",
        }
        field = field_by_parameter.get(parameter_id)
        return replace(snapshot, **{field: value}) if field is not None else snapshot

    def _open_current_node_editor(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            self._edit_selected_node()

    def _open_node_editor(self, item: QTreeWidgetItem, _column: int) -> None:
        """Open the appropriate parameter window from a direct tree interaction."""

        if not self._tree_editing_allowed():
            return
        self.tree.setCurrentItem(item)
        operator_row = item.data(0, self.operator_row_role)
        if isinstance(operator_row, dict):
            if operator_row.get("kind") == "output_policy":
                self._edit_output_policy_from_tree(dict(operator_row))
            else:
                self._edit_selected_roi()
            return
        structural = item.data(0, RecipeTreeWidget.structural_role)
        if structural == RecipeTreeWidget.finally_container:
            self._edit_automatic_shutdown("keithley.outputs_off")
            return
        if isinstance(structural, str) and structural.startswith("automatic_shutdown:"):
            self._edit_automatic_shutdown(structural.split(":", 1)[1])
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(node, RecipeNode):
            return
        self._edit_selected_node()

    def _edit_selected_fixed_keithley(self, node: RecipeNode) -> None:
        channel = str(node.data.get("channel", ""))
        mode = str(node.data.get("mode", ""))
        target = f"keithley.{channel}.{mode}"
        definition = next((item for item in _SWEEPABLE_PARAMETERS if item["target"] == target), None)
        if definition is None or mode not in {"current", "voltage"}:
            QMessageBox.information(
                self, "Edit Keithley setting", "This node is not a source current or voltage setting."
            )
            return
        dialog = FixedValueDialog(definition, self)
        dialog.value.setText(str(node.data.get("level", "")))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        replacement["level"] = dialog.value.text().strip()
        try:
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Updated fixed Keithley setting {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Edit Keithley setting", str(exc))

    def _show_tree_context_menu(self, position: object) -> None:
        """Expose the same safe commands under the currently pointed node."""

        point = position.toPoint() if hasattr(position, "toPoint") else position
        item = self.tree.itemAt(point)
        if item is not None:
            self.tree.setCurrentItem(item)
        else:
            self.tree.clearSelection()
            self.tree.setCurrentItem(None)
        menu = RoundMenu(parent=self.tree)

        def add_action(text: str, callback: Callable[[], None]) -> QAction:
            action = QAction(text, menu)
            action.triggered.connect(
                lambda _checked=False, command=callback: command()
            )
            menu.addAction(action)
            return action

        add_action("New empty sweep", self.new_recipe)
        if not self._tree_editing_allowed():
            if self._yaml_draft_pending() and not self._historical_sweep_active:
                add_action("Apply YAML to tree", self.apply_yaml_to_tree)
            menu.exec(self.tree.viewport().mapToGlobal(point))
            return
        menu.addSeparator()
        add_action(
            "Add sequence / group",
            lambda: self._add_basic_node("sequence"),
        )
        add_action(
            "Add device control / point generator",
            lambda: self._add_device_controls(),
        )
        add_action("Wrap in Repeat...", self._wrap_selected_in_repeat)
        menu.addSeparator()
        edit_device = add_action(
            "Device settings", self._edit_selected_device_settings
        )
        edit_roi = add_action("Edit ROI", self._edit_selected_roi)
        edit_comment = add_action("Edit comment", self._edit_selected_node)
        edit_acquisition = add_action(
            "Acquisition settings", self._edit_selected_node
        )
        edit_action = add_action("Action settings", self._edit_selected_node)
        toggle_enabled = add_action(
            "Enable selected node"
            if isinstance(node := (
                self.tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
                if self.tree.currentItem() is not None
                else None
            ), RecipeNode)
            and node.data.get("disabled") is True
            else "Disable selected node",
            self._toggle_selected_node_disabled,
        )
        duplicate = add_action("Duplicate selected node", self._duplicate_selected_node)
        delete = add_action("Delete selected node", self._delete_selected_node)
        move_up = add_action("Move selected node up", lambda: self._move_selected_sibling(-1))
        move_down = add_action("Move selected node down", lambda: self._move_selected_sibling(1))
        selected = self.tree.currentItem()
        node = selected.data(0, Qt.ItemDataRole.UserRole) if selected is not None else None
        operator_row = (
            selected.data(0, self.operator_row_role)
            if selected is not None
            else None
        )
        editable = self._tree_editing_allowed() and isinstance(node, RecipeNode)
        has_device_settings = bool(
            editable
            and (
                node.data.get("device_module")
                or self._legacy_device_configuration_node(node) is not None
            )
        )
        has_roi = bool(
            isinstance(operator_row, dict)
            or (
                editable
                and (
                    node.type == "sweep"
                    or (
                        node.data.get("device_module")
                        and any(
                            isinstance(action, dict)
                            and action.get("mode") == "sweep"
                            for action in node.data.get("parameter_actions", [])
                        )
                    )
                )
            )
        )
        edit_device.setEnabled(has_device_settings)
        edit_roi.setEnabled(has_roi)
        edit_comment.setEnabled(editable and node.type == "comment")
        edit_acquisition.setEnabled(
            editable
            and node.type in {"acquire_reference", "acquire_spectrum"}
        )
        edit_action.setEnabled(
            editable
            and not has_device_settings
            and not has_roi
            and node.type
            not in {"comment", "acquire_reference", "acquire_spectrum"}
        )
        toggle_enabled.setEnabled(
            editable and not self._tree_item_is_in_finally(selected)
        )
        duplicate.setEnabled(editable and selected.parent() is not None)
        delete.setEnabled(editable and selected.parent() is not None)
        move_up.setEnabled(editable and selected.parent() is not None)
        move_down.setEnabled(editable and selected.parent() is not None)
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _fixed_node_from_dialog(
        self, definition: dict[str, str], dialog: FixedValueDialog
    ) -> dict[str, object]:
        """Use the same device actions for a literal setpoint and a sweep point."""

        target = definition["target"]
        value = dialog.value.text().strip()
        if target.startswith("keithley."):
            _device, channel, mode = target.split(".")
            channel_settings = self._settings.keithley.safety.channels[channel]
            is_current = mode == "current"
            return {
                "id": self._new_node_id("configure-keithley"),
                "type": "configure_keithley",
                "channel": channel,
                "mode": "current" if is_current else "voltage",
                "level": value,
                "compliance": (
                    channel_settings.lab_limits.voltage_compliance.max
                    if is_current
                    else channel_settings.lab_limits.current_compliance.max
                ),
                "nplc": 1.0,
                "settle_time": "100 ms",
            }
        if target.startswith("rigol."):
            _device, channel, field = target.split(".")
            defaults = self._settings.rigol.safety.channels[channel].defaults
            return {
                "id": self._new_node_id("configure-rigol"),
                "type": "configure_rigol",
                "channel": int(channel),
                "waveform": str(defaults.get("waveform", "SIN")),
                "frequency": value if field == "frequency" else defaults.get("frequency", "1 kHz"),
                "high_level": value if field == "high_level" else defaults.get("high_level", "1 mV"),
                "low_level": value if field == "low_level" else defaults.get("low_level", "-1 mV"),
                "output_load": defaults.get("output_load_setting", "HIGHZ"),
            }
        if target.startswith("anritsu.spectrum."):
            field = target.rsplit(".", 1)[1]
            defaults = self._settings.anritsu.safety.defaults
            return {
                "id": self._new_node_id("configure-anritsu"),
                "type": "configure_anritsu",
                "start_frequency": value if field == "start_frequency" else defaults.get("start_frequency", "1 MHz"),
                "stop_frequency": value if field == "stop_frequency" else defaults.get("stop_frequency", "10 MHz"),
                "reference_level": value if field == "reference_level" else defaults.get("reference_level", "0 dBm"),
                "points": int(defaults.get("sweep_points", 1001)),
            }
        if target.startswith("moke_box."):
            return {
                "id": self._new_node_id("configure-moke-box"),
                "type": "configure_moke_box",
                "field_target": value,
            }
        field = target.rsplit(".", 1)[1]
        defaults = self._settings.anritsu.safety.defaults
        return {
            "id": self._new_node_id("configure-anritsu-sg"),
            "type": "configure_anritsu_sg",
            "frequency": value if field == "frequency" else defaults.get("sg_frequency", "1 GHz"),
            "power": value if field == "power" else defaults.get("sg_power", "-30 dBm"),
        }

    def _edit_selected_generator(
        self,
        *,
        node: RecipeNode | None = None,
        stage_index: int | None = None,
    ) -> None:
        item = self.tree.currentItem()
        if node is None:
            node = (
                item.data(0, Qt.ItemDataRole.UserRole)
                if item is not None
                else None
            )
        if not isinstance(node, RecipeNode) or node.type != "sweep":
            QMessageBox.information(self, "Edit generator", "Select a generated sweep node first.")
            return
        target = str(node.data.get("target", ""))
        definition = next((item for item in _SWEEPABLE_PARAMETERS if item["target"] == target), None)
        segments = self._native_sweep_segments(node)
        if definition is None or not segments:
            QMessageBox.information(
                self,
                "Edit generator",
                "This sweep target cannot be represented by the visual ROI editor.",
            )
            return
        if target.startswith("keithley."):
            _device, channel, mode = target.split(".")
            dialog: SweepGeneratorDialog = KeithleySweepBuilderDialog(
                self._settings,
                self,
                initial_segments=segments,
                initial_channel=channel,
                initial_mode=mode,
            )
            assert isinstance(dialog, KeithleySweepBuilderDialog)
            existing_config = next(
                (child for child in node.children if child.type == "configure_keithley"), None
            )
            if existing_config is not None:
                dialog.compliance.setText(str(existing_config.data.get("compliance", dialog.compliance.text())))
                dialog.nplc.setText(str(existing_config.data.get("nplc", dialog.nplc.text())))
                dialog.settle_time.setText(str(existing_config.data.get("settle_time", dialog.settle_time.text())))
                dialog.sense_mode.setCurrentText(str(existing_config.data.get("sense_mode", dialog.sense_mode.currentText())))
        else:
            dialog = SweepGeneratorDialog(definition, self, initial_segments=segments)
        dialog.select_interval(stage_index)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        children = [self._node_to_mapping(child) for child in node.children]
        if isinstance(dialog, KeithleySweepBuilderDialog):
            options = dialog.keithley_options()
            for child in children:
                if child["type"] == "configure_keithley":
                    child.update(options)
        replacement = {"id": node.id, "type": "sweep", **node.data}
        for legacy_field in ("start", "stop", "points", "spacing"):
            replacement.pop(legacy_field, None)
        replacement.update(
            {
                "target": target,
                "segments": dialog.segment_data(),
                "children": children,
            }
        )
        try:
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Updated point generator {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Edit generator", str(exc))

    @staticmethod
    def _node_to_mapping(node: RecipeNode) -> dict[str, object]:
        result: dict[str, object] = {"id": node.id, "type": node.type, **node.data}
        if node.children:
            result["children"] = [RecipePage._node_to_mapping(child) for child in node.children]
        if node.else_children:
            result["else"] = [RecipePage._node_to_mapping(child) for child in node.else_children]
        return result

    def _sweep_node_from_generator(
        self,
        definition: dict[str, str],
        segments: list[dict[str, object]],
        *,
        keithley_options: dict[str, object] | None = None,
    ) -> dict[str, object]:
        target = definition["target"]
        node_id = self._new_node_id("sweep")
        if target.startswith("keithley."):
            _device, channel, mode = target.split(".")
            channel_settings = self._settings.keithley.safety.channels[channel]
            is_current = mode == "current"
            compliance = (
                channel_settings.lab_limits.voltage_compliance.max
                if is_current
                else channel_settings.lab_limits.current_compliance.max
            )
            child: dict[str, object] = {
                "id": self._new_node_id("configure-keithley"),
                "type": "configure_keithley",
                "channel": channel,
                "mode": "current" if is_current else "voltage",
                "level": "${" + target + "}",
                "compliance": (keithley_options or {}).get("compliance", compliance),
                "nplc": (keithley_options or {}).get("nplc", 1.0),
                "settle_time": (keithley_options or {}).get("settle_time", "100 ms"),
                "sense_mode": (keithley_options or {}).get("sense_mode", "2wire"),
            }
        elif target.startswith("rigol."):
            _device, channel, field = target.split(".")
            defaults = self._settings.rigol.safety.channels[channel].defaults
            child = {
                "id": self._new_node_id("configure-rigol"),
                "type": "configure_rigol",
                "channel": int(channel),
                "waveform": str(defaults.get("waveform", "SIN")),
                "frequency": "${" + target + "}" if field == "frequency" else defaults.get("frequency", "1 kHz"),
                "high_level": "${" + target + "}" if field == "high_level" else defaults.get("high_level", "1 mV"),
                "low_level": "${" + target + "}" if field == "low_level" else defaults.get("low_level", "-1 mV"),
                "output_load": defaults.get("output_load_setting", "HIGHZ"),
            }
        elif target.startswith("anritsu.spectrum."):
            field = target.rsplit(".", 1)[1]
            defaults = self._settings.anritsu.safety.defaults
            child = {
                "id": self._new_node_id("configure-anritsu"),
                "type": "configure_anritsu",
                "start_frequency": "${" + target + "}" if field == "start_frequency" else defaults.get("start_frequency", "1 MHz"),
                "stop_frequency": "${" + target + "}" if field == "stop_frequency" else defaults.get("stop_frequency", "10 MHz"),
                "reference_level": "${" + target + "}" if field == "reference_level" else defaults.get("reference_level", "0 dBm"),
                "points": int(defaults.get("sweep_points", 1001)),
            }
        elif target.startswith("moke_box."):
            child = {
                "id": self._new_node_id("configure-moke-box"),
                "type": "configure_moke_box",
                "field_target": "${" + target + "}",
            }
        else:
            field = target.rsplit(".", 1)[1]
            defaults = self._settings.anritsu.safety.defaults
            child = {
                "id": self._new_node_id("configure-anritsu-sg"),
                "type": "configure_anritsu_sg",
                "frequency": "${" + target + "}" if field == "frequency" else defaults.get("sg_frequency", "1 GHz"),
                "power": "${" + target + "}" if field == "power" else defaults.get("sg_power", "-30 dBm"),
            }
        return {
            "id": node_id,
            "type": "sweep",
            "target": target,
            "segments": segments,
            "children": [child],
        }

    def _delete_selected_node(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(node, RecipeNode):
            QMessageBox.information(self, "Delete node", "Select a recipe node first.")
            return
        try:
            source = delete_recipe_node(self._builder_source(), node_id=node.id)
            self._apply_builder_source(source, f"Deleted {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Delete node", str(exc))

    @staticmethod
    def _tree_item_is_in_finally(item: QTreeWidgetItem | None) -> bool:
        return RecipeTreeWidget.item_is_in_finally(item)

    def _toggle_selected_node_disabled(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(node, RecipeNode):
            return
        if self._tree_item_is_in_finally(item):
            QMessageBox.warning(
                self,
                "Disable node",
                "Finally safety actions cannot be disabled.",
            )
            return
        replacement = self._node_to_mapping(node)
        disabled = node.data.get("disabled") is not True
        if disabled:
            replacement["disabled"] = True
        else:
            replacement.pop("disabled", None)
        try:
            source = replace_recipe_node(
                self._builder_source(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source,
                f"{'Disabled' if disabled else 'Enabled'} {node.id}",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Enable / disable node", str(exc))

    def _duplicate_selected_node(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        parent = item.parent() if item is not None else None
        if not isinstance(node, RecipeNode) or parent is None:
            QMessageBox.information(
                self, "Duplicate node", "Select a non-root recipe node to duplicate."
            )
            return
        destination = self._tree_parent_destination(parent)
        if destination is None:
            QMessageBox.warning(
                self,
                "Duplicate node",
                "The selected node has no editable recipe parent.",
            )
            return
        parent_id, branch = destination
        copy = self._clone_node_mapping(self._node_to_mapping(node))
        copy_id = str(copy["id"])
        try:
            source = add_recipe_node(
                self._builder_source(),
                parent_id=parent_id,
                branch=branch,
                index=RecipeTreeWidget._logical_index(
                    parent,
                    item,
                    below=True,
                ),
                node=copy,
            )
            self._apply_builder_source(
                source,
                f"Duplicated {node.id}",
                selected_node_id=copy_id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Duplicate node", str(exc))

    def _clone_node_mapping(
        self, mapping: dict[str, object]
    ) -> dict[str, object]:
        clone = deepcopy(mapping)
        clone["id"] = self._new_node_id(str(clone.get("type", "node")))
        for branch in ("children", "else"):
            raw_children = clone.get(branch)
            if isinstance(raw_children, list):
                clone[branch] = [
                    self._clone_node_mapping(child)
                    for child in raw_children
                    if isinstance(child, dict)
                ]
        return clone

    def _move_selected_sibling(self, delta: int) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        parent = item.parent() if item is not None else None
        if not isinstance(node, RecipeNode) or parent is None:
            return
        destination = self._tree_parent_destination(parent)
        if destination is None:
            return
        parent_id, branch = destination
        index = RecipeTreeWidget._logical_index(parent, item, below=False)
        count = RecipeTreeWidget._logical_child_count(parent)
        if delta < 0:
            target = index - 1
        else:
            target = index + 2
        if target < 0 or target > count:
            return
        self._move_recipe_node(node.id, parent_id, branch, target)

    def request_run(self) -> None:
        if self._plan is not None:
            self.run_requested.emit(
                self._plan,
                self.execution_mode.currentData() == "dry_run",
                str(self.execution_mode.currentData()),
                self._requested_output_directory(),
                self._requested_output_file_stem(),
            )

    def _execution_mode_changed(self, _index: int = -1) -> None:
        mode = str(self.execution_mode.currentData())
        dry_run = mode == "dry_run"
        manual = mode == "manual_step"
        self.run_button.setText(
            "Run dry run" if dry_run else "Start manual stages" if manual else "Run plan"
        )
        self.execution_mode_hint.setText(
            (
                "DRY RUN: configurations, setpoints and Anritsu RAW/processed spectra "
                "run normally; every source OUTPUT remains confirmed OFF."
            )
            if dry_run
            else (
                "MANUAL STAGES: every normal recipe action waits for Next. "
                "Finally and emergency shutdown always execute automatically."
                if manual
                else "Normal measurement: OUTPUT actions in the recipe are executed."
            )
        )
        if self._preflight_thread is not None and self._preflight_thread.isRunning():
            self._preflight_thread.requestInterruption()
        if self._plan is not None:
            self._plan = None
            self.run_button.setEnabled(False)
            self.plan_preflight_changed.emit(None)
            self.summary.setText(
                "Execution mode changed; validate the recipe again before running."
            )
        self._refresh_document_state()
