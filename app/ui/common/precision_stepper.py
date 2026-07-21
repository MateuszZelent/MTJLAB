"""Keyboard stepping for human-readable numeric ``QLineEdit`` values.

The station presents many physical values as text so their unit remains
visible (for example ``1.000 mA``).  Native spin boxes cannot express that
format, but their useful Up/Down behaviour should still be available.  This
module changes only the numeric token the operator typed; it never converts,
normalises, clamps, or otherwise changes the unit.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext
import re

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QAbstractSpinBox, QComboBox, QLineEdit, QWidget

from app.domain.quantities import QuantityError, parse_quantity


_NUMERIC_VALUE_RE = re.compile(
    r"^(?P<leading>\s*)"
    r"(?P<number>"
    r"(?P<sign>[+-]?)"
    r"(?P<mantissa>(?:\d+(?:[.,]\d*)?|[.,]\d+))"
    r"(?P<exponent>[eE][+-]?\d+)?"
    r")"
    r"(?P<unit_spacing>\s*)"
    r"(?P<unit>\S*)"
    r"(?P<trailing>\s*)$"
)


class PrecisionArrowStepper(QObject):
    """Apply spin-box-like arrow keys to plain numeric text fields.

    The number of fractional digits controls the increment.  Thus ``0.1``
    steps by ``0.1`` and ``0.001`` steps by ``0.001``.  Scientific notation
    keeps its displayed exponent and applies the same rule to its mantissa.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(watched, QLineEdit):
            return False
        if not isinstance(event, QKeyEvent) or not self._can_step(watched, event):
            return False

        direction = 1 if event.key() == Qt.Key.Key_Up else -1
        stepped_text = _step_text(watched.text(), direction)
        if stepped_text is None:
            return False

        original_text = watched.text()
        original_position = watched.cursorPosition()
        watched.setText(stepped_text)
        watched.setCursorPosition(
            _adjust_cursor_position(original_text, stepped_text, original_position)
        )
        # Text fields normally commit on focus loss.  An arrow-key increment
        # is an equally deliberate edit, so let existing per-page validation,
        # derived-value synchronisation and dirty-state wiring run now.  The
        # marker lets a range editor validate the result without replacing the
        # exact unit and decimal precision the operator used to choose a step.
        watched.setProperty("precisionArrowStepInProgress", True)
        try:
            watched.editingFinished.emit()
        finally:
            watched.setProperty("precisionArrowStepInProgress", None)
        event.accept()
        return True

    @staticmethod
    def _can_step(editor: QLineEdit, event: QKeyEvent) -> bool:
        if event.key() not in {Qt.Key.Key_Up, Qt.Key.Key_Down}:
            return False
        if editor.isReadOnly() or not editor.isEnabled():
            return False
        if editor.property("precisionArrowStepping") is False:
            return False
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False
        return not _belongs_to_native_composite(editor)


def install_precision_arrow_stepper(application: QApplication) -> None:
    """Install exactly one application-wide precision stepper."""

    stepper = getattr(application, "_station_precision_arrow_stepper", None)
    if not isinstance(stepper, PrecisionArrowStepper):
        stepper = PrecisionArrowStepper(application)
        application.installEventFilter(stepper)
        application._station_precision_arrow_stepper = stepper


def _belongs_to_native_composite(editor: QLineEdit) -> bool:
    """Leave spin boxes and editable combo boxes in control of arrow keys."""

    ancestor: QWidget | None = editor.parentWidget()
    while ancestor is not None:
        if isinstance(ancestor, (QAbstractSpinBox, QComboBox)):
            return True
        ancestor = ancestor.parentWidget()
    return False


def _step_text(text: str, direction: int) -> str | None:
    match = _NUMERIC_VALUE_RE.fullmatch(text)
    if match is None:
        return None

    number = match.group("number")
    unit = match.group("unit")
    if unit and not _has_known_unit(number, unit):
        return None

    mantissa = match.group("mantissa")
    exponent = match.group("exponent") or ""
    decimal_separator = "," if "," in mantissa else "."
    decimal_places = _decimal_places(mantissa)
    try:
        exponent_value = int(exponent[1:]) if exponent else 0
        value = Decimal(number.replace(",", "."))
        step = Decimal(1).scaleb(exponent_value - decimal_places)
        digits = sum(character.isdigit() for character in mantissa)
        with localcontext() as context:
            # Preserve the user's displayed precision even for values with
            # more digits than Decimal's default working context.
            context.prec = max(context.prec, digits + abs(exponent_value) + 4)
            stepped = value + (step if direction > 0 else -step)
        rendered_number = _format_number(
            stepped,
            decimal_places=decimal_places,
            decimal_separator=decimal_separator,
            exponent=exponent,
            force_plus=number.startswith("+") and stepped >= 0,
        )
    except (DecimalException, OverflowError, ValueError):
        return None
    return "".join(
        (
            match.group("leading"),
            rendered_number,
            match.group("unit_spacing"),
            unit,
            match.group("trailing"),
        )
    )


def _has_known_unit(number: str, unit: str) -> bool:
    """Avoid treating arbitrary numeric text (IDs, paths, codes) as a value."""

    try:
        parse_quantity(f"{number} {unit}")
    except (QuantityError, ValueError):
        return False
    return True


def _decimal_places(mantissa: str) -> int:
    separator = "," if "," in mantissa else "."
    return len(mantissa.rsplit(separator, 1)[1]) if separator in mantissa else 0


def _format_number(
    value: Decimal,
    *,
    decimal_places: int,
    decimal_separator: str,
    exponent: str,
    force_plus: bool,
) -> str:
    if exponent:
        value = value.scaleb(-int(exponent[1:]))
    rendered = f"{value:.{decimal_places}f}"
    if decimal_separator == ",":
        rendered = rendered.replace(".", ",")
    if force_plus and not rendered.startswith(("+", "-")):
        rendered = f"+{rendered}"
    return f"{rendered}{exponent}"


def _adjust_cursor_position(original: str, stepped: str, position: int) -> int:
    """Keep a caret after the numeric token aligned when its length changes."""

    original_match = _NUMERIC_VALUE_RE.fullmatch(original)
    stepped_match = _NUMERIC_VALUE_RE.fullmatch(stepped)
    if original_match is None or stepped_match is None:
        return min(position, len(stepped))
    original_number_end = len(original_match.group("leading")) + len(
        original_match.group("number")
    )
    number_delta = len(stepped_match.group("number")) - len(
        original_match.group("number")
    )
    if position > original_number_end:
        position += number_delta
    return max(0, min(position, len(stepped)))
