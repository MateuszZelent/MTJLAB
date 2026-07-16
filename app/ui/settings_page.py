"""Editable, validated station settings page with explicit approval workflow."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.domain.errors import ConfigurationError
from app.settings import SettingsRepository
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
        self, repository: SettingsRepository, parent: QWidget | None = None, *, read_only: bool = False
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._read_only = read_only
        self._raw: dict[str, Any] = {}
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
        self.tree = self.trees["general"]
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.reload_button = QPushButton("Reload")
        self.validate_button = QPushButton("Validate")
        self.save_button = QPushButton("Save changes")
        self.approve_button = QPushButton("Approve profile…")
        for button in (self.reload_button, self.validate_button, self.save_button, self.approve_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.reload_button.clicked.connect(self.reload)
        self.validate_button.clicked.connect(self.validate_draft)
        self.save_button.clicked.connect(self.save_draft)
        self.approve_button.clicked.connect(self.approve_profile)
        if self._read_only:
            self.validate_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.approve_button.setEnabled(False)

    def reload(self) -> None:
        self._autosave_timer.stop()
        try:
            loaded = self._repository.load()
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Configuration error", str(exc))
            return
        self._settings = loaded.settings
        self._raw = deepcopy(loaded.raw)
        self._dirty = False
        self._populate()
        self._update_subtitle()
        self.status.emit("settings.yml reloaded")

    def _update_subtitle(self) -> None:
        if self._settings is None:
            return
        state = self._settings.profile.state
        locked = "OUTPUTS LOCKED" if self._settings.outputs_locked else "profile approved"
        mode = " • SIMULATION: read-only settings" if self._read_only else ""
        self._subtitle.setText(
            f"File: {self._repository.path}  •  Profile: {self._settings.profile.name}  •  "
            f"State: {state}  •  {locked}{mode}"
        )

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
        if not self._read_only and path not in self._PROTECTED_PATHS:
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
        return draft

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
        if not self._dirty:
            return True
        try:
            draft = self._apply_tree_values()
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
        self._settings = settings
        self._dirty = False
        if not silent:
            self._populate()
        self._update_subtitle()
        self.settings_saved.emit(settings)
        self.status.emit(
            "Configuration autosaved; profile approval is required"
            if silent
            else "Configuration saved; profile approval is required"
        )
        return True

    def approve_profile(self) -> None:
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
        operator, ok = QInputDialog.getText(self, "Approve profile", "Approver name:")
        if not ok or not operator.strip():
            return
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
        draft["profile"]["approved_by"] = operator.strip()
        draft["profile"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        draft["profile"]["approval_note"] = "Profile approved by an operator in the GUI."
        try:
            settings = self._repository.save_raw(draft)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Not approved", str(exc))
            return
        self._raw = draft
        self._settings = settings
        self._populate()
        self._update_subtitle()
        self.settings_saved.emit(settings)
        self.status.emit("Profile approved")
