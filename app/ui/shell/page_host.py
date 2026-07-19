from __future__ import annotations

from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget


class FluentPageHost(QWidget):
    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fluentPageHost")
        self.content = content
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setProperty("stationSurface", "page")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(content)
        self.scroll_area.viewport().setProperty("stationSurface", "page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)
