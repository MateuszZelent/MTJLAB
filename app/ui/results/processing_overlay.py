"""Fluent progress surface scoped to the Results page's current calculation."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    IndeterminateProgressRing,
    MessageBoxBase,
    SubtitleLabel,
)

from app.ui.design_system import SPACING


class ResultProcessingOverlay(MessageBoxBase):
    """Cover only the result tab, leaving station navigation and safety reachable."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("resultProcessingOverlay")
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAccessibleName("Recalculating results")
        self.buttonGroup.hide()
        self._request_id = None
        self.panel = CardWidget(self.widget)
        self.panel.setProperty("stationSurface", "dialog")
        content = QVBoxLayout(self.panel)
        content.setContentsMargins(*(SPACING["xl"],) * 4)
        content.setSpacing(SPACING["md"])
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.addWidget(self.panel)
        self.progress = IndeterminateProgressRing(self.panel, start=False)
        self.progress.setFixedSize(40, 40)
        self.progress.setStrokeWidth(4)
        self.title = SubtitleLabel(self.panel)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.description = BodyLabel(self.panel)
        self.description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.description.setWordWrap(True)
        content.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignHCenter)
        content.addWidget(self.title)
        content.addWidget(self.description)
        parent.installEventFilter(self)
        self.hide()

    def begin(self, request_id: int, title: str, description: str) -> None:
        self._request_id = request_id
        self.title.setText(title)
        self.description.setText(description)
        self._fit_parent()
        self.show()
        self.raise_()

    def finish(self, request_id: int) -> None:
        if request_id == self._request_id:
            self.clear()

    def clear(self) -> None:
        self._request_id = None
        self.hide()

    def _fit_parent(self) -> None:
        parent = self.parentWidget()
        self.setGeometry(parent.rect())
        self.widget.setFixedWidth(min(460, max(200, parent.width() - 2 * SPACING["lg"])))

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._fit_parent()
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        # Worker feedback appears immediately, without a dialog fade or nested loop.
        self.progress.start()
        QDialog.showEvent(self, event)

    def hideEvent(self, event):
        self.progress.stop()
        super().hideEvent(event)

    def reject(self) -> None:
        # Escape must not dismiss the indication while its worker is still active.
        return
