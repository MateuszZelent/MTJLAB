"""Non-modal inline operator notification."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton


class NotificationBanner(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("notificationBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 7, 7)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setAccessibleName("Dismiss notification")
        self.close_button.clicked.connect(self.hide)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.close_button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, message: str, *, severity: str = "warning", timeout_ms: int = 10_000) -> None:
        self.setProperty("severity", severity)
        self.style().unpolish(self)
        self.style().polish(self)
        self.label.setText(message)
        self.setAccessibleName(f"{severity.title()} notification")
        self.setAccessibleDescription(message)
        self.show()
        if timeout_ms > 0:
            self._timer.start(timeout_ms)
        else:
            self._timer.stop()
