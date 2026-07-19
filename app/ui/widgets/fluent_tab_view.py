from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import SegmentedWidget


class FluentTabView(QWidget):
    """Fluent segmented navigation paired with a stacked page host."""

    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.navigation = SegmentedWidget(self)
        self.stack = QStackedWidget(self)
        self._routes: list[str] = []
        self._labels: list[str] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.navigation)
        layout.addWidget(self.stack, 1)

    def addTab(self, page: QWidget, label: str) -> int:
        index = self.stack.addWidget(page)
        route = f"tab-{id(self):x}-{index}"
        self._routes.append(route)
        self._labels.append(label)
        self.navigation.addItem(
            route,
            label,
            onClick=lambda _checked=False, index=index: self.setCurrentIndex(index),
        )
        if index == 0:
            self.setCurrentIndex(0)
        return index

    def count(self) -> int:
        return self.stack.count()

    def widget(self, index: int) -> QWidget | None:
        return self.stack.widget(index)

    def currentIndex(self) -> int:
        return self.stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < self.count() or not self.isTabVisible(index):
            return
        changed = index != self.stack.currentIndex()
        self.stack.setCurrentIndex(index)
        self.navigation.setCurrentItem(self._routes[index])
        if changed:
            self.currentChanged.emit(index)

    def tabText(self, index: int) -> str:
        return self._labels[index]

    def setTabToolTip(self, index: int, text: str) -> None:
        self.navigation.widget(self._routes[index]).setToolTip(text)

    def setTabEnabled(self, index: int, enabled: bool) -> None:
        self.navigation.widget(self._routes[index]).setEnabled(enabled)
        self.stack.widget(index).setEnabled(enabled)

    def isTabEnabled(self, index: int) -> bool:
        return self.navigation.widget(self._routes[index]).isEnabled()

    def setTabVisible(self, index: int, visible: bool) -> None:
        self.navigation.widget(self._routes[index]).setVisible(visible)
        if not visible and self.currentIndex() == index:
            replacement = next(
                (candidate for candidate in range(self.count()) if self.isTabVisible(candidate)),
                -1,
            )
            if replacement >= 0:
                self.setCurrentIndex(replacement)

    def isTabVisible(self, index: int) -> bool:
        return not self.navigation.widget(self._routes[index]).isHidden()
