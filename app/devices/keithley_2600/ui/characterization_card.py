"""Fluent UI card for Keithley sample characterization and reporting."""

from datetime import datetime, timezone
import math
import os
import re
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QSettings, QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SimpleCardWidget,
    SpinBox,
    StrongBodyLabel,
    ToolButton,
    isDarkTheme,
)

from app.devices.keithley_2600.characterization.analyzer import KeithleyCharacterizationAnalyzer
from app.devices.keithley_2600.characterization.export import KeithleyDataExporter
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    CharacterizationPoint,
    CharacterizationSweepConfig,
    ExtractedScientificParameters,
    SampleMetadata,
)
from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
from app.devices.keithley_2600.characterization.runner import CharacterizationWorker
from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.inventory.models import ActiveSampleTarget, SampleRunRecord
from app.inventory.store import InventoryStore
from app.safety.quick_controls import quick_control_safety_bounds
from app.settings.models import StationSettings
from app.ui.design_system import tokens_for
from app.ui.design_system.plot_theme import plot_theme
from app.ui.dialogs import StationMessageBox
from app.ui.widgets import LimitField, NotificationBanner

SETTINGS_SECTION = "LabControl"
KEY_LAST_SAMPLE_ID = "keithley_characterization/last_sample_id"
KEY_LAST_ROW = "keithley_characterization/last_row"
KEY_LAST_COL = "keithley_characterization/last_col"
KEY_LAST_DEVICE_LABEL = "keithley_characterization/last_device_label"
KEY_LAST_AREA = "keithley_characterization/last_area"
KEY_LAST_THICKNESS = "keithley_characterization/last_thickness"
KEY_LAST_OPERATOR = "keithley_characterization/last_operator"


class KeithleyCharacterizationCard(QWidget):
    """Integrated workspace for Keithley IV sweep, real-time plotting, and PDF reporting."""

    active_target_changed = Signal(object)  # ActiveSampleTarget
    browse_samples_requested = Signal()

    def __init__(
        self,
        controller: Any,
        settings: StationSettings,
        inventory_store: InventoryStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._settings = settings
        self._inventory_store: InventoryStore | None = inventory_store
        self._worker: CharacterizationWorker | None = None
        self._current_dataset: CharacterizationDataset | None = None
        self._current_parameters: ExtractedScientificParameters | None = None

        self._live_v_points: list[float] = []
        self._live_i_points: list[float] = []
        self._live_r_points: list[float] = []
        self._live_app_r_points: list[float] = []
        self._live_dem_points: list[float] = []
        self._live_comp_x: list[float] = []
        self._live_comp_y: list[float] = []
        self._active_plot_view: int = 0

        self._init_ui()
        self._update_limits_from_settings()
        self._update_plot_labels()
        self.refresh_samples_list()
        self._restore_saved_metadata_selection()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 8)
        main_layout.setSpacing(8)

        # 1. Safety banner for preflight messages
        self.banner = NotificationBanner()
        main_layout.addWidget(self.banner)

        # 2. Main splitter: Configuration (Left) | Live Plots & Analysis (Right)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("charSplitter")

        # --- LEFT PANEL: Settings & Sample Configuration ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 6, 0)
        left_layout.setSpacing(8)

        config_card = CardWidget()
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(12, 10, 12, 10)
        config_layout.setSpacing(6)

        config_title = StrongBodyLabel("Sample Characterization Parameters")
        config_layout.addWidget(config_title)

        form_layout = QFormLayout()
        form_layout.setSpacing(6)

        self.mode_combo = ComboBox()
        self.mode_combo.addItems(["Current Sweep (I → V)", "Voltage Sweep (V → I)"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        form_layout.addRow("Measurement mode:", self.mode_combo)

        self.channel_combo = ComboBox()
        self.channel_combo.addItems(["Channel A", "Channel B"])
        self.channel_combo.currentTextChanged.connect(self._on_channel_changed)
        form_layout.addRow("Keithley channel:", self.channel_combo)

        self.start_level_edit = LineEdit()
        self.start_level_edit.setText("-1 mA")
        self.start_level_field = self._bounded("level", self.start_level_edit)
        form_layout.addRow("Start level:", self.start_level_field)

        self.stop_level_edit = LineEdit()
        self.stop_level_edit.setText("1 mA")
        self.stop_level_field = self._bounded("level", self.stop_level_edit)
        form_layout.addRow("Stop level:", self.stop_level_field)

        self.points_spin = SpinBox()
        self.points_spin.setRange(3, 1001)
        self.points_spin.setValue(101)
        form_layout.addRow("Number of points:", self.points_spin)

        self.compliance_edit = LineEdit()
        self.compliance_edit.setText("670 mV")
        self.compliance_field = self._bounded("compliance", self.compliance_edit)
        form_layout.addRow("Compliance limit:", self.compliance_field)

        self.dwell_edit = LineEdit()
        self.dwell_edit.setText("50 ms")
        self.dwell_field = self._bounded("settle", self.dwell_edit)
        form_layout.addRow("Settling time (dwell):", self.dwell_field)

        self.sense_combo = ComboBox()
        self.sense_combo.addItems(["4-wire (Kelvin)", "2-wire"])
        form_layout.addRow("Sense mode:", self.sense_combo)

        config_layout.addLayout(form_layout)

        # Sample Metadata Section
        meta_header = QHBoxLayout()
        meta_title = StrongBodyLabel("Junction & Sample Metadata")
        meta_header.addWidget(meta_title)
        meta_header.addStretch(1)

        self.browse_samples_btn = ToolButton(FluentIcon.TILES, self)
        self.browse_samples_btn.setToolTip("Open Samples Inventory page to manage samples and device grids")
        self.browse_samples_btn.setFixedSize(28, 28)
        self.browse_samples_btn.clicked.connect(self.browse_samples_requested.emit)
        meta_header.addWidget(self.browse_samples_btn)
        config_layout.addLayout(meta_header)

        meta_form = QFormLayout()
        meta_form.setSpacing(6)

        # 1. Sample Selector (from InventoryStore)
        self.sample_combo = ComboBox(self)
        self.sample_combo.setPlaceholderText("Select Sample from Inventory…")
        self.sample_combo.currentIndexChanged.connect(self._on_sample_combo_changed)
        meta_form.addRow("Sample:", self.sample_combo)

        # 2. Device / Junction Selector (cascading for chosen sample)
        self.device_combo = ComboBox(self)
        self.device_combo.setPlaceholderText("Select Device / Junction…")
        self.device_combo.currentIndexChanged.connect(self._on_device_combo_changed)
        meta_form.addRow("Device / Junction:", self.device_combo)

        # 3. Synchronized / Editable fields
        self.sample_id_edit = LineEdit()
        self.sample_id_edit.setText("MTJ-Sample-01")
        self.sample_id_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Sample ID:", self.sample_id_edit)

        self.structure_edit = LineEdit()
        self.structure_edit.setPlaceholderText("e.g. R1:C1 · 200 nm Pillar A")
        self.structure_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Structure / chip:", self.structure_edit)

        self.area_edit = LineEdit()
        self.area_edit.setText("2.0")
        self.area_edit.setPlaceholderText("Area in µm²")
        self.area_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Junction area [µm²]:", self.area_edit)

        self.thickness_edit = LineEdit()
        self.thickness_edit.setText("1.0")
        self.thickness_edit.setPlaceholderText("Barrier in nm")
        self.thickness_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Barrier thickness [nm]:", self.thickness_edit)

        self.operator_edit = LineEdit()
        self.operator_edit.setPlaceholderText("Operator initials")
        self.operator_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Operator:", self.operator_edit)

        config_layout.addLayout(meta_form)
        left_layout.addWidget(config_card)
        left_layout.addStretch(1)

        # --- RIGHT PANEL: Live Plots & Scientific Metrics ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(8)

        # Plot 1: V(I) live
        plot_card = CardWidget()
        plot_layout = QVBoxLayout(plot_card)
        plot_layout.setContentsMargins(10, 8, 10, 8)
        plot_layout.setSpacing(4)

        plot_header = QHBoxLayout()
        plot_title = StrongBodyLabel("Real-time Measurement Curves: V(I) & R(I)")
        self.status_label = CaptionLabel("Ready for measurement")
        plot_header.addWidget(plot_title)
        plot_header.addSpacing(10)

        self.plot_view_nav = SegmentedWidget(self)
        self.plot_view_nav.addItem("iv", "V-I Curve", onClick=lambda: self._set_plot_view(0))
        self.plot_view_nav.addItem("res", "Resistance R", onClick=lambda: self._set_plot_view(1))
        self.plot_view_nav.setCurrentItem("iv")
        plot_header.addWidget(self.plot_view_nav)

        plot_header.addStretch(1)
        plot_header.addWidget(self.status_label)
        plot_layout.addLayout(plot_header)

        # pyqtgraph setup with Fluent design tokens
        theme_tokens = tokens_for("dark" if isDarkTheme() else "light")
        theme = plot_theme(theme_tokens)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground(theme.background)
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot_widget.setLabel("bottom", "Demanded level [SI]")
        self.plot_widget.setLabel("left", "Voltage response [V]")

        self.curve_iv = self.plot_widget.plot(
            pen=pg.mkPen(color="#0284c7", width=2),
            symbol="o",
            symbolSize=5,
            symbolBrush="#0284c7",
        )
        self.curve_clamped = self.plot_widget.plot(
            pen=None,
            symbol="x",
            symbolSize=8,
            symbolPen=pg.mkPen(color="#ef4444", width=2),
            symbolBrush="#ef4444",
        )
        self.compliance_line_pos = pg.InfiniteLine(angle=0, pen=pg.mkPen(color="#ef4444", style=Qt.PenStyle.DashLine, width=1.5))
        self.compliance_line_neg = pg.InfiniteLine(angle=0, pen=pg.mkPen(color="#ef4444", style=Qt.PenStyle.DashLine, width=1.5))
        self.plot_widget.addItem(self.compliance_line_pos)
        self.plot_widget.addItem(self.compliance_line_neg)

        self.curve_r_true = self.plot_widget.plot(
            pen=pg.mkPen(color="#059669", width=2),
            symbol="o",
            symbolSize=4,
            symbolBrush="#059669",
        )
        self.curve_r_app = self.plot_widget.plot(
            pen=pg.mkPen(color="#f59e0b", width=1.6, style=Qt.PenStyle.DashLine),
            symbol=None,
        )
        self.curve_r_true.setVisible(False)
        self.curve_r_app.setVisible(False)

        plot_layout.addWidget(self.plot_widget, 1)

        # Progress bar
        self.progress_bar = ProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        plot_layout.addWidget(self.progress_bar)

        right_layout.addWidget(plot_card, 2)

        # Scientific Metrics Summary Card
        summary_card = SimpleCardWidget()
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setContentsMargins(10, 8, 10, 8)
        summary_layout.setSpacing(4)

        metrics_title = StrongBodyLabel("Scientific Junction Analysis Results")
        summary_layout.addWidget(metrics_title)

        grid = QGridLayout()
        grid.setSpacing(8)

        self.metric_r0 = BodyLabel("R₀: —")
        self.metric_g0 = BodyLabel("G₀: —")
        self.metric_ra = BodyLabel("R·A: —")
        self.metric_comp = BodyLabel("Compliance: Not detected")
        self.metric_pmax = BodyLabel("P_max: —")
        self.metric_r2 = BodyLabel("Linearity R²: —")

        grid.addWidget(self.metric_r0, 0, 0)
        grid.addWidget(self.metric_g0, 0, 1)
        grid.addWidget(self.metric_ra, 0, 2)
        grid.addWidget(self.metric_comp, 1, 0)
        grid.addWidget(self.metric_pmax, 1, 1)
        grid.addWidget(self.metric_r2, 1, 2)

        summary_layout.addLayout(grid)
        right_layout.addWidget(summary_card, 1)

        left_scroll = ScrollArea()
        left_scroll.setWidget(left_widget)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.enableTransparentBackground()
        left_scroll.setMinimumWidth(440)

        splitter.addWidget(left_scroll)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        splitter.setSizes([480, 880])
        main_layout.addWidget(splitter, 1)

        # --- BOTTOM ACTION BAR ---
        actions_card = SimpleCardWidget()
        actions_layout = QHBoxLayout(actions_card)
        actions_layout.setContentsMargins(12, 6, 12, 6)
        actions_layout.setSpacing(10)

        self.start_button = PrimaryPushButton("Start Characterization", self)
        self.start_button.setIcon(FluentIcon.PLAY)
        self.start_button.clicked.connect(self._on_start_clicked)

        self.stop_button = PushButton("Stop", self)
        self.stop_button.setIcon(FluentIcon.CANCEL)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)

        self.pdf_button = PushButton("Generate PDF Report…", self)
        self.pdf_button.setIcon(FluentIcon.DOCUMENT)
        self.pdf_button.setEnabled(False)
        self.pdf_button.clicked.connect(self._on_generate_pdf_clicked)

        self.csv_button = PushButton("Export CSV…", self)
        self.csv_button.setIcon(FluentIcon.SHARE)
        self.csv_button.setEnabled(False)
        self.csv_button.clicked.connect(self._on_export_csv_clicked)

        actions_layout.addWidget(self.start_button)
        actions_layout.addWidget(self.stop_button)
        actions_layout.addStretch(1)
        actions_layout.addWidget(self.pdf_button)
        actions_layout.addWidget(self.csv_button)

        main_layout.addWidget(actions_card)

    def sizeHint(self) -> QSize:
        return QSize(800, 450)

    def minimumSizeHint(self) -> QSize:
        return QSize(400, 200)

    def _selected_channel(self) -> str:
        text = self.channel_combo.currentText()
        return "B" if "B" in text else "A"

    def _is_current_mode(self) -> bool:
        return (
            self.mode_combo.currentIndex() == 0
            or "current" in self.mode_combo.currentText().lower()
        )

    def _on_channel_changed(self) -> None:
        self.refresh_limits()
        self._update_limits_from_settings()

    def _on_mode_changed(self) -> None:
        self.refresh_limits()
        self._update_limits_from_settings()
        self._update_plot_labels()

    # -------------------------------------------------------------------------
    # Sample Inventory & Device Selection
    # -------------------------------------------------------------------------

    def set_inventory_store(self, store: InventoryStore) -> None:
        """Assign inventory store, refresh samples, and restore selection."""
        self._inventory_store = store
        self.refresh_samples_list()
        self._restore_saved_metadata_selection()

    def refresh_samples_list(self) -> None:
        """Reload samples from inventory store into the sample combobox."""
        current_id = self.selected_sample_id()
        self.sample_combo.blockSignals(True)
        self.sample_combo.clear()

        if self._inventory_store is not None:
            samples = self._inventory_store.list_samples()
            for s in samples:
                display = f"{s.name} ({s.sample_id})" if s.name and s.name != s.sample_id else s.sample_id
                self.sample_combo.addItem(display, userData=s.sample_id)

        # Add manual entry option
        self.sample_combo.addItem("(Custom / Manual)", userData="")
        self.sample_combo.blockSignals(False)

        # Reselect previous or first sample
        if current_id:
            idx = self._find_sample_index(current_id)
            if idx >= 0:
                self.sample_combo.setCurrentIndex(idx)
            else:
                self.sample_combo.setCurrentIndex(0)
        elif self.sample_combo.count() > 1:
            self.sample_combo.setCurrentIndex(0)
        else:
            self.sample_combo.setCurrentIndex(0)

        self._populate_device_combo_for_selected_sample()

    def selected_sample_id(self) -> str:
        idx = self.sample_combo.currentIndex()
        if idx < 0:
            return ""
        data = self.sample_combo.itemData(idx)
        return str(data or "")

    def selected_device_coord(self) -> tuple[str, str, str]:
        """Return (row, col, label) for currently selected device."""
        idx = self.device_combo.currentIndex()
        if idx < 0:
            return ("", "", "")
        data = self.device_combo.itemData(idx)
        if data and isinstance(data, (tuple, list)) and len(data) >= 3:
            return (str(data[0]), str(data[1]), str(data[2]))
        return ("", "", "")

    def _find_sample_index(self, sample_id: str) -> int:
        for i in range(self.sample_combo.count()):
            if self.sample_combo.itemData(i) == sample_id:
                return i
        return -1

    def _find_device_index(self, row: str, col: str) -> int:
        if not row and not col:
            return -1
        for i in range(self.device_combo.count()):
            data = self.device_combo.itemData(i)
            if data and isinstance(data, (tuple, list)) and len(data) >= 2:
                r, c = str(data[0]), str(data[1])
                if r == str(row) and c == str(col):
                    return i
        return -1

    def _populate_device_combo_for_selected_sample(self) -> None:
        """Populate device combobox with all cells/junctions from the active sample."""
        sample_id = self.selected_sample_id()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        if not sample_id or self._inventory_store is None:
            self.device_combo.addItem("(Manual coordinate)", userData=("", "", ""))
            self.device_combo.blockSignals(False)
            return

        sample = self._inventory_store.get_sample(sample_id)
        if sample is None:
            self.device_combo.addItem("(Manual coordinate)", userData=("", "", ""))
            self.device_combo.blockSignals(False)
            return

        has_devices = False
        for row in sample.rows:
            for col in sample.cols:
                has_devices = True
                label = sample.cell_label(row, col)
                state = sample.cell_state(row, col)
                coord = f"R{row}:C{col}"
                state_suffix = f" [{state}]" if state and state != "untested" else ""
                if label and label != f"R{row}C{col}":
                    display = f"{coord} — {label}{state_suffix}"
                else:
                    display = f"{coord}{state_suffix}"
                self.device_combo.addItem(display, userData=(str(row), str(col), str(label)))

        if not has_devices:
            self.device_combo.addItem("(No grid configured)", userData=("", "", ""))

        self.device_combo.blockSignals(False)

    def _on_sample_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        sample_id = self.selected_sample_id()
        if sample_id:
            self.sample_id_edit.blockSignals(True)
            self.sample_id_edit.setText(sample_id)
            self.sample_id_edit.blockSignals(False)
        self._populate_device_combo_for_selected_sample()
        if self.device_combo.count() > 0:
            self.device_combo.setCurrentIndex(0)
            self._on_device_combo_changed(0)
        else:
            self._persist_selection_to_settings(sample_id, "", "", "")

    def _on_device_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        data = self.device_combo.itemData(index)
        if not data or not isinstance(data, (tuple, list)):
            return
        row, col = str(data[0]), str(data[1])
        label = str(data[2]) if len(data) > 2 else ""
        if not row and not col:
            return

        sample_id = self.selected_sample_id()
        sample = self._inventory_store.get_sample(sample_id) if (self._inventory_store and sample_id) else None

        # Auto-update structure_edit
        struct_desc = f"R{row}:C{col}"
        if label and label != f"R{row}C{col}":
            struct_desc += f" · {label}"
        self.structure_edit.blockSignals(True)
        self.structure_edit.setText(struct_desc)
        self.structure_edit.blockSignals(False)

        # Check cell notes for area or barrier thickness hints
        if sample is not None:
            notes = sample.cell_notes(row, col)
            self._try_parse_and_fill_cell_hints(notes, label)

        # Save to QSettings and sync active target
        self._persist_selection_to_settings(sample_id, row, col, label)

    def _try_parse_and_fill_cell_hints(self, notes: str, label: str) -> None:
        combined = f"{label} {notes}"
        area_match = re.search(r"(?i)(?:area|powierzchnia)[:=\s]+([0-9.]+)", combined)
        if area_match:
            try:
                val = float(area_match.group(1))
                if val > 0:
                    self.area_edit.setText(str(val))
            except ValueError:
                pass

        thick_match = re.search(r"(?i)(?:thickness|grubość|barrier|bariera)[:=\s]+([0-9.]+)", combined)
        if thick_match:
            try:
                val = float(thick_match.group(1))
                if val > 0:
                    self.thickness_edit.setText(str(val))
            except ValueError:
                pass

    def _on_metadata_field_changed(self) -> None:
        settings = QSettings(SETTINGS_SECTION, SETTINGS_SECTION)
        settings.setValue(KEY_LAST_AREA, self.area_edit.text().strip())
        settings.setValue(KEY_LAST_THICKNESS, self.thickness_edit.text().strip())
        settings.setValue(KEY_LAST_OPERATOR, self.operator_edit.text().strip())

    def _persist_selection_to_settings(
        self, sample_id: str, row: str, col: str, device_label: str
    ) -> None:
        settings = QSettings(SETTINGS_SECTION, SETTINGS_SECTION)
        settings.setValue(KEY_LAST_SAMPLE_ID, sample_id)
        settings.setValue(KEY_LAST_ROW, row)
        settings.setValue(KEY_LAST_COL, col)
        settings.setValue(KEY_LAST_DEVICE_LABEL, device_label)
        settings.setValue(KEY_LAST_AREA, self.area_edit.text().strip())
        settings.setValue(KEY_LAST_THICKNESS, self.thickness_edit.text().strip())
        settings.setValue(KEY_LAST_OPERATOR, self.operator_edit.text().strip())

        # Also update active target in InventoryStore
        if self._inventory_store is not None and sample_id and (row or col):
            sample = self._inventory_store.get_sample(sample_id)
            sample_name = sample.name if sample else sample_id
            target = ActiveSampleTarget(
                sample_id=sample_id,
                sample_name=sample_name,
                row=row or None,
                col=col or None,
                device_label=device_label or None,
                notes=self.structure_edit.text().strip(),
            )
            self._inventory_store.set_active_target(target)
            self.active_target_changed.emit(target)

    def _restore_saved_metadata_selection(self) -> None:
        """Restore last chosen sample, device, operator, and specs from QSettings or active target."""
        settings = QSettings(SETTINGS_SECTION, SETTINGS_SECTION)

        # 1. Restore operator, area, thickness
        saved_operator = str(settings.value(KEY_LAST_OPERATOR, "") or "")
        if saved_operator:
            self.operator_edit.setText(saved_operator)

        saved_area = str(settings.value(KEY_LAST_AREA, "") or "")
        if saved_area:
            self.area_edit.setText(saved_area)

        saved_thickness = str(settings.value(KEY_LAST_THICKNESS, "") or "")
        if saved_thickness:
            self.thickness_edit.setText(saved_thickness)

        # 2. Determine target sample & coordinate:
        target_sample_id = ""
        target_row = ""
        target_col = ""

        if self._inventory_store is not None:
            active_target = self._inventory_store.get_active_target()
            if active_target.is_active and active_target.sample_id:
                target_sample_id = active_target.sample_id
                target_row = active_target.row or ""
                target_col = active_target.col or ""

        if not target_sample_id:
            target_sample_id = str(settings.value(KEY_LAST_SAMPLE_ID, "") or "")
            target_row = str(settings.value(KEY_LAST_ROW, "") or "")
            target_col = str(settings.value(KEY_LAST_COL, "") or "")

        if not target_sample_id:
            return

        idx = self._find_sample_index(target_sample_id)
        if idx >= 0:
            self.sample_combo.blockSignals(True)
            self.sample_combo.setCurrentIndex(idx)
            self.sample_combo.blockSignals(False)

            self.sample_id_edit.blockSignals(True)
            self.sample_id_edit.setText(target_sample_id)
            self.sample_id_edit.blockSignals(False)

            self._populate_device_combo_for_selected_sample()

            dev_idx = self._find_device_index(target_row, target_col)
            if dev_idx >= 0:
                self.device_combo.blockSignals(True)
                self.device_combo.setCurrentIndex(dev_idx)
                self.device_combo.blockSignals(False)
                self._on_device_combo_changed(dev_idx)
            elif self.device_combo.count() > 0:
                self.device_combo.setCurrentIndex(0)
                self._on_device_combo_changed(0)

    def set_active_sample_target(self, target: object) -> None:
        """Update selected sample and device target from global active target."""
        if not hasattr(target, "is_active") or not getattr(target, "is_active", False):
            return

        sample_id = getattr(target, "sample_id", None) or ""
        row = getattr(target, "row", None) or ""
        col = getattr(target, "col", None) or ""

        if not sample_id:
            return

        idx = self._find_sample_index(sample_id)
        if idx >= 0 and self.sample_combo.currentIndex() != idx:
            self.sample_combo.blockSignals(True)
            self.sample_combo.setCurrentIndex(idx)
            self.sample_combo.blockSignals(False)

            self.sample_id_edit.blockSignals(True)
            self.sample_id_edit.setText(sample_id)
            self.sample_id_edit.blockSignals(False)

            self._populate_device_combo_for_selected_sample()

        dev_idx = self._find_device_index(str(row), str(col))
        if dev_idx >= 0 and self.device_combo.currentIndex() != dev_idx:
            self.device_combo.blockSignals(True)
            self.device_combo.setCurrentIndex(dev_idx)
            self.device_combo.blockSignals(False)
            self._on_device_combo_changed(dev_idx)

    def _bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self.limit_values(key), range_mode=True)
        field.setProperty("limitKey", key)
        field.setProperty("characterizationField", True)
        field.edit_button.setFixedWidth(78)
        field.edit_button.setFixedHeight(30)
        field.edit_button.setIcon(FluentIcon.EDIT)
        field.edit_button.setText("Edit")
        return field

    def limit_values(self, key: str) -> tuple[object, object]:
        ch = self._selected_channel()
        is_current = self._is_current_mode()
        mode = "current" if is_current else "voltage"
        try:
            limits = self._settings.keithley.safety.channels[ch].lab_limits
        except Exception:
            return "NOT SET", "NOT SET"

        if key == "level":
            try:
                bounds = quick_control_safety_bounds(self._settings)
                bound = bounds.get(f"keithley.{ch}.{mode}")
                if bound is not None:
                    return bound.minimum_text, bound.maximum_text
            except Exception:
                pass
            configured = limits.source_current if is_current else limits.source_voltage
            return configured.min, configured.max
        if key == "compliance":
            value = limits.voltage_compliance if is_current else limits.current_compliance
            if not value.enabled:
                return "HARDWARE", "HARDWARE"
            return value.min, value.max
        if key == "settle":
            if not limits.point_settle_time.enabled:
                return "DISABLED", "DISABLED"
            return limits.point_settle_time.min, limits.point_settle_time.max
        return "NOT SET", "NOT SET"

    def refresh_limits(self, *_args: object) -> None:
        for field in (
            getattr(self, "start_level_field", None),
            getattr(self, "stop_level_field", None),
            getattr(self, "compliance_field", None),
            getattr(self, "dwell_field", None),
        ):
            if field is not None:
                key = str(field.property("limitKey"))
                field.set_limits(*self.limit_values(key))

    def _set_plot_view(self, view_idx: int) -> None:
        self._active_plot_view = view_idx
        if view_idx == 0:
            self.curve_iv.setVisible(True)
            self.curve_clamped.setVisible(True)
            self.compliance_line_pos.setVisible(True)
            self.compliance_line_neg.setVisible(True)
            self.curve_r_true.setVisible(False)
            self.curve_r_app.setVisible(False)
            self._update_plot_labels()
        else:
            self.curve_iv.setVisible(False)
            self.curve_clamped.setVisible(False)
            self.compliance_line_pos.setVisible(False)
            self.compliance_line_neg.setVisible(False)
            self.curve_r_true.setVisible(True)
            self.curve_r_app.setVisible(True)
            is_current = self._is_current_mode()
            self.plot_widget.setLabel("bottom", "Demanded Current [A]" if is_current else "Demanded Voltage [V]")
            self.plot_widget.setLabel("left", "Resistance R [Ω]")

    def _update_plot_labels(self) -> None:
        is_current = self._is_current_mode()
        if self._active_plot_view == 1:
            self.plot_widget.setLabel("bottom", "Demanded Current [A]" if is_current else "Demanded Voltage [V]")
            self.plot_widget.setLabel("left", "Resistance R [Ω]")
            return
        if is_current:
            self.plot_widget.setLabel("bottom", "Demanded Current [A]")
            self.plot_widget.setLabel("left", "Voltage Response [V]")
        else:
            self.plot_widget.setLabel("bottom", "Demanded Voltage [V]")
            self.plot_widget.setLabel("left", "Current Response [A]")

    def _update_limits_from_settings(self) -> None:
        ch = self._selected_channel()
        try:
            channel_settings = self._settings.keithley.safety.channels[ch]
            limits = channel_settings.lab_limits
            # Autofill compliance and levels based on mode
            if self._is_current_mode():
                self.compliance_edit.setText(limits.voltage_compliance.max)
                self.start_level_edit.setText(limits.source_current.min)
                self.stop_level_edit.setText(limits.source_current.max)
            else:
                self.compliance_edit.setText(limits.current_compliance.max)
                self.start_level_edit.setText(limits.source_voltage.min)
                self.stop_level_edit.setText(limits.source_voltage.max)
        except Exception:
            pass
        self.refresh_limits()

    def _build_config(self) -> CharacterizationSweepConfig:
        ch = self._selected_channel()
        is_current = self._is_current_mode()
        mode = "current" if is_current else "voltage"

        dim_sweep = DIMENSION_CURRENT if is_current else DIMENSION_VOLTAGE
        dim_comp = DIMENSION_VOLTAGE if is_current else DIMENSION_CURRENT

        start_si = parse_quantity(self.start_level_edit.text(), dim_sweep).si_value
        stop_si = parse_quantity(self.stop_level_edit.text(), dim_sweep).si_value
        comp_si = abs(parse_quantity(self.compliance_edit.text(), dim_comp).si_value)
        dwell_si = max(0.0, parse_quantity(self.dwell_edit.text(), DIMENSION_TIME).si_value)

        if comp_si <= 0:
            raise ValueError("Compliance limit must be greater than zero.")
        if abs(stop_si - start_si) < 1e-15:
            raise ValueError("Start and stop levels cannot be identical.")

        area_val: float | None = None
        area_text = self.area_edit.text().strip()
        if area_text:
            try:
                area_val = float(area_text)
            except ValueError:
                pass

        thick_val = 1.0
        thick_text = self.thickness_edit.text().strip()
        if thick_text:
            try:
                thick_val = float(thick_text)
            except ValueError:
                pass

        sense_mode = "4wire" if "4" in self.sense_combo.currentText() else "2wire"

        metadata = SampleMetadata(
            sample_id=self.sample_id_edit.text().strip() or "Sample-1",
            structure_name=self.structure_edit.text().strip(),
            operator=self.operator_edit.text().strip(),
            junction_area_um2=area_val,
            nominal_barrier_thickness_nm=thick_val,
        )

        return CharacterizationSweepConfig(
            channel=ch,
            mode=mode,
            start_level_si=start_si,
            stop_level_si=stop_si,
            points_count=self.points_spin.value(),
            compliance_si=comp_si,
            dwell_time_s=dwell_si,
            sense_mode=sense_mode,
            metadata=metadata,
        )

    @Slot()
    def _on_start_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        try:
            config = self._build_config()
        except Exception as exc:
            self.banner.show_message(f"Invalid input parameters: {exc}")
            return

        # Preflight safety check
        try:
            from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
            KeithleyCharacterizationRunner.validate_preflight(config, self._settings)
        except SafetyViolation as exc:
            self.banner.show_message(f"Station safety preflight rejection: {exc}")
            return

        # Reset plots and data
        self._live_v_points.clear()
        self._live_i_points.clear()
        self._live_r_points.clear()
        self._live_app_r_points.clear()
        self._live_dem_points.clear()
        self._live_comp_x.clear()
        self._live_comp_y.clear()
        self.curve_iv.setData([], [])
        self.curve_clamped.setData([], [])
        self.curve_r_true.setData([], [])
        self.curve_r_app.setData([], [])
        self.progress_bar.setValue(0)
        self.status_label.setText("Measurement in progress...")

        self._update_plot_labels()
        comp_val = config.compliance_si
        self.compliance_line_pos.setValue(comp_val)
        self.compliance_line_neg.setValue(-comp_val)

        try:
            device_proxy = self._controller.adapter_for_run()
        except Exception as exc:
            self.banner.show_message(f"Keithley instrument unavailable: {exc}")
            return

        if hasattr(device_proxy, "connected") and not device_proxy.connected:
            self.banner.show_message("Keithley instrument is not connected. Connect the device before starting measurement.")
            return

        self._worker = CharacterizationWorker(
            device=device_proxy,
            config=config,
            settings=self._settings,
            parent=self,
        )
        self._worker.point_acquired.connect(self._on_point_acquired)
        self._worker.progress_changed.connect(self._on_progress_changed)
        self._worker.compliance_event.connect(self._on_compliance_event)
        self._worker.finished_dataset.connect(self._on_sweep_finished)
        self._worker.failed.connect(self._on_sweep_failed)

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.pdf_button.setEnabled(False)
        self.csv_button.setEnabled(False)

        self._worker.start()

    @Slot()
    def _on_stop_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText("Stopping and ramping down to zero...")
            self._worker.request_stop()

    @Slot(object)
    def _on_point_acquired(self, point: CharacterizationPoint) -> None:
        is_current = self._worker is None or self._worker._config.mode == "current"
        self._live_dem_points.append(point.demanded_si)
        if math.isfinite(point.true_resistance_ohm):
            self._live_r_points.append(point.true_resistance_ohm)
        else:
            self._live_r_points.append(self._live_r_points[-1] if self._live_r_points else 0.0)
        self._live_app_r_points.append(point.apparent_resistance_ohm if math.isfinite(point.apparent_resistance_ohm) else 0.0)

        if is_current:
            self._live_i_points.append(point.demanded_si)
            self._live_v_points.append(point.measured_voltage_v)
            self.curve_iv.setData(self._live_i_points, self._live_v_points)
            if point.compliance_active:
                self._live_comp_x.append(point.demanded_si)
                self._live_comp_y.append(point.measured_voltage_v)
                self.curve_clamped.setData(self._live_comp_x, self._live_comp_y)
        else:
            self._live_v_points.append(point.demanded_si)
            self._live_i_points.append(point.measured_current_a)
            self.curve_iv.setData(self._live_v_points, self._live_i_points)
            if point.compliance_active:
                self._live_comp_x.append(point.demanded_si)
                self._live_comp_y.append(point.measured_current_a)
                self.curve_clamped.setData(self._live_comp_x, self._live_comp_y)

        self.curve_r_true.setData(self._live_dem_points, self._live_r_points)
        self.curve_r_app.setData(self._live_dem_points, self._live_app_r_points)

    @Slot(int, int)
    def _on_progress_changed(self, current: int, total: int) -> None:
        pct = int((current / total) * 100)
        self.progress_bar.setValue(pct)
        self.status_label.setText(f"Point {current} of {total} ({pct}%)")

    @Slot(str)
    def _on_compliance_event(self, msg: str) -> None:
        self.metric_comp.setText("Compliance: ACTIVE (clamping)")
        self.metric_comp.setStyleSheet("color: #ef4444; font-weight: bold;")

    @Slot(object)
    def _on_sweep_finished(self, dataset: CharacterizationDataset) -> None:
        self._current_dataset = dataset
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.pdf_button.setEnabled(True)
        self.csv_button.setEnabled(True)
        self.status_label.setText("Measurement completed successfully")

        # Run scientific analysis
        params = KeithleyCharacterizationAnalyzer.analyze(dataset)
        self._current_parameters = params

        # Update metrics cards
        r0 = params.zero_bias_resistance_ohm
        g0 = params.zero_bias_conductance_s
        self.metric_r0.setText(f"R₀: {r0:.1f} Ω" if math.isfinite(r0) else "R₀: —")
        self.metric_g0.setText(f"G₀: {g0 * 1e3:.3f} mS" if math.isfinite(g0) else "G₀: —")
        if params.ra_product_ohm_um2 is not None:
            self.metric_ra.setText(f"R·A: {params.ra_product_ohm_um2:.1f} Ω·μm²")
        else:
            self.metric_ra.setText("R·A: no area specified")

        if params.compliance_detected and params.compliance_onset_point:
            ci, cv = params.compliance_onset_point
            if dataset.config.mode == "current":
                self.metric_comp.setText(f"Compliance: Onset |I|={abs(ci)*1e3:.2f} mA ({params.clamped_points_fraction*100:.0f}% saturation)")
            else:
                self.metric_comp.setText(f"Compliance: Onset |V|={abs(cv)*1e3:.1f} mV ({params.clamped_points_fraction*100:.0f}% saturation)")
            self.metric_comp.setStyleSheet("color: #ef4444; font-weight: bold;")
        else:
            self.metric_comp.setText("Compliance: None (linear ohmic range)")
            self.metric_comp.setStyleSheet("color: #059669; font-weight: bold;")

        self.metric_pmax.setText(f"P_max: {params.max_power_dissipated_w * 1e3:.2f} mW")
        self.metric_r2.setText(f"Linearity R²: {params.linearity_r2:.4f}")

        # Record measurement in inventory if a registered sample and cell are active
        sample_id = self.selected_sample_id()
        row, col, label = self.selected_device_coord()
        if self._inventory_store is not None and sample_id and (row or col):
            try:
                sample = self._inventory_store.get_sample(sample_id)
                s_name = sample.name if sample else sample_id
                rec = SampleRunRecord(
                    sample_id=sample_id,
                    sample_name=s_name,
                    row=str(row),
                    col=str(col),
                    device_label=str(label or f"R{row}:C{col}"),
                    run_path="",
                    run_sha256="",
                    created_at_utc=getattr(dataset, "completed_at_iso", "") or datetime.now(timezone.utc).isoformat(),
                    status="completed",
                    point_count=len(dataset.points),
                    spectrum_count=0,
                    recipe_name=f"Keithley IV Characterization ({dataset.config.mode})",
                    notes=f"R₀={r0:.1f} Ω, G₀={g0*1e3:.3f} mS, Linearity R²={params.linearity_r2:.4f}",
                )
                self._inventory_store.record_run(rec)
                self._populate_device_combo_for_selected_sample()
                new_idx = self._find_device_index(str(row), str(col))
                if new_idx >= 0:
                    self.device_combo.blockSignals(True)
                    self.device_combo.setCurrentIndex(new_idx)
                    self.device_combo.blockSignals(False)
            except Exception:
                pass

    @Slot(str)
    def _on_sweep_failed(self, error_msg: str) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText(f"Error: {error_msg}")
        self.banner.show_message(f"Characterization execution failed: {error_msg}")

    @Slot()
    def _on_generate_pdf_clicked(self) -> None:
        if self._current_dataset is None or self._current_parameters is None:
            return

        default_name = f"Report_{self._current_dataset.config.metadata.sample_id}.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Sample Characterization PDF Report",
            default_name,
            "PDF Document (*.pdf)",
        )
        if not path:
            return

        try:
            res_path = KeithleyPdfReportGenerator.generate(
                self._current_dataset,
                self._current_parameters,
                path,
            )
            StationMessageBox.information(
                self,
                "PDF Report Generated",
                f"Laboratory report generated successfully:\n{res_path}\n\nDo you want to open it now?",
            )
            # Try opening in default viewer on Windows
            if os.name == "nt":
                try:
                    os.startfile(str(res_path))
                except Exception:
                    pass
        except Exception as exc:
            StationMessageBox.critical(self, "PDF Generation Error", f"Failed to generate report: {exc}")

    @Slot()
    def _on_export_csv_clicked(self) -> None:
        if self._current_dataset is None:
            return

        default_name = f"Data_{self._current_dataset.config.metadata.sample_id}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Raw Measurement Data to CSV",
            default_name,
            "CSV File (*.csv)",
        )
        if not path:
            return

        try:
            res_path = KeithleyDataExporter.export_csv(self._current_dataset, path)
            StationMessageBox.information(
                self,
                "Export Completed",
                f"Measurement data successfully exported:\n{res_path}",
            )
        except Exception as exc:
            StationMessageBox.critical(self, "CSV Export Error", f"Failed to export data: {exc}")

    def set_settings(self, settings: StationSettings) -> None:
        """Update station settings and refresh limit fields."""
        self._settings = settings
        self._update_limits_from_settings()
        for field in (
            getattr(self, "start_level_field", None),
            getattr(self, "stop_level_field", None),
            getattr(self, "compliance_field", None),
            getattr(self, "dwell_field", None),
        ):
            if field is not None:
                field.validate_and_clamp()

    def closeEvent(self, event) -> None:
        """Safely terminate background acquisition worker on card close."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(2000)
        super().closeEvent(event)
