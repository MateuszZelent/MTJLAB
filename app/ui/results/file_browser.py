"""File browser panel for the Results page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import PrimaryPushButton, PushButton, TreeWidget
from app.ui.dialogs import StationFileDialog as QFileDialog

from app.storage import Hdf5RunReader, ThatecRunReader


class FileBrowserPanel(QWidget):
    """Left-hand panel listing HDF5 result files."""

    file_selected = Signal(object)  # Path | None
    file_opened = Signal(object)    # Path

    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._output_dir = Path(output_dir)
        self._selected_path: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.location = QLabel()
        self.location.setObjectName("muted")
        layout.addWidget(self.location)

        actions = QHBoxLayout()
        refresh = PushButton("Refresh file list", self)
        open_file = PrimaryPushButton("Open HDF5 file…", self)
        actions.addWidget(refresh)
        actions.addWidget(open_file)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.runs = TreeWidget(self)
        self.runs.setHeaderLabels(["File", "State", "Spectra", "Points"])
        self.runs.setMinimumWidth(240)
        self.runs.setColumnWidth(0, 220)
        layout.addWidget(self.runs, 1)

        refresh.clicked.connect(self.refresh)
        open_file.clicked.connect(self.browse_file)
        self.runs.currentItemChanged.connect(self._on_current_changed)

    @property
    def selected_path(self) -> Path | None:
        return self._selected_path

    def refresh(self) -> None:
        """Reload the file list from the output directory."""
        self.location.setText(f"Directory: {self._output_dir.resolve()}")
        self.runs.clear()
        for summary in Hdf5RunReader.list_runs(self._output_dir):
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
            self.runs.addTopLevelItem(item)
        if self._selected_path is not None:
            self._restore_selection()

    def browse_file(self) -> None:
        """Open a file dialog to select an HDF5 result file."""
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Open THATEC HDF5 result",
            str(self._output_dir),
            "HDF5 results (*.h5 *.hdf5);;All files (*)",
        )
        if selected:
            self.open_file(Path(selected))

    def open_file(self, path: Path) -> None:
        """Add and select an arbitrary result file."""
        target = Path(path)
        existing = self._find_item_by_path(target)
        if existing is None:
            try:
                run = ThatecRunReader.describe(target)
            except Exception:
                # File will still be added with minimal info so operator sees it.
                run = None
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
                [target.name, "THATEC", str(spectrum_count), str(point_count)]
            )
            existing.setData(0, Qt.ItemDataRole.UserRole, str(target))
            existing.setToolTip(0, str(target.resolve()))
            self.runs.addTopLevelItem(existing)
        self.runs.setCurrentItem(existing)

    def has_files(self) -> bool:
        """Return whether the browser contains any entries."""
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

    def _restore_selection(self) -> None:
        if self._selected_path is None:
            return
        item = self._find_item_by_path(self._selected_path)
        if item is not None:
            self.runs.setCurrentItem(item)
