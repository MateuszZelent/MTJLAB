"""Hierarchical measurement tree widget for sample devices and measurement sweeps."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    PushButton,
    RoundMenu,
    SearchLineEdit,
    SubtitleLabel,
    ToolButton,
    TreeWidget,
)

from app.inventory.models import Sample, SampleRunRecord


class MeasurementTreeWidget(QWidget):
    """Hierarchical and flat measurement browser tree with grouping, search, and multi-selection."""

    run_selected = Signal(object)  # SampleRunRecord
    runs_checked_changed = Signal(list)  # list[SampleRunRecord]
    open_in_results_requested = Signal(str)  # run_path

    VIEW_BY_DEVICE = "Group by Device / Pillar"
    VIEW_BY_RECIPE = "Group by Recipe"
    VIEW_BY_DATE = "Group by Date"
    VIEW_FLAT = "Flat List (Chronological)"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runs: list[SampleRunRecord] = []
        self._sample: Sample | None = None
        self._updating_checks = False
        self._selected_run: SampleRunRecord | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Header toolbar
        header_bar = QHBoxLayout()
        header_bar.setContentsMargins(2, 2, 2, 2)
        header_bar.setSpacing(6)

        header_title = SubtitleLabel("Measurements Tree", self)
        header_title.setStyleSheet("font-size: 13px; font-weight: 600;")
        header_bar.addWidget(header_title)
        header_bar.addStretch(1)

        self.count_badge = CaptionLabel("0 sweeps", self)
        self.count_badge.setStyleSheet(
            "background: palette(midlight); padding: 3px 8px; border-radius: 4px; font-weight: 600;"
        )
        header_bar.addWidget(self.count_badge)

        self.expand_all_btn = ToolButton(FluentIcon.DOWN, self)
        self.expand_all_btn.setToolTip("Expand All")
        self.expand_all_btn.clicked.connect(self._expand_all)
        header_bar.addWidget(self.expand_all_btn)

        self.collapse_all_btn = ToolButton(FluentIcon.UP, self)
        self.collapse_all_btn.setToolTip("Collapse All")
        self.collapse_all_btn.clicked.connect(self._collapse_all)
        header_bar.addWidget(self.collapse_all_btn)

        layout.addLayout(header_bar)

        # Search and grouping bar
        filter_bar = QHBoxLayout()
        filter_bar.setContentsMargins(2, 0, 2, 0)
        filter_bar.setSpacing(6)

        self.search_input = SearchLineEdit(self)
        self.search_input.setPlaceholderText("Search recipe, coordinate, ID...")
        self.search_input.textChanged.connect(self._on_filter_changed)
        filter_bar.addWidget(self.search_input, 2)

        self.grouping_combo = ComboBox(self)
        self.grouping_combo.addItems([
            self.VIEW_BY_DEVICE,
            self.VIEW_BY_RECIPE,
            self.VIEW_BY_DATE,
            self.VIEW_FLAT,
        ])
        self.grouping_combo.currentIndexChanged.connect(self._rebuild_tree)
        filter_bar.addWidget(self.grouping_combo, 2)

        self.status_combo = ComboBox(self)
        self.status_combo.addItems([
            "All Statuses",
            "Completed Only",
            "Failed / Aborted",
            "With eLab Link",
        ])
        self.status_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_bar.addWidget(self.status_combo, 1)

        layout.addLayout(filter_bar)

        # Multi-select quick toolbar
        multi_bar = QHBoxLayout()
        multi_bar.setContentsMargins(4, 0, 4, 0)
        multi_bar.setSpacing(6)

        self.checked_count_label = CaptionLabel("0 selected for comparison", self)
        self.checked_count_label.setStyleSheet("color: palette(highlight); font-weight: 600;")
        multi_bar.addWidget(self.checked_count_label)
        multi_bar.addStretch(1)

        self.select_all_btn = PushButton("Select All", self)
        self.select_all_btn.setFixedHeight(26)
        self.select_all_btn.clicked.connect(self._check_all)
        multi_bar.addWidget(self.select_all_btn)

        self.clear_selection_btn = PushButton("Clear", self)
        self.clear_selection_btn.setFixedHeight(26)
        self.clear_selection_btn.clicked.connect(self._uncheck_all)
        multi_bar.addWidget(self.clear_selection_btn)

        layout.addLayout(multi_bar)

        # The Tree Widget
        self.tree = TreeWidget(self)
        self.tree.setHeaderLabels(["Sweep / Device", "Date (UTC)", "Pts", "Status", "eLab"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        self.tree.currentItemChanged.connect(self._on_current_item_changed)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

        layout.addWidget(self.tree, 1)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def set_runs(self, runs: Sequence[SampleRunRecord], sample: Sample | None = None) -> None:
        """Populate the tree with measurement runs."""
        self._runs = list(runs)
        self._sample = sample
        self.count_badge.setText(f"{len(self._runs)} sweeps")
        self._rebuild_tree()

    def clear(self) -> None:
        """Clear all runs and items."""
        self._runs.clear()
        self._sample = None
        self._selected_run = None
        self.tree.clear()
        self.count_badge.setText("0 sweeps")
        self.checked_count_label.setText("0 selected for comparison")

    def filter_by_device(self, row: str, col: str) -> None:
        """Filter or locate measurements belonging to a specific row and col."""
        self.grouping_combo.setCurrentText(self.VIEW_BY_DEVICE)
        self.search_input.setText(f"R{row}:C{col}")

    def get_selected_run(self) -> SampleRunRecord | None:
        """Return the currently highlighted/selected run."""
        return self._selected_run

    def get_checked_runs(self) -> list[SampleRunRecord]:
        """Return all runs whose leaf checkboxes are checked."""
        checked: list[SampleRunRecord] = []

        def walk(item: QTreeWidgetItem) -> None:
            run = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(run, SampleRunRecord) and item.checkState(0) == Qt.CheckState.Checked:
                checked.append(run)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return checked

    # -------------------------------------------------------------------------
    # Internal Tree Population & Grouping
    # -------------------------------------------------------------------------

    def _matches_filter(self, run: SampleRunRecord, query: str, status_filter: str) -> bool:
        if query:
            q = query.lower()
            coord_str = f"r{run.row}:c{run.col}".lower()
            plain_coord = f"{run.row},{run.col}".lower()
            recipe = run.recipe_name.lower()
            label = run.device_label.lower()
            date = run.created_at_utc.lower()
            path = run.run_path.lower()
            if not (
                q in coord_str
                or q in plain_coord
                or q in recipe
                or q in label
                or q in date
                or q in path
            ):
                return False

        if status_filter == "Completed Only":
            if run.status.lower() != "completed":
                return False
        elif status_filter == "Failed / Aborted":
            if run.status.lower() in ("completed", "active", "running"):
                return False
        elif status_filter == "With eLab Link":
            if not (run.elab_experiment_id or run.elab_url or run.elab_status == "uploaded"):
                return False

        return True

    def _rebuild_tree(self) -> None:
        self._updating_checks = True
        self.tree.clear()

        query = self.search_input.text().strip()
        status_filter = self.status_combo.currentText()

        visible_runs = [r for r in self._runs if self._matches_filter(r, query, status_filter)]
        mode = self.grouping_combo.currentText()

        if mode == self.VIEW_BY_DEVICE:
            self._build_grouped_by_device(visible_runs)
        elif mode == self.VIEW_BY_RECIPE:
            self._build_grouped_by_recipe(visible_runs)
        elif mode == self.VIEW_BY_DATE:
            self._build_grouped_by_date(visible_runs)
        else:
            self._build_flat(visible_runs)

        self._updating_checks = False
        self._update_checked_count()

        # Select first run if available
        if self.tree.topLevelItemCount() > 0:
            first_top = self.tree.topLevelItem(0)
            if first_top.childCount() > 0:
                self.tree.setCurrentItem(first_top.child(0))
            else:
                self.tree.setCurrentItem(first_top)

    def _build_grouped_by_device(self, runs: list[SampleRunRecord]) -> None:
        grouped: dict[tuple[str, str], list[SampleRunRecord]] = defaultdict(list)
        for r in runs:
            grouped[(r.row, r.col)].append(r)

        # Sort coordinates naturally if numeric, else string
        def sort_key(k: tuple[str, str]) -> tuple[int | str, int | str]:
            r_val: int | str = int(k[0]) if k[0].isdigit() else k[0]
            c_val: int | str = int(k[1]) if k[1].isdigit() else k[1]
            return (r_val, c_val)

        for coord in sorted(grouped.keys(), key=sort_key):
            coord_runs = grouped[coord]
            row, col = coord
            label = coord_runs[0].device_label or (
                self._sample.cell_label(row, col) if self._sample else ""
            )
            title = f"Device R{row}:C{col}"
            if label:
                title += f" ({label})"

            group_item = QTreeWidgetItem([
                f"{title}  [{len(coord_runs)} sweeps]",
                "",
                "",
                "",
                "",
            ])
            group_item.setIcon(0, FluentIcon.TILES.icon())
            group_item.setFlags(
                group_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            group_item.setCheckState(0, Qt.CheckState.Unchecked)

            for r in coord_runs:
                child = self._create_run_item(r, label_col0=r.recipe_name or Path(r.run_path).name)
                group_item.addChild(child)

            self.tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)

    def _build_grouped_by_recipe(self, runs: list[SampleRunRecord]) -> None:
        grouped: dict[str, list[SampleRunRecord]] = defaultdict(list)
        for r in runs:
            recipe = r.recipe_name or "Unnamed Sweep"
            grouped[recipe].append(r)

        for recipe in sorted(grouped.keys()):
            recipe_runs = grouped[recipe]
            group_item = QTreeWidgetItem([
                f"Recipe: {recipe}  [{len(recipe_runs)} sweeps]",
                "",
                "",
                "",
                "",
            ])
            group_item.setIcon(0, FluentIcon.DOCUMENT.icon())
            group_item.setFlags(
                group_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            group_item.setCheckState(0, Qt.CheckState.Unchecked)

            for r in recipe_runs:
                child = self._create_run_item(
                    r, label_col0=f"R{r.row}:C{r.col} ({r.device_label})" if r.device_label else f"R{r.row}:C{r.col}"
                )
                group_item.addChild(child)

            self.tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)

    def _build_grouped_by_date(self, runs: list[SampleRunRecord]) -> None:
        grouped: dict[str, list[SampleRunRecord]] = defaultdict(list)
        for r in runs:
            date_str = r.created_at_utc[:10] if len(r.created_at_utc) >= 10 else "Unknown Date"
            grouped[date_str].append(r)

        for date_key in sorted(grouped.keys(), reverse=True):
            date_runs = grouped[date_key]
            group_item = QTreeWidgetItem([
                f"Date: {date_key}  [{len(date_runs)} sweeps]",
                "",
                "",
                "",
                "",
            ])
            group_item.setIcon(0, FluentIcon.CALENDAR.icon())
            group_item.setFlags(
                group_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            group_item.setCheckState(0, Qt.CheckState.Unchecked)

            for r in date_runs:
                child = self._create_run_item(
                    r, label_col0=f"R{r.row}:C{r.col} · {r.recipe_name}"
                )
                group_item.addChild(child)

            self.tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)

    def _build_flat(self, runs: list[SampleRunRecord]) -> None:
        # Sort descending by timestamp
        sorted_runs = sorted(runs, key=lambda x: x.created_at_utc, reverse=True)
        for r in sorted_runs:
            lbl = f"R{r.row}:C{r.col} · {r.recipe_name or Path(r.run_path).name}"
            item = self._create_run_item(r, label_col0=lbl)
            self.tree.addTopLevelItem(item)

    def _create_run_item(self, run: SampleRunRecord, label_col0: str) -> QTreeWidgetItem:
        time_str = run.created_at_utc.replace("T", " ")[:19] if run.created_at_utc else "—"
        pts_str = str(run.point_count) if run.point_count > 0 else (
            f"{run.spectrum_count} sp" if run.spectrum_count > 0 else "0"
        )
        status_str = run.status.capitalize() if run.status else "Unknown"

        elab_str = "—"
        if run.elab_experiment_id:
            elab_str = f"#{run.elab_experiment_id}"
        elif run.elab_status == "uploaded":
            elab_str = "Uploaded"
        elif run.elab_status == "failed":
            elab_str = "Failed"

        item = QTreeWidgetItem([
            label_col0,
            time_str,
            pts_str,
            status_str,
            elab_str,
        ])
        item.setIcon(0, FluentIcon.VIEW.icon())
        item.setData(0, Qt.ItemDataRole.UserRole, run)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)

        # Style status text with color
        if run.status.lower() == "completed":
            item.setForeground(3, QColor(34, 197, 94))
        elif run.status.lower() in ("failed", "aborted", "error"):
            item.setForeground(3, QColor(220, 38, 38))

        if run.elab_experiment_id or run.elab_status == "uploaded":
            item.setForeground(4, QColor(30, 102, 245))

        return item

    # -------------------------------------------------------------------------
    # Slots & Events
    # -------------------------------------------------------------------------

    def _on_filter_changed(self) -> None:
        self._rebuild_tree()

    def _expand_all(self) -> None:
        self.tree.expandAll()

    def _collapse_all(self) -> None:
        self.tree.collapseAll()

    def _on_current_item_changed(
        self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None
    ) -> None:
        if current is None:
            return
        run = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(run, SampleRunRecord):
            self._selected_run = run
            self.run_selected.emit(run)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or self._updating_checks:
            return

        self._updating_checks = True
        try:
            # If a parent item was checked/unchecked, propagate to children
            if item.childCount() > 0:
                parent_state = item.checkState(0)
                if parent_state in (Qt.CheckState.Checked, Qt.CheckState.Unchecked):
                    for i in range(item.childCount()):
                        item.child(i).setCheckState(0, parent_state)
            else:
                # If a child changed, verify if parent needs update
                parent = item.parent()
                if parent is not None:
                    # Update parent check state based on all siblings
                    total = parent.childCount()
                    checked = sum(
                        1
                        for i in range(total)
                        if parent.child(i).checkState(0) == Qt.CheckState.Checked
                    )
                    if checked == total:
                        parent.setCheckState(0, Qt.CheckState.Checked)
                    elif checked == 0:
                        parent.setCheckState(0, Qt.CheckState.Unchecked)
                    else:
                        parent.setCheckState(0, Qt.CheckState.PartiallyChecked)
        finally:
            self._updating_checks = False

        self._update_checked_count()
        self.runs_checked_changed.emit(self.get_checked_runs())

    def _update_checked_count(self) -> None:
        count = len(self.get_checked_runs())
        self.checked_count_label.setText(f"{count} selected for comparison")
        self.clear_selection_btn.setEnabled(count > 0)

    def _check_all(self) -> None:
        self._updating_checks = True
        try:
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                top.setCheckState(0, Qt.CheckState.Checked)
                for c in range(top.childCount()):
                    top.child(c).setCheckState(0, Qt.CheckState.Checked)
        finally:
            self._updating_checks = False
        self._update_checked_count()
        self.runs_checked_changed.emit(self.get_checked_runs())

    def _uncheck_all(self) -> None:
        self._updating_checks = True
        try:
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                top.setCheckState(0, Qt.CheckState.Unchecked)
                for c in range(top.childCount()):
                    top.child(c).setCheckState(0, Qt.CheckState.Unchecked)
        finally:
            self._updating_checks = False
        self._update_checked_count()
        self.runs_checked_changed.emit(self.get_checked_runs())

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return

        run = item.data(0, Qt.ItemDataRole.UserRole)
        menu = RoundMenu(parent=self)

        if isinstance(run, SampleRunRecord):
            menu.addAction(
                Action(
                    FluentIcon.DOCUMENT,
                    "Open in Results Tab",
                    triggered=lambda: self.open_in_results_requested.emit(run.run_path),
                )
            )
            menu.addSeparator()
            menu.addAction(
                Action(
                    FluentIcon.COPY,
                    "Copy File Path",
                    triggered=lambda: QGuiApplication.clipboard().setText(run.run_path),
                )
            )
            if run.run_sha256:
                menu.addAction(
                    Action(
                        FluentIcon.FINGERPRINT,
                        "Copy SHA-256 Checksum",
                        triggered=lambda: QGuiApplication.clipboard().setText(run.run_sha256),
                    )
                )
            menu.addAction(
                Action(
                    FluentIcon.FOLDER,
                    "Reveal in Explorer",
                    triggered=lambda: self._reveal_file(run.run_path),
                )
            )
            if run.elab_url or run.elab_experiment_id:
                url = run.elab_url or ""
                menu.addSeparator()
                menu.addAction(
                    Action(
                        FluentIcon.LINK,
                        f"Open in eLabFTW ({elab_str if (elab_str := run.elab_experiment_id) else 'Link'})",
                        triggered=lambda: self._open_elab(url),
                    )
                )
        else:
            # Group item context menu
            menu.addAction(
                Action(
                    FluentIcon.ACCEPT,
                    "Select / Check All in Group",
                    triggered=lambda: self._set_group_checked(item, Qt.CheckState.Checked),
                )
            )
            menu.addAction(
                Action(
                    FluentIcon.CANCEL,
                    "Uncheck All in Group",
                    triggered=lambda: self._set_group_checked(item, Qt.CheckState.Unchecked),
                )
            )

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _set_group_checked(self, group_item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        self._updating_checks = True
        try:
            group_item.setCheckState(0, state)
            for i in range(group_item.childCount()):
                group_item.child(i).setCheckState(0, state)
        finally:
            self._updating_checks = False
        self._update_checked_count()
        self.runs_checked_changed.emit(self.get_checked_runs())

    def _reveal_file(self, file_path: str) -> None:
        p = Path(file_path)
        folder = p.parent if p.exists() else p
        QDesktopServices.openUrl(f"file:///{folder.as_posix()}")

    def _open_elab(self, url: str) -> None:
        if url:
            QDesktopServices.openUrl(url)
