"""A Fluent numeric draft with an independent display unit and explicit apply."""

import math
from decimal import Decimal, DecimalException, localcontext

from PySide6.QtCore import QEvent, QRegularExpression, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import ComboBox, LineEdit, PushButton

from app.domain.quantities import parse_quantity
from app.domain.quick_controls import _QUANTITY


class QuantityEditor(QWidget):
    textChanged = Signal(str)
    commit_requested = Signal()
    step_requested = Signal(int, bool)

    def __init__(self, text: str, dimension: str, units: tuple[str, ...], label: str, *, number_only=False):
        super().__init__()
        self.dimension = dimension
        self.is_draft = False
        self.input_was_rejected = False
        self._explicit_arrow_steps = False
        self._held_arrow = None
        self.number = LineEdit(self)
        self.number.setAccessibleName(f"{label} value")
        self.number.setPlaceholderText("Enter a number")
        if number_only:
            numeric = QRegularExpression(r"[+-]?(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?")
            self.number.setValidator(QRegularExpressionValidator(numeric, self.number))
            self.number.setToolTip("Enter a number. Select its unit beside the field.")
            self.number.inputRejected.connect(self._reject_number_input)
        self.number.installEventFilter(self)
        self.unit = ComboBox(self)
        self.unit.setAccessibleName(f"{label} unit")
        self.unit.setToolTip("Change the display unit while keeping the same physical value.")
        self.unit.addItems(list(units))
        self._units_by_scale = {self._scale(unit): unit for unit in units}
        self._display_unit = self.unit.currentText()
        self.apply = PushButton("Apply", self)
        self.apply.setAccessibleName(f"Apply {label}")
        self.apply.setAutoDefault(False)
        self.apply.setToolTip("Validate and apply this value. You can also press Enter.")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.number, 1)
        layout.addWidget(self.unit)
        layout.addWidget(self.apply)
        self.setFocusProxy(self.number)
        self.number.textEdited.connect(self._number_edited)
        self.number.textChanged.connect(lambda: self.textChanged.emit(self.text()))
        self.number.editingFinished.connect(self._mark_arrow_draft)
        self.apply.clicked.connect(self.commit_requested)
        self.unit.currentTextChanged.connect(self._unit_changed)
        self.setText(text)

    def enable_explicit_arrow_steps(self) -> None:
        self._explicit_arrow_steps = True
        self.number.setProperty("precisionArrowStepping", False)

    def stop_arrow_repeat(self) -> None:
        self._held_arrow = None

    def eventFilter(self, watched, event):
        if watched is not self.number:
            return super().eventFilter(watched, event)
        if event.type() in (QEvent.Type.FocusOut, QEvent.Type.Hide, QEvent.Type.EnabledChange):
            self.stop_arrow_repeat()
        if event.type() == QEvent.Type.KeyRelease and not event.isAutoRepeat() and event.key() == self._held_arrow:
            self.stop_arrow_repeat()
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if self._explicit_arrow_steps and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                repeated = event.isAutoRepeat()
                if event.modifiers() not in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier):
                    self.stop_arrow_repeat()
                elif not repeated or self._held_arrow == key:
                    self._held_arrow = key
                    self.step_requested.emit(1 if key == Qt.Key.Key_Up else -1, repeated)
                event.accept()
                return True
            self.stop_arrow_repeat()
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not event.isAutoRepeat():
                    self.commit_requested.emit()
                event.accept()
                return True  # Consume Enter before a floating dialog can activate a default button.
        return super().eventFilter(watched, event)

    def text(self) -> str:
        return f"{self.number.text().strip()} {self._display_unit}"

    def setText(self, text: str) -> None:
        match = _QUANTITY.fullmatch(text)
        if match is not None:
            unit = match.group("unit")
            try:
                scale = self._scale(unit)
            except ValueError:
                match = None
        if match is not None:
            unit = self._units_by_scale.setdefault(scale, unit)
            with QSignalBlocker(self.unit):
                if self.unit.findText(unit) < 0:
                    self.unit.addItem(unit)
                self.unit.setCurrentText(unit)
            self._display_unit = unit
            text = match.group("number")
        self.is_draft = False
        self.input_was_rejected = False
        self._set_number(text)

    def set_value_si(self, value_si: float) -> None:
        if not math.isfinite(value_si):
            raise ValueError("The value must be finite.")
        value = Decimal(f"{value_si:.15g}") / self._scale(self._display_unit)
        self.is_draft = False
        self.input_was_rejected = False
        self._set_number(self._compact_number(value))

    def value_si(self) -> float:
        if self.input_was_rejected:
            raise ValueError("Invalid characters rejected. Select the unit beside the field and re-enter the number.")
        return parse_quantity(self.text(), self.dimension).si_value

    def committed_text(self) -> str:
        """Validate the complete draft and remove redundant decimal zeros."""
        self.value_si()
        try:
            value = Decimal(self.number.text().strip().replace(",", "."))
            return f"{self._compact_number(value)} {self._display_unit}"
        except DecimalException as exc:
            raise ValueError("Enter a finite number in the selected unit.") from exc

    def mark_draft(self) -> None:
        self.is_draft = True

    def _number_edited(self) -> None:
        self.input_was_rejected = False
        self.mark_draft()

    def _reject_number_input(self) -> None:
        self.input_was_rejected = True
        self.mark_draft()
        self.textChanged.emit(self.text())

    def restore_input(self, number: str, unit: str, is_draft: bool, input_was_rejected: bool) -> None:
        with QSignalBlocker(self.unit):
            self.unit.setCurrentText(unit)
        self._display_unit = unit
        self.input_was_rejected = input_was_rejected
        self.is_draft = is_draft
        self._set_number(number)

    def _mark_arrow_draft(self) -> None:
        if self.number.property("precisionArrowStepInProgress"):
            self.mark_draft()

    def _set_number(self, text: str) -> None:
        with QSignalBlocker(self.number):
            if self.number.text() != text:
                self.number.setText(text)
        self.textChanged.emit(self.text())

    def _unit_changed(self, unit: str) -> None:
        number = self.number.text().strip()
        if not number:
            self._display_unit = unit
            self.textChanged.emit(self.text())
            return
        try:
            parse_quantity(self.text(), self.dimension)
            value = Decimal(number.replace(",", "."))
            with localcontext() as context:
                context.prec = max(context.prec, len(value.as_tuple().digits))
                value = value * self._scale(self._display_unit) / self._scale(unit)
            text = self._compact_number(value)
            parse_quantity(f"{text} {unit}", self.dimension)
        except (ValueError, DecimalException):
            with QSignalBlocker(self.unit):
                self.unit.setCurrentText(self._display_unit)
            return
        self._display_unit = unit
        self._set_number(text)

    def _scale(self, unit: str) -> Decimal:
        return Decimal(str(parse_quantity(f"1 {unit}", self.dimension).si_value))

    @staticmethod
    def _compact_number(value: Decimal) -> str:
        if not value:
            return "0"
        with localcontext() as context:
            context.prec = max(context.prec, len(value.as_tuple().digits))
            value = value.normalize()
        return format(value, "f" if -6 <= value.adjusted() < 12 else "g")
