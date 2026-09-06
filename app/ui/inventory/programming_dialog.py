"""Sample programming and grid configuration wizard dialog with fine-grained structure editing."""

from __future__ import annotations

from dataclasses import replace
import string

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    SimpleCardWidget,
    SpinBox,
    SubtitleLabel,
    TableWidget,
    ToolButton,
)

from app.inventory.models import Sample


class ColumnLabelsEdit(PlainTextEdit):
    """Multi-line plain text editor for comma-separated column dimension labels with word-wrap."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrapMode(QTextOption.WrapMode.WordWrap)

    def text(self) -> str:
        """Compatibility accessor returning plain text contents."""
        return self.toPlainText()

    def setText(self, text: str) -> None:
        """Compatibility mutator setting plain text contents."""
        self.setPlainText(text)


class RenumberRowsDialog(QDialog):
    """Dialog allowing users to quickly shift or renumber all rows for a sample."""

    def __init__(self, sample: Sample, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sample = sample
        self.setWindowTitle(f"Renumber Rows · {sample.name}")
        self.setMinimumWidth(440)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        layout.addWidget(SubtitleLabel(f"Renumber Rows: {sample.name}", self))
        layout.addWidget(
            CaptionLabel(
                "Change starting row number (e.g. from 1..10 to 20..30) while preserving existing device measurements and statuses.",
                self,
            )
        )

        card = SimpleCardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        # Current row info
        grid.addWidget(BodyLabel("Current Rows:", card), 0, 0)
        first_r = sample.rows[0] if sample.rows else "1"
        last_r = sample.rows[-1] if sample.rows else "1"
        curr_label = CaptionLabel(f"Row {first_r} to Row {last_r} ({len(sample.rows)} rows)", card)
        grid.addWidget(curr_label, 0, 1)

        # Start row
        grid.addWidget(BodyLabel("Start Row Number *:", card), 1, 0)
        self.start_spin = SpinBox(card)
        self.start_spin.setRange(0, 5000)
        default_start = 20 if first_r == "1" else (int(first_r) if first_r.isdigit() else 20)
        self.start_spin.setValue(default_start)
        grid.addWidget(self.start_spin, 1, 1)

        # End row
        grid.addWidget(BodyLabel("End Row Number:", card), 2, 0)
        self.end_spin = SpinBox(card)
        self.end_spin.setRange(0, 5000)
        default_count = len(sample.rows) or 11
        self.end_spin.setValue(default_start + default_count - 1)
        grid.addWidget(self.end_spin, 2, 1)

        # Total count
        grid.addWidget(BodyLabel("Total Rows Count:", card), 3, 0)
        self.count_spin = SpinBox(card)
        self.count_spin.setRange(1, 1000)
        self.count_spin.setValue(default_count)
        grid.addWidget(self.count_spin, 3, 1)

        # Optional prefix
        grid.addWidget(BodyLabel("Row Prefix:", card), 4, 0)
        self.prefix_input = LineEdit(card)
        self.prefix_input.setPlaceholderText("Optional (e.g. Row, Strip, l)")
        grid.addWidget(self.prefix_input, 4, 1)

        card_layout.addLayout(grid)

        # Live sync between start, end, count
        self._syncing = False
        self.start_spin.valueChanged.connect(self._on_start_changed)
        self.end_spin.valueChanged.connect(self._on_end_changed)
        self.count_spin.valueChanged.connect(self._on_count_changed)

        self.preview_label = CaptionLabel("", card)
        self.preview_label.setStyleSheet("font-weight: 500; color: #0098ff;")
        card_layout.addWidget(self.preview_label)
        self._update_preview()

        layout.addWidget(card)

        # Bottom buttons
        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        cancel_btn = PushButton("Cancel", self)
        cancel_btn.clicked.connect(self.reject)
        apply_btn = PrimaryPushButton("Apply Renumbering", self, FluentIcon.SYNC)
        apply_btn.clicked.connect(self.accept)
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(apply_btn)
        layout.addLayout(btn_box)

    def _on_start_changed(self, val: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.end_spin.setValue(val + self.count_spin.value() - 1)
            self._update_preview()
        finally:
            self._syncing = False

    def _on_end_changed(self, val: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            start = self.start_spin.value()
            if val >= start:
                self.count_spin.setValue(val - start + 1)
            self._update_preview()
        finally:
            self._syncing = False

    def _on_count_changed(self, val: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.end_spin.setValue(self.start_spin.value() + val - 1)
            self._update_preview()
        finally:
            self._syncing = False

    def _update_preview(self) -> None:
        start = self.start_spin.value()
        end = self.end_spin.value()
        count = self.count_spin.value()
        self.preview_label.setText(f"Result: Rows {start} through {end} ({count} rows total)")

    def get_renumbered_sample(self) -> Sample:
        start = self.start_spin.value()
        count = self.count_spin.value()
        prefix = self.prefix_input.text().strip()
        return self._sample.with_row_renumbering(start_row=start, count=count, row_prefix=prefix)


class SampleProgrammingDialog(QDialog):
    """Wizard to define, configure, or re-program a sample and its device grid.

    Provides both quick presets and fine-grained per-row and per-column table editing
    using native Fluent styling, clean card containers, and responsive layouts.
    """

    _COLUMN_PRESETS = {
        "Custom comma-separated...": "",
        "INL 10-Pillar Wedge (100 nm - 1 µm, P1-P10)": "100 nm (P1), 200 nm (P2), 250 nm (P3), 300 nm (P4), 400 nm (P5), 500 nm (P6), 600 nm (P7), 700 nm (P8), 800 nm (P9), 1 µm (P10)",
        "MTJ Standard (50 nm, 100 nm, 200 nm, 500 nm, 1 µm)": "50 nm, 100 nm, 200 nm, 500 nm, 1 µm",
        "Submicron Wedge (40 nm, 60 nm, 80 nm, 100 nm, 150 nm, 200 nm)": "40 nm, 60 nm, 80 nm, 100 nm, 150 nm, 200 nm",
        "Micron Pillars (1 µm, 2 µm, 5 µm, 10 µm, 20 µm)": "1 µm, 2 µm, 5 µm, 10 µm, 20 µm",
        "Circular MTJ (50 nm, 100 nm, 150 nm, 200 nm)": "50 nm, 100 nm, 150 nm, 200 nm",
        "Elliptical (100x200 nm, 100x300 nm, 100x400 nm)": "100x200 nm, 100x300 nm, 100x400 nm",
    }

    def __init__(
        self,
        sample: Sample | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._existing_sample = sample
        is_edit = sample is not None

        self.setWindowTitle("Edit Sample Structure & Matrix" if is_edit else "New Sample & Grid Setup")
        self.setMinimumWidth(840)
        self.setMinimumHeight(660)
        self.resize(890, 710)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Header Title
        title_text = f"Edit Structure: {sample.name}" if is_edit else "Define New Sample Structure"
        layout.addWidget(SubtitleLabel(title_text, self))
        layout.addWidget(
            CaptionLabel(
                "Configure sample metadata, device grid layout, and individual row and column dimensions.",
                self,
            )
        )

        # Top Card: Sample Metadata
        meta_card = SimpleCardWidget(self)
        meta_layout = QVBoxLayout(meta_card)
        meta_layout.setContentsMargins(16, 14, 16, 14)
        meta_layout.setSpacing(10)

        meta_grid = QGridLayout()
        meta_grid.setHorizontalSpacing(14)
        meta_grid.setVerticalSpacing(8)

        meta_grid.addWidget(BodyLabel("Sample ID *:", meta_card), 0, 0)
        self.id_input = LineEdit(meta_card)
        self.id_input.setPlaceholderText("e.g. INL-MTJ-2026-001, COFEB-WEDGE-A")
        meta_grid.addWidget(self.id_input, 0, 1)

        meta_grid.addWidget(BodyLabel("Sample Name:", meta_card), 0, 2)
        self.name_input = LineEdit(meta_card)
        self.name_input.setPlaceholderText("Human-readable title (e.g. CoFeB/MgO Wedge)")
        meta_grid.addWidget(self.name_input, 0, 3)

        meta_grid.addWidget(BodyLabel("Tags:", meta_card), 1, 0)
        self.tags_input = LineEdit(meta_card)
        self.tags_input.setPlaceholderText("Comma-separated tags (e.g. CoFeB, MTJ, Wedge, 300K)")
        meta_grid.addWidget(self.tags_input, 1, 1, 1, 3)

        meta_grid.addWidget(BodyLabel("Notes / Stack:", meta_card), 2, 0, Qt.AlignmentFlag.AlignTop)
        self.desc_input = PlainTextEdit(meta_card)
        self.desc_input.setPlaceholderText("Fabrication stack details, wafer position, lithography notes...")
        self.desc_input.setFixedHeight(54)
        meta_grid.addWidget(self.desc_input, 2, 1, 1, 3)

        meta_layout.addLayout(meta_grid)
        layout.addWidget(meta_card)

        # Tabs for Grid Configuration
        self.tabs = SegmentedWidget(self)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.stack, 1)

        # === Tab 1: Dimensions & Presets ===
        tab_generator = QWidget(self.stack)
        gen_vbox = QVBoxLayout(tab_generator)
        gen_vbox.setContentsMargins(0, 8, 0, 0)
        gen_vbox.setSpacing(10)

        gen_card = SimpleCardWidget(tab_generator)
        gen_card_layout = QVBoxLayout(gen_card)
        gen_card_layout.setContentsMargins(16, 14, 16, 14)
        gen_card_layout.setSpacing(12)

        gen_grid = QGridLayout()
        gen_grid.setHorizontalSpacing(14)
        gen_grid.setVerticalSpacing(10)

        # Rows setup
        gen_grid.addWidget(BodyLabel("Rows Setup:", gen_card), 0, 0)
        row_box = QHBoxLayout()
        row_box.setSpacing(6)
        self.rows_count = SpinBox(gen_card)
        self.rows_count.setRange(1, 500)
        self.rows_count.setValue(10)
        self.rows_count.setFixedWidth(70)

        self.row_scheme = ComboBox(gen_card)
        self.row_scheme.addItems(["1..N (1, 2, 3...)", "Custom Range (e.g. 20..30)", "Letters (A, B, C...)"])
        self.row_scheme.currentIndexChanged.connect(self._on_row_scheme_changed)

        self.row_start_label = CaptionLabel("From:", gen_card)
        self.row_start_label.setVisible(False)
        self.row_start = SpinBox(gen_card)
        self.row_start.setRange(0, 5000)
        self.row_start.setValue(20)
        self.row_start.setFixedWidth(70)
        self.row_start.setVisible(False)

        self.row_end_label = CaptionLabel("To:", gen_card)
        self.row_end_label.setVisible(False)
        self.row_end = SpinBox(gen_card)
        self.row_end.setRange(0, 5000)
        self.row_end.setValue(30)
        self.row_end.setFixedWidth(70)
        self.row_end.setVisible(False)

        row_box.addWidget(self.rows_count)
        row_box.addWidget(self.row_scheme)
        row_box.addWidget(self.row_start_label)
        row_box.addWidget(self.row_start)
        row_box.addWidget(self.row_end_label)
        row_box.addWidget(self.row_end)
        row_box.addStretch(1)
        gen_grid.addLayout(row_box, 0, 1)

        gen_grid.addWidget(BodyLabel("Row Prefix:", gen_card), 0, 2)
        self.row_label_prefix = LineEdit(gen_card)
        self.row_label_prefix.setPlaceholderText("Optional label prefix (e.g. Strip, Row, l)")
        gen_grid.addWidget(self.row_label_prefix, 0, 3)

        # Columns setup
        gen_grid.addWidget(BodyLabel("Columns Setup:", gen_card), 1, 0)
        col_box = QHBoxLayout()
        col_box.setSpacing(8)
        self.cols_count = SpinBox(gen_card)
        self.cols_count.setRange(1, 100)
        self.cols_count.setValue(5)
        self.cols_count.setFixedWidth(70)

        self.col_scheme = ComboBox(gen_card)
        self.col_scheme.addItems(["1..N (1, 2, 3...)", "Letters (A, B, C...)"])

        col_box.addWidget(self.cols_count)
        col_box.addWidget(self.col_scheme)
        col_box.addStretch(1)
        gen_grid.addLayout(col_box, 1, 1)

        gen_grid.addWidget(BodyLabel("Preset:", gen_card), 1, 2)
        self.col_presets = ComboBox(gen_card)
        self.col_presets.addItems(list(self._COLUMN_PRESETS.keys()))
        self.col_presets.currentIndexChanged.connect(self._on_col_preset_changed)
        gen_grid.addWidget(self.col_presets, 1, 3)

        # Column dimension labels
        gen_grid.addWidget(BodyLabel("Column Dimensions:", gen_card), 2, 0, Qt.AlignmentFlag.AlignTop)
        col_labels_vbox = QVBoxLayout()
        col_labels_vbox.setSpacing(4)
        self.col_labels_input = ColumnLabelsEdit(gen_card)
        self.col_labels_input.setPlaceholderText("e.g. 50 nm, 100 nm, 200 nm, 500 nm, 1 µm")
        self.col_labels_input.setFixedHeight(58)
        col_labels_vbox.addWidget(self.col_labels_input)
        col_labels_vbox.addWidget(
            CaptionLabel(
                "Comma-separated dimensions or labels assigned across columns 1 to N. Wraps automatically so all pillar labels remain fully visible.",
                gen_card,
            )
        )
        gen_grid.addLayout(col_labels_vbox, 2, 1, 1, 3)

        gen_card_layout.addLayout(gen_grid)

        # Dynamic range caption
        self.row_range_caption = CaptionLabel("", gen_card)
        self.row_range_caption.setStyleSheet("color: #0098ff; font-weight: 500;")
        self.row_range_caption.setVisible(False)
        gen_card_layout.addWidget(self.row_range_caption)

        apply_btn = PushButton("Apply Generator to Detailed Tables", gen_card, FluentIcon.SYNC)
        apply_btn.setToolTip("Regenerate the fine-grained row and column lists below from these presets")
        apply_btn.clicked.connect(self._apply_generator_to_tables)
        gen_card_layout.addWidget(apply_btn, 0, Qt.AlignmentFlag.AlignRight)
        gen_card_layout.addStretch(1)

        gen_vbox.addWidget(gen_card)

        self.tabs.addItem(
            "generatorTab",
            "Dimensions && Presets",
            onClick=lambda: self.stack.setCurrentIndex(0),
        )
        self.stack.addWidget(tab_generator)

        # === Tab 2: Detailed Rows & Columns Editor ===
        tab_detailed = QWidget(self.stack)
        detailed_layout = QHBoxLayout(tab_detailed)
        detailed_layout.setContentsMargins(0, 8, 0, 0)
        detailed_layout.setSpacing(12)

        # --- Left Card: Rows Table ---
        rows_card = SimpleCardWidget(tab_detailed)
        rows_vbox = QVBoxLayout(rows_card)
        rows_vbox.setContentsMargins(14, 12, 14, 12)
        rows_vbox.setSpacing(8)

        rows_header = QHBoxLayout()
        rows_title = SubtitleLabel("Rows", rows_card)
        self.rows_badge = CaptionLabel("0 rows", rows_card)
        rows_header.addWidget(rows_title)
        rows_header.addStretch(1)
        rows_header.addWidget(self.rows_badge)
        rows_vbox.addLayout(rows_header)

        self.rows_table = TableWidget(rows_card)
        self.rows_table.setColumnCount(2)
        self.rows_table.setHorizontalHeaderLabels(["Row Key (ID)", "Row Label (Name)"])
        self.rows_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.rows_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rows_table.setBorderVisible(True)
        self.rows_table.setBorderRadius(6)
        rows_vbox.addWidget(self.rows_table)

        rows_btn_box = QHBoxLayout()
        self.add_row_btn = PushButton("Add Row", rows_card, FluentIcon.ADD)
        self.add_row_btn.clicked.connect(self._on_add_row)
        self.del_row_btn = PushButton("Delete", rows_card, FluentIcon.DELETE)
        self.del_row_btn.clicked.connect(self._on_delete_row)
        self.renumber_rows_btn = PushButton("Renumber...", rows_card, FluentIcon.SYNC)
        self.renumber_rows_btn.setToolTip("Renumber all rows in table starting from a given number (e.g. 20, 21...)")
        self.renumber_rows_btn.clicked.connect(self._on_renumber_rows_clicked)
        self.move_row_up_btn = ToolButton(FluentIcon.UP, rows_card)
        self.move_row_up_btn.setToolTip("Move row up")
        self.move_row_up_btn.clicked.connect(lambda: self._move_row(-1))
        self.move_row_down_btn = ToolButton(FluentIcon.DOWN, rows_card)
        self.move_row_down_btn.setToolTip("Move row down")
        self.move_row_down_btn.clicked.connect(lambda: self._move_row(1))
        rows_btn_box.addWidget(self.add_row_btn)
        rows_btn_box.addWidget(self.del_row_btn)
        rows_btn_box.addWidget(self.renumber_rows_btn)
        rows_btn_box.addStretch(1)
        rows_btn_box.addWidget(self.move_row_up_btn)
        rows_btn_box.addWidget(self.move_row_down_btn)
        rows_vbox.addLayout(rows_btn_box)

        detailed_layout.addWidget(rows_card, 1)

        # --- Right Card: Columns Table ---
        cols_card = SimpleCardWidget(tab_detailed)
        cols_vbox = QVBoxLayout(cols_card)
        cols_vbox.setContentsMargins(14, 12, 14, 12)
        cols_vbox.setSpacing(8)

        cols_header = QHBoxLayout()
        cols_title = SubtitleLabel("Columns (Dimensions)", cols_card)
        self.cols_badge = CaptionLabel("0 cols", cols_card)
        cols_header.addWidget(cols_title)
        cols_header.addStretch(1)
        cols_header.addWidget(self.cols_badge)
        cols_vbox.addLayout(cols_header)

        self.cols_table = TableWidget(cols_card)
        self.cols_table.setColumnCount(2)
        self.cols_table.setHorizontalHeaderLabels(["Col Key (ID)", "Col Label (e.g. 200 nm)"])
        self.cols_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.cols_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.cols_table.setBorderVisible(True)
        self.cols_table.setBorderRadius(6)
        cols_vbox.addWidget(self.cols_table)

        cols_btn_box = QHBoxLayout()
        self.add_col_btn = PushButton("Add Col", cols_card, FluentIcon.ADD)
        self.add_col_btn.clicked.connect(self._on_add_col)
        self.del_col_btn = PushButton("Delete", cols_card, FluentIcon.DELETE)
        self.del_col_btn.clicked.connect(self._on_delete_col)
        self.move_col_up_btn = ToolButton(FluentIcon.LEFT_ARROW, cols_card)
        self.move_col_up_btn.setToolTip("Move column left")
        self.move_col_up_btn.clicked.connect(lambda: self._move_col(-1))
        self.move_col_down_btn = ToolButton(FluentIcon.RIGHT_ARROW, cols_card)
        self.move_col_down_btn.setToolTip("Move column right")
        self.move_col_down_btn.clicked.connect(lambda: self._move_col(1))
        cols_btn_box.addWidget(self.add_col_btn)
        cols_btn_box.addWidget(self.del_col_btn)
        cols_btn_box.addStretch(1)
        cols_btn_box.addWidget(self.move_col_up_btn)
        cols_btn_box.addWidget(self.move_col_down_btn)
        cols_vbox.addLayout(cols_btn_box)

        detailed_layout.addWidget(cols_card, 1)

        self.tabs.addItem(
            "detailedTab",
            "Row && Column Tables",
            onClick=lambda: self.stack.setCurrentIndex(1),
        )
        self.stack.addWidget(tab_detailed)

        # Default to tab 0 so main dimensions & presets are immediately editable
        self.tabs.setCurrentItem("generatorTab")
        self.stack.setCurrentIndex(0)

        # Bottom Actions
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 6, 0, 0)
        button_layout.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        save_title = "Save Structure Changes" if is_edit else "Create Sample & Grid"
        self.save_button = PrimaryPushButton(save_title, self, FluentIcon.SAVE if is_edit else FluentIcon.ACCEPT)
        self.save_button.clicked.connect(self._on_save)

        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.save_button)
        layout.addLayout(button_layout)

        # Wire up generator live sync
        self._syncing_from_sample = False
        self._syncing_row_range = False
        self.rows_count.valueChanged.connect(self._on_rows_count_changed)
        self.row_scheme.currentIndexChanged.connect(self._on_generator_input_changed)
        self.row_start.valueChanged.connect(self._on_row_start_changed)
        self.row_end.valueChanged.connect(self._on_row_end_changed)
        self.row_label_prefix.textChanged.connect(self._on_generator_input_changed)
        self.cols_count.valueChanged.connect(self._on_generator_input_changed)
        self.col_scheme.currentIndexChanged.connect(self._on_generator_input_changed)
        self.col_labels_input.textChanged.connect(self._on_generator_input_changed)

        # Populate
        if sample is not None:
            self._populate_from_sample(sample)
        else:
            self._apply_generator_to_tables()

    def _update_badges(self) -> None:
        if hasattr(self, "rows_badge"):
            self.rows_badge.setText(f"{self.rows_table.rowCount()} rows")
        if hasattr(self, "cols_badge"):
            self.cols_badge.setText(f"{self.cols_table.rowCount()} cols")

    def _on_generator_input_changed(self, *args: object) -> None:
        if getattr(self, "_syncing_from_sample", False):
            return
        if self._existing_sample is None or self.stack.currentIndex() == 0:
            self._apply_generator_to_tables()

    def _on_row_scheme_changed(self, index: int) -> None:
        is_custom = index == 1
        self.row_start_label.setVisible(is_custom)
        self.row_start.setVisible(is_custom)
        self.row_end_label.setVisible(is_custom)
        self.row_end.setVisible(is_custom)
        self.row_range_caption.setVisible(is_custom)
        if is_custom:
            self.row_end.setValue(self.row_start.value() + self.rows_count.value() - 1)
            self._update_row_range_caption()
        self._on_generator_input_changed()

    def _on_row_start_changed(self, val: int) -> None:
        if getattr(self, "_syncing_row_range", False):
            return
        self._syncing_row_range = True
        try:
            self.row_end.setValue(val + self.rows_count.value() - 1)
            self._update_row_range_caption()
        finally:
            self._syncing_row_range = False
        self._on_generator_input_changed()

    def _on_row_end_changed(self, val: int) -> None:
        if getattr(self, "_syncing_row_range", False):
            return
        self._syncing_row_range = True
        try:
            start = self.row_start.value()
            if val >= start:
                self.rows_count.setValue(val - start + 1)
            self._update_row_range_caption()
        finally:
            self._syncing_row_range = False
        self._on_generator_input_changed()

    def _on_rows_count_changed(self, val: int) -> None:
        if getattr(self, "_syncing_row_range", False):
            return
        self._syncing_row_range = True
        try:
            self.row_end.setValue(self.row_start.value() + val - 1)
            self._update_row_range_caption()
        finally:
            self._syncing_row_range = False
        self._on_generator_input_changed()

    def _update_row_range_caption(self) -> None:
        if hasattr(self, "row_range_caption"):
            start = self.row_start.value()
            end = self.row_end.value()
            count = self.rows_count.value()
            self.row_range_caption.setText(f"→ Generating Rows {start} through {end} ({count} rows total)")

    def _on_col_preset_changed(self, index: int) -> None:
        key = self.col_presets.currentText()
        val = self._COLUMN_PRESETS.get(key, "")
        if val:
            self.col_labels_input.setText(val)
            parts = [p.strip() for p in val.split(",") if p.strip()]
            if parts:
                self.cols_count.setValue(len(parts))

    def _apply_generator_to_tables(self) -> None:
        """Generate row and column lists from the quick generator inputs into the detailed tables."""
        r_count = self.rows_count.value()
        r_scheme = self.row_scheme.currentIndex()
        row_keys: list[str] = []
        if r_scheme == 0:  # 1..N
            row_keys = [str(i) for i in range(1, r_count + 1)]
        elif r_scheme == 1:  # Custom start
            start = self.row_start.value()
            row_keys = [str(start + i) for i in range(r_count)]
        else:  # Letters
            letters = list(string.ascii_uppercase)
            row_keys = [letters[i % 26] + (str(i // 26) if i >= 26 else "") for i in range(r_count)]

        row_prefix = self.row_label_prefix.text().strip()
        self.rows_table.setRowCount(len(row_keys))
        for idx, r in enumerate(row_keys):
            existing_label = ""
            if self._existing_sample:
                if r in self._existing_sample.row_labels:
                    existing_label = self._existing_sample.row_labels[r]
                elif idx < len(self._existing_sample.rows):
                    old_r = self._existing_sample.rows[idx]
                    old_l = self._existing_sample.row_labels.get(old_r, "")
                    if old_l in (old_r, f"Row {old_r}"):
                        existing_label = f"Row {r}"
                    else:
                        existing_label = old_l

            label = existing_label or (f"{row_prefix} {r}".strip() if row_prefix else f"Row {r}")
            item_key = QTableWidgetItem(r)
            item_label = QTableWidgetItem(label)
            self.rows_table.setItem(idx, 0, item_key)
            self.rows_table.setItem(idx, 1, item_label)

        c_count = self.cols_count.value()
        c_scheme = self.col_scheme.currentIndex()
        col_keys: list[str] = []
        if c_scheme == 0:
            col_keys = [str(i) for i in range(1, c_count + 1)]
        else:
            letters = list(string.ascii_uppercase)
            col_keys = [letters[i % 26] for i in range(c_count)]

        raw_text = self.col_labels_input.text().replace("\n", ",")
        raw_labels = [p.strip() for p in raw_text.split(",") if p.strip()]
        self.cols_table.setRowCount(len(col_keys))
        for idx, c in enumerate(col_keys):
            existing_label = (
                self._existing_sample.col_labels.get(c, "")
                if self._existing_sample
                else ""
            )
            label = (
                raw_labels[idx]
                if idx < len(raw_labels)
                else existing_label or f"Col {c}"
            )
            item_key = QTableWidgetItem(c)
            item_label = QTableWidgetItem(label)
            self.cols_table.setItem(idx, 0, item_key)
            self.cols_table.setItem(idx, 1, item_label)

        self._update_badges()

    def _populate_from_sample(self, sample: Sample) -> None:
        self._syncing_from_sample = True
        try:
            self.id_input.setText(sample.sample_id)
            self.id_input.setEnabled(False)  # Keep ID stable in edit mode
            self.name_input.setText(sample.name)
            self.tags_input.setText(", ".join(sample.tags))
            self.desc_input.setPlainText(sample.description)

            count = len(sample.rows) or 10
            self.rows_count.setValue(count)
            self.cols_count.setValue(len(sample.cols) or 5)

            if sample.rows and all(r.isdigit() for r in sample.rows):
                start = int(sample.rows[0])
                if start != 1:
                    self.row_scheme.setCurrentIndex(1)
                    self.row_start.setValue(start)
                    self.row_end.setValue(start + count - 1)
                    self._on_row_scheme_changed(1)

            if sample.col_labels:
                labels = [sample.col_labels.get(c, "") for c in sample.cols]
                self.col_labels_input.setText(", ".join(filter(bool, labels)))

            self.rows_table.setRowCount(len(sample.rows))
            for idx, r in enumerate(sample.rows):
                label = sample.row_labels.get(r, f"Row {r}")
                self.rows_table.setItem(idx, 0, QTableWidgetItem(str(r)))
                self.rows_table.setItem(idx, 1, QTableWidgetItem(str(label)))

            self.cols_table.setRowCount(len(sample.cols))
            for idx, c in enumerate(sample.cols):
                label = sample.col_labels.get(c, f"Col {c}")
                self.cols_table.setItem(idx, 0, QTableWidgetItem(str(c)))
                self.cols_table.setItem(idx, 1, QTableWidgetItem(str(label)))

            self._update_badges()
        finally:
            self._syncing_from_sample = False

    def _on_renumber_rows_clicked(self) -> None:
        """Prompt user for a starting row number and renumber all table rows sequentially."""
        current_rows = self.rows_table.rowCount()
        if current_rows == 0:
            return
        first_item = self.rows_table.item(0, 0)
        current_start = int(first_item.text()) if first_item and first_item.text().isdigit() else 1
        default_start = 20 if current_start == 1 else current_start

        dlg = QDialog(self)
        dlg.setWindowTitle("Renumber Rows")
        dlg.setMinimumWidth(340)
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setSpacing(12)
        dlg_layout.addWidget(SubtitleLabel("Renumber Rows", dlg))
        dlg_layout.addWidget(
            CaptionLabel(f"Enter starting index for all {current_rows} rows in table:", dlg)
        )

        h_box = QHBoxLayout()
        h_box.addWidget(BodyLabel("Start Row Number:", dlg))
        spin = SpinBox(dlg)
        spin.setRange(0, 5000)
        spin.setValue(default_start)
        h_box.addWidget(spin)
        dlg_layout.addLayout(h_box)

        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        cancel_b = PushButton("Cancel", dlg)
        cancel_b.clicked.connect(dlg.reject)
        apply_b = PrimaryPushButton("Apply", dlg, FluentIcon.SYNC)
        apply_b.clicked.connect(dlg.accept)
        btn_box.addWidget(cancel_b)
        btn_box.addWidget(apply_b)
        dlg_layout.addLayout(btn_box)

        if dlg.exec():
            start_num = spin.value()
            for r_idx in range(current_rows):
                new_key = str(start_num + r_idx)
                key_item = self.rows_table.item(r_idx, 0)
                label_item = self.rows_table.item(r_idx, 1)
                old_key = key_item.text() if key_item else str(r_idx + 1)
                old_label = label_item.text() if label_item else f"Row {old_key}"

                self.rows_table.setItem(r_idx, 0, QTableWidgetItem(new_key))
                if old_label in (old_key, f"Row {old_key}", ""):
                    self.rows_table.setItem(r_idx, 1, QTableWidgetItem(f"Row {new_key}"))

    def _on_add_row(self) -> None:
        count = self.rows_table.rowCount()
        # Find next row number based on last row
        last_item = self.rows_table.item(count - 1, 0) if count > 0 else None
        if last_item and last_item.text().isdigit():
            next_key = str(int(last_item.text()) + 1)
        else:
            next_key = str(count + 1)
        self.rows_table.insertRow(count)
        self.rows_table.setItem(count, 0, QTableWidgetItem(next_key))
        self.rows_table.setItem(count, 1, QTableWidgetItem(f"Row {next_key}"))
        self.rows_table.setCurrentCell(count, 1)
        self._update_badges()

    def _on_delete_row(self) -> None:
        curr = self.rows_table.currentRow()
        if curr >= 0 and self.rows_table.rowCount() > 1:
            self.rows_table.removeRow(curr)
            self._update_badges()

    def _move_row(self, delta: int) -> None:
        curr = self.rows_table.currentRow()
        target = curr + delta
        if curr < 0 or target < 0 or target >= self.rows_table.rowCount():
            return
        key_curr = self.rows_table.item(curr, 0).text() if self.rows_table.item(curr, 0) else ""
        label_curr = self.rows_table.item(curr, 1).text() if self.rows_table.item(curr, 1) else ""
        key_target = self.rows_table.item(target, 0).text() if self.rows_table.item(target, 0) else ""
        label_target = self.rows_table.item(target, 1).text() if self.rows_table.item(target, 1) else ""

        self.rows_table.setItem(curr, 0, QTableWidgetItem(key_target))
        self.rows_table.setItem(curr, 1, QTableWidgetItem(label_target))
        self.rows_table.setItem(target, 0, QTableWidgetItem(key_curr))
        self.rows_table.setItem(target, 1, QTableWidgetItem(label_curr))
        self.rows_table.setCurrentCell(target, 1)

    def _on_add_col(self) -> None:
        count = self.cols_table.rowCount()
        next_key = str(count + 1)
        self.cols_table.insertRow(count)
        self.cols_table.setItem(count, 0, QTableWidgetItem(next_key))
        self.cols_table.setItem(count, 1, QTableWidgetItem(f"Col {next_key}"))
        self.cols_table.setCurrentCell(count, 1)
        self._update_badges()

    def _on_delete_col(self) -> None:
        curr = self.cols_table.currentRow()
        if curr >= 0 and self.cols_table.rowCount() > 1:
            self.cols_table.removeRow(curr)
            self._update_badges()

    def _move_col(self, delta: int) -> None:
        curr = self.cols_table.currentRow()
        target = curr + delta
        if curr < 0 or target < 0 or target >= self.cols_table.rowCount():
            return
        key_curr = self.cols_table.item(curr, 0).text() if self.cols_table.item(curr, 0) else ""
        label_curr = self.cols_table.item(curr, 1).text() if self.cols_table.item(curr, 1) else ""
        key_target = self.cols_table.item(target, 0).text() if self.cols_table.item(target, 0) else ""
        label_target = self.cols_table.item(target, 1).text() if self.cols_table.item(target, 1) else ""

        self.cols_table.setItem(curr, 0, QTableWidgetItem(key_target))
        self.cols_table.setItem(curr, 1, QTableWidgetItem(label_target))
        self.cols_table.setItem(target, 0, QTableWidgetItem(key_curr))
        self.cols_table.setItem(target, 1, QTableWidgetItem(label_curr))
        self.cols_table.setCurrentCell(target, 1)

    def _on_save(self) -> None:
        sample_id = self.id_input.text().strip()
        if not sample_id:
            self.id_input.setFocus()
            return
        if self.rows_table.rowCount() == 0:
            self._on_add_row()
        if self.cols_table.rowCount() == 0:
            self._on_add_col()
        self.accept()

    def get_sample(self) -> Sample:
        """Build or update the configured Sample object from dialog fields."""
        sample_id = self.id_input.text().strip()
        name = self.name_input.text().strip() or sample_id
        tags = tuple(
            t.strip() for t in self.tags_input.text().split(",") if t.strip()
        )
        desc = self.desc_input.toPlainText().strip()

        row_keys: list[str] = []
        row_labels: dict[str, str] = {}
        for r_idx in range(self.rows_table.rowCount()):
            k_item = self.rows_table.item(r_idx, 0)
            l_item = self.rows_table.item(r_idx, 1)
            key = k_item.text().strip() if k_item else str(r_idx + 1)
            label = l_item.text().strip() if l_item else key
            if not key:
                key = str(r_idx + 1)
            row_keys.append(key)
            if label:
                row_labels[key] = label

        col_keys: list[str] = []
        col_labels: dict[str, str] = {}
        for c_idx in range(self.cols_table.rowCount()):
            k_item = self.cols_table.item(c_idx, 0)
            l_item = self.cols_table.item(c_idx, 1)
            key = k_item.text().strip() if k_item else str(c_idx + 1)
            label = l_item.text().strip() if l_item else key
            if not key:
                key = str(c_idx + 1)
            col_keys.append(key)
            if label:
                col_labels[key] = label

        if self._existing_sample is not None:
            # Map positional row changes so cell states, labels and notes are preserved
            row_mapping = {
                old_r: row_keys[idx]
                for idx, old_r in enumerate(self._existing_sample.rows)
                if idx < len(row_keys)
            }
            updated = self._existing_sample.with_structure(
                rows=row_keys,
                row_labels=row_labels,
                cols=col_keys,
                col_labels=col_labels,
                row_mapping=row_mapping,
            )
            return replace(
                updated,
                name=name,
                description=desc,
                tags=tags,
            )

        return Sample(
            sample_id=sample_id,
            name=name,
            description=desc,
            tags=tags,
            rows=tuple(row_keys),
            row_labels=row_labels,
            cols=tuple(col_keys),
            col_labels=col_labels,
        )
