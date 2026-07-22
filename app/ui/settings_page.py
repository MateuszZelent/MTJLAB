"""Editable, validated station settings page."""

from __future__ import annotations

from copy import deepcopy
import json
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QHeaderView,
    QDialog,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QFormLayout,
    QFrame,
    QGridLayout,
    QSpinBox,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSizePolicy,
    QStackedWidget,
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
from app.security import AccessPolicy, Permission
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.dialogs import StationDialog, StationMessageBox as QMessageBox
from app.settings import SettingsRepository
from app.settings.diagnostics import (
    configuration_diagnostics,
    redacted_settings,
    structural_diff,
)
from app.settings.models import StationSettings
from app.settings.validation import format_settings_validation_error
from app.ui.widgets import show_toast
from app.ui.settings_guidance import SettingsPath


_LIMIT_VALIDATION_MESSAGE_ROLE = int(Qt.ItemDataRole.UserRole) + 101


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
    status = Signal(str)

    _PROTECTED_PATHS = {
        ("devices", "anritsu", "safety", "reference_level", "min"),
        ("devices", "anritsu", "safety", "reference_level", "max"),
    }
    _OPERATOR_EDITABLE_PATHS = {
        ("devices", "rigol", "safety", "allow_output_enable"),
    }

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
        self._form_editors: dict[tuple[str | int, ...], QWidget] = {}
        self._field_errors: dict[tuple[str | int, ...], QLabel] = {}
        self._limit_error_items: list[QTableWidgetItem] = []
        self._limit_items_by_path: dict[tuple[str | int, ...], QTableWidgetItem] = {}
        self._safety_limit_editors: dict[tuple[str | int, ...], QLineEdit] = {}
        self._safety_limit_error_labels: dict[tuple[str | int, ...], QLabel] = {}
        self._changing = False
        self._dirty = False
        self._autosave_enabled = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(750)
        # Compatibility object only: persistence now requires an explicit
        # SAVE SETTINGS / Save changes action.
        self._build()
        self.reload()

    def _build(self) -> None:
        self.setProperty("stationSurface", "page")
        self.setObjectName("settingsPage")
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
        # These widgets retain the editable draft representation consumed by
        # validation and persistence, but are not part of the operator-facing
        # Fluent forms.  Keep them below one permanently hidden parent so a
        # native style/theme refresh cannot expose their default 100x30
        # geometry at the page origin.
        self._draft_model_host = QWidget(self)
        self._draft_model_host.setObjectName("settingsDraftModelHost")
        self._draft_model_host.hide()
        self.trees: dict[str, QTreeWidget] = {}
        self.forms: dict[str, ScrollArea] = {}
        for key, label in (
            ("general", "General"),
            ("rigol", "Rigol"),
            ("keithley", "Keithley"),
            ("anritsu", "Anritsu"),
            ("moke_box", "MOKE Box"),
            ("lakeshore_gaussmeter", "Lake Shore 475"),
        ):
            tree = TreeWidget(self._draft_model_host)
            tree.setHeaderLabels(["Parameter", "Value"])
            tree.setAlternatingRowColors(True)
            tree.itemChanged.connect(self._changed)
            self.trees[key] = tree
            form = ScrollArea()
            form.setObjectName("settingsForm")
            form.setProperty("stationSurface", "page")
            form.viewport().setProperty("stationSurface", "page")
            form.setWidgetResizable(True)
            form.setFrameShape(QFrame.Shape.NoFrame)
            self.forms[key] = form
            self.tabs.addTab(form, label)
        limits_page = QWidget()
        limits_page.setObjectName("settingsSpecialPage")
        limits_page.setProperty("stationSurface", "page")
        limits_layout = QVBoxLayout(limits_page)
        limits_layout.setContentsMargins(18, 18, 18, 18)
        limits_layout.setSpacing(12)
        limits_title = StrongBodyLabel("Safety limits")
        limits_title.setObjectName("sectionTitle")
        limits_description = BodyLabel(
            "Laboratory software boundaries. Enter values with units, for example "
            "10 mA, 67 mV or 1 MHz. Disabling a limit does not disable immutable "
            "immutable instrument limits."
        )
        limits_description.setObjectName("muted")
        limits_description.setWordWrap(True)
        self._limit_validation_toast_message: str | None = None
        limits_card = CardWidget()
        limits_card.setObjectName("settingsTableCard")
        limits_card_layout = QVBoxLayout(limits_card)
        limits_card_layout.setContentsMargins(14, 14, 14, 14)
        self.limits_table = TableWidget(self._draft_model_host)
        self.limits_table.setColumnCount(7)
        self.limits_table.setHorizontalHeaderLabels(
            ["Device / scope", "Parameter", "Minimum", "Maximum", "Unit", "Default", "Source"]
        )
        self.limits_table.setAlternatingRowColors(True)
        self.limits_table.setItemDelegate(_SafetyLimitValidationDelegate(self.limits_table))
        self.limits_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.limits_table.verticalHeader().setVisible(False)
        limits_header = self.limits_table.horizontalHeader()
        limits_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        limits_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(4, 7):
            limits_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (2, 3):
            limits_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.limits_table.setColumnWidth(column, 170)
        self.limits_table.itemChanged.connect(self._limit_changed)
        # Retained as the draft model used by the station settings editor.
        # The operator-facing editor below is intentionally card based.
        self.limits_table.hide()
        self.limits_scroll = ScrollArea()
        self.limits_scroll.setObjectName("settingsForm")
        self.limits_scroll.setWidgetResizable(True)
        self.limits_scroll.setFrameShape(QFrame.Shape.NoFrame)
        limits_card_layout.addWidget(self.limits_scroll)
        limits_layout.addWidget(limits_title)
        limits_layout.addWidget(limits_description)
        limits_layout.addWidget(limits_card, 1)
        self.limits_page = limits_page
        self.tabs.addTab(limits_page, "Safety limits")
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
        self.tabs.addTab(roles_page, "Access roles")
        diagnostics_page = QWidget()
        diagnostics_page.setObjectName("settingsSpecialPage")
        diagnostics_page.setProperty("stationSurface", "page")
        diagnostics_layout = QVBoxLayout(diagnostics_page)
        diagnostics_layout.setContentsMargins(18, 18, 18, 18)
        diagnostics_layout.setSpacing(12)
        diagnostics_title = StrongBodyLabel("Configuration diagnostics")
        diagnostics_title.setObjectName("sectionTitle")
        diagnostics_description = BodyLabel("Read-only checks for the active station configuration.")
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
        self.tabs.addTab(diagnostics_page, "Diagnostics")
        self.tree = self.trees["general"]
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
        for index, button in enumerate((
            self.reload_button,
            self.discard_button,
            self.diff_button,
            self.validate_button,
            self.save_button,
        )):
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
            QBoxLayout.Direction.TopToBottom
            if compact
            else QBoxLayout.Direction.LeftToRight
        )

    def set_access_policy(self, access_policy: AccessPolicy) -> None:
        self._access = access_policy
        self._read_only = self._base_read_only or not access_policy.allows(
            Permission.EDIT_SETTINGS
        )
        can_edit = not self._read_only or self._can_edit_operator_output()
        self.validate_button.setEnabled(can_edit)
        self.save_button.setEnabled(can_edit)
        self.discard_button.setEnabled(can_edit)
        self._update_role_controls()
        self.reload()

    def _can_edit_operator_output(self) -> bool:
        return (
            not self._base_read_only
            and self._access.allows(Permission.OPERATE_OUTPUT)
        )

    def _update_role_controls(self) -> None:
        allowed = (
            not self._base_read_only
            and self._access.allows(Permission.MANAGE_ROLES)
        )
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

    def accept_external_snapshot(
        self, settings: StationSettings, raw: dict[str, Any]
    ) -> None:
        """Refresh a clean settings page after a background external save.

        An open local draft keeps its widgets untouched.  ``save_draft`` merges
        only locally changed leaves into the latest file, so that draft cannot
        later overwrite the background update with stale values.
        """

        self._settings = settings
        if self._dirty:
            self.status.emit(
                "External settings saved; the open local draft was preserved"
            )
            return
        self._raw = deepcopy(raw)
        self._persisted_raw = deepcopy(raw)
        self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()

    def stage_external_snapshot(
        self, settings: StationSettings, raw: dict[str, Any]
    ) -> None:
        """Replace the in-memory draft without persisting it."""

        self._autosave_timer.stop()
        self._settings = settings
        self._raw = deepcopy(raw)
        self._dirty = self._raw != self._persisted_raw
        self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()
        self.status.emit("Unsaved settings changes; press SAVE SETTINGS")

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
        layout = QVBoxLayout(dialog)
        summary = BodyLabel(
            f"{len(changes)} unsaved structural change(s). Values will be validated before saving."
        )
        summary.setWordWrap(True)
        text = PlainTextEdit(dialog)
        text.setReadOnly(True)
        text.setPlainText("\n".join(changes) if changes else "No unsaved changes.")
        layout.addWidget(summary)
        layout.addWidget(text, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        close = PushButton("Close", dialog)
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

    def _populate(self) -> None:
        self._changing = True
        try:
            for tree in self.trees.values():
                tree.clear()
            self._choice_editors.clear()
            general = {
                key: value
                for key, value in self._raw.items()
                if key not in {"schema_version", "devices", "access_control"}
            }
            self._add_items(self.trees["general"], None, general, ())
            devices = self._raw.get("devices", {})
            for device in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"):
                self._add_items(
                    self.trees[device],
                    None,
                    devices.get(device, {}),
                    ("devices", device),
                )
            for tree in self.trees.values():
                tree.expandToDepth(3)
                tree.resizeColumnToContents(0)
            self._populate_forms(general, devices)
            self._populate_limits()
            self._populate_roles()
        finally:
            self._changing = False

    def _populate_forms(self, general: dict[str, Any], devices: dict[str, Any]) -> None:
        self._form_editors.clear()
        self._field_errors.clear()
        self._populate_form("general", general, ())
        for device in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"):
            self._populate_form("%s" % device, devices.get(device, {}), ("devices", device))

    @staticmethod
    def _title(text: object) -> str:
        return str(text).replace("_", " ").replace("-", " ").title()

    def _populate_form(
        self, name: str, data: Any, prefix: tuple[str | int, ...]
    ) -> None:
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

        def card_for(section: str) -> QFormLayout:
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
                layout.addWidget(card)
                cards[section] = form
            return cards[section]

        def walk(value: Any, path: tuple[str | int, ...], labels: tuple[str, ...]) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == "lab_limits":
                        continue
                    walk(nested, path + (str(key),), labels + (self._title(key),))
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

        walk(data, prefix, ())
        layout.addStretch(1)
        self.forms[name].setWidget(host)

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
        )

    def _form_editor(self, path: tuple[str | int, ...], value: Any) -> QWidget:
        editable = self._form_editable(path)
        choices = self._choices_for_path(path, value)
        if isinstance(value, bool):
            editor = CheckBox("Enabled")
            editor.setChecked(value)
            editor.toggled.connect(lambda _checked, path=path: self._form_changed(path))
        elif choices:
            editor = ComboBox()
            for label, data in choices:
                editor.addItem(label, userData=data)
            editor.setCurrentIndex(max(0, editor.findData(self._format_scalar(value))))
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
            elif value is None and path and str(path[-1]) in {
                "max",
                "max_abs",
                "max_expected_power_at_connector",
                "min",
                "minimum_internal_attenuation",
                "nominal",
            }:
                # Optional safety quantities start empty but become explicit
                # number-plus-unit values when configured.
                precision_steppable = True
            editor.setProperty("precisionArrowStepping", precision_steppable)
            editor.editingFinished.connect(lambda path=path: self._form_changed(path))
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
        self._form_editors[path] = editor
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
        role_path = bool(path) and path[0] == "access_control"
        role_path_editable = not role_path or self._access.allows(Permission.MANAGE_ROLES)
        operator_output_permission = (
            path in self._OPERATOR_EDITABLE_PATHS
            and not self._base_read_only
            and self._access.allows(Permission.OPERATE_OUTPUT)
        )
        if (
            (not self._read_only or operator_output_permission)
            and role_path_editable
            and path not in self._PROTECTED_PATHS
        ):
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
        if not self._changing and column == 1 and item.data(0, Qt.ItemDataRole.UserRole) is not None:
            path = tuple(item.data(0, Qt.ItemDataRole.UserRole))
            self._sync_limit_from_tree(path, item.text(1))
            editor = self._form_editors.get(path)
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
        annotation = cls._annotation_for_path(path)
        if annotation is not None and get_origin(annotation) is Literal:
            return tuple((str(option), str(option)) for option in get_args(annotation))
        return ()

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
        editor.currentIndexChanged.connect(
            lambda _index, item=item, editor=editor: item.setText(1, str(editor.currentData()))
        )
        tree.setItemWidget(item, 1, editor)
        self._choice_editors[path] = editor

    def editor_for_path(self, path: tuple[str | int, ...]) -> QComboBox | None:
        """Return the list editor for a settings value, when it has choices."""

        editor = self._form_editors.get(path)
        return editor if isinstance(editor, QComboBox) else self._choice_editors.get(path)

    def _apply_tree_values(self) -> dict[str, Any]:
        draft = deepcopy(self._raw)

        for path, editor in self._form_editors.items():
            original = self._get_path(draft, path)
            self._set_path(draft, path, self._form_value(editor, original))

        def walk(item: QTreeWidgetItem) -> None:
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if path is not None:
                if tuple(path) in self._form_editors:
                    return
                original = self._get_path(draft, tuple(path))
                self._set_path(draft, tuple(path), self._parse_scalar(item.text(1), original))
            for index in range(item.childCount()):
                walk(item.child(index))

        for tree in self.trees.values():
            for index in range(tree.topLevelItemCount()):
                walk(tree.topLevelItem(index))
        for row in range(self.limits_table.rowCount()):
            for column in (2, 3):
                item = self.limits_table.item(row, column)
                path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
                if path is None:
                    continue
                original = self._get_path(draft, tuple(path))
                self._set_path(draft, tuple(path), self._parse_scalar(item.text(), original))
        return draft

    def _populate_limits(self) -> None:
        self.limits_table.setRowCount(0)
        self._limit_items_by_path.clear()

        def walk(value: Any, path: tuple[str | int, ...]) -> None:
            if not isinstance(value, dict):
                return
            if "min" in value and "max" in value:
                if path != ("devices", "anritsu", "safety", "reference_level"):
                    self._add_limit_row(path, value)
            for key, nested in value.items():
                if isinstance(nested, dict):
                    walk(nested, path + (str(key),))
                elif (
                    key == "max_abs_power"
                    and path[-1:] == ("lab_limits",)
                    and isinstance(nested, str)
                ):
                    self._add_scalar_limit_row(path + (str(key),), nested)

        devices = self._raw.get("devices", {})
        if isinstance(devices, dict):
            for device, nested in devices.items():
                walk(nested, ("devices", str(device)))
        self._populate_safety_limit_cards()

    def _populate_safety_limit_cards(self) -> None:
        """Build the operator-facing safety editor from the canonical limit rows."""

        self._safety_limit_editors.clear()
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

        for row in range(self.limits_table.rowCount()):
            minimum = self.limits_table.item(row, 2)
            maximum = self.limits_table.item(row, 3)
            if minimum is None or maximum is None:
                continue
            min_path = tuple(minimum.data(Qt.ItemDataRole.UserRole) or ())
            max_path = tuple(maximum.data(Qt.ItemDataRole.UserRole) or ())
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

            scope_item = self.limits_table.item(row, 0)
            parameter_item = self.limits_table.item(row, 1)
            unit_item = self.limits_table.item(row, 4)
            row_widget = CardWidget(self)
            row_widget.setObjectName("safetyLimitRow")
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(12, 10, 12, 10)
            row_layout.setSpacing(7)
            values_layout = QHBoxLayout()
            values_layout.setSpacing(10)
            label_text = parameter_item.text().title() if parameter_item is not None else "Limit"
            if scope_item is not None and scope_item.text() != device.lower():
                label_text = f"{label_text}  ·  {scope_item.text()}"
            label = BodyLabel(label_text)
            label.setObjectName("safetyLimitLabel")
            label.setMinimumWidth(250)
            values_layout.addWidget(label, 1)
            scalar_limit = not max_path
            values_layout.addWidget(
                self._safety_boundary_label("LIMIT" if scalar_limit else "MIN")
            )
            minimum_editor = self._make_safety_limit_editor(min_path, minimum.text())
            values_layout.addWidget(minimum_editor)
            if not scalar_limit:
                values_layout.addWidget(self._safety_boundary_label("MAX"))
                maximum_editor = self._make_safety_limit_editor(max_path, maximum.text())
                values_layout.addWidget(maximum_editor)
            unit = BodyLabel(unit_item.text() if unit_item is not None else "SI / explicit")
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
                lambda disabled, p=enabled_path, row=row_widget, editors=(
                    minimum_editor,
                    None if scalar_limit else maximum_editor,
                ): self._set_limit_disabled(p, disabled, row, editors)
            )
            values_layout.addWidget(disable_limit)
            minimum_editor.setEnabled(enabled and not self._read_only)
            if not scalar_limit:
                maximum_editor.setEnabled(enabled and not self._read_only)
            row_widget.setProperty("limitDisabled", not enabled)
            row_layout.addLayout(values_layout)
            error = BodyLabel()
            error.setObjectName("settingsFieldError")
            error.setWordWrap(True)
            error.hide()
            row_layout.addWidget(error)
            self._safety_limit_error_labels[min_path] = error
            if not scalar_limit:
                self._safety_limit_error_labels[max_path] = error
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
        row.setProperty("limitDisabled", disabled)
        row.style().unpolish(row)
        row.style().polish(row)
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

    def _make_safety_limit_editor(
        self, path: tuple[str | int, ...], value: str
    ) -> QLineEdit:
        editor = LineEdit()
        editor.setText(value)
        editor.setObjectName("safetyLimitInput")
        editor.setMinimumWidth(156)
        editor.setReadOnly(self._read_only)
        editor.setProperty("limitPath", path)
        editor.editingFinished.connect(lambda p=path, e=editor: self._commit_safety_limit_editor(p, e))
        self._safety_limit_editors[path] = editor
        return editor

    def _commit_safety_limit_editor(
        self, path: tuple[str | int, ...], editor: QLineEdit
    ) -> None:
        item = self._limit_items_by_path.get(path)
        if item is not None and item.text() != editor.text():
            item.setText(editor.text())

    def _populate_roles(self) -> None:
        self.role_table.setRowCount(0)
        access = self._raw.get("access_control", {})
        assignments = access.get("user_roles", {}) if isinstance(access, dict) else {}
        if not isinstance(assignments, dict):
            return
        for username, roles in sorted(assignments.items(), key=lambda item: str(item[0]).casefold()):
            row = self.role_table.rowCount()
            self.role_table.insertRow(row)
            self.role_table.setItem(row, 0, QTableWidgetItem(str(username)))
            role_text = ", ".join(str(role) for role in roles) if isinstance(roles, list) else str(roles)
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
        layout = QVBoxLayout(dialog)
        note = BodyLabel("Assign one or more station roles to an operating-system account.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(BodyLabel("Operating-system account"))
        account = LineEdit(dialog)
        # Account names are identifiers even when an installation happens to
        # use digits only; Up/Down must not rewrite them as numeric values.
        account.setProperty("precisionArrowStepping", False)
        account.setText(username)
        account.setPlaceholderText("DOMAIN\\user")
        layout.addWidget(account)
        layout.addWidget(BodyLabel("Roles"))
        checkboxes: list[QCheckBox] = []
        for role in self._role_choices():
            check = CheckBox(role.capitalize(), dialog)
            check.setProperty("role", role)
            check.setChecked(role in roles)
            layout.addWidget(check)
            checkboxes.append(check)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", dialog)
        save = PrimaryPushButton("Save roles", dialog)
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
            QMessageBox.warning(self, "User role", "Enter an account name and select at least one role.")
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

    def _add_limit_row(self, path: tuple[str | int, ...], value: dict[str, Any]) -> None:
        row = self.limits_table.rowCount()
        self.limits_table.insertRow(row)
        scope_parts = [str(part) for part in path[1:-1] if str(part) not in {"safety", "lab_limits"}]
        scope = " / ".join(scope_parts) or str(path[1])
        parameter = str(path[-1]).replace("_", " ")
        minimum = self._format_scalar(value.get("min"))
        maximum = self._format_scalar(value.get("max"))
        unit = self._range_unit(minimum, maximum)
        default = self._default_for_limit(path)
        values = (scope, parameter, minimum, maximum, unit, default, "Station configuration")
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if column in {2, 3}:
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    path + (("min" if column == 2 else "max"),),
                )
                self._limit_items_by_path[tuple(item.data(Qt.ItemDataRole.UserRole))] = item
                if not self._read_only:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.limits_table.setItem(row, column, item)

    def _add_scalar_limit_row(
        self, path: tuple[str | int, ...], value: str
    ) -> None:
        """Add a one-value safety limit such as maximum allowed power."""

        row = self.limits_table.rowCount()
        self.limits_table.insertRow(row)
        scope_parts = [
            str(part)
            for part in path[1:-1]
            if str(part) not in {"safety", "lab_limits"}
        ]
        scope = " / ".join(scope_parts) or str(path[1])
        parameter = str(path[-1]).replace("_", " ")
        # The value itself always carries its unit and may use a different SI
        # prefix after editing (for example 6700 uW -> 6.7 mW).  A duplicated
        # fixed suffix would incorrectly imply that the editor is unitless.
        unit = "explicit unit"
        values = (scope, parameter, value, "—", unit, "—", "Station configuration")
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if column == 2:
                item.setData(Qt.ItemDataRole.UserRole, path)
                self._limit_items_by_path[path] = item
                if not self._read_only:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.limits_table.setItem(row, column, item)

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
        parameter = (
            str(path[-2])
            if len(path) >= 2 and leaf in {"min", "max", "max_abs"}
            else leaf
        )
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
        if "voltage" in parameter or parameter in {"high_level", "low_level", "offset", "amplitude_vpp"}:
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

    def _validate_limit_row(self, row: int) -> None:
        minimum = self.limits_table.item(row, 2)
        maximum = self.limits_table.item(row, 3)
        if minimum is None or maximum is None:
            return
        path = minimum.data(Qt.ItemDataRole.UserRole)
        if path is None:
            return
        dimension = self._limit_dimension(tuple(path))
        if maximum.data(Qt.ItemDataRole.UserRole) is None:
            self._set_limit_validation(minimum, None)
            try:
                if dimension is None:
                    float(minimum.text().strip().replace(",", "."))
                else:
                    value = parse_quantity(
                        minimum.text(), dimension, require_unit=True
                    ).si_value
                    if value <= 0:
                        raise ValueError("Value must be greater than zero.")
            except (QuantityError, ValueError) as exc:
                expected = f" Enter {self._dimension_unit_hint(dimension)}."
                self._set_limit_validation(
                    minimum, f"Invalid limit value:{expected} {exc}"
                )
            return
        values: dict[str, float] = {}
        for item, boundary in ((minimum, "minimum"), (maximum, "maximum")):
            # A range is a single validation unit: correcting either boundary must
            # also remove stale errors left on the opposite boundary.
            self._set_limit_validation(item, None)
            if self._is_empty_limit_value(item.text()):
                continue
            try:
                if dimension is None:
                    float(item.text().strip().replace(",", "."))
                else:
                    values[boundary] = parse_quantity(
                        item.text(), dimension, require_unit=True
                    ).si_value
            except (QuantityError, ValueError) as exc:
                expected = f" Enter {self._dimension_unit_hint(dimension)}."
                self._set_limit_validation(item, f"Invalid {boundary} value:{expected} {exc}")
        if len(values) == 2 and values["minimum"] > values["maximum"]:
            message = "Minimum cannot exceed maximum."
            self._set_limit_validation(minimum, message)
            self._set_limit_validation(maximum, message)

    def _limit_changed(self, item: QTableWidgetItem) -> None:
        if self._changing or item.column() not in {2, 3}:
            return
        if item in self._limit_error_items:
            self._set_limit_validation(item, None)
        path = item.data(Qt.ItemDataRole.UserRole)
        if path is None:
            return
        safety_editor = self._safety_limit_editors.get(tuple(path))
        if safety_editor is not None and safety_editor.text() != item.text():
            safety_editor.setText(item.text())
        self._validate_limit_row(item.row())
        if self._limit_error_items:
            self._show_limit_validation_toast(
                "Incorrect value or unit. Correct the red outlined field before saving."
            )
        else:
            self._limit_validation_toast_message = None
        self._sync_tree_from_limit(tuple(path), item.text())
        self._dirty = True
        if self._autosave_enabled:
            self.status.emit("Safety-limit autosave pending…")

    def _sync_tree_from_limit(self, path: tuple[str | int, ...], text: str) -> None:
        self._changing = True
        try:
            editor = self._form_editors.get(path)
            if isinstance(editor, QLineEdit):
                editor.setText(text)
            for tree in self.trees.values():
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
            for row in range(self.limits_table.rowCount()):
                for column in (2, 3):
                    item = self.limits_table.item(row, column)
                    if item is not None and tuple(item.data(Qt.ItemDataRole.UserRole) or ()) == path:
                        item.setText(text)
                        safety_editor = self._safety_limit_editors.get(path)
                        if safety_editor is not None and safety_editor.text() != text:
                            safety_editor.setText(text)
                        return
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
        widget.setProperty("validationState", state)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _clear_validation_error(self, path: tuple[str | int, ...]) -> None:
        editor = self._form_editors.get(path)
        message = self._field_errors.get(path)
        if editor is not None:
            self._set_validation_state(editor, "")
        if message is not None:
            message.clear()
            message.hide()

    def _clear_validation_errors(self) -> None:
        for path in tuple(self._form_editors):
            self._clear_validation_error(path)
        for item in tuple(self._limit_error_items):
            self._set_limit_validation(item, None)
        self._limit_validation_toast_message = None

    def _show_limit_validation_toast(self, message: str) -> None:
        """Announce a new limit-validation problem without reflowing the tab."""

        if message == self._limit_validation_toast_message:
            return
        self._limit_validation_toast_message = message
        show_toast(self, message, severity="error", timeout_ms=8_000, title="Safety limits")

    def _set_limit_validation(self, item: QTableWidgetItem, message: str | None) -> None:
        """Update error metadata without recursively re-entering itemChanged."""

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

    def _sync_safety_limit_validation(self, item: QTableWidgetItem) -> None:
        path = tuple(item.data(Qt.ItemDataRole.UserRole) or ())
        if not path:
            return
        editor = self._safety_limit_editors.get(path)
        message = item.data(_LIMIT_VALIDATION_MESSAGE_ROLE)
        if editor is not None:
            self._set_validation_state(editor, "error" if message else "")
            editor.setToolTip(f"Validation error: {message}" if message else "")
        label = self._safety_limit_error_labels.get(path)
        if label is not None:
            # MIN and MAX share one concise message line.  Do not hide it while
            # the sibling boundary still has an error.
            active_messages = [
                candidate.data(_LIMIT_VALIDATION_MESSAGE_ROLE)
                for candidate_path, candidate in self._limit_items_by_path.items()
                if self._safety_limit_error_labels.get(candidate_path) is label
                and candidate.data(_LIMIT_VALIDATION_MESSAGE_ROLE)
            ]
            active_message = active_messages[0] if active_messages else None
            label.setText(str(active_message or ""))
            label.setVisible(bool(active_message))

    def _mark_invalid_path(self, path: tuple[str | int, ...], message: str) -> QWidget | None:
        candidates = [
            candidate for candidate in self._form_editors
            if (
                len(candidate) <= len(path) and tuple(path[: len(candidate)]) == candidate
            )
            or (
                len(path) <= len(candidate) and tuple(candidate[: len(path)]) == path
            )
        ]
        marked_editor: QWidget | None = None
        if candidates:
            longest = max(len(candidate) for candidate in candidates)
            for matched in (
                candidate for candidate in candidates if len(candidate) == longest
            ):
                editor = self._form_editors[matched]
                self._set_validation_state(editor, "error")
                label = self._field_errors[matched]
                label.setText(message)
                label.show()
                if marked_editor is None:
                    marked_editor = editor
        for limit_path, item in self._limit_items_by_path.items():
            if (
                tuple(path[: len(limit_path)]) == limit_path
                or tuple(limit_path[: len(path)]) == path
            ):
                self._set_limit_validation(item, message)
        return marked_editor

    def reveal_settings_issues(self, paths: tuple[SettingsPath, ...], message: str) -> None:
        """Open and visibly mark fields implicated by a rejected safety action."""

        if not paths:
            return
        first_path = paths[0]
        device = str(first_path[1]) if len(first_path) > 1 and first_path[0] == "devices" else "general"
        form = self.forms.get(device)
        if form is not None:
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
            return tuple(
                anritsu_safety + ("frequency", boundary)
                for boundary in ("min", "max")
            )
        if "optional range must define both limits" in message:
            return self._missing_range_boundaries(draft)
        if "signal generator output permission requires complete frequency and power limits" in message:
            return tuple(
                ("devices", "anritsu", "signal_generator", range_name, boundary)
                for range_name in ("frequency", "power")
                for boundary in ("min", "max")
            )
        return ()

    def _show_validation_errors(self, error: Exception, draft: dict[str, Any] | None = None) -> None:
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
            if first_editor is None and draft is not None and not self._limit_error_items:
                for path in self._changed_leaf_paths(self._persisted_raw, draft):
                    editor = self._mark_invalid_path(path, str(error))
                    if first_editor is None and editor is not None:
                        first_editor = editor
            if self._limit_error_items:
                first = self._limit_error_items[0]
                self._show_limit_validation_toast(
                    "Correct the highlighted safety-limit fields before validating again."
                )
                self.tabs.setCurrentWidget(self.limits_page)
                path = tuple(first.data(Qt.ItemDataRole.UserRole) or ())
                editor = self._safety_limit_editors.get(path)
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

    def validate_draft(self) -> StationSettings | None:
        try:
            draft = self._apply_tree_values()
            settings = StationSettings.model_validate(draft)
        except (ConfigurationError, ValueError) as exc:
            self._validation_failed("Validation error", exc, locals().get("draft"))
            return None
        self._clear_validation_errors()
        QMessageBox.information(self, "Validation", "The configuration is valid.")
        self.status.emit("Configuration validation passed")
        return settings

    def save_draft(self, *, silent: bool = False) -> bool:
        if not self._dirty:
            return True
        try:
            local_draft = self._apply_tree_values()
            local_changed_paths = self._changed_leaf_paths(
                self._persisted_raw, local_draft
            )
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
            if (
                local_draft.get("access_control")
                != self._persisted_raw.get("access_control")
                and not self._access.allows(Permission.MANAGE_ROLES)
            ):
                raise AuthorizationError(
                    "An engineer or service identity is required to change access-control settings."
                )
            repair_result = [False]

            def merge_latest(
                latest: dict[str, Any], _settings: StationSettings
            ) -> dict[str, Any]:
                # A device page may have saved defaults in the background
                # while this draft was open. Overlay only locally edited
                # leaves onto the newest document under one repository lock.
                draft = deepcopy(latest)
                for path in local_changed_paths:
                    self._set_path(
                        draft,
                        path,
                        deepcopy(self._get_path(local_draft, path)),
                    )
                repair_result[0] = self._repository.repair_known_issues(draft)
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
        self._settings = settings
        self._dirty = False
        self._clear_validation_errors()
        if not silent:
            self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()
        self.settings_saved.emit(settings)
        self.status.emit(
            "Configuration saved and known inconsistencies repaired"
            if silent and repaired
            else "Configuration saved and known inconsistencies repaired"
            if repaired
            else "Configuration saved"
            if silent
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
                        cls._changed_leaf_paths(
                            before[key], after[key], path + (str(key),)
                        )
                    )
            return changed
        if isinstance(before, list) and isinstance(after, list):
            changed = set()
            for index in range(max(len(before), len(after))):
                if index >= len(before) or index >= len(after):
                    changed.add(path + (index,))
                else:
                    changed.update(
                        cls._changed_leaf_paths(
                            before[index], after[index], path + (index,)
                        )
                    )
            return changed
        return {path} if before != after else set()
