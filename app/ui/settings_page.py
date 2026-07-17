"""Editable, validated station settings page with explicit approval workflow."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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
from app.security import AccessPolicy, Permission
from app.settings import SettingsRepository
from app.settings.diagnostics import (
    configuration_diagnostics,
    redacted_settings,
    structural_diff,
)
from app.settings.models import StationSettings


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
            self.tabs.addTab(tree, label)
        self.limits_table = QTableWidget(0, 7)
        self.limits_table.setHorizontalHeaderLabels(
            ["Device / scope", "Parameter", "Minimum", "Maximum", "Unit", "Default", "Source"]
        )
        self.limits_table.setAlternatingRowColors(True)
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
        self.add_role_button = QPushButton("Add assignment…")
        self.remove_role_button = QPushButton("Remove assignment")
        role_buttons.addWidget(self.add_role_button)
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
        self.remove_role_button.clicked.connect(self._remove_role_assignment)
        refresh_diagnostics.clicked.connect(self._refresh_diagnostics)
        export_diagnostics.clicked.connect(self.export_redacted_configuration)
        if self._read_only:
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
        self.validate_button.setEnabled(not self._read_only)
        self.save_button.setEnabled(not self._read_only)
        self.discard_button.setEnabled(not self._read_only)
        self.approve_button.setEnabled(
            not self._base_read_only
            and access_policy.allows(Permission.APPROVE_PROFILE)
        )
        self._update_role_controls()
        self.reload()

    def _update_role_controls(self) -> None:
        allowed = (
            not self._base_read_only
            and self._access.allows(Permission.MANAGE_ROLES)
        )
        self.add_role_button.setEnabled(allowed)
        self.remove_role_button.setEnabled(allowed)
        self.roles_info.setText(
            "Identity provider: operating-system login. Unassigned accounts receive the configured "
            "default operator role. Only a service identity can change assignments in this table."
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
        mode = " • read-only for this role" if self._read_only else ""
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
            general = {key: value for key, value in self._raw.items() if key != "devices"}
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
            self._populate_limits()
            self._populate_roles()
        finally:
            self._changing = False

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
        if not self._read_only and role_path_editable and path not in self._PROTECTED_PATHS:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        else:
            item.setToolTip(1, "This field can only be changed with the Approve profile button.")
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
            self._sync_limit_from_tree(tuple(item.data(0, Qt.ItemDataRole.UserRole)), item.text(1))
            self._dirty = True
            autosave = bool(self._raw.get("application", {}).get("settings_autosave", False))
            if autosave and not self._read_only:
                self._autosave_timer.start()
                self.status.emit("Autosave pending…")

    def _apply_tree_values(self) -> dict[str, Any]:
        draft = deepcopy(self._raw)

        def walk(item: QTreeWidgetItem) -> None:
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if path is not None:
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

    def _add_role_assignment(self) -> None:
        if not self._require_access(Permission.MANAGE_ROLES, "adding an OS role assignment"):
            return
        username, ok = QInputDialog.getText(
            self, "Add role assignment", "Exact operating-system account (for example DOMAIN\\user):"
        )
        username = username.strip()
        if not ok or not username:
            return
        role, ok = QInputDialog.getItem(
            self,
            "Assigned role",
            "Role:",
            ["operator", "engineer", "service"],
            0,
            False,
        )
        if not ok:
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
        assignments[username] = [str(role)]
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

    def validate_draft(self) -> StationSettings | None:
        try:
            draft = self._apply_tree_values()
            settings = StationSettings.model_validate(draft)
        except (ConfigurationError, ValueError) as exc:
            QMessageBox.critical(self, "Validation error", str(exc))
            return None
        QMessageBox.information(self, "Validation", "The configuration is valid.")
        self.status.emit("Configuration validation passed")
        return settings

    def save_draft(self, *, silent: bool = False) -> bool:
        if not self._require_access(Permission.EDIT_SETTINGS, "saving station settings", silent=silent):
            return False
        if not self._dirty:
            return True
        try:
            draft = self._apply_tree_values()
            if (
                draft.get("access_control") != self._persisted_raw.get("access_control")
                and not self._access.allows(Permission.MANAGE_ROLES)
            ):
                raise AuthorizationError(
                    "Only a service identity can change access_control settings."
                )
            # A limit edit invalidates a previous approval and retains the safe
            # output lock.  Explicit approval is handled below.
            draft["profile"]["state"] = "unverified"
            draft["profile"]["approved_by"] = None
            draft["profile"]["approved_at"] = None
            draft["profile"]["approval_note"] = "Profile approval is required after settings changes."
            settings = self._repository.save_raw(draft)
        except (ConfigurationError, ValueError) as exc:
            if silent:
                self.status.emit(f"Autosave rejected invalid settings: {exc}")
            else:
                QMessageBox.critical(self, "Changes not saved", str(exc))
            return False
        self._raw = draft
        self._persisted_raw = deepcopy(draft)
        self._settings = settings
        self._dirty = False
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
