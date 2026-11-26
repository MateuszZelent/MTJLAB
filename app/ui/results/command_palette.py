"""Keyboard-driven Fluent Command Palette (Ctrl+K) for ResultsPage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QListWidgetItem
from qfluentwidgets import CaptionLabel, ListWidget, SearchLineEdit

from app.ui.dialogs import StationDialog


@dataclass(frozen=True, slots=True)
class _CommandEntry:
    category: str
    label: str
    shortcut: str
    action: Callable[[], None]


class ResultsCommandPalette(StationDialog):
    """Search and invoke Results Browser views, toggles, and analysis tools."""

    def __init__(self, results_page) -> None:
        super().__init__(results_page)
        self._page = results_page
        self.setWindowTitle("Results Command Palette")
        self.resize(520, 400)
        self.setMinimumSize(420, 300)

        layout = self.modal_content_layout(spacing=8)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("Type a command, tab, or analysis tool…")
        self.search.setAccessibleName("Filter Results commands")
        self.search.textChanged.connect(self._filter_commands)
        self.search.installEventFilter(self)
        layout.addWidget(self.search)

        self.list_widget = ListWidget(self)
        self.list_widget.setAccessibleName("Matching Results commands")
        self.list_widget.itemActivated.connect(self._execute_item)
        self.list_widget.itemClicked.connect(self._execute_item)
        layout.addWidget(self.list_widget, 1)

        hint = CaptionLabel("Enter to run · Esc to close", self)
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self._commands = self._build_commands()
        self._filtered: list[_CommandEntry] = []
        self._filter_commands("")
        self.search.setFocus()

    def _build_commands(self) -> list[_CommandEntry]:
        page = self._page
        commands = [
            _CommandEntry("View", "Go to Overview & Metadata", "", lambda: page.result_tabs.setCurrentIndex(page._overview_index)),
            _CommandEntry("View", "Go to Sweep Tree (shared with Sweeps)", "", lambda: page.result_tabs.setCurrentIndex(page._tree_index)),
            _CommandEntry("View", "Go to I/V/P & Scalars", "", lambda: page.result_tabs.setCurrentIndex(page._scalar_index)),
            _CommandEntry("View", "Go to Spectrum Workbench", "", lambda: page.result_tabs.setCurrentIndex(page._spectrum_index)),
            _CommandEntry("Layout", "Toggle Run Browser Sidebar", "Ctrl+B", page.toggle_browser_sidebar),
            _CommandEntry("File", "Open External HDF5 / PyThat File…", "", page.browse_result_file),
            _CommandEntry("File", "Refresh Result Catalogue", "", page.refresh),
        ]
        if not page.result_tabs.navigation.widget(page.result_tabs._routes[page._heatmap_index]).isHidden():
            commands.append(
                _CommandEntry("View", "Go to 2D Heatmaps & 1D Slice", "", lambda: page.result_tabs.setCurrentIndex(page._heatmap_index))
            )
        if hasattr(page.spectrum_tab, "toggle_inspector"):
            commands.append(
                _CommandEntry("Layout", "Toggle Spectrum Analysis Inspector", "I", page.spectrum_tab.toggle_inspector)
            )
        if hasattr(page.spectrum_tab, "toggle_resonance_fit"):
            commands.append(
                _CommandEntry("Fit", "Toggle Resonance Curve Fit (Lorentzian / Gaussian)", "F", lambda: (page.result_tabs.setCurrentIndex(page._spectrum_index), page.spectrum_tab.toggle_resonance_fit()))
            )
        if hasattr(page.spectrum_tab, "toggle_ab_wipe"):
            commands.append(
                _CommandEntry("DSP", "Toggle A/B Wipe Curtain (Raw HDF5 vs Processed)", "W", lambda: (page.result_tabs.setCurrentIndex(page._spectrum_index), page.spectrum_tab.toggle_ab_wipe()))
            )
        if hasattr(page.heatmap_tab, "toggle_inspector"):
            commands.append(
                _CommandEntry("Layout", "Toggle Heatmap Analysis Inspector", "I", page.heatmap_tab.toggle_inspector)
            )
        if hasattr(page.heatmap_tab, "toggle_kittel_overlay"):
            commands.append(
                _CommandEntry("Physics", "Toggle Kittel / Ridge Dispersion Overlay", "", lambda: (page.result_tabs.setCurrentIndex(page._heatmap_index), page.heatmap_tab.toggle_kittel_overlay()))
            )
        if page.open_sweep_button.isEnabled():
            commands.append(_CommandEntry("Sweeps", "Open Reconstructed Sweep in Sweeps Tab", "", page._request_open_sweep))
        if page.resume_button.isEnabled():
            commands.append(_CommandEntry("Run", "Resume from Safe Checkpoint", "", page._request_resume))
        return commands

    def _filter_commands(self, text: str) -> None:
        query = text.strip().casefold()
        self.list_widget.clear()
        self._filtered = [
            entry
            for entry in self._commands
            if not query or query in f"{entry.category} {entry.label} {entry.shortcut}".casefold()
        ]
        for entry in self._filtered:
            suffix = f"   [{entry.shortcut}]" if entry.shortcut else ""
            item = QListWidgetItem(f"[{entry.category}]  {entry.label}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.list_widget.addItem(item)
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def _execute_item(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, _CommandEntry):
            self.accept()
            entry.action()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down and self.list_widget.count():
                row = min(self.list_widget.count() - 1, self.list_widget.currentRow() + 1)
                self.list_widget.setCurrentRow(row)
                return True
            if key == Qt.Key.Key_Up and self.list_widget.count():
                row = max(0, self.list_widget.currentRow() - 1)
                self.list_widget.setCurrentRow(row)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._execute_item(self.list_widget.currentItem())
                return True
        return super().eventFilter(watched, event)
