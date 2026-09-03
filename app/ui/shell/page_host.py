from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import ScrollArea


class FluentPageHost(QWidget):
    def __init__(
        self,
        content: QWidget,
        parent: QWidget | None = None,
        *,
        fill_viewport: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("fluentPageHost")
        self.content = content
        owns_viewport = (
            fill_viewport
            if fill_viewport is not None
            else getattr(content, "owns_viewport", False)
        )
        # Ignore desktop-oriented width hints so every page is laid out at the
        # actual Fluent viewport width. Workspace pages expand vertically to fill
        # the available viewport so internal splitters and trees own scrolling;
        # document pages retain their preferred height for natural scrolling.
        content.setMinimumWidth(0)
        content.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding if owns_viewport else QSizePolicy.Policy.Preferred,
        )
        self.scroll_area = ScrollArea(self)
        self.scroll_area.setProperty("stationSurface", "page")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(ScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setWidget(content)
        self.scroll_area.viewport().setProperty("stationSurface", "page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)
