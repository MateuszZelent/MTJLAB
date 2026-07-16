"""Editable, validated station settings page with explicit approval workflow."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
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

    def __init__(self, repository: SettingsRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repository = repository
        self._raw: dict[str, Any] = {}
        self._settings: StationSettings | None = None
        self._changing = False
        self._dirty = False
        self._build()
        self.reload()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("Ustawienia stanowiska")
        title.setObjectName("pageTitle")
        self._subtitle = QLabel()
        self._subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self._subtitle)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Parametr", "Wartość"])
        self.tree.setAlternatingRowColors(True)
        self.tree.itemChanged.connect(self._changed)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.reload_button = QPushButton("Wczytaj ponownie")
        self.validate_button = QPushButton("Waliduj")
        self.save_button = QPushButton("Zapisz zmiany")
        self.approve_button = QPushButton("Zatwierdź profil…")
        for button in (self.reload_button, self.validate_button, self.save_button, self.approve_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.reload_button.clicked.connect(self.reload)
        self.validate_button.clicked.connect(self.validate_draft)
        self.save_button.clicked.connect(self.save_draft)
        self.approve_button.clicked.connect(self.approve_profile)

    def reload(self) -> None:
        try:
            loaded = self._repository.load()
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Błąd konfiguracji", str(exc))
            return
        self._settings = loaded.settings
        self._raw = deepcopy(loaded.raw)
        self._dirty = False
        self._populate()
        self._update_subtitle()
        self.status.emit("Wczytano settings.yml")

    def _update_subtitle(self) -> None:
        if self._settings is None:
            return
        state = self._settings.profile.state
        locked = "WYJŚCIA ZABLOKOWANE" if self._settings.outputs_locked else "profil zatwierdzony"
        self._subtitle.setText(
            f"Plik: {self._repository.path}  •  Profil: {self._settings.profile.name}  •  "
            f"Stan: {state}  •  {locked}"
        )

    def _populate(self) -> None:
        self._changing = True
        try:
            self.tree.clear()
            self._add_items(None, self._raw, ())
            self.tree.expandToDepth(2)
            self.tree.resizeColumnToContents(0)
        finally:
            self._changing = False

    def _add_items(
        self, parent: QTreeWidgetItem | None, value: Any, path: tuple[str | int, ...]
    ) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                item = QTreeWidgetItem([str(key), ""])
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._add_items(item, nested, path + (str(key),))
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                item = QTreeWidgetItem([f"[{index}]", ""])
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._add_items(item, nested, path + (index,))
            return
        item = QTreeWidgetItem(["wartość", self._format_scalar(value)])
        item.setData(0, Qt.ItemDataRole.UserRole, path)
        if path not in self._PROTECTED_PATHS:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        else:
            item.setToolTip(1, "To pole jest zmieniane wyłącznie przez przycisk „Zatwierdź profil”.")
        if parent is None:
            self.tree.addTopLevelItem(item)
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

    def _apply_tree_values(self) -> dict[str, Any]:
        draft = deepcopy(self._raw)

        def walk(item: QTreeWidgetItem) -> None:
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if path is not None:
                original = self._get_path(draft, tuple(path))
                self._set_path(draft, tuple(path), self._parse_scalar(item.text(1), original))
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))
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
                raise ConfigurationError("Wartość logiczna musi być true albo false.")
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
            QMessageBox.critical(self, "Błąd walidacji", str(exc))
            return None
        QMessageBox.information(self, "Walidacja", "Konfiguracja jest poprawna.")
        self.status.emit("Konfiguracja przeszła walidację")
        return settings

    def save_draft(self) -> None:
        try:
            draft = self._apply_tree_values()
            # A limit edit invalidates a previous approval and retains the safe
            # output lock.  Explicit approval is handled below.
            draft["profile"]["state"] = "unverified"
            draft["profile"]["approved_by"] = None
            draft["profile"]["approved_at"] = None
            draft["profile"]["approval_note"] = "Profil wymaga ponownego zatwierdzenia po zmianie ustawień."
            settings = self._repository.save_raw(draft)
        except (ConfigurationError, ValueError) as exc:
            QMessageBox.critical(self, "Nie zapisano", str(exc))
            return
        self._raw = draft
        self._settings = settings
        self._dirty = False
        self._populate()
        self._update_subtitle()
        self.settings_saved.emit(settings)
        self.status.emit("Zapisano konfigurację; profil wymaga zatwierdzenia")

    def approve_profile(self) -> None:
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Niezapisane zmiany",
                "Najpierw zapisać zmiany? Zatwierdzenie będzie dotyczyło zapisanego profilu.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
            self.save_draft()
            if self._dirty:
                return
        if self._settings is None:
            return
        operator, ok = QInputDialog.getText(self, "Zatwierdź profil", "Imię i nazwisko osoby zatwierdzającej:")
        if not ok or not operator.strip():
            return
        phrase = f"APPROVE {self._settings.profile.id}"
        confirmation, ok = QInputDialog.getText(
            self,
            "Potwierdzenie",
            f"Wpisz dokładnie: {phrase}",
        )
        if not ok or confirmation.strip() != phrase:
            QMessageBox.warning(self, "Nie zatwierdzono", "Fraza potwierdzająca jest nieprawidłowa.")
            return
        draft = deepcopy(self._raw)
        draft["profile"]["state"] = "approved"
        draft["profile"]["approved_by"] = operator.strip()
        draft["profile"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        draft["profile"]["approval_note"] = "Profil zatwierdzony w GUI przez operatora."
        try:
            settings = self._repository.save_raw(draft)
        except ConfigurationError as exc:
            QMessageBox.critical(self, "Nie zatwierdzono", str(exc))
            return
        self._raw = draft
        self._settings = settings
        self._populate()
        self._update_subtitle()
        self.settings_saved.emit(settings)
        self.status.emit("Profil został zatwierdzony")

