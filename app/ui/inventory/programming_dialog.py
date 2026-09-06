"""Sample programming and grid configuration wizard dialog with fine-grained structure editing."""

from __future__ import annotations

from dataclasses import replace
import string

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    SpinBox,
    SubtitleLabel,
    ToolButton,
)

from app.inventory.models import Sample


class SampleProgrammingDialog(QDialog):
    """Wizard to define, configure, or re-program a sample and its device grid.

    Provides both quick presets and fine-grained per-row and per-column table editing.
    """

    _COLUMN_PRESETS = {
        "Custom comma-separated...": "",
        "MTJ Standard (50 nm, 100 nm, 200 nm, 500 nm, 1 µm)": "50 nm, 100 nm, 200 nm, 500 nm, 1 µm",
        "Submicron Wedge (40 nm, 60 nm, 80 nm, 100 nm, 150 nm, 200 nm)": "40 nm, 60 nm, 80 nm, 100 nm, 150 nm, 200 nm",
        "Micron Pillars (1 µm, 2 µm, 5 µm, 10 µm, 20 µm)": "1 µm, 2 µm, 5 µm, 10 µm, 20 µm",
        "Circular MTJ (50nm, 100nm, 150nm, 200nm)": "50 nm, 100 nm, 150 nm, 200 nm",
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
        self.setMinimumWidth(720)
        self.setMinimumHeight(560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Header Title
        title_text = f"Edit Structure: {sample.name}" if is_edit else "Define New Sample Structure"
        layout.addWidget(SubtitleLabel(title_text, self))
        layout.addWidget(
            CaptionLabel(
                "Configure sample metadata, grid matrix layout, and individual row and column labels.",
                self,
            )
        )

        # Metadata Form
        meta_form = QFormLayout()
        meta_form.setSpacing(8)
        meta_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.id_input = LineEdit(self)
        self.id_input.setPlaceholderText("e.g. XYZ, MTJ-2026-001, COFEB-WEDGE-A")
        meta_form.addRow("Sample ID *:", self.id_input)

        self.name_input = LineEdit(self)
        self.name_input.setPlaceholderText("Human-readable title (e.g. CoFeB/MgO 1.2 nm Wedge)")
        meta_form.addRow("Sample Name:", self.name_input)

        self.tags_input = LineEdit(self)
        self.tags_input.setPlaceholderText("Comma-separated tags (e.g. CoFeB, MTJ, Wedge, 300K)")
        meta_form.addRow("Tags:", self.tags_input)

        self.desc_input = PlainTextEdit(self)
        self.desc_input.setPlaceholderText("Fabrication stack details, wafer position, lithography notes...")
        self.desc_input.setMaximumHeight(60)
        meta_form.addRow("Notes / Stack:", self.desc_input)

        layout.addLayout(meta_form)

        # Tabs for Grid Configuration: 1. Presets / Fast Generator, 2. Detailed Rows & Columns Editor
        self.tabs = SegmentedWidget(self)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.stack, 1)

        # === Tab 1: Quick Generator & Presets ===
        tab_generator = QWidget(self.stack)
        gen_layout = QVBoxLayout(tab_generator)
        gen_layout.setContentsMargins(0, 8, 0, 0)
        gen_layout.setSpacing(10)

        grid_form = QFormLayout()
        grid_form.setSpacing(8)
        grid_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Rows setup
        row_box = QHBoxLayout()
        self.rows_count = SpinBox(self)
        self.rows_count.setRange(1, 200)
        self.rows_count.setValue(10)

        self.row_scheme = ComboBox(self)
        self.row_scheme.addItems(["1..N (1, 2, 3...)", "Custom Start (e.g. 20, 21...)", "Letters (A, B, C...)"])
        self.row_scheme.currentIndexChanged.connect(self._on_row_scheme_changed)

        self.row_start = SpinBox(self)
        self.row_start.setRange(0, 1000)
        self.row_start.setValue(1)
        self.row_start.setEnabled(False)

        row_box.addWidget(self.rows_count)
        row_box.addWidget(self.row_scheme)
        row_box.addWidget(self.row_start)
        grid_form.addRow("Rows Count & Scheme:", row_box)

        self.row_label_prefix = LineEdit(self)
        self.row_label_prefix.setPlaceholderText("Optional label prefix (e.g. Strip, Row)")
        grid_form.addRow("Row Label Pattern:", self.row_label_prefix)

        # Columns setup
        col_box = QHBoxLayout()
        self.cols_count = SpinBox(self)
        self.cols_count.setRange(1, 100)
        self.cols_count.setValue(5)

        self.col_scheme = ComboBox(self)
        self.col_scheme.addItems(["1..N (1, 2, 3...)", "Letters (A, B, C...)"])

        col_box.addWidget(self.cols_count)
        col_box.addWidget(self.col_scheme)
        grid_form.addRow("Columns Count & Scheme:", col_box)

        self.col_presets = ComboBox(self)
        self.col_presets.addItems(list(self._COLUMN_PRESETS.keys()))
        self.col_presets.currentIndexChanged.connect(self._on_col_preset_changed)
        grid_form.addRow("Column Dimension Preset:", self.col_presets)

        self.col_labels_input = LineEdit(self)
        self.col_labels_input.setPlaceholderText("e.g. 50 nm, 100 nm, 200 nm, 500 nm, 1 µm")
        grid_form.addRow("Column Labels *:", self.col_labels_input)

        gen_layout.addLayout(grid_form)

        apply_btn = PushButton("Apply Generator to Detailed Tables", tab_generator, FluentIcon.SYNC)
        apply_btn.setToolTip("Regenerate the fine-grained row and column lists below from these presets")
        apply_btn.clicked.connect(self._apply_generator_to_tables)
        gen_layout.addWidget(apply_btn, 0, Qt.AlignmentFlag.AlignRight)
        gen_layout.addStretch(1)

        self.tabs.addItem("generatorTab", "1. Dimensions & Presets (Liczba rzędów, kolumn i presety)", onClick=lambda: self.stack.setCurrentIndex(0))
        self.stack.addWidget(tab_generator)

        # === Tab 2: Detailed Rows & Columns Editor ===
        tab_detailed = QWidget(self.stack)
        detailed_layout = QHBoxLayout(tab_detailed)
        detailed_layout.setContentsMargins(0, 8, 0, 0)
        detailed_layout.setSpacing(14)

        # --- Left: Rows Table ---
        rows_group = QGroupBox("Rows (Wiersze)", tab_detailed)
        rows_vbox = QVBoxLayout(rows_group)
        rows_vbox.setContentsMargins(8, 8, 8, 8)
        rows_vbox.setSpacing(6)

        self.rows_table = QTableWidget(rows_group)
        self.rows_table.setColumnCount(2)
        self.rows_table.setHorizontalHeaderLabels(["Row Key (ID)", "Row Label (Name)"])
        self.rows_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.rows_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rows_vbox.addWidget(self.rows_table)

        rows_btn_box = QHBoxLayout()
        self.add_row_btn = PushButton("+ Add Row", rows_group, FluentIcon.ADD)
        self.add_row_btn.clicked.connect(self._on_add_row)
        self.del_row_btn = PushButton("Delete", rows_group, FluentIcon.DELETE)
        self.del_row_btn.clicked.connect(self._on_delete_row)
        self.move_row_up_btn = ToolButton(FluentIcon.UP, rows_group)
        self.move_row_up_btn.clicked.connect(lambda: self._move_row(-1))
        self.move_row_down_btn = ToolButton(FluentIcon.DOWN, rows_group)
        self.move_row_down_btn.clicked.connect(lambda: self._move_row(1))
        rows_btn_box.addWidget(self.add_row_btn)
        rows_btn_box.addWidget(self.del_row_btn)
        rows_btn_box.addWidget(self.move_row_up_btn)
        rows_btn_box.addWidget(self.move_row_down_btn)
        rows_vbox.addLayout(rows_btn_box)

        detailed_layout.addWidget(rows_group, 1)

        # --- Right: Columns Table ---
        cols_group = QGroupBox("Columns (Kolumny / Wymiary)", tab_detailed)
        cols_vbox = QVBoxLayout(cols_group)
        cols_vbox.setContentsMargins(8, 8, 8, 8)
        cols_vbox.setSpacing(6)

        self.cols_table = QTableWidget(cols_group)
        self.cols_table.setColumnCount(2)
        self.cols_table.setHorizontalHeaderLabels(["Col Key (ID)", "Col Label (e.g. 200 nm)"])
        self.cols_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.cols_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        cols_vbox.addWidget(self.cols_table)

        cols_btn_box = QHBoxLayout()
        self.add_col_btn = PushButton("+ Add Col", cols_group, FluentIcon.ADD)
        self.add_col_btn.clicked.connect(self._on_add_col)
        self.del_col_btn = PushButton("Delete", cols_group, FluentIcon.DELETE)
        self.del_col_btn.clicked.connect(self._on_delete_col)
        self.move_col_up_btn = ToolButton(FluentIcon.LEFT_ARROW, cols_group)
        self.move_col_up_btn.clicked.connect(lambda: self._move_col(-1))
        self.move_col_down_btn = ToolButton(FluentIcon.RIGHT_ARROW, cols_group)
        self.move_col_down_btn.clicked.connect(lambda: self._move_col(1))
        cols_btn_box.addWidget(self.add_col_btn)
        cols_btn_box.addWidget(self.del_col_btn)
        cols_btn_box.addWidget(self.move_col_up_btn)
        cols_btn_box.addWidget(self.move_col_down_btn)
        cols_vbox.addLayout(cols_btn_box)

        detailed_layout.addWidget(cols_group, 1)

        self.tabs.addItem("detailedTab", "2. Individual Rows & Columns (Pojedyncze wiersze i kolumny)", onClick=lambda: self.stack.setCurrentIndex(1))
        self.stack.addWidget(tab_detailed)

        # Default to tab 0 so main dimensions & presets are immediately editable
        self.tabs.setCurrentItem("generatorTab")
        self.stack.setCurrentIndex(0)

        # Bottom Actions
        button_layout = QHBoxLayout()
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
        self.rows_count.valueChanged.connect(self._on_generator_input_changed)
        self.row_scheme.currentIndexChanged.connect(self._on_generator_input_changed)
        self.row_start.valueChanged.connect(self._on_generator_input_changed)
        self.row_label_prefix.textChanged.connect(self._on_generator_input_changed)
        self.cols_count.valueChanged.connect(self._on_generator_input_changed)
        self.col_scheme.currentIndexChanged.connect(self._on_generator_input_changed)
        self.col_labels_input.textChanged.connect(self._on_generator_input_changed)

        # Populate
        if sample is not None:
            self._populate_from_sample(sample)
        else:
            self._apply_generator_to_tables()

    def _on_generator_input_changed(self, *args: object) -> None:
        if getattr(self, "_syncing_from_sample", False):
            return
        if self._existing_sample is None or self.stack.currentIndex() == 0:
            self._apply_generator_to_tables()

    def _on_row_scheme_changed(self, index: int) -> None:
        self.row_start.setEnabled(index == 1)

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
            existing_label = (
                self._existing_sample.row_labels.get(r, "")
                if self._existing_sample
                else ""
            )
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

        raw_labels = [p.strip() for p in self.col_labels_input.text().split(",") if p.strip()]
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

    def _populate_from_sample(self, sample: Sample) -> None:
        self._syncing_from_sample = True
        try:
            self.id_input.setText(sample.sample_id)
            self.id_input.setEnabled(False)  # Keep ID stable in edit mode
            self.name_input.setText(sample.name)
            self.tags_input.setText(", ".join(sample.tags))
            self.desc_input.setPlainText(sample.description)

            self.rows_count.setValue(len(sample.rows) or 10)
            self.cols_count.setValue(len(sample.cols) or 5)

            if sample.rows and all(r.isdigit() for r in sample.rows):
                start = int(sample.rows[0])
                if start != 1:
                    self.row_scheme.setCurrentIndex(1)
                    self.row_start.setValue(start)
                    self.row_start.setEnabled(True)

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
        finally:
            self._syncing_from_sample = False

    def _on_add_row(self) -> None:
        count = self.rows_table.rowCount()
        next_key = str(count + 1)
        self.rows_table.insertRow(count)
        self.rows_table.setItem(count, 0, QTableWidgetItem(next_key))
        self.rows_table.setItem(count, 1, QTableWidgetItem(f"Row {next_key}"))
        self.rows_table.setCurrentCell(count, 1)

    def _on_delete_row(self) -> None:
        curr = self.rows_table.currentRow()
        if curr >= 0 and self.rows_table.rowCount() > 1:
            self.rows_table.removeRow(curr)

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

    def _on_delete_col(self) -> None:
        curr = self.cols_table.currentRow()
        if curr >= 0 and self.cols_table.rowCount() > 1:
            self.cols_table.removeRow(curr)

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
            updated = self._existing_sample.with_structure(
                rows=row_keys,
                row_labels=row_labels,
                cols=col_keys,
                col_labels=col_labels,
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
