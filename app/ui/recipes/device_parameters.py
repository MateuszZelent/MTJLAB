"""Generic device-parameter selection dialogs for the recipe workspace."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QVBoxLayout, QWidget,
)

from collections.abc import Mapping, Sequence

from app.recipes.parameter_registry import SWEEPABLE_PARAMETERS


class DeviceParameterDialog(QDialog):
    """Let an operator select every controllable field for one instrument."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        initial_device: str | None = None,
        definitions: Sequence[Mapping[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._definitions = tuple(dict(definition) for definition in (definitions or SWEEPABLE_PARAMETERS))
        self.setWindowTitle("Add device controls")
        self.setMinimumSize(460, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose an instrument, then select one or more fields to sweep."))
        self.device = QComboBox()
        devices = tuple(dict.fromkeys(definition["device"] for definition in self._definitions))
        self.device.addItems(devices)
        layout.addWidget(self.device)
        self.operation = QComboBox()
        self.operation.addItem("Create dynamic sweep", "sweep")
        self.operation.addItem("Set one fixed value", "fixed")
        layout.addWidget(self.operation)
        self.fields = QListWidget()
        layout.addWidget(self.fields, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Open point generators")
        layout.addWidget(buttons)
        self.device.currentTextChanged.connect(self._refresh)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
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
