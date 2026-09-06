"""Interactive visual matrix / grid widget for sample devices."""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import Action, FluentIcon, RoundMenu, SimpleCardWidget

from app.inventory.models import ActiveSampleTarget, Sample, SampleRunRecord


class SampleMatrixWidget(SimpleCardWidget):
    """Visual interactive grid displaying sample rows, columns, labels, and device states."""

    cell_selected = Signal(str, str)  # row, col
    cell_activated = Signal(str, str)  # row, col (double click -> set as active target)
    col_rename_requested = Signal(str, str)  # col_key, current_label
    row_rename_requested = Signal(str, str)  # row_key, current_label
    col_add_requested = Signal(str, str)  # ref_col, "before"|"after"
    row_add_requested = Signal(str, str)  # ref_row, "above"|"below"
    col_delete_requested = Signal(str)  # col_key
    row_delete_requested = Signal(str)  # row_key
    cell_state_change_requested = Signal(str, str, str)  # row_key, col_key, new_state
    batch_cell_state_change_requested = Signal(list, str)  # list of (row, col), new_state
    row_state_change_requested = Signal(str, str)  # row_key, new_state
    col_state_change_requested = Signal(str, str)  # col_key, new_state
    explore_runs_requested = Signal(str, str)  # row_key, col_key
    renumber_rows_requested = Signal()  # request renumbering dialog for all rows

    # State colors (semi-transparent backgrounds for light/dark compatibility)
    _STATE_COLORS = {
        "untested": None,
        "completed": QColor(34, 197, 94, 120),  # vibrant green for All Measurements Complete
        "burned": QColor(220, 38, 38, 130),     # prominent red for Burned / Damaged
        "measured": QColor(30, 102, 245, 45),   # blue tint
        "good": QColor(64, 160, 43, 60),        # soft green tint
        "shorted": QColor(210, 15, 57, 75),     # crimson tint
        "open": QColor(223, 142, 29, 70),       # amber tint
        "degraded": QColor(136, 57, 239, 60),   # purple tint
    }

    _ACTIVE_BG = QColor(30, 102, 245, 90)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sample: Sample | None = None
        self._active_target: ActiveSampleTarget | None = None
        self._run_counts: Counter[tuple[str, str]] = Counter()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        self.table = QTableWidget(self)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(38)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setMinimumSectionSize(28)
        self.table.setStyleSheet(
            "QTableWidget {"
            "border: none;"
            "background: transparent;"
            "gridline-color: palette(mid);"
            "font-size: 11px;"
            "}"
            "QTableWidget::item {"
            "padding: 2px 3px;"
            "border-radius: 3px;"
            "}"
            "QTableWidget::item:selected {"
            "background-color: rgba(30, 102, 245, 0.25);"
            "outline: none;"
            "}"
            "QHeaderView::section {"
            "font-size: 11px;"
            "padding: 2px 4px;"
            "font-weight: 500;"
            "}"
        )

        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.horizontalHeader().sectionDoubleClicked.connect(
            self._on_col_header_double_clicked
        )
        self.table.verticalHeader().sectionDoubleClicked.connect(
            self._on_row_header_double_clicked
        )
        self.table.horizontalHeader().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.horizontalHeader().customContextMenuRequested.connect(
            self._on_col_header_context_menu
        )
        self.table.verticalHeader().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.verticalHeader().customContextMenuRequested.connect(
            self._on_row_header_context_menu
        )
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        layout.addWidget(self.table)

    def set_sample(
        self,
        sample: Sample | None,
        run_records: Sequence[SampleRunRecord] = (),
        active_target: ActiveSampleTarget | None = None,
    ) -> None:
        """Populate the grid with rows, columns, labels and cell states."""
        self._sample = sample
        self._active_target = active_target
        self._run_counts = Counter(
            (str(r.row), str(r.col)) for r in run_records
        )

        if sample is None or not sample.rows or not sample.cols:
            self.table.clear()
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            return

        self.table.blockSignals(True)
        self.table.clear()
        self.table.setRowCount(len(sample.rows))
        self.table.setColumnCount(len(sample.cols))

        # Setup column headers
        col_headers: list[str] = []
        for c in sample.cols:
            col_label = sample.col_labels.get(c, "")
            header = f"Col {c}\n{col_label}" if col_label else f"Col {c}"
            col_headers.append(header)
        self.table.setHorizontalHeaderLabels(col_headers)

        # Setup row headers
        row_headers: list[str] = []
        for r in sample.rows:
            row_label = sample.row_labels.get(r, "")
            header = f"Row {r}\n{row_label}" if row_label else f"Row {r}"
            row_headers.append(header)
        self.table.setVerticalHeaderLabels(row_headers)

        # Populate cells
        is_target_sample = (
            active_target is not None
            and active_target.is_active
            and active_target.sample_id == sample.sample_id
        )

        for row_idx, r in enumerate(sample.rows):
            for col_idx, c in enumerate(sample.cols):
                label = sample.cell_label(r, c)
                state = sample.cell_state(r, c)
                run_count = self._run_counts.get((r, c), 0)

                is_active = (
                    is_target_sample
                    and active_target is not None
                    and str(active_target.row) == r
                    and str(active_target.col) == c
                )

                lines = []
                if is_active:
                    lines.append("★ ACTIVE")
                lines.append(f"R{r}:C{c}")
                if label and label != f"R{r}C{c}":
                    lines.append(label)

                # Status label badge
                if state == "burned":
                    lines.append("🔥 BURNED")
                elif state == "completed":
                    lines.append("✔ COMPLETED")
                elif state != "untested":
                    lines.append(f"[{state.upper()}]")

                # Sweep / run count
                if run_count > 0:
                    lines.append(f"({run_count} runs)")

                item_text = "\n".join(lines)
                item = QTableWidgetItem(item_text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                font = item.font()
                font.setPointSize(8)
                if is_active or state in {"burned", "completed"}:
                    font.setBold(True)
                item.setFont(font)

                # Background color
                if is_active:
                    item.setBackground(self._ACTIVE_BG)
                elif state in self._STATE_COLORS and self._STATE_COLORS[state] is not None:
                    item.setBackground(self._STATE_COLORS[state])

                item.setData(Qt.ItemDataRole.UserRole, (r, c))
                self.table.setItem(row_idx, col_idx, item)

        self.table.blockSignals(False)

    def select_cell(self, row: str, col: str) -> None:
        if self._sample is None:
            return
        try:
            r_idx = self._sample.rows.index(str(row))
            c_idx = self._sample.cols.index(str(col))
            self.table.setCurrentCell(r_idx, c_idx)
        except ValueError:
            pass

    def get_selected_coordinates(self) -> tuple[str, str] | None:
        selected_items = self.table.selectedItems()
        if not selected_items:
            return None
        data = selected_items[0].data(Qt.ItemDataRole.UserRole)
        if data and isinstance(data, (tuple, list)) and len(data) == 2:
            return str(data[0]), str(data[1])
        return None

    def _on_cell_clicked(self, row_idx: int, col_idx: int) -> None:
        item = self.table.item(row_idx, col_idx)
        if item is not None:
            data = item.data(Qt.ItemDataRole.UserRole)
            if data and len(data) == 2:
                self.cell_selected.emit(str(data[0]), str(data[1]))

    def _on_cell_double_clicked(self, row_idx: int, col_idx: int) -> None:
        item = self.table.item(row_idx, col_idx)
        if item is not None:
            data = item.data(Qt.ItemDataRole.UserRole)
            if data and len(data) == 2:
                self.cell_activated.emit(str(data[0]), str(data[1]))

    def _on_col_header_double_clicked(self, logical_index: int) -> None:
        if self._sample is None or logical_index >= len(self._sample.cols):
            return
        col_key = self._sample.cols[logical_index]
        current_label = self._sample.col_labels.get(col_key, "")
        self.col_rename_requested.emit(col_key, current_label)

    def _on_row_header_double_clicked(self, logical_index: int) -> None:
        if self._sample is None or logical_index >= len(self._sample.rows):
            return
        row_key = self._sample.rows[logical_index]
        current_label = self._sample.row_labels.get(row_key, "")
        self.row_rename_requested.emit(row_key, current_label)

    def _on_col_header_context_menu(self, pos: QPoint) -> None:
        if self._sample is None:
            return
        col_idx = self.table.horizontalHeader().logicalIndexAt(pos)
        if col_idx < 0 or col_idx >= len(self._sample.cols):
            return
        col_key = self._sample.cols[col_idx]
        current_label = self._sample.col_labels.get(col_key, "")

        menu = RoundMenu(parent=self)
        menu.addAction(
            Action(
                FluentIcon.EDIT,
                f"Rename Column {col_key} ('{current_label or col_key}')...",
                triggered=lambda: self.col_rename_requested.emit(col_key, current_label),
            )
        )
        menu.addSeparator()
        col_state_menu = RoundMenu(f"Mark Entire Column {col_key} As", menu)
        col_state_menu.setIcon(FluentIcon.FLAG)
        col_state_menu.addAction(
            Action(
                "✔ Completed (Green)",
                triggered=lambda: self.col_state_change_requested.emit(col_key, "completed"),
            )
        )
        col_state_menu.addAction(
            Action(
                "🔥 Burned / Damaged (Red)",
                triggered=lambda: self.col_state_change_requested.emit(col_key, "burned"),
            )
        )
        col_state_menu.addAction(
            Action(
                "Untested (Reset)",
                triggered=lambda: self.col_state_change_requested.emit(col_key, "untested"),
            )
        )
        menu.addMenu(col_state_menu)
        menu.addSeparator()
        menu.addAction(
            Action(
                FluentIcon.ADD,
                "Insert Column to the Left",
                triggered=lambda: self.col_add_requested.emit(col_key, "before"),
            )
        )
        menu.addAction(
            Action(
                FluentIcon.ADD,
                "Insert Column to the Right",
                triggered=lambda: self.col_add_requested.emit(col_key, "after"),
            )
        )
        menu.addSeparator()
        menu.addAction(
            Action(
                FluentIcon.DELETE,
                f"Delete Column {col_key}",
                triggered=lambda: self.col_delete_requested.emit(col_key),
            )
        )
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _on_row_header_context_menu(self, pos: QPoint) -> None:
        if self._sample is None:
            return
        row_idx = self.table.verticalHeader().logicalIndexAt(pos)
        if row_idx < 0 or row_idx >= len(self._sample.rows):
            return
        row_key = self._sample.rows[row_idx]
        current_label = self._sample.row_labels.get(row_key, "")

        menu = RoundMenu(parent=self)
        menu.addAction(
            Action(
                FluentIcon.EDIT,
                f"Rename Row {row_key} ('{current_label or row_key}')...",
                triggered=lambda: self.row_rename_requested.emit(row_key, current_label),
            )
        )
        menu.addAction(
            Action(
                FluentIcon.SYNC,
                "Renumber All Rows (Change Start Index)...",
                triggered=self.renumber_rows_requested.emit,
            )
        )
        menu.addSeparator()
        row_state_menu = RoundMenu(f"Mark Entire Row {row_key} As", menu)
        row_state_menu.setIcon(FluentIcon.FLAG)
        row_state_menu.addAction(
            Action(
                "✔ Completed (Green)",
                triggered=lambda: self.row_state_change_requested.emit(row_key, "completed"),
            )
        )
        row_state_menu.addAction(
            Action(
                "🔥 Burned / Damaged (Red)",
                triggered=lambda: self.row_state_change_requested.emit(row_key, "burned"),
            )
        )
        row_state_menu.addAction(
            Action(
                "Untested (Reset)",
                triggered=lambda: self.row_state_change_requested.emit(row_key, "untested"),
            )
        )
        menu.addMenu(row_state_menu)
        menu.addSeparator()
        menu.addAction(
            Action(
                FluentIcon.ADD,
                "Insert Row Above",
                triggered=lambda: self.row_add_requested.emit(row_key, "above"),
            )
        )
        menu.addAction(
            Action(
                FluentIcon.ADD,
                "Insert Row Below",
                triggered=lambda: self.row_add_requested.emit(row_key, "below"),
            )
        )
        menu.addSeparator()
        menu.addAction(
            Action(
                FluentIcon.DELETE,
                f"Delete Row {row_key}",
                triggered=lambda: self.row_delete_requested.emit(row_key),
            )
        )
        menu.exec(self.table.verticalHeader().mapToGlobal(pos))

    def _on_table_context_menu(self, pos: QPoint) -> None:
        if self._sample is None:
            return

        # Collect selected cells
        selected_coords: list[tuple[str, str]] = []
        for it in self.table.selectedItems():
            data = it.data(Qt.ItemDataRole.UserRole)
            if data and len(data) == 2:
                selected_coords.append((str(data[0]), str(data[1])))

        # Also check item under cursor
        clicked_item = self.table.itemAt(pos)
        clicked_coord: tuple[str, str] | None = None
        if clicked_item is not None:
            cdata = clicked_item.data(Qt.ItemDataRole.UserRole)
            if cdata and len(cdata) == 2:
                clicked_coord = (str(cdata[0]), str(cdata[1]))

        if not selected_coords and clicked_coord is None:
            return

        # If clicked item is not in current selection, restrict to clicked item
        if clicked_coord and clicked_coord not in selected_coords:
            selected_coords = [clicked_coord]

        menu = RoundMenu(parent=self)

        # If single cell, provide Set as Active Target and Explore in Tree
        if len(selected_coords) == 1:
            r, c = selected_coords[0]
            menu.addAction(
                Action(
                    FluentIcon.TAG,
                    f"Set R{r}:C{c} as Active Target",
                    triggered=lambda: self.cell_activated.emit(r, c),
                )
            )
            menu.addAction(
                Action(
                    FluentIcon.HISTORY,
                    f"Explore Sweeps for R{r}:C{c} in Tree...",
                    triggered=lambda: self.explore_runs_requested.emit(r, c),
                )
            )
            menu.addSeparator()

        count_desc = (
            f"{len(selected_coords)} Selected Devices"
            if len(selected_coords) > 1
            else f"R{selected_coords[0][0]}:C{selected_coords[0][1]}"
        )

        # Primary user requested quick actions: Completed and Burned
        menu.addAction(
            Action(
                FluentIcon.ACCEPT,
                f"✔ Mark {count_desc} as Completed (Green)",
                triggered=lambda: self._apply_batch_state(selected_coords, "completed"),
            )
        )
        menu.addAction(
            Action(
                FluentIcon.CANCEL,
                f"🔥 Mark {count_desc} as Burned / Damaged (Red)",
                triggered=lambda: self._apply_batch_state(selected_coords, "burned"),
            )
        )
        menu.addSeparator()

        state_menu = RoundMenu("More Device States", menu)
        state_menu.setIcon(FluentIcon.FLAG)
        for state, label in (
            ("untested", "Untested (Default)"),
            ("good", "Good (Functional)"),
            ("measured", "Measured"),
            ("shorted", "Shorted (Defect)"),
            ("open", "Open (Disconnected)"),
            ("degraded", "Degraded / High Resistance"),
        ):
            state_menu.addAction(
                Action(
                    label,
                    triggered=lambda checked=False, s=state: self._apply_batch_state(selected_coords, s),
                )
            )
        menu.addMenu(state_menu)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _apply_batch_state(self, coords: list[tuple[str, str]], state: str) -> None:
        if len(coords) == 1:
            self.cell_state_change_requested.emit(coords[0][0], coords[0][1], state)
        else:
            self.batch_cell_state_change_requested.emit(coords, state)
