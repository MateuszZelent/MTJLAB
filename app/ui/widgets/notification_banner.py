"""Non-intrusive Fluent toast notifications.

``NotificationBanner`` is a page-scoped notification boundary. Unlike the old
inline card it occupies no layout space: messages are rendered by QFluent's
overlay layer and therefore never move a page's controls or plots.
"""

from __future__ import annotations

from PySide6.QtWidgets import QSizePolicy, QWidget
from qfluentwidgets import InfoBar, InfoBarPosition


_TOAST_METHODS = {
    "info": InfoBar.info,
    "success": InfoBar.success,
    "warning": InfoBar.warning,
    "error": InfoBar.error,
}


def show_toast(
    parent: QWidget,
    message: str,
    *,
    severity: str = "warning",
    timeout_ms: int = 10_000,
    title: str | None = None,
) -> None:
    """Present one page-scoped notification without changing its geometry."""

    normalized = severity.strip().lower()
    method = _TOAST_METHODS.get(normalized, InfoBar.warning)
    owner = parent.window() if parent.window().isVisible() else parent
    method(
        title=title or normalized.title(),
        content=message,
        isClosable=True,
        position=InfoBarPosition.TOP_RIGHT,
        duration=-1 if timeout_ms <= 0 else timeout_ms,
        parent=owner,
    )


class NotificationBanner(QWidget):
    """Zero-height host for page-scoped Fluent notifications.

    ``last_message`` is retained as lightweight page state for diagnostics and
    automated verification; it is not a second visual notification surface.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notificationToastHost")
        self.setFixedHeight(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self.last_message = ""
        self.last_severity = ""
        self.hide()

    def show_message(
        self,
        message: str,
        *,
        severity: str = "warning",
        timeout_ms: int = 10_000,
    ) -> None:
        self.last_message = message
        self.last_severity = severity.strip().lower()
        show_toast(
            self,
            message,
            severity=severity,
            timeout_ms=timeout_ms,
        )
