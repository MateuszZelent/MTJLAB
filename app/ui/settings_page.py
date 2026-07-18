"""Editable, validated station settings page with explicit approval workflow."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPalette, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QFormLayout,
    QGroupBox,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
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
from app.settings import SettingsRepository
from app.settings.diagnostics import (
    configuration_diagnostics,
    redacted_settings,
    structural_diff,
)
from app.settings.models import StationSettings


_LIMIT_VALIDATION_MESSAGE_ROLE = int(Qt.ItemDataRole.UserRole) + 101


class _SafetyLimitValidationDelegate(QStyledItemDelegate):
    """Paint validation failures above stylesheet-driven table selection colours."""

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: Any) -> QWidget:
        editor = QLineEdit(parent)
        editor.setPlaceholderText("e.g. 10 mA")
        editor.setStyleSheet(
            "QLineEdit { background: #ffffff; color: #101820; border: 2px solid #1976bd; "
            "border-radius: 4px; padding: 2px 5px; selection-background-color: #1976bd; "
            "selection-color: #ffffff; }"
        )
        return editor

    def paint(self, painter: Any, option: QStyleOptionViewItem, index: Any) -> None:
        message = index.data(_LIMIT_VALIDATION_MESSAGE_ROLE)
        if not message:
            super().paint(painter, option, index)
            return
        highlighted = QStyleOptionViewItem(option)
        highlighted.state &= ~QStyle.StateFlag.State_Selected
        highlighted.palette.setColor(QPalette.ColorRole.Base, QColor("#ffe1e6"))
        highlighted.palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#ffe1e6"))
        highlighted.palette.setColor(QPalette.ColorRole.Text, QColor("#8d1024"))
        super().paint(painter, highlighted, index)
        painter.save()
        painter.setPen(QPen(QColor("#d32645"), 2))
        painter.drawRect(option.rect.adjusted(1, 1, -2, -2))
        painter.restore()


class SettingsPage(QWidget):
    """A generic leaf editor for every value in `.config/settings.yml`.

    Editing a profile always revokes its approval.  Approval is a separate,
    deliberately explicit operation with an operator name and confirmation
    phrase, keeping raw safety limits editable without silently unlocking
    outputs.
    """

    settings_saved = Signal(object)
    status = Signal(str)

    _PROTECTED_PATHS = {
        ("profile", "state"),
        ("profile", "approved_by"),
        ("profile", "approved_at"),
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
        self._changing = False
        self._dirty = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(750)
        self._autosave_timer.timeout.connect(lambda: self.save_draft(silent=True))
        self._build()
        self.reload()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("Station settings")
        title.setObjectName("pageTitle")
        self._subtitle = QLabel()
        self._subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self._subtitle)

        self.tabs = QTabWidget()
        self.trees: dict[str, QTreeWidget] = {}
        self.forms: dict[str, QScrollArea] = {}
        for key, label in (
            ("general", "General"),
            ("rigol", "Rigol"),
            ("keithley", "Keithley"),
            ("anritsu", "Anritsu"),
        ):
            tree = QTreeWidget()
            tree.setHeaderLabels(["Parameter", "Value"])
            tree.setAlternatingRowColors(True)
            tree.itemChanged.connect(self._changed)
            self.trees[key] = tree
            form = QScrollArea()
            form.setObjectName("settingsForm")
            form.setWidgetResizable(True)
            form.setFrameShape(QScrollArea.Shape.NoFrame)
            self.forms[key] = form
            self.tabs.addTab(form, label)
        self.limits_table = QTableWidget(0, 7)
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
        for column in range(2, 7):
            limits_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.limits_table.itemChanged.connect(self._limit_changed)
        self.tabs.addTab(self.limits_table, "Safety limits")
        roles_page = QWidget()
        roles_layout = QVBoxLayout(roles_page)
        self.roles_info = QLabel()
        self.roles_info.setWordWrap(True)
        self.role_table = QTableWidget(0, 2)
        self.role_table.setHorizontalHeaderLabels(["Operating-system account", "Assigned role(s)"])
        self.role_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.role_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.role_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.role_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        role_buttons = QHBoxLayout()
        self.add_role_button = QPushButton("Add user…")
        self.edit_role_button = QPushButton("Edit selected…")
        self.remove_role_button = QPushButton("Remove selected")
        role_buttons.addWidget(self.add_role_button)
        role_buttons.addWidget(self.edit_role_button)
        role_buttons.addWidget(self.remove_role_button)
        role_buttons.addStretch(1)
        roles_layout.addWidget(self.roles_info)
        roles_layout.addWidget(self.role_table, 1)
        roles_layout.addLayout(role_buttons)
        self.tabs.addTab(roles_page, "Access roles")
        diagnostics_page = QWidget()
        diagnostics_layout = QVBoxLayout(diagnostics_page)
        self.diagnostics_text = QPlainTextEdit()
        self.diagnostics_text.setReadOnly(True)
        diagnostics_buttons = QHBoxLayout()
        refresh_diagnostics = QPushButton("Refresh diagnostics")
        export_diagnostics = QPushButton("Export redacted configuration…")
        diagnostics_buttons.addWidget(refresh_diagnostics)
        diagnostics_buttons.addWidget(export_diagnostics)
        diagnostics_buttons.addStretch(1)
        diagnostics_layout.addWidget(self.diagnostics_text, 1)
        diagnostics_layout.addLayout(diagnostics_buttons)
        self.tabs.addTab(diagnostics_page, "Diagnostics")
        self.tree = self.trees["general"]
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.reload_button = QPushButton("Reload")
        self.discard_button = QPushButton("Discard draft")
        self.diff_button = QPushButton("Show changes…")
        self.validate_button = QPushButton("Validate")
        self.save_button = QPushButton("Save changes")
        self.approve_button = QPushButton("Approve profile…")
        for button in (
            self.reload_button,
            self.discard_button,
            self.diff_button,
            self.validate_button,
            self.save_button,
            self.approve_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.reload_button.clicked.connect(self.reload)
        self.discard_button.clicked.connect(self.discard_draft)
        self.diff_button.clicked.connect(self.show_changes)
        self.validate_button.clicked.connect(self.validate_draft)
        self.save_button.clicked.connect(self.save_draft)
        self.approve_button.clicked.connect(self.approve_profile)
        self.add_role_button.clicked.connect(self._add_role_assignment)
        self.edit_role_button.clicked.connect(self._edit_role_assignment)
        self.remove_role_button.clicked.connect(self._remove_role_assignment)
        refresh_diagnostics.clicked.connect(self._refresh_diagnostics)
        export_diagnostics.clicked.connect(self.export_redacted_configuration)
        if self._read_only and not self._can_edit_operator_output():
            self.validate_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.discard_button.setEnabled(False)
        self.approve_button.setEnabled(
            not self._base_read_only and self._access.allows(Permission.APPROVE_PROFILE)
        )
        self._update_role_controls()

    def set_access_policy(self, access_policy: AccessPolicy) -> None:
        self._access = access_policy
        self._read_only = self._base_read_only or not access_policy.allows(
            Permission.EDIT_SETTINGS
        )
        can_edit = not self._read_only or self._can_edit_operator_output()
        self.validate_button.setEnabled(can_edit)
        self.save_button.setEnabled(can_edit)
        self.discard_button.setEnabled(can_edit)
        self.approve_button.setEnabled(
            not self._base_read_only
            and access_policy.allows(Permission.APPROVE_PROFILE)
        )
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

    def _update_subtitle(self) -> None:
        if self._settings is None:
            return
        state = self._settings.profile.state
        locked = "OUTPUTS LOCKED" if self._settings.outputs_locked else "profile approved"
        mode = (
            " • limited edit: Rigol output permission only"
            if self._read_only and self._can_edit_operator_output()
            else " • read-only for this role"
            if self._read_only
            else ""
        )
        self._subtitle.setText(
            f"File: {self._repository.path}  •  Profile: {self._settings.profile.name}  •  "
            f"State: {state}  •  {locked}  •  User: {self._access.identity.display_name}{mode}"
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
        dialog = QDialog(self)
        dialog.setWindowTitle("Settings changes")
        dialog.resize(760, 480)
        layout = QVBoxLayout(dialog)
        summary = QLabel(
            f"{len(changes)} unsaved structural change(s). Saving any safety change revokes profile approval."
        )
        summary.setWordWrap(True)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n".join(changes) if changes else "No unsaved changes.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(summary)
        layout.addWidget(text, 1)
        layout.addWidget(buttons)
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
            for device in ("rigol", "keithley", "anritsu"):
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
        for device in ("rigol", "keithley", "anritsu"):
            self._populate_form("%s" % device, devices.get(device, {}), ("devices", device))

    @staticmethod
    def _title(text: object) -> str:
        return str(text).replace("_", " ").replace("-", " ").title()

    def _populate_form(
        self, name: str, data: Any, prefix: tuple[str | int, ...]
    ) -> None:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        cards: dict[str, QFormLayout] = {}

        def card_for(section: str) -> QFormLayout:
            if section not in cards:
                card = QGroupBox(section)
                card.setObjectName("settingsCard")
                form = QFormLayout(card)
                form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                form.setHorizontalSpacing(18)
                form.setVerticalSpacing(10)
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
            card_for(section).addRow(QLabel(label), self._form_editor(path, value))

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
            editor = QCheckBox("Enabled")
            editor.setChecked(value)
            editor.toggled.connect(lambda _checked, path=path: self._form_changed(path))
        elif choices:
            editor = QComboBox()
            for label, data in choices:
                editor.addItem(label, data)
            editor.setCurrentIndex(max(0, editor.findData(self._format_scalar(value))))
            editor.currentIndexChanged.connect(lambda _index, path=path: self._form_changed(path))
        elif isinstance(value, int) and not isinstance(value, bool):
            editor = QSpinBox()
            editor.setRange(-1_000_000_000, 1_000_000_000)
            editor.setValue(value)
            editor.valueChanged.connect(lambda _number, path=path: self._form_changed(path))
        else:
            editor = QLineEdit(self._format_scalar(value))
            editor.editingFinished.connect(lambda path=path: self._form_changed(path))
        editor.setEnabled(editable)
        editor.setToolTip(" · ".join(str(part) for part in path))
        field = QWidget()
        field_layout = QVBoxLayout(field)
        field_layout.setContentsMargins(0, 0, 0, 0)
        field_layout.setSpacing(3)
        error = QLabel()
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
        autosave = bool(self._raw.get("application", {}).get("settings_autosave", False))
        if autosave and not self._read_only:
            self._autosave_timer.start()
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
            autosave = bool(self._raw.get("application", {}).get("settings_autosave", False))
            if autosave and not self._read_only:
                self._autosave_timer.start()
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
        editor = QComboBox(tree)
        for label, data in choices:
            editor.addItem(label, data)
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
                self._add_limit_row(path, value)
            for key, nested in value.items():
                if isinstance(nested, dict):
                    walk(nested, path + (str(key),))

        devices = self._raw.get("devices", {})
        if isinstance(devices, dict):
            for device, nested in devices.items():
                walk(nested, ("devices", str(device)))

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
        dialog = QDialog(self)
        dialog.setWindowTitle("User access role")
        dialog.setMinimumWidth(420)
        layout = QVBoxLayout(dialog)
        note = QLabel("Assign one or more station roles to an operating-system account.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel("Operating-system account"))
        account = QLineEdit(username)
        account.setPlaceholderText("DOMAIN\\user")
        layout.addWidget(account)
        layout.addWidget(QLabel("Roles"))
        checkboxes: list[QCheckBox] = []
        for role in self._role_choices():
            check = QCheckBox(role.capitalize())
            check.setProperty("role", role)
            check.setChecked(role in roles)
            layout.addWidget(check)
            checkboxes.append(check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
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
        values = (scope, parameter, minimum, maximum, unit, default, "Laboratory profile")
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

    def _limit_changed(self, item: QTableWidgetItem) -> None:
        if self._changing or item.column() not in {2, 3}:
            return
        if item in self._limit_error_items:
            item.setBackground(Qt.GlobalColor.transparent)
            item.setData(_LIMIT_VALIDATION_MESSAGE_ROLE, None)
            item.setToolTip("")
            self._limit_error_items.remove(item)
        path = item.data(Qt.ItemDataRole.UserRole)
        if path is None:
            return
        self._sync_tree_from_limit(tuple(path), item.text())
        self._dirty = True
        autosave = bool(self._raw.get("application", {}).get("settings_autosave", False))
        if autosave and not self._read_only:
            self._autosave_timer.start()
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
        for item in self._limit_error_items:
            item.setBackground(Qt.GlobalColor.transparent)
            item.setData(_LIMIT_VALIDATION_MESSAGE_ROLE, None)
            item.setToolTip("")
        self._limit_error_items.clear()

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
            matched = max(candidates, key=len)
            editor = self._form_editors[matched]
            self._set_validation_state(editor, "error")
            label = self._field_errors[matched]
            label.setText(message)
            label.show()
            marked_editor = editor
        for limit_path, item in self._limit_items_by_path.items():
            if (
                tuple(path[: len(limit_path)]) == limit_path
                or tuple(limit_path[: len(path)]) == path
            ):
                item.setData(_LIMIT_VALIDATION_MESSAGE_ROLE, message)
                item.setToolTip(f"Validation error: {message}")
                if item not in self._limit_error_items:
                    self._limit_error_items.append(item)
        return marked_editor

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
        if "anritsu acquisition requires complete frequency and reference-level limits" in message:
            return tuple(
                anritsu_safety + (range_name, boundary)
                for range_name in ("frequency", "reference_level")
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
                self.tabs.setCurrentWidget(self.limits_table)
                self.limits_table.setCurrentItem(first)
                self.limits_table.scrollToItem(first)
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
        QMessageBox.critical(self, title, str(error))

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
            draft = self._apply_tree_values()
            changed_paths = self._changed_leaf_paths(self._persisted_raw, draft)
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
                draft.get("access_control") != self._persisted_raw.get("access_control")
                and not self._access.allows(Permission.MANAGE_ROLES)
            ):
                raise AuthorizationError(
                    "An engineer or service identity is required to change access-control settings."
                )
            # A limit edit invalidates a previous approval and retains the safe
            # output lock.  Explicit approval is handled below.
            draft["profile"]["state"] = "unverified"
            draft["profile"]["approved_by"] = None
            draft["profile"]["approved_at"] = None
            draft["profile"]["approval_note"] = "Profile approval is required after settings changes."
            settings = self._repository.save_raw(draft)
        except AuthorizationError as exc:
            if silent:
                self.status.emit(f"Autosave rejected invalid settings: {exc}")
            else:
                QMessageBox.critical(self, "Changes not saved", str(exc))
            return False
        except (ConfigurationError, ValueError) as exc:
            if silent:
                self.status.emit(f"Autosave rejected invalid settings: {exc}")
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
            "Configuration autosaved; profile approval is required"
            if silent
            else "Configuration saved; profile approval is required"
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

    def approve_profile(self) -> None:
        if not self._require_access(Permission.APPROVE_PROFILE, "approving the safety profile"):
            return
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                "Save changes first? Approval will apply to the saved profile.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.save_draft()
            if self._dirty:
                return
        if self._settings is None:
            return
        operator = self._access.identity.username
        phrase = f"APPROVE {self._settings.profile.id}"
        confirmation, ok = QInputDialog.getText(
            self,
            "Confirmation",
            f"Enter exactly: {phrase}",
        )
        if not ok or confirmation.strip() != phrase:
            QMessageBox.warning(self, "Not approved", "The confirmation phrase is incorrect.")
            return
        draft = deepcopy(self._raw)
        draft["profile"]["state"] = "approved"
        draft["profile"]["approved_by"] = operator
        draft["profile"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        draft["profile"]["approval_note"] = (
            "Profile approved by an authenticated engineer/service identity in the GUI."
        )
        try:
            settings = self._repository.save_raw(draft)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Not approved", str(exc))
            return
        self._raw = draft
        self._persisted_raw = deepcopy(draft)
        self._settings = settings
        self._populate()
        self._update_subtitle()
        self._refresh_diagnostics()
        self.settings_saved.emit(settings)
        self.status.emit("Profile approved")
