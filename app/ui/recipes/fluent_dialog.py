"""Fluent-themed base window for complex recipe editors."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QWidget


class StationDialog(QDialog):
    """Theme-aware host for every station-owned popup or floating window.

    QFluentWidgets has no general-purpose complex form dialog. Recipe editors
    retain native modal semantics while their controls are Fluent-native and
    the host consumes the station's live light/dark surface tokens.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        }:
            self.update()
            for child in self.findChildren(QWidget):
                child.update()


class FluentRecipeDialog(StationDialog):
    """Backward-compatible semantic name for complex recipe editors."""
