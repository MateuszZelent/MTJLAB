"""Fluent file browser for immutable HDF5 measurement results."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import math
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    TreeWidget,
)

from app.storage import Hdf5RunReader, RunSummary, ThatecRunReader
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask

COL_FILE = 0
COL_DATE = 1
COL_STATE = 2
COL_OPERATOR = 3
COL_SPECTRA = 4
COL_POINTS = 5


def format_timestamp(iso_str: str | None) -> str:
    """Format an ISO UTC timestamp into readable text."""
    if not iso_str:
        return "—"
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str[:19].replace("T", " ")


def categorize_date(iso_str: str | None, now_utc: datetime) -> str:
    """Categorize an ISO timestamp into Today, Yesterday, This week, This month, Older."""
    if not iso_str:
        return "Older"
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned).astimezone(timezone.utc)
        now = now_utc.astimezone(timezone.utc)
        diff_days = (now.date() - dt.date()).days
        if diff_days <= 0:
            return "Today"
        elif diff_days == 1:
            return "Yesterday"
        elif diff_days <= 7:
            return "This week"
        elif diff_days <= 30:
            return "This month"
        else:
            return "Older"
    except Exception:
        return "Older"


class ResultFileItem(QTreeWidgetItem):
    """Sortable TreeWidgetItem representing a recorded HDF5 run."""

    def __init__(self, summary: RunSummary, formatted_date: str) -> None:
        super().__init__(
            [
                summary.path.name,
                formatted_date,
                summary.status,
                summary.operator or "—",
                str(summary.spectrum_count),
                str(summary.point_count),
            ]
        )
        self.summary = summary
        self.raw_timestamp = summary.created_at_utc or ""
        self.setData(COL_FILE, Qt.ItemDataRole.UserRole, str(summary.path))
        self.setData(COL_DATE, Qt.ItemDataRole.UserRole, str(summary.path))
        self.setToolTip(COL_FILE, str(summary.path.resolve()))
        self.setToolTip(COL_DATE, f"Recorded (UTC): {summary.created_at_utc or 'Unknown'}")
        self.setToolTip(
            COL_STATE,
            f"State: {summary.status}\nOperator: {summary.operator or 'Unknown'}\n"
            f"Spectra: {summary.spectrum_count}\nCheckpoints: {summary.point_count}",
        )

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        if not isinstance(other, ResultFileItem):
            return super().__lt__(other)
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else COL_DATE
        if col == COL_DATE:
            return (self.raw_timestamp or "") < (other.raw_timestamp or "")
        elif col in (COL_SPECTRA, COL_POINTS):
            try:
                v1 = int(self.text(col))
                v2 = int(other.text(col))
                return v1 < v2
            except (ValueError, TypeError):
                pass
        return self.text(col).casefold() < other.text(col).casefold()


class DateGroupItem(QTreeWidgetItem):
    """Collapsible category grouping runs by date."""

    def __init__(self, title: str, count: int) -> None:
        super().__init__([f"{title} ({count})", "", "", "", "", ""])
        font = self.font(0)
        font.setBold(True)
        self.setFont(0, font)
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.group_title = title

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        order = {"Today": 0, "Yesterday": 1, "This week": 2, "This month": 3, "Older": 4}
        g1 = getattr(self, "group_title", self.text(0))
        g2 = getattr(other, "group_title", other.text(0))
        return order.get(g1, 99) < order.get(g2, 99)


class FileBrowserPanel(QWidget):
    """List station and public THATEC/PyThat HDF5 results."""

    file_selected = Signal(object)  # Path | None
    file_opened = Signal(object)  # Path
    directory_changed = Signal(object)  # Path
    files_loaded = Signal(bool)
    _ASYNC_REFRESH_FILE_COUNT = 8
    _ASYNC_REFRESH_BYTES = 32 * 1024 * 1024

    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._output_dir = Path(output_dir)
        self._selected_path: Path | None = None
        self._state_action: Callable[[], None] = self.browse_file
        self._refresh_request_id = 0
        self._refresh_task: ResultReadTask | None = None

        self._all_summaries: list[RunSummary] = []
        self._filtered_summaries: list[RunSummary] = []
        self._view_mode = "flat"  # "flat" or "grouped"
        self._current_page = 1
        self._page_size = 25  # 15, 25, 50, 100, 0 (all)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # --- Directory location banner ---
        self.location = BodyLabel(self)
        self.location.setObjectName("muted")
        self.location.setWordWrap(True)
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.location)

        # --- Primary directory actions ---
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.refresh_button = PushButton("Refresh", self)
        self.change_directory_button = PushButton("Change directory...", self)
        self.open_file_button = PrimaryPushButton("Open HDF5 / PyThat file...", self)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.change_directory_button)
        actions.addWidget(self.open_file_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        # --- Search input ---
        self.search = LineEdit(self)
        self.search.setProperty("precisionArrowStepping", False)
        self.search.setPlaceholderText("Filter by file name, operator, or state...")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Filter result files")
        layout.addWidget(self.search)

        # --- Filter controls bar ---
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)

        self.state_filter = ComboBox(self)
        self.state_filter.setAccessibleName("Filter by state")
        self.state_filter.addItems(
            ["All states", "completed", "incomplete", "faulted", "aborted", "THATEC", "unreadable"]
        )
        self.state_filter.setToolTip("Filter by run execution state")
        filter_bar.addWidget(self.state_filter, 1)

        self.operator_filter = ComboBox(self)
        self.operator_filter.setAccessibleName("Filter by operator")
        self.operator_filter.addItem("All operators")
        self.operator_filter.setToolTip("Filter by authenticated operator")
        filter_bar.addWidget(self.operator_filter, 1)

        self.date_filter = ComboBox(self)
        self.date_filter.setAccessibleName("Filter by date")
        self.date_filter.addItems(
            ["All time", "Today", "Yesterday", "Last 7 days", "Last 30 days"]
        )
        self.date_filter.setToolTip("Filter by recording date range")
        filter_bar.addWidget(self.date_filter, 1)

        self.view_mode_combo = ComboBox(self)
        self.view_mode_combo.setAccessibleName("View mode")
        self.view_mode_combo.addItem("Flat list", userData="flat")
        self.view_mode_combo.addItem("Group by date", userData="grouped")
        self.view_mode_combo.setToolTip("Switch between flat table and date-grouped view")
        filter_bar.addWidget(self.view_mode_combo, 1)

        self.clear_filter_btn = PushButton("Clear", self)
        self.clear_filter_btn.setToolTip("Reset all search and filter criteria")
        self.clear_filter_btn.setEnabled(False)
        filter_bar.addWidget(self.clear_filter_btn)

        layout.addLayout(filter_bar)

        # --- Results table ---
        self.runs = TreeWidget(self)
        self.runs.setHeaderLabels(["File", "Date", "State", "Operator", "Spectra", "Points"])
        self.runs.setMinimumWidth(280)
        self.runs.setUniformRowHeights(True)
        self.runs.setAccessibleName("Recorded HDF5 results")
        self.runs.setSortingEnabled(True)
        header = self.runs.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.runs.sortByColumn(COL_DATE, Qt.SortOrder.DescendingOrder)

        header.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_DATE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_STATE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_OPERATOR, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_SPECTRA, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_POINTS, QHeaderView.ResizeMode.Interactive)

        self.runs.setColumnWidth(COL_FILE, 220)
        self.runs.setColumnWidth(COL_DATE, 155)
        self.runs.setColumnWidth(COL_STATE, 95)
        self.runs.setColumnWidth(COL_OPERATOR, 110)
        self.runs.setColumnWidth(COL_SPECTRA, 65)
        self.runs.setColumnWidth(COL_POINTS, 65)

        self.state_card = ResultsStateCard(self)
        self.content = QStackedWidget(self)
        self.content.addWidget(self.runs)
        self.content.addWidget(self.state_card)
        layout.addWidget(self.content, 1)

        # --- Pagination toolbar ---
        self.pagination_bar = QWidget(self)
        pag_layout = QHBoxLayout(self.pagination_bar)
        pag_layout.setContentsMargins(0, 2, 0, 0)
        pag_layout.setSpacing(6)

        self.first_page_btn = PushButton("|<", self.pagination_bar)
        self.first_page_btn.setFixedWidth(36)
        self.first_page_btn.setToolTip("First page")
        self.prev_page_btn = PushButton("<", self.pagination_bar)
        self.prev_page_btn.setFixedWidth(36)
        self.prev_page_btn.setToolTip("Previous page")

        self.page_label = CaptionLabel("Page 1 of 1 (0 runs)", self.pagination_bar)
        self.page_label.setObjectName("muted")

        self.next_page_btn = PushButton(">", self.pagination_bar)
        self.next_page_btn.setFixedWidth(36)
        self.next_page_btn.setToolTip("Next page")
        self.last_page_btn = PushButton(">|", self.pagination_bar)
        self.last_page_btn.setFixedWidth(36)
        self.last_page_btn.setToolTip("Last page")

        pag_layout.addWidget(self.first_page_btn)
        pag_layout.addWidget(self.prev_page_btn)
        pag_layout.addWidget(self.page_label)
        pag_layout.addWidget(self.next_page_btn)
        pag_layout.addWidget(self.last_page_btn)
        pag_layout.addStretch(1)

        pag_layout.addWidget(CaptionLabel("Rows per page:", self.pagination_bar))
        self.page_size_combo = ComboBox(self.pagination_bar)
        self.page_size_combo.addItems(["15", "25", "50", "100", "All"])
        self.page_size_combo.setCurrentIndex(1)  # default 25
        pag_layout.addWidget(self.page_size_combo)

        layout.addWidget(self.pagination_bar)

        # --- Connections ---
        self.refresh_button.clicked.connect(self.refresh)
        self.change_directory_button.clicked.connect(self.choose_directory)
        self.open_file_button.clicked.connect(self.browse_file)
        self.search.textChanged.connect(self._on_filter_changed)
        self.state_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.operator_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.date_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.view_mode_combo.currentIndexChanged.connect(self._on_view_mode_changed)
        self.clear_filter_btn.clicked.connect(self.clear_filters)
        self.runs.currentItemChanged.connect(self._on_current_changed)
        self.state_card.action_requested.connect(self._run_state_action)

        self.first_page_btn.clicked.connect(lambda: self._set_page(1))
        self.prev_page_btn.clicked.connect(lambda: self._set_page(self._current_page - 1))
        self.next_page_btn.clicked.connect(lambda: self._set_page(self._current_page + 1))
        self.last_page_btn.clicked.connect(lambda: self._set_page(self._total_pages()))
        self.page_size_combo.currentTextChanged.connect(self._on_page_size_changed)

    @property
    def selected_path(self) -> Path | None:
        return self._selected_path

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    def set_output_directory(self, output_dir: str | Path) -> None:
        next_output_dir = Path(output_dir).expanduser()
        if next_output_dir == self._output_dir:
            return
        self._output_dir = next_output_dir
        self._selected_path = None
        self.directory_changed.emit(self._output_dir)
        self.file_selected.emit(None)
        self.refresh()

    def refresh(self) -> None:
        """Reload known results and preserve selection only when it still exists."""
        self.location.setText(f"Directory: {self._output_dir.resolve()}")
        previous = self._selected_path
        self._cancel_refresh()
        if self._should_refresh_async():
            self.runs.clear()
            self.state_card.show_state(
                title="Loading result files",
                description="Reading the result index in the background...",
                accessible_name="Loading result files",
                loading=True,
            )
            self.content.setCurrentWidget(self.state_card)
            request_id = self._refresh_request_id
            task = ResultReadTask(request_id, Hdf5RunReader.list_runs, self._output_dir)
            self._refresh_task = task
            task.signals.loaded.connect(self._on_refresh_loaded)
            task.signals.failed.connect(self._on_refresh_failed)
            QThreadPool.globalInstance().start(task)
            return
        self._populate_summaries(Hdf5RunReader.list_runs(self._output_dir), previous)

    def _should_refresh_async(self) -> bool:
        try:
            paths = tuple(self._output_dir.glob("*.h5")) + tuple(
                self._output_dir.glob("*.hdf5")
            )
            total_bytes = sum(path.stat().st_size for path in paths if path.is_file())
        except OSError:
            return False
        return len(paths) >= self._ASYNC_REFRESH_FILE_COUNT or total_bytes >= self._ASYNC_REFRESH_BYTES

    def _cancel_refresh(self) -> None:
        self._refresh_request_id += 1
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

    def _on_refresh_loaded(self, request_id: int, summaries: object) -> None:
        if request_id != self._refresh_request_id:
            return
        self._refresh_task = None
        if not isinstance(summaries, tuple):
            self._on_refresh_failed(request_id, "The result index returned an invalid payload.")
            return
        self._populate_summaries(summaries, self._selected_path)

    def _on_refresh_failed(self, request_id: int, message: str) -> None:
        if request_id != self._refresh_request_id:
            return
        self._refresh_task = None
        self._state_action = self.refresh
        self.state_card.show_state(
            title="Cannot index result files",
            description=message,
            accessible_name="Cannot index result files",
            action_text="Retry refresh",
        )
        self.content.setCurrentWidget(self.state_card)

    def _populate_summaries(self, summaries: tuple[object, ...], previous: Path | None) -> None:
        self._all_summaries = [s for s in summaries if isinstance(s, RunSummary)]

        # Populate operator filter choices dynamically
        current_op = self.operator_filter.currentText()
        distinct_operators = sorted({s.operator for s in self._all_summaries if s.operator})
        self.operator_filter.blockSignals(True)
        self.operator_filter.clear()
        self.operator_filter.addItem("All operators")
        for op in distinct_operators:
            self.operator_filter.addItem(op)
        idx = self.operator_filter.findText(current_op)
        if idx >= 0:
            self.operator_filter.setCurrentIndex(idx)
        self.operator_filter.blockSignals(False)

        self._filter_and_render(previous=previous)
        self.files_loaded.emit(self.has_files())

    def _on_filter_changed(self) -> None:
        self._current_page = 1
        has_active_filters = (
            bool(self.search.text().strip())
            or self.state_filter.currentIndex() > 0
            or self.operator_filter.currentIndex() > 0
            or self.date_filter.currentIndex() > 0
        )
        self.clear_filter_btn.setEnabled(has_active_filters)
        self._filter_and_render(previous=self._selected_path)

    def clear_filters(self) -> None:
        self.search.clear()
        self.state_filter.setCurrentIndex(0)
        self.operator_filter.setCurrentIndex(0)
        self.date_filter.setCurrentIndex(0)
        self.clear_filter_btn.setEnabled(False)

    def _on_view_mode_changed(self) -> None:
        self._view_mode = str(self.view_mode_combo.currentData() or "flat")
        self._current_page = 1
        self.pagination_bar.setVisible(self._view_mode == "flat")
        self._filter_and_render(previous=self._selected_path)

    def _on_page_size_changed(self, text: str) -> None:
        if text == "All":
            self._page_size = 0
        else:
            try:
                self._page_size = int(text)
            except ValueError:
                self._page_size = 25
        self._current_page = 1
        self._filter_and_render(previous=self._selected_path)

    def _total_pages(self) -> int:
        if self._page_size <= 0:
            return 1
        count = len(self._filtered_summaries)
        return max(1, math.ceil(count / self._page_size))

    def _set_page(self, page: int) -> None:
        total = self._total_pages()
        target = max(1, min(page, total))
        if target != self._current_page:
            self._current_page = target
            self._render_page(previous=self._selected_path)

    def _filter_and_render(self, previous: Path | None = None) -> None:
        query = self.search.text().strip().casefold()
        state_filter = self.state_filter.currentText()
        operator_filter = self.operator_filter.currentText()
        date_filter = self.date_filter.currentText()

        now = datetime.now(timezone.utc)

        filtered: list[RunSummary] = []
        for s in self._all_summaries:
            formatted_date = format_timestamp(s.created_at_utc)
            # Text filter
            if query:
                searchable = f"{s.path.name} {s.status} {s.operator or ''} {formatted_date}".casefold()
                if query not in searchable:
                    continue

            # State filter
            if state_filter != "All states" and s.status.casefold() != state_filter.casefold():
                continue

            # Operator filter
            if operator_filter != "All operators" and (s.operator or "").casefold() != operator_filter.casefold():
                continue

            # Date range filter
            if date_filter != "All time":
                cat = categorize_date(s.created_at_utc, now)
                if date_filter == "Today" and cat != "Today":
                    continue
                elif date_filter == "Yesterday" and cat != "Yesterday":
                    continue
                elif date_filter == "Last 7 days" and cat not in ("Today", "Yesterday", "This week"):
                    continue
                elif date_filter == "Last 30 days" and cat not in ("Today", "Yesterday", "This week", "This month"):
                    continue

            filtered.append(s)

        self._filtered_summaries = filtered
        self._render_page(previous=previous)

    def _render_page(self, previous: Path | None = None) -> None:
        total_items = len(self._filtered_summaries)
        total_pages = self._total_pages()
        self._current_page = max(1, min(self._current_page, total_pages))

        # Update pagination bar
        self.page_label.setText(
            f"Page {self._current_page} of {total_pages} ({total_items} runs)"
        )
        self.first_page_btn.setEnabled(self._current_page > 1)
        self.prev_page_btn.setEnabled(self._current_page > 1)
        self.next_page_btn.setEnabled(self._current_page < total_pages)
        self.last_page_btn.setEnabled(self._current_page < total_pages)

        # Handle empty states
        if len(self._all_summaries) == 0:
            self._state_action = self.browse_file
            self.state_card.show_state(
                title="No recorded results yet",
                description=(
                    "Open a THATEC/PyThat HDF5 file, or choose another result "
                    "directory to inspect stored spectra."
                ),
                accessible_name="No HDF5 result files",
                action_text="Open result file...",
            )
            self.content.setCurrentWidget(self.state_card)
            return
        elif total_items == 0:
            self._state_action = self.clear_filters
            self.state_card.show_state(
                title="No matching result files",
                description="Clear filters or search for another file name, state, or operator.",
                accessible_name="No result files match the filter",
                action_text="Clear filters",
            )
            self.content.setCurrentWidget(self.state_card)
            return

        self.content.setCurrentWidget(self.runs)
        self.runs.blockSignals(True)
        self.runs.setSortingEnabled(False)
        self.runs.clear()

        now = datetime.now(timezone.utc)

        if self._view_mode == "grouped":
            groups: dict[str, list[RunSummary]] = {
                "Today": [],
                "Yesterday": [],
                "This week": [],
                "This month": [],
                "Older": [],
            }
            for s in self._filtered_summaries:
                cat = categorize_date(s.created_at_utc, now)
                groups[cat].append(s)

            for title, items in groups.items():
                if not items:
                    continue
                group_item = DateGroupItem(title, len(items))
                self.runs.addTopLevelItem(group_item)
                for s in items:
                    date_text = format_timestamp(s.created_at_utc)
                    child = ResultFileItem(s, date_text)
                    group_item.addChild(child)
            self.runs.expandAll()
        else:
            # Flat paginated view
            if self._page_size > 0:
                start_idx = (self._current_page - 1) * self._page_size
                page_items = self._filtered_summaries[start_idx : start_idx + self._page_size]
            else:
                page_items = self._filtered_summaries

            for s in page_items:
                date_text = format_timestamp(s.created_at_utc)
                item = ResultFileItem(s, date_text)
                self.runs.addTopLevelItem(item)

        self.runs.setSortingEnabled(True)
        self.runs.blockSignals(False)

        if previous is not None:
            self._restore_selection(previous)

    def choose_directory(self) -> None:
        """Browse another directory without changing persisted station settings."""
        selected = QFileDialog.getExistingDirectory(
            self,
            "Browse result directory",
            str(self._output_dir),
        )
        if not selected:
            return
        self._output_dir = Path(selected)
        self._selected_path = None
        self.directory_changed.emit(self._output_dir)
        self.file_selected.emit(None)
        self.refresh()

    def browse_file(self) -> None:
        """Open one station or public THATEC/PyThat HDF5 result."""
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Open THATEC / PyThat HDF5 result",
            str(self._output_dir),
            "HDF5 results (*.h5 *.hdf5);;All files (*)",
        )
        if selected:
            self.open_file(Path(selected))

    def open_file(self, path: Path) -> None:
        """Add and select an arbitrary result without modifying it."""
        target = Path(path).expanduser()
        if not target.is_file():
            self._show_error(
                "Result file not found",
                f"The selected path is not a readable file:\n{target}",
            )
            return
        if target.suffix.lower() not in {".h5", ".hdf5"}:
            self._show_error(
                "Unsupported result file",
                "Choose an HDF5 result with the .h5 or .hdf5 extension.",
            )
            return

        # Check if already in summaries
        existing_summary = next((s for s in self._all_summaries if s.path == target), None)
        if existing_summary is None:
            try:
                summary = Hdf5RunReader.summary(target)
            except Exception:
                try:
                    run = ThatecRunReader.describe(target)
                    shapes = [row.shape[0] for row in run.rows.values() if row.shape]
                    summary = RunSummary(
                        path=target,
                        created_at_utc=Hdf5RunReader._extract_timestamp(target, None),
                        status="THATEC",
                        point_count=max(shapes, default=0),
                        spectrum_count=sum(len(row.shape) >= 2 for row in run.rows.values()),
                        plan_sha256=None,
                        application_version=None,
                        operator=None,
                    )
                except Exception:
                    summary = RunSummary(
                        path=target,
                        created_at_utc=Hdf5RunReader._extract_timestamp(target, None),
                        status="unreadable",
                        point_count=0,
                        spectrum_count=0,
                        plan_sha256=None,
                        application_version=None,
                        operator=None,
                    )
            self._all_summaries.insert(0, summary)

        self.clear_filters()
        self._selected_path = target
        self._filter_and_render(previous=target)
        self._restore_selection(target)
        self.file_opened.emit(target)

    def has_files(self) -> bool:
        """Return whether the browser contains any entries before filtering."""
        return len(self._all_summaries) > 0

    def _on_current_changed(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if item is None or isinstance(item, DateGroupItem):
            return
        path_str = str(item.data(COL_DATE, Qt.ItemDataRole.UserRole) or item.data(COL_FILE, Qt.ItemDataRole.UserRole) or "")
        if not path_str:
            return
        path = Path(path_str)
        self._selected_path = path
        self.file_selected.emit(path)

    def _find_item_by_path(self, target: Path) -> QTreeWidgetItem | None:
        for i in range(self.runs.topLevelItemCount()):
            top = self.runs.topLevelItem(i)
            if isinstance(top, DateGroupItem):
                for j in range(top.childCount()):
                    child = top.child(j)
                    p_str = str(child.data(COL_DATE, Qt.ItemDataRole.UserRole) or child.data(COL_FILE, Qt.ItemDataRole.UserRole) or "")
                    if p_str and Path(p_str) == target:
                        return child
            else:
                p_str = str(top.data(COL_DATE, Qt.ItemDataRole.UserRole) or top.data(COL_FILE, Qt.ItemDataRole.UserRole) or "")
                if p_str and Path(p_str) == target:
                    return top
        return None

    def _restore_selection(self, target: Path | None = None) -> bool:
        to_find = target or self._selected_path
        if to_find is None:
            return False
        item = self._find_item_by_path(to_find)
        if item is None:
            return False
        self.runs.setCurrentItem(item)
        return True

    def _apply_filter(self, text: str) -> None:
        """Backwards-compatibility filter helper."""
        self.search.setText(text)

    def _show_error(self, title: str, description: str) -> None:
        self._state_action = self.clear_filters
        self.state_card.show_state(
            title=title,
            description=description,
            accessible_name=title,
            action_text="Return to file list",
        )
        self.content.setCurrentWidget(self.state_card)

    def _run_state_action(self) -> None:
        self._state_action()

    def closeEvent(self, event) -> None:
        self._cancel_refresh()
        super().closeEvent(event)
