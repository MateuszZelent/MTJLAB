"""Stored-run results page independent of device UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QSplitter,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, CardWidget, ComboBox, Pivot, PrimaryPushButton, PushButton, SubtitleLabel

from app.storage import (
    Hdf5RunReader,
    ThatecRunReader,
    read_pythat_run_data,
)
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.file_browser import FileBrowserPanel
from app.ui.results.heatmap_tab import HeatmapResultsTab
from app.ui.results.metadata_panel import MetadataPanel
from app.ui.results.spectrum_tab import SpectrumResultsTab
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.sweep_tree_panel import SweepTreePanel


class _FluentResultSections(QWidget):
    """Fluent Pivot navigation backed by a real page stack.

    It deliberately provides the operator-facing Results navigation. The
    small semantic page-management API stays local to this Fluent widget.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.navigation = Pivot(self)
        self.compact_navigation = ComboBox(self)
        self.compact_navigation.setAccessibleName("Results section")
        self.compact_navigation.hide()
        self.compact_navigation.currentIndexChanged.connect(self.setCurrentIndex)
        self.stack = QStackedWidget(self)
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        layout.addWidget(self.navigation)
        layout.addWidget(self.compact_navigation)
        layout.addWidget(self.stack, 1)
        self._routes: list[str] = []
        self._labels: list[str] = []
        self._compact_layout: bool | None = None
        self._navigation_sync_pending = False

    def addTab(self, page: QWidget, label: str) -> int:
        index = self.stack.addWidget(page)
        route = f"result-section-{index}"
        self._routes.append(route)
        self._labels.append(label)
        self.compact_navigation.addItem(label, userData=index)
        self.navigation.addItem(
            route,
            label,
            onClick=lambda _checked=False, index=index: self.setCurrentIndex(index),
        )
        if index == 0:
            self.setCurrentIndex(index)
        return index

    def setCurrentIndex(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.navigation.setCurrentItem(self._routes[index])
        if self.compact_navigation.currentIndex() != index:
            self.compact_navigation.blockSignals(True)
            self.compact_navigation.setCurrentIndex(index)
            self.compact_navigation.blockSignals(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_navigation_mode()
        self._schedule_navigation_sync()

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if event.type() in {QEvent.Type.Show, QEvent.Type.LayoutRequest}:
            self._schedule_navigation_sync()
        return result

    def _schedule_navigation_sync(self) -> None:
        if self._navigation_sync_pending:
            return
        self._navigation_sync_pending = True
        QTimer.singleShot(0, self._sync_navigation_mode)

    def _sync_navigation_mode(self) -> None:
        self._navigation_sync_pending = False
        # Pivot item widths can be recalculated after a theme or shell
        # navigation transition.  A fixed 620 px threshold allowed a cached
        # 900+ px Pivot to be shown in a narrower Results pane, clipping its
        # final route without producing a scrollbar.  Use both a comfortable
        # baseline and the navigation's current natural width.
        required_width = max(1_000, self.navigation.sizeHint().width() + 8)
        visible_width = self.visibleRegion().boundingRect().width()
        available_width = self.width()
        if visible_width > 0:
            available_width = min(available_width, visible_width)
        compact = available_width < required_width
        visibility_matches = (
            self.navigation.isHidden() == compact
            and self.compact_navigation.isHidden() == (not compact)
        )
        if compact == self._compact_layout and visibility_matches:
            return
        self._compact_layout = compact
        self.navigation.setVisible(not compact)
        self.compact_navigation.setVisible(compact)

    def setTabVisible(self, index: int, visible: bool) -> None:
        self.navigation.widget(self._routes[index]).setVisible(visible)
        self.compact_navigation.setItemEnabled(index, visible)
        if not visible and self.stack.currentIndex() == index:
            replacement = next(
                (
                    candidate
                    for candidate in range(self.stack.count())
                    if self.navigation.widget(self._routes[candidate]).isVisible()
                    and self.compact_navigation.isItemEnabled(candidate)
                ),
                -1,
            )
            if replacement >= 0:
                self.setCurrentIndex(replacement)



class ResultsPage(QWidget):
    """Browse immutable run files without opening an instrument session.

    The page is divided into three sections:

    * **Left** — ``FileBrowserPanel`` listing HDF5 results in the output
      directory.
    * **Right** — Fluent section navigation for:
        - *Overview* — run metadata, recipe snapshot, settings and device
          state (``MetadataPanel``).
        - *Sweep Tree* — reconstructed THATEC experiment hierarchy
          (``SweepTreePanel``).
        - *Spectrum* — individual spectrum browser with navigation
          (``SpectrumResultsTab``).
        - *Heatmaps* — 2-D colourmap of all checkpoints for spectral rows
          (``HeatmapResultsTab``).  Only visible when the result contains
          2-D data.
    """

    resume_requested = Signal(object)
    open_sweep_requested = Signal(object, object)

    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_path: Path | None = None
        self._thatec_run = None
        self._thatec_tree_available = False

        layout = QVBoxLayout(self)

        # --- Title ---
        title = SubtitleLabel("Results", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = BodyLabel(
            "Review recorded runs, reconstruct their executed Sweep, and resume only at "
            "confirmed safe checkpoints.",
            self,
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # --- Deliberate operational actions ---
        self.action_card = CardWidget(self)
        self.actions_layout = QHBoxLayout(self.action_card)
        actions = self.actions_layout
        actions.setContentsMargins(16, 12, 16, 12)
        action_copy = QVBoxLayout()
        action_copy.addWidget(BodyLabel("Run actions", self.action_card))
        action_note = BodyLabel(
            "Actions are enabled only when the selected result proves they are safe.",
            self.action_card,
        )
        action_note.setWordWrap(True)
        action_copy.addWidget(action_note)
        actions.addLayout(action_copy, 1)
        self.open_sweep_button = PrimaryPushButton("Open reconstructed Sweep", self.action_card)
        self.open_sweep_button.setEnabled(False)
        self.open_sweep_button.setToolTip(
            "Build the executed THATEC measurement tree in the Sweeps workspace."
        )
        self.resume_button = PushButton("Resume from safe checkpoint", self.action_card)
        self.resume_button.setEnabled(False)
        self.resume_button.setToolTip(
            "Available only for interrupted runs containing a confirmed safe boundary."
        )
        actions.addWidget(self.open_sweep_button)
        actions.addWidget(self.resume_button)
        actions.addStretch(1)
        layout.addWidget(self.action_card)

        # --- Main splitter ---
        self.results_splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter = self.results_splitter

        # Left: file browser
        self.file_browser = FileBrowserPanel(output_dir)
        splitter.addWidget(self.file_browser)

        # Right: tabbed result views
        self.result_tabs = _FluentResultSections()
        self.section_navigation = self.result_tabs.navigation
        self.result_stack = self.result_tabs.stack

        # Tab: Overview (metadata)
        self.metadata_panel = MetadataPanel()
        self._overview_index = self.result_tabs.addTab(
            self.metadata_panel, "Overview"
        )

        # Tab: Sweep Tree
        self.sweep_tree = SweepTreePanel()
        self._tree_index = self.result_tabs.addTab(
            self.sweep_tree, "Sweep Tree"
        )

        # Tab: Spectrum
        self.spectrum_tab = SpectrumResultsTab()
        self._spectrum_index = self.result_tabs.addTab(
            self.spectrum_tab, "Spectrum"
        )

        # Tab: Heatmaps
        self.heatmap_tab = HeatmapResultsTab()
        self._heatmap_index = self.result_tabs.addTab(
            self.heatmap_tab, "Heatmaps"
        )

        result_card = CardWidget(self)
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(16, 12, 16, 16)
        self.result_state = ResultsStateCard(result_card)
        self.result_state.set_compact(True)
        result_layout.addWidget(self.result_state)
        result_layout.addWidget(self.result_tabs)
        splitter.addWidget(result_card)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self._compact_layout: bool | None = None

        # --- Connections ---
        self.file_browser.file_selected.connect(self._on_file_selected)
        self.resume_button.clicked.connect(self._request_resume)
        self.open_sweep_button.clicked.connect(self._request_open_sweep)
        self.result_state.action_requested.connect(self.browse_result_file)

        # Cross-tab coordination: sweep tree → spectrum
        self.sweep_tree.spectrum_requested.connect(
            self.spectrum_tab.show_thatec_spectrum
        )
        self.sweep_tree.spectrum_requested.connect(self._switch_to_spectrum)

        # Point selection → device state panel
        self.spectrum_tab.device_state_changed.connect(
            self.metadata_panel.show_device_state
        )

        # Cross-tab coordination: heatmap click → spectrum
        self.heatmap_tab.checkpoint_clicked.connect(
            self.spectrum_tab.show_thatec_spectrum
        )
        self.heatmap_tab.checkpoint_clicked.connect(self._switch_to_spectrum)

        # Initial state
        self._set_heatmap_visible(False)
        self.file_browser.refresh()
        if self.file_browser.has_files():
            self._show_result_state(
                "Select a recorded result",
                "Choose an HDF5 file to inspect metadata, the executed Sweep, and spectra.",
                action_text="Open result file...",
            )
        else:
            self._show_result_state(
                "No recorded results yet",
                "Open a THATEC/PyThat HDF5 file or choose another result directory.",
                action_text="Open result file...",
            )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.result_tabs._sync_navigation_mode()
        self.result_tabs._schedule_navigation_sync()
        # The expanded Fluent navigation leaves roughly 1000 px of content at
        # a 1280 px station window.  Switch before action labels and the file
        # browser start imposing a horizontal minimum on the page host.
        compact = event.size().width() < 1100
        if self._compact_layout == compact:
            return
        self._compact_layout = compact
        self.results_splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        self.actions_layout.setDirection(
            QBoxLayout.Direction.TopToBottom
            if compact
            else QBoxLayout.Direction.LeftToRight
        )
        self.results_splitter.setSizes(
            [260, 760] if compact else [280, 900]
        )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # A page inside QFluent's stack can retain the geometry it had while
        # hidden. Re-evaluate nested navigation after the host becomes visible
        # so a stale wide Pivot cannot leak beyond the current viewport.
        self.result_tabs._sync_navigation_mode()
        self.result_tabs._schedule_navigation_sync()

    # ------------------------------------------------------------------
    # Public API (backwards-compatible)
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Refresh the file list and clear result views."""
        self.resume_button.setEnabled(False)
        self.open_sweep_button.setEnabled(False)
        self._thatec_tree_available = False
        self.metadata_panel.clear()
        self.sweep_tree.clear()
        self.spectrum_tab.clear()
        self.heatmap_tab.clear()
        self._set_heatmap_visible(False)
        self.file_browser.refresh()
        if not self.file_browser.has_files():
            self._show_result_state(
                "No recorded results yet",
                "Open a THATEC/PyThat HDF5 file or choose another result directory.",
                action_text="Open result file...",
            )
        else:
            self._show_result_state(
                "Select a recorded result",
                "Choose a file from the browser to inspect its immutable contents.",
            )

    def browse_result_file(self) -> None:
        """Open a file dialog to select an HDF5 result file."""
        self.file_browser.browse_file()

    def open_result_file(self, path: str | Path) -> None:
        """Add and open an arbitrary public THATEC result file in this session."""
        self.file_browser.open_file(Path(path))

    # ------------------------------------------------------------------
    # File selection handler
    # ------------------------------------------------------------------

    def _on_file_selected(self, path_or_none: object) -> None:
        """Handle file selection from the file browser."""
        self.resume_button.setEnabled(False)
        self.open_sweep_button.setEnabled(False)
        self._thatec_tree_available = False
        self.metadata_panel.clear()
        self.sweep_tree.clear()
        self.spectrum_tab.clear()
        self.heatmap_tab.clear()
        self._set_heatmap_visible(False)

        if path_or_none is None:
            self._selected_path = None
            self._thatec_run = None
            self._thatec_tree_available = False
            if self.file_browser.has_files():
                self._show_result_state(
                    "Select a recorded result",
                    "Choose a file from the browser to inspect its immutable contents.",
                )
            else:
                self._show_result_state(
                    "No recorded results yet",
                    "Open a THATEC/PyThat HDF5 file or choose another result directory.",
                    action_text="Open result file...",
                )
            return

        path = Path(str(path_or_none))
        self._selected_path = path
        self._show_result_state(
            "Loading result",
            f"Reading public and station metadata from {path.name}...",
            loading=True,
        )

        # --- Load THATEC tree ---
        try:
            self._thatec_run = ThatecRunReader.describe(path)
        except Exception as exc:
            self._show_result_state(
                "Cannot read result",
                str(exc),
                action_text="Open another file...",
            )
            self._thatec_run = None
            return

        # A public PyThat-compatible measurement can legitimately omit the
        # optional visual tree. That must not hide its spectra or metadata.
        try:
            tree = ThatecRunReader.tree(path)
            self._thatec_tree_available = True
        except Exception:
            tree = ()
            self._thatec_tree_available = False

        # Sweep tree panel
        self.sweep_tree.load(path, self._thatec_run, tree)
        self.open_sweep_button.setEnabled(self._thatec_tree_available)

        # --- Load private HDF5 detail (if available) ---
        try:
            detail = Hdf5RunReader.detail(path)
            points = Hdf5RunReader.points(path)
        except Exception:
            detail = None
            points = ()

        # PyThat enriches metadata when available, but it must not suppress
        # the core HDF5 browser when an optional bridge cannot open a file.
        try:
            pythat_data = read_pythat_run_data(path)
        except Exception:
            pythat_data = None

        # Resume button
        self.resume_button.setEnabled(
            bool(
                detail
                and detail.summary.status
                in {"aborted", "faulted", "incomplete"}
            )
        )

        # Metadata panel
        if detail is not None:
            self.metadata_panel.show_detail(detail)
        else:
            self.metadata_panel.show_thatec_summary(path, self._thatec_run)
        self.metadata_panel.show_pythat(pythat_data)

        # Spectrum tab
        self.spectrum_tab.load(path, self._thatec_run, points)

        # Heatmap tab (only for results with 2-D spectral data)
        if find_heatmap_rows(self._thatec_run):
            self._set_heatmap_visible(True)
            self.heatmap_tab.load(path, self._thatec_run)
        else:
            self._set_heatmap_visible(False)
        self.result_state.hide()

    def _show_result_state(
        self,
        title: str,
        description: str,
        *,
        loading: bool = False,
        action_text: str = "",
    ) -> None:
        self.result_state.show_state(
            title=title,
            description=description,
            accessible_name=title,
            loading=loading,
            action_text=action_text,
        )
        self.result_state.show()

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def _set_heatmap_visible(self, visible: bool) -> None:
        """Show or hide the Heatmaps tab."""
        self.result_tabs.setTabVisible(self._heatmap_index, visible)

    def _switch_to_spectrum(self, *_args: object) -> None:
        """Switch to the Spectrum tab (called after cross-tab navigation)."""
        self.result_tabs.setCurrentIndex(self._spectrum_index)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _request_resume(self) -> None:
        if self._selected_path is not None and self.resume_button.isEnabled():
            self.resume_requested.emit(self._selected_path)

    def _request_open_sweep(self) -> None:
        """Open the public THATEC execution tree in the dedicated Sweep workspace."""
        if (
            self._thatec_run is None
            or self._selected_path is None
            or not self._thatec_tree_available
        ):
            return
        try:
            tree = ThatecRunReader.tree(self._selected_path)
        except Exception as exc:
            self.metadata_panel.metadata.setPlainText(
                f"Cannot reconstruct THATEC Sweep:\n{exc}"
            )
            return
        self.open_sweep_requested.emit(self._thatec_run, tree)

    # ------------------------------------------------------------------
    # Backwards-compatibility aliases (used by existing tests and external
    # code that accessed the old flat attribute API).
    # ------------------------------------------------------------------

    @property
    def runs(self):
        """Alias for ``file_browser.runs`` (backwards compat)."""
        return self.file_browser.runs

    @property
    def metadata(self):
        """Alias for ``metadata_panel.metadata`` (backwards compat)."""
        return self.metadata_panel.metadata

    @property
    def recipe_snapshot(self):
        """Alias for ``metadata_panel.recipe_snapshot`` (backwards compat)."""
        return self.metadata_panel.recipe_snapshot

    @property
    def settings_snapshot(self):
        """Alias for ``metadata_panel.settings_snapshot`` (backwards compat)."""
        return self.metadata_panel.settings_snapshot

    @property
    def pythat_data(self):
        """Alias for ``metadata_panel.pythat_data`` (backwards compat)."""
        return self.metadata_panel.pythat_data

    @property
    def device_state(self):
        """Alias for ``metadata_panel.device_state`` (backwards compat)."""
        return self.metadata_panel.device_state

    @property
    def details_tabs(self):
        """Alias for ``metadata_panel.tabs`` (backwards compat)."""
        return self.metadata_panel.tabs

    @property
    def points(self):
        """Alias for ``spectrum_tab.points`` (backwards compat)."""
        return self.spectrum_tab.points

    @property
    def spectrum_plot(self):
        """Alias for ``spectrum_tab.spectrum_plot`` (backwards compat)."""
        return self.spectrum_tab.spectrum_plot

    @property
    def spectrum_info(self):
        """Alias for ``spectrum_tab.spectrum_info`` (backwards compat)."""
        return self.spectrum_tab.spectrum_info

    @property
    def experiment_tree(self):
        """Alias for ``sweep_tree.tree`` (backwards compat)."""
        return self.sweep_tree.tree

    @property
    def inspector(self):
        """Alias for ``sweep_tree.inspector`` (backwards compat)."""
        return self.sweep_tree.inspector

    def _find_tree_item(self, row_id: str):
        """Alias for ``sweep_tree.find_tree_item`` (backwards compat)."""
        return self.sweep_tree.find_tree_item(row_id)
