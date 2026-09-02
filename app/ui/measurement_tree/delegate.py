"""Small semantic accent layered over QFluentWidgets' native delegate."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyleOptionViewItem
from qfluentwidgets import TreeItemDelegate

from app.ui.measurement_tree.model import MeasurementTreeRole


class MeasurementTreeDelegate(TreeItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        super().paint(painter, option, index)
        phase = index.data(MeasurementTreeRole.EXECUTION_PHASE)
        if phase not in {"running", "applied"} or index.column() != 0:
            return
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2563eb" if phase == "running" else "#16a34a"))
        painter.drawRoundedRect(option.rect.left() + 4, option.rect.top() + 7, 3, max(6, option.rect.height() - 14), 1, 1)
        painter.restore()
