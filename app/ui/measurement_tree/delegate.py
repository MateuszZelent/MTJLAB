"""Small semantic accent layered over QFluentWidgets' native delegate."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyleOptionViewItem
from qfluentwidgets import TreeItemDelegate, isDarkTheme

from app.ui.measurement_tree.model import MeasurementTreeRole
from app.ui.design_system.tokens import tokens_for


class MeasurementTreeDelegate(TreeItemDelegate):
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(34, hint.height()))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        phase = index.data(MeasurementTreeRole.EXECUTION_PHASE)
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        if phase == "running":
            highlight = QColor(tokens.accent)
            highlight.setAlpha(36 if isDarkTheme() else 26)
            painter.fillRect(option.rect, highlight)
        super().paint(painter, option, index)
        if index.column() != 0:
            return
        accent = index.data(MeasurementTreeRole.ACCENT_COLOR)
        color = (
            tokens.accent
            if phase == "running"
            else tokens.success
            if phase == "applied"
            else str(accent or tokens.neutral)
        )
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(
            option.rect.left() + 4,
            option.rect.top() + 7,
            4,
            max(8, option.rect.height() - 14),
            2,
            2,
        )
        painter.restore()
