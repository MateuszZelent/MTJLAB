"""Stored-run results page independent of device UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, QThreadPool, QTimer, Qt, Signal
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
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    SubtitleLabel,
)

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
from app.ui.results.workers import ResultReadTask


@dataclass(frozen=True, slots=True)
class _ResultPayload:
    """Immutable reader output passed from a worker to the GUI thread."""

    path: Path
    thatec_run: object
    tree: tuple[object, ...]
    tree_available: bool
    detail: object | None
    points: tuple[object, ...]
    references: tuple[object, ...]
    pythat_data: object | None


def _read_result_payload(path: Path) -> _ResultPayload:
    """Read all optional result layers without touching Qt widgets."""

    thatec_run = ThatecRunReader.describe(path)
    try:
        tree = ThatecRunReader.tree(path)
        tree_available = True
    except Exception:
        tree = ()
        tree_available = False

    try:
        detail = Hdf5RunReader.detail(path)
    except Exception:
        detail = None
    try:
        points = Hdf5RunReader.points(path)
    except Exception:
        points = ()
    try:
        references = Hdf5RunReader.references(path)
    except Exception:
        references = ()
    try:
        pythat_data = read_pythat_run_data(path)
    except Exception:
        pythat_data = None
    return _ResultPayload(
        path=path,
        thatec_run=thatec_run,
        tree=tuple(tree),
        tree_available=tree_available,
        detail=detail,
        points=tuple(points),
        references=tuple(references),
        pythat_data=pythat_data,
    )


def _make_payload(
    path: Path,
    thatec_run: object,
    tree: tuple[object, ...],
    tree_available: bool,
    detail: object | None,
    points: tuple[object, ...],
    references: tuple[object, ...],
    pythat_data: object | None,
) -> _ResultPayload:
    return _ResultPayload(
        path=path,
        thatec_run=thatec_run,
        tree=tuple(tree),
        tree_available=tree_available,
        detail=detail,
        points=tuple(points),
        references=tuple(references),
        pythat_data=pythat_data,
    )


class _FluentResultSections(QWidget):
    """Fluent Segmented navigation backed by a real page stack.

    It deliberately provides the operator-facing Results navigation. The
    small semantic page-management API stays local to this Fluent widget.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.navigation = SegmentedWidget(self)
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

    def addTab(self, page: QWidget, label: str, icon: FluentIcon | None = None) -> int:
        index = self.stack.addWidget(page)
        route = f"result-section-{index}"
        self._routes.append(route)
        self._labels.append(label)
        self.compact_navigation.addItem(label, userData=index)
        self.navigation.addItem(
            route,
            label,
            onClick=lambda _checked=False, index=index: self.setCurrentIndex(index),
            icon=icon,
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

    def minimumSizeHint(self) -> QSize:
        cur = self.stack.currentWidget()
        nav_hint = self.navigation.minimumSizeHint()
        if cur is not None:
            c_hint = cur.minimumSizeHint()
            return QSize(max(c_hint.width(), nav_hint.width()), c_hint.height() + nav_hint.height() + 8)
        return super().minimumSizeHint()

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
        items_width = sum(
            self.navigation.widget(r).sizeHint().width()
            for r in self._routes
            if self.navigation.widget(r) is not None
        )
        required_width = max(420, items_width + 16)
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
    result_selected = Signal(object)
    _ASYNC_LOAD_BYTES = 4 * 1024 * 1024

    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.owns_viewport = True
        self._selected_path: Path | None = None
        self._thatec_run = None
        self._thatec_tree_available = False
        self._result_request_id = 0
        self._result_task: ResultReadTask | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(6)

        # --- Title ---
        title = SubtitleLabel("Results", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = CaptionLabel(
            "Open HDF5 results, inspect the complete measurement tree, filter checkpoint "
            "parameter sets, and browse raw or processed spectra.",
            self,
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # --- Deliberate operational actions ---
        self.action_card = CardWidget(self)
        self.action_card.setFixedHeight(38)
        self.actions_layout = QHBoxLayout(self.action_card)
        actions = self.actions_layout
        actions.setContentsMargins(12, 4, 12, 4)
        actions.setSpacing(8)
        action_note = CaptionLabel(
            "Actions are enabled only when the selected result proves safe.",
            self.action_card,
        )
        action_note.setObjectName("muted")
        action_note.setWordWrap(True)
        actions.addWidget(action_note, 1)
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
        layout.addWidget(self.action_card)

        # --- Main splitter ---
        self.results_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.results_splitter.setMinimumHeight(240)
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
            self.metadata_panel, "Overview", icon=FluentIcon.INFO
        )

        # Tab: Sweep Tree
        self.sweep_tree = SweepTreePanel()
        self._tree_index = self.result_tabs.addTab(
            self.sweep_tree, "Sweep tree", icon=FluentIcon.LEAF
        )

        # Tab: Spectrum
        self.spectrum_tab = SpectrumResultsTab()
        self._spectrum_index = self.result_tabs.addTab(
            self.spectrum_tab, "Spectrum", icon=FluentIcon.VIEW
        )

        # Tab: Heatmaps
        self.heatmap_tab = HeatmapResultsTab()
        self._heatmap_index = self.result_tabs.addTab(
            self.heatmap_tab, "Heatmaps", icon=FluentIcon.TILES
        )

        result_card = CardWidget(self)
        result_card.setMinimumHeight(240)
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(12, 8, 12, 8)
        result_layout.setSpacing(6)
        self.result_state = ResultsStateCard(result_card)
        self.result_state.set_compact(True)
        result_layout.addWidget(self.result_state)
        result_layout.addWidget(self.result_tabs)
        splitter.addWidget(result_card)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        self._compact_layout: bool | None = None
        self.setMinimumHeight(320)

        # --- Connections ---
        self.file_browser.file_selected.connect(self._on_file_selected)
        self.file_browser.files_loaded.connect(self._on_file_list_loaded)
        self.resume_button.clicked.connect(self._request_resume)
        self.open_sweep_button.clicked.connect(self._request_open_sweep)
        self.result_state.action_requested.connect(self.browse_result_file)

        # Cross-tab coordination: sweep tree → spectrum
        self.sweep_tree.spectrum_requested.connect(
            self.spectrum_tab.show_thatec_spectrum
        )
        self.sweep_tree.spectrum_requested.connect(self._switch_to_spectrum)
        self.sweep_tree.stored_spectrum_requested.connect(
            self.spectrum_tab.show_stored_spectrum
        )
        self.sweep_tree.stored_spectrum_requested.connect(self._switch_to_spectrum)
        self.sweep_tree.reference_spectrum_requested.connect(
            self.spectrum_tab.show_reference
        )
        self.sweep_tree.reference_spectrum_requested.connect(self._switch_to_spectrum)

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

    def sizeHint(self) -> QSize:
        return QSize(800, 500)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.result_tabs._sync_navigation_mode()
        self.result_tabs._schedule_navigation_sync()
        # The expanded Fluent navigation leaves roughly 1000 px of content at
        # a 1280 px station window.  Switch before action labels and the file
        # browser start imposing a horizontal minimum on the page host.
        compact = event.size().width() < 850
        actions_compact = event.size().width() < 600
        self.action_card.setFixedHeight(68 if actions_compact else 38)
        self.actions_layout.setDirection(
            QBoxLayout.Direction.TopToBottom
            if actions_compact
            else QBoxLayout.Direction.LeftToRight
        )
        if self._compact_layout == compact:
            return
        self._compact_layout = compact
        self.results_splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        browser_width = min(380, max(260, int(event.size().width() * 0.32)))
        results_width = max(500, event.size().width() - browser_width)
        self.results_splitter.setSizes(
            [160, 760] if compact else [browser_width, results_width]
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
        self._cancel_result_load()
        self.resume_button.setEnabled(False)
        self.open_sweep_button.setEnabled(False)
        self._thatec_tree_available = False
        self.metadata_panel.clear()
        self.sweep_tree.clear()
        self.spectrum_tab.clear()
        self.heatmap_tab.clear()
        self._set_heatmap_visible(False)
        self.file_browser.refresh()
        if self._selected_path is not None or self._result_task is not None:
            return
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

    def set_output_directory(self, output_dir: str | Path) -> None:
        """Point Results at the directory used by the latest run."""

        self.file_browser.set_output_directory(output_dir)

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
        self._cancel_result_load()
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
            self.result_selected.emit(None)
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
        self.result_selected.emit(path)
        self._show_result_state(
            "Loading result",
            f"Reading public and station metadata from {path.name}...",
            loading=True,
        )
        request_id = self._begin_result_request()
        if self._should_load_async(path):
            task = ResultReadTask(request_id, _read_result_payload, path)
            self._result_task = task
            task.signals.loaded.connect(self._on_result_loaded)
            task.signals.failed.connect(self._on_result_failed)
            QThreadPool.globalInstance().start(task)
            return
        try:
            payload = _read_result_payload(path)
        except Exception as exc:
            self._on_result_failed(request_id, str(exc))
            return
        self._apply_result_payload(request_id, payload)

    def _should_load_async(self, path: Path) -> bool:
        """Keep large HDF5 reads and optional bridges off the GUI thread."""

        try:
            return path.stat().st_size >= self._ASYNC_LOAD_BYTES
        except OSError:
            return False

    def _begin_result_request(self) -> int:
        self._result_request_id += 1
        return self._result_request_id

    def _cancel_result_load(self) -> None:
        self._result_request_id += 1
        if self._result_task is not None:
            self._result_task.cancel()
            self._result_task = None

    def _on_result_loaded(self, request_id: int, payload: object) -> None:
        if request_id != self._result_request_id or not isinstance(payload, _ResultPayload):
            return
        self._result_task = None
        self._apply_result_payload(request_id, payload)

    def _on_result_failed(self, request_id: int, message: str) -> None:
        if request_id != self._result_request_id:
            return
        self._result_task = None
        self._show_result_state(
            "Cannot read result",
            message,
            action_text="Open another file...",
        )
        self._thatec_run = None

    def _apply_result_payload(self, request_id: int, payload: _ResultPayload) -> None:
        if request_id != self._result_request_id:
            return
        self._thatec_run = payload.thatec_run
        self._thatec_tree_available = payload.tree_available
        self.open_sweep_button.setEnabled(payload.tree_available)

        # The tree is intentionally enriched with private checkpoint and
        # reference layers when present. Public-only THATEC files still get
        # their complete /measurement tree from ``run.rows``.
        self.sweep_tree.load(
            payload.path,
            payload.thatec_run,
            payload.tree,
            points=payload.points,
            references=payload.references,
            detail=payload.detail,
        )
        self.resume_button.setEnabled(
            bool(
                payload.detail
                and payload.detail.summary.status
                in {"aborted", "faulted", "incomplete"}
            )
        )
        if payload.detail is not None:
            self.metadata_panel.show_detail(payload.detail)
        else:
            self.metadata_panel.show_thatec_summary(payload.path, payload.thatec_run)
        self.metadata_panel.show_pythat(payload.pythat_data)
        self.spectrum_tab.load(payload.path, payload.thatec_run, payload.points)
        if find_heatmap_rows(payload.thatec_run):
            self._set_heatmap_visible(True)
            self.heatmap_tab.load(payload.path, payload.thatec_run, payload.points)
        else:
            self._set_heatmap_visible(False)
        self.result_state.hide()

    def closeEvent(self, event) -> None:
        self._cancel_result_load()
        super().closeEvent(event)

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

    def _on_file_list_loaded(self, has_files: bool) -> None:
        """Refresh the page-level empty state after a background index completes."""

        if self._selected_path is not None or self._result_task is not None:
            return
        if has_files:
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
