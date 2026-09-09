"""Fluent UI card for Keithley sample characterization and reporting."""

from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re
from collections.abc import Callable
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QSettings, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QSplitter,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    CardWidget,
    ComboBox,
    FluentIcon,
    FlowLayout,
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
from app.devices.keithley_2600.characterization.report_paths import report_path, create_run_directory
from app.devices.keithley_2600.characterization.export import KeithleyDataExporter
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    CharacterizationPoint,
    CharacterizationSweepConfig,
    ExtractedScientificParameters,
    SampleMetadata,
)
from app.devices.keithley_2600.characterization.runner import CharacterizationWorker
from app.devices.keithley_2600 import KeithleySourceRequest
from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
    parse_quantity,
)
from app.inventory.models import ActiveSampleTarget, SampleRunRecord
from app.inventory.store import InventoryStore
from app.safety.quick_controls import quick_control_safety_bounds
from app.settings.models import StationSettings
from app.storage.naming import sanitize_run_file_stem
from app.ui.design_system import tokens_for
from app.ui.design_system.plot_theme import plot_theme
from app.ui.dialogs import StationMessageBox
from app.ui.widgets import LimitField, NotificationBanner

SETTINGS_SECTION = "LabControl"
KEY_LAST_SAMPLE_ID = "keithley_characterization/last_sample_id"
KEY_LAST_ROW = "keithley_characterization/last_row"
KEY_LAST_COL = "keithley_characterization/last_col"
KEY_LAST_DEVICE_LABEL = "keithley_characterization/last_device_label"
KEY_LAST_DIAMETER = "keithley_characterization/last_diameter"
KEY_LAST_AREA = "keithley_characterization/last_area"
KEY_LAST_THICKNESS = "keithley_characterization/last_thickness"
KEY_LAST_OPERATOR = "keithley_characterization/last_operator"
KEY_SWEEP_DRAFTS = "keithley_characterization/operator_drafts_v1"

INL_PILLAR_PRESETS: list[tuple[str, float]] = [
    ("Custom / manual", 0.0),
    ("1000 nm (P10 · 1.0 µm)", 1000.0),
    ("800 nm (P9 · 0.8 µm)", 800.0),
    ("600 nm (P8 · 0.6 µm)", 600.0),
    ("400 nm (P7 · 0.4 µm)", 400.0),
    ("350 nm (P6 · 0.35 µm)", 350.0),
    ("300 nm (P5 / P4 · 0.3 µm)", 300.0),
    ("250 nm (P3 · 0.25 µm)", 250.0),
    ("200 nm (P2 · 0.2 µm)", 200.0),
    ("100 nm (P1 · 0.1 µm)", 100.0),
]

TARGET_RA_PRODUCT_OHM_UM2 = 8.0  # From INL sample specifications: RA = 8 Ω·µm²
ESTIMATED_LEAD_RESISTANCE_OHM = 25.0  # Series / lead resistance typical for bottom/top contacts


class CompactComboBox(ComboBox):
    """Fluent ComboBox that caps horizontal sizeHint so long item text does not stretch form layouts."""

    def sizeHint(self) -> QSize:
        sh = super().sizeHint()
        return QSize(min(sh.width(), 260), sh.height())


class KeithleyCharacterizationCard(QWidget):
    """Integrated workspace for Keithley IV sweep, real-time plotting, and PDF reporting."""

    active_target_changed = Signal(object)  # ActiveSampleTarget
    browse_samples_requested = Signal()
    measurement_saved = Signal(str)  # sample_id
    field_policies_changed = Signal(object)

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
        self._field_worker = None
        self._field_lease = None
        self._field_recovery_worker = None
        self._field_report_worker = None
        self._field_report_directory = None
        self._stored_field_series = None
        self._displayed_field_dataset = None
        self._field_overlay_items = []
        self._field_overlay_legend = None
        self._current_dataset: CharacterizationDataset | None = None
        self._current_parameters: ExtractedScientificParameters | None = None
        self._current_csv_path: Path | None = None
        self._current_pdf_path: Path | None = None
        self._pending_single_report = None
        self._run_inventory_target: tuple[str, str, str, str] | None = None
        self._source_request_provider: Callable[
            [str, str, float | None], KeithleySourceRequest
        ] | None = None
        self._compliance_policy_provider: Callable[[str], str] | None = None
        self._compliance_policy_transition_provider: Callable[
            [str, str, bool, Callable[[str], None], Callable[[str], None]], bool
        ] | None = None

        # A characterization sweep may temporarily switch a channel from the
        # normal ``warn_clamp`` (or legacy ``skip``) response to ``stop``.  The
        # transition is deliberately kept in the UI state machine so that no
        # worker can energize the instrument before the adapter has confirmed
        # the requested policy, and so that restoration happens after the
        # runner's OUTPUT-OFF finally block.
        self._temporary_policy_channel: str | None = None
        self._temporary_policy_original: str | None = None
        self._temporary_policy_phase = "idle"
        self._pending_start_config: CharacterizationSweepConfig | None = None
        self._pending_start_device: Any | None = None

        self._live_v_points: list[float] = []
        self._live_i_points: list[float] = []
        self._live_r_points: list[float] = []
        self._live_app_r_points: list[float] = []
        self._live_dem_points: list[float] = []
        self._live_comp_x: list[float] = []
        self._live_comp_y: list[float] = []
        self._active_plot_view: int = 0
        self._syncing_geometry: bool = False
        # Sweep drafts belong to a channel and source dimension. Hardware
        # settings and bounds still come exclusively from that channel's card.
        self._draft_channel = "A"
        self._draft_mode = 0
        self._channel_modes: dict[str, int] = {"A": 0, "B": 0}
        self._sweep_drafts: dict[tuple[str, int], tuple[str, str, int]] = {}

        self._init_ui()
        self._update_limits_from_settings()
        self._update_plot_labels()
        self.refresh_samples_list()
        self._restore_saved_metadata_selection()
        self._restore_operator_drafts()
        for control in (self.start_level_edit, self.stop_level_edit, self.points_spin,
                        *self.field_panel.draft_text_controls().values(),
                        self.field_panel.interval_points, self.field_panel.analysis_reference):
            control.editingFinished.connect(self._save_operator_drafts)
        for control in (self.field_panel.enabled_box, self.field_panel.continue_a):
            control.toggled.connect(self._save_operator_drafts)
        self.channel_combo.currentIndexChanged.connect(self._save_operator_drafts)
        self.mode_combo.currentIndexChanged.connect(self._save_operator_drafts)
        self.field_panel.input_mode.currentIndexChanged.connect(self._save_operator_drafts)
        self.field_panel.validation_requested.connect(self._refresh_field_validation)
        self.start_level_edit.textChanged.connect(self._refresh_field_validation)
        self.stop_level_edit.textChanged.connect(self._refresh_field_validation)
        self.points_spin.valueChanged.connect(self._refresh_field_validation)
        self._refresh_field_validation()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 8)
        main_layout.setSpacing(8)

        # 1. Safety banner for preflight messages
        self.banner = NotificationBanner()
        main_layout.addWidget(self.banner)

        # 2. Main splitter: Configuration (Left) | Live Plots & Analysis (Right)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self._workspace_splitter = splitter
        splitter.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
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

        self.mode_combo = CompactComboBox()
        self.mode_combo.addItems(["Current Sweep (I → V)", "Voltage Sweep (V → I)"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        form_layout.addRow("Sweep mode:", self.mode_combo)

        self.channel_combo = CompactComboBox()
        self.channel_combo.addItems(["Channel A", "Channel B"])
        self.channel_combo.currentTextChanged.connect(self._on_channel_changed)
        form_layout.addRow("Channel:", self.channel_combo)

        self.start_level_edit = LineEdit()
        self.start_level_edit.setText("-100 uA")
        self.start_level_field = self._bounded("level", self.start_level_edit)
        form_layout.addRow("Start level:", self.start_level_field)

        self.stop_level_edit = LineEdit()
        self.stop_level_edit.setText("100 uA")
        self.stop_level_field = self._bounded("level", self.stop_level_edit)
        form_layout.addRow("Stop level:", self.stop_level_field)

        self.points_spin = SpinBox()
        self.points_spin.setRange(3, 1001)
        self.points_spin.setValue(101)
        form_layout.addRow("Points:", self.points_spin)

        self.compliance_edit = LineEdit()
        self.compliance_edit.setText("500 mV")
        self.compliance_edit.setReadOnly(True)
        self.compliance_field = self._bounded("compliance", self.compliance_edit)
        self.compliance_field.edit_button.hide()
        form_layout.addRow("Compliance (Keithley card):", self.compliance_field)

        self.dwell_edit = LineEdit()
        self.dwell_edit.setText("50 ms")
        self.dwell_edit.setReadOnly(True)
        self.dwell_field = self._bounded("settle", self.dwell_edit)
        self.dwell_field.edit_button.hide()
        form_layout.addRow("Settling time (Keithley card):", self.dwell_field)

        config_layout.addLayout(form_layout)

        self.shared_configuration_label = CaptionLabel(
            "Source and measurement settings are inherited from the Keithley card."
        )
        self.shared_configuration_label.setObjectName("characterizationSharedConfiguration")
        self.shared_configuration_label.setWordWrap(True)
        config_layout.addWidget(self.shared_configuration_label)

        self.sense_warning_label = CaptionLabel(
            "⚠️ 4-wire (Kelvin) mode is enabled in Settings for this channel. "
            "Ensure physical Sense HI and Sense LO leads are connected to the DUT. "
            "Floating sense leads will cause the SMU to output full rail voltage (~20–40 V) and destroy delicate MTJ tunnel junctions!"
        )
        self.sense_warning_label.setWordWrap(True)
        self.sense_warning_label.setStyleSheet("color: #dc2626; font-weight: 500;")
        self.sense_warning_label.hide()
        config_layout.addWidget(self.sense_warning_label)

        from app.devices.keithley_2600.ui.field_series_panel import FieldSeriesPanel
        self.field_panel = FieldSeriesPanel(self)
        self.field_panel.refresh_bounds(self._settings)
        self._sync_field_series_availability()
        config_layout.addWidget(self.field_panel)

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
        self.sample_combo = CompactComboBox(self)
        self.sample_combo.setPlaceholderText("Select Sample from Inventory…")
        self.sample_combo.currentIndexChanged.connect(self._on_sample_combo_changed)
        meta_form.addRow("Sample:", self.sample_combo)

        # 2. Device / Junction Selector (cascading for chosen sample)
        self.device_combo = CompactComboBox(self)
        self.device_combo.setPlaceholderText("Select Device / Junction…")
        self.device_combo.currentIndexChanged.connect(self._on_device_combo_changed)
        meta_form.addRow("Device / cell:", self.device_combo)

        # 3. Synchronized / Editable fields
        self.sample_id_edit = LineEdit()
        self.sample_id_edit.setText("MTJ-Sample-01")
        self.sample_id_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Sample ID:", self.sample_id_edit)

        self.structure_edit = LineEdit()
        self.structure_edit.setPlaceholderText("e.g. R1:C1 · 200 nm Pillar A")
        self.structure_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Structure:", self.structure_edit)

        # Diameter row with LineEdit and Preset ComboBox
        diameter_row = QHBoxLayout()
        diameter_row.setContentsMargins(0, 0, 0, 0)
        diameter_row.setSpacing(6)

        self.diameter_edit = LineEdit()
        self.diameter_edit.setText("1000 nm")
        self.diameter_edit.setPlaceholderText("e.g. 600 nm or 0.6 um")
        self.diameter_edit.textChanged.connect(self._on_diameter_changed)
        self.diameter_edit.textChanged.connect(self._on_metadata_field_changed)
        diameter_row.addWidget(self.diameter_edit, 1)

        self.diameter_preset_combo = CompactComboBox(self)
        for label, d_val in INL_PILLAR_PRESETS:
            self.diameter_preset_combo.addItem(label, userData=d_val)
        self.diameter_preset_combo.currentIndexChanged.connect(self._on_diameter_preset_changed)
        diameter_row.addWidget(self.diameter_preset_combo, 1)
        meta_form.addRow("Pillar diameter:", diameter_row)

        # Junction area with live expected resistance calculation
        area_layout = QVBoxLayout()
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_layout.setSpacing(2)

        self.area_edit = LineEdit()
        self.area_edit.setText("0.7854")
        self.area_edit.setPlaceholderText("Area in µm²")
        self.area_edit.textChanged.connect(self._on_area_changed)
        self.area_edit.textChanged.connect(self._on_metadata_field_changed)
        area_layout.addWidget(self.area_edit)

        self.expected_resistance_label = CaptionLabel(self)
        self.expected_resistance_label.setWordWrap(True)
        self.expected_resistance_label.setStyleSheet("color: #0284c7; font-weight: 500;")
        area_layout.addWidget(self.expected_resistance_label)
        meta_form.addRow("Area [µm²]:", area_layout)

        self._update_expected_resistance(0.7854)
        self._sync_preset_combo_to_diameter(1000.0)

        self.thickness_edit = LineEdit()
        self.thickness_edit.setText("0.85")
        self.thickness_edit.setPlaceholderText("Barrier in nm (MgO)")
        self.thickness_edit.setToolTip("Target RA = 8 Ω·µm² corresponds to ~0.80 - 0.85 nm MgO tunnel barrier")
        self.thickness_edit.textChanged.connect(self._on_metadata_field_changed)
        meta_form.addRow("Barrier [nm]:", self.thickness_edit)

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
        self.plot_view_nav.currentItemChanged.connect(self._on_plot_view_route_changed)
        self.plot_view_nav.setCurrentItem("iv")
        plot_header.addWidget(self.plot_view_nav)

        plot_header.addStretch(1)
        plot_header.addWidget(self.status_label)
        plot_layout.addLayout(plot_header)
        series_row = QHBoxLayout()
        self.open_series_button = PushButton("Open saved field series…", self)
        self.open_series_button.clicked.connect(self._choose_saved_field_series)
        self.field_curve_combo = CompactComboBox(self)
        self.field_curve_combo.setPlaceholderText("Select a field curve")
        self.field_curve_combo.currentIndexChanged.connect(self._select_field_curve)
        series_row.addWidget(self.open_series_button)
        series_row.addWidget(self.field_curve_combo, 1)
        self.field_overlay_check = CheckBox("Overlay curves", self)
        self.field_overlay_check.setToolTip("Compare valid points before compliance; field targets skipped on B compliance are excluded.")
        self.field_overlay_check.toggled.connect(lambda _: self._set_plot_view(self._active_plot_view))
        overlay_row = QHBoxLayout()
        overlay_row.addWidget(self.field_overlay_check)
        self.field_overlay_group = CompactComboBox(self)
        self.field_overlay_group.setToolTip("Overlay up to 8 original field-list positions at a time")
        self.field_overlay_group.hide()
        self.field_overlay_group.currentIndexChanged.connect(lambda _: self._refresh_field_overlays())
        overlay_row.addWidget(self.field_overlay_group)
        self.annotate_curve_button = PushButton("Annotate curve", self)
        self.annotate_curve_button.clicked.connect(self._annotate_saved_curve)
        overlay_row.addWidget(self.annotate_curve_button)
        overlay_row.addStretch(1)
        plot_layout.addLayout(series_row)
        plot_layout.addLayout(overlay_row)
        self.saved_series_context = CaptionLabel(self)
        self.saved_series_context.setWordWrap(True)
        self.saved_series_context.hide()
        plot_layout.addWidget(self.saved_series_context)

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
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_scroll.enableTransparentBackground()
        left_scroll.setMinimumWidth(560)

        splitter.addWidget(left_scroll)
        splitter.addWidget(right_widget)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 560])
        main_layout.addWidget(splitter, 1)

        # --- BOTTOM ACTION BAR ---
        actions_card = SimpleCardWidget()
        actions_layout = FlowLayout(actions_card, needAni=False, isTight=True)
        actions_layout.setContentsMargins(12, 6, 12, 6)
        actions_layout.setHorizontalSpacing(10)
        actions_layout.setVerticalSpacing(6)

        self.start_button = PrimaryPushButton("Start Characterization", self)
        self.start_button.setIcon(FluentIcon.PLAY)
        self.start_button.clicked.connect(self._on_start_clicked)

        self.stop_button = PushButton("Stop", self)
        self.stop_button.setIcon(FluentIcon.CANCEL)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)

        self.pdf_button = PushButton("Open PDF Report", self)
        self.pdf_button.setIcon(FluentIcon.DOCUMENT)
        self.pdf_button.setEnabled(False)
        self.pdf_button.clicked.connect(self._on_generate_pdf_clicked)

        self.csv_button = PushButton("Open CSV Data", self)
        self.csv_button.setIcon(FluentIcon.SHARE)
        self.csv_button.setEnabled(False)
        self.csv_button.clicked.connect(self._on_export_csv_clicked)

        self.policy_retry_button = PushButton("Retry policy restore", self)
        self.policy_retry_button.setIcon(FluentIcon.SYNC)
        self.policy_retry_button.setVisible(False)
        self.policy_retry_button.setEnabled(False)
        self.policy_retry_button.clicked.connect(self._on_policy_retry_clicked)
        self.field_report_retry_button = PushButton("Regenerate series reports", self)
        self.field_report_retry_button.hide()
        self.field_report_retry_button.clicked.connect(self._start_field_reports)
        self.field_summary_pdf_button = PushButton("Summary PDF", self)
        self.field_summary_csv_button = PushButton("Summary CSV", self)
        for button in (self.field_summary_pdf_button, self.field_summary_csv_button):
            button.hide()
        self.field_summary_pdf_button.clicked.connect(lambda: self._open_field_summary("field_series_report.pdf"))
        self.field_summary_csv_button.clicked.connect(lambda: self._open_field_summary("field_series_summary.csv"))

        actions_layout.addWidget(self.start_button)
        actions_layout.addWidget(self.stop_button)
        actions_layout.addWidget(self.policy_retry_button)
        actions_layout.addWidget(self.field_report_retry_button)
        actions_layout.addWidget(self.field_summary_pdf_button)
        actions_layout.addWidget(self.field_summary_csv_button)
        actions_layout.addWidget(self.pdf_button)
        actions_layout.addWidget(self.csv_button)

        main_layout.addWidget(actions_card)

    def sizeHint(self) -> QSize:
        return QSize(800, 450)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        splitter = getattr(self, "_workspace_splitter", None)
        if splitter is None:
            return
        orientation = Qt.Orientation.Horizontal if self.width() >= 1360 else Qt.Orientation.Vertical
        if splitter.orientation() != orientation:
            splitter.setOrientation(orientation)
            splitter.setSizes([560, 800] if orientation == Qt.Orientation.Horizontal else [300, 500])

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
        self._remember_sweep_draft()
        self._draft_channel = self._selected_channel()
        self._draft_mode = self._channel_modes[self._draft_channel]
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self._draft_mode)
        self.mode_combo.blockSignals(False)
        self._restore_sweep_draft()
        self.refresh_limits()
        self._update_limits_from_settings()
        self._update_plot_labels()
        self.refresh_shared_source_configuration()
        self._sync_field_series_availability()

    def _sync_field_series_availability(self) -> None:
        """Expose the A-sample/B-field workflow only from the Channel A form."""
        panel = getattr(self, "field_panel", None)
        if panel is None:
            return
        channel_a = self._selected_channel() == "A"
        if not channel_a:
            panel.enabled_box.setChecked(False)
        panel.enabled_box.setEnabled(channel_a)
        panel.enabled_box.setToolTip(
            "Uses Channel A for the sample sweep and Channel B for field-line current."
            if channel_a else "Select Channel A to enable the field-line series."
        )
        self._refresh_field_validation()

    def _refresh_field_validation(self, *_args) -> bool:
        panel = getattr(self, "field_panel", None)
        if panel is None:
            return False
        if not panel.enabled_box.isChecked():
            panel.show_ramp_summary(None)
            message = ("Select Channel A to enable this procedure."
                       if self._selected_channel() != "A" else
                       "Enable the field-line series to validate the complete A/B procedure.")
            panel.show_validation(None, message)
            return False
        if self._selected_channel() != "A":
            panel.show_ramp_summary(None)
            panel.show_validation(False, "Field-line series requires sample Channel A.")
            return False
        if self._source_request_provider is None:
            panel.show_ramp_summary(None)
            panel.show_validation(None, "Waiting for the shared Keithley A/B configuration.")
            return False
        try:
            from app.devices.keithley_2600.characterization.field_series import (
                FieldSeriesRunner,
                estimated_field_target_durations_s,
            )

            sweep = self._build_config(compliance_policy_override="stop", channel="A")
            source = self._source_request_provider("B", "current", 0.0)
            config = panel.build_config(sweep, source)
            FieldSeriesRunner.validate(config, self._settings)
        except Exception as exc:
            panel.show_ramp_summary(None)
            panel.show_validation(False, f"Fix before start: {exc}")
            return False
        count = len(config.currents_a)
        limits = self._settings.keithley.safety.channels["B"].lab_limits
        estimates = estimated_field_target_durations_s(
            config, max_points=limits.sweep_points_max)
        panel.show_ramp_summary(config, max_points=limits.sweep_points_max)
        panel.show_validation(
            True,
            f"Ready: {count} B value{'s' if count != 1 else ''}; A/B limits and units passed. "
            f"Estimated series time ≈ {sum(estimates):.1f} s; longest B target ≈ "
            f"{max(estimates):.1f} s.",
        )
        return True

    def _on_mode_changed(self) -> None:
        self._remember_sweep_draft()
        self._draft_mode = self.mode_combo.currentIndex()
        self._channel_modes[self._draft_channel] = self._draft_mode
        self._restore_sweep_draft()
        self.refresh_limits()
        self._update_limits_from_settings()
        self._update_plot_labels()
        self.refresh_shared_source_configuration()

    def _remember_sweep_draft(self) -> None:
        self._sweep_drafts[(self._draft_channel, self._draft_mode)] = (
            self.start_level_edit.text(), self.stop_level_edit.text(), self.points_spin.value(),
        )

    def _save_operator_drafts(self, *_args) -> None:
        """Persist operator input only; never cache inherited hardware settings."""
        self._remember_sweep_draft()
        payload = {
            "schema_version": 1,
            "channel": self._draft_channel,
            "modes": self._channel_modes,
            "sweeps": {f"{ch}:{mode}": list(values)
                       for (ch, mode), values in self._sweep_drafts.items()},
            "field": self.field_panel.draft_state(),
        }
        settings = QSettings(SETTINGS_SECTION, SETTINGS_SECTION)
        settings.setValue(KEY_SWEEP_DRAFTS, json.dumps(payload, ensure_ascii=False))
        settings.sync()

    def _restore_operator_drafts(self) -> None:
        settings = QSettings(SETTINGS_SECTION, SETTINGS_SECTION)
        raw = settings.value(KEY_SWEEP_DRAFTS, "")
        if not raw:
            return
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError("Unsupported draft schema")
            channel, modes, sweeps = payload["channel"], payload["modes"], payload["sweeps"]
            if channel not in ("A", "B") or not isinstance(modes, dict) or set(modes) != {"A", "B"}:
                raise ValueError("Invalid draft channels")
            if any(type(mode) is not int or mode not in (0, 1) for mode in modes.values()):
                raise ValueError("Invalid draft modes")
            if not isinstance(sweeps, dict):
                raise ValueError("Invalid sweep drafts")
            drafts = {}
            for key, values in sweeps.items():
                if key not in ("A:0", "A:1", "B:0", "B:1") or not isinstance(values, list) or len(values) != 3:
                    raise ValueError("Invalid sweep draft")
                if not all(isinstance(value, str) for value in values[:2]) or type(values[2]) is not int or not 3 <= values[2] <= 2147483647:
                    raise ValueError("Invalid draft values")
                drafts[(key[0], int(key[2]))] = tuple(values)
            self.field_panel.validate_draft_state(payload["field"])
        except (ValueError, TypeError, KeyError):
            self.banner.show_message("Saved characterization inputs could not be restored. Review the form before starting.", severity="warning")
            return
        self._sweep_drafts = drafts
        self._channel_modes = dict(modes)
        self._draft_channel, self._draft_mode = channel, modes[channel]
        self.channel_combo.blockSignals(True)
        self.mode_combo.blockSignals(True)
        self.channel_combo.setCurrentIndex(0 if channel == "A" else 1)
        self.mode_combo.setCurrentIndex(self._draft_mode)
        self.mode_combo.blockSignals(False)
        self.channel_combo.blockSignals(False)
        self._restore_sweep_draft()
        self.refresh_limits()
        self._update_limits_from_settings()
        self._update_plot_labels()
        self.field_panel.restore_draft_state(payload["field"])
        self._sync_field_series_availability()

    def _restore_sweep_draft(self) -> None:
        unit = "uA" if self._draft_mode == 0 else "mV"
        start, stop, points = self._sweep_drafts.get(
            (self._draft_channel, self._draft_mode), (f"-100 {unit}", f"100 {unit}", 101),
        )
        self.start_level_edit.setText(start)
        self.stop_level_edit.setText(stop)
        limits = self._settings.keithley.safety.channels[self._draft_channel].lab_limits
        self.points_spin.setMaximum(limits.sweep_points_max)
        self.points_spin.setValue(points)


    # -------------------------------------------------------------------------
    # Sample Inventory & Device Selection
    # -------------------------------------------------------------------------

    def set_inventory_store(self, store: InventoryStore) -> None:
        """Assign inventory store, refresh samples, and restore selection."""
        self._inventory_store = store
        self.refresh_samples_list()
        self._restore_saved_metadata_selection()

    def set_source_request_provider(
        self,
        provider: Callable[[str, str, float | None], KeithleySourceRequest],
        compliance_policy_provider: Callable[[str], str] | None = None,
        compliance_policy_transition_provider: Callable[
            [str, str, bool, Callable[[str], None], Callable[[str], None]], bool
        ] | None = None,
    ) -> None:
        """Use the normal Keithley card as the only hardware-configuration source."""
        self._source_request_provider = provider
        self._compliance_policy_provider = compliance_policy_provider
        self._compliance_policy_transition_provider = (
            compliance_policy_transition_provider
        )
        self.refresh_shared_source_configuration()
        self._refresh_field_validation()

    def refresh_shared_source_configuration(self) -> None:
        """Project the normal card's current hardware settings into this page."""
        if self._source_request_provider is None:
            return
        channel = self._selected_channel()
        mode = "current" if self._is_current_mode() else "voltage"
        try:
            request = self._source_request_provider(channel, mode, None)
        except Exception as exc:
            self.shared_configuration_label.setText(
                f"Keithley card settings are not valid: {exc}"
            )
            self.shared_configuration_label.setStyleSheet(
                "color: #dc2626; font-weight: 600;"
            )
            return

        compliance_dimension = (
            DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
        )
        compliance_unit = "mV" if mode == "current" else "uA"
        self.compliance_edit.setText(
            format_quantity_auto(
                request.compliance_si,
                compliance_dimension,
                preferred_unit=compliance_unit,
            )
        )
        self.dwell_edit.setText(
            format_quantity_auto(
                request.settle_time_s,
                DIMENSION_TIME,
                preferred_unit="ms",
            )
        )
        source_dimension = (
            DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        )
        source_range = (
            "AUTO"
            if request.source_autorange
            else format_quantity_auto(request.source_range_si or 0.0, source_dimension)
        )
        voltage_range = (
            "AUTO"
            if request.measure_voltage_autorange
            else format_quantity_auto(
                request.measure_voltage_range_si or 0.0, DIMENSION_VOLTAGE
            )
        )
        current_range = (
            "AUTO"
            if request.measure_current_autorange
            else format_quantity_auto(
                request.measure_current_range_si or 0.0, DIMENSION_CURRENT
            )
        )
        sense = "2-wire local" if request.sense_mode == "2wire" else "4-wire Kelvin"
        self.shared_configuration_label.setText(
            "Inherited from Keithley card · "
            f"NPLC {request.nplc:g} · settling {self.dwell_edit.text()} · "
            f"source range {source_range} · measure V {voltage_range} · "
            f"measure I {current_range} · {sense}"
        )
        self.shared_configuration_label.setStyleSheet("")

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

    def _parse_diameter_to_nm(self, text: str) -> float | None:
        """Parse diameter text string to float in nanometers."""
        raw = text.strip().replace(",", ".")
        if not raw:
            return None
        um_match = re.search(
            r"^([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*(?:um|µm|microns?)$",
            raw,
            re.IGNORECASE,
        )
        if um_match:
            try:
                return float(um_match.group(1)) * 1000.0
            except ValueError:
                return None
        nm_match = re.search(
            r"^([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*(?:nm)?$",
            raw,
            re.IGNORECASE,
        )
        if nm_match:
            try:
                return float(nm_match.group(1))
            except ValueError:
                return None
        return None

    def _parse_area_to_um2(self, text: str) -> float | None:
        """Parse junction area text string to float in um^2."""
        raw = text.strip().replace(",", ".")
        if not raw:
            return None
        match = re.search(r"^([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", raw)
        if match:
            try:
                val = float(match.group(1))
                return val if val > 0 else None
            except ValueError:
                return None
        return None

    def _update_expected_resistance(self, area_um2: float | None) -> None:
        """Calculate and display expected MTJ resistance for nominal RA = 8 Ω·µm²."""
        if not area_um2 or area_um2 <= 0:
            self.expected_resistance_label.setText(
                "💡 Enter diameter or area to calculate expected R (RA = 8 Ω·µm²)"
            )
            return

        r_barrier = TARGET_RA_PRODUCT_OHM_UM2 / area_um2
        r_total = r_barrier + ESTIMATED_LEAD_RESISTANCE_OHM

        def _fmt_r(val: float) -> str:
            if val < 1000.0:
                return f"{val:.1f} Ω" if val >= 10 else f"{val:.2f} Ω"
            elif val < 1e6:
                return f"{val / 1e3:.2f} kΩ"
            else:
                return f"{val / 1e6:.2f} MΩ"

        self.expected_resistance_label.setText(
            f"💡 RA = 8 Ω·µm²: R_MTJ ≈ {_fmt_r(r_barrier)} · total ~{_fmt_r(r_total)}"
        )

    def _sync_preset_combo_to_diameter(self, d_nm: float) -> None:
        matched_idx = 0
        for idx in range(1, self.diameter_preset_combo.count()):
            preset_val = self.diameter_preset_combo.itemData(idx)
            if preset_val and abs(float(preset_val) - d_nm) < 0.5:
                matched_idx = idx
                break
        self.diameter_preset_combo.blockSignals(True)
        self.diameter_preset_combo.setCurrentIndex(matched_idx)
        self.diameter_preset_combo.blockSignals(False)

    def _on_diameter_changed(self) -> None:
        if self._syncing_geometry:
            return
        self._syncing_geometry = True
        try:
            d_nm = self._parse_diameter_to_nm(self.diameter_edit.text())
            if d_nm is not None and d_nm > 0:
                d_um = d_nm / 1000.0
                area = math.pi * ((d_um / 2.0) ** 2)
                if area >= 1.0:
                    area_str = f"{area:.4f}".rstrip("0").rstrip(".")
                elif area >= 0.01:
                    area_str = f"{area:.4f}"
                else:
                    area_str = f"{area:.6f}".rstrip("0").rstrip(".")
                self.area_edit.setText(area_str)
                self._update_expected_resistance(area)
                self._sync_preset_combo_to_diameter(d_nm)
            else:
                self._update_expected_resistance(None)
        finally:
            self._syncing_geometry = False

    def _on_area_changed(self) -> None:
        if self._syncing_geometry:
            return
        self._syncing_geometry = True
        try:
            area_um2 = self._parse_area_to_um2(self.area_edit.text())
            if area_um2 is not None and area_um2 > 0:
                d_um = 2.0 * math.sqrt(area_um2 / math.pi)
                d_nm = d_um * 1000.0
                if abs(d_nm - round(d_nm)) < 0.2:
                    d_nm = float(round(d_nm))
                if d_nm >= 100:
                    d_str = f"{d_nm:.1f}".rstrip("0").rstrip(".")
                else:
                    d_str = f"{d_nm:.2f}".rstrip("0").rstrip(".")
                self.diameter_edit.setText(f"{d_str} nm")
                self._update_expected_resistance(area_um2)
                self._sync_preset_combo_to_diameter(d_nm)
            else:
                self._update_expected_resistance(None)
        finally:
            self._syncing_geometry = False

    def _on_diameter_preset_changed(self, index: int) -> None:
        if index <= 0 or self._syncing_geometry:
            return
        preset_val = self.diameter_preset_combo.itemData(index)
        if preset_val and isinstance(preset_val, (int, float)) and preset_val > 0:
            self.diameter_edit.setText(f"{int(preset_val)} nm")

    def _try_parse_and_fill_cell_hints(self, notes: str, label: str) -> None:
        combined = f"{label} {notes}"

        # 1. Look for explicit area first (e.g. "area: 3.14 um2", "powierzchnia: 0.28")
        area_match = re.search(r"(?i)(?:area|powierzchnia)[:=\s]+([0-9.]+)", combined)
        if area_match:
            try:
                val = float(area_match.group(1))
                if val > 0:
                    self.area_edit.setText(str(val))
            except ValueError:
                pass

        # 2. Look for Pillar code P1-P10 (from INL wafer designs: P1=100nm .. P10=1000nm)
        pillar_match = re.search(r"\bP(10|[1-9])\b", combined, re.IGNORECASE)
        pillar_diameters = {
            "10": 1000.0,
            "9": 800.0,
            "8": 600.0,
            "7": 400.0,
            "6": 350.0,
            "5": 300.0,
            "4": 300.0,
            "3": 250.0,
            "2": 200.0,
            "1": 100.0,
        }
        if not area_match and pillar_match and pillar_match.group(1) in pillar_diameters:
            code = pillar_match.group(1)
            self.diameter_edit.setText(f"{int(pillar_diameters[code])} nm")
        elif not area_match:
            # 3. Look for diameter explicit specifications e.g. "d=600nm", "diam: 200", "diameter: 1 um"
            diam_match = re.search(
                r"(?i)(?:diameter|średnica|diam|fi|Ø|\bd\b)\s*[:=]?\s*([0-9.]+)\s*(nm|um|µm)?",
                combined,
            )
            if diam_match:
                try:
                    val = float(diam_match.group(1))
                    unit = (diam_match.group(2) or "nm").lower()
                    if "um" in unit or "µm" in unit:
                        val *= 1000.0
                    if val > 0:
                        self.diameter_edit.setText(f"{val:g} nm")
                except ValueError:
                    pass
            else:
                # 4. Look for pillar size e.g. "600 nm pillar", "200nm pillar"
                pillar_size_match = re.search(
                    r"\b([0-9.]+)\s*(nm|um|µm)\s*(?:pillar|nanopillar|filon|słup)\b",
                    combined,
                    re.IGNORECASE,
                )
                if pillar_size_match:
                    try:
                        val = float(pillar_size_match.group(1))
                        unit = (pillar_size_match.group(2) or "nm").lower()
                        if "um" in unit or "µm" in unit:
                            val *= 1000.0
                        if val > 0:
                            self.diameter_edit.setText(f"{val:g} nm")
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
        settings.setValue(KEY_LAST_DIAMETER, self.diameter_edit.text().strip())
        settings.setValue(KEY_LAST_AREA, self.area_edit.text().strip())
        settings.setValue(KEY_LAST_THICKNESS, self.thickness_edit.text().strip())
        settings.setValue(KEY_LAST_OPERATOR, self.operator_edit.text().strip())
        settings.sync()

    def _persist_selection_to_settings(
        self, sample_id: str, row: str, col: str, device_label: str
    ) -> None:
        settings = QSettings(SETTINGS_SECTION, SETTINGS_SECTION)
        settings.setValue(KEY_LAST_SAMPLE_ID, sample_id)
        settings.setValue(KEY_LAST_ROW, row)
        settings.setValue(KEY_LAST_COL, col)
        settings.setValue(KEY_LAST_DEVICE_LABEL, device_label)
        settings.setValue(KEY_LAST_DIAMETER, self.diameter_edit.text().strip())
        settings.setValue(KEY_LAST_AREA, self.area_edit.text().strip())
        settings.setValue(KEY_LAST_THICKNESS, self.thickness_edit.text().strip())
        settings.setValue(KEY_LAST_OPERATOR, self.operator_edit.text().strip())
        settings.sync()

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

        # 1. Restore operator, diameter, area, thickness
        saved_operator = str(settings.value(KEY_LAST_OPERATOR, "") or "")
        if saved_operator:
            self.operator_edit.setText(saved_operator)

        self._syncing_geometry = True
        try:
            saved_diameter = str(settings.value(KEY_LAST_DIAMETER, "") or "")
            if saved_diameter:
                self.diameter_edit.setText(saved_diameter)
            saved_area = str(settings.value(KEY_LAST_AREA, "") or "")
            if saved_area:
                self.area_edit.setText(saved_area)
                self._update_expected_resistance(self._parse_area_to_um2(saved_area))
        finally:
            self._syncing_geometry = False

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
        editor.setMaximumWidth(85)
        field.range_pill.setMinimumWidth(120)
        field.edit_button.setFixedWidth(68)
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
        self._update_plot_labels()
        self._fit_saved_field_plot()
        self._refresh_field_overlays()

    @Slot(str)
    def _on_plot_view_route_changed(self, route_key: str) -> None:
        """Keep plotted curves synchronized with the Fluent segmented control."""
        if not hasattr(self, "curve_iv"):
            return
        self._set_plot_view(1 if route_key == "res" else 0)

    def _update_plot_labels(self) -> None:
        is_current = (self._displayed_field_dataset.config.mode == "current"
                      if self._displayed_field_dataset is not None else self._is_current_mode())
        if self.field_overlay_check.isChecked() and self._stored_field_series is not None:
            is_current = self._stored_field_series.manifest["config"]["sweep"]["mode"] == "current"
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
            is_4wire = channel_settings.sense_mode == "4wire"
            self.sense_warning_label.setText(
                f"⚠️ Channel {ch} is configured for 4-wire (Kelvin) mode in Station Settings. "
                "Ensure physical Sense HI and Sense LO leads are connected to the DUT. "
                "Floating sense leads will bypass compliance and output full rail voltage (~20–40 V), destroying delicate MTJ barriers!"
            )
            self.sense_warning_label.setVisible(is_4wire)
            limits = channel_settings.lab_limits
            if self._is_current_mode():
                min_curr_si = parse_quantity(limits.source_current.min, DIMENSION_CURRENT).si_value
                max_curr_si = parse_quantity(limits.source_current.max, DIMENSION_CURRENT).si_value
                try:
                    start_si = parse_quantity(self.start_level_edit.text(), DIMENSION_CURRENT).si_value
                    if start_si < min_curr_si:
                        self.start_level_edit.setText(limits.source_current.min)
                    elif start_si > max_curr_si:
                        self.start_level_edit.setText(limits.source_current.max)
                except Exception:
                    pass
                try:
                    stop_si = parse_quantity(self.stop_level_edit.text(), DIMENSION_CURRENT).si_value
                    if stop_si > max_curr_si:
                        self.stop_level_edit.setText(limits.source_current.max)
                    elif stop_si < min_curr_si:
                        self.stop_level_edit.setText(limits.source_current.min)
                except Exception:
                    pass
            else:
                min_volt_si = parse_quantity(limits.source_voltage.min, DIMENSION_VOLTAGE).si_value
                max_volt_si = parse_quantity(limits.source_voltage.max, DIMENSION_VOLTAGE).si_value
                try:
                    start_si = parse_quantity(self.start_level_edit.text(), DIMENSION_VOLTAGE).si_value
                    if start_si < min_volt_si:
                        self.start_level_edit.setText(limits.source_voltage.min)
                    elif start_si > max_volt_si:
                        self.start_level_edit.setText(limits.source_voltage.max)
                except Exception:
                    pass
                try:
                    stop_si = parse_quantity(self.stop_level_edit.text(), DIMENSION_VOLTAGE).si_value
                    if stop_si > max_volt_si:
                        self.stop_level_edit.setText(limits.source_voltage.max)
                    elif stop_si < min_volt_si:
                        self.stop_level_edit.setText(limits.source_voltage.min)
                except Exception:
                    pass
        except Exception:
            pass
        self.refresh_limits()

    def _build_config(
        self, *, compliance_policy_override: str | None = None, channel: str | None = None,
    ) -> CharacterizationSweepConfig:
        self._remember_sweep_draft()
        ch = channel or self._selected_channel()
        mode_index = self._channel_modes[ch] if channel else self.mode_combo.currentIndex()
        is_current = mode_index == 0
        mode = "current" if is_current else "voltage"

        dim_sweep = DIMENSION_CURRENT if is_current else DIMENSION_VOLTAGE
        unit = "uA" if is_current else "mV"
        start_text, stop_text, point_count = self._sweep_drafts.get(
            (ch, mode_index), (f"-100 {unit}", f"100 {unit}", 101),
        )
        start_si = parse_quantity(start_text, dim_sweep).si_value
        stop_si = parse_quantity(stop_text, dim_sweep).si_value
        if self._source_request_provider is None or self._compliance_policy_provider is None:
            raise SafetyViolation(
                "Shared normal Keithley card configuration is unavailable; "
                "characterization cannot start."
            )
        shared_request = self._source_request_provider(ch, mode, start_si)
        if shared_request.channel != ch or shared_request.mode != mode:
            raise SafetyViolation(
                "Keithley card returned a different channel or source mode."
            )
        comp_si = shared_request.compliance_si
        dwell_si = shared_request.settle_time_s
        compliance_policy = (
            compliance_policy_override
            if compliance_policy_override is not None
            else self._compliance_policy_provider(ch)
        )

        if compliance_policy != "stop":
            raise SafetyViolation(
                "Characterization is blocked because the normal Keithley card "
                f"uses compliance policy {compliance_policy!r}. Select 'Stop on "
                "compliance' there before the manual check and characterization."
            )

        if comp_si <= 0:
            raise ValueError("Compliance limit must be greater than zero.")
        if abs(stop_si - start_si) < 1e-15:
            raise ValueError("Start and stop levels cannot be identical.")

        diameter_val = self._parse_diameter_to_nm(self.diameter_edit.text())

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

        metadata = SampleMetadata(
            sample_id=self.sample_id_edit.text().strip() or "Sample-1",
            structure_name=self.structure_edit.text().strip(),
            operator=self.operator_edit.text().strip(),
            diameter_nm=diameter_val,
            junction_area_um2=area_val,
            nominal_barrier_thickness_nm=thick_val,
        )

        return CharacterizationSweepConfig(
            channel=ch,
            mode=mode,
            start_level_si=start_si,
            stop_level_si=stop_si,
            points_count=point_count,
            compliance_si=comp_si,
            compliance_policy=compliance_policy,  # type: ignore[arg-type]
            dwell_time_s=dwell_si,
            nplc=shared_request.nplc,
            sense_mode=shared_request.sense_mode,
            source_autorange=shared_request.source_autorange,
            source_range_si=shared_request.source_range_si,
            measure_voltage_autorange=shared_request.measure_voltage_autorange,
            measure_voltage_range_si=shared_request.measure_voltage_range_si,
            measure_current_autorange=shared_request.measure_current_autorange,
            measure_current_range_si=shared_request.measure_current_range_si,
            metadata=metadata,
        )

    def _start_field_series(self) -> None:
        from app.devices.keithley_2600.characterization.field_scenario import build_field_scenario
        from app.devices.keithley_2600.characterization.field_worker import FieldSeriesWorker
        from app.devices.keithley_2600.ui.field_scenario_dialog import FieldScenarioDialog

        if (self._worker is not None and self._worker.isRunning()) or self._temporary_policy_phase != "idle":
            return
        if self._selected_channel() != "A":
            self.banner.show_message(
                "Field-line series requires Channel A for the sample sweep. Select Channel A first.",
                severity="error",
            )
            return
        try:
            sweep = self._build_config(compliance_policy_override="stop", channel="A")
            source = self._source_request_provider("B", "current", 0.0)
            config = self.field_panel.build_config(sweep, source)
            proxy = self._controller.adapter_for_run()
            initial = {ch: str(proxy.compliance_policy(ch)) for ch in ("A", "B")}
            originals = {ch: str(self._compliance_policy_provider(ch)) for ch in ("A", "B")}
            scenario = build_field_scenario(config, self._settings, initial, originals)
            row, col, label = self.selected_device_coord()
            selected_id = self.selected_sample_id()
            run_target = (selected_id, str(row), str(col), str(label))
            sample = self._inventory_store.get_sample(selected_id) if self._inventory_store and selected_id else None
            inventory_target = ({
                "sample_id": sample.sample_id, "sample_name": sample.name,
                "row": str(row), "col": str(col), "device_label": str(label),
            } if sample is not None else None)
            for ch in ("A", "B"):
                proxy.confirm_output_off(ch)
        except Exception as exc:
            self.banner.show_message(f"Field series preflight blocked: {exc}", severity="error")
            return

        # This is the only path to the field worker from the UI. Cancel/close
        # exits before reservation, policy writes, or output enablement.
        dialog = FieldScenarioDialog(scenario, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            self.status_label.setText("Field series cancelled before output enable")
            dialog.deleteLater()
            return
        dialog.deleteLater()
        try:
            self._field_lease = self._controller.acquire_run_lease()
            self._run_inventory_target = run_target
            directory = self._automatic_run_directory(CharacterizationDataset(sweep, (), "", ""))
            worker = FieldSeriesWorker(
                self._field_lease, self._settings, scenario.config, directory,
                dict(scenario.initial_policies), dict(scenario.restore_policies), self,
                reviewed_scenario=scenario,
                inventory_target=inventory_target,
            )
            self._field_worker = worker
            self._stored_field_series = None
            self.field_overlay_check.setChecked(False)
            self.field_overlay_group.hide()
            self.field_curve_combo.clear()
            worker.policies_changed.connect(self.field_policies_changed.emit)
            worker.event.connect(self._on_field_event)
            worker.finished.connect(self._on_field_finished)
            self.channel_combo.setCurrentIndex(0)
            self._set_run_input_lock(True)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.pdf_button.setEnabled(False)
            self.csv_button.setEnabled(False)
            self._current_pdf_path = self._current_csv_path = None
            self.progress_bar.setValue(0)
            self.status_label.setText("Preparing reviewed field series; both outputs must be OFF...")
            self._clear_field_plot()
            worker.start()
        except Exception as exc:
            # No worker has begun if construction/start failed. Releasing a
            # busy gate still rejects, leaving the card blocked for recovery.
            if self._field_lease is not None:
                try:
                    self._field_lease.release()
                    self._field_lease = None
                except Exception:
                    pass
            self._set_run_input_lock(self._field_lease is not None)
            self.start_button.setEnabled(self._field_lease is None)
            self.stop_button.setEnabled(False)
            self.banner.show_message(f"Field series could not start: {exc}", severity="error")

    def _clear_field_plot(self):
        self._remove_field_overlays()
        self._displayed_field_dataset = None
        self.saved_series_context.hide()
        self.plot_widget.enableAutoRange()
        for values in (self._live_v_points, self._live_i_points, self._live_r_points,
                       self._live_app_r_points, self._live_dem_points,
                       self._live_comp_x, self._live_comp_y):
            values.clear()
        for curve in (self.curve_iv, self.curve_clamped, self.curve_r_true, self.curve_r_app):
            curve.setData([], [])
        self._update_plot_labels()
        if self._field_worker is not None:
            compliance = self._field_worker.config.sweep.compliance_si
            self.compliance_line_pos.setValue(compliance)
            self.compliance_line_neg.setValue(-compliance)

    @Slot(str, object)
    def _on_field_event(self, kind, data):
        if kind == "field_start":
            self._clear_field_plot()
            self.status_label.setText(
                f"Field target {data['field_index'] + 1}/{len(self._field_worker.config.currents_a)}: "
                f"B = {format_quantity_auto(data['target_a'], 'current')}")
        elif kind == "sample_point":
            from app.devices.keithley_2600.characterization.models import FieldLineObservation
            values = {name: data[name] for name in CharacterizationPoint.__dataclass_fields__}
            for name in ("field_before", "field_after"):
                if values[name] is not None:
                    values[name] = FieldLineObservation(**values[name])
            self._on_point_acquired(CharacterizationPoint(**values))
        elif kind == "curve_saved":
            self.progress_bar.setValue(int(100 * (data["index"] + 1) / len(self._field_worker.config.currents_a)))
            if data["dataset"] is not None:
                self._current_dataset = data["dataset"]
                target = self._field_worker.config.currents_a[data["index"]]
                self._current_csv_path = self._field_worker.directory / f"{data['index'] + 1:04d}_Ib_{target:+.9g}A" / "characterization.csv"

    @Slot()
    def _on_field_finished(self):
        outcome = self._field_worker.outcome
        self.stop_button.setEnabled(False)
        if outcome is None:
            self.banner.show_message("Field worker ended without a shutdown result.", severity="error", timeout_ms=0)
            self.policy_retry_button.show()
            return
        self.status_label.setText(f"Field series: {outcome.status}; " + (
            "both outputs OFF" if outcome.outputs_off else "OUTPUT state unconfirmed"))
        self.csv_button.setEnabled(self._current_csv_path is not None)
        if outcome.outputs_off and outcome.policies_restored:
            self._release_field_lease()
            if self._field_lease is None and outcome.directory.is_dir():
                self._field_report_directory = outcome.directory
                catalogue_error = self._register_field_catalogue()
                if catalogue_error:
                    self.banner.show_message(catalogue_error, severity="error", timeout_ms=0)
                self._start_field_reports()
        else:
            self.policy_retry_button.show()
            self.policy_retry_button.setEnabled(True)
            StationMessageBox.critical(
                self,
                "Field-line characterization requires recovery",
                "The procedure ended, but OUTPUT OFF or compliance-policy restoration "
                "was not confirmed. Do not start another measurement until recovery succeeds.",
            )
        if outcome.errors:
            self.banner.show_message("; ".join(outcome.errors), severity="error", timeout_ms=0)

    def _release_field_lease(self):
        try:
            self._field_lease.release()
        except Exception as exc:
            self.banner.show_message(f"Instrument reservation remains held: {exc}", severity="error", timeout_ms=0)
            self.policy_retry_button.show()
            return
        self._field_lease = None
        self._set_run_input_lock(False)
        self.start_button.setEnabled(True)
        self.policy_retry_button.hide()

    @Slot()
    def _start_field_reports(self):
        if self._field_lease is not None or self._field_report_directory is None:
            return
        if self._field_report_worker is not None and self._field_report_worker.isRunning():
            return
        try:
            from app.devices.keithley_2600.characterization.field_reports import FieldReportWorker
        except Exception as exc:
            self.field_report_retry_button.show()
            self.field_report_retry_button.setEnabled(True)
            self.banner.show_message(f"Report generator unavailable: {exc}", severity="error", timeout_ms=0)
            self._show_field_completion_dialog([f"Report generator unavailable: {exc}"])
            return
        self._field_report_worker = FieldReportWorker(self._field_report_directory, self)
        self._field_report_worker.finished.connect(self._on_field_reports_finished)
        self.field_report_retry_button.show()
        self.field_report_retry_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.status_label.setText(f"{self.status_label.text()} · Generating reports from saved data...")
        self.open_series_button.setEnabled(False)
        self._field_report_worker.start()

    @Slot()
    def _on_field_reports_finished(self):
        worker = self._field_report_worker
        self.start_button.setEnabled(self._field_lease is None)
        self.open_series_button.setEnabled(True)
        self.field_report_retry_button.setEnabled(True)
        errors = [worker.error] if worker.error else list(worker.result.errors)
        catalogue_error = self._register_field_catalogue()
        if catalogue_error:
            errors.append(catalogue_error)
        if worker.result is not None and worker.result.report_paths:
            candidate = report_path(self._current_csv_path.parent, field_curve=True, existing=True) if self._current_csv_path else None
            self._current_pdf_path = candidate if candidate in worker.result.report_paths else worker.result.report_paths[-1]
            self.pdf_button.setEnabled(True)
        if errors:
            self.banner.show_message("Report generation incomplete: " + "; ".join(errors), severity="error", timeout_ms=0)
            self.status_label.setText("Series data retained · report generation incomplete")
        else:
            acquisition_status = self._field_worker.outcome.status if self._field_worker is not None else "saved"
            self.status_label.setText(f"Field series: {acquisition_status} · individual reports saved")
        display_error = None
        try:
            self.open_field_series(worker.directory)
        except Exception as exc:
            display_error = str(exc)
            self.banner.show_message(f"Saved series could not be displayed: {exc}", severity="error", timeout_ms=0)
        self._show_field_completion_dialog(errors, display_error)

    def _show_field_completion_dialog(self, report_errors, display_error=None) -> None:
        outcome = self._field_worker.outcome if self._field_worker is not None else None
        if outcome is None:
            return
        curves = tuple(self._stored_field_series.curves) if self._stored_field_series is not None else ()
        completed = sum(curve.status == "completed" for curve in curves)
        sample_compliance = sum(curve.status == "sample_compliance" for curve in curves)
        skipped = sum(curve.status == "skipped_field_compliance" for curve in curves)
        total = len(curves) or len(self._field_worker.config.currents_a)
        analysis_configured = self._field_worker.config.analysis_current_window_a is not None
        lines = [
            f"Acquisition status: {outcome.status}",
            f"Field targets: {total}; completed: {completed}; A compliance: "
            f"{sample_compliance}; B compliance/skipped: {skipped}",
            "Keithley outputs A and B: confirmed OFF" if outcome.outputs_off
            else "Keithley output state: NOT CONFIRMED",
            "Previous compliance policies: restored" if outcome.policies_restored
            else "Previous compliance policies: restoration required",
            f"Saved directory: {outcome.directory}",
        ]
        if not analysis_configured:
            lines.append(
                "Fit analysis: not calculated because no current window was selected; "
                "raw curves remain available."
            )
        all_errors = [str(error) for error in report_errors if error]
        if display_error:
            all_errors.append(f"Opening saved series: {display_error}")
        if all_errors:
            lines.append("Report/display warnings: " + "; ".join(all_errors))
        safe_complete = (outcome.outputs_off and outcome.policies_restored
                         and outcome.status in {"completed", "completed_with_skips"}
                         and not all_errors)
        title = "Field-line characterization completed" if safe_complete else "Field-line characterization ended"
        method = StationMessageBox.information if safe_complete else StationMessageBox.warning
        method(self, title, "\n\n".join(lines))

    def _choose_saved_field_series(self):
        from app.ui.dialogs import StationFileDialog
        directory = StationFileDialog.getExistingDirectory(self, "Open field-series directory")
        if directory:
            try:
                self.open_field_series(Path(directory))
            except Exception as exc:
                self.banner.show_message(f"Cannot open field series: {exc}", severity="error")

    def open_field_series(self, directory):
        """Read and display saved curves without changing any source configuration."""
        if (self._field_lease is not None or self._temporary_policy_phase != "idle"
                or (self._worker is not None and self._worker.isRunning())
                or (self._field_report_worker is not None and self._field_report_worker.isRunning())):
            raise SafetyViolation("Wait for the active measurement to finish before opening saved curves.")
        from app.devices.keithley_2600.characterization.field_reader import load_field_series
        series = load_field_series(directory)
        self._stored_field_series = series
        self._field_report_directory = series.directory
        self.field_overlay_group.blockSignals(True)
        self.field_overlay_group.clear()
        for offset in range(0, len(series.curves), 8):
            self.field_overlay_group.addItem(f"Overlay #{offset + 1}–#{min(offset + 8, len(series.curves))}")
        self.field_overlay_group.setVisible(len(series.curves) > 8)
        self.field_overlay_group.blockSignals(False)
        self.field_curve_combo.blockSignals(True)
        self.field_curve_combo.clear()
        for curve in series.curves:
            self.field_curve_combo.addItem(
                f"#{curve.index + 1} · B {format_quantity_auto(curve.current_a, 'current')} · "
                f"{curve.status} · history {curve.history_segment}")
        candidates = [curve.index for curve in series.curves if curve.dataset is not None]
        selected = candidates[-1] if candidates else 0
        self.field_curve_combo.setCurrentIndex(selected)
        self.field_curve_combo.blockSignals(False)
        self._select_field_curve(selected)
        self.field_report_retry_button.setVisible(series.manifest["status"] != "running")
        self.field_summary_pdf_button.setVisible(report_path(series.directory, summary=True, existing=True).is_file())
        self.field_summary_csv_button.setVisible((series.directory / "field_series_summary.csv").is_file())

    def _open_field_summary(self, filename):
        if self._stored_field_series is not None:
            path = self._stored_field_series.directory / filename
            if filename == "field_series_report.pdf":
                path = report_path(self._stored_field_series.directory, summary=True, existing=True)
            if path.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _annotate_saved_curve(self):
        series = self._stored_field_series
        index = self.field_curve_combo.currentIndex()
        if self._field_lease is not None or self._temporary_policy_phase != "idle" or (
            self._worker is not None and self._worker.isRunning()
        ) or (self._field_report_worker is not None and self._field_report_worker.isRunning()):
            self.banner.show_message("Wait for measurement and reporting to finish before annotating.")
            return
        if series is None or not 0 <= index < len(series.curves) or not (
            series.curves[index].dataset and series.curves[index].dataset.points
        ):
            self.banner.show_message("Select a saved curve with acquired points first.")
            return
        from app.devices.keithley_2600.ui.observation_dialog import ObservationDialog
        dialog = ObservationDialog(series.curves[index], self.operator_edit.text(), self)
        if dialog.exec() and dialog.saved_path is not None:
            self._field_report_directory = series.directory
            self._start_field_reports()

    def _select_field_curve(self, index):
        series = self._stored_field_series
        if series is None or not 0 <= index < len(series.curves):
            return
        curve = series.curves[index]
        self._clear_field_plot()
        dataset = curve.dataset
        self._current_dataset = dataset
        self._current_parameters = None
        self._displayed_field_dataset = dataset
        self._current_csv_path = curve.directory / "characterization.csv" if dataset is not None else None
        pdf = report_path(curve.directory, field_curve=True, existing=True) if curve.directory else None
        self._current_pdf_path = pdf if pdf and pdf.is_file() else None
        self.csv_button.setEnabled(self._current_csv_path is not None)
        self.pdf_button.setEnabled(self._current_pdf_path is not None)
        self.status_label.setText(f"Saved field item {index + 1}: {curve.status}")
        for metric, title in ((self.metric_r0, "R₀"), (self.metric_g0, "G₀"), (self.metric_ra, "R·A"),
                              (self.metric_pmax, "P_max"), (self.metric_r2, "R²")):
            metric.setText(f"{title}: —")
        self.metric_comp.setText(f"Status: {curve.status}")
        self.metric_comp.setStyleSheet("")
        if dataset is None:
            self._refresh_field_overlays()
            return
        config = dataset.config
        self.saved_series_context.setText(
            f"Saved configuration · {config.metadata.sample_id} · {config.metadata.structure_name} · "
            f"{format_quantity_auto(config.start_level_si, config.mode)} → "
            f"{format_quantity_auto(config.stop_level_si, config.mode)} · "
            f"compliance {format_quantity_auto(config.compliance_si, 'voltage' if config.mode == 'current' else 'current')} · "
            f"{len(dataset.points)} acquired points")
        self.saved_series_context.show()
        x = [point.demanded_si for point in dataset.points]
        y = [(point.measured_voltage_v if dataset.config.mode == "current" else point.measured_current_a)
             if point.valid else math.nan for point in dataset.points]
        self.curve_iv.setData(x, y, connect="finite")
        self.curve_r_true.setData(x, [p.true_resistance_ohm if p.valid else math.nan for p in dataset.points], connect="finite")
        self.curve_r_app.setData(x, [p.apparent_resistance_ohm if p.valid else math.nan for p in dataset.points], connect="finite")
        clamped = [(point.demanded_si, value) for point, value in zip(dataset.points, y) if point.compliance_active]
        self.curve_clamped.setData([p[0] for p in clamped], [p[1] for p in clamped])
        self.compliance_line_pos.setValue(dataset.config.compliance_si)
        self.compliance_line_neg.setValue(-dataset.config.compliance_si)
        self._update_plot_labels()
        parameters = KeithleyCharacterizationAnalyzer.analyze(dataset)
        self._current_parameters = parameters
        r0 = parameters.zero_bias_resistance_ohm
        self.metric_r0.setText(f"R₀: {format_quantity_auto(r0, 'resistance')}" if math.isfinite(r0) else "R₀: —")
        g0 = parameters.zero_bias_conductance_s
        self.metric_g0.setText(f"G₀: {g0:.6g} S" if math.isfinite(g0) else "G₀: —")
        if parameters.ra_product_ohm_um2 is not None:
            self.metric_ra.setText(f"R·A: {parameters.ra_product_ohm_um2:.6g} Ω·µm²")
        if math.isfinite(parameters.max_power_dissipated_w):
            self.metric_pmax.setText(f"P_max: {format_quantity_auto(parameters.max_power_dissipated_w, 'power')}")
        self.metric_r2.setText(f"R²: {parameters.linearity_r2:.5g}" if math.isfinite(parameters.linearity_r2) else "R²: —")
        self.plot_widget.enableAutoRange()
        self._fit_saved_field_plot()
        self._refresh_field_overlays()

    def _remove_field_overlays(self):
        for item in self._field_overlay_items:
            self.plot_widget.removeItem(item)
        self._field_overlay_items.clear()
        if self._field_overlay_legend is not None:
            self._field_overlay_legend.clear()
            self._field_overlay_legend.hide()

    def _refresh_field_overlays(self):
        self._remove_field_overlays()
        if not self.field_overlay_check.isChecked() or self._stored_field_series is None:
            return
        from app.devices.keithley_2600.characterization.field_plot_data import field_overlay_curves
        offset = max(0, self.field_overlay_group.currentIndex()) * 8
        selected_series = replace(self._stored_field_series, curves=self._stored_field_series.curves[offset:offset + 8])
        curves = field_overlay_curves(selected_series, resistance=self._active_plot_view == 1)
        for item in (self.curve_iv, self.curve_clamped, self.curve_r_true, self.curve_r_app):
            item.hide()
        if self._field_overlay_legend is None:
            theme = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
            self._field_overlay_legend = self.plot_widget.addLegend(
                offset=(12, 12), labelTextColor=theme.axes, brush=theme.background)
        self._field_overlay_legend.show()
        self._field_overlay_legend.setColumnCount(2 if len(curves) > 4 else 1)
        values = []
        for curve in curves:
            brightness = 220 if isDarkTheme() else 150
            color = pg.intColor(curve.index, hues=max(len(self._stored_field_series.curves), 6),
                                minValue=brightness, maxValue=brightness, sat=190)
            item = self.plot_widget.plot(
                curve.x, curve.y, pen=pg.mkPen(color, width=2), symbol="o", symbolSize=4,
                symbolBrush=color, name=curve.label, connect="finite")
            self._field_overlay_items.append(item)
            values.extend(value for value in curve.y if math.isfinite(value))
        if values:
            low, high = min(values), max(values)
            center = (low + high) / 2
            span = max(high - low, max(abs(low), abs(high)) * 0.02, 1e-12)
            self.plot_widget.setYRange(center - span / 2, center + span / 2, padding=0.08)
        self._update_plot_labels()

    def _fit_saved_field_plot(self):
        dataset = self._displayed_field_dataset
        if dataset is None:
            return
        values = []
        for point in dataset.points:
            if not point.valid:
                continue
            candidates = ((point.true_resistance_ohm, point.apparent_resistance_ohm)
                          if self._active_plot_view == 1 else
                          (point.measured_voltage_v if dataset.config.mode == "current" else point.measured_current_a,))
            values.extend(value for value in candidates if math.isfinite(value))
        if values:
            low, high = min(values), max(values)
            # Display padding only: do not magnify floating-point residue in
            # a constant curve into an apparent physical resistance change.
            center = (low + high) / 2
            span = max(high - low, max(abs(low), abs(high)) * 0.02, 1e-12)
            self.plot_widget.setYRange(center - span / 2, center + span / 2, padding=0.08)

    def _register_field_catalogue(self):
        if self._inventory_store is None or self._field_report_directory is None:
            return None
        try:
            from app.devices.keithley_2600.characterization.field_catalogue import register_field_series
            records = register_field_series(self._inventory_store, self._field_report_directory)
            for sample_id in {record.sample_id for record in records}:
                self.measurement_saved.emit(sample_id)
        except Exception as exc:
            return f"Measurement catalogue: {exc}"
        return None

    def _retry_field_restore(self):
        from app.devices.keithley_2600.characterization.field_worker import FieldPolicyRecoveryWorker
        if self._field_worker is None or self._field_worker.isRunning():
            return
        if self._field_recovery_worker is not None and self._field_recovery_worker.isRunning():
            return
        self.policy_retry_button.setEnabled(False)
        worker = FieldPolicyRecoveryWorker(self._field_lease, self._field_worker.restore_policies, self)
        self._field_recovery_worker = worker
        worker.finished_recovery.connect(self._on_field_recovered)
        worker.start()

    @Slot(bool, bool, object)
    def _on_field_recovered(self, off, restored, errors):
        if off and restored:
            self.field_policies_changed.emit(dict(self._field_worker.restore_policies))
            self._release_field_lease()
            if self._field_lease is None:
                self._field_report_directory = self._field_worker.directory
                self._start_field_reports()
        else:
            self.policy_retry_button.setEnabled(True)
            self.banner.show_message("; ".join(errors), severity="error", timeout_ms=0)

    @Slot()
    def _on_start_clicked(self) -> None:
        if self._field_report_worker is not None and self._field_report_worker.isRunning():
            return
        if self._field_lease is not None:
            return
        if self.field_panel.enabled_box.isChecked():
            if not self._refresh_field_validation():
                self.banner.show_message(
                    "Field-series validation failed. Correct the highlighted validation message; "
                    "no hardware configuration was changed.",
                    severity="error",
                )
                return
            self._start_field_series()
            return
        self._field_worker = None
        self._field_report_directory = None
        self.field_report_retry_button.hide()
        self.field_summary_pdf_button.hide()
        self.field_summary_csv_button.hide()
        if (
            self._worker is not None
            and self._worker.isRunning()
        ) or self._temporary_policy_phase != "idle":
            return

        channel = self._selected_channel()
        try:
            device_proxy = self._controller.adapter_for_run()
        except Exception as exc:
            self.banner.show_message(f"Keithley instrument unavailable: {exc}")
            return

        try:
            if hasattr(device_proxy, "connected") and not device_proxy.connected:
                self.banner.show_message(
                    "Keithley instrument is not connected. Connect the device before starting measurement."
                )
                return
        except Exception as exc:
            self.banner.show_message(
                "Keithley connection state could not be read; characterization was blocked: "
                f"{exc}"
            )
            return

        # The normal card remains the single source of every source, range,
        # sense, NPLC, dwell and compliance-limit field.  Only the response to
        # a compliance event is allowed to be overridden for this run.
        try:
            config = self._build_config(compliance_policy_override="stop")
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

        # Do not begin a policy transition while a manual run is still
        # energizing the selected channel.  This read-only snapshot is also
        # the explicit proof that the modal's "no output" promise is true.
        try:
            readback = device_proxy.read_configuration()
            channels = getattr(readback, "channels", ())
            channel_readback = next(
                (
                    item
                    for item in channels
                    if str(getattr(item, "channel", "")) == channel
                ),
                None,
            )
            if channel_readback is None:
                raise SafetyViolation(
                    f"Keithley channel {channel} output state was not returned by readback."
                )
            if bool(getattr(channel_readback, "output_enabled", True)):
                self.banner.show_message(
                    f"Keithley channel {channel} OUTPUT is ON. Turn it OFF before starting "
                    "characterization; card settings will be applied and verified automatically."
                )
                return
        except Exception as exc:
            self.banner.show_message(
                "Keithley output state could not be confirmed OFF; characterization was blocked: "
                f"{exc}"
            )
            return

        try:
            active_policy = str(device_proxy.compliance_policy(channel))
        except Exception as exc:
            self.banner.show_message(
                "Keithley compliance policy could not be read; characterization was blocked: "
                f"{exc}"
            )
            return

        if active_policy not in {"stop", "warn_clamp", "skip"}:
            self.banner.show_message(
                "Keithley returned an unknown compliance policy; characterization was blocked."
            )
            return

        try:
            normal_policy = str(self._compliance_policy_provider(channel))
        except Exception as exc:
            self.banner.show_message(
                "The normal Keithley card policy could not be read; characterization was blocked: "
                f"{exc}"
            )
            return
        if normal_policy not in {"stop", "warn_clamp", "skip"}:
            self.banner.show_message(
                "The normal Keithley card has a pending or unknown compliance policy; "
                "wait for its readback before starting characterization."
            )
            return

        # Treat either side of the shared state as authoritative only after
        # they agree.  A stale page projection is synchronized through the
        # same temporary transaction, then restored to the policy shown in the
        # normal card after OUTPUT OFF.
        transition_needed = not (
            active_policy == "stop" and normal_policy == "stop"
        )
        if transition_needed:
            labels = {
                "stop": "Stop on compliance",
                "warn_clamp": "Warn & clamp",
                "skip": "Skip compliance (legacy)",
            }
            current_label = labels.get(normal_policy, normal_policy)
            adapter_note = ""
            if active_policy != normal_policy:
                adapter_note = (
                    f"\n\nThe adapter currently reports '{active_policy}', while the "
                    f"normal card shows '{normal_policy}'. The application will "
                    "synchronize the adapter before the sweep."
                )
            choice = StationMessageBox.warning(
                self,
                "Temporary Keithley safety policy",
                (
                    f"Channel {channel} is currently using '{current_label}'.\n\n"
                    "Characterization requires 'Stop on compliance' so the output "
                    "is switched OFF at the first compliance event.\n\n"
                    "The application will change only this response policy for the "
                    "duration of this measurement. Source level, compliance limit, "
                    "ranges, NPLC, settling time and sense wiring remain inherited "
                    "from the normal Keithley card. The previous policy will be "
                    "restored after OUTPUT OFF is confirmed.\n\n"
                    "No output will be enabled until you confirm."
                    + adapter_note
                ),
                StationMessageBox.StandardButton.Yes
                | StationMessageBox.StandardButton.Cancel,
                StationMessageBox.StandardButton.Cancel,
            )
            if choice != StationMessageBox.StandardButton.Yes:
                self.status_label.setText("Characterization cancelled before output enable")
                self.banner.show_message(
                    "Characterization was cancelled; Keithley output remains OFF.",
                    severity="warning",
                )
                return

            self._temporary_policy_channel = channel
            self._temporary_policy_original = normal_policy
            self._temporary_policy_phase = "setting"
            self._pending_start_config = config
            self._pending_start_device = device_proxy
            self._set_run_input_lock(True)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.status_label.setText(
                f"Applying temporary Stop on compliance to channel {channel}; output remains OFF..."
            )
            provider = self._compliance_policy_transition_provider
            if provider is None:
                self._on_policy_transition_failed(
                    "The normal Keithley card cannot apply a temporary compliance policy."
                )
                return
            if not provider(
                channel,
                "stop",
                True,
                self._on_policy_set_for_characterization,
                self._on_policy_transition_failed,
            ):
                self._on_policy_transition_failed(
                    "The normal Keithley card rejected the temporary compliance policy."
                )
            return

        self._start_worker(config, device_proxy)

    def _start_worker(
        self,
        config: CharacterizationSweepConfig,
        device_proxy: Any,
    ) -> None:
        """Start the worker only after all policy and source preflight gates pass."""

        self._stored_field_series = None
        self._displayed_field_dataset = None
        self._remove_field_overlays()
        self.field_overlay_check.setChecked(False)
        self.field_overlay_group.hide()
        self.field_curve_combo.clear()
        self.saved_series_context.hide()
        self.plot_widget.enableAutoRange()
        self._set_run_input_lock(True)

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

        row, col, label = self.selected_device_coord()
        self._run_inventory_target = (
            self.selected_sample_id(), str(row), str(col), str(label)
        )

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
        self._current_csv_path = None
        self._current_pdf_path = None

        self._worker.start()

    def _on_policy_set_for_characterization(self, policy: str) -> None:
        """Handle a confirmed temporary policy change before starting output."""

        if self._temporary_policy_phase != "setting":
            return
        channel = self._temporary_policy_channel
        config = self._pending_start_config
        device_proxy = self._pending_start_device
        if channel is None or config is None or device_proxy is None:
            self._on_policy_transition_failed(
                "Characterization lost its pending safety context before output enable."
            )
            return
        if policy != "stop":
            self._on_policy_transition_failed(
                f"Keithley confirmed unexpected compliance policy {policy!r}; expected 'stop'."
            )
            return
        try:
            actual_policy = str(device_proxy.compliance_policy(channel))
        except Exception as exc:
            self._on_policy_transition_failed(
                f"Keithley Stop on compliance readback failed: {exc}"
            )
            return
        if actual_policy != "stop":
            self._on_policy_transition_failed(
                f"Keithley readback returned {actual_policy!r} after requesting 'stop'."
            )
            return

        self._temporary_policy_phase = "running"
        self._pending_start_config = None
        self._pending_start_device = None
        self._start_worker(config, device_proxy)

    def _on_policy_transition_failed(self, error: str) -> None:
        """Fail closed and restore the original policy after a transition fault."""

        phase = self._temporary_policy_phase
        channel = self._temporary_policy_channel
        original = self._temporary_policy_original
        if phase == "setting" and channel and original:
            # A failed write may still have reached the adapter.  Always issue
            # an explicit restore request before declaring the start aborted.
            self._temporary_policy_phase = "restoring_after_start_failure"
            self._pending_start_config = None
            self._pending_start_device = None
            provider = self._compliance_policy_transition_provider
            if provider is not None and provider(
                channel,
                original,
                False,
                self._on_policy_restored_after_start_failure,
                self._on_policy_transition_failed,
            ):
                self.status_label.setText(
                    f"Temporary policy failed; restoring channel {channel} policy with output OFF..."
                )
                return
            self._on_policy_transition_failed(
                f"{error} Policy restoration could not be queued."
            )
            return

        if phase == "restoring_after_start_failure":
            self._temporary_policy_phase = "restore_failed"
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.policy_retry_button.setVisible(True)
            self.policy_retry_button.setEnabled(True)
            self.status_label.setText(
                "Characterization was not started; OUTPUT remains OFF, but the previous compliance policy was not confirmed."
            )
            self.banner.show_message(
                "Characterization was blocked and OUTPUT remains OFF. "
                f"Keithley policy restoration failed: {error}",
                severity="error",
                timeout_ms=0,
            )
            StationMessageBox.critical(
                self,
                "Keithley policy restoration failed",
                "OUTPUT remains OFF. Do not continue characterization until the "
                "normal Keithley card confirms the previous compliance policy.\n\n"
                f"{error}",
            )
            return

        if phase == "restoring":
            self._temporary_policy_phase = "restore_failed"
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.policy_retry_button.setVisible(True)
            self.policy_retry_button.setEnabled(True)
            self.status_label.setText(
                "Measurement ended with OUTPUT OFF; previous compliance policy was not confirmed."
            )
            self.banner.show_message(
                "Measurement ended safely with OUTPUT OFF, but restoring the previous "
                f"Keithley compliance policy failed: {error}",
                severity="error",
                timeout_ms=0,
            )
            StationMessageBox.critical(
                self,
                "Keithley policy restoration failed",
                "OUTPUT remains OFF. Do not start another characterization until the "
                "previous compliance policy is confirmed in the normal Keithley card.\n\n"
                f"{error}",
            )
            return

        self._temporary_policy_phase = "idle"
        self._temporary_policy_channel = None
        self._temporary_policy_original = None
        self._set_run_input_lock(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.policy_retry_button.setVisible(False)
        self.policy_retry_button.setEnabled(False)
        self.status_label.setText(f"Characterization was blocked: {error}")
        self.banner.show_message(f"Characterization was blocked: {error}")

    def _on_policy_restored_after_start_failure(self, policy: str) -> None:
        expected = self._temporary_policy_original
        if expected is None or policy != expected:
            self._on_policy_transition_failed(
                f"Keithley restored {policy!r}; expected {expected!r}."
            )
            return
        self._temporary_policy_phase = "idle"
        self._temporary_policy_channel = None
        self._temporary_policy_original = None
        self._set_run_input_lock(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.policy_retry_button.setVisible(False)
        self.policy_retry_button.setEnabled(False)
        self.status_label.setText(
            "Characterization was cancelled before output enable; previous policy restored."
        )
        self.banner.show_message(
            "Characterization was not started; Keithley policy restored and output remains OFF.",
            severity="warning",
        )

    def _begin_policy_restore(self) -> None:
        """Restore the pre-characterization policy after the runner is fully OFF."""

        channel = self._temporary_policy_channel
        original = self._temporary_policy_original
        if channel is None or original is None:
            self._finalize_run_ui()
            return
        self._temporary_policy_phase = "restoring"
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status_label.setText(
            f"{self.status_label.text()} OUTPUT OFF confirmed; restoring channel "
            f"{channel} compliance policy..."
        )
        provider = self._compliance_policy_transition_provider
        if provider is None or not provider(
            channel,
            original,
            False,
            self._on_policy_restored,
            self._on_policy_transition_failed,
        ):
            self._on_policy_transition_failed(
                "The normal Keithley card could not queue policy restoration."
            )

    def _on_policy_restored(self, policy: str) -> None:
        expected = self._temporary_policy_original
        if expected is None or policy != expected:
            self._on_policy_transition_failed(
                f"Keithley restored {policy!r}; expected {expected!r}."
            )
            return
        policy_label = {
            "warn_clamp": "Warn & clamp",
            "skip": "Skip compliance (legacy)",
            "stop": "Stop on compliance",
        }.get(expected, expected)
        self.status_label.setText(
            f"{self.status_label.text()} Policy restored: {policy_label}."
        )
        self._temporary_policy_phase = "idle"
        self._temporary_policy_channel = None
        self._temporary_policy_original = None
        self.policy_retry_button.setVisible(False)
        self.policy_retry_button.setEnabled(False)
        self._finalize_run_ui()

    def _finalize_run_ui(self) -> None:
        self._finish_single_report_after_restore()
        self._set_run_input_lock(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.policy_retry_button.setVisible(False)
        self.policy_retry_button.setEnabled(False)
        self._run_inventory_target = None

    def _set_run_input_lock(self, locked: bool) -> None:
        """Freeze the sweep snapshot while policy/output transitions are active."""

        for name in (
            "mode_combo",
            "channel_combo",
            "start_level_field",
            "stop_level_field",
            "points_spin",
            "sample_combo",
            "device_combo",
            "field_panel",
            "field_curve_combo",
            "open_series_button",
            "field_overlay_check",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(not locked)

    @Slot()
    def _on_policy_retry_clicked(self) -> None:
        """Retry restoration only after independently confirming OUTPUT OFF."""

        if self._field_lease is not None:
            self._retry_field_restore()
            return

        if self._temporary_policy_phase != "restore_failed":
            return
        channel = self._temporary_policy_channel
        if channel is None:
            return
        try:
            device_proxy = self._controller.adapter_for_run()
            if hasattr(device_proxy, "connected") and not device_proxy.connected:
                raise RuntimeError("Keithley instrument is not connected.")
            confirm_output_off = getattr(device_proxy, "confirm_output_off", None)
            if callable(confirm_output_off):
                confirm_output_off(channel)
            else:
                device_proxy.assert_output_state(channel, expected_enabled=False)
        except Exception as exc:
            self.banner.show_message(
                "Policy restoration retry blocked; OUTPUT OFF is not confirmed: "
                f"{exc}",
                severity="error",
                timeout_ms=0,
            )
            return
        self.policy_retry_button.setEnabled(False)
        self._temporary_policy_phase = "restoring"
        self._begin_policy_restore()

    @Slot()
    def _on_stop_clicked(self) -> None:
        if self._field_worker is not None and self._field_worker.isRunning():
            self.status_label.setText("Stopping field series; confirming both outputs OFF...")
            self._field_worker.request_stop()
            return
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText("Stopping and ramping down to zero...")
            self._worker.request_stop()

    @Slot(object)
    def _on_point_acquired(self, point: CharacterizationPoint) -> None:
        is_current = (
            self._field_worker.config.sweep.mode == "current" if self._field_worker is not None
            else self._worker is None or self._worker._config.mode == "current"
        )
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
        self.metric_comp.setText("Compliance: DETECTED — stopping safely")
        self.metric_comp.setStyleSheet("color: #ef4444; font-weight: bold;")
        self.status_label.setText("Compliance detected — stopping and switching output OFF")

    @Slot(object)
    def _on_sweep_finished(self, dataset: CharacterizationDataset) -> None:
        self._current_dataset = dataset
        # Keep the characterization gate closed until the temporary policy has
        # been restored.  The runner has already completed its OUTPUT-OFF
        # finally block before this signal is delivered.
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.pdf_button.setEnabled(False)
        self.csv_button.setEnabled(False)
        if dataset.completion_status == "stopped_on_compliance":
            self.status_label.setText(
                f"Stopped safely on compliance after {len(dataset.points)} of "
                f"{dataset.config.points_count} points — output OFF"
            )
            self.banner.show_message(
                dataset.termination_detail
                or "Compliance detected. The sweep stopped before the next setpoint and the output was switched OFF.",
                severity="warning",
                timeout_ms=0,
            )
        elif dataset.completion_status == "cancelled":
            self.status_label.setText(
                f"Measurement cancelled after {len(dataset.points)} of "
                f"{dataset.config.points_count} points — output OFF"
            )
        else:
            self.status_label.setText("Measurement completed successfully — output OFF")

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
                onset = f"|I|={abs(ci)*1e3:.2f} mA"
            else:
                onset = f"|V|={abs(cv)*1e3:.1f} mV"
            if dataset.completion_status == "stopped_on_compliance":
                self.metric_comp.setText(f"Compliance: STOP at {onset}")
            else:
                self.metric_comp.setText(
                    f"Compliance: Onset {onset} "
                    f"({params.clamped_points_fraction*100:.0f}% saturation)"
                )
            self.metric_comp.setStyleSheet("color: #ef4444; font-weight: bold;")
        else:
            self.metric_comp.setText("Compliance: Not reached in the measured range")
            self.metric_comp.setStyleSheet("color: #059669; font-weight: bold;")

        self.metric_pmax.setText(f"P_max: {params.max_power_dissipated_w * 1e3:.2f} mW")
        self.metric_r2.setText(f"Linearity R²: {params.linearity_r2:.4f}" if math.isfinite(params.linearity_r2) else "Linearity R²: —")

        self._save_completed_measurement(dataset, params)
        self._begin_policy_restore()

    def _automatic_run_directory(self, dataset: CharacterizationDataset) -> Path:
        sample_id = sanitize_run_file_stem(
            dataset.config.metadata.sample_id or self.selected_sample_id(), fallback="sample"
        )
        if self._run_inventory_target is not None:
            selected_id, row, col, _label = self._run_inventory_target
        else:
            selected_id = self.selected_sample_id()
            row, col, _label = self.selected_device_coord()
        sample = (
            self._inventory_store.get_sample(selected_id)
            if self._inventory_store is not None and selected_id
            else None
        )
        if sample is not None and self._inventory_store is not None:
            root = self._inventory_store.measurement_directory_for(
                sample.sample_id, "Keithley_2600", "characterization"
            )
        else:
            root = Path(
                str(self._settings.storage.get("output_directory", "./measurements"))
            ) / "samples" / sample_id

        coord = sanitize_run_file_stem(
            f"R{row}C{col}" if row and col else "unassigned", fallback="unassigned"
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return root.resolve() / coord / timestamp

    def _save_completed_measurement(
        self,
        dataset: CharacterizationDataset,
        params: ExtractedScientificParameters,
    ) -> None:
        """Persist raw CSV now; defer PDF until policy restoration completes."""
        run_dir = self._automatic_run_directory(dataset)
        try:
            run_dir = create_run_directory(run_dir)
        except Exception as exc:
            self.banner.show_message(
                f"Measurement finished with output OFF, but its run directory could not be created: {exc}",
                severity="error",
                timeout_ms=0,
            )
            self._run_inventory_target = None
            return

        csv_path = run_dir / "characterization.csv"
        pdf_path = report_path(run_dir)
        errors: list[str] = []
        try:
            self._current_csv_path = KeithleyDataExporter.export_csv(dataset, csv_path)
        except Exception as exc:
            self._current_csv_path = None
            errors.append(f"CSV: {exc}")

        try:
            from app.devices.keithley_2600.characterization.rigol_report import export_rigol_equivalence
            export_rigol_equivalence(dataset, run_dir / "rigol_equivalence.csv")
        except Exception as exc:
            errors.append(f"Rigol equivalence CSV: {exc}")

        self._current_pdf_path = None
        self._pending_single_report = (dataset, params, pdf_path, None)

        self.csv_button.setEnabled(self._current_csv_path is not None)
        self.pdf_button.setEnabled(self._current_pdf_path is not None)

        if self._run_inventory_target is not None:
            sample_id, row, col, label = self._run_inventory_target
        else:
            sample_id = self.selected_sample_id()
            row, col, label = self.selected_device_coord()
        if (
            self._inventory_store is not None
            and sample_id
            and (row or col)
            and self._current_csv_path is not None
        ):
            try:
                sample = self._inventory_store.get_sample(sample_id)
                s_name = sample.name if sample else sample_id
                digest = hashlib.sha256(self._current_csv_path.read_bytes()).hexdigest()
                rec = SampleRunRecord(
                    sample_id=sample_id,
                    sample_name=s_name,
                    row=str(row),
                    col=str(col),
                    device_label=str(label or f"R{row}:C{col}"),
                    run_path=str(self._current_csv_path),
                    run_sha256=digest,
                    created_at_utc=dataset.completed_at_iso or datetime.now(timezone.utc).isoformat(),
                    status=dataset.completion_status,
                    point_count=len(dataset.points),
                    spectrum_count=0,
                    recipe_name=f"Keithley IV Characterization ({dataset.config.mode})",
                    notes=(
                        f"R₀={params.zero_bias_resistance_ohm:.1f} Ω, "
                        f"G₀={params.zero_bias_conductance_s*1e3:.3f} mS, "
                        f"Linearity R²={params.linearity_r2:.4f}"
                        + (f"; {dataset.termination_detail}" if dataset.termination_detail else "")
                    ),
                    csv_path=str(self._current_csv_path),
                    report_path=str(self._current_pdf_path or ""),
                )
                self._inventory_store.record_run(rec)
                self._pending_single_report = (dataset, params, pdf_path, rec)
                self._populate_device_combo_for_selected_sample()
                self.measurement_saved.emit(sample_id)
            except Exception as exc:
                errors.append(f"sample catalogue: {exc}")

        if errors:
            self.banner.show_message(
                "Measurement finished with output OFF, but automatic artifact saving was incomplete: "
                + " | ".join(errors),
                severity="error",
                timeout_ms=0,
            )
        else:
            self.status_label.setText(
                f"{self.status_label.text()} · CSV saved; PDF pending policy restoration"
            )
        self._run_inventory_target = None

    def _finish_single_report_after_restore(self) -> None:
        pending = self._pending_single_report
        if pending is None or self._temporary_policy_phase != "idle":
            return
        dataset, params, path, record = pending
        self._pending_single_report = None
        try:
            from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
            self._current_pdf_path = KeithleyPdfReportGenerator.generate(dataset, params, path)
            self.pdf_button.setEnabled(True)
            if record is not None and self._inventory_store is not None:
                self._inventory_store.register_run_artifacts(
                    replace(record, report_path=str(self._current_pdf_path)))
                self.measurement_saved.emit(record.sample_id)
            self.status_label.setText(f"{self.status_label.text()} · PDF saved automatically")
        except Exception as exc:
            self.banner.show_message(
                f"Output is OFF and policy restoration finished, but PDF publication failed: {exc}",
                severity="error", timeout_ms=0)

    @Slot(str)
    def _on_sweep_failed(self, error_msg: str) -> None:
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status_label.setText(f"Error: {error_msg}")
        self.banner.show_message(f"Characterization execution failed: {error_msg}")
        if self._worker is not None and not self._worker.output_off_confirmed:
            # Restoring the response policy while the adapter cannot confirm
            # OUTPUT OFF would hide a potentially energized DUT.  Keep the
            # temporary stop policy locked and fail closed.
            self._temporary_policy_phase = "restore_failed"
            self.policy_retry_button.setVisible(True)
            self.policy_retry_button.setEnabled(True)
            self._run_inventory_target = None
            self.banner.show_message(
                "Characterization failed and Keithley OUTPUT OFF could not be confirmed. "
                "The previous compliance policy was not restored.",
                severity="error",
                timeout_ms=0,
            )
            StationMessageBox.critical(
                self,
                "Keithley OUTPUT OFF not confirmed",
                "Do not start another measurement. OUTPUT state and compliance policy "
                "must be verified in the normal Keithley card before continuing.\n\n"
                f"{error_msg}",
            )
            return
        self._begin_policy_restore()

    @Slot()
    def _on_generate_pdf_clicked(self) -> None:
        self._open_saved_artifact(self._current_pdf_path, "PDF report")

    @Slot()
    def _on_export_csv_clicked(self) -> None:
        self._open_saved_artifact(self._current_csv_path, "CSV data")

    def _open_saved_artifact(self, path: Path | None, description: str) -> None:
        if path is None or not path.is_file():
            StationMessageBox.critical(
                self, "File unavailable", f"The automatically saved {description} is unavailable."
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            StationMessageBox.critical(
                self, "Open file failed", f"Could not open {description}:\n{path}"
            )

    def set_settings(self, settings: StationSettings) -> None:
        """Update station settings and refresh limit fields."""
        self._settings = settings
        self.field_panel.refresh_bounds(settings)
        self._update_limits_from_settings()
        for field in (
            getattr(self, "start_level_field", None),
            getattr(self, "stop_level_field", None),
            getattr(self, "compliance_field", None),
            getattr(self, "dwell_field", None),
        ):
            if field is not None:
                field.validate_and_clamp()
        self.refresh_shared_source_configuration()
        self._refresh_field_validation()

    def prepare_application_shutdown(self) -> bool:
        """Keep the controller alive until acquisition, restoration and reports finish."""
        if self._field_recovery_worker is not None and self._field_recovery_worker.isRunning():
            return False
        if self._field_worker is not None and self._field_worker.isRunning():
            self._field_worker.request_stop()
            return False
        if self._field_lease is not None:
            return False
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            return False
        if self._temporary_policy_phase != "idle":
            return False
        if self._field_report_worker is not None and self._field_report_worker.isRunning():
            return False
        self._save_operator_drafts()
        return True

    def closeEvent(self, event) -> None:
        """Safely terminate background acquisition worker on card close."""
        self._save_operator_drafts()
        if self._field_report_worker is not None and self._field_report_worker.isRunning():
            event.ignore()
            return
        if self._field_lease is not None:
            if self._field_worker is not None and self._field_worker.isRunning():
                self._field_worker.request_stop()
            event.ignore()
            return
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(2000)
        super().closeEvent(event)
