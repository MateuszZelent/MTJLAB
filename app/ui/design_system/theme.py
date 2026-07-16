"""Theme resolution independent from individual pages and charts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication


def effective_theme(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized in {"light", "dark"}:
        return normalized
    if normalized != "system":
        return "dark"
    application = QGuiApplication.instance()
    if application is None:
        return "dark"
    return "dark" if application.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"
