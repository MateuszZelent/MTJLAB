"""Reusable safety-range editor widgets shared by device pages."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, LineEdit, PrimaryPushButton, PushButton, StrongBodyLabel
from app.ui.dialogs import StationDialog

from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
    parse_quantity,
)


class LimitField(QWidget):
    """A value editor with an always-visible approved MIN/MAX range."""

    edit_requested = Signal()

    def __init__(self, editor: QWidget, minimum: object = None, maximum: object = None) -> None:
        super().__init__()
        self.editor = editor
        self._minimum_value = minimum
        self._maximum_value = maximum
        self._last_valid = editor.text() if isinstance(editor, QLineEdit) else None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(editor, 1)
        self.minimum = BodyLabel()
        self.maximum = BodyLabel()
        for label in (self.minimum, self.maximum):
            label.setObjectName("limitBadge")
            label.setMinimumWidth(88)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.minimum)
        row.addWidget(self.maximum)
        self.edit_button = PushButton("Edit…", self)
        self.edit_button.setObjectName("limitEditButton")
        self.edit_button.setToolTip(
            "Edit this safety range in a popup window. Values are validated before saving."
        )
        self.edit_button.clicked.connect(self.edit_requested)
        row.addWidget(self.edit_button)
        layout.addLayout(row)
        self.validation_warning = BodyLabel()
        self.validation_warning.setObjectName("inlineValidationWarning")
        self.validation_warning.setStyleSheet("color: #d84343; font-weight: 600;")
        self.validation_warning.setWordWrap(True)
        self.validation_warning.hide()
        layout.addWidget(self.validation_warning)
        self.set_limits(minimum, maximum)
        editing_finished = getattr(editor, "editingFinished", None)
        if editing_finished is not None:
            editing_finished.connect(self.validate_and_clamp)

    def set_limits(self, minimum: object, maximum: object) -> None:
        self._minimum_value = minimum
        self._maximum_value = maximum
        minimum_text = "NOT SET" if minimum is None else str(minimum)
        maximum_text = "NOT SET" if maximum is None else str(maximum)
        self.minimum.setText(f"MIN  {minimum_text}")
        self.maximum.setText(f"MAX  {maximum_text}")
        incomplete = minimum is None or maximum is None
        state = "undefined" if incomplete else "defined"
        self.setProperty("limitState", state)
        for label in (self.minimum, self.maximum):
            label.setProperty("limitState", state)
            label.style().unpolish(label)
            label.style().polish(label)
        self.setToolTip(
            "Approved laboratory range. The operation is rejected before SCPI is sent "
            "when a value or sweep endpoint is outside this range."
        )

    def _show_validation_warning(self, message: str) -> None:
        self.validation_warning.setText(f"Warning: {message}")
        self.validation_warning.show()
        self.editor.setStyleSheet("border: 2px solid #d84343;")

    def _clear_validation_warning(self) -> None:
        self.validation_warning.clear()
        self.validation_warning.hide()
        self.editor.setStyleSheet("")

    def _quantity_values(self) -> tuple[float, float | None, float | None, str] | None:
        if not isinstance(self.editor, QLineEdit):
            return None
        boundaries = [value for value in (self._minimum_value, self._maximum_value) if value is not None]
        if not boundaries or any(not isinstance(value, str) for value in boundaries):
            return None
        dimensions = (
            DIMENSION_VOLTAGE,
            DIMENSION_CURRENT,
            DIMENSION_FREQUENCY,
            DIMENSION_RESISTANCE,
            DIMENSION_TIME,
            DIMENSION_DBM,
        )
        for dimension in dimensions:
            try:
                parsed_bounds = [parse_quantity(value, dimension).si_value for value in boundaries]
                current = parse_quantity(self.editor.text(), dimension, require_unit=False).si_value
            except Exception:
                continue
            minimum = parsed_bounds[0] if self._minimum_value is not None else None
            maximum = parsed_bounds[-1] if self._maximum_value is not None else None
            return current, minimum, maximum, dimension
        return None

    def validate_and_clamp(self) -> bool:
        """Clamp a field on focus loss, while final safety validation remains authoritative."""

        if isinstance(self.editor, QSpinBox):
            minimum = self._minimum_value if isinstance(self._minimum_value, int) else None
            maximum = self._maximum_value if isinstance(self._maximum_value, int) else None
            value = self.editor.value()
        else:
            textual_bounds = [
                str(value).lower()
                for value in (self._minimum_value, self._maximum_value)
                if value is not None
            ]
            if any(value.startswith(">") or "n/a" in value or "no profile" in value for value in textual_bounds):
                return True
            parsed = self._quantity_values()
            if parsed is None:
                if isinstance(self.editor, QLineEdit) and self._last_valid is not None:
                    current = self.editor.text().strip()
                    if current and current.upper() != "AUTO":
                        self.editor.setText(self._last_valid)
                        self._show_validation_warning(
                            f"Invalid value or unit. Restored the previous value: {self._last_valid}."
                        )
                        return False
                return True
            value, minimum, maximum, dimension = parsed

        if minimum is not None and value < minimum:
            replacement = str(self._minimum_value)
            self._set_editor_value(self._minimum_value)
            self._show_validation_warning(f"Value was below MIN and has been changed to {replacement}.")
            return False
        if maximum is not None and value > maximum:
            replacement = str(self._maximum_value)
            self._set_editor_value(self._maximum_value)
            self._show_validation_warning(f"Value exceeded MAX and has been changed to {replacement}.")
            return False
        if isinstance(self.editor, QLineEdit):
            normalized = format_quantity_auto(value, dimension)
            self.editor.setText(normalized)
            self._last_valid = normalized
        self._clear_validation_warning()
        return True

    def _set_editor_value(self, value: object) -> None:
        if isinstance(self.editor, QSpinBox):
            self.editor.setValue(int(value))
        elif isinstance(self.editor, QLineEdit):
            self.editor.setText(str(value))
            self._last_valid = str(value)


class LimitEditDialog(StationDialog):
    """Small focused editor for one safety range."""

    def __init__(
        self,
        title: str,
        minimum: object,
        maximum: object,
        *,
        maximum_enabled: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit limits — {title}")
        self.setModal(True)
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        heading = StrongBodyLabel(title, self)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        note = BodyLabel(
            "Enter explicit units where applicable (for example: 10 mA, 67 mV, 1 MHz). "
            "The complete configuration is validated before it is saved."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.minimum = LineEdit(self)
        self.minimum.setText("" if minimum is None else str(minimum))
        self.maximum = LineEdit(self)
        self.maximum.setText("" if maximum is None else str(maximum))
        self.maximum.setEnabled(maximum_enabled)
        if not maximum_enabled:
            self.maximum.setPlaceholderText("Not applicable")
        form.addRow("Minimum", self.minimum)
        form.addRow("Maximum", self.maximum)
        layout.addLayout(form)
        warning = BodyLabel("Saving a safety limit change sets the safety profile to UNVERIFIED.", self)
        warning.setWordWrap(True)
        layout.addWidget(warning)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", self)
        save = PrimaryPushButton("Save limits", self)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        footer.addWidget(cancel)
        footer.addWidget(save)
        layout.addLayout(footer)
