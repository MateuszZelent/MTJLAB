"""Fluent searchable combo box with real-time popup filtering."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import ComboBox, MenuAnimationType, SearchLineEdit
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu


class SearchableComboBoxMenu(ComboBoxMenu):
    """Dropdown menu equipped with an embedded search filter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.hBoxLayout.removeWidget(self.view)

        self.content_layout = QVBoxLayout()
        self.content_layout.setContentsMargins(6, 6, 6, 6)
        self.content_layout.setSpacing(6)

        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText("Search templates...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedHeight(30)
        self.search_edit.installEventFilter(self)

        self.content_layout.addWidget(self.search_edit)
        self.content_layout.addWidget(self.view)
        self.hBoxLayout.addLayout(self.content_layout)

        self.search_edit.textChanged.connect(self._on_search_changed)
        self.search_edit.returnPressed.connect(self._on_return_pressed)

    def exec(
        self,
        pos,
        ani: bool = True,
        aniType: MenuAnimationType = MenuAnimationType.DROP_DOWN,
    ):
        """Explicitly override exec to avoid PySide6 Shiboken dispatch to QMenu.exec."""
        return ComboBoxMenu.exec(self, pos, ani=ani, aniType=aniType)

    def _on_search_changed(self, text: str) -> None:
        query = text.strip().casefold()
        first_visible = None
        for i in range(self.view.count()):
            item = self.view.item(i)
            match = (not query) or (query in item.text().casefold())
            item.setHidden(not match)
            if match and first_visible is None:
                first_visible = item
        if first_visible:
            self.view.setCurrentItem(first_visible)

    def _on_return_pressed(self) -> None:
        current = self.view.currentItem()
        if current and not current.isHidden():
            self.view.itemClicked.emit(current)
            self.close()

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        if obj is self.search_edit and event.type() == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event  # type: ignore[assignment]
            key = key_event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._navigate_visible(key == Qt.Key.Key_Down)
                return True
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def _navigate_visible(self, forward: bool) -> None:
        current_row = self.view.currentRow()
        count = self.view.count()
        if count == 0:
            return
        step = 1 if forward else -1
        idx = current_row + step if current_row >= 0 else (0 if forward else count - 1)
        while 0 <= idx < count:
            item = self.view.item(idx)
            if not item.isHidden():
                self.view.setCurrentItem(item)
                return
            idx += step

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.search_edit.clear()
        QTimer.singleShot(0, self.search_edit.setFocus)


class SearchableComboBox(ComboBox):
    """Fluent ComboBox that displays an embedded search filter when expanded."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self._search_placeholder = "Search templates..."

    def setSearchPlaceholder(self, placeholder: str) -> None:
        self._search_placeholder = placeholder

    def _createComboMenu(self) -> SearchableComboBoxMenu:
        menu = SearchableComboBoxMenu(self)
        menu.search_edit.setPlaceholderText(self._search_placeholder)
        return menu

    def keyPressEvent(self, event: QKeyEvent) -> None:
        text = event.text()
        if text and text.isprintable() and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            self._showComboMenu()
            if isinstance(self.dropMenu, SearchableComboBoxMenu):
                self.dropMenu.search_edit.setText(text)
            event.accept()
            return
        super().keyPressEvent(event)
