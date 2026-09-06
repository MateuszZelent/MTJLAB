"""Fluent-native sample inventory and device matrix management page."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SearchLineEdit,
    SegmentedWidget,
    SimpleCardWidget,
    SubtitleLabel,
    TitleLabel,
    ToolButton,
)

from app.inventory.models import (
    ActiveSampleTarget,
    Sample,
    SampleAttachment,
    SampleRunRecord,
)
from app.inventory.store import InventoryStore
from app.ui.inventory.attachment_card import AttachmentCard
from app.ui.inventory.attachment_viewer import open_attachment
from app.ui.inventory.matrix_widget import SampleMatrixWidget
from app.ui.inventory.measurement_browser_view import MeasurementBrowserView
from app.ui.inventory.programming_dialog import RenumberRowsDialog, SampleProgrammingDialog


class RenameHeaderDialog(QDialog):
    """Modal prompt to rename an individual row or column label."""

    def __init__(
        self,
        header_type: str,
        key: str,
        current_label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Rename {header_type} {key}")
        self.setMinimumWidth(380)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        layout.addWidget(SubtitleLabel(f"Rename {header_type} {key}", self))
        layout.addWidget(
            CaptionLabel(
                f"Enter the new display label / dimension for {header_type.lower()} {key}:",
                self,
            )
        )

        self.label_input = LineEdit(self)
        self.label_input.setText(current_label)
        self.label_input.setPlaceholderText(
            "e.g. 200 nm" if header_type == "Column" else "e.g. Strip Alpha"
        )
        layout.addWidget(self.label_input)

        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        self.cancel_btn = PushButton("Cancel", self)
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = PrimaryPushButton("Save Label", self, FluentIcon.SAVE)
        self.save_btn.clicked.connect(self.accept)
        btn_box.addWidget(self.cancel_btn)
        btn_box.addWidget(self.save_btn)
        layout.addLayout(btn_box)

    def get_label(self) -> str:
        return self.label_input.text().strip()


class AddHeaderDialog(QDialog):
    """Modal prompt to insert a new row or column."""

    def __init__(
        self,
        header_type: str,
        default_key: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Add New {header_type}")
        self.setMinimumWidth(380)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        layout.addWidget(SubtitleLabel(f"Add New {header_type}", self))

        form = QFormLayout()
        form.setSpacing(8)

        self.key_input = LineEdit(self)
        self.key_input.setText(default_key)
        form.addRow(f"{header_type} Key (ID) *:", self.key_input)

        self.label_input = LineEdit(self)
        self.label_input.setPlaceholderText(
            "e.g. 350 nm" if header_type == "Column" else "e.g. Row 11"
        )
        form.addRow(f"{header_type} Label:", self.label_input)
        layout.addLayout(form)

        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        self.cancel_btn = PushButton("Cancel", self)
        self.cancel_btn.clicked.connect(self.reject)
        self.add_btn = PrimaryPushButton("Add", self, FluentIcon.ADD)
        self.add_btn.clicked.connect(self.accept)
        btn_box.addWidget(self.cancel_btn)
        btn_box.addWidget(self.add_btn)
        layout.addLayout(btn_box)

    def get_data(self) -> tuple[str, str]:
        key = self.key_input.text().strip()
        label = self.label_input.text().strip() or key
        return key, label


class SampleInventoryPage(QWidget):
    """Central laboratory sample catalogue, device grid matrix, and measurement ledger."""

    active_target_changed = Signal(object)  # ActiveSampleTarget
    open_result_requested = Signal(str)     # run_path (.h5)
    status = Signal(str)
    samples_updated = Signal()

    def __init__(
        self,
        store: InventoryStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.owns_viewport = True
        self.store = store
        self._current_sample: Sample | None = None
        self._selected_cell: tuple[str, str] | None = None

        self._build_ui()
        self.refresh_samples()

    def minimumSizeHint(self) -> QSize:
        """Allow the inventory page to fit any viewport size without forced horizontal overflow."""
        return QSize(0, 0)

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)

        # ---------------------------------------------------------------------
        # Top Header Bar: Title, Active Target Pill, Global Actions
        # ---------------------------------------------------------------------
        header_card = SimpleCardWidget(self)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(12)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(TitleLabel("Sample & Coordinate Inventory", header_card))
        title_box.addWidget(
            CaptionLabel(
                "Organize device matrices, inspect MTJ pillar grids, set measurement targets and link eLab records.",
                header_card,
            )
        )
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)

        # Active target pill / banner
        self.active_target_card = SimpleCardWidget(header_card)
        self.active_target_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.active_target_card.setStyleSheet(
            "SimpleCardWidget { border: 1.5px solid palette(highlight); border-radius: 6px; padding: 4px; }"
        )
        pill_layout = QHBoxLayout(self.active_target_card)
        pill_layout.setContentsMargins(10, 6, 10, 6)
        pill_layout.setSpacing(8)

        pill_icon = IconWidget(FluentIcon.TAG, self.active_target_card)
        pill_icon.setFixedSize(20, 20)
        pill_layout.addWidget(pill_icon)

        self.active_target_label = BodyLabel("No active target", self.active_target_card)
        self.active_target_label.setStyleSheet("font-weight: 600;")
        self.active_target_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        pill_layout.addWidget(self.active_target_label)

        self.next_device_btn = ToolButton(FluentIcon.RIGHT_ARROW, self.active_target_card)
        self.next_device_btn.setToolTip("Advance to next device (next column/row)")
        self.next_device_btn.clicked.connect(self._advance_to_next_device)
        pill_layout.addWidget(self.next_device_btn)

        self.clear_target_btn = ToolButton(FluentIcon.CANCEL, self.active_target_card)
        self.clear_target_btn.setToolTip("Clear active target")
        self.clear_target_btn.clicked.connect(self._clear_active_target)
        pill_layout.addWidget(self.clear_target_btn)

        header_layout.addWidget(self.active_target_card)

        # New Sample button
        self.new_sample_btn = PrimaryPushButton("+ New Sample", header_card, FluentIcon.ADD)
        self.new_sample_btn.clicked.connect(self._create_new_sample)
        header_layout.addWidget(self.new_sample_btn)

        main_layout.addWidget(header_card)

        # ---------------------------------------------------------------------
        # Splitter: Left Catalog (Master) vs Right Workspace (Detail)
        # ---------------------------------------------------------------------
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)

        # === Left Panel: Sample Catalog ===
        left_panel = SimpleCardWidget(self.splitter)
        left_panel.setMinimumWidth(180)
        left_panel.setMaximumWidth(380)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)

        left_layout.addWidget(SubtitleLabel("Samples", left_panel))

        self.search_input = SearchLineEdit(left_panel)
        self.search_input.setPlaceholderText("Search samples...")
        self.search_input.textChanged.connect(self._filter_samples)
        left_layout.addWidget(self.search_input)

        self.sample_list = QListWidget(left_panel)
        self.sample_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sample_list.setWordWrap(True)
        self.sample_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.sample_list.setStyleSheet(
            "QListWidget {"
            "border: 1px solid palette(mid);"
            "border-radius: 6px;"
            "padding: 4px;"
            "}"
            "QListWidget::item {"
            "padding: 8px 10px;"
            "border-radius: 4px;"
            "margin-bottom: 2px;"
            "}"
            "QListWidget::item:selected {"
            "background-color: rgba(30, 102, 245, 0.25);"
            "}"
        )
        self.sample_list.currentItemChanged.connect(self._on_sample_selection_changed)
        left_layout.addWidget(self.sample_list, 1)

        left_actions = QHBoxLayout()
        left_actions.setSpacing(6)
        self.edit_grid_btn = PushButton("Edit", left_panel, FluentIcon.EDIT)
        self.edit_grid_btn.setToolTip("Edit metadata, dimensions, and rename rows/columns")
        self.edit_grid_btn.clicked.connect(self._edit_current_sample)
        self.delete_sample_btn = PushButton("Delete", left_panel, FluentIcon.DELETE)
        self.delete_sample_btn.clicked.connect(self._delete_current_sample)

        left_actions.addWidget(self.edit_grid_btn)
        left_actions.addWidget(self.delete_sample_btn)
        left_layout.addLayout(left_actions)

        self.splitter.addWidget(left_panel)

        # === Right Panel: Sample Detail & Tabs ===
        right_panel = QWidget(self.splitter)
        right_panel.setMinimumWidth(260)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        # Header Info Card - 2 Row Responsive Layout
        self.sample_header_card = SimpleCardWidget(right_panel)
        self.sample_header_card.setMinimumWidth(260)
        header_info_layout = QVBoxLayout(self.sample_header_card)
        header_info_layout.setContentsMargins(14, 10, 14, 10)
        header_info_layout.setSpacing(6)

        # Row 1: Sample Title & Actions
        header_row1 = QHBoxLayout()
        header_row1.setSpacing(10)
        sample_name_box = QVBoxLayout()
        sample_name_box.setSpacing(2)
        self.current_sample_title = SubtitleLabel("Select a sample", self.sample_header_card)
        self.current_sample_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.current_sample_desc = CaptionLabel("No sample selected", self.sample_header_card)
        self.current_sample_desc.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sample_name_box.addWidget(self.current_sample_title)
        sample_name_box.addWidget(self.current_sample_desc)
        header_row1.addLayout(sample_name_box, 1)

        self.edit_structure_header_btn = PushButton(
            "Edit Structure...", self.sample_header_card, FluentIcon.EDIT
        )
        self.edit_structure_header_btn.setToolTip(
            "Edit matrix dimensions, rename rows and columns, change structure"
        )
        self.edit_structure_header_btn.clicked.connect(self._edit_current_sample)
        header_row1.addWidget(self.edit_structure_header_btn)

        self.renumber_rows_header_btn = PushButton(
            "Renumber Rows...", self.sample_header_card, FluentIcon.SYNC
        )
        self.renumber_rows_header_btn.setToolTip(
            "Quickly shift or renumber rows (e.g. from 1..10 to 20..30) while preserving measurements"
        )
        self.renumber_rows_header_btn.clicked.connect(self._on_renumber_rows_requested)
        header_row1.addWidget(self.renumber_rows_header_btn)

        self.view_measurements_header_btn = PushButton(
            "Measurements Tree", self.sample_header_card, FluentIcon.VIEW
        )
        self.view_measurements_header_btn.setToolTip(
            "Switch to hierarchical measurement tree, inspection plot, and figures of merit"
        )
        self.view_measurements_header_btn.clicked.connect(self._switch_to_measurements_tab)
        header_row1.addWidget(self.view_measurements_header_btn)

        header_info_layout.addLayout(header_row1)

        # Row 2: Stats badges
        header_row2 = QHBoxLayout()
        header_row2.setSpacing(8)
        self.stats_devices_label = CaptionLabel("Devices: -", self.sample_header_card)
        self.stats_tested_label = CaptionLabel("Tested: -", self.sample_header_card)
        self.stats_completed_label = CaptionLabel("Completed: -", self.sample_header_card)
        self.stats_completed_label.setStyleSheet(
            "background: rgba(34, 197, 94, 0.18); color: #15803d; padding: 3px 8px; border-radius: 4px; font-weight: 600;"
        )
        self.stats_burned_label = CaptionLabel("Burned: -", self.sample_header_card)
        self.stats_burned_label.setStyleSheet(
            "background: rgba(220, 38, 38, 0.18); color: #b91c1c; padding: 3px 8px; border-radius: 4px; font-weight: 600;"
        )
        self.stats_runs_label = CaptionLabel("Sweeps: -", self.sample_header_card)
        for label in (self.stats_devices_label, self.stats_tested_label, self.stats_runs_label):
            label.setStyleSheet(
                "background: palette(midlight); padding: 3px 8px; border-radius: 4px; font-weight: 500;"
            )
        for label in (
            self.stats_devices_label,
            self.stats_tested_label,
            self.stats_completed_label,
            self.stats_burned_label,
            self.stats_runs_label,
        ):
            header_row2.addWidget(label)
        header_row2.addStretch(1)
        header_info_layout.addLayout(header_row2)

        right_layout.addWidget(self.sample_header_card)

        # Tab Navigation (SegmentedWidget)
        self.tabs = SegmentedWidget(right_panel)
        self.stack = QStackedWidget(right_panel)
        right_layout.addWidget(self.tabs)
        right_layout.addWidget(self.stack, 1)

        # ---------------------------------------------------------------------
        # Tab 1: Device Matrix (Siatka) + Cell Inspector
        # ---------------------------------------------------------------------
        matrix_page = QWidget(self.stack)
        matrix_layout = QVBoxLayout(matrix_page)
        matrix_layout.setContentsMargins(0, 0, 0, 0)
        matrix_layout.setSpacing(0)

        self.matrix_splitter = QSplitter(Qt.Orientation.Horizontal, matrix_page)
        self.matrix_splitter.setChildrenCollapsible(False)
        self.matrix_splitter.setHandleWidth(6)

        # Left: Interactive Matrix
        self.matrix_widget = SampleMatrixWidget(self.matrix_splitter)
        self.matrix_widget.setMinimumWidth(240)
        self.matrix_widget.cell_selected.connect(self._on_cell_selected)
        self.matrix_widget.cell_activated.connect(self._on_cell_activated)
        self.matrix_widget.col_rename_requested.connect(self._on_rename_column_requested)
        self.matrix_widget.row_rename_requested.connect(self._on_rename_row_requested)
        self.matrix_widget.col_add_requested.connect(self._on_add_column_requested)
        self.matrix_widget.row_add_requested.connect(self._on_add_row_requested)
        self.matrix_widget.col_delete_requested.connect(self._on_delete_column_requested)
        self.matrix_widget.row_delete_requested.connect(self._on_delete_row_requested)
        self.matrix_widget.cell_state_change_requested.connect(self._on_cell_state_change_requested)
        self.matrix_widget.batch_cell_state_change_requested.connect(self._on_batch_cell_state_change_requested)
        self.matrix_widget.row_state_change_requested.connect(self._on_row_state_change_requested)
        self.matrix_widget.col_state_change_requested.connect(self._on_col_state_change_requested)
        self.matrix_widget.explore_runs_requested.connect(self._explore_cell_in_tree)
        self.matrix_widget.renumber_rows_requested.connect(self._on_renumber_rows_requested)
        self.matrix_splitter.addWidget(self.matrix_widget)

        # Right: Cell Inspector with scroll area to prevent overlap on compact heights
        self.inspector_card = SimpleCardWidget(self.matrix_splitter)
        self.inspector_card.setMinimumWidth(230)
        self.inspector_card.setMaximumWidth(380)
        inspector_card_layout = QVBoxLayout(self.inspector_card)
        inspector_card_layout.setContentsMargins(10, 10, 10, 10)
        inspector_card_layout.setSpacing(8)

        inspector_card_layout.addWidget(SubtitleLabel("Device Inspector", self.inspector_card))
        self.inspector_coord_label = BodyLabel("Select a cell in the grid", self.inspector_card)
        self.inspector_coord_label.setStyleSheet("font-weight: 600;")
        self.inspector_coord_label.setWordWrap(True)
        inspector_card_layout.addWidget(self.inspector_coord_label)

        # Scrollable container for inspector controls
        inspector_scroll = ScrollArea(self.inspector_card)
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setFrameShape(ScrollArea.Shape.NoFrame)
        inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inspector_scroll_content = QWidget(inspector_scroll)
        inspector_layout = QVBoxLayout(inspector_scroll_content)
        inspector_layout.setContentsMargins(0, 0, 4, 0)
        inspector_layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(6)
        self.cell_label_input = LineEdit(inspector_scroll_content)
        self.cell_label_input.setPlaceholderText("e.g. 200 nm Pillar A")
        form.addRow("Device Label:", self.cell_label_input)

        self.row_label_input = LineEdit(inspector_scroll_content)
        self.row_label_input.setPlaceholderText("e.g. Row 1, Strip Alpha")
        form.addRow("Row Label:", self.row_label_input)

        self.col_label_input = LineEdit(inspector_scroll_content)
        self.col_label_input.setPlaceholderText("e.g. 200 nm")
        form.addRow("Col Label:", self.col_label_input)

        self.cell_state_combo = ComboBox(inspector_scroll_content)
        self.cell_state_combo.addItems([
            "untested", "completed", "good", "measured", "burned", "shorted", "open", "degraded"
        ])
        form.addRow("State:", self.cell_state_combo)
        inspector_layout.addLayout(form)

        # Quick State Action buttons
        quick_state_layout = QHBoxLayout()
        self.quick_completed_btn = PushButton("✔ Completed", inspector_scroll_content, FluentIcon.ACCEPT)
        self.quick_completed_btn.setToolTip("Quickly mark cell as Completed (Green) and save immediately")
        self.quick_completed_btn.clicked.connect(lambda: self._quick_mark_state("completed"))

        self.quick_burned_btn = PushButton("🔥 Burned", inspector_scroll_content, FluentIcon.CANCEL)
        self.quick_burned_btn.setToolTip("Quickly mark cell as Burned / Damaged (Red) and save immediately")
        self.quick_burned_btn.clicked.connect(lambda: self._quick_mark_state("burned"))

        quick_state_layout.addWidget(self.quick_completed_btn)
        quick_state_layout.addWidget(self.quick_burned_btn)
        inspector_layout.addLayout(quick_state_layout)

        inspector_layout.addWidget(CaptionLabel("Device Notes / Resistance:", inspector_scroll_content))
        self.cell_notes_input = PlainTextEdit(inspector_scroll_content)
        self.cell_notes_input.setMaximumHeight(60)
        inspector_layout.addWidget(self.cell_notes_input)

        cell_btns = QHBoxLayout()
        self.save_cell_btn = PushButton("Save Cell", inspector_scroll_content, FluentIcon.SAVE)
        self.save_cell_btn.clicked.connect(self._save_cell_changes)
        self.set_target_btn = PrimaryPushButton("★ Set Target", inspector_scroll_content, FluentIcon.TAG)
        self.set_target_btn.clicked.connect(self._set_selected_as_active_target)
        cell_btns.addWidget(self.save_cell_btn)
        cell_btns.addWidget(self.set_target_btn)
        inspector_layout.addLayout(cell_btns)

        runs_hdr_layout = QHBoxLayout()
        runs_hdr_layout.addWidget(CaptionLabel("Cell Measurement Sweeps:", inspector_scroll_content))
        runs_hdr_layout.addStretch(1)
        self.explore_cell_runs_btn = ToolButton(FluentIcon.VIEW, inspector_scroll_content)
        self.explore_cell_runs_btn.setToolTip("Explore Cell Sweeps in Measurement Tree")
        self.explore_cell_runs_btn.clicked.connect(self._explore_current_cell_in_tree)
        runs_hdr_layout.addWidget(self.explore_cell_runs_btn)
        inspector_layout.addLayout(runs_hdr_layout)

        self.cell_runs_table = QTableWidget(inspector_scroll_content)
        self.cell_runs_table.setColumnCount(3)
        self.cell_runs_table.setHorizontalHeaderLabels(["Run / Recipe", "Pts", "Status"])
        self.cell_runs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.cell_runs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.cell_runs_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.cell_runs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cell_runs_table.itemDoubleClicked.connect(self._on_cell_run_double_clicked)
        self.cell_runs_table.setMinimumHeight(80)
        inspector_layout.addWidget(self.cell_runs_table, 1)

        inspector_scroll.setWidget(inspector_scroll_content)
        inspector_card_layout.addWidget(inspector_scroll, 1)

        self.matrix_splitter.addWidget(self.inspector_card)
        self.matrix_splitter.setStretchFactor(0, 1)
        self.matrix_splitter.setStretchFactor(1, 0)
        self.matrix_splitter.setSizes([800, 270])

        matrix_layout.addWidget(self.matrix_splitter, 1)
        self._add_tab(matrix_page, "matrixTab", "Device Grid", FluentIcon.TILES)

        # ---------------------------------------------------------------------
        # Tab 2: Commercial Measurement Browser (Tree + Plot + Figures of Merit)
        # ---------------------------------------------------------------------
        self.measurement_browser = MeasurementBrowserView(self.stack)
        self.measurement_browser.open_in_results_requested.connect(self.open_result_requested.emit)
        # Reference alias for compatibility
        self.runs_table = self.measurement_browser.tree_widget.tree

        self._add_tab(self.measurement_browser, "runsTab", "Measurements & Curves", FluentIcon.HISTORY)

        # ---------------------------------------------------------------------
        # Tab 3: Attachments & Notes (Microscope images, PDFs, fabrication notes)
        # ---------------------------------------------------------------------
        att_page = QWidget(self.stack)
        att_page_layout = QVBoxLayout(att_page)
        att_page_layout.setContentsMargins(0, 0, 0, 0)
        att_page_layout.setSpacing(0)

        self.att_splitter = QSplitter(Qt.Orientation.Horizontal, att_page)
        self.att_splitter.setChildrenCollapsible(False)
        self.att_splitter.setHandleWidth(6)

        # Left: Rich Sample Notes
        notes_card = SimpleCardWidget(self.att_splitter)
        notes_card.setMinimumWidth(240)
        notes_layout = QVBoxLayout(notes_card)
        notes_layout.setContentsMargins(12, 12, 12, 12)
        notes_layout.setSpacing(8)

        notes_toolbar = QHBoxLayout()
        notes_toolbar.addWidget(SubtitleLabel("Sample Research Notes", notes_card))
        notes_toolbar.addStretch(1)
        self.save_notes_btn = PushButton("Save Notes", notes_card, FluentIcon.SAVE)
        self.save_notes_btn.clicked.connect(self._save_sample_notes)
        notes_toolbar.addWidget(self.save_notes_btn)
        notes_layout.addLayout(notes_toolbar)

        self.sample_notes_edit = PlainTextEdit(notes_card)
        self.sample_notes_edit.setPlaceholderText(
            "Document fabrication batch details, stack layers (e.g. Sub/Ta 5/CoFeB 1.2/MgO/CoFeB/Ta), "
            "annealing temperatures, optical microscope observations, wire bonding pinouts..."
        )
        notes_layout.addWidget(self.sample_notes_edit, 1)
        self.att_splitter.addWidget(notes_card)

        # Right: Attachments Gallery (Images, PDFs)
        gallery_card = SimpleCardWidget(self.att_splitter)
        gallery_card.setMinimumWidth(260)
        gallery_layout = QVBoxLayout(gallery_card)
        gallery_layout.setContentsMargins(12, 12, 12, 12)
        gallery_layout.setSpacing(8)

        gallery_toolbar = QHBoxLayout()
        gallery_toolbar.addWidget(SubtitleLabel("Files & Layouts", gallery_card))
        gallery_toolbar.addStretch(1)
        self.add_attachment_btn = PrimaryPushButton(
            "+ Add Photo / PDF...", gallery_card, FluentIcon.ADD
        )
        self.add_attachment_btn.clicked.connect(self._prompt_add_attachment)
        gallery_toolbar.addWidget(self.add_attachment_btn)
        gallery_layout.addLayout(gallery_toolbar)

        # Scrollable area for attachment cards
        self.attachments_scroll = QScrollArea(gallery_card)
        self.attachments_scroll.setWidgetResizable(True)
        self.attachments_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.attachments_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.attachments_container = QWidget(self.attachments_scroll)
        self.attachments_container_layout = QVBoxLayout(self.attachments_container)
        self.attachments_container_layout.setContentsMargins(0, 0, 0, 0)
        self.attachments_container_layout.setSpacing(8)
        self.attachments_container_layout.addStretch(1)
        self.attachments_scroll.setWidget(self.attachments_container)
        gallery_layout.addWidget(self.attachments_scroll, 1)

        self.att_splitter.addWidget(gallery_card)
        self.att_splitter.setStretchFactor(0, 1)
        self.att_splitter.setStretchFactor(1, 1)
        self.att_splitter.setSizes([500, 500])

        att_page_layout.addWidget(self.att_splitter, 1)

        self._add_tab(att_page, "attachmentsTab", "Photos, PDFs & Notes", FluentIcon.DOCUMENT)

        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([220, 950])
        main_layout.addWidget(self.splitter, 1)

        self._sync_active_target_display()

    def _add_tab(self, widget: QWidget, route: str, title: str, icon: FluentIcon) -> None:
        index = self.stack.addWidget(widget)
        self.tabs.addItem(
            routeKey=route,
            text=title,
            onClick=lambda: self.stack.setCurrentIndex(index),
            icon=icon,
        )
        if index == 0:
            self.tabs.setCurrentItem(route)
            self.stack.setCurrentIndex(index)

    # -------------------------------------------------------------------------
    # Sample Management Logic
    # -------------------------------------------------------------------------

    def refresh_samples(self) -> None:
        """Reload all samples from SQLite store."""
        current_id = self._current_sample.sample_id if self._current_sample else None
        samples = self.store.list_samples()

        self.sample_list.blockSignals(True)
        self.sample_list.clear()

        active = self.store.get_active_target()
        filter_text = self.search_input.text().strip().lower()

        selected_row = -1
        for idx, s in enumerate(samples):
            if filter_text:
                haystack = f"{s.sample_id} {s.name} {' '.join(s.tags)} {s.description}".lower()
                if filter_text not in haystack:
                    continue

            is_active_sample = active.is_active and active.sample_id == s.sample_id
            star = " ★" if is_active_sample else ""
            item_text = f"{s.name} ({s.sample_id}){star}\n{len(s.rows)}×{len(s.cols)} grid"

            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, s.sample_id)
            self.sample_list.addItem(item)

            if current_id is not None and s.sample_id == current_id:
                selected_row = self.sample_list.count() - 1

        self.sample_list.blockSignals(False)

        if self.sample_list.count() > 0:
            if selected_row >= 0:
                self.sample_list.setCurrentRow(selected_row)
            else:
                self.sample_list.setCurrentRow(0)
        else:
            self._set_current_sample(None)

        self._sync_active_target_display()
        self.samples_updated.emit()

    def _filter_samples(self, _text: str) -> None:
        self.refresh_samples()

    def _on_sample_selection_changed(
        self, current: QListWidgetItem | None, _prev: QListWidgetItem | None
    ) -> None:
        if current is None:
            self._set_current_sample(None)
            return
        sample_id = current.data(Qt.ItemDataRole.UserRole)
        sample = self.store.get_sample(sample_id)
        self._set_current_sample(sample)

    def _refresh_stats(self, sample: Sample) -> tuple[SampleRunRecord, ...]:
        total = sample.total_cells()
        tested = sum(
            1 for state in sample.device_states.values()
            if state in {"measured", "good", "completed", "burned", "shorted", "open", "degraded"}
        )
        completed = sum(1 for state in sample.device_states.values() if state == "completed")
        burned = sum(1 for state in sample.device_states.values() if state == "burned")
        all_runs = self.store.list_runs_for_sample(sample.sample_id)

        self.stats_devices_label.setText(f"Devices: {total}")
        self.stats_tested_label.setText(f"Tested: {tested}/{total}")
        self.stats_completed_label.setText(f"Completed: {completed}")
        self.stats_burned_label.setText(f"Burned: {burned}")
        self.stats_runs_label.setText(f"Sweeps: {len(all_runs)}")
        return all_runs

    def _set_current_sample(self, sample: Sample | None) -> None:
        self._current_sample = sample
        if sample is None:
            self.current_sample_title.setText("Select or create a sample")
            self.current_sample_desc.setText("No sample selected.")
            self.stats_devices_label.setText("Devices: -")
            self.stats_tested_label.setText("Tested: -")
            self.stats_completed_label.setText("Completed: -")
            self.stats_burned_label.setText("Burned: -")
            self.stats_runs_label.setText("Sweeps: -")
            self.matrix_widget.set_sample(None)
            self.sample_notes_edit.setPlainText("")
            self._populate_attachments(())
            self.measurement_browser.clear()
            return

        self.current_sample_title.setText(f"{sample.name} [{sample.sample_id}]")
        tags_str = f" · Tags: {', '.join(sample.tags)}" if sample.tags else ""
        self.current_sample_desc.setText(f"Created: {sample.created_at_utc[:10]}{tags_str}")

        all_runs = self._refresh_stats(sample)

        # Populate Matrix
        active_target = self.store.get_active_target()
        self.matrix_widget.set_sample(sample, run_records=all_runs, active_target=active_target)

        # Select first cell or previously selected
        if sample.rows and sample.cols:
            r0, c0 = sample.rows[0], sample.cols[0]
            self._on_cell_selected(r0, c0)

        # Populate Notes
        self.sample_notes_edit.setPlainText(sample.description)

        # Populate Attachments
        self._populate_attachments(sample.attachments)

        # Populate Measurement Browser (Tree + Plot + Figures of Merit)
        self.measurement_browser.set_runs(all_runs, sample=sample)

    def _create_new_sample(self) -> None:
        dialog = SampleProgrammingDialog(parent=self)
        if dialog.exec():
            sample = dialog.get_sample()
            self.store.save_sample(sample)
            self.status.emit(f"Sample {sample.sample_id} created.")
            self.refresh_samples()

    def _edit_current_sample(self) -> None:
        if self._current_sample is None:
            return
        dialog = SampleProgrammingDialog(sample=self._current_sample, parent=self)
        if dialog.exec():
            updated = dialog.get_sample()
            self.store.save_sample(updated)
            self.status.emit(f"Sample {updated.sample_id} updated.")
            self.refresh_samples()

    def _on_renumber_rows_requested(self) -> None:
        if self._current_sample is None:
            return
        dialog = RenumberRowsDialog(sample=self._current_sample, parent=self)
        if dialog.exec():
            old_rows = list(self._current_sample.rows)
            updated = dialog.get_renumbered_sample()
            new_rows = list(updated.rows)
            row_mapping = {old: new_rows[i] for i, old in enumerate(old_rows) if i < len(new_rows)}
            self.store.remap_sample_rows(self._current_sample.sample_id, row_mapping)
            self.store.save_sample(updated)
            self.status.emit(
                f"Renumbered rows for {updated.name} to {updated.rows[0]}..{updated.rows[-1]}."
            )
            self.refresh_samples()
            self._set_current_sample(updated)
            InfoBar.success(
                title="Rows Renumbered",
                content=(
                    f"Sample '{updated.name}' rows renumbered to "
                    f"{updated.rows[0]}..{updated.rows[-1]} ({len(updated.rows)} rows total)."
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3500,
                parent=self,
            )

    def _delete_current_sample(self) -> None:
        if self._current_sample is None:
            return
        sid = self._current_sample.sample_id
        self.store.delete_sample(sid)
        self.status.emit(f"Sample {sid} deleted.")
        self.refresh_samples()

    # -------------------------------------------------------------------------
    # Cell Selection & Inspector Logic
    # -------------------------------------------------------------------------

    def _on_cell_selected(self, row: str, col: str) -> None:
        self._selected_cell = (row, col)
        if self._current_sample is None:
            return

        label = self._current_sample.cell_label(row, col)
        state = self._current_sample.cell_state(row, col)
        notes = self._current_sample.cell_notes(row, col)

        row_label = self._current_sample.row_labels.get(row, "")
        col_label = self._current_sample.col_labels.get(col, "")
        coord_detail = f"Row {row} (Col {col})"
        if row_label or col_label:
            coord_detail += f" · {row_label} / {col_label}".strip(" / ")

        self.inspector_coord_label.setText(coord_detail)
        self.cell_label_input.setText(label)
        self.row_label_input.setText(row_label)
        self.col_label_input.setText(col_label)

        state_idx = self.cell_state_combo.findText(state)
        if state_idx >= 0:
            self.cell_state_combo.setCurrentIndex(state_idx)
        else:
            self.cell_state_combo.setCurrentIndex(0)

        self.cell_notes_input.setPlainText(notes)

        # Refresh cell runs mini-table
        cell_runs = self.store.list_runs_for_cell(self._current_sample.sample_id, row, col)
        self.cell_runs_table.setRowCount(len(cell_runs))
        for idx, cr in enumerate(cell_runs):
            f_name = Path(cr.run_path).name or cr.recipe_name
            item_f = QTableWidgetItem(f_name)
            item_f.setData(Qt.ItemDataRole.UserRole, cr.run_path)
            self.cell_runs_table.setItem(idx, 0, item_f)
            self.cell_runs_table.setItem(idx, 1, QTableWidgetItem(str(cr.point_count)))
            self.cell_runs_table.setItem(idx, 2, QTableWidgetItem(cr.status))

    def _on_cell_activated(self, row: str, col: str) -> None:
        """Double click sets as active measurement target."""
        self._on_cell_selected(row, col)
        self._set_selected_as_active_target()

    def _save_cell_changes(self) -> None:
        if self._current_sample is None or self._selected_cell is None:
            return
        row, col = self._selected_cell
        new_label = self.cell_label_input.text().strip()
        new_row_label = self.row_label_input.text().strip()
        new_col_label = self.col_label_input.text().strip()
        new_state = self.cell_state_combo.currentText()
        new_notes = self.cell_notes_input.toPlainText().strip()

        updated = self._current_sample.with_cell_update(
            row, col, label=new_label, state=new_state, notes=new_notes
        )
        if new_row_label != self._current_sample.row_labels.get(row, ""):
            updated = updated.with_row_label(row, new_row_label)
        if new_col_label != self._current_sample.col_labels.get(col, ""):
            updated = updated.with_col_label(col, new_col_label)

        self.store.save_sample(updated)
        self._current_sample = updated
        self._refresh_stats(updated)

        # Update matrix view
        all_runs = self.store.list_runs_for_sample(updated.sample_id)
        active = self.store.get_active_target()
        self.matrix_widget.set_sample(updated, run_records=all_runs, active_target=active)
        self.matrix_widget.select_cell(row, col)

        InfoBar.success(
            title="Device Updated",
            content=f"Saved settings for R{row}:C{col} and labels.",
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000,
        )

    def _on_rename_column_requested(self, col: str, current_label: str) -> None:
        if self._current_sample is None:
            return
        dlg = RenameHeaderDialog("Column", col, current_label, parent=self)
        if dlg.exec():
            new_label = dlg.get_label()
            updated = self._current_sample.with_col_label(col, new_label)
            self.store.save_sample(updated)
            self._set_current_sample(updated)
            self.status.emit(f"Column {col} label updated to '{new_label}'.")

    def _on_rename_row_requested(self, row: str, current_label: str) -> None:
        if self._current_sample is None:
            return
        dlg = RenameHeaderDialog("Row", row, current_label, parent=self)
        if dlg.exec():
            new_label = dlg.get_label()
            updated = self._current_sample.with_row_label(row, new_label)
            self.store.save_sample(updated)
            self._set_current_sample(updated)
            self.status.emit(f"Row {row} label updated to '{new_label}'.")

    def _on_add_column_requested(self, ref_col: str, position: str) -> None:
        if self._current_sample is None:
            return
        next_key = str(len(self._current_sample.cols) + 1)
        dlg = AddHeaderDialog("Column", next_key, parent=self)
        if dlg.exec():
            new_key, new_label = dlg.get_data()
            if not new_key:
                return
            after = ref_col if position == "after" else None
            if position == "before":
                try:
                    idx = self._current_sample.cols.index(ref_col)
                    after = self._current_sample.cols[idx - 1] if idx > 0 else None
                except ValueError:
                    after = None
            updated = self._current_sample.with_added_col(new_key, new_label, after_col=after)
            self.store.save_sample(updated)
            self._set_current_sample(updated)
            self.status.emit(f"Added column {new_key} ('{new_label}').")

    def _on_add_row_requested(self, ref_row: str, position: str) -> None:
        if self._current_sample is None:
            return
        next_key = str(len(self._current_sample.rows) + 1)
        dlg = AddHeaderDialog("Row", next_key, parent=self)
        if dlg.exec():
            new_key, new_label = dlg.get_data()
            if not new_key:
                return
            after = ref_row if position == "below" else None
            if position == "above":
                try:
                    idx = self._current_sample.rows.index(ref_row)
                    after = self._current_sample.rows[idx - 1] if idx > 0 else None
                except ValueError:
                    after = None
            updated = self._current_sample.with_added_row(new_key, new_label, after_row=after)
            self.store.save_sample(updated)
            self._set_current_sample(updated)
            self.status.emit(f"Added row {new_key} ('{new_label}').")

    def _on_delete_column_requested(self, col: str) -> None:
        if self._current_sample is None or len(self._current_sample.cols) <= 1:
            return
        updated = self._current_sample.with_deleted_col(col)
        self.store.save_sample(updated)
        self._set_current_sample(updated)
        self.status.emit(f"Deleted column {col}.")

    def _on_delete_row_requested(self, row: str) -> None:
        if self._current_sample is None or len(self._current_sample.rows) <= 1:
            return
        updated = self._current_sample.with_deleted_row(row)
        self.store.save_sample(updated)
        self._set_current_sample(updated)
        self.status.emit(f"Deleted row {row}.")

    def _quick_mark_state(self, new_state: str) -> None:
        if self._current_sample is None or self._selected_cell is None:
            return
        idx = self.cell_state_combo.findText(new_state)
        if idx >= 0:
            self.cell_state_combo.setCurrentIndex(idx)
        self._save_cell_changes()

    def _on_cell_state_change_requested(self, row: str, col: str, new_state: str) -> None:
        if self._current_sample is None:
            return
        updated = self._current_sample.with_cell_update(row, col, state=new_state)
        self.store.save_sample(updated)
        self._set_current_sample(updated)
        self.matrix_widget.select_cell(row, col)
        self.status.emit(f"Marked R{row}:C{col} as {new_state}.")

    def _on_batch_cell_state_change_requested(
        self, coords: list[tuple[str, str]], new_state: str
    ) -> None:
        if self._current_sample is None or not coords:
            return
        updated = self._current_sample.with_cells_state(coords, state=new_state)
        self.store.save_sample(updated)
        self._set_current_sample(updated)
        if coords:
            self.matrix_widget.select_cell(coords[0][0], coords[0][1])
        self.status.emit(f"Marked {len(coords)} devices as {new_state}.")

    def _on_row_state_change_requested(self, row: str, new_state: str) -> None:
        if self._current_sample is None:
            return
        updated = self._current_sample.with_row_state(row, state=new_state)
        self.store.save_sample(updated)
        self._set_current_sample(updated)
        self.status.emit(f"Marked entire row {row} as {new_state}.")

    def _on_col_state_change_requested(self, col: str, new_state: str) -> None:
        if self._current_sample is None:
            return
        updated = self._current_sample.with_col_state(col, state=new_state)
        self.store.save_sample(updated)
        self._set_current_sample(updated)
        self.status.emit(f"Marked entire column {col} as {new_state}.")

    # -------------------------------------------------------------------------
    # Active Target Handling
    # -------------------------------------------------------------------------

    def _set_selected_as_active_target(self) -> None:
        if self._current_sample is None or self._selected_cell is None:
            return
        row, col = self._selected_cell
        label = self.cell_label_input.text().strip() or self._current_sample.cell_label(row, col)
        notes = self.cell_notes_input.toPlainText().strip()

        target = ActiveSampleTarget(
            sample_id=self._current_sample.sample_id,
            sample_name=self._current_sample.name,
            row=row,
            col=col,
            device_label=label,
            notes=notes,
        )
        self.store.set_active_target(target)
        self._sync_active_target_display()
        self.active_target_changed.emit(target)
        self.status.emit(f"Active measurement target set: {target.display_text()}")

        # Refresh matrix active highlight
        all_runs = self.store.list_runs_for_sample(self._current_sample.sample_id)
        self.matrix_widget.set_sample(self._current_sample, run_records=all_runs, active_target=target)
        self.matrix_widget.select_cell(row, col)

        InfoBar.info(
            title="Target Activated",
            content=f"Upcoming measurements will record DUT: {target.display_text()}",
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000,
        )

    def _clear_active_target(self) -> None:
        self.store.clear_active_target()
        self._sync_active_target_display()
        self.active_target_changed.emit(ActiveSampleTarget())
        if self._current_sample:
            all_runs = self.store.list_runs_for_sample(self._current_sample.sample_id)
            self.matrix_widget.set_sample(self._current_sample, run_records=all_runs, active_target=ActiveSampleTarget())
        self.status.emit("Active measurement target cleared.")

    def _advance_to_next_device(self) -> None:
        """Advance target to next cell in current sample."""
        active = self.store.get_active_target()
        if not active.is_active or self._current_sample is None:
            return
        if active.sample_id != self._current_sample.sample_id:
            return

        rows = self._current_sample.rows
        cols = self._current_sample.cols
        if not rows or not cols:
            return

        try:
            r_idx = rows.index(str(active.row))
            c_idx = cols.index(str(active.col))
        except ValueError:
            r_idx, c_idx = 0, 0

        # Advance column first, then next row
        c_idx += 1
        if c_idx >= len(cols):
            c_idx = 0
            r_idx += 1
            if r_idx >= len(rows):
                r_idx = 0  # wrap around

        next_r = rows[r_idx]
        next_c = cols[c_idx]
        next_label = self._current_sample.cell_label(next_r, next_c)

        next_target = ActiveSampleTarget(
            sample_id=self._current_sample.sample_id,
            sample_name=self._current_sample.name,
            row=next_r,
            col=next_c,
            device_label=next_label,
            notes=self._current_sample.cell_notes(next_r, next_c),
        )
        self.store.set_active_target(next_target)
        self._sync_active_target_display()
        self.active_target_changed.emit(next_target)

        self._on_cell_selected(next_r, next_c)
        all_runs = self.store.list_runs_for_sample(self._current_sample.sample_id)
        self.matrix_widget.set_sample(self._current_sample, run_records=all_runs, active_target=next_target)
        self.matrix_widget.select_cell(next_r, next_c)
        self.status.emit(f"Advanced target to: {next_target.display_text()}")

    def _sync_active_target_display(self) -> None:
        active = self.store.get_active_target()
        if active.is_active:
            dev = f"[{active.device_label}]" if active.device_label else f"[R{active.row}:C{active.col}]"
            short_text = f"Target: {active.sample_id} · R{active.row}:C{active.col} {dev}"
            self.active_target_label.setText(short_text)
            self.active_target_card.setToolTip(f"Active DUT: {active.display_text()}")
            self.active_target_card.show()
        else:
            self.active_target_label.setText("No active sample target")
            self.active_target_card.setToolTip("Select a sample cell and click '★ Set Target' to activate a measurement target.")

    # -------------------------------------------------------------------------
    # Attachments & Notes
    # -------------------------------------------------------------------------

    def _save_sample_notes(self) -> None:
        if self._current_sample is None:
            return
        new_desc = self.sample_notes_edit.toPlainText().strip()
        updated = Sample(
            sample_id=self._current_sample.sample_id,
            name=self._current_sample.name,
            description=new_desc,
            tags=self._current_sample.tags,
            rows=self._current_sample.rows,
            row_labels=self._current_sample.row_labels,
            cols=self._current_sample.cols,
            col_labels=self._current_sample.col_labels,
            device_labels=self._current_sample.device_labels,
            device_states=self._current_sample.device_states,
            device_notes=self._current_sample.device_notes,
            attachments=self._current_sample.attachments,
        )
        self.store.save_sample(updated)
        self._current_sample = updated
        InfoBar.success(
            title="Notes Saved",
            content="Sample research notes updated successfully.",
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000,
        )

    def _prompt_add_attachment(self) -> None:
        if self._current_sample is None:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Attachment (Image or PDF)",
            "",
            "All Supported (*.png *.jpg *.jpeg *.pdf *.bmp *.tif *.tiff);;Images (*.png *.jpg *.jpeg *.bmp);;PDF Documents (*.pdf);;All Files (*.*)",
        )
        if not file_path:
            return

        att = self.store.add_attachment(self._current_sample.sample_id, file_path)
        # Reload sample with new attachments
        self._current_sample = self.store.get_sample(self._current_sample.sample_id)
        if self._current_sample:
            self._populate_attachments(self._current_sample.attachments)
        self.status.emit(f"Added attachment {att.filename} to {att.sample_id}.")

    def _populate_attachments(self, attachments: tuple[SampleAttachment, ...]) -> None:
        # Clear existing card widgets
        while self.attachments_container_layout.count() > 1:
            child = self.attachments_container_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        for att in attachments:
            full_path = self.store.get_attachment_path(att)
            card = AttachmentCard(att, full_path, self.attachments_container)
            card.open_requested.connect(self._open_attachment)
            card.delete_requested.connect(self._delete_attachment)
            self.attachments_container_layout.insertWidget(
                self.attachments_container_layout.count() - 1, card
            )

    def _open_attachment(self, attachment: SampleAttachment) -> None:
        open_attachment(attachment, self.store, parent=self)

    def _delete_attachment(self, attachment: SampleAttachment) -> None:
        self.store.delete_attachment(attachment.id)
        if self._current_sample:
            self._current_sample = self.store.get_sample(self._current_sample.sample_id)
            if self._current_sample:
                self._populate_attachments(self._current_sample.attachments)
        self.status.emit(f"Deleted attachment {attachment.filename}.")

    # -------------------------------------------------------------------------
    # Measurement Runs History Tab
    # -------------------------------------------------------------------------

    def _populate_runs_table(self, runs: tuple[SampleRunRecord, ...] | Sequence[SampleRunRecord]) -> None:
        self.measurement_browser.set_runs(runs, sample=self._current_sample)

    def _open_selected_run(self) -> None:
        selected = self.measurement_browser.tree_widget.get_selected_run()
        if selected and selected.run_path:
            self.open_result_requested.emit(str(selected.run_path))

    def _switch_to_measurements_tab(self) -> None:
        self.tabs.setCurrentItem("runsTab")
        self.stack.setCurrentWidget(self.measurement_browser)

    def _explore_cell_in_tree(self, row: str, col: str) -> None:
        self._switch_to_measurements_tab()
        self.measurement_browser.filter_by_device(row, col)

    def _explore_current_cell_in_tree(self) -> None:
        if self._selected_cell is not None:
            self._explore_cell_in_tree(self._selected_cell[0], self._selected_cell[1])
        else:
            self._switch_to_measurements_tab()

    def _on_cell_run_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        target_item = self.cell_runs_table.item(row, 0)
        if target_item:
            path = target_item.data(Qt.ItemDataRole.UserRole)
            if path:
                self.open_result_requested.emit(str(path))
