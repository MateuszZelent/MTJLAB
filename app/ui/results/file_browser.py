"""Fluent file browser for immutable HDF5 measurement results."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, LineEdit, PrimaryPushButton, PushButton, TreeWidget

from app.storage import Hdf5RunReader, ThatecRunReader
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.location = BodyLabel(self)
        self.location.setObjectName("muted")
        self.location.setWordWrap(True)
        self.location.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.location)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.refresh_button = PushButton("Refresh", self)
        self.change_directory_button = PushButton("Change directory...", self)
        self.open_file_button = PrimaryPushButton(
            "Open HDF5 / PyThat file...", self
        )
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.change_directory_button)
        actions.addWidget(self.open_file_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.search = LineEdit(self)
        self.search.setProperty("precisionArrowStepping", False)
        self.search.setPlaceholderText("Filter by file name or run state")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Filter result files")
        layout.addWidget(self.search)

        self.runs = TreeWidget(self)
        self.runs.setHeaderLabels(["File", "State", "Spectra", "Points"])
        self.runs.setMinimumWidth(240)
        self.runs.setColumnWidth(0, 220)
        self.runs.setUniformRowHeights(True)
        self.runs.setAccessibleName("Recorded HDF5 results")

        self.state_card = ResultsStateCard(self)
        self.content = QStackedWidget(self)
        self.content.addWidget(self.runs)
        self.content.addWidget(self.state_card)
        layout.addWidget(self.content, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.change_directory_button.clicked.connect(self.choose_directory)
        self.open_file_button.clicked.connect(self.browse_file)
        self.search.textChanged.connect(self._apply_filter)
        self.runs.currentItemChanged.connect(self._on_current_changed)
        self.state_card.action_requested.connect(self._run_state_action)

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
        self.runs.blockSignals(True)
        self.runs.clear()
        for summary in summaries:
            item = QTreeWidgetItem(
                [
                    summary.path.name,
                    summary.status,
                    str(summary.spectrum_count),
                    str(summary.point_count),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, str(summary.path))
            item.setToolTip(0, str(summary.path.resolve()))
            item.setToolTip(
                1,
                f"{summary.status}; {summary.spectrum_count} spectra; "
                f"{summary.point_count} checkpoints",
            )
            self.runs.addTopLevelItem(item)
        self.runs.blockSignals(False)

        restored = previous is not None and self._restore_selection()
        if previous is not None and not restored:
            self._selected_path = None
            self.file_selected.emit(None)
        self._apply_filter(self.search.text())
        self.files_loaded.emit(self.has_files())

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

        existing = self._find_item_by_path(target)
        if existing is None:
            unreadable_reason = ""
            try:
                run = ThatecRunReader.describe(target)
            except Exception as exc:
                # Keep a malformed record visible; silently omitting it would hide
                # potentially recoverable scientific data from the operator.
                run = None
                unreadable_reason = str(exc)
            if run is not None:
                point_count = max(
                    (row.shape[0] for row in run.rows.values() if row.shape),
                    default=0,
                )
                spectrum_count = sum(
                    1 for row in run.rows.values() if len(row.shape) >= 2
                )
            else:
                point_count = 0
                spectrum_count = 0
            existing = QTreeWidgetItem(
                [
                    target.name,
                    "THATEC" if run is not None else "unreadable",
                    str(spectrum_count),
                    str(point_count),
                ]
            )
            existing.setData(0, Qt.ItemDataRole.UserRole, str(target))
            existing.setToolTip(0, str(target.resolve()))
            if unreadable_reason:
                existing.setToolTip(1, unreadable_reason)
            self.runs.addTopLevelItem(existing)

        self.search.clear()
        self.content.setCurrentWidget(self.runs)
        self.runs.setCurrentItem(existing)
        self.file_opened.emit(target)

    def has_files(self) -> bool:
        """Return whether the browser contains any entries before filtering."""

        return self.runs.topLevelItemCount() > 0

    def _on_current_changed(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if item is None:
            self._selected_path = None
            self.file_selected.emit(None)
            return
        path = Path(str(item.data(0, Qt.ItemDataRole.UserRole)))
        self._selected_path = path
        self.file_selected.emit(path)

    def _find_item_by_path(self, target: Path) -> QTreeWidgetItem | None:
        for index in range(self.runs.topLevelItemCount()):
            candidate = self.runs.topLevelItem(index)
            if Path(str(candidate.data(0, Qt.ItemDataRole.UserRole))) == target:
                return candidate
        return None

    def _restore_selection(self) -> bool:
        if self._selected_path is None:
            return False
        item = self._find_item_by_path(self._selected_path)
        if item is None:
            return False
        self.runs.setCurrentItem(item)
        return True

    def _apply_filter(self, text: str) -> None:
        query = text.strip().casefold()
        visible_count = 0
        for index in range(self.runs.topLevelItemCount()):
            item = self.runs.topLevelItem(index)
            searchable = " ".join(
                item.text(column) for column in range(self.runs.columnCount())
            ).casefold()
            visible = not query or query in searchable
            item.setHidden(not visible)
            visible_count += int(visible)

        if self.runs.topLevelItemCount() == 0:
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
        elif visible_count == 0:
            self._state_action = self.search.clear
            self.state_card.show_state(
                title="No matching result files",
                description=(
                    "Clear the filter or search for another file name or run state."
                ),
                accessible_name="No result files match the filter",
                action_text="Clear filter",
            )
            self.content.setCurrentWidget(self.state_card)
        else:
            self.content.setCurrentWidget(self.runs)

    def _show_error(self, title: str, description: str) -> None:
        self._state_action = lambda: self._apply_filter(self.search.text())
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
