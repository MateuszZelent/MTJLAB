"""Fluent dialog for choosing and managing favorite eLabFTW experiment templates."""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QTableWidgetItem,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon as FIF,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    SubtitleLabel,
    TableWidget,
)

from app.integrations.elab.config import ElabTemplateReference
from app.ui.dialogs import StationDialog


class ElabFavoritesDialog(StationDialog):
    """Dialog for picking and managing favorite eLabFTW experiment templates."""

    favorites_changed = Signal(object)

    def __init__(
        self,
        favorites: Sequence[ElabTemplateReference],
        *,
        selected_template_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Favorite eLabFTW Templates")
        self.setModal(True)
        self.resize(560, 480)
        self.setMinimumWidth(460)
        self.setMinimumHeight(380)

        self._favorites: list[ElabTemplateReference] = list(favorites)
        self._initial_selected_id = selected_template_id
        self._chosen_template: tuple[int, str] | None = None

        surface = self.use_modal_shell_content().surface
        layout = self.modal_shell.surface_layout
        layout.setSpacing(10)

        heading = SubtitleLabel("Favorite Templates", surface)
        layout.addWidget(heading)

        description = CaptionLabel(
            "Select a template from your favorites or remove unneeded shortcuts.",
            surface,
        )
        description.setObjectName("muted")
        description.setWordWrap(True)
        layout.addWidget(description)

        self.search_edit = SearchLineEdit(surface)
        self.search_edit.setPlaceholderText("Search favorites...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("Search favorites")
        self.search_edit.textChanged.connect(self._filter_table)
        layout.addWidget(self.search_edit)

        self.table = TableWidget(surface)
        self.table.setObjectName("elabFavoritesTable")
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["ID", "Template name"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.table.itemSelectionChanged.connect(self._update_button_states)
        layout.addWidget(self.table)

        self.empty_label = CaptionLabel(
            "No saved favorite templates.\n"
            "You can add a template to favorites by clicking the heart icon in the eLab tab.",
            surface,
        )
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.select_button = PrimaryPushButton("Select template", surface)
        self.select_button.setIcon(FIF.ACCEPT)
        self.select_button.clicked.connect(self._on_select_clicked)
        button_row.addWidget(self.select_button)

        self.remove_button = PushButton("Remove from favorites", surface)
        self.remove_button.setIcon(FIF.DELETE)
        self.remove_button.clicked.connect(self._on_remove_clicked)
        button_row.addWidget(self.remove_button)

        button_row.addStretch(1)

        self.cancel_button = PushButton("Cancel", surface)
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_button)

        layout.addLayout(button_row)

        self._populate_table()
        self._update_button_states()

    def _populate_table(self) -> None:
        self.table.setRowCount(len(self._favorites))
        has_items = bool(self._favorites)
        self.table.setVisible(has_items)
        self.empty_label.setVisible(not has_items)
        self.search_edit.setEnabled(has_items)

        target_row = -1
        for row, reference in enumerate(self._favorites):
            id_item = QTableWidgetItem(f"#{reference.id}")
            id_item.setData(Qt.ItemDataRole.UserRole, reference.id)
            id_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            title_item = QTableWidgetItem(reference.title)
            title_item.setData(Qt.ItemDataRole.UserRole, reference.title)

            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, title_item)

            if self._initial_selected_id is not None and reference.id == self._initial_selected_id:
                target_row = row

        if target_row >= 0:
            self.table.selectRow(target_row)
        elif self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _filter_table(self, query: str) -> None:
        text = query.strip().casefold()
        first_visible = -1
        for row in range(self.table.rowCount()):
            id_item = self.table.item(row, 0)
            title_item = self.table.item(row, 1)
            id_str = id_item.text().casefold() if id_item else ""
            title_str = title_item.text().casefold() if title_item else ""
            matches = (not text) or (text in id_str) or (text in title_str)
            self.table.setRowHidden(row, not matches)
            if matches and first_visible < 0:
                first_visible = row

        if first_visible >= 0:
            current_row = self.table.currentRow()
            if current_row < 0 or self.table.isRowHidden(current_row):
                self.table.selectRow(first_visible)

        self._update_button_states()

    def _current_selection(self) -> tuple[int, str] | None:
        row = self.table.currentRow()
        if row < 0 or self.table.isRowHidden(row):
            return None
        id_item = self.table.item(row, 0)
        title_item = self.table.item(row, 1)
        if id_item is None or title_item is None:
            return None
        template_id = id_item.data(Qt.ItemDataRole.UserRole)
        template_title = title_item.data(Qt.ItemDataRole.UserRole)
        return (int(template_id), str(template_title))

    def _update_button_states(self) -> None:
        has_sel = self._current_selection() is not None
        self.select_button.setEnabled(has_sel)
        self.remove_button.setEnabled(has_sel)

    def _on_select_clicked(self) -> None:
        sel = self._current_selection()
        if sel is not None:
            self._chosen_template = sel
            self.accept()

    def _on_item_double_clicked(self, _item: QTableWidgetItem) -> None:
        self._on_select_clicked()

    def _on_remove_clicked(self) -> None:
        sel = self._current_selection()
        if sel is None:
            return
        tid, _ = sel
        self._favorites = [ref for ref in self._favorites if ref.id != tid]
        self.favorites_changed.emit(tuple(self._favorites))
        self._populate_table()
        self._filter_table(self.search_edit.text())
        self._update_button_states()

    def selected_template(self) -> tuple[int, str] | None:
        """Return the template selected by the user, or None if cancelled."""
        return self._chosen_template

    def updated_favorites(self) -> tuple[ElabTemplateReference, ...]:
        """Return the current set of favorites after any removals."""
        return tuple(self._favorites)
