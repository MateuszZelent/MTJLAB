from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import ScrollArea


class FluentPageHost(QWidget):
    def __init__(
        self,
        content: QWidget | None = None,
        parent: QWidget | None = None,
        *,
        fill_viewport: bool | None = None,
        content_factory: Callable[[], QWidget] | None = None,
    ) -> None:
        super().__init__(parent)
        if content is None and content_factory is None:
            raise ValueError("FluentPageHost requires either content or content_factory.")
        self.setObjectName("fluentPageHost")
        self._content: QWidget | None = None
        self._content_factory = content_factory
        self._fill_viewport = fill_viewport
        self.scroll_area = ScrollArea(self)
        self.scroll_area.setProperty("stationSurface", "page")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(ScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.viewport().setProperty("stationSurface", "page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)
        if content is not None:
            self._attach_content(content)

    @property
    def is_realized(self) -> bool:
        return self._content is not None

    @property
    def content(self) -> QWidget:
        return self.ensure_content()

    def ensure_content(self) -> QWidget:
        if self._content is None:
            if self._content_factory is None:
                raise RuntimeError("Lazy FluentPageHost has no content factory.")
            factory = self._content_factory
            self._content_factory = None
            self._attach_content(factory())
        return self._content

    def _attach_content(self, content: QWidget) -> None:
        self._content = content
        owns_viewport = (
            self._fill_viewport
            if self._fill_viewport is not None
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
        self.scroll_area.setWidget(content)
        content._scroll_area = self.scroll_area

    def showEvent(self, event: QShowEvent) -> None:
        self.ensure_content()
        super().showEvent(event)
