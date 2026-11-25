"""Editable, validated station settings page."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    Pivot,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TreeWidget,
)

from app.domain.errors import AuthorizationError, ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    QuantityError,
    parse_quantity,
)
from app.safety.keithley_limit_reconciliation import (
    propose_keithley_limit_adjustments,
)
from app.security import AccessPolicy, Permission
from app.settings import SettingsRepository
from app.settings.diagnostics import (
    configuration_diagnostics,
    redacted_settings,
    structural_diff,
)
from app.settings.models import StationSettings
from app.settings.validation import format_settings_validation_error
from app.ui.design_system import set_dynamic_property
from app.ui.dialogs import StationDialog
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.dialogs import StationMessageBox as QMessageBox
from app.ui.settings_guidance import SettingsPath
from app.ui.widgets import KeithleyLimitProposalDialog, show_toast


@dataclass
class _LimitRowRecord:
    """Lightweight in-memory canonical representation of one safety-limit row."""

    scope: str
    parameter: str
    min_path: tuple[str | int, ...]
    max_path: tuple[str | int, ...] | None
    min_text: str
    max_text: str
    unit: str
    default_text: str
    source: str = "Enforced station profile"
    min_error: str | None = None
    max_error: str | None = None


class _LazyScrollArea(ScrollArea):
    """Populate a settings route's form cards on first show or direct widget() access."""

    def __init__(self, on_demand: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_demand = on_demand

    def showEvent(self, event) -> None:
        self._on_demand()
        super().showEvent(event)

    def widget(self) -> QWidget | None:
        self._on_demand()
        return super().widget()


_LIMIT_VALIDATION_MESSAGE_ROLE = int(Qt.ItemDataRole.UserRole) + 101
_MOKE_PATH = ("devices", "moke_box")
_MOKE_DERIVED_FIELDS = {"allowed_vout_channels", "test_vout_channels"}
_MOKE_ASSIGNMENT_FIELDS = ("enabled", "endpoint", "protocol_qualified", "allow_vout_control")

_SETTINGS_SECTION_LABELS = {
    "defaults": "Startup form values (not safety limits)",
}

_KEITHLEY_STARTUP_VALUE_LABELS = {
    "source_mode": "Initial source mode shown on the Keithley card",
    "source_current": "Initial source current shown on the Keithley card",
    "source_voltage": "Initial source voltage shown on the Keithley card",
    "current_compliance": "Initial current compliance shown on the Keithley card",
    "voltage_compliance": "Initial voltage compliance shown on the Keithley card",
    "settling_time": "Initial settling time shown on the Keithley card",
}

_SAFETY_LIMIT_LABELS = {
    "source_current": "Allowed source-current setting",
    "source_voltage": "Allowed source-voltage setting",
    "current_compliance": "Allowed current-compliance setting",
    "voltage_compliance": "Allowed voltage-compliance setting",
    "measured_current_trip": "Emergency measured-current cutoff (forces A+B OFF)",
    "measured_voltage_trip": "Emergency measured-voltage cutoff (forces A+B OFF)",
    "max_abs_power": "Maximum absolute power",
    "point_settle_time": "Allowed point settling time",
}


class _FluentSettingsSections(QWidget):
    """Fluent route picker and page stack for the Settings workspace.

    This is the interactive navigation itself, rather than a hidden legacy
    tab control.  Its small familiar API keeps the Settings workflow code
    focused on settings instead of navigation plumbing.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.navigation = Pivot(self)
        self.navigation.setItemFontSize(13)
        self.compact_navigation = ComboBox(self)
        self.compact_navigation.setAccessibleName("Settings section")
        # Start compact so Pivot's wide size hint cannot inflate the page
        # before its first real resize.  resizeEvent selects the proper mode.
        self.navigation.hide()
        self.compact_navigation.currentIndexChanged.connect(self.setCurrentIndex)
        self.stack = QStackedWidget(self)
        self.stack.setProperty("stationSurface", "page")
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        layout.addWidget(self.navigation)
        layout.addWidget(self.compact_navigation)
        layout.addWidget(self.stack, 1)
        self._routes: list[str] = []
        self._labels: list[str] = []
        self._on_index_changed: Callable[[int], None] | None = None

    def set_on_index_changed(self, callback: Callable[[int], None] | None) -> None:
        self._on_index_changed = callback

    def addTab(self, page: QWidget, label: str) -> int:
        index = self.stack.addWidget(page)
        route = f"settings-section-{index}"
        self._routes.append(route)
        self._labels.append(label)
        self.compact_navigation.addItem(label, userData=index)
        self.navigation.addItem(
            route,
            label,
            onClick=lambda _checked=False, index=index: self.setCurrentIndex(index),
        )
        if index == 0:
            self.setCurrentIndex(0)
        return index

    def count(self) -> int:
        return self.stack.count()

    def tabText(self, index: int) -> str:
        return self._labels[index]

    def currentWidget(self) -> QWidget:
        return self.stack.currentWidget()

    def setCurrentWidget(self, page: QWidget) -> None:
        self.setCurrentIndex(self.stack.indexOf(page))

    def setCurrentIndex(self, index: int) -> None:
        if self._on_index_changed is not None:
            self._on_index_changed(index)
        self.stack.setCurrentIndex(index)
        route = self._routes[index]
        self.navigation.setCurrentItem(route)
        if self.compact_navigation.currentIndex() != index:
            self.compact_navigation.blockSignals(True)
            self.compact_navigation.setCurrentIndex(index)
            self.compact_navigation.blockSignals(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # Nine station routes need a genuinely wide workspace.  A stable
        # breakpoint avoids font/platform-dependent size hints making the
        # Pivot flicker between itself and the compact selector after a theme
        # change.
        compact = event.size().width() < 1500
        self.navigation.setVisible(not compact)
        self.compact_navigation.setVisible(compact)


class _SafetyLimitValidationDelegate(QStyledItemDelegate):
    """Use the shared validation-state contract for inline limit edits."""

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: Any) -> QWidget:
        editor = LineEdit(parent)
        editor.setPlaceholderText("e.g. 10 mA")
        if index.data(_LIMIT_VALIDATION_MESSAGE_ROLE):
            editor.setProperty("validationState", "error")
        return editor


class SettingsPage(QWidget):
    """A generic validated leaf editor for every value in `.config/settings.yml`."""

    settings_saved = Signal(object)
    moke_address_change_requested = Signal()
    status = Signal(str)

    _PROTECTED_PATHS = frozenset({
        ("devices", "anritsu", "safety", "reference_level", "min"),
        ("devices", "anritsu", "safety", "reference_level", "max"),
        _MOKE_PATH + ("endpoint",),
        _MOKE_PATH + ("protocol_qualified",),
    })
    _OPERATOR_EDITABLE_PATHS = frozenset({
        ("devices", "rigol", "safety", "allow_output_enable"),
    })

    def __init__(
        self,
        repository: SettingsRepository,
        parent: QWidget | None = None,
        *,
        read_only: bool = False,
        access_policy: AccessPolicy | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._base_read_only = read_only
        self._access = access_policy or AccessPolicy.from_settings(repository.load().settings)
        self._read_only = read_only or not self._access.allows(Permission.EDIT_SETTINGS)
        self._raw: dict[str, Any] = {}
        self._persisted_raw: dict[str, Any] = {}
        self._settings: StationSettings | None = None
        self._choice_editors: dict[tuple[str | int, ...], QComboBox] = {}
        self._form_editors_store: dict[tuple[str | int, ...], QWidget] = {}
        self._field_errors: dict[tuple[str | int, ...], QLabel] = {}
        self._limit_rows: list[_LimitRowRecord] = []
        self._limit_records_by_path: dict[tuple[str | int, ...], tuple[_LimitRowRecord, str]] = {}
        self._limit_error_items: list[QTableWidgetItem] = []
        self._limit_items_by_path_store: dict[tuple[str | int, ...], QTableWidgetItem] = {}
        self._safety_limit_editors_store: dict[tuple[str | int, ...], QLineEdit] = {}
        self._safety_limit_error_labels: dict[tuple[str | int, ...], QLabel] = {}
        self._trees_cache: dict[str, QTreeWidget] | None = None
        self._trees_dirty = True
        self._limits_table_widget: TableWidget | None = None
        self._limits_table_dirty = True
        self._built_forms: set[str] = set()
        self._limits_cards_built = False
        self._roles_built = False
        self._diagnostics_dirty = True
        self._tab_names: list[str] = []
        self._changing = False
        self._source_autorange_acknowledged: set[tuple[str | int, ...]] = set()
        self._dirty = False
        self._autosave_enabled = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(750)
        # Compatibility object only: persistence now requires an explicit
        # SAVE SETTINGS / Save changes action.
        self._build()
        self.reload()

    _FORM_TAB_SPECS = (
        ("general", "General"),
        ("rigol", "Rigol"),
        ("keithley", "Keithley"),
        ("anritsu", "Anritsu"),
        ("moke_box", "MOKE Box"),
        ("lakeshore_gaussmeter", "Lake Shore 475"),
    )

    @property
    def trees(self) -> dict[str, QTreeWidget]:
        self._ensure_trees_built()
        assert self._trees_cache is not None
        return self._trees_cache

    @property
    def tree(self) -> QTreeWidget:
        return self.trees["general"]

    @property
    def limits_table(self) -> TableWidget:
        self._ensure_limits_table_built()
        assert self._limits_table_widget is not None
        return self._limits_table_widget

    @property
    def _limit_items_by_path(self) -> dict[tuple[str | int, ...], QTableWidgetItem]:
        self._ensure_limits_table_built()
        return self._limit_items_by_path_store

    @property
    def _form_editors(self) -> dict[tuple[str | int, ...], QWidget]:
        self._ensure_all_forms_built()
        return self._form_editors_store

    @property
    def _safety_limit_editors(self) -> dict[tuple[str | int, ...], QLineEdit]:
        self._ensure_limits_cards_built()
        return self._safety_limit_editors_store

    def _build(self) -> None:
        self.setProperty("stationSurface", "page")
        self.setObjectName("settingsPage")
        self.owns_viewport = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)

        self.profile_card = CardWidget(self)
        self.profile_card.setObjectName("settingsProfileCard")
        profile_layout = QVBoxLayout(self.profile_card)
        profile_layout.setContentsMargins(18, 14, 18, 14)
        profile_layout.setSpacing(4)
        title = SubtitleLabel("Station settings", self.profile_card)
        title.setObjectName("pageTitle")
        self._subtitle = BodyLabel(self.profile_card)
        self._subtitle.setObjectName("settingsProfileSummary")
        self._subtitle.setWordWrap(True)
        profile_layout.addWidget(title)
        profile_layout.addWidget(self._subtitle)
        layout.addWidget(self.profile_card)

        self.tabs = _FluentSettingsSections(self)
        self.section_navigation = self.tabs.navigation
        self.page_stack = self.tabs.stack
        # Hidden host reserved for legacy tree/table draft-model widgets if a
        # caller explicitly requests them via the lazy properties.
        self._draft_model_host = QWidget(self)
        self._draft_model_host.setObjectName("settingsDraftModelHost")
        self._draft_model_host.hide()
        self.forms: dict[str, ScrollArea] = {}
        self._tab_names = []
        for key, label in self._FORM_TAB_SPECS:
            form = _LazyScrollArea(lambda tab_key=key: self._ensure_form_built(tab_key))
            form.setObjectName("settingsForm")
            form.setProperty("stationSurface", "page")
            form.viewport().setProperty("stationSurface", "page")
            form.setWidgetResizable(True)
            form.setFrameShape(QFrame.Shape.NoFrame)
            self.forms[key] = form
            self._tab_names.append(key)
            self.tabs.addTab(form, label)
        limits_page = QWidget()
        limits_page.setObjectName("settingsSpecialPage")
        limits_page.setProperty("stationSurface", "page")
        limits_layout = QVBoxLayout(limits_page)
        limits_layout.setContentsMargins(18, 18, 18, 18)
        limits_layout.setSpacing(12)
        limits_title = StrongBodyLabel("Station safety boundaries")
        limits_title.setObjectName("sectionTitle")
        limits_description = BodyLabel(
            "These are enforced station boundaries, not startup values. Source and "
            "compliance rows constrain settings; emergency measured-value cutoffs trip "
            "during readback and force both Keithley outputs OFF. Enter explicit units, "
            "for example 10 mA, 67 mV or 1 MHz. Disabling a software boundary never "
            "disables immutable instrument limits."
        )
        limits_description.setObjectName("muted")
        limits_description.setWordWrap(True)
        self._limit_validation_toast_message: str | None = None
        limits_card = CardWidget()
        limits_card.setObjectName("settingsTableCard")
        limits_card_layout = QVBoxLayout(limits_card)
        limits_card_layout.setContentsMargins(14, 14, 14, 14)
        self.limits_scroll = _LazyScrollArea(self._ensure_limits_cards_built)
        self.limits_scroll.setObjectName("settingsForm")
        self.limits_scroll.setWidgetResizable(True)
        self.limits_scroll.setFrameShape(QFrame.Shape.NoFrame)
        limits_card_layout.addWidget(self.limits_scroll)
        limits_layout.addWidget(limits_title)
        limits_layout.addWidget(limits_description)
        limits_layout.addWidget(limits_card, 1)
        self.limits_page = limits_page
        self._tab_names.append("limits")
        self.tabs.addTab(limits_page, "Safety boundaries")
        roles_page = QWidget()
        roles_page.setObjectName("settingsSpecialPage")
        roles_page.setProperty("stationSurface", "page")
        roles_layout = QVBoxLayout(roles_page)
        roles_layout.setContentsMargins(18, 18, 18, 18)
        roles_layout.setSpacing(12)
        roles_title = StrongBodyLabel("Access roles")
        roles_title.setObjectName("sectionTitle")
        self.roles_info = BodyLabel()
        self.roles_info.setWordWrap(True)
        self.roles_info.setObjectName("muted")
        roles_card = CardWidget()
        roles_card.setObjectName("settingsTableCard")
        roles_card_layout = QVBoxLayout(roles_card)
        roles_card_layout.setContentsMargins(10, 10, 10, 10)
        self.role_table = TableWidget(self)
        self.role_table.setColumnCount(2)
        self.role_table.setHorizontalHeaderLabels(["Operating-system account", "Assigned role(s)"])
        self.role_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.role_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.role_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.role_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        role_buttons = QHBoxLayout()
        self.add_role_button = PrimaryPushButton("Add user…")
        self.edit_role_button = PushButton("Edit selected…")
        self.remove_role_button = PushButton("Remove selected")
        role_buttons.addWidget(self.add_role_button)
        role_buttons.addWidget(self.edit_role_button)
        role_buttons.addWidget(self.remove_role_button)
        role_buttons.addStretch(1)
        roles_card_layout.addWidget(self.role_table, 1)
        roles_card_layout.addLayout(role_buttons)
        roles_layout.addWidget(roles_title)
        roles_layout.addWidget(self.roles_info)
        roles_layout.addWidget(roles_card, 1)
        self._tab_names.append("roles")
        self.tabs.addTab(roles_page, "Access roles")
        diagnostics_page = QWidget()
        diagnostics_page.setObjectName("settingsSpecialPage")
        diagnostics_page.setProperty("stationSurface", "page")
        diagnostics_layout = QVBoxLayout(diagnostics_page)
        diagnostics_layout.setContentsMargins(18, 18, 18, 18)
        diagnostics_layout.setSpacing(12)
        diagnostics_title = StrongBodyLabel("Configuration diagnostics")
        diagnostics_title.setObjectName("sectionTitle")
        diagnostics_description = BodyLabel(
            "Read-only checks for the active station configuration."
        )
        diagnostics_description.setObjectName("muted")
        diagnostics_card = CardWidget()
        diagnostics_card.setObjectName("settingsTableCard")
        diagnostics_card_layout = QVBoxLayout(diagnostics_card)
        diagnostics_card_layout.setContentsMargins(10, 10, 10, 10)
        self.diagnostics_text = PlainTextEdit(self)
        self.diagnostics_text.setReadOnly(True)
        diagnostics_buttons = QHBoxLayout()
        refresh_diagnostics = PushButton("Refresh diagnostics")
        export_diagnostics = PushButton("Export redacted configuration…")
        diagnostics_buttons.addWidget(refresh_diagnostics)
        diagnostics_buttons.addWidget(export_diagnostics)
        diagnostics_buttons.addStretch(1)
        diagnostics_card_layout.addWidget(self.diagnostics_text, 1)
        diagnostics_layout.addWidget(diagnostics_title)
        diagnostics_layout.addWidget(diagnostics_description)
        diagnostics_layout.addWidget(diagnostics_card, 1)
        diagnostics_layout.addLayout(diagnostics_buttons)
        self._tab_names.append("diagnostics")
        self.tabs.addTab(diagnostics_page, "Diagnostics")
        self.tabs.set_on_index_changed(self._on_settings_tab_selected)
        layout.addWidget(self.tabs, 1)

        self.action_card = CardWidget(self)
        self.action_layout = QHBoxLayout(self.action_card)
        action_layout = self.action_layout
        action_layout.setContentsMargins(16, 12, 16, 12)
        action_layout.setSpacing(20)
        action_copy = QVBoxLayout()
        workflow_title = StrongBodyLabel("Configuration workflow", self.action_card)
        action_copy.addWidget(workflow_title)
        action_note = BodyLabel(
            "Validate before saving. Device permissions, limits and hardware readback remain enforced.",
            self.action_card,
        )
        action_note.setWordWrap(True)
        action_copy.addWidget(action_note)
        action_copy.addStretch(1)
        action_layout.addLayout(action_copy, 2)
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(8)
        self.reload_button = PushButton(FluentIcon.SYNC, "Reload")
        self.discard_button = PushButton("Discard draft")
        self.diff_button = PushButton(FluentIcon.DOCUMENT, "Show changes…")
        self.validate_button = PushButton("Validate")
        self.save_button = PrimaryPushButton(FluentIcon.SAVE, "Save changes")
        for index, button in enumerate(
            (
                self.reload_button,
                self.discard_button,
                self.diff_button,
                self.validate_button,
                self.save_button,
            )
        ):
            button.setMinimumWidth(136)
            buttons.addWidget(button, index // 3, index % 3)
        action_layout.addLayout(buttons, 3)
        layout.insertWidget(1, self.action_card)
        self._compact_layout: bool | None = None
        self.reload_button.clicked.connect(self.reload)
        self.discard_button.clicked.connect(self.discard_draft)
        self.diff_button.clicked.connect(self.show_changes)
        self.validate_button.clicked.connect(self.validate_draft)
        self.save_button.clicked.connect(self.save_draft)
        self.add_role_button.clicked.connect(self._add_role_assignment)
        self.edit_role_button.clicked.connect(self._edit_role_assignment)
        self.remove_role_button.clicked.connect(self._remove_role_assignment)
        refresh_diagnostics.clicked.connect(self._refresh_diagnostics)
        export_diagnostics.clicked.connect(self.export_redacted_configuration)
        if self._read_only and not self._can_edit_operator_output():
            self.validate_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.discard_button.setEnabled(False)
        self._update_role_controls()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        compact = event.size().width() < 900
        if self._compact_layout == compact:
            return
        self._compact_layout = compact
        self.action_layout.setDirection(
            QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        )

    def set_access_policy(self, access_policy: AccessPolicy) -> None:
        self._access = access_policy
        self._read_only = self._base_read_only or not access_policy.allows(Permission.EDIT_SETTINGS)
        can_edit = not self._read_only or self._can_edit_operator_output()
        self.validate_button.setEnabled(can_edit)
        self.save_button.setEnabled(can_edit)
        self.discard_button.setEnabled(can_edit)
        self._update_role_controls()
        self.reload()

    def _can_edit_operator_output(self) -> bool:
        return not self._base_read_only and self._access.allows(Permission.OPERATE_OUTPUT)

    def _update_role_controls(self) -> None:
        allowed = not self._base_read_only and self._access.allows(Permission.MANAGE_ROLES)
        self.add_role_button.setEnabled(allowed)
        self.edit_role_button.setEnabled(allowed)
        self.remove_role_button.setEnabled(allowed)
        self.roles_info.setText(
            "Identity provider: operating-system login. Unassigned accounts receive the configured "
            "default operator role. Engineers can manage operator and engineer accounts; "
            "only a service identity can grant the service role."
            + ("" if allowed else " This session is read-only for role assignments.")
        )

    def _require_access(self, permission: Permission, action: str, *, silent: bool = False) -> bool:
        try:
            self._access.require(permission, action=action)
        except AuthorizationError as exc:
            self.status.emit(f"ACCESS DENIED: {exc}")
            if not silent:
                QMessageBox.warning(self, "Access denied", str(exc))
            return False
        return True

    def reload(self) -> None:
        self._autosave_timer.stop()
        try:
            loaded = self._repository.load()
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Configuration error", str(exc))
            return
        self._settings = loaded.settings
        self._raw = deepcopy(loaded.raw)
        self._persisted_raw = deepcopy(loaded.raw)
        self._dirty = False
        self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()
        self.status.emit("settings.yml reloaded")

    @property
    def current_moke_address(self) -> str | None:
        return self._settings.moke_box.endpoint if self._settings is not None else None

    def accept_external_snapshot(self, settings: StationSettings, raw: dict[str, Any]) -> None:
        """Refresh a clean settings page after a background external save.

        An open draft keeps its edits and picks up any new MOKE address immediately.
        ``save_draft`` merges locally edited leaves into the latest file.
        """

        self._settings = settings
        if self._dirty:
            old_moke = self._get_path(self._persisted_raw, _MOKE_PATH)
            new_moke = self._get_path(raw, _MOKE_PATH)
            if any(old_moke[key] != new_moke[key] for key in ("endpoint", "protocol_qualified")):
                self._refresh_moke_assignment(old_moke, new_moke)
            self.status.emit("External settings saved; the open local draft was preserved")
            return
        self._raw = deepcopy(raw)
        self._persisted_raw = deepcopy(raw)
        self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()

    def _refresh_moke_assignment(self, old_moke: dict, new_moke: dict) -> None:
        """Refresh connection evidence and drop any permission edit for the old address."""
        draft_moke = self._get_path(self._raw, _MOKE_PATH)
        enabled = self._form_editors_store.get(_MOKE_PATH + ("enabled",))
        keep_enabled_edit = (
            isinstance(enabled, QCheckBox) and enabled.isChecked() != old_moke["enabled"]
        )
        was_changing = self._changing
        self._changing = True
        try:
            for key in (*_MOKE_ASSIGNMENT_FIELDS, *_MOKE_DERIVED_FIELDS):
                old_moke[key] = deepcopy(new_moke[key])
                draft_moke[key] = deepcopy(new_moke[key])
            if isinstance(enabled, QCheckBox) and not keep_enabled_edit:
                enabled.setChecked(new_moke["enabled"])
            allow_editor = self._form_editors_store.get(_MOKE_PATH + ("allow_vout_control",))
            if isinstance(allow_editor, QCheckBox):
                allow_editor.setChecked(new_moke["allow_vout_control"])
            qualified_editor = self._form_editors_store.get(_MOKE_PATH + ("protocol_qualified",))
            if isinstance(qualified_editor, QCheckBox):
                qualified_editor.setChecked(new_moke["protocol_qualified"])
            endpoint_editor = self._form_editors_store.get(_MOKE_PATH + ("endpoint",))
            if isinstance(endpoint_editor, QLineEdit):
                endpoint_editor.setText(self._format_scalar(new_moke["endpoint"]))
        finally:
            self._changing = was_changing

    def stage_external_snapshot(self, settings: StationSettings, raw: dict[str, Any]) -> None:
        """Replace the in-memory draft without persisting it."""

        self._autosave_timer.stop()
        self._settings = settings
        self._raw = deepcopy(raw)
        self._dirty = self._raw != self._persisted_raw
        self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()
        self.status.emit("Unsaved settings changes; press SAVE SETTINGS")

    def stage_limit_snapshot(
        self,
        settings: StationSettings,
        raw: dict[str, Any],
        changed_paths: set[tuple[str | int, ...]],
    ) -> None:
        """Stage changed safety-limit leaves without rebuilding every settings route."""

        if not self._can_stage_limit_snapshot(changed_paths):
            raise ValueError("Limit staging accepts only changed safety-limit leaves.")
        self._autosave_timer.stop()
        self._settings = settings
        self._raw = deepcopy(raw)
        self._dirty = self._raw != self._persisted_raw
        previous_changing = self._changing
        self._changing = True
        try:
            for path in changed_paths:
                value = self._get_path(self._raw, path)
                formatted = self._format_scalar(value)
                self._sync_limit_from_tree(path, formatted)
                self._sync_tree_from_limit(path, formatted)
        finally:
            self._changing = previous_changing
        self._update_subtitle()
        self.status.emit("Unsaved settings changes; press SAVE SETTINGS")

    @staticmethod
    def _can_stage_limit_snapshot(
        changed_paths: set[tuple[str | int, ...]],
    ) -> bool:
        """Return whether every changed leaf belongs to a direct safety-limit edit."""

        return bool(changed_paths) and all(
            len(path) >= 5
            and path[0] == "devices"
            and path[2] == "safety"
            and ("lab_limits" in path or path[-1] in {"min", "max"})
            for path in changed_paths
        )

    def _update_subtitle(self) -> None:
        if self._settings is None:
            return
        mode = (
            " • limited edit: Rigol output permission only"
            if self._read_only and self._can_edit_operator_output()
            else " • read-only for this role"
            if self._read_only
            else ""
        )
        self._subtitle.setText(
            f"File: {self._repository.path}  •  Configuration: "
            f"{self._settings.profile.name}  •  User: "
            f"{self._access.identity.display_name}{mode}"
        )

    def _refresh_diagnostics(self) -> None:
        self.diagnostics_text.setPlainText(
            "\n".join(configuration_diagnostics(self._repository.path, self._raw))
        )
        self._diagnostics_dirty = False

    def discard_draft(self) -> None:
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Discard draft",
                "Discard all unsaved changes and reload the persisted configuration?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.reload()
        self.status.emit("Settings draft discarded")

    def show_changes(self) -> None:
        try:
            draft = self._apply_tree_values()
        except (ConfigurationError, ValueError) as exc:
            QMessageBox.critical(self, "Cannot compare settings", str(exc))
            return
        changes = structural_diff(self._persisted_raw, draft)
        dialog = StationDialog(self)
        dialog.setWindowTitle("Settings changes")
        dialog.resize(760, 480)
        surface = dialog.use_modal_shell_content().surface
        layout = dialog.modal_content_layout(spacing=10)
        summary = BodyLabel(
            f"{len(changes)} unsaved structural change(s). Values will be validated before saving."
        )
        summary.setWordWrap(True)
        text = PlainTextEdit(surface)
        text.setReadOnly(True)
        text.setPlainText("\n".join(changes) if changes else "No unsaved changes.")
        layout.addWidget(summary)
        layout.addWidget(text, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        close = PushButton("Close", surface)
        close.clicked.connect(dialog.reject)
        footer.addWidget(close)
        layout.addLayout(footer)
        dialog.exec()

    def export_redacted_configuration(self) -> None:
        destination, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export redacted configuration",
            "lab-control-settings-redacted.json",
            "JSON files (*.json)",
        )
        if not destination:
            return
        try:
            payload = redacted_settings(self._apply_tree_values())
            with open(destination, "x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
        except FileExistsError:
            QMessageBox.warning(self, "Export not written", "The selected file already exists.")
            return
        except (OSError, ConfigurationError, ValueError) as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.emit(f"Redacted configuration exported: {destination}")

    def _on_settings_tab_selected(self, index: int) -> None:
        if not self._raw or index < 0 or index >= len(self._tab_names):
            return
        name = self._tab_names[index]
        if name in self.forms:
            self._ensure_form_built(name)
        elif name == "limits":
            self._ensure_limits_cards_built()
        elif name == "roles":
            self._ensure_roles_built()
        elif name == "diagnostics" and self._diagnostics_dirty:
            self._refresh_diagnostics()

    def _general_section_data(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self._raw.items()
            if key not in {"schema_version", "devices", "access_control"}
        }

    def _ensure_form_built(self, name: str) -> None:
        if name in self._built_forms or not self._raw:
            return
        previous_changing = self._changing
        self._changing = True
        try:
            self._built_forms.add(name)
            if name == "general":
                self._populate_form("general", self._general_section_data(), ())
            else:
                devices = self._raw.get("devices", {})
                self._populate_form(name, devices.get(name, {}), ("devices", name))
        finally:
            self._changing = previous_changing

    def _ensure_all_forms_built(self) -> None:
        for key, _label in self._FORM_TAB_SPECS:
            self._ensure_form_built(key)

    def _ensure_trees_built(self) -> None:
        if self._trees_cache is None:
            self._trees_cache = {}
            for key, _label in self._FORM_TAB_SPECS:
                tree = TreeWidget(self._draft_model_host)
                tree.setHeaderLabels(["Parameter", "Value"])
                tree.setAlternatingRowColors(True)
                tree.itemChanged.connect(self._changed)
                self._trees_cache[key] = tree
            self._trees_dirty = True
        if self._trees_dirty and self._raw:
            self._populate_trees()

    def _populate_trees(self) -> None:
        assert self._trees_cache is not None
        previous_changing = self._changing
        self._changing = True
        try:
            for tree in self._trees_cache.values():
                tree.clear()
            self._choice_editors.clear()
            general = self._general_section_data()
            self._add_items(self._trees_cache["general"], None, general, ())
            devices = self._raw.get("devices", {})
            for device in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"):
                self._add_items(
                    self._trees_cache[device],
                    None,
                    devices.get(device, {}),
                    ("devices", device),
                )
            for tree in self._trees_cache.values():
                tree.expandToDepth(3)
            self._trees_dirty = False
        finally:
            self._changing = previous_changing

    def _ensure_limits_table_built(self) -> None:
        if self._limits_table_widget is None:
            table = TableWidget(self._draft_model_host)
            table.setColumnCount(7)
            table.setHorizontalHeaderLabels(
                ["Device / scope", "Parameter", "Minimum", "Maximum", "Unit", "Default", "Source"]
            )
            table.setAlternatingRowColors(True)
            table.setItemDelegate(_SafetyLimitValidationDelegate(table))
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.verticalHeader().setVisible(False)
            limits_header = table.horizontalHeader()
            for column in range(7):
                limits_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(2, 170)
            table.setColumnWidth(3, 170)
            table.itemChanged.connect(self._limit_changed)
            table.hide()
            self._limits_table_widget = table
            self._limits_table_dirty = True
        if self._limits_table_dirty:
            self._populate_limits_table()

    def _populate_limits_table(self) -> None:
        assert self._limits_table_widget is not None
        table = self._limits_table_widget
        previous_changing = self._changing
        self._changing = True
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.setRowCount(len(self._limit_rows))
            self._limit_items_by_path_store.clear()
            self._limit_error_items.clear()
            for row, record in enumerate(self._limit_rows):
                values = (
                    record.scope,
                    record.parameter,
                    record.min_text,
                    record.max_text,
                    record.unit,
                    record.default_text,
                    record.source,
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    if column == 2:
                        item.setData(Qt.ItemDataRole.UserRole, record.min_path)
                        self._limit_items_by_path_store[record.min_path] = item
                        if not self._read_only:
                            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                        else:
                            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        if record.min_error:
                            item.setData(_LIMIT_VALIDATION_MESSAGE_ROLE, record.min_error)
                            item.setToolTip(f"Validation error: {record.min_error}")
                            self._limit_error_items.append(item)
                    elif column == 3 and record.max_path is not None:
                        item.setData(Qt.ItemDataRole.UserRole, record.max_path)
                        self._limit_items_by_path_store[record.max_path] = item
                        if not self._read_only:
                            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                        else:
                            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        if record.max_error:
                            item.setData(_LIMIT_VALIDATION_MESSAGE_ROLE, record.max_error)
                            item.setToolTip(f"Validation error: {record.max_error}")
                            self._limit_error_items.append(item)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    table.setItem(row, column, item)
            self._limits_table_dirty = False
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
            self._changing = previous_changing

    def _ensure_limits_cards_built(self) -> None:
        if self._limits_cards_built or not self._raw:
            return
        previous_changing = self._changing
        self._changing = True
        try:
            self._limits_cards_built = True
            self._populate_safety_limit_cards()
        finally:
            self._changing = previous_changing

    def _ensure_roles_built(self) -> None:
        if self._roles_built or not self._raw:
            return
        previous_changing = self._changing
        self._changing = True
        try:
            self._roles_built = True
            self._populate_roles()
        finally:
            self._changing = previous_changing

    def _populate(self) -> None:
        self._changing = True
        try:
            self._choice_editors.clear()
            self._form_editors_store.clear()
            self._field_errors.clear()
            self._safety_limit_editors_store.clear()
            self._safety_limit_error_labels.clear()
            self._limit_error_items.clear()
            self._built_forms.clear()
            self._limits_cards_built = False
            self._roles_built = False
            self._trees_dirty = True
            self._limits_table_dirty = True
            self._diagnostics_dirty = True
            self._collect_limit_rows()
            if self._trees_cache is not None:
                self._populate_trees()
            if self._limits_table_widget is not None:
                self._populate_limits_table()
            current_index = self.tabs.stack.currentIndex()
            if 0 <= current_index < len(self._tab_names):
                self._on_settings_tab_selected(current_index)
            else:
                self._ensure_form_built("general")
        finally:
            self._changing = False

    def _populate_forms(self, general: dict[str, Any], devices: dict[str, Any]) -> None:
        self._form_editors_store.clear()
        self._field_errors.clear()
        self._built_forms.clear()
        self._built_forms.add("general")
        self._populate_form("general", general, ())
        for device in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"):
            self._built_forms.add(device)
            self._populate_form(f"{device}", devices.get(device, {}), ("devices", device))

    @staticmethod
    def _title(text: object) -> str:
        key = str(text)
        return _SETTINGS_SECTION_LABELS.get(
            key, key.replace("_", " ").replace("-", " ").title()
        )

    def _populate_form(self, name: str, data: Any, prefix: tuple[str | int, ...]) -> None:
        host = QWidget()
        host.setProperty("stationSurface", "page")
        host.setMinimumWidth(0)
        host.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        layout = QVBoxLayout(host)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        cards: dict[str, QFormLayout] = {}

        def card_for(section: str, target: QVBoxLayout = layout) -> QFormLayout:
            if section not in cards:
                card = CardWidget(host)
                card.setObjectName("settingsCard")
                card.setMinimumWidth(0)
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(16, 14, 16, 16)
                card_layout.setSpacing(10)
                heading = StrongBodyLabel(section, card)
                heading.setObjectName("settingsSectionTitle")
                card_layout.addWidget(heading)
                form_host = QWidget(card)
                form_host.setMinimumWidth(0)
                form_host.setSizePolicy(
                    QSizePolicy.Policy.Ignored,
                    QSizePolicy.Policy.Preferred,
                )
                form = QFormLayout(form_host)
                form.setContentsMargins(0, 0, 0, 0)
                form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                form.setHorizontalSpacing(18)
                form.setVerticalSpacing(10)
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
                card_layout.addWidget(form_host)
                target.addWidget(card)
                cards[section] = form
            return cards[section]

        def walk(value: Any, path: tuple[str | int, ...], labels: tuple[str, ...]) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == "lab_limits":
                        continue
                    child_label = self._title(key)
                    if (
                        name == "keithley"
                        and "defaults" in path
                        and str(key) in _KEITHLEY_STARTUP_VALUE_LABELS
                    ):
                        child_label = _KEITHLEY_STARTUP_VALUE_LABELS[str(key)]
                    walk(nested, path + (str(key),), labels + (child_label,))
                return
            if isinstance(value, list):
                for index, nested in enumerate(value):
                    walk(nested, path + (index,), labels + (str(index + 1),))
                return
            section = labels[0] if labels else "Station"
            label = " › ".join(labels[1:]) or section
            label_widget = BodyLabel(label)
            label_widget.setWordWrap(True)
            label_widget.setMinimumWidth(240)
            label_widget.setMaximumWidth(320)
            label_widget.setToolTip(label)
            card_for(section).addRow(label_widget, self._form_editor(path, value))

        if name == "moke_box":
            connection = card_for("MOKE Box connection")
            endpoint = self._form_editor(prefix + ("endpoint",), data["endpoint"])
            change_address = PushButton("Change TCP address", icon=FluentIcon.SEARCH)
            change_address.setEnabled(
                not self._base_read_only and self._access.allows(Permission.ASSIGN_VISA)
            )
            change_address.clicked.connect(self.moke_address_change_requested)
            address_row = QHBoxLayout()
            address_row.setSpacing(8)
            address_row.addWidget(endpoint, 1)
            address_row.addWidget(change_address)
            connection.addRow(BodyLabel("TCP address"), address_row)
            connection.addRow(self._form_editor(prefix + ("enabled",), data["enabled"],
                                                checkbox_text="Enable MOKE Box"))

            permissions = card_for("VOUT permissions")
            master_path = prefix + ("allow_vout_control",)
            permissions.addRow(self._form_editor(master_path, data["allow_vout_control"],
                                                 checkbox_text="Allow voltage control"))
            primary = data["voltage_control"]
            primary_path = prefix + ("voltage_control",)
            channels = {
                profile["channel"]: (prefix + ("channel_profiles", key), profile)
                for key, profile in data["channel_profiles"].items()
            }
            channels[primary["channel"]] = (primary_path, primary)
            guidance = BodyLabel()
            guidance.setWordWrap(True)
            permissions.addRow(guidance)
            permissions.addRow(BodyLabel("Approved channels"))
            approval_controls = QGridLayout()
            approval_controls.setContentsMargins(0, 0, 0, 0)
            approval_controls.setHorizontalSpacing(16)
            approval_controls.setVerticalSpacing(6)
            approval_host = QWidget()
            approval_host.setLayout(approval_controls)
            permissions.addRow(approval_host)

            details = card_for("Channel profiles")
            selector = ComboBox()
            selector.setAccessibleName("VOUT channel profile")
            stack = QStackedWidget()
            stack.setMinimumWidth(0)
            stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            details.addRow(selector)
            details.addRow(stack)
            for index, (channel, (path, profile)) in enumerate(sorted(channels.items())):
                label = f"VOUT{channel}" + (" · Kepco coil" if path == primary_path else "")
                approval_path = path + ("approved",)
                field = self._form_editor(approval_path, profile["approved"], checkbox_text=label)
                approval_controls.addWidget(field, index // 2, index % 2)
                selector.addItem(label, userData=channel)
                profile_page = QWidget()
                profile_layout = QVBoxLayout(profile_page)
                profile_layout.setContentsMargins(0, 0, 0, 0)
                profile_form = QFormLayout()
                profile_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                profile_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
                for key, value in profile.items():
                    if key in {"approved", "channel"}:
                        continue
                    profile_form.addRow(
                        BodyLabel(self._title(key)), self._form_editor(path + (key,), value),
                    )
                profile_layout.addLayout(profile_form)
                profile_layout.addStretch(1)
                stack.addWidget(profile_page)
                self._bind_moke_approval(path)
            selector.currentIndexChanged.connect(stack.setCurrentIndex)
            selector.setCurrentIndex(0)

            master = self._form_editors_store[master_path]
            enabled = self._form_editors_store[prefix + ("enabled",)]
            primary_approval = self._form_editors_store[primary_path + ("approved",)]

            def update_moke_permission() -> None:
                saved_connection = self._get_path(self._raw, prefix)
                qualified = bool(saved_connection["endpoint"] and saved_connection["protocol_qualified"])
                if not qualified:
                    reason = "Test and assign the MOKE Box address to allow voltage control."
                elif not enabled.isChecked():
                    reason = "Enable the MOKE Box connection to allow voltage control."
                elif not primary_approval.isChecked():
                    reason = f"Approve the Kepco coil profile for VOUT{primary['channel']} first."
                else:
                    reason = "Confirm the wiring and qualification for this address before allowing voltage control."
                ready = qualified and enabled.isChecked() and primary_approval.isChecked()
                if not ready:
                    master.setChecked(False)
                master.setEnabled(ready and self._form_editable(master_path))
                guidance.setText(reason)

            evidence_hint = BodyLabel("To approve a channel, enter its binding ID and qualification reference.")
            evidence_hint.setWordWrap(True)
            details.insertRow(0, evidence_hint)
            other_fields = {key: value for key, value in data.items() if key not in {
                "enabled", "endpoint", "allow_vout_control", "voltage_control", "channel_profiles",
                *_MOKE_DERIVED_FIELDS,
            }}
            walk(other_fields, prefix, ("Connection and readout defaults",))
            enabled.toggled.connect(update_moke_permission)
            primary_approval.toggled.connect(update_moke_permission)
            self._form_editors_store[prefix + ("endpoint",)].textChanged.connect(update_moke_permission)
            self._form_editors_store[prefix + ("protocol_qualified",)].toggled.connect(update_moke_permission)
            update_moke_permission()
        else:
            walk(data, prefix, ())
        layout.addStretch(1)
        self.forms[name].setWidget(host)

    def _bind_moke_approval(self, path: tuple[str | int, ...]) -> None:
        approval_path = path + ("approved",)
        approval = self._form_editors_store[approval_path]
        binding = self._form_editors_store[path + ("binding_id",)]
        reference = self._form_editors_store[path + ("qualification_reference",)]

        def update_approval() -> None:
            has_evidence = bool(binding.text().strip() and reference.text().strip())
            if not has_evidence:
                approval.setChecked(False)
            approval.setEnabled(has_evidence and self._form_editable(approval_path))
            approval.setToolTip(
                "Allow this channel when voltage control is permitted."
                if has_evidence else
                "Enter the binding ID and qualification reference in Channel profiles to approve this channel."
            )

        binding.editingFinished.connect(update_approval)
        reference.editingFinished.connect(update_approval)
        update_approval()

    def _form_editable(self, path: tuple[str | int, ...]) -> bool:
        role_path = bool(path) and path[0] == "access_control"
        operator_output = (
            path in self._OPERATOR_EDITABLE_PATHS
            and not self._base_read_only
            and self._access.allows(Permission.OPERATE_OUTPUT)
        )
        return (
            (not self._read_only or operator_output)
            and (not role_path or self._access.allows(Permission.MANAGE_ROLES))
            and path not in self._PROTECTED_PATHS
            and not (path[:2] == _MOKE_PATH and path[-1] == "channel")
        )

    def _form_editor(
        self, path: tuple[str | int, ...], value: Any, *, checkbox_text: str = "Enabled"
    ) -> QWidget:
        editable = self._form_editable(path)
        choices = self._choices_for_path(path, value)
        if isinstance(value, bool):
            editor = CheckBox(checkbox_text)
            editor.setChecked(value)
            if self._is_source_autorange_path(path):
                editor.setText("Source AUTO (exception for other experiments)")
                editor.toggled.connect(
                    lambda checked, path=path, ed=editor: self._on_source_autorange_form_changed(checked, path, ed)
                )
            else:
                editor.toggled.connect(lambda _checked, path=path: self._form_changed(path))
        elif choices:
            editor = ComboBox()
            for label, data in choices:
                editor.addItem(label, userData=data)
            editor.setCurrentIndex(max(0, editor.findData(self._format_scalar(value))))
            if self._is_keithley_sense_mode_path(path):
                editor.currentIndexChanged.connect(
                    lambda index, path=path, ed=editor: self._on_sense_mode_choice_changed(index, path, ed)
                )
            else:
                editor.currentIndexChanged.connect(lambda _index, path=path: self._form_changed(path))
        elif isinstance(value, int) and not isinstance(value, bool):
            editor = SpinBox()
            editor.setRange(-1_000_000_000, 1_000_000_000)
            editor.setValue(value)
            editor.valueChanged.connect(lambda _number, path=path: self._form_changed(path))
        else:
            editor = LineEdit()
            editor.setText(self._format_scalar(value))
            # Free-text settings include paths, VISA resources, endpoints,
            # serial numbers and other opaque identifiers.  A digits-only
            # identifier must never be rewritten by the application-wide
            # quantity stepper.  Explicit quantities remain steppable; the
            # settings contract requires their unit to be present.
            precision_steppable = False
            if isinstance(value, float):
                # YAML floating-point scalars are dimensionless numeric
                # settings (for example NPLC) and use the written decimals.
                precision_steppable = True
            elif isinstance(value, str):
                try:
                    parse_quantity(value)
                except (QuantityError, ValueError):
                    pass
                else:
                    precision_steppable = True
            elif (
                value is None
                and path
                and str(path[-1])
                in {
                    "max",
                    "max_abs",
                    "max_expected_power_at_connector",
                    "min",
                    "minimum_internal_attenuation",
                    "nominal",
                }
            ):
                # Optional safety quantities start empty but become explicit
                # number-plus-unit values when configured.
                precision_steppable = True
            editor.setProperty("precisionArrowStepping", precision_steppable)
            editor.editingFinished.connect(lambda path=path: self._form_changed(path))
        if path == _MOKE_PATH + ("endpoint",):
            editor.setReadOnly(True)
        else:
            editor.setEnabled(editable)
        editor.setToolTip(" · ".join(str(part) for part in path))
        field = QWidget()
        field.setMinimumWidth(0)
        field.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        field_layout = QVBoxLayout(field)
        field_layout.setContentsMargins(0, 0, 0, 0)
        field_layout.setSpacing(3)
        error = BodyLabel()
        error.setObjectName("settingsFieldError")
        error.setWordWrap(True)
        error.hide()
        field_layout.addWidget(editor)
        field_layout.addWidget(error)
        self._form_editors_store[path] = editor
        self._field_errors[path] = error
        return field

    def _form_value(self, editor: QWidget, original: Any) -> Any:
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QComboBox):
            return self._parse_scalar(str(editor.currentData()), original)
        if isinstance(editor, QSpinBox):
            return editor.value()
        if isinstance(editor, QLineEdit):
            return self._parse_scalar(editor.text(), original)
        return original

    def _form_changed(self, path: tuple[str | int, ...]) -> None:
        if self._changing:
            return
        self._clear_validation_error(path)
        self._dirty = True
        if self._autosave_enabled:
            self.status.emit("Autosave pending…")

    def _add_items(
        self,
        tree: QTreeWidget,
        parent: QTreeWidgetItem | None,
        value: Any,
        path: tuple[str | int, ...],
    ) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if path == _MOKE_PATH and key in _MOKE_DERIVED_FIELDS:
                    continue
                item = QTreeWidgetItem([str(key), ""])
                if parent is None:
                    tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._add_items(tree, item, nested, path + (str(key),))
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                item = QTreeWidgetItem([f"[{index}]", ""])
                if parent is None:
                    tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._add_items(tree, item, nested, path + (index,))
            return
        item = QTreeWidgetItem(["value", self._format_scalar(value)])
        item.setData(0, Qt.ItemDataRole.UserRole, path)
        if self._form_editable(path):
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            choices = self._choices_for_path(path, value)
            if choices:
                self._install_choice_editor(tree, item, path, choices)
        else:
            item.setToolTip(
                1,
                "Read-only for the current role. General settings and safety limits "
                "require an engineer identity.",
            )
        if parent is None:
            tree.addTopLevelItem(item)
        else:
            parent.addChild(item)

    @staticmethod
    def _format_scalar(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _changed(self, item: QTreeWidgetItem, column: int) -> None:
        if (
            not self._changing
            and column == 1
            and item.data(0, Qt.ItemDataRole.UserRole) is not None
        ):
            path = tuple(item.data(0, Qt.ItemDataRole.UserRole))
            self._sync_limit_from_tree(path, item.text(1))
            editor = self._form_editors_store.get(path)
            if isinstance(editor, QLineEdit):
                editor.setText(item.text(1))
            elif isinstance(editor, QCheckBox):
                editor.setChecked(item.text(1).strip().lower() == "true")
            elif isinstance(editor, QComboBox):
                editor.setCurrentIndex(max(0, editor.findData(item.text(1))))
            self._dirty = True
            if self._autosave_enabled:
                self.status.emit("Autosave pending…")

    @staticmethod
    def _unwrap_annotation(annotation: object) -> object:
        origin = get_origin(annotation)
        if origin in (Union, UnionType):
            choices = [item for item in get_args(annotation) if item is not type(None)]
            return choices[0] if len(choices) == 1 else annotation
        return annotation

    @classmethod
    def _annotation_for_path(cls, path: tuple[str | int, ...]) -> object | None:
        annotation: object = StationSettings
        for part in path:
            annotation = cls._unwrap_annotation(annotation)
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                field = annotation.model_fields.get(str(part))
                if field is None:
                    return None
                annotation = field.annotation
                continue
            origin = get_origin(annotation)
            arguments = get_args(annotation)
            if origin is dict:
                annotation = arguments[1] if len(arguments) > 1 else Any
            elif origin in (list, tuple):
                annotation = arguments[0] if arguments else Any
            else:
                return None
        return cls._unwrap_annotation(annotation)

    @classmethod
    def _choices_for_path(
        cls, path: tuple[str | int, ...], value: object
    ) -> tuple[tuple[str, str], ...]:
        if isinstance(value, bool):
            return (("Yes", "true"), ("No", "false"))
        if cls._is_keithley_sense_mode_path(path):
            return (("2-wire (Local; required)", "2wire"),)
        annotation = cls._annotation_for_path(path)
        if annotation is not None and get_origin(annotation) is Literal:
            return tuple((str(option), str(option)) for option in get_args(annotation))
        return ()

    @staticmethod
    def _is_source_autorange_path(path: tuple[str | int, ...]) -> bool:
        return bool(path) and path[-1] == "source_autorange" and "keithley" in path and "defaults" in path

    def _confirm_source_autorange(self, path: tuple[str | int, ...]) -> bool:
        channel = next((str(part) for part in path if part in ("A", "B")), "?")
        reply = QMessageBox.warning(
            self.window(),
            f"Enable source autorange ? Channel {channel}",
            f"Channel {channel}: source AUTO is an exception for experiments requiring automatic range selection.\n\n"
            "OFF with a fixed range remains the default for MTJ measurements. Automatic source range changes "
            "can introduce switching transients and additional settling; compliance alone does not guarantee "
            "protection of a sensitive sample.\n\n"
            "After saving, manual measurements, recipes and characterization will use this channel's AUTO setting. "
            "The main control page displays the setting but cannot change it. "
            "Saving Settings does not itself enable the output.\n\n"
            "Enable source AUTO for this channel?",
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            defaultButton=QMessageBox.StandardButton.Cancel,
        )
        accepted = reply == QMessageBox.StandardButton.Yes
        if accepted:
            self._source_autorange_acknowledged.add(path)
        return accepted

    def _on_source_autorange_form_changed(
        self, checked: bool, path: tuple[str | int, ...], editor: CheckBox
    ) -> None:
        if self._changing:
            return
        if not checked:
            self._source_autorange_acknowledged.discard(path)
        if checked and not self._confirm_source_autorange(path):
            previous = editor.blockSignals(True)
            editor.setChecked(False)
            editor.blockSignals(previous)
            return
        self._form_changed(path)

    def _on_source_autorange_tree_changed(
        self, item: QTreeWidgetItem, editor: ComboBox, path: tuple[str | int, ...]
    ) -> None:
        if self._changing:
            return
        if str(editor.currentData()) != "true":
            self._source_autorange_acknowledged.discard(path)
        if str(editor.currentData()) == "true" and not self._confirm_source_autorange(path):
            previous = editor.blockSignals(True)
            editor.setCurrentIndex(editor.findData("false"))
            editor.blockSignals(previous)
            return
        item.setText(1, str(editor.currentData()))

    @staticmethod
    def _is_keithley_sense_mode_path(path: tuple[str | int, ...]) -> bool:
        if not path:
            return False
        return str(path[-1]) == "sense_mode" and "keithley" in [str(p) for p in path]

    def _on_sense_mode_choice_changed(
        self, index: int, path: tuple[str | int, ...], editor: ComboBox
    ) -> None:
        if self._changing:
            return
        if editor.currentData() != "2wire":
            QMessageBox.warning(self.window(), "Prohibited sense mode",
                                "Only 2-wire local sense is allowed. 4-wire remote sense is prohibited.")
            return
        self._form_changed(path)

    def _on_sense_mode_tree_changed(
        self, index: int, item: QTreeWidgetItem, editor: ComboBox, path: tuple[str | int, ...]
    ) -> None:
        if self._changing:
            return
        if editor.currentData() != "2wire":
            QMessageBox.warning(self.window(), "Prohibited sense mode",
                                "Only 2-wire local sense is allowed. 4-wire remote sense is prohibited.")
            return
        item.setText(1, "2wire")

    def _install_choice_editor(
        self,
        tree: QTreeWidget,
        item: QTreeWidgetItem,
        path: tuple[str | int, ...],
        choices: tuple[tuple[str, str], ...],
    ) -> None:
        editor = ComboBox(tree)
        for label, data in choices:
            editor.addItem(label, userData=data)
        index = editor.findData(self._format_scalar(self._get_path(self._raw, path)))
        editor.setCurrentIndex(max(index, 0))
        editor.setToolTip("Select a validated value from the list.")
        editor.setEnabled(bool(item.flags() & Qt.ItemFlag.ItemIsEditable))
        if self._is_source_autorange_path(path):
            editor.currentIndexChanged.connect(
                lambda _index, item=item, ed=editor, p=path: self._on_source_autorange_tree_changed(item, ed, p)
            )
        elif self._is_keithley_sense_mode_path(path):
            editor.currentIndexChanged.connect(
                lambda index, item=item, ed=editor, p=path: self._on_sense_mode_tree_changed(index, item, ed, p)
            )
        else:
            editor.currentIndexChanged.connect(
                lambda _index, item=item, editor=editor: item.setText(1, str(editor.currentData()))
            )
        tree.setItemWidget(item, 1, editor)
        self._choice_editors[path] = editor

    def _form_tab_for_path(self, path: tuple[str | int, ...]) -> str:
        if len(path) >= 2 and path[0] == "devices" and str(path[1]) in self.forms:
            return str(path[1])
        return "general"

    def editor_for_path(self, path: tuple[str | int, ...]) -> QComboBox | None:
        """Return the list editor for a settings value, when it has choices."""

        self._ensure_form_built(self._form_tab_for_path(path))
        editor = self._form_editors_store.get(path)
        if isinstance(editor, QComboBox):
            return editor
        self._ensure_trees_built()
        return self._choice_editors.get(path)

    def _apply_tree_values(self) -> dict[str, Any]:
        draft = deepcopy(self._raw)

        for path, editor in self._form_editors_store.items():
            original = self._get_path(draft, path)
            val = self._form_value(editor, original)
            self._set_path(draft, path, val)
            if (
                self._is_keithley_sense_mode_path(path)
                and len(path) >= 2
                and path[-1] == "sense_mode"
                and str(path[-2]) in ("A", "B")
            ):
                def_path = path[:-1] + ("defaults", "sense_mode")
                self._set_path(draft, def_path, val)

        if self._trees_cache is not None and not self._trees_dirty:
            def walk(item: QTreeWidgetItem) -> None:
                path = item.data(0, Qt.ItemDataRole.UserRole)
                if path is not None:
                    if tuple(path) in self._form_editors_store:
                        return
                    original = self._get_path(draft, tuple(path))
                    val = self._parse_scalar(item.text(1), original)
                    self._set_path(draft, tuple(path), val)
                    if (
                        self._is_keithley_sense_mode_path(tuple(path))
                        and len(path) >= 2
                        and path[-1] == "sense_mode"
                        and str(path[-2]) in ("A", "B")
                    ):
                        def_path = tuple(path[:-1]) + ("defaults", "sense_mode")
                        self._set_path(draft, def_path, val)
                for index in range(item.childCount()):
                    walk(item.child(index))

            for tree in self._trees_cache.values():
                for index in range(tree.topLevelItemCount()):
                    walk(tree.topLevelItem(index))

        if self._limits_table_widget is not None and not self._limits_table_dirty:
            for row in range(self._limits_table_widget.rowCount()):
                for column in (2, 3):
                    item = self._limits_table_widget.item(row, column)
                    path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
                    if path is None:
                        continue
                    original = self._get_path(draft, tuple(path))
                    self._set_path(draft, tuple(path), self._parse_scalar(item.text(), original))
        else:
            for record in self._limit_rows:
                min_original = self._get_path(draft, record.min_path)
                self._set_path(draft, record.min_path, self._parse_scalar(record.min_text, min_original))
                if record.max_path is not None:
                    max_original = self._get_path(draft, record.max_path)
                    self._set_path(draft, record.max_path, self._parse_scalar(record.max_text, max_original))
        return draft

    def _collect_limit_rows(self) -> None:
        self._limit_rows.clear()
        self._limit_records_by_path.clear()

        def walk(value: Any, path: tuple[str | int, ...]) -> None:
            if not isinstance(value, dict):
                return
            if (
                "min" in value
                and "max" in value
                and path != ("devices", "anritsu", "safety", "reference_level")
            ):
                self._add_limit_record(path, value)
            for key, nested in value.items():
                if isinstance(nested, dict):
                    walk(nested, path + (str(key),))
                elif (
                    key in {"max_abs_power", "combined_voltage_limit"}
                    and path[-1:] == ("lab_limits",)
                    and isinstance(nested, str)
                ):
                    self._add_scalar_limit_record(path + (str(key),), nested)

        devices = self._raw.get("devices", {})
        if isinstance(devices, dict):
            for device, nested in devices.items():
                walk(nested, ("devices", str(device)))

    def _populate_limits(self) -> None:
        self._collect_limit_rows()
        if self._limits_table_widget is not None:
            self._populate_limits_table()
        self._limits_cards_built = True
        self._populate_safety_limit_cards()

    def _populate_safety_limit_cards(self) -> None:
        """Build the operator-facing safety editor from the canonical limit rows."""

        self._safety_limit_editors_store.clear()
        self._safety_limit_error_labels.clear()
        content = QWidget()
        content.setMinimumWidth(0)
        content.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)
        content_layout.setSpacing(14)
        device_layouts: dict[str, QVBoxLayout] = {}

        for record in self._limit_rows:
            min_path = record.min_path
            max_path = record.max_path
            if not min_path:
                continue
            device = str(min_path[1]).capitalize() if len(min_path) > 1 else "Device"
            if device not in device_layouts:
                card = CardWidget(self)
                card.setObjectName("safetyDeviceCard")
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(18, 16, 18, 16)
                card_layout.setSpacing(10)
                heading = BodyLabel(device)
                heading.setObjectName("safetyDeviceTitle")
                card_layout.addWidget(heading)
                content_layout.addWidget(card)
                device_layouts[device] = card_layout

            row_widget = CardWidget(self)
            row_widget.setObjectName("safetyLimitRow")
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(12, 10, 12, 10)
            row_layout.setSpacing(7)
            values_layout = QHBoxLayout()
            values_layout.setSpacing(10)
            label_text = record.parameter or "Limit"
            if record.scope and record.scope != device.lower():
                label_text = f"{label_text}  ·  {record.scope}"
            label = BodyLabel(label_text)
            label.setObjectName("safetyLimitLabel")
            label.setMinimumWidth(250)
            values_layout.addWidget(label, 1)
            scalar_limit = max_path is None
            values_layout.addWidget(self._safety_boundary_label("LIMIT" if scalar_limit else "MIN"))
            minimum_editor = self._make_safety_limit_editor(min_path, record.min_text)
            values_layout.addWidget(minimum_editor)
            maximum_editor: QLineEdit | None = None
            if not scalar_limit and max_path is not None:
                values_layout.addWidget(self._safety_boundary_label("MAX"))
                maximum_editor = self._make_safety_limit_editor(max_path, record.max_text)
                values_layout.addWidget(maximum_editor)
            unit = BodyLabel(record.unit or "SI / explicit")
            unit.setObjectName("safetyLimitUnit")
            unit.setMinimumWidth(84)
            values_layout.addWidget(unit)
            enabled_path = self._limit_enabled_path(min_path, scalar_limit)
            enabled = self._limit_enabled_value(enabled_path)
            disable_limit = CheckBox("Disable limit", row_widget)
            disable_limit.setProperty("limitEnabledPath", enabled_path)
            disable_limit.setChecked(not enabled)
            disable_limit.setEnabled(not self._read_only)
            disable_limit.setToolTip(
                "Ignore this station-profile limit. Immutable hardware limits remain active."
            )
            disable_limit.toggled.connect(
                lambda disabled, p=enabled_path, row=row_widget, editors=(minimum_editor, maximum_editor): (
                    self._set_limit_disabled(p, disabled, row, editors)
                )
            )
            values_layout.addWidget(disable_limit)
            minimum_editor.setEnabled(enabled and not self._read_only)
            if maximum_editor is not None:
                maximum_editor.setEnabled(enabled and not self._read_only)
            row_widget.setProperty("limitDisabled", not enabled)
            row_layout.addLayout(values_layout)
            error = BodyLabel()
            error.setObjectName("settingsFieldError")
            error.setWordWrap(True)
            active_err = record.min_error or record.max_error
            if active_err:
                error.setText(active_err)
                error.show()
            else:
                error.hide()
            row_layout.addWidget(error)
            self._safety_limit_error_labels[min_path] = error
            if record.min_error:
                self._set_validation_state(minimum_editor, "error")
                minimum_editor.setToolTip(f"Validation error: {record.min_error}")
            if max_path is not None:
                self._safety_limit_error_labels[max_path] = error
                if maximum_editor is not None and record.max_error:
                    self._set_validation_state(maximum_editor, "error")
                    maximum_editor.setToolTip(f"Validation error: {record.max_error}")
            device_layouts[device].addWidget(row_widget)

        content_layout.addStretch(1)
        self.limits_scroll.setWidget(content)

    @staticmethod
    def _limit_enabled_path(
        value_path: tuple[str | int, ...], scalar_limit: bool
    ) -> tuple[str | int, ...]:
        if scalar_limit:
            return value_path[:-1] + (f"{value_path[-1]}_enabled",)
        return value_path[:-1] + ("enabled",)

    def _limit_enabled_value(self, path: tuple[str | int, ...]) -> bool:
        try:
            return bool(self._get_path(self._raw, path))
        except (KeyError, IndexError, TypeError):
            return True

    def _set_limit_disabled(
        self,
        path: tuple[str | int, ...],
        disabled: bool,
        row: QWidget,
        editors: tuple[QLineEdit, QLineEdit | None],
    ) -> None:
        self._set_path(self._raw, path, not disabled)
        for editor in editors:
            if editor is not None:
                editor.setEnabled(not disabled and not self._read_only)
        set_dynamic_property(row, "limitDisabled", disabled)
        self._dirty = True
        self.status.emit(
            f"Safety limit {'disabled' if disabled else 'enabled'} in the draft. "
            "Use Save changes to persist it."
        )

    @staticmethod
    def _safety_boundary_label(text: str) -> QLabel:
        label = BodyLabel(text)
        label.setObjectName("safetyLimitTag")
        return label

    def _make_safety_limit_editor(self, path: tuple[str | int, ...], value: str) -> QLineEdit:
        editor = LineEdit()
        editor.setText(value)
        editor.setObjectName("safetyLimitInput")
        editor.setMinimumWidth(156)
        editor.setReadOnly(self._read_only)
        editor.setProperty("limitPath", path)
        editor.editingFinished.connect(
            lambda p=path, e=editor: self._commit_safety_limit_editor(p, e)
        )
        self._safety_limit_editors_store[path] = editor
        return editor

    def _commit_safety_limit_editor(self, path: tuple[str | int, ...], editor: QLineEdit) -> None:
        if self._is_keithley_limit_path(path):
            self._commit_keithley_safety_limit_editor(path, editor)
            return
        if self._limits_table_widget is not None and not self._limits_table_dirty:
            item = self._limit_items_by_path_store.get(path)
            if item is not None and item.text() != editor.text():
                item.setText(editor.text())
            return
        entry = self._limit_records_by_path.get(path)
        if entry is None:
            return
        record, side = entry
        current_text = record.min_text if side == "min" else record.max_text
        if current_text == editor.text():
            return
        if side == "min":
            record.min_text = editor.text()
        else:
            record.max_text = editor.text()
        self._validate_limit_record(record)
        if self._has_limit_errors():
            self._show_limit_validation_toast(
                "Incorrect value or unit. Correct the red outlined field before saving."
            )
        else:
            self._limit_validation_toast_message = None
        self._sync_tree_from_limit(path, editor.text())
        self._dirty = True
        if self._autosave_enabled:
            self.status.emit("Safety-limit autosave pending…")

    @staticmethod
    def _is_keithley_limit_path(path: tuple[str | int, ...]) -> bool:
        return (
            len(path) >= 7
            and path[:3] == ("devices", "keithley", "safety")
            and "lab_limits" in path
        )

    def _commit_keithley_safety_limit_editor(
        self, path: tuple[str | int, ...], editor: QLineEdit
    ) -> None:
        try:
            draft = self._apply_tree_values()
            original = self._get_path(draft, path)
            primary_value = self._parse_scalar(editor.text(), original)
            self._set_path(draft, path, primary_value)
            limit_index = path.index("lab_limits")
            channel_limits = self._get_path(draft, path[: limit_index + 1])
            if not isinstance(channel_limits, dict):
                raise ConfigurationError("Keithley channel limits are not configured.")
            relative_path = tuple(str(part) for part in path[limit_index + 1 :])
            proposal = propose_keithley_limit_adjustments(
                channel_limits, relative_path, str(primary_value)
            )
            if proposal.adjustments:
                confirmation = KeithleyLimitProposalDialog(proposal, self)
                if confirmation.exec() != QDialog.DialogCode.Accepted:
                    editor.setText(str(self._get_path(self._raw, path)))
                    return
                for adjustment in proposal.adjustments:
                    target: Any = channel_limits
                    for part in adjustment.path[:-1]:
                        target = target[part]
                    target[adjustment.path[-1]] = adjustment.proposed
            settings = StationSettings.model_validate(draft)
        except (ConfigurationError, ValueError, KeyError, TypeError) as exc:
            self._set_limit_path_validation(path, str(exc))
            editor.setText(str(self._get_path(self._raw, path)))
            return
        changed_paths = {path}
        prefix = path[: limit_index + 1]
        changed_paths.update(prefix + adjustment.path for adjustment in proposal.adjustments)
        self.stage_limit_snapshot(settings, draft, changed_paths)

    def _populate_roles(self) -> None:
        self.role_table.setRowCount(0)
        access = self._raw.get("access_control", {})
        assignments = access.get("user_roles", {}) if isinstance(access, dict) else {}
        if not isinstance(assignments, dict):
            return
        for username, roles in sorted(
            assignments.items(), key=lambda item: str(item[0]).casefold()
        ):
            row = self.role_table.rowCount()
            self.role_table.insertRow(row)
            self.role_table.setItem(row, 0, QTableWidgetItem(str(username)))
            role_text = (
                ", ".join(str(role) for role in roles) if isinstance(roles, list) else str(roles)
            )
            self.role_table.setItem(row, 1, QTableWidgetItem(role_text))

    def _role_choices(self) -> tuple[str, ...]:
        if any(role.value == "service" for role in self._access.identity.roles):
            return ("operator", "engineer", "service")
        return ("operator", "engineer")

    def _choose_roles(
        self, *, username: str = "", roles: tuple[str, ...] = ()
    ) -> tuple[str, tuple[str, ...]] | None:
        dialog = StationDialog(self)
        dialog.setWindowTitle("User access role")
        dialog.setMinimumWidth(420)
        surface = dialog.use_modal_shell_content().surface
        layout = dialog.modal_content_layout(spacing=10)
        note = BodyLabel("Assign one or more station roles to an operating-system account.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(BodyLabel("Operating-system account"))
        account = LineEdit(surface)
        # Account names are identifiers even when an installation happens to
        # use digits only; Up/Down must not rewrite them as numeric values.
        account.setProperty("precisionArrowStepping", False)
        account.setText(username)
        account.setPlaceholderText("DOMAIN\\user")
        layout.addWidget(account)
        layout.addWidget(BodyLabel("Roles"))
        checkboxes: list[QCheckBox] = []
        for role in self._role_choices():
            check = CheckBox(role.capitalize(), surface)
            check.setProperty("role", role)
            check.setChecked(role in roles)
            layout.addWidget(check)
            checkboxes.append(check)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", surface)
        save = PrimaryPushButton("Save roles", surface)
        cancel.clicked.connect(dialog.reject)
        save.clicked.connect(dialog.accept)
        footer.addWidget(cancel)
        footer.addWidget(save)
        layout.addLayout(footer)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        selected = tuple(str(check.property("role")) for check in checkboxes if check.isChecked())
        normalized = account.text().strip()
        if not normalized or not selected:
            QMessageBox.warning(
                self, "User role", "Enter an account name and select at least one role."
            )
            return None
        return normalized, selected

    def _add_role_assignment(self) -> None:
        if not self._require_access(Permission.MANAGE_ROLES, "adding an OS role assignment"):
            return
        selection = self._choose_roles()
        if selection is None:
            return
        self._upsert_role_assignment(*selection)

    def _edit_role_assignment(self) -> None:
        if not self._require_access(Permission.MANAGE_ROLES, "editing an OS role assignment"):
            return
        row = self.role_table.currentRow()
        account = self.role_table.item(row, 0) if row >= 0 else None
        assigned = self.role_table.item(row, 1) if row >= 0 else None
        if account is None or assigned is None:
            return
        selection = self._choose_roles(
            username=account.text(),
            roles=tuple(part.strip() for part in assigned.text().split(",") if part.strip()),
        )
        if selection is None:
            return
        assignments = self._raw.setdefault("access_control", {}).setdefault("user_roles", {})
        assignments.pop(account.text(), None)
        self._upsert_role_assignment(*selection)

    def _upsert_role_assignment(self, username: str, roles: tuple[str, ...]) -> None:
        if "service" in roles and not any(
            role.value == "service" for role in self._access.identity.roles
        ):
            QMessageBox.warning(
                self,
                "Access denied",
                "Only a service identity can grant the service role.",
            )
            return
        assignments = self._raw.setdefault("access_control", {}).setdefault("user_roles", {})
        normalized = AccessPolicy.normalize_username(username)
        existing = next(
            (
                candidate
                for candidate in assignments
                if AccessPolicy.normalize_username(str(candidate)) == normalized
            ),
            None,
        )
        if existing is not None:
            answer = QMessageBox.question(
                self,
                "Replace role assignment",
                f"Replace the existing assignment for {existing}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            del assignments[existing]
        assignments[username] = list(roles)
        self._dirty = True
        self._populate()
        self.status.emit(
            "Role assignment changed in the draft; Save changes and restart the application to apply it."
        )

    def _remove_role_assignment(self) -> None:
        if not self._require_access(Permission.MANAGE_ROLES, "removing an OS role assignment"):
            return
        row = self.role_table.currentRow()
        if row < 0:
            return
        username_item = self.role_table.item(row, 0)
        if username_item is None:
            return
        username = username_item.text()
        answer = QMessageBox.question(
            self,
            "Remove role assignment",
            f"Remove the explicit role assignment for {username}? The account will fall back to operator.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        assignments = self._raw.get("access_control", {}).get("user_roles", {})
        assignments.pop(username, None)
        self._dirty = True
        self._populate()
        self.status.emit(
            "Role assignment removed from the draft; Save changes and restart the application to apply it."
        )

    def _add_limit_record(self, path: tuple[str | int, ...], value: dict[str, Any]) -> None:
        scope_parts = [
            str(part) for part in path[1:-1] if str(part) not in {"safety", "lab_limits"}
        ]
        scope = " / ".join(scope_parts) or str(path[1])
        parameter_key = str(path[-1])
        parameter = _SAFETY_LIMIT_LABELS.get(
            parameter_key, parameter_key.replace("_", " ").capitalize()
        )
        minimum = self._format_scalar(value.get("min"))
        maximum = self._format_scalar(value.get("max"))
        unit = self._range_unit(minimum, maximum)
        default = self._default_for_limit(path)
        min_path = path + ("min",)
        max_path = path + ("max",)
        record = _LimitRowRecord(
            scope=scope,
            parameter=parameter,
            min_path=min_path,
            max_path=max_path,
            min_text=minimum,
            max_text=maximum,
            unit=unit,
            default_text=default,
        )
        self._limit_rows.append(record)
        self._limit_records_by_path[min_path] = (record, "min")
        self._limit_records_by_path[max_path] = (record, "max")

    def _add_scalar_limit_record(self, path: tuple[str | int, ...], value: str) -> None:
        """Add a one-value safety limit such as maximum allowed power."""

        scope_parts = [
            str(part) for part in path[1:-1] if str(part) not in {"safety", "lab_limits"}
        ]
        scope = " / ".join(scope_parts) or str(path[1])
        parameter_key = str(path[-1])
        parameter = _SAFETY_LIMIT_LABELS.get(
            parameter_key, parameter_key.replace("_", " ").capitalize()
        )
        record = _LimitRowRecord(
            scope=scope,
            parameter=parameter,
            min_path=path,
            max_path=None,
            min_text=value,
            max_text="—",
            unit="explicit unit",
            default_text="—",
        )
        self._limit_rows.append(record)
        self._limit_records_by_path[path] = (record, "min")

    @staticmethod
    def _range_unit(minimum: str, maximum: str) -> str:
        units: list[str] = []
        for text in (minimum, maximum):
            parts = text.strip().split()
            if len(parts) > 1:
                units.append(parts[-1])
        return units[0] if units and all(unit == units[0] for unit in units) else "SI / explicit"

    def _default_for_limit(self, path: tuple[str | int, ...]) -> str:
        if "lab_limits" not in path:
            return "—"
        index = path.index("lab_limits")
        try:
            owner = self._get_path(self._raw, path[:index])
        except (KeyError, IndexError, TypeError):
            return "—"
        defaults = owner.get("defaults", {}) if isinstance(owner, dict) else {}
        value = defaults.get(path[-1]) if isinstance(defaults, dict) else None
        return self._format_scalar(value) if value is not None else "—"

    @staticmethod
    def _limit_dimension(path: tuple[str | int, ...]) -> str | None:
        leaf = str(path[-1]) if path else ""
        parameter = str(path[-2]) if len(path) >= 2 and leaf in {"min", "max", "max_abs"} else leaf
        exact = {
            "frequency": DIMENSION_FREQUENCY,
            "reference_level": DIMENSION_DBM,
            "point_settle_time": DIMENSION_TIME,
            "max_expected_power_at_connector": DIMENSION_DBM,
            "external_attenuation": DIMENSION_DB,
            "minimum_internal_attenuation": DIMENSION_DB,
            "minimum_impedance": DIMENSION_RESISTANCE,
            "max_abs_power": DIMENSION_POWER,
            "estimated_load_power": DIMENSION_POWER,
            "source_current": DIMENSION_CURRENT,
            "current_compliance": DIMENSION_CURRENT,
            "voltage_compliance": DIMENSION_VOLTAGE,
            "source_voltage": DIMENSION_VOLTAGE,
            "measured_current_trip": DIMENSION_CURRENT,
            "measured_voltage_trip": DIMENSION_VOLTAGE,
            "estimated_load_current": DIMENSION_CURRENT,
            "settle_time": DIMENSION_TIME,
            "modulation_rate": DIMENSION_FREQUENCY,
            "sweep_duration": DIMENSION_TIME,
            "burst_period": DIMENSION_TIME,
        }
        if parameter in exact:
            return exact[parameter]
        if "current" in parameter:
            return DIMENSION_CURRENT
        if "voltage" in parameter or parameter in {
            "high_level",
            "low_level",
            "offset",
            "amplitude_vpp",
        }:
            return DIMENSION_VOLTAGE
        if "frequency" in parameter:
            return DIMENSION_FREQUENCY
        if "power" in parameter:
            return DIMENSION_POWER
        if "time" in parameter or "settle" in parameter or "delay" in parameter:
            return DIMENSION_TIME
        return None

    @staticmethod
    def _dimension_unit_hint(dimension: str | None) -> str:
        return {
            DIMENSION_POWER: "a power unit (W, mW, uW or nW)",
            DIMENSION_CURRENT: "a current unit (A, mA, uA or nA)",
            DIMENSION_VOLTAGE: "a voltage unit (V, mV or uV)",
            DIMENSION_FREQUENCY: "a frequency unit (Hz, kHz, MHz or GHz)",
            DIMENSION_TIME: "a time unit (s, ms or us)",
            DIMENSION_RESISTANCE: "a resistance unit (ohm, kohm or Mohm)",
            DIMENSION_DBM: "dBm",
            DIMENSION_DB: "dB",
        }.get(dimension, "a number")

    @staticmethod
    def _is_empty_limit_value(text: str) -> bool:
        return text.strip().lower() in {"", "null", "none"}

    def _has_limit_errors(self) -> bool:
        return bool(self._limit_error_items) or any(
            record.min_error or record.max_error for record in self._limit_rows
        )

    def _validate_limit_record(self, record: _LimitRowRecord) -> None:
        dimension = self._limit_dimension(record.min_path)
        if record.max_path is None:
            self._set_limit_path_validation(record.min_path, None)
            try:
                if dimension is None:
                    float(record.min_text.strip().replace(",", "."))
                else:
                    value = parse_quantity(record.min_text, dimension, require_unit=True).si_value
                    if value <= 0:
                        raise ValueError("Value must be greater than zero.")
            except (QuantityError, ValueError) as exc:
                expected = f" Enter {self._dimension_unit_hint(dimension)}."
                self._set_limit_path_validation(
                    record.min_path, f"Invalid limit value:{expected} {exc}"
                )
            return
        values: dict[str, float] = {}
        for path, text, boundary in (
            (record.min_path, record.min_text, "minimum"),
            (record.max_path, record.max_text, "maximum"),
        ):
            self._set_limit_path_validation(path, None)
            if self._is_empty_limit_value(text):
                continue
            try:
                if dimension is None:
                    float(text.strip().replace(",", "."))
                else:
                    values[boundary] = parse_quantity(
                        text, dimension, require_unit=True
                    ).si_value
            except (QuantityError, ValueError) as exc:
                expected = f" Enter {self._dimension_unit_hint(dimension)}."
                self._set_limit_path_validation(
                    path, f"Invalid {boundary} value:{expected} {exc}"
                )
        if len(values) == 2 and values["minimum"] > values["maximum"]:
            message = "Minimum cannot exceed maximum."
            self._set_limit_path_validation(record.min_path, message)
            self._set_limit_path_validation(record.max_path, message)

    def _validate_limit_row(self, row: int) -> None:
        if 0 <= row < len(self._limit_rows):
            record = self._limit_rows[row]
            if self._limits_table_widget is not None:
                minimum = self._limits_table_widget.item(row, 2)
                maximum = self._limits_table_widget.item(row, 3)
                if minimum is not None:
                    record.min_text = minimum.text()
                if maximum is not None and record.max_path is not None:
                    record.max_text = maximum.text()
            self._validate_limit_record(record)
            return
        if self._limits_table_widget is None:
            return
        minimum = self._limits_table_widget.item(row, 2)
        maximum = self._limits_table_widget.item(row, 3)
        if minimum is None or maximum is None:
            return
        path = minimum.data(Qt.ItemDataRole.UserRole)
        if path is None:
            return
        entry = self._limit_records_by_path.get(tuple(path))
        if entry is not None:
            self._validate_limit_record(entry[0])

    def _limit_changed(self, item: QTableWidgetItem) -> None:
        if self._changing or item.column() not in {2, 3}:
            return
        if item in self._limit_error_items:
            self._set_limit_validation(item, None)
        path = item.data(Qt.ItemDataRole.UserRole)
        if path is None:
            return
        path_tuple = tuple(path)
        entry = self._limit_records_by_path.get(path_tuple)
        if entry is not None:
            record, side = entry
            if side == "min":
                record.min_text = item.text()
            else:
                record.max_text = item.text()
        safety_editor = self._safety_limit_editors_store.get(path_tuple)
        if safety_editor is not None and safety_editor.text() != item.text():
            safety_editor.setText(item.text())
        self._validate_limit_row(item.row())
        if self._has_limit_errors():
            self._show_limit_validation_toast(
                "Incorrect value or unit. Correct the red outlined field before saving."
            )
        else:
            self._limit_validation_toast_message = None
        self._sync_tree_from_limit(path_tuple, item.text())
        self._dirty = True
        if self._autosave_enabled:
            self.status.emit("Safety-limit autosave pending…")

    def _sync_tree_from_limit(self, path: tuple[str | int, ...], text: str) -> None:
        self._changing = True
        try:
            editor = self._form_editors_store.get(path)
            if isinstance(editor, QLineEdit):
                editor.setText(text)
            if self._trees_cache is not None and not self._trees_dirty:
                for tree in self._trees_cache.values():
                    iterator = QTreeWidgetItemIterator(tree)
                    while iterator.value() is not None:
                        candidate = iterator.value()
                        if tuple(candidate.data(0, Qt.ItemDataRole.UserRole) or ()) == path:
                            candidate.setText(1, text)
                            return
                        iterator += 1
        finally:
            self._changing = False

    def _sync_limit_from_tree(self, path: tuple[str | int, ...], text: str) -> None:
        self._changing = True
        try:
            entry = self._limit_records_by_path.get(path)
            if entry is not None:
                record, side = entry
                if side == "min":
                    record.min_text = text
                else:
                    record.max_text = text
            safety_editor = self._safety_limit_editors_store.get(path)
            if safety_editor is not None and safety_editor.text() != text:
                safety_editor.setText(text)
            if self._limits_table_widget is not None and not self._limits_table_dirty:
                item = self._limit_items_by_path_store.get(path)
                if item is not None and item.text() != text:
                    item.setText(text)
        finally:
            self._changing = False

    @staticmethod
    def _get_path(data: dict[str, Any], path: tuple[str | int, ...]) -> Any:
        current: Any = data
        for part in path:
            current = current[part]
        return current

    @staticmethod
    def _set_path(data: dict[str, Any], path: tuple[str | int, ...], value: Any) -> None:
        current: Any = data
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = value

    @staticmethod
    def _parse_scalar(text: str, original: Any) -> Any:
        value = text.strip()
        if original is None:
            return None if value.lower() in {"", "null", "none"} else value
        if isinstance(original, bool):
            if value.lower() not in {"true", "false"}:
                raise ConfigurationError("A Boolean value must be true or false.")
            return value.lower() == "true"
        if isinstance(original, int) and not isinstance(original, bool):
            return int(value)
        if isinstance(original, float):
            return float(value.replace(",", "."))
        return value

    @staticmethod
    def _set_validation_state(widget: QWidget, state: str) -> None:
        if (widget.property("validationState") or "") == state:
            return
        set_dynamic_property(widget, "validationState", state)

    def _clear_validation_error(self, path: tuple[str | int, ...]) -> None:
        editor = self._form_editors_store.get(path)
        message = self._field_errors.get(path)
        if editor is not None:
            self._set_validation_state(editor, "")
        if message is not None:
            message.clear()
            message.hide()

    def _clear_validation_errors(self) -> None:
        for path in tuple(self._form_editors_store):
            self._clear_validation_error(path)
        for record in self._limit_rows:
            if record.min_error:
                self._set_limit_path_validation(record.min_path, None)
            if record.max_path is not None and record.max_error:
                self._set_limit_path_validation(record.max_path, None)
        for item in tuple(self._limit_error_items):
            self._set_limit_validation(item, None)
        self._limit_validation_toast_message = None

    def _show_limit_validation_toast(self, message: str) -> None:
        """Announce a new limit-validation problem without reflowing the tab."""

        if message == self._limit_validation_toast_message:
            return
        self._limit_validation_toast_message = message
        show_toast(
            self,
            message,
            severity="error",
            timeout_ms=8_000,
            title="Station safety boundaries",
        )

    def _set_limit_path_validation(
        self, path: tuple[str | int, ...], message: str | None
    ) -> None:
        entry = self._limit_records_by_path.get(path)
        if entry is not None:
            record, side = entry
            if side == "min":
                record.min_error = message
            else:
                record.max_error = message
        if self._limits_table_widget is not None and not self._limits_table_dirty:
            item = self._limit_items_by_path_store.get(path)
            if item is not None:
                self._set_limit_validation(item, message)
                return
        self._sync_safety_limit_validation_path(path, message)

    def _set_limit_validation(
        self, item: QTableWidgetItem | None, message: str | None
    ) -> None:
        """Update error metadata without recursively re-entering itemChanged."""
        if item is None:
            return
        path = tuple(item.data(Qt.ItemDataRole.UserRole) or ())
        if path:
            entry = self._limit_records_by_path.get(path)
            if entry is not None:
                record, side = entry
                if side == "min":
                    record.min_error = message
                else:
                    record.max_error = message
        previous_changing = self._changing
        self._changing = True
        try:
            if item in self._limit_error_items:
                self._limit_error_items.remove(item)
            item.setData(_LIMIT_VALIDATION_MESSAGE_ROLE, message)
            item.setToolTip(f"Validation error: {message}" if message else "")
            if message:
                self._limit_error_items.append(item)
        finally:
            self._changing = previous_changing
        self._sync_safety_limit_validation(item)

    def _sync_safety_limit_validation_path(
        self, path: tuple[str | int, ...], message: str | None
    ) -> None:
        if not path:
            return
        editor = self._safety_limit_editors_store.get(path)
        if editor is not None:
            self._set_validation_state(editor, "error" if message else "")
            editor.setToolTip(f"Validation error: {message}" if message else "")
        label = self._safety_limit_error_labels.get(path)
        if label is not None:
            entry = self._limit_records_by_path.get(path)
            active_message: str | None = None
            if entry is not None:
                record, _side = entry
                active_message = record.min_error or record.max_error
            else:
                active_message = message
            label.setText(str(active_message or ""))
            label.setVisible(bool(active_message))

    def _sync_safety_limit_validation(self, item: QTableWidgetItem) -> None:
        path = tuple(item.data(Qt.ItemDataRole.UserRole) or ())
        if not path:
            return
        self._sync_safety_limit_validation_path(
            path, item.data(_LIMIT_VALIDATION_MESSAGE_ROLE)
        )

    def _mark_invalid_path(self, path: tuple[str | int, ...], message: str) -> QWidget | None:
        self._ensure_form_built(self._form_tab_for_path(path))
        candidates = [
            candidate
            for candidate in self._form_editors_store
            if (len(candidate) <= len(path) and tuple(path[: len(candidate)]) == candidate)
            or (len(path) <= len(candidate) and tuple(candidate[: len(path)]) == path)
        ]
        marked_editor: QWidget | None = None
        if candidates:
            longest = max(len(candidate) for candidate in candidates)
            for matched in (candidate for candidate in candidates if len(candidate) == longest):
                editor = self._form_editors_store[matched]
                self._set_validation_state(editor, "error")
                label = self._field_errors[matched]
                label.setText(message)
                label.show()
                if marked_editor is None:
                    marked_editor = editor
        for limit_path in tuple(self._limit_records_by_path):
            if (
                tuple(path[: len(limit_path)]) == limit_path
                or tuple(limit_path[: len(path)]) == path
            ):
                self._set_limit_path_validation(limit_path, message)
        return marked_editor

    def reveal_settings_issues(self, paths: tuple[SettingsPath, ...], message: str) -> None:
        """Open and visibly mark fields implicated by a rejected safety action."""

        if not paths:
            return
        first_path = paths[0]
        device = (
            str(first_path[1]) if len(first_path) > 1 and first_path[0] == "devices" else "general"
        )
        form = self.forms.get(device)
        if form is not None:
            self._ensure_form_built(device)
            self.tabs.setCurrentWidget(form)
        first_editor: QWidget | None = None
        for path in paths:
            editor = self._mark_invalid_path(path, message)
            if first_editor is None and editor is not None:
                first_editor = editor
        if first_editor is not None:

            def focus_issue() -> None:
                if form is not None:
                    form.ensureWidgetVisible(first_editor, 36, 72)
                first_editor.setFocus()

            QTimer.singleShot(0, focus_issue)
        self.status.emit("Settings fields requiring correction were highlighted")

    @staticmethod
    def _missing_range_boundaries(
        value: Any, path: tuple[str | int, ...] = ()
    ) -> tuple[tuple[str | int, ...], ...]:
        missing: list[tuple[str | int, ...]] = []
        if isinstance(value, dict):
            if "min" in value and "max" in value:
                for boundary in ("min", "max"):
                    if value[boundary] is None:
                        missing.append(path + (boundary,))
            for key, nested in value.items():
                if isinstance(nested, (dict, list)):
                    missing.extend(
                        SettingsPage._missing_range_boundaries(nested, path + (str(key),))
                    )
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                missing.extend(SettingsPage._missing_range_boundaries(nested, path + (index,)))
        return tuple(missing)

    def _semantic_error_paths(
        self, error: Exception, draft: dict[str, Any]
    ) -> tuple[tuple[str | int, ...], ...]:
        message = str(error).lower()
        anritsu_safety = ("devices", "anritsu", "safety")
        if "anritsu acquisition requires a complete frequency limit" in message:
            return tuple(anritsu_safety + ("frequency", boundary) for boundary in ("min", "max"))
        if "optional range must define both limits" in message:
            return self._missing_range_boundaries(draft)
        if (
            "signal generator output permission requires complete frequency and power limits"
            in message
        ):
            return tuple(
                ("devices", "anritsu", "signal_generator", range_name, boundary)
                for range_name in ("frequency", "power")
                for boundary in ("min", "max")
            )
        return ()

    def _show_validation_errors(
        self, error: Exception, draft: dict[str, Any] | None = None
    ) -> None:
        previous_changing = self._changing
        self._changing = True
        try:
            self._clear_validation_errors()
            details = error.errors() if isinstance(error, ValidationError) else []
            first_editor: QWidget | None = None
            for detail in details:
                location = tuple(detail.get("loc", ()))
                message = str(detail.get("msg", "Invalid value."))
                editor = self._mark_invalid_path(location, message)
                if first_editor is None and editor is not None:
                    first_editor = editor
            if draft is not None:
                for path in self._semantic_error_paths(error, draft):
                    editor = self._mark_invalid_path(path, str(error))
                    if first_editor is None and editor is not None:
                        first_editor = editor
            if first_editor is None and draft is not None and not self._has_limit_errors():
                for path in self._changed_leaf_paths(self._persisted_raw, draft):
                    editor = self._mark_invalid_path(path, str(error))
                    if first_editor is None and editor is not None:
                        first_editor = editor
            if self._has_limit_errors():
                self._show_limit_validation_toast(
                    "Correct the highlighted safety-limit fields before validating again."
                )
                self._ensure_limits_cards_built()
                self.tabs.setCurrentWidget(self.limits_page)
                first_limit_path: tuple[str | int, ...] | None = None
                if self._limit_error_items:
                    first_limit_path = tuple(
                        self._limit_error_items[0].data(Qt.ItemDataRole.UserRole) or ()
                    )
                else:
                    for record in self._limit_rows:
                        if record.min_error:
                            first_limit_path = record.min_path
                            break
                        if record.max_path is not None and record.max_error:
                            first_limit_path = record.max_path
                            break
                editor = (
                    self._safety_limit_editors_store.get(first_limit_path)
                    if first_limit_path
                    else None
                )
                if editor is not None:
                    self.limits_scroll.ensureWidgetVisible(editor, 36, 72)
                    editor.setFocus()
            elif first_editor is not None:
                first_editor.setFocus()
                first_editor.ensurePolished()
                first_editor.scroll(0, 0)
        finally:
            self._changing = previous_changing

    def _validation_failed(
        self, title: str, error: Exception, draft: dict[str, Any] | None = None
    ) -> None:
        self._show_validation_errors(error, draft)
        QMessageBox.critical(self, title, format_settings_validation_error(error))

    @staticmethod
    def _sync_moke_vout_permissions(draft: dict[str, Any]) -> None:
        """Keep the persisted channel lists in step with the editable approvals."""
        moke = draft["devices"]["moke_box"]
        allowed: list[int] = []
        tests: list[int] = []
        if moke["allow_vout_control"]:
            allowed.append(moke["voltage_control"]["channel"])
            for key in sorted(moke["channel_profiles"]):
                profile = moke["channel_profiles"][key]
                if profile["approved"]:
                    allowed.append(profile["channel"])
                    if profile["kepco_mode"] == "dac_test":
                        tests.append(profile["channel"])
        for key, eligible in (("allowed_vout_channels", allowed), ("test_vout_channels", tests)):
            current = moke[key]
            # Preserve the order in which already permitted outputs are shut down.
            moke[key] = [channel for channel in current if channel in eligible]
            moke[key].extend(channel for channel in eligible if channel not in current)

    def validate_draft(self) -> StationSettings | None:
        try:
            draft = self._apply_tree_values()
            self._sync_moke_vout_permissions(draft)
            settings = StationSettings.model_validate(draft)
        except (ConfigurationError, ValueError) as exc:
            self._validation_failed("Validation error", exc, locals().get("draft"))
            return None
        self._clear_validation_errors()
        QMessageBox.information(self, "Validation", "The configuration is valid.")
        self.status.emit("Configuration validation passed")
        return settings

    def save_draft(
        self,
        *,
        silent: bool = False,
        extra_transform: Callable[[dict[str, Any]], None] | None = None,
    ) -> bool:
        """Persist the settings draft and optional device-form updates once.

        ``extra_transform`` runs under the repository transaction after local
        Settings-page leaves are merged. It lets the shell include all device
        forms in the same validation, atomic YAML replacement and UI refresh.
        """

        if not self._dirty and extra_transform is None:
            return True
        try:
            local_draft = self._apply_tree_values()
            local_changed_paths = self._changed_leaf_paths(self._persisted_raw, local_draft)
            removed_paths = set()
            for path in local_changed_paths:
                try:
                    local_value = self._get_path(local_draft, path)
                except KeyError:
                    removed_paths.add(path)
                    continue
                if self._is_source_autorange_path(path) and local_value is True and path not in self._source_autorange_acknowledged:
                    if silent:
                        raise AuthorizationError("Source AUTO requires explicit operator confirmation in Settings.")
                    if not self._confirm_source_autorange(path):
                        return False
            changed_paths = local_changed_paths
            general_edit_allowed = self._access.allows(Permission.EDIT_SETTINGS)
            operator_output_edit = (
                bool(changed_paths)
                and changed_paths.issubset(self._OPERATOR_EDITABLE_PATHS)
                and self._access.allows(Permission.OPERATE_OUTPUT)
                and not self._base_read_only
            )
            if not general_edit_allowed and not operator_output_edit:
                self._access.require(
                    Permission.EDIT_SETTINGS,
                    action="saving station settings",
                )
            if local_draft.get("access_control") != self._persisted_raw.get(
                "access_control"
            ) and not self._access.allows(Permission.MANAGE_ROLES):
                raise AuthorizationError(
                    "An engineer or service identity is required to change access-control settings."
                )
            repair_result = [False]

            def merge_latest(latest: dict[str, Any], _settings: StationSettings) -> dict[str, Any]:
                # A device page may have saved defaults in the background
                # while this draft was open. Overlay only locally edited
                # leaves onto the newest document under one repository lock.
                moke_master = _MOKE_PATH + ("allow_vout_control",)
                moke_endpoint = _MOKE_PATH + ("endpoint",)
                if (
                    moke_master in local_changed_paths
                    and self._get_path(local_draft, moke_master)
                    and self._get_path(latest, moke_endpoint)
                    != self._get_path(self._persisted_raw, moke_endpoint)
                ):
                    raise ConfigurationError(
                        "The MOKE Box address changed while this draft was open. "
                        "Clear Allow voltage control, save your changes, then enable it for the new address."
                    )
                draft = deepcopy(latest)
                for path in local_changed_paths:
                    if path in removed_paths:
                        try:
                            parent = self._get_path(draft, path[:-1])
                        except KeyError:
                            continue
                        parent.pop(path[-1], None)
                        continue
                    self._set_path(
                        draft,
                        path,
                        deepcopy(self._get_path(local_draft, path)),
                    )
                if extra_transform is not None:
                    extra_transform(draft)
                self._sync_moke_vout_permissions(draft)
                repair_result[0] = self._repository.repair_known_issues(draft)
                for channel in ("A", "B"):
                    path = ("devices", "keithley", "safety", "channels", channel, "defaults", "source_autorange")
                    if self._get_path(draft, path[:-1]).get("source_autorange", False) is True and self._get_path(latest, path[:-1]).get("source_autorange", False) is not True and path not in self._source_autorange_acknowledged:
                        raise AuthorizationError("Source AUTO requires explicit operator confirmation in Settings.")
                return draft

            settings, draft = self._repository.update_raw(merge_latest)
            repaired = repair_result[0]
        except AuthorizationError as exc:
            if silent:
                self.status.emit(f"Background save rejected invalid settings: {exc}")
            else:
                QMessageBox.critical(self, "Changes not saved", str(exc))
            return False
        except (ConfigurationError, ValueError) as exc:
            if silent:
                self.status.emit(f"Background save rejected invalid settings: {exc}")
            else:
                self._validation_failed("Changes not saved", exc, locals().get("draft"))
            return False
        self._raw = draft
        self._persisted_raw = deepcopy(draft)
        self._source_autorange_acknowledged.clear()
        self._settings = settings
        self._dirty = False
        self._clear_validation_errors()
        # Rebuild when a repair or concurrent MOKE assignment changed the editor values.
        moke_connection_changed = any(
            self._get_path(local_draft, _MOKE_PATH + (key,)) != self._get_path(draft, _MOKE_PATH + (key,))
            for key in _MOKE_ASSIGNMENT_FIELDS
        )
        if (not silent and repaired) or moke_connection_changed:
            self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()
        self.settings_saved.emit(settings)
        self.status.emit(
            "Configuration saved and known inconsistencies repaired"
            if repaired
            else "Configuration saved"
        )
        return True

    @classmethod
    def _changed_leaf_paths(
        cls,
        before: object,
        after: object,
        path: tuple[str | int, ...] = (),
    ) -> set[tuple[str | int, ...]]:
        if isinstance(before, dict) and isinstance(after, dict):
            changed: set[tuple[str | int, ...]] = set()
            for key in set(before) | set(after):
                if key not in before or key not in after:
                    changed.add(path + (str(key),))
                else:
                    changed.update(
                        cls._changed_leaf_paths(before[key], after[key], path + (str(key),))
                    )
            return changed
        if isinstance(before, list) and isinstance(after, list):
            changed = set()
            for index in range(max(len(before), len(after))):
                if index >= len(before) or index >= len(after):
                    changed.add(path + (index,))
                else:
                    changed.update(
                        cls._changed_leaf_paths(before[index], after[index], path + (index,))
                    )
            return changed
        return {path} if before != after else set()
