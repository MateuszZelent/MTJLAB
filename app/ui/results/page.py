"""Stored-run results page independent of device UI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QResizeEvent, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CardWidget,
    ComboBox,
    FluentIcon,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    StrongBodyLabel,
    TransparentToolButton,
)

from app.storage import (
    Hdf5RunReader,
    ThatecRunReader,
    read_pythat_run_data,
)
from app.storage.filesystem_paths import native_path
from app.storage.heatmap_analysis_store import read_heatmap_analysis
from app.ui.results.batch_dialog import BatchPostprocessDialog
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.file_browser import FileBrowserPanel
from app.ui.results.heatmap_tab import HeatmapResultsTab
from app.ui.results.metadata_panel import MetadataPanel
from app.ui.results.scalar_tab import ScalarResultsTab
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
    derived_heatmap: object | None = None


def _read_result_payload(path: Path) -> _ResultPayload:
    """Read all optional result layers without touching Qt widgets."""

    derived = read_heatmap_analysis(path)
    if derived is not None:
        return _ResultPayload(path, None, (), False, None, (), (), None, derived)
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
        points = Hdf5RunReader.points(path, include_details=False)
    except Exception:
        points = ()
    try:
        references = Hdf5RunReader.references(path, metadata_only=True)
    except Exception:
        references = ()
    # PyThat's inspection performs a full temporary conversion of all samples.
    # Browsing requires only the lazy THATEC/HDF5 readers above.
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
    """Clean Fluent SegmentedWidget navigation backed by a real page stack."""

    current_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
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
        was_blocked = self.compact_navigation.blockSignals(True)
        self.compact_navigation.addItem(label, userData=index)
        self.compact_navigation.blockSignals(was_blocked)
        self.navigation.addItem(
            route,
            label.replace("&", "&&"),
            onClick=lambda _checked=False, index=index: self.setCurrentIndex(index),
            icon=icon,
        )
        if index == 0:
            self.setCurrentIndex(index)
        return index

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < self.stack.count() or self.navigation.widget(self._routes[index]).isHidden():
            return
        self.stack.setCurrentIndex(index)
        self.navigation.setCurrentItem(self._routes[index])
        if self.compact_navigation.currentIndex() != index:
            self.compact_navigation.blockSignals(True)
            self.compact_navigation.setCurrentIndex(index)
            self.compact_navigation.blockSignals(False)
        self.current_changed.emit(index)

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
        QTimer.singleShot(0, self, self._sync_navigation_mode)

    def _sync_navigation_mode(self) -> None:
        self._navigation_sync_pending = False
        items_width = sum(
            self.navigation.widget(r).sizeHint().width()
            for r in self._routes
            if self.navigation.widget(r) is not None and not self.navigation.widget(r).isHidden()
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
                    if not self.navigation.widget(self._routes[candidate]).isHidden()
                ),
                -1,
            )
            if replacement >= 0:
                self.setCurrentIndex(replacement)



class ResultsPage(QWidget):
    """Browse immutable run files without opening an instrument session.

    The page is divided into three sections:

    * **Left** — ``FileBrowserPanel`` listing HDF5 results and sample
      measurement artifacts in the configured catalogue tree.
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

    def __init__(
        self,
        output_dir: str,
        parent: QWidget | None = None,
        *,
        catalogue_tree: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("resultsPage")
        self.setProperty("stationSurface", "page")
        self.owns_viewport = True
        self._selected_path: Path | None = None
        self._thatec_run = None
        self._thatec_tree = ()
        self._thatec_tree_available = False
        self._result_request_id = 0
        self._result_task: ResultReadTask | None = None
        self._pythat_task = None
        self._loaded_signature = None
        self._requested_signature = None
        self._read_pool = QThreadPool(self)
        self._read_pool.setMaxThreadCount(1)
        self._pending_completed_result: Path | None = None
        self._selected_artifact: Path | None = None
        self._browser_visible = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        # --- Unified Single-Row Context Header & Operational Actions ---
        self.action_card = CardWidget(self)
        self.action_card.setObjectName("resultsHeaderCard")
        self.action_card.setFixedHeight(42)
        self.actions_layout = QHBoxLayout(self.action_card)
        actions = self.actions_layout
        actions.setContentsMargins(10, 4, 10, 4)
        actions.setSpacing(8)

        self.toggle_browser_button = TransparentToolButton(FluentIcon.MENU, self.action_card)
        self.toggle_browser_button.setAccessibleName("Toggle run browser sidebar")
        self.toggle_browser_button.setToolTip("Show or hide the result browser sidebar (Ctrl+B)")
        self.toggle_browser_button.clicked.connect(self.toggle_browser_sidebar)
        actions.addWidget(self.toggle_browser_button)

        self.run_title_label = StrongBodyLabel("Results", self.action_card)
        self.run_title_label.setObjectName("resultsRunTitle")
        actions.addWidget(self.run_title_label)

        self.run_status_label = QLabel("Read-only HDF5", self.action_card)
        self.run_status_label.setObjectName("resultsRunStatus")
        self.run_status_label.setProperty("runStatus", "neutral")
        actions.addWidget(self.run_status_label, 1)

        self.command_palette_button = PushButton(FluentIcon.SEARCH, "Commands", self.action_card)
        self.command_palette_button.setToolTip("Open Results Command Palette (Ctrl+K)")
        self.command_palette_button.clicked.connect(self.open_command_palette)
        actions.addWidget(self.command_palette_button)

        self.resume_button = PushButton(FluentIcon.PLAY, "Resume", self.action_card)
        self.resume_button.setEnabled(False)
        self.resume_button.setToolTip(
            "Resume from a confirmed safe checkpoint (interrupted runs only)."
        )
        self.open_sweep_button = PrimaryPushButton(
            FluentIcon.LINK, "Open in Sweeps", self.action_card
        )
        self.open_sweep_button.setEnabled(False)
        self.open_sweep_button.setToolTip(
            "Build the executed THATEC measurement tree in the Sweeps workspace."
        )
        actions.addWidget(self.resume_button)
        actions.addWidget(self.open_sweep_button)
        layout.addWidget(self.action_card)

        # --- Main splitter ---
        self.results_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.results_splitter.setObjectName("resultsMainSplitter")
        self.results_splitter.setMinimumHeight(240)
        splitter = self.results_splitter

        # Left: file browser
        self.file_browser = FileBrowserPanel(
            output_dir, catalogue_tree=catalogue_tree
        )
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

        # Tab: Sweep Tree (shared MeasurementTreeView linked with Sweeps)
        self.sweep_tree = SweepTreePanel()
        self._tree_index = self.result_tabs.addTab(
            self.sweep_tree, "Sweep tree", icon=FluentIcon.LEAF
        )

        self.scalar_tab = ScalarResultsTab()
        self._scalar_index = self.result_tabs.addTab(
            self.scalar_tab, "I/V/P & Scalars", icon=FluentIcon.ALIGNMENT
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
        self._batch_dialog = None
        self.heatmap_tab.batch_requested.connect(self._open_batch_postprocessing)
        self.heatmap_tab.powerpoint_requested.connect(
            lambda identity, items, state, preview: self._open_batch_postprocessing(
                identity, items, state, preview, power_point=True
            )
        )
        self.spectrum_tab.batch_requested.connect(self._open_batch_postprocessing)
        self.sweep_tree.open_in_sweeps_requested.connect(
            lambda _semantic_id: self._request_open_sweep()
        )

        result_card = CardWidget(self)
        result_card.setObjectName("resultsWorkspaceHost")
        result_card.setMinimumHeight(240)
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(10, 6, 10, 6)
        result_layout.setSpacing(6)
        self.result_state = ResultsStateCard(result_card)
        self.result_state.set_compact(True)
        result_layout.addWidget(self.result_state)
        result_layout.addWidget(self.result_tabs, 1)
        splitter.addWidget(result_card)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        self._compact_layout: bool | None = None
        self.setMinimumHeight(320)

        # Keyboard shortcuts scoped to ResultsPage
        self._shortcut_cmd = QShortcut(QKeySequence("Ctrl+K"), self)
        self._shortcut_cmd.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_cmd.activated.connect(self.open_command_palette)
        self._shortcut_browser = QShortcut(QKeySequence("Ctrl+B"), self)
        self._shortcut_browser.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_browser.activated.connect(self.toggle_browser_sidebar)

        # --- Connections ---
        self.file_browser.file_selected.connect(self._on_file_selected)
        self.file_browser.files_loaded.connect(self._on_file_list_loaded)
        self.metadata_panel.pythat_requested.connect(self._inspect_pythat)
        self.resume_button.clicked.connect(self._request_resume)
        self.open_sweep_button.clicked.connect(self._request_open_sweep)
        self.result_state.action_requested.connect(self._run_result_state_action)

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

        self._syncing_processing = False
        self._reference_view_active = False
        self.spectrum_tab.processing_controls.changed.connect(
            self._sync_processing
        )
        self.heatmap_tab.processing_controls.changed.connect(
            self._sync_processing
        )
        self.spectrum_tab.view_controls.changed.connect(self._sync_reference_view)

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
        actions_compact = event.size().width() < 640
        self.action_card.setFixedHeight(74 if actions_compact else 42)
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
        browser_width = min(350, max(260, int(event.size().width() * 0.26)))
        results_width = max(500, event.size().width() - browser_width)
        self.results_splitter.setSizes(
            [160, 760] if compact else [browser_width, results_width]
        )

    def toggle_browser_sidebar(self) -> None:
        """Show or hide the left result browser drawer to maximize plot canvas area."""
        self._browser_visible = not self._browser_visible
        self.file_browser.setVisible(self._browser_visible)

    def open_command_palette(self) -> None:
        """Open the keyboard-driven Results Command Palette (Ctrl+K)."""
        from app.ui.results.command_palette import ResultsCommandPalette

        dialog = ResultsCommandPalette(self)
        dialog.exec()
        dialog.deleteLater()

    def _update_header_context(
        self, title: str, status_text: str, status_key: str = "neutral"
    ) -> None:
        self.run_title_label.setText(title)
        self.run_status_label.setText(status_text)
        self.run_status_label.setProperty("runStatus", status_key)
        style = self.run_status_label.style()
        if style is not None:
            style.unpolish(self.run_status_label)
            style.polish(self.run_status_label)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # A page inside QFluent's stack can retain the geometry it had while
        # hidden. Re-evaluate nested navigation after the host becomes visible
        # so a stale wide Pivot cannot leak beyond the current viewport.
        self.result_tabs._sync_navigation_mode()
        self.result_tabs._schedule_navigation_sync()
        self._load_pending_completed_result()

    # ------------------------------------------------------------------
    # Public API (backwards-compatible)
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Refresh the catalogue; preserve unchanged result analysis and selection."""
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

    def offer_completed_result(self, path: Path) -> None:
        """Defer potentially large result imports until this page is visible."""
        self._pending_completed_result = Path(path)
        if self.isVisible():
            self._load_pending_completed_result()

    def _load_pending_completed_result(self) -> None:
        path = self._pending_completed_result
        if path is None or not self.isVisible():
            return
        self._pending_completed_result = None
        self.refresh()
        self.select_result_path(path)

    def set_output_directory(self, output_dir: str | Path) -> None:
        """Point Results at a result folder or the global Samples catalogue."""

        self.file_browser.set_output_directory(output_dir)

    def browse_result_file(self) -> None:
        """Open a file dialog to select an HDF5 result file."""
        self.file_browser.browse_file()

    def open_result_file(self, path: str | Path) -> None:
        """Add and open an arbitrary public THATEC result file in this session."""
        self.file_browser.open_file(Path(path))

    def select_result_path(self, path: str | Path) -> bool:
        """Focus a result from a run callback while preserving catalogue context."""

        target = Path(path).expanduser().resolve()
        if not native_path(target).is_file() or target.suffix.lower() not in {
            ".h5",
            ".hdf5",
            ".csv",
            ".pdf",
            ".json", ".jsonl", ".yml",
        }:
            return False
        try:
            target.relative_to(self.file_browser.output_dir.resolve())
        except ValueError:
            self.set_output_directory(target.parent)
        return self.file_browser.select_path(target)

    # ------------------------------------------------------------------
    # File selection handler
    # ------------------------------------------------------------------

    def _on_file_selected(self, path_or_none: object) -> None:
        """Handle file selection from the file browser."""
        if path_or_none is not None:
            path = Path(str(path_or_none)).expanduser().resolve()
            signature = self._file_signature(path)
            if path == self._selected_path and (
                self._result_task is not None or (
                    self._loaded_signature is not None and signature == self._loaded_signature)
            ):
                return
        self._loaded_signature = None
        self._cancel_result_load()
        self.resume_button.setEnabled(False)
        self.open_sweep_button.setEnabled(False)
        self._thatec_tree_available = False
        self._thatec_tree = ()
        self.metadata_panel.clear()
        self.sweep_tree.clear()
        self.spectrum_tab.clear()
        self.heatmap_tab.clear()
        self.scalar_tab.clear()
        self._set_heatmap_visible(False)

        if path_or_none is None:
            self._selected_path = None
            self._thatec_run = None
            self._selected_artifact = None
            self._thatec_tree_available = False
            self._update_header_context("Results", "Read-only HDF5", "neutral")
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

        path = Path(str(path_or_none)).expanduser().resolve()
        self._selected_path = path
        self._requested_signature = self._file_signature(path)
        self.result_selected.emit(path)
        if path.suffix.lower() in {".csv", ".pdf", ".json", ".jsonl", ".yml"}:
            self._selected_artifact = path
            kind = (
                "CSV characterization data"
                if path.suffix.lower() == ".csv"
                else "PDF measurement report" if path.suffix.lower() == ".pdf"
                else "Sweep metadata and provenance"
            )
            self._show_result_state(
                kind,
                (
                    f"This file is stored in the sample catalogue:\n{path}\n\n"
                    "Open file to inspect its contents."
                ),
                action_text="Open file",
            )
            return
        self._selected_artifact = None
        self._show_result_state(
            "Loading result",
            f"Reading public and station metadata from {path.name}...",
            loading=True,
        )
        request_id = self._begin_result_request()
        # File size cannot bound metadata/bridge latency, particularly on
        # network storage. Every archive read uses the same worker boundary.
        task = ResultReadTask(request_id, _read_result_payload, path)
        self._result_task = task
        task.signals.loaded.connect(self._on_result_loaded)
        task.signals.failed.connect(self._on_result_failed)
        self._read_pool.start(task)

    def _begin_result_request(self) -> int:
        self._result_request_id += 1
        return self._result_request_id

    def _cancel_result_load(self) -> None:
        self._result_request_id += 1
        if self._result_task is not None:
            self._result_task.cancel()
            self._result_task = None
        if self._pythat_task is not None:
            self._pythat_task.cancel()
            self._pythat_task = None
        self._read_pool.clear()

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
        self._selected_artifact = None

    def _apply_result_payload(self, request_id: int, payload: _ResultPayload) -> None:
        if request_id != self._result_request_id:
            return
        if payload.derived_heatmap is not None:
            self._thatec_run = None
            self._loaded_signature = self._requested_signature
            self._update_header_context(payload.path.name, "● Derived Heatmap", "neutral")
            self.metadata_panel.show_heatmap_analysis(payload.path, payload.derived_heatmap.metadata)
            self.metadata_panel.show_pythat(None)
            self.metadata_panel.pythat_button.setEnabled(False)
            self.metadata_panel.pythat_data.setPlainText("Derived heatmap snapshot · pylab.heatmap-analysis.v1\nOpen the source measurement for THATEC/PyThat inspection.")
            self._set_heatmap_visible(True)
            self.heatmap_tab.load_analysis(payload.path, payload.derived_heatmap)
            self.result_tabs.setCurrentIndex(self._heatmap_index)
            self.result_state.hide()
            return
        self._thatec_run = payload.thatec_run
        self._loaded_signature = self._requested_signature
        self._thatec_tree = payload.tree
        self._thatec_tree_available = payload.tree_available
        self.open_sweep_button.setEnabled(payload.tree_available)
        display_name = (
            payload.path.parent.name
            if payload.path.name == "data.h5"
            else payload.path.name
        )
        if payload.detail is not None:
            raw_status = payload.detail.summary.status
            status_key = raw_status if raw_status in {"completed", "aborted", "faulted", "incomplete"} else "neutral"
            self._update_header_context(display_name, f"● {raw_status.capitalize()}", status_key)
        else:
            self._update_header_context(display_name, "● THATEC Public", "neutral")

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
        self.scalar_tab.load(payload.path, payload.thatec_run, payload.points)
        self._reference_view_active = False
        self.spectrum_tab.load(payload.path, payload.thatec_run, payload.points, references=payload.references)
        if find_heatmap_rows(payload.thatec_run):
            self._set_heatmap_visible(True)
            self.heatmap_tab.load(payload.path, payload.thatec_run, payload.points, references=payload.references)
        else:
            self._set_heatmap_visible(False)
        self.result_state.hide()

    @staticmethod
    def _file_signature(path):
        try:
            info = native_path(path).stat()
            return (info.st_size, info.st_mtime_ns)
        except OSError:
            return None

    def _inspect_pythat(self):
        if self._selected_path is None or self._pythat_task is not None:
            return
        self.metadata_panel.pythat_button.setEnabled(False)
        self.metadata_panel.pythat_data.setPlainText("# Inspecting compatibility with PyThat…")
        task = ResultReadTask(self._result_request_id, read_pythat_run_data, self._selected_path)
        self._pythat_task = task
        task.signals.loaded.connect(self._pythat_loaded)
        task.signals.failed.connect(self._pythat_failed)
        self._read_pool.start(task)

    def _pythat_loaded(self, request_id, data):
        if request_id == self._result_request_id:
            self._pythat_task = None
            self.metadata_panel.show_pythat(data)

    def _pythat_failed(self, request_id, message):
        if request_id == self._result_request_id:
            self._pythat_task = None
            self.metadata_panel.pythat_button.setEnabled(True)
            self.metadata_panel.pythat_data.setPlainText(f"# PyThat inspection failed\n{message}")

    def _sync_processing(self, state) -> None:
        """Share the effective correction, including active Spectrum Compare."""
        if self._syncing_processing:
            return
        self._syncing_processing = True
        try:
            view = self.spectrum_tab.view_controls
            view_changed = view.set_reference_processing(state)
            self._reference_view_active = view.state.show_reference
            for tab in (self.spectrum_tab, self.heatmap_tab):
                changed = tab.processing_controls.state != state
                if changed:
                    tab.processing_controls.set_state(state)
                if changed or tab is self.spectrum_tab and view_changed:
                    tab._processing_changed(state)
        finally:
            self._syncing_processing = False

    def _sync_reference_view(self, view) -> None:
        if self._syncing_processing:
            return
        processing = self.spectrum_tab.processing_controls.state
        if view.show_reference:
            self._sync_processing(view.reference_processing(processing))
        elif self._reference_view_active:
            self._sync_processing(replace(processing, operation="none",
                                          reference_index=None, reference_sweep=None))
        self._reference_view_active = self.spectrum_tab.view_controls.state.show_reference

    def _open_batch_postprocessing(self, identity, items, state, preview, *, power_point=False):
        if self._batch_dialog is not None and self._batch_dialog.isVisible():
            self._batch_dialog.raise_()
            return
        if self._batch_dialog is not None:
            self._batch_dialog.deleteLater()
        self._batch_dialog = BatchPostprocessDialog(identity, items, state, preview, self, power_point=power_point)
        self._batch_dialog.open()

    def closeEvent(self, event) -> None:
        if not self.shutdown():
            event.ignore()
            return
        super().closeEvent(event)

    def shutdown(self, timeout_ms: int = 5_000) -> bool:
        """Keep native readers alive until their pools have stopped."""
        import time

        self._pending_completed_result = None
        self._cancel_result_load()
        self._read_pool.clear()
        self.file_browser._cancel_refresh()
        self.file_browser._read_pool.clear()
        self.spectrum_tab._invalidate_pending_reads()
        self.spectrum_tab._cancel_filter_read()
        self.spectrum_tab.peak_tools.close()
        self.spectrum_tab.spectrum_plot.close_windows()
        self.sweep_tree.cancel_detail_read()
        self.heatmap_tab._invalidate_pending_read()
        self.scalar_tab.cancel_read()
        self.scalar_tab._read_pool.clear()
        deadline = time.monotonic() + timeout_ms / 1000
        if self._batch_dialog is not None:
            self._batch_dialog.request_stop()
        for pool in (self._read_pool, self.file_browser._read_pool,
                     self.spectrum_tab._read_pool, self.spectrum_tab._filter_pool,
                     self.spectrum_tab.peak_tools.pool,
                     self.sweep_tree._detail_pool,
                     self.heatmap_tab._read_pool, self.heatmap_tab.heatmap._export_pool,
                     self.scalar_tab._read_pool, self._batch_dialog.pool if self._batch_dialog else None):
            if pool is None:
                continue
            remaining = max(0, int((deadline-time.monotonic()) * 1000))
            if not pool.waitForDone(remaining):
                return False
        return True

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

    def _run_result_state_action(self) -> None:
        if self._selected_artifact is not None and self._selected_artifact.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._selected_artifact)))
            return
        self.browse_result_file()

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
        # Use the same immutable reader result shown by this page. Reopening
        # HDF5 here both blocks Qt and can mix a newer tree with an older run.
        self.open_sweep_requested.emit(self._thatec_run, self._thatec_tree)

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
