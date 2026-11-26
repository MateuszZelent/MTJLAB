"""Fluent file browser for immutable HDF5 measurement results."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BreadcrumbBar,
    CaptionLabel,
    ComboBox,
    FlowLayout,
    FluentIcon,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    SegmentedWidget,
    ToggleToolButton,
    ToolButton,
    TreeWidget,
    isDarkTheme,
)

from app.storage import Hdf5RunReader, RunSummary, ThatecRunReader
from app.storage.filesystem_paths import native_path
from app.ui.design_system import tokens_for
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask

COL_FILE = 0
COL_DATE = 1
COL_STATE = 2
COL_OPERATOR = 3
COL_SPECTRA = 4
COL_POINTS = 5

_BUNDLE_DIR_RE = re.compile(
    r"^([1-9][0-9]*)_(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z(?:_(.*))?$"
)
_RESUMABLE_STATES = frozenset({"aborted", "faulted", "incomplete"})
_SECTION_RANKS = {
    "measurements": 0,
    "sweeps": 1,
    "characterization": 2,
    "attachments": 15,
    "post-processing": 20,
    "postprocessing": 20,
    "bundle files": 30,
    "references": 35,
}


@dataclass(frozen=True)
class _CatalogueIndex:
    summaries: tuple
    artifacts: tuple[Path, ...]
    timestamps: dict


def _discover_artifacts(directory, catalogue, cancelled):
    if not catalogue or not directory.is_dir():
        return ()
    from app.storage.run_bundle import is_bundle_companion

    artifacts = []
    for path in directory.rglob("*"):
        if cancelled():
            raise InterruptedError("Result catalogue indexing cancelled")
        if not path.is_file():
            continue
        bundle_file = (path.parent / "metadata.json").is_file() or is_bundle_companion(path)
        allowed = {".csv", ".pdf"} | ({".json", ".jsonl", ".yml", ".h5"} if bundle_file else set())
        if (
            path.suffix.lower() in allowed
            and path.name not in {"data.h5", "info.csv"}
            and "measurements" in {part.casefold() for part in path.parts}
        ):
            artifacts.append(path)
    return tuple(sorted(artifacts, key=lambda p: str(p).casefold()))


def _read_catalogue(directory, catalogue, *, cancelled=lambda: False):
    summaries = Hdf5RunReader.list_runs(directory, recursive=True, cancelled=cancelled)
    artifacts = _discover_artifacts(directory, catalogue, cancelled)
    return _CatalogueIndex(
        summaries,
        artifacts,
        {p: Hdf5RunReader._extract_timestamp(p, None) for p in artifacts},
    )


def _read_file_summary(target):
    try:
        return Hdf5RunReader.summary(target)
    except Exception:
        try:
            run = ThatecRunReader.describe(target)
        except Exception:
            run = None
        return RunSummary(
            target,
            Hdf5RunReader._extract_timestamp(target, None),
            "THATEC" if run else "unreadable",
            max((r.shape[0] for r in run.rows.values() if r.shape), default=0) if run else 0,
            sum(len(r.shape) >= 2 for r in run.rows.values()) if run else 0,
            None,
            None,
            None,
        )


def _mixed_item_sort_key(item: QTreeWidgetItem, column: int) -> tuple[int, str, str]:
    """Return a deterministic key without calling Qt's virtual comparator.

    PySide can dispatch ``super().__lt__`` back into the Python override when
    different QTreeWidgetItem subclasses are siblings. Catalogue folders and
    result/artifact leaves are intentionally mixed, so keep that comparison
    entirely in Python and place folders before files.
    """
    priority = int(getattr(item, "_sort_priority", 10))
    return (
        priority,
        item.text(column).casefold(),
        item.text(COL_FILE).casefold(),
    )


def _elide_crumb(text: str, max_len: int = 18) -> str:
    if len(text) <= max_len:
        return text
    head = max(6, max_len - 7)
    return f"{text[:head]}…{text[-5:]}"


def _format_run_bundle_label(folder_name: str, *, fallback: str = "data.h5") -> tuple[str, int | None]:
    """Turn a numbered bundle folder like ``3_20261125T102512Z_FMR_Sweep`` into ``#3 · FMR Sweep``."""
    match = _BUNDLE_DIR_RE.match(folder_name)
    if not match:
        return (folder_name or fallback, None)
    number = int(match.group(1))
    year, month, day, hour, minute = match.group(2, 3, 4, 5, 6)
    suffix = (match.group(8) or "").replace("_", " ").strip()
    if suffix:
        return (f"#{number} · {suffix}", number)
    return (f"#{number} · {year}-{month}-{day} {hour}:{minute}", number)


def _display_status(status: str) -> str:
    cleaned = (status or "").strip()
    if not cleaned:
        return "—"
    if cleaned.upper() == "THATEC":
        return "THATEC"
    return cleaned.capitalize()


def _status_brush(status: str) -> QBrush:
    tokens = tokens_for("dark" if isDarkTheme() else "light")
    low = (status or "").strip().casefold()
    if low == "completed":
        return QBrush(QColor(tokens.success))
    if low == "aborted":
        return QBrush(QColor(tokens.caution))
    if low in {"faulted", "incomplete", "unreadable"}:
        return QBrush(QColor(tokens.danger))
    return QBrush(QColor(tokens.text_muted))


def format_timestamp(iso_str: str | None) -> str:
    """Format an ISO UTC timestamp into compact readable text (YYYY-MM-DD HH:MM)."""
    if not iso_str:
        return "—"
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:16].replace("T", " ")


def categorize_date(iso_str: str | None, now_utc: datetime) -> str:
    """Categorize an ISO timestamp into Today, Yesterday, This week, This month, Older."""
    if not iso_str:
        return "Older"
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned).astimezone(UTC)
        now = now_utc.astimezone(UTC)
        diff_days = (now.date() - dt.date()).days
        if diff_days <= 0:
            return "Today"
        if diff_days == 1:
            return "Yesterday"
        if diff_days <= 7:
            return "This week"
        if diff_days <= 30:
            return "This month"
        return "Older"
    except Exception:
        return "Older"


class ResultFileItem(QTreeWidgetItem):
    """Sortable TreeWidgetItem representing a recorded HDF5 run."""

    _sort_priority = 10

    def __init__(
        self,
        summary: RunSummary,
        formatted_date: str,
        *,
        display_name: str | None = None,
        sweep_number: int | None = None,
    ) -> None:
        if display_name is None:
            if summary.path.name == "data.h5":
                display_name, parsed_num = _format_run_bundle_label(
                    summary.path.parent.name, fallback=summary.path.name
                )
                if sweep_number is None:
                    sweep_number = parsed_num
            else:
                display_name = summary.path.name
        super().__init__(
            [
                display_name,
                formatted_date,
                _display_status(summary.status),
                summary.operator or "—",
                str(summary.spectrum_count),
                str(summary.point_count),
            ]
        )
        self.summary = summary
        self.raw_timestamp = summary.created_at_utc or ""
        self._sweep_number = sweep_number
        self.setData(COL_FILE, Qt.ItemDataRole.UserRole, str(summary.path))
        self.setData(COL_DATE, Qt.ItemDataRole.UserRole, str(summary.path))
        self.setForeground(COL_STATE, _status_brush(summary.status))
        self.setToolTip(COL_FILE, str(summary.path.absolute()))
        self.setToolTip(COL_DATE, f"Recorded (UTC): {summary.created_at_utc or 'Unknown'}")
        self.setToolTip(
            COL_STATE,
            f"State: {summary.status}\nOperator: {summary.operator or 'Unknown'}\n"
            f"Spectra: {summary.spectrum_count}\nCheckpoints: {summary.point_count}",
        )

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        if not isinstance(other, ResultFileItem):
            tree = self.treeWidget()
            col = tree.sortColumn() if tree is not None else COL_DATE
            return _mixed_item_sort_key(self, col) < _mixed_item_sort_key(other, col)
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else COL_DATE
        if col == COL_DATE:
            if (self.raw_timestamp or "") != (other.raw_timestamp or ""):
                return (self.raw_timestamp or "") < (other.raw_timestamp or "")
            if self._sweep_number is not None and other._sweep_number is not None:
                return self._sweep_number < other._sweep_number
        elif col in (COL_SPECTRA, COL_POINTS):
            try:
                v1 = int(self.text(col))
                v2 = int(other.text(col))
                return v1 < v2
            except (ValueError, TypeError):
                pass
        elif col == COL_FILE and self._sweep_number is not None and other._sweep_number is not None:
            return self._sweep_number < other._sweep_number
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


class SampleGroupItem(QTreeWidgetItem):
    """Collapsible category grouping runs by sample name and ID."""

    def __init__(self, title: str, count: int) -> None:
        super().__init__([f"{title} ({count})", "", "", "", "", ""])
        font = self.font(0)
        font.setBold(True)
        self.setFont(0, font)
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.group_title = title


class CatalogueGroupItem(QTreeWidgetItem):
    """A non-selectable folder row in the sample measurement catalogue tree."""

    _sort_priority = 0

    def __init__(self, title: str, count: int, *, collapsed_by_default: bool = False) -> None:
        super().__init__([f"{title} ({count})", "", "", "", "", ""])
        self.group_title = title
        self._title = title
        self.collapsed_by_default = collapsed_by_default
        self._section_rank = _SECTION_RANKS.get(title.casefold(), 10)
        match = re.match(r"^([1-9][0-9]*)_\d{8}T\d{6}Z_", title)
        self._sweep_number = int(match[1]) if match else None
        font = self.font(0)
        if self._section_rank <= 2:
            font.setBold(True)
        self.setFont(0, font)
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.setIcon(0, FluentIcon.FOLDER.icon())

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        tree = self.treeWidget()
        if isinstance(other, CatalogueGroupItem):
            if self._section_rank != other._section_rank:
                # Keep Measurements above Post-processing regardless of ascending/descending sort.
                descending = (
                    tree is not None
                    and tree.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
                )
                return (
                    self._section_rank > other._section_rank
                    if descending
                    else self._section_rank < other._section_rank
                )
            if self._sweep_number is not None and other._sweep_number is not None:
                return self._sweep_number < other._sweep_number
            return self._title.casefold() < other._title.casefold()
        col = tree.sortColumn() if tree is not None else COL_FILE
        return _mixed_item_sort_key(self, col) < _mixed_item_sort_key(other, col)


class CatalogueArtifactItem(QTreeWidgetItem):
    """A CSV/PDF/JSON/YML measurement artifact or run sidecar shown under its folder or run."""

    _sort_priority = 10

    def __init__(self, path: Path, recorded=None) -> None:
        suffix = path.suffix.lower().lstrip(".").upper() or "FILE"
        super().__init__([path.name, format_timestamp(recorded), suffix, "—", "—", "—"])
        self.path = path
        self.setData(COL_FILE, Qt.ItemDataRole.UserRole, str(path))
        self.setData(COL_DATE, Qt.ItemDataRole.UserRole, str(path))
        self.setIcon(COL_FILE, FluentIcon.DOCUMENT.icon())
        self.setToolTip(COL_FILE, f"Measurement artifact:\n{path.absolute()}")

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else COL_DATE
        return _mixed_item_sort_key(self, col) < _mixed_item_sort_key(other, col)


class FileBrowserPanel(QWidget):
    """Browse flat result folders or a nested sample measurement catalogue."""

    file_selected = Signal(object)  # Path | None
    file_opened = Signal(object)  # Path
    directory_changed = Signal(object)  # Path
    files_loaded = Signal(bool)

    def __init__(
        self,
        output_dir: str,
        parent: QWidget | None = None,
        *,
        catalogue_tree: bool = False,
    ) -> None:
        super().__init__(parent)
        self._output_dir = Path(output_dir).expanduser().absolute()
        self._active_directory: Path | None = None
        self._catalogue_root = self._output_dir
        self._context_initialized = False
        self._catalogue_tree_requested = catalogue_tree
        self._selected_path: Path | None = None
        self._pending_selected_path: Path | None = None
        self._state_action: Callable[[], None] = self.browse_file
        self._refresh_request_id = 0
        self._refresh_task: ResultReadTask | None = None
        self._read_pool = QThreadPool(self)
        self._read_pool.setMaxThreadCount(1)

        self._all_summaries: list[RunSummary] = []
        self._filtered_summaries: list[RunSummary] = []
        self._catalogue_artifacts: list[Path] = []
        self._artifact_timestamps = {}
        self._filtered_artifacts: list[Path] = []
        self._breadcrumb_paths: dict[str, Path] = {}
        self._scope = "all"  # "all" | "sweeps" | "postprocessing" | "resumable"
        self._view_mode = "flat"  # "flat" | "grouped" | "sample" | "catalogue"
        self._current_page = 1
        self._page_size = 25  # 5, 15, 25, 50, 100, 0 (all)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # --- Interactive Sample / Directory Breadcrumb Bar ---
        crumb_frame = QFrame(self)
        crumb_frame.setObjectName("resultsBreadcrumbBar")
        crumb_layout = QVBoxLayout(crumb_frame)
        crumb_layout.setContentsMargins(8, 4, 8, 4)
        crumb_layout.setSpacing(2)
        self.breadcrumb_bar = BreadcrumbBar(crumb_frame)
        self.breadcrumb_bar.setAccessibleName("Sample directory breadcrumbs")
        self.breadcrumb_bar.setSpacing(6)
        self.breadcrumb_bar.currentItemChanged.connect(self._on_breadcrumb_clicked)
        crumb_layout.addWidget(self.breadcrumb_bar)

        self.location = CaptionLabel(crumb_frame)
        self.location.setObjectName("muted")
        self.location.setWordWrap(False)
        self.location.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        crumb_layout.addWidget(self.location)
        layout.addWidget(crumb_frame)

        # --- Compact single-row directory & file actions ---
        self.actions_host = QWidget(self)
        actions = FlowLayout(self.actions_host, needAni=False)
        self._actions_flow = actions
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(5)
        actions.setVerticalSpacing(4)

        self.parent_directory_button = ToolButton(FluentIcon.UP, self.actions_host)
        self.parent_directory_button.setAccessibleName("Go to parent folder")
        self.refresh_button = ToolButton(FluentIcon.SYNC, self.actions_host)
        self.refresh_button.setAccessibleName("Refresh result index")
        self.refresh_button.setToolTip("Refresh result index in background")
        self.change_directory_button = ToolButton(FluentIcon.FOLDER, self.actions_host)
        self.change_directory_button.setAccessibleName("Choose folder")
        self.change_directory_button.setToolTip("Choose another result directory")

        self.active_directory_button = PushButton("Active DUT", self.actions_host)
        self.catalogue_directory_button = PushButton("Samples", self.actions_host)
        self.open_file_button = PrimaryPushButton(
            FluentIcon.DOCUMENT, "Open…", self.actions_host
        )
        self.open_file_button.setToolTip("Open an external THATEC/PyThat HDF5 file")
        self.active_directory_button.setEnabled(False)
        self.catalogue_directory_button.setVisible(catalogue_tree)

        for btn in (
            self.parent_directory_button,
            self.refresh_button,
            self.change_directory_button,
            self.active_directory_button,
            self.catalogue_directory_button,
            self.open_file_button,
        ):
            btn.setMinimumHeight(28)
            actions.addWidget(btn)
        layout.addWidget(self.actions_host)

        # --- Search input + Faceted Filter Drawer toggle ---
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        self.search = SearchLineEdit(self)
        self.search.setProperty("precisionArrowStepping", False)
        self.search.setPlaceholderText("Filter runs, operator, state, or sample…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Filter result files")
        search_row.addWidget(self.search, 1)

        self.filters_toggle_button = ToggleToolButton(FluentIcon.FILTER, self)
        self.filters_toggle_button.setAccessibleName("Toggle faceted run filters")
        self.filters_toggle_button.setToolTip(
            "Show or hide state, operator, date, and grouping filters"
        )
        self.filters_toggle_button.setChecked(False)
        search_row.addWidget(self.filters_toggle_button)

        self.clear_filter_btn = PushButton("Clear", self)
        self.clear_filter_btn.setToolTip("Reset all search and filter criteria")
        self.clear_filter_btn.setEnabled(False)
        search_row.addWidget(self.clear_filter_btn)
        layout.addLayout(search_row)

        # --- Quick Scope Filter Switch (All / Sweeps / Post-proc / Resumable) ---
        self.scope_switch = SegmentedWidget(self)
        self.scope_switch.setAccessibleName("Quick run scope filter")
        for key, label in (
            ("all", "All"),
            ("sweeps", "Sweeps"),
            ("postprocessing", "Post-proc"),
            ("resumable", "Resumable"),
        ):
            self.scope_switch.addItem(
                key,
                label,
                onClick=lambda _checked=False, scope=key: self._on_scope_changed(scope),
            )
        self.scope_switch.setCurrentItem("all")
        layout.addWidget(self.scope_switch)

        # --- Collapsible 2x2 Faceted Filter Panel (prevents combo text truncation) ---
        self.filters_panel = QWidget(self)
        filter_grid = QGridLayout(self.filters_panel)
        filter_grid.setContentsMargins(0, 2, 0, 2)
        filter_grid.setHorizontalSpacing(6)
        filter_grid.setVerticalSpacing(6)

        self.state_filter = ComboBox(self.filters_panel)
        self.state_filter.setAccessibleName("Filter by state")
        self.state_filter.addItems(
            ["All states", "completed", "incomplete", "faulted", "aborted", "THATEC", "unreadable"]
        )
        self.state_filter.setToolTip("Filter by run execution state")
        filter_grid.addWidget(self.state_filter, 0, 0)

        self.operator_filter = ComboBox(self.filters_panel)
        self.operator_filter.setAccessibleName("Filter by operator")
        self.operator_filter.addItem("All operators")
        self.operator_filter.setToolTip("Filter by authenticated operator")
        filter_grid.addWidget(self.operator_filter, 0, 1)

        self.date_filter = ComboBox(self.filters_panel)
        self.date_filter.setAccessibleName("Filter by date")
        self.date_filter.addItems(
            ["All time", "Today", "Yesterday", "Last 7 days", "Last 30 days"]
        )
        self.date_filter.setToolTip("Filter by recording date range")
        filter_grid.addWidget(self.date_filter, 1, 0)

        self.view_mode_combo = ComboBox(self.filters_panel)
        self.view_mode_combo.setAccessibleName("View mode")
        self.view_mode_combo.addItem("Flat list", userData="flat")
        self.view_mode_combo.addItem("Group by date", userData="grouped")
        self.view_mode_combo.addItem("Group by Sample & DUT", userData="sample")
        self.view_mode_combo.addItem("Sample catalogue tree", userData="catalogue")
        self.view_mode_combo.setToolTip(
            "Switch between flat, date-grouped, sample-grouped, and catalogue tree views"
        )
        filter_grid.addWidget(self.view_mode_combo, 1, 1)
        filter_grid.setColumnStretch(0, 1)
        filter_grid.setColumnStretch(1, 1)

        for combo in (
            self.state_filter,
            self.operator_filter,
            self.date_filter,
            self.view_mode_combo,
        ):
            combo.setMinimumWidth(90)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        self.filters_panel.hide()
        self.filters_toggle_button.toggled.connect(self.filters_panel.setVisible)
        layout.addWidget(self.filters_panel)

        # --- Results table ---
        self.runs = TreeWidget(self)
        self.runs.setHeaderLabels(["File", "Date", "State", "Operator", "Spectra", "Points"])
        self.runs.setMinimumWidth(240)
        self.runs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.runs.setUniformRowHeights(True)
        self.runs.setAlternatingRowColors(False)
        self.runs.setAccessibleName("Recorded measurement results and artifacts")
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

        self.runs.setColumnWidth(COL_FILE, 178 if catalogue_tree else 178)
        self.runs.setColumnWidth(COL_DATE, 108)
        self.runs.setColumnWidth(COL_STATE, 74)
        self.runs.setColumnWidth(COL_OPERATOR, 90)
        self.runs.setColumnWidth(COL_SPECTRA, 56)
        self.runs.setColumnWidth(COL_POINTS, 56)

        self.state_card = ResultsStateCard(self)
        self.content = QStackedWidget(self)
        self.content.addWidget(self.runs)
        self.content.addWidget(self.state_card)
        layout.addWidget(self.content, 1)

        # --- Pagination toolbar ---
        self.pagination_bar = QWidget(self)
        pag_layout = QHBoxLayout(self.pagination_bar)
        pag_layout.setContentsMargins(0, 2, 0, 0)
        pag_layout.setSpacing(4)

        self.first_page_btn = PushButton("|<", self.pagination_bar)
        self.first_page_btn.setFixedWidth(32)
        self.first_page_btn.setToolTip("First page")
        self.prev_page_btn = PushButton("<", self.pagination_bar)
        self.prev_page_btn.setFixedWidth(30)
        self.prev_page_btn.setToolTip("Previous page")

        self.page_label = CaptionLabel("Page 1 of 1 (0 runs)", self.pagination_bar)
        self.page_label.setObjectName("muted")

        self.next_page_btn = PushButton(">", self.pagination_bar)
        self.next_page_btn.setFixedWidth(30)
        self.next_page_btn.setToolTip("Next page")
        self.last_page_btn = PushButton(">|", self.pagination_bar)
        self.last_page_btn.setFixedWidth(32)
        self.last_page_btn.setToolTip("Last page")

        pag_layout.addWidget(self.first_page_btn)
        pag_layout.addWidget(self.prev_page_btn)
        pag_layout.addWidget(self.page_label)
        pag_layout.addWidget(self.next_page_btn)
        pag_layout.addWidget(self.last_page_btn)
        pag_layout.addStretch(1)

        pag_layout.addWidget(CaptionLabel("Rows:", self.pagination_bar))
        self.page_size_combo = ComboBox(self.pagination_bar)
        self.page_size_combo.setFixedWidth(68)
        self.page_size_combo.addItems(["5", "15", "25", "50", "100", "All"])
        self.page_size_combo.setCurrentIndex(2)  # default 25
        pag_layout.addWidget(self.page_size_combo)

        layout.addWidget(self.pagination_bar)

        # --- Connections ---
        self.refresh_button.clicked.connect(self.refresh)
        self.change_directory_button.clicked.connect(self.choose_directory)
        self.open_file_button.clicked.connect(self.browse_file)
        self.parent_directory_button.clicked.connect(
            lambda: self.set_output_directory(self._output_dir.parent)
        )
        self.active_directory_button.clicked.connect(self._return_to_active_directory)
        self.catalogue_directory_button.clicked.connect(
            lambda: self.set_output_directory(self._catalogue_root)
        )
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

        if self._catalogue_tree_requested:
            self._view_mode = "catalogue"
            self.view_mode_combo.setCurrentIndex(self.view_mode_combo.findData("catalogue"))
            self.pagination_bar.setVisible(False)
        self._sync_directory_actions()

    def minimumSizeHint(self) -> QSize:
        return QSize(280, 200)

    def _sync_action_height(self):
        self.actions_host.setMinimumHeight(self._actions_flow.heightForWidth(max(260, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_action_height()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._sync_action_height)

    @property
    def selected_path(self) -> Path | None:
        return self._selected_path

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    def set_output_directory(self, output_dir: str | Path) -> None:
        next_output_dir = Path(output_dir).expanduser().absolute()
        if next_output_dir == self._output_dir:
            return
        self._output_dir = next_output_dir
        self._sync_directory_actions()
        self._selected_path = None
        self._pending_selected_path = None
        self.directory_changed.emit(self._output_dir)
        self.file_selected.emit(None)
        self.refresh()

    def set_sample_context(
        self, active_directory: str | Path | None, catalogue_root: str | Path
    ) -> None:
        """Follow a changed active DUT, retaining manual navigation on refresh."""
        active = Path(active_directory).expanduser().absolute() if active_directory is not None else None
        root = Path(catalogue_root).expanduser().absolute()
        changed = (
            not self._context_initialized
            or active != self._active_directory
            or root != self._catalogue_root
        )
        self._active_directory, self._catalogue_root = active, root
        self._context_initialized = True
        self._sync_directory_actions()
        if changed:
            self.set_output_directory(active or root)

    def _return_to_active_directory(self):
        if self._active_directory is not None:
            self.set_output_directory(self._active_directory)

    def _on_breadcrumb_clicked(self, route_key: str) -> None:
        target = self._breadcrumb_paths.get(route_key)
        if target is not None and target != self._output_dir:
            self.set_output_directory(target)

    def _sync_breadcrumbs(self) -> None:
        """Rebuild clickable Fluent BreadcrumbBar segments for the current sample/folder path."""
        crumbs: list[tuple[str, Path]] = []
        try:
            relative = self._output_dir.relative_to(self._catalogue_root)
            root_label = "Samples" if self._catalogue_tree_requested else (self._catalogue_root.name or str(self._catalogue_root))
            crumbs.append((root_label, self._catalogue_root))
            accum = self._catalogue_root
            for part in relative.parts:
                accum = accum / part
                # Skip pure boilerplate intermediate folder "devices" in display crumbs
                if part.casefold() == "devices" and accum != self._output_dir:
                    continue
                crumbs.append((_elide_crumb(part, 16), accum))
        except ValueError:
            ancestors: list[Path] = []
            cur = self._output_dir
            for _ in range(3):
                ancestors.append(cur)
                if cur.parent == cur:
                    break
                cur = cur.parent
            for path in reversed(ancestors):
                label = path.name or str(path)
                crumbs.append((_elide_crumb(label, 16), path))

        self._breadcrumb_paths = {}
        was_blocked = self.breadcrumb_bar.blockSignals(True)
        try:
            self.breadcrumb_bar.clear()
            for idx, (label, path) in enumerate(crumbs):
                key = f"crumb-{idx}-{path}"
                self._breadcrumb_paths[key] = path
                self.breadcrumb_bar.addItem(key, label)
                item = self.breadcrumb_bar.itemMap.get(key)
                if item is not None:
                    item.setToolTip(str(path))
        finally:
            self.breadcrumb_bar.blockSignals(was_blocked)

    def _sync_directory_actions(self):
        self._sync_breadcrumbs()
        try:
            relative = self._output_dir.relative_to(self._catalogue_root)
            display = (
                " / ".join(p for p in relative.parts if p.casefold() != "devices")
                if relative.parts
                else ("All samples" if self._catalogue_tree_requested else str(self._output_dir))
            )
        except ValueError:
            display = str(self._output_dir)
        self.location.setText(display)
        self.location.setToolTip(str(self._output_dir))
        self.parent_directory_button.setEnabled(self._output_dir.parent != self._output_dir)
        self.parent_directory_button.setToolTip(f"Browse parent folder: {self._output_dir.parent}")
        self.active_directory_button.setEnabled(self._active_directory is not None)
        self.active_directory_button.setToolTip(
            f"Return to the active sample/device: {self._active_directory}"
            if self._active_directory is not None
            else "Select an active sample/device in Samples."
        )
        self.catalogue_directory_button.setToolTip(
            f"Browse the complete Samples catalogue: {self._catalogue_root}"
        )

    def refresh(self) -> None:
        """Reload known results and preserve selection only when it still exists."""
        self._sync_directory_actions()
        self._cancel_refresh()
        self.state_card.show_state(
            title="Loading result files",
            description="Reading the result index in the background...",
            accessible_name="Loading result files",
            loading=True,
        )
        if not self.has_files():
            self.content.setCurrentWidget(self.state_card)
        task = ResultReadTask(
            self._refresh_request_id,
            _read_catalogue,
            self._output_dir,
            self._catalogue_tree_requested,
            cooperative_cancel=True,
        )
        self._refresh_task = task
        task.signals.loaded.connect(self._on_refresh_loaded)
        task.signals.failed.connect(self._on_refresh_failed)
        self._read_pool.start(task)

    def _cancel_refresh(self) -> None:
        self._refresh_request_id += 1
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        self._read_pool.clear()

    def _on_refresh_loaded(self, request_id: int, summaries: object) -> None:
        if request_id != self._refresh_request_id:
            return
        self._refresh_task = None
        if not isinstance(summaries, _CatalogueIndex):
            self._on_refresh_failed(request_id, "The result index returned an invalid payload.")
            return
        self._catalogue_artifacts = list(summaries.artifacts)
        self._artifact_timestamps = summaries.timestamps
        pending = self._pending_selected_path
        self._pending_selected_path = None
        self._populate_summaries(summaries.summaries, self._selected_path)
        if pending is not None:
            self.select_path(pending)

    def _on_refresh_failed(self, request_id: int, message: str) -> None:
        if request_id != self._refresh_request_id:
            return
        self._refresh_task = None
        self._pending_selected_path = None
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
        if previous is not None:
            if self._find_item_by_path(previous) is not None:
                self.file_selected.emit(previous)
            elif not native_path(previous).is_file():
                self._selected_path = None
                self.file_selected.emit(None)
        self.files_loaded.emit(self.has_files())

    def _on_scope_changed(self, scope: str) -> None:
        if scope == self._scope:
            return
        self._scope = scope
        self._on_filter_changed()

    def _on_filter_changed(self) -> None:
        self._current_page = 1
        has_active_filters = (
            bool(self.search.text().strip())
            or self._scope != "all"
            or self.state_filter.currentIndex() > 0
            or self.operator_filter.currentIndex() > 0
            or self.date_filter.currentIndex() > 0
        )
        self.clear_filter_btn.setEnabled(has_active_filters)
        self._filter_and_render(previous=self._selected_path)

    def clear_filters(self) -> None:
        controls = (self.search, self.state_filter, self.operator_filter, self.date_filter)
        for control in controls:
            control.blockSignals(True)
        self.search.clear()
        for control in controls[1:]:
            control.setCurrentIndex(0)
        for control in controls:
            control.blockSignals(False)
        self._scope = "all"
        self.scope_switch.setCurrentItem("all")
        self.clear_filter_btn.setEnabled(False)
        self._current_page = 1
        self._filter_and_render(previous=self._selected_path)

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

    @staticmethod
    def _is_postprocessing_path(path: Path) -> bool:
        return "postprocessing" in {part.casefold() for part in path.parts}

    def _filter_and_render(self, previous: Path | None = None) -> None:
        query = self.search.text().strip().casefold()
        state_filter = self.state_filter.currentText()
        operator_filter = self.operator_filter.currentText()
        date_filter = self.date_filter.currentText()
        scope = self._scope

        now = datetime.now(UTC)

        filtered: list[RunSummary] = []
        for s in self._all_summaries:
            is_post = self._is_postprocessing_path(s.path)
            if scope == "sweeps" and is_post:
                continue
            if scope == "postprocessing" and not is_post:
                continue
            if scope == "resumable" and s.status.casefold() not in _RESUMABLE_STATES:
                continue

            formatted_date = format_timestamp(s.created_at_utc)
            if query:
                searchable = (
                    f"{s.path.name} {s.path.parent.name} {s.status} {s.operator or ''} {formatted_date} "
                    f"{s.sample_id or ''} {s.sample_name or ''} {s.sample_row or ''} "
                    f"{s.sample_col or ''} {s.sample_coordinate_label or ''}"
                ).casefold()
                if query not in searchable:
                    continue

            if state_filter != "All states" and s.status.casefold() != state_filter.casefold():
                continue

            if (
                operator_filter != "All operators"
                and (s.operator or "").casefold() != operator_filter.casefold()
            ):
                continue

            if date_filter != "All time":
                cat = categorize_date(s.created_at_utc, now)
                if (
                    (date_filter == "Today" and cat != "Today")
                    or (date_filter == "Yesterday" and cat != "Yesterday")
                    or (
                        date_filter == "Last 7 days"
                        and cat not in ("Today", "Yesterday", "This week")
                    )
                    or (
                        date_filter == "Last 30 days"
                        and cat not in ("Today", "Yesterday", "This week", "This month")
                    )
                ):
                    continue

            filtered.append(s)

        self._filtered_summaries = filtered
        self._filtered_artifacts = []
        if self._view_mode == "catalogue" and scope in {"all", "postprocessing"}:
            for artifact in self._catalogue_artifacts:
                is_post = self._is_postprocessing_path(artifact)
                if scope == "postprocessing" and not is_post:
                    continue
                searchable = f"{artifact.name} {artifact} " + " ".join(artifact.parts)
                if query and query not in searchable.casefold():
                    continue
                if state_filter != "All states":
                    continue
                if date_filter != "All time":
                    timestamp = self._artifact_timestamps.get(artifact)
                    cat = categorize_date(timestamp, now)
                    if (
                        (date_filter == "Today" and cat != "Today")
                        or (date_filter == "Yesterday" and cat != "Yesterday")
                        or (
                            date_filter == "Last 7 days"
                            and cat not in ("Today", "Yesterday", "This week")
                        )
                        or (
                            date_filter == "Last 30 days"
                            and cat not in ("Today", "Yesterday", "This week", "This month")
                        )
                    ):
                        continue
                self._filtered_artifacts.append(artifact)
        self._render_page(previous=previous)

    def _render_page(self, previous: Path | None = None) -> None:
        total_items = len(self._filtered_summaries)
        if self._view_mode == "catalogue":
            total_items += len(self._filtered_artifacts)
        total_pages = self._total_pages()
        self._current_page = max(1, min(self._current_page, total_pages))

        self.page_label.setText(
            f"Page {self._current_page} of {total_pages} ({total_items} runs)"
        )
        self.first_page_btn.setEnabled(self._current_page > 1)
        self.prev_page_btn.setEnabled(self._current_page > 1)
        self.next_page_btn.setEnabled(self._current_page < total_pages)
        self.last_page_btn.setEnabled(self._current_page < total_pages)

        all_entries = len(self._all_summaries) + len(self._catalogue_artifacts)
        if all_entries == 0:
            self._state_action = self.browse_file
            self.state_card.show_state(
                title="No recorded results yet",
                description=(
                    "Open a THATEC/PyThat HDF5 file, or choose another result "
                    "directory to inspect stored spectra and measurement artifacts."
                ),
                accessible_name="No measurement result files",
                action_text="Open result file...",
            )
            self.content.setCurrentWidget(self.state_card)
            return
        if total_items == 0:
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

        now = datetime.now(UTC)

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
        elif self._view_mode == "sample":
            sample_groups: dict[str, list[RunSummary]] = defaultdict(list)
            for s in self._filtered_summaries:
                if s.sample_name or s.sample_id:
                    s_label = s.sample_name or s.sample_id
                    sample_groups[f"Sample: {s_label}"].append(s)
                else:
                    sample_groups["Unassigned Sample"].append(s)

            for title in sorted(sample_groups.keys()):
                items = sample_groups[title]
                group_item = SampleGroupItem(title, len(items))
                self.runs.addTopLevelItem(group_item)
                for s in items:
                    date_text = format_timestamp(s.created_at_utc)
                    child = ResultFileItem(s, date_text)
                    if s.sample_row and s.sample_col:
                        coord_lbl = f"R{s.sample_row}:C{s.sample_col}"
                        if s.sample_coordinate_label:
                            coord_lbl += f" ({s.sample_coordinate_label})"
                        child.setText(COL_OPERATOR, f"{coord_lbl} · {s.operator or '—'}")
                    group_item.addChild(child)
            self.runs.expandAll()
        elif self._view_mode == "catalogue":
            self._render_catalogue_tree(self._filtered_summaries, self._filtered_artifacts)
        else:
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

    def _catalogue_parts(self, summary: RunSummary) -> tuple[str, ...]:
        """Return path components relative to the configured catalogue root."""
        try:
            relative = summary.path.absolute().relative_to(self._output_dir.absolute())
            parts = tuple(part for part in relative.parts if part)
        except ValueError:
            parts = (summary.path.name,)
        return parts or (summary.path.name,)

    @staticmethod
    def _catalogue_label(part: str) -> str:
        labels = {
            "measurements": "Measurements",
            "sweeps": "Sweeps",
            "postprocessing": "Post-processing",
            "attachments": "Attachments",
            "characterization": "Characterization",
            "references": "References",
        }
        return labels.get(part.casefold(), part)

    def _render_catalogue_tree(
        self, summaries: list[RunSummary], artifacts: list[Path]
    ) -> None:
        """Render the sample catalogue with run bundles consolidated into 1-click rows.

        Each ``<bundle>/data.h5`` becomes a first-class selectable run row titled
        from its bundle folder (e.g. ``#3 · FMR Sweep``). Companion files
        (``metadata.json``, ``recipe.yml``, ``events.jsonl``, ``references/``…)
        are nested inside a collapsed ``Bundle files`` child node so the run list
        stays clean while keeping every artifact reachable.
        """
        # 1. Identify run bundles (directories containing data.h5)
        bundle_runs: dict[tuple[str, ...], tuple[tuple[str, ...], RunSummary]] = {}
        standalone_runs: list[tuple[tuple[str, ...], RunSummary]] = []
        for summary in summaries:
            parts = self._catalogue_parts(summary)
            if summary.path.name == "data.h5" and len(parts) >= 2:
                bundle_key = parts[:-1]
                bundle_runs[bundle_key] = (parts, summary)
            else:
                standalone_runs.append((parts, summary))

        # 2. Partition artifacts AND nested bundle .h5 files (e.g. references/ref0000.h5) into bundle companions
        bundle_companions: dict[tuple[str, ...], list[tuple[tuple[str, ...], Path]]] = defaultdict(
            list
        )
        true_standalone_runs: list[tuple[tuple[str, ...], RunSummary]] = []
        for parts, summary in standalone_runs:
            matched_bundle = next(
                (parts[:d] for d in range(len(parts) - 1, 0, -1) if parts[:d] in bundle_runs),
                None,
            )
            if matched_bundle is not None:
                bundle_companions[matched_bundle].append(
                    (parts[len(matched_bundle) :], summary.path)
                )
            else:
                true_standalone_runs.append((parts, summary))
        standalone_runs = true_standalone_runs

        seen_companion_paths: set[Path] = {
            art_path.absolute()
            for items in bundle_companions.values()
            for _rel, art_path in items
        }
        standalone_artifacts: list[tuple[tuple[str, ...], Path]] = []
        for artifact in artifacts:
            if artifact.absolute() in seen_companion_paths:
                continue
            parts = self._relative_catalogue_parts(artifact)
            matched_bundle = None
            for depth in range(len(parts) - 1, 0, -1):
                prefix = parts[:depth]
                if prefix in bundle_runs:
                    matched_bundle = prefix
                    break
            if matched_bundle is not None:
                bundle_companions[matched_bundle].append((parts[len(matched_bundle) :], artifact))
            else:
                standalone_artifacts.append((parts, artifact))

        # 3. Count logical items per folder (counting each run bundle as 1 run!)
        logical_entries: list[tuple[tuple[str, ...], object]] = []
        for bundle_key, (parts, summary) in bundle_runs.items():
            # Bundle sits at bundle_key (one level above data.h5!)
            logical_entries.append((bundle_key, ("bundle", parts, summary)))
        for parts, summary in standalone_runs:
            logical_entries.append((parts, ("run", parts, summary)))
        for parts, artifact in standalone_artifacts:
            logical_entries.append((parts, ("artifact", parts, artifact)))

        logical_entries.sort(key=lambda item: tuple(part.casefold() for part in item[0]))

        counts: dict[tuple[str, ...], int] = defaultdict(int)
        for key_parts, _payload in logical_entries:
            for depth in range(1, len(key_parts)):
                counts[key_parts[:depth]] += 1

        groups: dict[tuple[str, ...], CatalogueGroupItem] = {}

        def ensure_group(path_parts: tuple[str, ...]) -> QTreeWidgetItem | TreeWidget:
            parent: QTreeWidgetItem | TreeWidget = self.runs
            for depth in range(1, len(path_parts) + 1):
                key = path_parts[:depth]
                group = groups.get(key)
                if group is None:
                    label = self._catalogue_label(path_parts[depth - 1])
                    collapse_default = path_parts[depth - 1].casefold() == "postprocessing"
                    group = CatalogueGroupItem(
                        label, counts[key], collapsed_by_default=collapse_default
                    )
                    groups[key] = group
                    if depth == 1:
                        self.runs.addTopLevelItem(group)
                    else:
                        parent.addChild(group)
                parent = group
            return parent

        for key_parts, payload in logical_entries:
            kind, full_parts, obj = payload
            parent = ensure_group(key_parts[:-1]) if len(key_parts) > 1 else self.runs

            if kind == "bundle":
                summary: RunSummary = obj
                folder_name = key_parts[-1]
                display_name, sweep_num = _format_run_bundle_label(
                    folder_name, fallback=summary.path.name
                )
                leaf = ResultFileItem(
                    summary,
                    format_timestamp(summary.created_at_utc),
                    display_name=display_name,
                    sweep_number=sweep_num,
                )
                leaf.setToolTip(
                    COL_FILE,
                    f"Measurement run: {folder_name}\nHDF5 archive: {summary.path.absolute()}",
                )
                if isinstance(parent, TreeWidget):
                    self.runs.addTopLevelItem(leaf)
                else:
                    parent.addChild(leaf)

                companions = bundle_companions.get(key_parts, [])
                if companions:
                    bundle_group = CatalogueGroupItem(
                        "Bundle files", len(companions), collapsed_by_default=True
                    )
                    leaf.addChild(bundle_group)
                    subgroups: dict[tuple[str, ...], CatalogueGroupItem] = {}
                    subcounts: dict[tuple[str, ...], int] = defaultdict(int)
                    for rel_parts, _art in companions:
                        for d in range(1, len(rel_parts)):
                            subcounts[rel_parts[:d]] += 1
                    for rel_parts, art_path in sorted(
                        companions, key=lambda pair: tuple(p.casefold() for p in pair[0])
                    ):
                        art_parent: QTreeWidgetItem = bundle_group
                        for d in range(1, len(rel_parts)):
                            subkey = rel_parts[:d]
                            sub = subgroups.get(subkey)
                            if sub is None:
                                sub = CatalogueGroupItem(
                                    self._catalogue_label(rel_parts[d - 1]),
                                    subcounts[subkey],
                                    collapsed_by_default=True,
                                )
                                subgroups[subkey] = sub
                                art_parent.addChild(sub)
                            art_parent = sub
                        art_leaf = CatalogueArtifactItem(
                            art_path, self._artifact_timestamps.get(art_path)
                        )
                        art_leaf.setText(COL_FILE, rel_parts[-1])
                        art_parent.addChild(art_leaf)
            elif kind == "run":
                summary = obj
                leaf = ResultFileItem(
                    summary,
                    format_timestamp(summary.created_at_utc),
                    display_name=full_parts[-1],
                )
                leaf.setToolTip(
                    COL_FILE,
                    f"Relative path: {Path(*full_parts)}\nAbsolute path: {summary.path.absolute()}",
                )
                if isinstance(parent, TreeWidget):
                    self.runs.addTopLevelItem(leaf)
                else:
                    parent.addChild(leaf)
            else:
                art_path: Path = obj
                leaf = CatalogueArtifactItem(art_path, self._artifact_timestamps.get(art_path))
                leaf.setText(COL_FILE, full_parts[-1])
                leaf.setToolTip(
                    COL_FILE,
                    f"Relative path: {Path(*full_parts)}\nAbsolute path: {art_path.absolute()}",
                )
                if isinstance(parent, TreeWidget):
                    self.runs.addTopLevelItem(leaf)
                else:
                    parent.addChild(leaf)

        # Smart expansion: open Measurements/Sweeps so runs are 1 click away,
        # keep Post-processing and Bundle sidecars neatly collapsed unless searching.
        searching = bool(self.search.text().strip()) or self._scope == "postprocessing"

        def apply_expansion(item: QTreeWidgetItem) -> None:
            if isinstance(item, CatalogueGroupItem):
                should_expand = searching or not item.collapsed_by_default
                item.setExpanded(should_expand)
            elif isinstance(item, ResultFileItem):
                item.setExpanded(searching)
            for idx in range(item.childCount()):
                apply_expansion(item.child(idx))

        for top_idx in range(self.runs.topLevelItemCount()):
            apply_expansion(self.runs.topLevelItem(top_idx))

    def _relative_catalogue_parts(self, path: Path) -> tuple[str, ...]:
        try:
            relative = path.absolute().relative_to(self._output_dir.absolute())
            parts = tuple(part for part in relative.parts if part)
        except ValueError:
            parts = (path.name,)
        return parts or (path.name,)

    def choose_directory(self) -> None:
        """Browse another directory without changing persisted station settings."""
        selected = QFileDialog.getExistingDirectory(
            self,
            "Browse result directory",
            str(self._output_dir),
        )
        if not selected:
            return
        self._output_dir = Path(selected).expanduser().absolute()
        self._sync_directory_actions()
        self._selected_path = None
        self._pending_selected_path = None
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
        self._pending_selected_path = None
        target = Path(path).expanduser().absolute()
        if not native_path(target).is_file():
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

        existing_summary = next((s for s in self._all_summaries if s.path == target), None)
        if existing_summary is None:
            self._cancel_refresh()
            self._selected_path = target
            task = ResultReadTask(self._refresh_request_id, _read_file_summary, target)
            self._refresh_task = task
            task.signals.loaded.connect(self._file_summary_loaded)
            task.signals.failed.connect(self._on_refresh_failed)
            self._read_pool.start(task)
            self.file_selected.emit(target)
            self.file_opened.emit(target)
            return

        self.clear_filters()
        self._selected_path = target
        self._filter_and_render(previous=target)
        self._restore_selection(target)
        self.file_opened.emit(target)

    def _file_summary_loaded(self, request_id, summary):
        if request_id != self._refresh_request_id:
            return
        self._refresh_task = None
        self._all_summaries.insert(0, summary)
        self.clear_filters()
        self._filter_and_render(previous=summary.path)
        self.files_loaded.emit(self.has_files())
        pending = self._pending_selected_path
        self._pending_selected_path = None
        if pending is not None:
            self.select_path(pending)

    def select_path(self, path: str | Path) -> bool:
        """Select an already indexed result or catalogue artifact."""
        target = Path(path).expanduser().absolute()
        if not native_path(target).is_file():
            return False
        if target.suffix.lower() not in {".h5", ".hdf5", ".csv", ".pdf", ".json", ".jsonl", ".yml"}:
            return False
        if self._refresh_task is not None:
            self._pending_selected_path = target
            if target.suffix.lower() in {".csv", ".pdf", ".json", ".jsonl", ".yml"}:
                self._selected_path = target
                self.file_selected.emit(target)
            return True
        if target.suffix.lower() in {".csv", ".pdf", ".json", ".jsonl", ".yml"}:
            if target not in {artifact.absolute() for artifact in self._catalogue_artifacts}:
                return False
            self.clear_filters()
            self._selected_path = target
            self._filter_and_render(previous=target)
            self._restore_selection(target)
            self.file_selected.emit(target)
            return True
        if not any(summary.path.absolute() == target for summary in self._all_summaries):
            self.open_file(target)
            return True
        self.clear_filters()
        self._selected_path = target
        self._filter_and_render(previous=target)
        self._restore_selection(target)
        self.file_selected.emit(target)
        return True

    def has_files(self) -> bool:
        """Return whether the browser contains any entries before filtering."""
        return bool(self._all_summaries or self._catalogue_artifacts)

    def _on_current_changed(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if item is None or isinstance(
            item, (DateGroupItem, SampleGroupItem, CatalogueGroupItem)
        ):
            return
        path_str = str(
            item.data(COL_DATE, Qt.ItemDataRole.UserRole)
            or item.data(COL_FILE, Qt.ItemDataRole.UserRole)
            or ""
        )
        if not path_str:
            return
        path = Path(path_str).expanduser().absolute()
        self._pending_selected_path = None
        self._selected_path = path
        self.file_selected.emit(path)

    def _find_item_by_path(self, target: Path) -> QTreeWidgetItem | None:
        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if not isinstance(item, (DateGroupItem, SampleGroupItem, CatalogueGroupItem)):
                p_str = str(
                    item.data(COL_DATE, Qt.ItemDataRole.UserRole)
                    or item.data(COL_FILE, Qt.ItemDataRole.UserRole)
                    or ""
                )
                if p_str and Path(p_str).expanduser().absolute() == target.absolute():
                    return item
            for index in range(item.childCount()):
                found = walk(item.child(index))
                if found is not None:
                    return found
            return None

        for index in range(self.runs.topLevelItemCount()):
            found = walk(self.runs.topLevelItem(index))
            if found is not None:
                return found
        return None

    def _restore_selection(self, target: Path | None = None) -> bool:
        to_find = target or self._selected_path
        if to_find is None:
            return False
        item = self._find_item_by_path(to_find)
        if item is None:
            return False
        # Expand any collapsed parent containers (e.g. Bundle files / Post-processing)
        ancestor = item.parent()
        while ancestor is not None:
            ancestor.setExpanded(True)
            ancestor = ancestor.parent()
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
        self._read_pool.clear()
        if not self._read_pool.waitForDone(5_000):
            event.ignore()
            return
        super().closeEvent(event)
