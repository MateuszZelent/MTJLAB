"""Generic device-parameter selection dialogs for the recipe workspace."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QListWidgetItem, QHBoxLayout, QVBoxLayout, QWidget,
)
from qfluentwidgets import BodyLabel, ComboBox, ListWidget, PrimaryPushButton, PushButton
from app.ui.dialogs import StationMessageBox as QMessageBox

from collections.abc import Mapping, Sequence

from app.recipes.parameter_registry import SWEEPABLE_PARAMETERS
from app.ui.recipes.fluent_dialog import FluentRecipeDialog


class DeviceParameterDialog(FluentRecipeDialog):
    """Let an operator select every controllable field for one instrument."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        initial_device: str | None = None,
        definitions: Sequence[Mapping[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        source_definitions = (
            SWEEPABLE_PARAMETERS if definitions is None else definitions
        )
        self._definitions = tuple(
            dict(definition) for definition in source_definitions
        )
        self.setWindowTitle("Add device controls")
        self.setMinimumSize(460, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(BodyLabel("Choose an instrument, then select one or more fields to sweep."))
        self.device = ComboBox(self)
        devices = tuple(dict.fromkeys(definition["device"] for definition in self._definitions))
        self.device.addItems(devices)
        layout.addWidget(self.device)
        self.operation = ComboBox(self)
        self.operation.addItem("Create dynamic sweep", userData="sweep")
        self.operation.addItem("Set one fixed value", userData="fixed")
        layout.addWidget(self.operation)
        self.fields = ListWidget(self)
        layout.addWidget(self.fields, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.open_button = PrimaryPushButton("Open point generators", self)
        self.cancel_button = PushButton("Cancel", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)
        self.device.currentTextChanged.connect(self._refresh)
        self.open_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.open_button.setEnabled(bool(devices))
        if initial_device is not None:
            self.device.setCurrentText(initial_device)
        self._refresh()

    def _refresh(self) -> None:
        self.fields.clear()
        for definition in self._definitions:
            if definition["device"] != self.device.currentText():
                continue
            item = QListWidgetItem(definition["label"])
            item.setData(Qt.ItemDataRole.UserRole, definition)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.fields.addItem(item)

    def selected(self) -> tuple[dict[str, str], ...]:
        return tuple(
            dict(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.fields.count())
            if (item := self.fields.item(index)).checkState() == Qt.CheckState.Checked
        )

    def operation_kind(self) -> str:
        return str(self.operation.currentData())

    def accept(self) -> None:
        if not self.selected():
            QMessageBox.information(self, "Device controls", "Select at least one controllable field.")
            return
        super().accept()
