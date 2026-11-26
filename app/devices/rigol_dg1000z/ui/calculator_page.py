"""Fluent read-only Rigol DG1032Z -> MTJ Load Calculator workspace."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event
from typing import Any

import numpy as np
import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter, SVGExporter
from PySide6.QtCore import (
    QEvent,
    QObject,
    QRunnable,
    QSignalBlocker,
    QSize,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FlowLayout,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
    isDarkTheme,
)

from app.devices.rigol_dg1000z.mtj_calculator import (
    DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V,
    AmbiguousBranchError,
    CalculatorRangeError,
    ConstantResistanceModel,
    DcSolveResult,
    ReferenceCurvePass,
    ReferenceIVCurve,
    RigolVoltageLimitEstimate,
    TransferSeries,
    WaveformRequest,
    WaveformSolveResult,
    compute_rigol_voltage_limit_for_mtj,
    curve_transfer_series,
    export_conversion_csv,
    parse_rigol_load,
    solve_dc_point,
    solve_waveform,
)
from app.devices.rigol_dg1000z.reference_library import (
    LibraryScanResult,
    ReferenceLibraryScanner,
    load_characterization_json_curve,
    load_csv_reference_curves,
    load_hdf5_reference_curves,
)
from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_FREQUENCY,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    QuantityError,
    format_quantity_auto,
    parse_quantity,
)
from app.inventory.models import ActiveSampleTarget
from app.inventory.store import InventoryStore
from app.settings.models import StationSettings
from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationFileDialog
from app.ui.results.file_browser import FileBrowserPanel
from app.ui.widgets import NotificationBanner
from app.ui.widgets.plot_ownership import create_plot_widget, own_signal_proxy
from app.ui.widgets.plot_style import apply_scientific_style

_NARROW_STACK_THRESHOLD_PX = 960
_HERO_STACK_THRESHOLD_PX = 1080
_AUTO_FOLDER_SCAN_MAX_FILES = 40
_MOKE_ALL_KEY = "__ALL__"
_MOKE_NONE_KEY = "__NONE__"


class _LibraryScanSignals(QObject):
    """Queued completion signals for a background reference-curve load."""

    loaded = Signal(int, object)
    failed = Signal(int, str)
    finished = Signal(int)


class _LibraryScanTask(QRunnable):
    """Execute one read-only HDF5/CSV curve extraction outside the Qt GUI thread."""

    def __init__(
        self,
        request_id: int,
        operation: Callable[..., Any],
        **kwargs: object,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.operation = operation
        self.kwargs = kwargs
        self.signals = _LibraryScanSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            if self._cancelled.is_set():
                return
            result = self.operation(**self.kwargs, cancelled=self._cancelled.is_set)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, InterruptedError) as exc:
            if not self._cancelled.is_set():
                self.signals.failed.emit(self.request_id, str(exc))
        else:
            if not self._cancelled.is_set():
                self.signals.loaded.emit(self.request_id, result)
        finally:
            self.signals.finished.emit(self.request_id)


def _fmt(value: float | None, dimension: str, *, precision: int = 4) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return format_quantity_auto(value, dimension, precision=precision)


def _fmt_range(low: float, high: float, dimension: str, *, precision: int = 4) -> str:
    if math.isclose(low, high, rel_tol=1e-9, abs_tol=1e-12):
        return _fmt(low, dimension, precision=precision)
    return f"{_fmt(low, dimension, precision=precision)} … {_fmt(high, dimension, precision=precision)}"


def _moke_group_key(value_v: float | None) -> str:
    if value_v is None or not math.isfinite(value_v):
        return _MOKE_NONE_KEY
    # Group within 0.05 mV so tiny DAC readback jitter stays under one MOKE setting.
    return f"{round(value_v / 5e-5)}"


def _moke_group_label(key: str, sample_v: float | None) -> str:
    if key == _MOKE_NONE_KEY or sample_v is None:
        return "Not recorded / manual"
    return format_quantity_auto(sample_v, DIMENSION_VOLTAGE, precision=5)


def _kb_curve_label(curve: ReferenceIVCurve) -> str:
    kb_val = (
        curve.keithley_b_setpoint_a
        if curve.keithley_b_setpoint_a is not None
        else curve.keithley_b_measured_a
    )
    kb_txt = (
        f"KB {_fmt(kb_val, DIMENSION_CURRENT, precision=4)}"
        if kb_val is not None
        else "KB not recorded"
    )
    stats = curve.resistance_stats
    r_txt = f"R {stats.min_ohm:.1f}–{stats.max_ohm:.1f} Ω" if stats is not None else "R —"
    run_name = (
        curve.source_path.parent.name
        if curve.source_path is not None and curve.source_path.name == "data.h5"
        else (curve.source_path.name if curve.source_path is not None else curve.title)
    )
    visit_suffix = ""
    if " · Visit " in curve.title:
        visit_suffix = " · " + curve.title.rsplit(" · ", 1)[-1]
    return f"{kb_txt} · {r_txt} · {run_name}{visit_suffix}"


class _HeroActionsHost(QWidget):
    """FlowLayout host with preferred desktop width but zero minimum width so narrow resize never locks."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preferred_width = 270
        self.flow = FlowLayout(self, needAni=False, isTight=True)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(6)
        self.flow.setVerticalSpacing(6)
        self.setMinimumWidth(0)

    def set_preferred_width(self, width: int) -> None:
        self._preferred_width = max(120, int(width))
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        return QSize(self._preferred_width, self.flow.heightForWidth(self._preferred_width))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.flow.heightForWidth(self._preferred_width))


class _KpiCard(SimpleCardWidget):
    """Compact metric card displaying primary calculated value and secondary detail."""

    def __init__(self, heading: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        self.heading_label = CaptionLabel(heading, self)
        self.heading_label.setObjectName("muted")
        self.heading_label.setWordWrap(True)
        self.heading_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.value_label = SubtitleLabel("—", self)
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.value_label.setWordWrap(True)
        self.value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.detail_label = CaptionLabel("", self)
        self.detail_label.setObjectName("muted")
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_label.setWordWrap(True)
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.heading_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)

    def set_values(self, primary: str, detail: str = "") -> None:
        self.value_label.setText(primary)
        self.detail_label.setText(detail)


class RigolLoadCalculatorPage(QWidget):
    """Advisory Rigol DG1032Z -> MTJ calculator using the shared Results file browser."""

    status = Signal(str)
    rigol_voltage_limit_requested = Signal(int, float, object)

    def __init__(
        self,
        settings: StationSettings,
        *,
        inventory_store: InventoryStore | None = None,
        initial_directory: str | Path | None = None,
        catalogue_root: str | Path | None = None,
        rigol_snapshot_provider: Callable[[int], RigolConfigurationSnapshot] | None = None,
        rigol_advanced_modes_provider: Callable[[int], Sequence[str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("rigolLoadCalculatorPage")
        self._settings = settings
        self._inventory_store = inventory_store
        self._rigol_snapshot_provider = rigol_snapshot_provider
        self._rigol_advanced_modes_provider = rigol_advanced_modes_provider
        self._waveform_inverted = False
        self._import_advisories: tuple[str, ...] = ()

        default_root = Path(
            catalogue_root
            or initial_directory
            or (inventory_store.catalogue_root if inventory_store is not None else Path.cwd())
        ).expanduser().resolve()
        start_dir = Path(initial_directory).expanduser().resolve() if initial_directory else default_root
        self._initial_dir = start_dir
        self._catalogue_root = default_root

        self._scanner = ReferenceLibraryScanner()
        self._scan_pool = QThreadPool(self)
        self._scan_pool.setMaxThreadCount(1)
        self._scan_request_id = 0
        self._scan_task: _LibraryScanTask | None = None
        self._is_scanning = False
        self._initial_browser_loaded = False
        self._syncing_browser_selection = False

        # Curves loaded from either the active DUT folder or the currently selected file.
        self._saved_curves: tuple[ReferenceIVCurve, ...] = ()
        self._visible_kb_curves: tuple[ReferenceIVCurve, ...] = ()
        self._csv_curves: tuple[ReferenceIVCurve, ...] = ()
        self._selected_saved_curve: ReferenceIVCurve | None = None
        self._selected_csv_curve: ReferenceIVCurve | None = None
        self._source_mode = "saved"  # "saved" | "csv" | "resistor"
        self._signal_mode = "dc"  # "dc" | "waveform"
        self._plot_view_mode = "transfer"  # "transfer" | "reference"
        self._syncing_levels = False
        self._theme = "dark" if isDarkTheme() else "light"
        self._limit_editing_allowed = False
        self._limit_editing_reason = (
            "Open inside the station window with EDIT_SETTINGS permission to stage Rigol safety limits."
        )
        self._limit_staging_locked = False

        self.last_dc_result: DcSolveResult | None = None
        self.last_waveform_result: WaveformSolveResult | None = None
        self.last_limit_estimate: RigolVoltageLimitEstimate | None = None
        self._last_transfer_series: TransferSeries | None = None

        self._build_ui()
        self._connect_signals()
        self._update_source_mode_ui()
        self._update_signal_mode_ui()
        self._update_waveform_controls_visibility()
        self.apply_theme(self._theme)
        self._recalculate()

    @property
    def source_mode(self) -> str:
        return self._source_mode

    @property
    def signal_mode(self) -> str:
        return self._signal_mode

    @property
    def is_scanning(self) -> bool:
        return self._is_scanning

    @property
    def active_curve(self) -> ReferenceIVCurve | None:
        if self._source_mode == "saved":
            return self._selected_saved_curve
        if self._source_mode == "csv":
            return self._selected_csv_curve
        return None

    @property
    def active_pass(self) -> ReferenceCurvePass | None:
        curve = self.active_curve
        if curve is None or not curve.passes:
            return None
        idx = self.pass_combo.currentIndex()
        if 0 <= idx < len(curve.passes):
            return curve.passes[idx]
        return curve.passes[0]

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        self._recalculate()

    def configure_limit_editing(self, *, enabled: bool, reason: str) -> None:
        """Mirror station RBAC and simulation guards for the Set Rigol Voltage Limit button."""
        self._limit_editing_allowed = bool(enabled)
        self._limit_editing_reason = str(reason)
        self._sync_set_limit_button_state()

    def set_limit_staging_locked(self, locked: bool) -> None:
        """Disable limit staging during active sweeps while keeping calculations interactive."""
        self._limit_staging_locked = bool(locked)
        self._sync_set_limit_button_state()

    def _sync_set_limit_button_state(self) -> None:
        if not hasattr(self, "set_rigol_limit_button"):
            return
        if self._limit_staging_locked:
            self.set_rigol_limit_button.setEnabled(False)
            self.set_rigol_limit_button.setToolTip(
                "Safety-limit staging is disabled while a measurement run is active."
            )
            return
        if not self._limit_editing_allowed:
            self.set_rigol_limit_button.setEnabled(False)
            self.set_rigol_limit_button.setToolTip(self._limit_editing_reason)
            return
        if self.last_limit_estimate is None or self.last_limit_estimate.rigol_symmetric_limit_v <= 0.0:
            self.set_rigol_limit_button.setEnabled(False)
            self.set_rigol_limit_button.setToolTip(
                "Select a valid MTJ reference load and MTJ voltage bounds to calculate a Rigol limit."
            )
            return
        self.set_rigol_limit_button.setEnabled(True)
        self.set_rigol_limit_button.setToolTip(
            "Review and stage the calculated Rigol combined_voltage_limit (Vpp + |offset|) "
            "in Settings. Press SAVE SETTINGS to persist."
        )

    def set_inventory_store(self, store: InventoryStore | None) -> None:
        self._inventory_store = store

    def set_sample_context(
        self,
        active_directory: str | Path | None,
        catalogue_root: str | Path,
        *,
        target: ActiveSampleTarget | None = None,
    ) -> None:
        """Keep the embedded Results browser synced with the active DUT target."""
        self._catalogue_root = Path(catalogue_root).expanduser().resolve()
        if target is not None and target.is_active:
            self.active_target_label.setText(f"Active target: {target.display_text()}")
        elif active_directory is not None:
            self.active_target_label.setText(f"Target folder: {Path(active_directory).name}")
        else:
            self.active_target_label.setText("Active target: All samples")
        self.file_browser.set_sample_context(active_directory, catalogue_root)
        self._initial_browser_loaded = True

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 1. Responsive hero header card (stacks cleanly on narrow screens)
        self.hero_card = CardWidget(self)
        self.hero_card.setObjectName("rigolCalcHero")
        self._hero_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.hero_card)
        self._hero_layout.setContentsMargins(14, 10, 14, 10)
        self._hero_layout.setSpacing(10)
        heading_box = QVBoxLayout()
        heading_box.setSpacing(2)
        title = TitleLabel("Rigol DG1032Z · MTJ Load Calculator", self.hero_card)
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        subtitle = CaptionLabel(
            "Read-only 50 Ω Thevenin estimate · No VISA commands · Direct resistive / measured I–V model",
            self.hero_card,
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        heading_box.addWidget(title)
        heading_box.addWidget(subtitle)
        self._hero_layout.addLayout(heading_box, 1)

        self._hero_actions_host = _HeroActionsHost(self.hero_card)
        self._hero_actions_flow = self._hero_actions_host.flow
        self.import_ch1_button = PushButton("Import CH1", self._hero_actions_host)
        self.import_ch1_button.setToolTip(
            "Copy current carrier, load, and waveform settings from Rigol CH1 (read-only)."
        )
        self.import_ch2_button = PushButton("Import CH2", self._hero_actions_host)
        self.import_ch2_button.setToolTip(
            "Copy current carrier, load, and waveform settings from Rigol CH2 (read-only)."
        )
        self.export_csv_button = PushButton("Export CSV…", self._hero_actions_host)
        self.export_csv_button.setIcon(FluentIcon.SAVE)
        self.export_plot_button = PushButton("Export plot…", self._hero_actions_host)
        self.export_plot_button.setIcon(FluentIcon.SAVE_AS)
        for btn in (
            self.import_ch1_button,
            self.import_ch2_button,
            self.export_csv_button,
            self.export_plot_button,
        ):
            self._hero_actions_flow.addWidget(btn)
        if self._rigol_snapshot_provider is None:
            self.import_ch1_button.setEnabled(False)
            self.import_ch2_button.setEnabled(False)
        self._hero_layout.addWidget(self._hero_actions_host)
        root.addWidget(self.hero_card)

        # 2. Advisory / error toast host
        self.banner = NotificationBanner(self)
        root.addWidget(self.banner)

        # 3. Main responsive splitter
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.main_splitter.setObjectName("rigolCalcSplitter")
        self.main_splitter.setChildrenCollapsible(False)

        # --- LEFT COLUMN: Scrollable Results Browser + Field (MOKE/KB) + Rigol & MTJ Limit ---
        self.left_scroll = ScrollArea(self.main_splitter)
        self.left_scroll.setObjectName("rigolCalcLeftScroll")
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left_scroll.setMinimumWidth(0)
        self.left_scroll.enableTransparentBackground()

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 14, 0)
        left_layout.setSpacing(10)

        # Card 1: MTJ Reference Load
        ref_card = CardWidget(left_panel)
        ref_card.setMinimumWidth(0)
        ref_layout = QVBoxLayout(ref_card)
        ref_layout.setContentsMargins(12, 10, 12, 10)
        ref_layout.setSpacing(6)
        ref_title = StrongBodyLabel("MTJ reference load", ref_card)
        ref_title.setObjectName("sectionTitle")
        self.active_target_label = CaptionLabel("Active target: All samples", ref_card)
        self.active_target_label.setObjectName("muted")
        self.active_target_label.setWordWrap(True)
        self.active_target_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        ref_layout.addWidget(ref_title)
        ref_layout.addWidget(self.active_target_label)

        self.source_segment = SegmentedWidget(ref_card)
        self.source_segment.addItem("saved", "Saved Runs", onClick=lambda: self.set_source_mode("saved"))
        self.source_segment.addItem("csv", "CSV File", onClick=lambda: self.set_source_mode("csv"))
        self.source_segment.addItem("resistor", "Fixed R", onClick=lambda: self.set_source_mode("resistor"))
        self.source_segment.setCurrentItem("saved")
        ref_layout.addWidget(self.source_segment)

        # Sub-page 0: Exact same FileBrowserPanel as Results + MOKE Box & Keithley B selectors
        self._saved_source_widget = QWidget(ref_card)
        saved_widget = self._saved_source_widget
        saved_layout = QVBoxLayout(saved_widget)
        saved_layout.setContentsMargins(0, 4, 0, 0)
        saved_layout.setSpacing(8)

        self.file_browser = FileBrowserPanel(
            str(self._initial_dir),
            saved_widget,
            catalogue_tree=True,
        )
        self.file_browser.setMinimumWidth(0)
        self.file_browser.setMinimumHeight(210)
        self.file_browser.setMaximumHeight(290)
        saved_layout.addWidget(self.file_browser)

        # Field-dependent operating condition selectors (MOKE Box VOUT0 & Keithley B)
        field_box = SimpleCardWidget(saved_widget)
        field_layout = QVBoxLayout(field_box)
        field_layout.setContentsMargins(10, 8, 10, 8)
        field_layout.setSpacing(6)
        field_head = QHBoxLayout()
        field_title = StrongBodyLabel("Field condition (MOKE Box & Keithley B)", field_box)
        self.scan_folder_button = PushButton("Scan folder conditions", field_box)
        self.scan_folder_button.setToolTip(
            "Index all runs in the currently browsed folder so you can pick any MOKE Box / Keithley B condition directly."
        )
        field_head.addWidget(field_title, 1)
        field_head.addWidget(self.scan_folder_button)
        field_layout.addLayout(field_head)

        field_form = QFormLayout()
        field_form.setContentsMargins(0, 0, 0, 0)
        field_form.setSpacing(6)
        field_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        field_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.moke_combo = ComboBox(field_box)
        self.moke_combo.setMinimumWidth(140)
        self.kb_combo = ComboBox(field_box)
        self.kb_combo.setMinimumWidth(140)
        field_form.addRow(BodyLabel("MOKE Box (VOUT0)", field_box), self.moke_combo)
        field_form.addRow(BodyLabel("Keithley B & R(field)", field_box), self.kb_combo)
        field_layout.addLayout(field_form)

        self.scan_status_label = CaptionLabel(
            "Select a measurement above or browse the active DUT folder to pick MOKE / Keithley B conditions.",
            field_box,
        )
        self.scan_status_label.setObjectName("muted")
        self.scan_status_label.setWordWrap(True)
        self.scan_status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        field_layout.addWidget(self.scan_status_label)
        saved_layout.addWidget(field_box)
        ref_layout.addWidget(saved_widget)

        # Sub-page 1: CSV import
        self._csv_source_widget = QWidget(ref_card)
        csv_widget = self._csv_source_widget
        csv_layout = QVBoxLayout(csv_widget)
        csv_layout.setContentsMargins(0, 4, 0, 0)
        csv_layout.setSpacing(8)
        self.load_csv_button = PrimaryPushButton("Open Keithley CSV file…", csv_widget)
        self.load_csv_button.setIcon(FluentIcon.FOLDER)
        csv_layout.addWidget(self.load_csv_button)
        self.csv_path_label = CaptionLabel(
            "Accepts sweep CSV exports (e.g. keithley-a.csv with _mV/_mA units) "
            "or Keithley characterization.csv files.",
            csv_widget,
        )
        self.csv_path_label.setObjectName("muted")
        self.csv_path_label.setWordWrap(True)
        self.csv_path_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        csv_layout.addWidget(self.csv_path_label)
        slice_row = QHBoxLayout()
        self.csv_slice_label = CaptionLabel("Condition slice", csv_widget)
        self.csv_slice_combo = ComboBox(csv_widget)
        slice_row.addWidget(self.csv_slice_label)
        slice_row.addWidget(self.csv_slice_combo, 1)
        csv_layout.addLayout(slice_row)
        self.csv_slice_label.hide()
        self.csv_slice_combo.hide()
        ref_layout.addWidget(csv_widget)

        # Sub-page 2: Constant / averaged resistance
        self._resistor_source_widget = QWidget(ref_card)
        res_widget = self._resistor_source_widget
        res_layout = QVBoxLayout(res_widget)
        res_layout.setContentsMargins(0, 4, 0, 0)
        res_layout.setSpacing(6)
        res_form = QFormLayout()
        res_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        res_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.r_nominal_edit = LineEdit(res_widget)
        self.r_nominal_edit.setText("114 ohm")
        self.r_nominal_edit.setPlaceholderText("e.g. 114 ohm")
        self.r_min_edit = LineEdit(res_widget)
        self.r_min_edit.setText("108 ohm")
        self.r_min_edit.setPlaceholderText("optional, e.g. 108 ohm")
        self.r_max_edit = LineEdit(res_widget)
        self.r_max_edit.setText("120 ohm")
        self.r_max_edit.setPlaceholderText("optional, e.g. 120 ohm")
        self.resistor_span_min_edit = LineEdit(res_widget)
        self.resistor_span_min_edit.setText("-949.1 mV")
        self.resistor_span_min_edit.setPlaceholderText("e.g. -950 mV")
        self.resistor_span_max_edit = LineEdit(res_widget)
        self.resistor_span_max_edit.setText("949.1 mV")
        self.resistor_span_max_edit.setPlaceholderText("e.g. 950 mV")
        self.auto_span_from_limit_check = CheckBox("Auto", res_widget)
        self.auto_span_from_limit_check.setChecked(True)
        self.auto_span_from_limit_check.setToolTip(
            "Automatically set the Rigol V_set plot range from the MTJ min/max voltage bounds below."
        )
        span_row = QHBoxLayout()
        span_row.setSpacing(6)
        span_row.addWidget(self.resistor_span_min_edit, 1)
        span_row.addWidget(BodyLabel("…", res_widget))
        span_row.addWidget(self.resistor_span_max_edit, 1)
        span_row.addWidget(self.auto_span_from_limit_check)
        res_form.addRow(BodyLabel("Nominal R_MTJ", res_widget), self.r_nominal_edit)
        res_form.addRow(BodyLabel("Min R (optional)", res_widget), self.r_min_edit)
        res_form.addRow(BodyLabel("Max R (optional)", res_widget), self.r_max_edit)
        res_form.addRow(BodyLabel("Rigol V_set span", res_widget), span_row)
        res_layout.addLayout(res_form)
        res_hint = CaptionLabel(
            "Enter explicit units (e.g. 114 ohm, -950 mV). Min/Max shade the R uncertainty band; "
            "Auto sets Rigol V_set span from the MTJ min/max voltage window.",
            res_widget,
        )
        res_hint.setObjectName("muted")
        res_hint.setWordWrap(True)
        res_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        res_layout.addWidget(res_hint)
        ref_layout.addWidget(res_widget)

        # Active measured curve summary + pass selector + copy-to-fixed-R
        self.curve_footer = QWidget(ref_card)
        curve_footer_layout = QVBoxLayout(self.curve_footer)
        curve_footer_layout.setContentsMargins(0, 4, 0, 0)
        curve_footer_layout.setSpacing(6)
        self.reference_summary_label = CaptionLabel("No measured curve selected.", self.curve_footer)
        self.reference_summary_label.setWordWrap(True)
        self.reference_summary_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        curve_footer_layout.addWidget(self.reference_summary_label)

        pass_row = QHBoxLayout()
        pass_row.setSpacing(6)
        pass_row.addWidget(CaptionLabel("Sweep pass", self.curve_footer))
        self.pass_combo = ComboBox(self.curve_footer)
        self.use_curve_r_range_button = PushButton("Use R range as Fixed R", self.curve_footer)
        self.use_curve_r_range_button.setEnabled(False)
        pass_row.addWidget(self.pass_combo, 1)
        pass_row.addWidget(self.use_curve_r_range_button)
        curve_footer_layout.addLayout(pass_row)
        ref_layout.addWidget(self.curve_footer)
        left_layout.addWidget(ref_card)

        # Card 2: Rigol Signal Settings
        sig_card = CardWidget(left_panel)
        sig_layout = QVBoxLayout(sig_card)
        sig_layout.setContentsMargins(12, 10, 12, 10)
        sig_layout.setSpacing(8)
        sig_title = StrongBodyLabel("Rigol settings & operating point", sig_card)
        sig_title.setObjectName("sectionTitle")
        sig_layout.addWidget(sig_title)

        common_form = QFormLayout()
        common_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        common_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.channel_combo = ComboBox(sig_card)
        self.channel_combo.addItem("CH1", userData=1)
        self.channel_combo.addItem("CH2", userData=2)
        self.load_combo = ComboBox(sig_card)
        self.load_combo.addItem("HighZ (Voc = Vset)", userData="HIGHZ")
        self.load_combo.addItem("50 Ω (Voc = 2 × Vset)", userData="50")
        self.load_combo.addItem("Custom LOAD…", userData="CUSTOM")
        self.custom_load_edit = LineEdit(sig_card)
        self.custom_load_edit.setText("50 ohm")
        self.custom_load_edit.setPlaceholderText("e.g. 100 ohm, 1 kohm")
        self.custom_load_edit.hide()
        common_form.addRow(BodyLabel("Limit reference", sig_card), self.channel_combo)
        common_form.addRow(BodyLabel("Rigol LOAD mode", sig_card), self.load_combo)
        common_form.addRow(BodyLabel("Custom LOAD", sig_card), self.custom_load_edit)
        self._common_form = common_form
        common_form.setRowVisible(self.custom_load_edit, False)
        sig_layout.addLayout(common_form)

        self.signal_segment = SegmentedWidget(sig_card)
        self.signal_segment.addItem("dc", "DC Point", onClick=lambda: self.set_signal_mode("dc"))
        self.signal_segment.addItem(
            "waveform", "Waveform (AC / Pulse)", onClick=lambda: self.set_signal_mode("waveform")
        )
        self.signal_segment.setCurrentItem("dc")
        sig_layout.addWidget(self.signal_segment)

        # DC sub-page
        self._dc_signal_widget = QWidget(sig_card)
        dc_widget = self._dc_signal_widget
        dc_layout = QVBoxLayout(dc_widget)
        dc_layout.setContentsMargins(0, 4, 0, 0)
        dc_layout.setSpacing(6)
        dc_form = QFormLayout()
        dc_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        dc_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.dc_direction_combo = ComboBox(dc_widget)
        self.dc_direction_combo.addItem("Rigol setting (V_set)", userData="setting")
        self.dc_direction_combo.addItem("Target MTJ voltage (V_MTJ)", userData="voltage")
        self.dc_direction_combo.addItem("Target MTJ current (I_MTJ)", userData="current")
        self.dc_value_edit = LineEdit(dc_widget)
        self.dc_value_edit.setText("100 mV")
        self.dc_value_edit.setPlaceholderText("e.g. 100 mV")
        dc_form.addRow(BodyLabel("Solve from", dc_widget), self.dc_direction_combo)
        dc_form.addRow(BodyLabel("Input value", dc_widget), self.dc_value_edit)
        dc_layout.addLayout(dc_form)
        self.dc_range_label = CaptionLabel("", dc_widget)
        self.dc_range_label.setObjectName("muted")
        self.dc_range_label.setWordWrap(True)
        dc_layout.addWidget(self.dc_range_label)
        sig_layout.addWidget(dc_widget)

        # Waveform sub-page
        self._waveform_signal_widget = QWidget(sig_card)
        wf_widget = self._waveform_signal_widget
        wf_layout = QVBoxLayout(wf_widget)
        wf_layout.setContentsMargins(0, 4, 0, 0)
        wf_layout.setSpacing(6)
        self.wf_form = QFormLayout()
        self.wf_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.wf_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.waveform_combo = ComboBox(wf_widget)
        self.waveform_combo.addItems(["SIN", "SQU", "RAMP", "PULS", "DC"])
        self.level_entry_combo = ComboBox(wf_widget)
        self.level_entry_combo.addItem("Amplitude (Vpp) + Offset", userData="vpp_offset")
        self.level_entry_combo.addItem("High Level + Low Level", userData="high_low")
        self.wf_vpp_edit = LineEdit(wf_widget)
        self.wf_vpp_edit.setText("200 mV")
        self.wf_offset_edit = LineEdit(wf_widget)
        self.wf_offset_edit.setText("0 mV")
        self.wf_high_edit = LineEdit(wf_widget)
        self.wf_high_edit.setText("100 mV")
        self.wf_low_edit = LineEdit(wf_widget)
        self.wf_low_edit.setText("-100 mV")
        self.wf_freq_edit = LineEdit(wf_widget)
        self.wf_freq_edit.setText("1 kHz")
        self.wf_duty_edit = LineEdit(wf_widget)
        self.wf_duty_edit.setText("50")
        self.wf_symmetry_edit = LineEdit(wf_widget)
        self.wf_symmetry_edit.setText("50")
        self.wf_pulse_width_edit = LineEdit(wf_widget)
        self.wf_pulse_width_edit.setText("100 us")
        self.wf_pulse_lead_edit = LineEdit(wf_widget)
        self.wf_pulse_lead_edit.setText("10 ns")
        self.wf_pulse_trail_edit = LineEdit(wf_widget)
        self.wf_pulse_trail_edit.setText("10 ns")

        self.wf_form.addRow(BodyLabel("Waveform", wf_widget), self.waveform_combo)
        self.wf_form.addRow(BodyLabel("Level entry", wf_widget), self.level_entry_combo)
        self.wf_form.addRow(BodyLabel("Amplitude (Vpp)", wf_widget), self.wf_vpp_edit)
        self.wf_form.addRow(BodyLabel("Offset / DC level", wf_widget), self.wf_offset_edit)
        self.wf_form.addRow(BodyLabel("High level", wf_widget), self.wf_high_edit)
        self.wf_form.addRow(BodyLabel("Low level", wf_widget), self.wf_low_edit)
        self.wf_form.addRow(BodyLabel("Frequency", wf_widget), self.wf_freq_edit)
        self.wf_form.addRow(BodyLabel("Duty [%]", wf_widget), self.wf_duty_edit)
        self.wf_form.addRow(BodyLabel("Symmetry [%]", wf_widget), self.wf_symmetry_edit)
        self.wf_form.addRow(BodyLabel("Pulse width", wf_widget), self.wf_pulse_width_edit)
        self.wf_form.addRow(BodyLabel("Leading edge", wf_widget), self.wf_pulse_lead_edit)
        self.wf_form.addRow(BodyLabel("Trailing edge", wf_widget), self.wf_pulse_trail_edit)
        wf_layout.addLayout(self.wf_form)
        sig_layout.addWidget(wf_widget)

        # MTJ safe voltage window -> calculated conservative Rigol voltage limit
        self.limit_card = SimpleCardWidget(sig_card)
        limit_layout = QVBoxLayout(self.limit_card)
        limit_layout.setContentsMargins(10, 8, 10, 8)
        limit_layout.setSpacing(6)

        limit_header = QHBoxLayout()
        limit_header.setSpacing(6)
        limit_title = StrongBodyLabel("MTJ voltage bounds → Rigol limit", self.limit_card)
        self.set_rigol_limit_button = PushButton("Set Rigol Voltage Limit…", self.limit_card)
        self.set_rigol_limit_button.setIcon(FluentIcon.EDIT)
        limit_header.addWidget(limit_title, 1)
        limit_header.addWidget(self.set_rigol_limit_button)
        limit_layout.addLayout(limit_header)

        limit_form = QFormLayout()
        limit_form.setContentsMargins(0, 0, 0, 0)
        limit_form.setSpacing(6)
        limit_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        limit_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.mtj_v_min_edit = LineEdit(self.limit_card)
        self.mtj_v_min_edit.setText("-670 mV")
        self.mtj_v_min_edit.setPlaceholderText("e.g. -670 mV")
        self.mtj_v_max_edit = LineEdit(self.limit_card)
        self.mtj_v_max_edit.setText("670 mV")
        self.mtj_v_max_edit.setPlaceholderText("e.g. 670 mV")
        mtj_bounds_row = QHBoxLayout()
        mtj_bounds_row.setSpacing(6)
        mtj_bounds_row.addWidget(self.mtj_v_min_edit, 1)
        mtj_bounds_row.addWidget(BodyLabel("…", self.limit_card))
        mtj_bounds_row.addWidget(self.mtj_v_max_edit, 1)
        limit_form.addRow(BodyLabel("MTJ V min / max", self.limit_card), mtj_bounds_row)
        limit_layout.addLayout(limit_form)

        self.rigol_limit_summary_label = CaptionLabel("", self.limit_card)
        self.rigol_limit_summary_label.setObjectName("muted")
        self.rigol_limit_summary_label.setWordWrap(True)
        self.rigol_limit_summary_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.rigol_limit_summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        limit_layout.addWidget(self.rigol_limit_summary_label)
        sig_layout.addWidget(self.limit_card)

        left_panel.setMinimumWidth(0)
        left_layout.addWidget(sig_card)
        left_layout.addStretch(1)
        self.left_scroll.setWidget(left_panel)
        self.main_splitter.addWidget(self.left_scroll)

        # --- RIGHT COLUMN: KPI cards + Synchronized PyQtGraph plots ---
        right_panel = QWidget(self.main_splitter)
        right_panel.setMinimumWidth(0)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(10)

        self._kpi_grid = QGridLayout()
        self._kpi_grid.setContentsMargins(0, 0, 0, 0)
        self._kpi_grid.setHorizontalSpacing(8)
        self._kpi_grid.setVerticalSpacing(8)
        self.kpi_rigol = _KpiCard("Rigol Setting", right_panel)
        self.kpi_voltage = _KpiCard("Estimated MTJ Voltage", right_panel)
        self.kpi_current = _KpiCard("Estimated MTJ Current", right_panel)
        self.kpi_power = _KpiCard("Power & Impedance", right_panel)
        self._kpi_cards = (self.kpi_rigol, self.kpi_voltage, self.kpi_current, self.kpi_power)
        self._kpi_columns = 0
        self._arrange_kpi_cards(columns=4)
        right_layout.addLayout(self._kpi_grid)

        # Plot card
        plot_card = CardWidget(right_panel)
        plot_card.setMinimumWidth(0)
        plot_layout = QVBoxLayout(plot_card)
        plot_layout.setContentsMargins(12, 10, 12, 10)
        plot_layout.setSpacing(8)

        self._plot_toolbar_host = QWidget(plot_card)
        self._plot_toolbar_host.setMinimumWidth(0)
        self._plot_toolbar_flow = FlowLayout(self._plot_toolbar_host, needAni=False, isTight=True)
        self._plot_toolbar_flow.setContentsMargins(0, 0, 0, 0)
        self._plot_toolbar_flow.setHorizontalSpacing(8)
        self._plot_toolbar_flow.setVerticalSpacing(6)
        self.plot_view_segment = SegmentedWidget(self._plot_toolbar_host)
        self.plot_view_segment.addItem(
            "transfer", "Rigol → MTJ Maps", onClick=lambda: self.set_plot_view("transfer")
        )
        self.plot_view_segment.addItem(
            "reference", "Reference Keithley I–V / R", onClick=lambda: self.set_plot_view("reference")
        )
        self.plot_view_segment.setCurrentItem("transfer")
        self.unity_line_check = CheckBox("Show V_MTJ = V_set", self._plot_toolbar_host)
        self.unity_line_check.setChecked(True)
        self.fit_axes_button = PushButton("Fit axes", self._plot_toolbar_host)
        self.hover_readout_label = CaptionLabel("Hover plot for readout", self._plot_toolbar_host)
        self.hover_readout_label.setObjectName("muted")
        for item in (
            self.plot_view_segment,
            self.unity_line_check,
            self.fit_axes_button,
            self.hover_readout_label,
        ):
            self._plot_toolbar_flow.addWidget(item)
        plot_layout.addWidget(self._plot_toolbar_host)

        self.plot_stack = QStackedWidget(plot_card)

        # Transfer view: V_MTJ(V_set) + I_MTJ(V_set)
        transfer_host = QWidget(self.plot_stack)
        transfer_layout = QVBoxLayout(transfer_host)
        transfer_layout.setContentsMargins(0, 0, 0, 0)
        transfer_layout.setSpacing(8)
        self.voltage_plot = create_plot_widget(transfer_host)
        self.voltage_plot.setMinimumHeight(185)
        self.voltage_plot.addLegend()
        self.voltage_plot.setLabel("left", "MTJ Voltage", units="V")
        self.voltage_plot.setLabel("bottom", "Rigol Setting", units="V")

        self.current_plot = create_plot_widget(transfer_host)
        self.current_plot.setMinimumHeight(185)
        self.current_plot.addLegend()
        self.current_plot.setLabel("left", "MTJ Current", units="A")
        self.current_plot.setLabel("bottom", "Rigol Setting", units="V")
        self.current_plot.setXLink(self.voltage_plot)

        self._v_cross_x = pg.InfiniteLine(angle=90, movable=False)
        self._v_cross_y = pg.InfiniteLine(angle=0, movable=False)
        self._i_cross_x = pg.InfiniteLine(angle=90, movable=False)
        self._i_cross_y = pg.InfiniteLine(angle=0, movable=False)
        for line in (self._v_cross_x, self._v_cross_y, self._i_cross_x, self._i_cross_y):
            line.hide()
        self.voltage_plot.addItem(self._v_cross_x, ignoreBounds=True)
        self.voltage_plot.addItem(self._v_cross_y, ignoreBounds=True)
        self.current_plot.addItem(self._i_cross_x, ignoreBounds=True)
        self.current_plot.addItem(self._i_cross_y, ignoreBounds=True)

        self.voltage_plot.viewport().installEventFilter(self)
        self.current_plot.viewport().installEventFilter(self)
        self._v_mouse_proxy = pg.SignalProxy(
            self.voltage_plot.scene().sigMouseMoved,
            rateLimit=30,
            slot=lambda evt: self._on_transfer_hover(self.voltage_plot, evt),
        )
        self._i_mouse_proxy = pg.SignalProxy(
            self.current_plot.scene().sigMouseMoved,
            rateLimit=30,
            slot=lambda evt: self._on_transfer_hover(self.current_plot, evt),
        )
        own_signal_proxy(self._v_mouse_proxy, self)
        own_signal_proxy(self._i_mouse_proxy, self)

        transfer_layout.addWidget(self.voltage_plot, 1)
        transfer_layout.addWidget(self.current_plot, 1)
        self.plot_stack.addWidget(transfer_host)

        # Reference view: Keithley V(I) + R(I)
        ref_plot_host = QWidget(self.plot_stack)
        ref_plot_layout = QVBoxLayout(ref_plot_host)
        ref_plot_layout.setContentsMargins(0, 0, 0, 0)
        ref_plot_layout.setSpacing(8)
        self.ref_iv_plot = create_plot_widget(ref_plot_host)
        self.ref_iv_plot.setMinimumHeight(185)
        self.ref_iv_plot.addLegend()
        self.ref_iv_plot.setLabel("left", "Keithley Voltage", units="V")
        self.ref_iv_plot.setLabel("bottom", "Keithley Current", units="A")
        self.ref_r_plot = create_plot_widget(ref_plot_host)
        self.ref_r_plot.setMinimumHeight(185)
        self.ref_r_plot.addLegend()
        self.ref_r_plot.setLabel("left", "MTJ Resistance", units="Ω")
        self.ref_r_plot.setLabel("bottom", "Keithley Current", units="A")
        self.ref_r_plot.setXLink(self.ref_iv_plot)
        ref_plot_layout.addWidget(self.ref_iv_plot, 1)
        ref_plot_layout.addWidget(self.ref_r_plot, 1)
        self.plot_stack.addWidget(ref_plot_host)

        plot_layout.addWidget(self.plot_stack, 1)

        footer = CaptionLabel(
            "Model: Voc = k · V_set = V_MTJ + 50 Ω · I_MTJ (k = 1 for HighZ, k = 2 for LOAD 50 Ω). "
            "In HighZ, V_MTJ < V_set; in LOAD 50 Ω with R_MTJ > 50 Ω, V_MTJ = (1 + Γ) · V_set > V_set. "
            "Quasi-static direct-connection estimate: ignores RF reactance, cable ringing, bias-tees, "
            "and attenuators. Advisory only — not a current measurement or hardware limit.",
            plot_card,
        )
        footer.setObjectName("muted")
        footer.setWordWrap(True)
        footer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        plot_layout.addWidget(footer)
        right_layout.addWidget(plot_card, 1)

        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setStretchFactor(0, 5)
        self.main_splitter.setStretchFactor(1, 6)
        self.main_splitter.setSizes([505, 615])
        root.addWidget(self.main_splitter, 1)

    def _connect_signals(self) -> None:
        self.import_ch1_button.clicked.connect(lambda: self.import_from_rigol_channel(1))
        self.import_ch2_button.clicked.connect(lambda: self.import_from_rigol_channel(2))
        self.export_csv_button.clicked.connect(self._export_csv_dialog)
        self.export_plot_button.clicked.connect(self._export_plot_dialog)

        self.file_browser.file_selected.connect(self._on_browser_file_selected)
        self.file_browser.files_loaded.connect(self._on_browser_files_loaded)
        self.scan_folder_button.clicked.connect(self.scan_current_browser_folder)
        self.moke_combo.currentIndexChanged.connect(self._on_moke_selection_changed)
        self.kb_combo.currentIndexChanged.connect(self._on_kb_selection_changed)

        self.load_csv_button.clicked.connect(self._open_csv_dialog)
        self.csv_slice_combo.currentIndexChanged.connect(self._on_csv_slice_changed)
        self.pass_combo.currentIndexChanged.connect(self._recalculate)
        self.use_curve_r_range_button.clicked.connect(self._copy_curve_resistance_to_fixed_r)

        for field in (
            self.r_nominal_edit,
            self.r_min_edit,
            self.r_max_edit,
            self.resistor_span_min_edit,
            self.resistor_span_max_edit,
            self.mtj_v_min_edit,
            self.mtj_v_max_edit,
            self.custom_load_edit,
            self.dc_value_edit,
            self.wf_freq_edit,
            self.wf_duty_edit,
            self.wf_symmetry_edit,
            self.wf_pulse_width_edit,
            self.wf_pulse_lead_edit,
            self.wf_pulse_trail_edit,
        ):
            field.textChanged.connect(self._recalculate)

        self.resistor_span_min_edit.textEdited.connect(self._on_manual_span_edited)
        self.resistor_span_max_edit.textEdited.connect(self._on_manual_span_edited)
        self.auto_span_from_limit_check.toggled.connect(self._on_auto_span_toggled)
        self.set_rigol_limit_button.clicked.connect(self._on_set_rigol_limit_clicked)

        self.wf_vpp_edit.textChanged.connect(self._sync_high_low_from_vpp_offset)
        self.wf_offset_edit.textChanged.connect(self._sync_high_low_from_vpp_offset)
        self.wf_high_edit.textChanged.connect(self._sync_vpp_offset_from_high_low)
        self.wf_low_edit.textChanged.connect(self._sync_vpp_offset_from_high_low)

        self.channel_combo.currentIndexChanged.connect(self._recalculate)
        self.load_combo.currentIndexChanged.connect(self._on_load_combo_changed)
        self.dc_direction_combo.currentIndexChanged.connect(self._on_dc_direction_changed)
        self.waveform_combo.currentIndexChanged.connect(self._update_waveform_controls_visibility)
        self.level_entry_combo.currentIndexChanged.connect(self._update_waveform_controls_visibility)
        self.unity_line_check.toggled.connect(self._redraw_plots)
        self.fit_axes_button.clicked.connect(self._fit_active_plots)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._update_responsive_layout()
        if not self._initial_browser_loaded:
            self._initial_browser_loaded = True
            self.file_browser.refresh()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_responsive_layout()

    def _arrange_kpi_cards(self, *, columns: int) -> None:
        if self._kpi_columns == columns:
            return
        self._kpi_columns = columns
        for card in self._kpi_cards:
            self._kpi_grid.removeWidget(card)
        for col in range(4):
            self._kpi_grid.setColumnStretch(col, 1 if col < columns else 0)
        for idx, card in enumerate(self._kpi_cards):
            self._kpi_grid.addWidget(card, idx // columns, idx % columns)

    def _update_responsive_layout(self) -> None:
        narrow_split = self.width() < _NARROW_STACK_THRESHOLD_PX
        target_orient = Qt.Orientation.Vertical if narrow_split else Qt.Orientation.Horizontal
        if self.main_splitter.orientation() != target_orient:
            self.main_splitter.setOrientation(target_orient)
            if target_orient == Qt.Orientation.Vertical:
                self.main_splitter.setSizes([max(160, self.height() // 3), max(220, (self.height() * 2) // 3)])
            else:
                self.main_splitter.setSizes([505, max(500, self.width() - 525)])
        self._arrange_kpi_cards(columns=2 if narrow_split else 4)

        hero_dir = (
            QBoxLayout.Direction.TopToBottom
            if self.width() < _HERO_STACK_THRESHOLD_PX
            else QBoxLayout.Direction.LeftToRight
        )
        if self._hero_layout.direction() != hero_dir:
            self._hero_layout.setDirection(hero_dir)
        if hero_dir == QBoxLayout.Direction.TopToBottom:
            actions_w = max(240, self.width() - 60)
            self._hero_actions_host.setMaximumWidth(16_777_215)
        else:
            # 2-button-wide compact block (2x2) without raising minimumSizeHint().
            actions_w = 270
            self._hero_actions_host.setMaximumWidth(actions_w)
        self._hero_actions_host.set_preferred_width(actions_w)
        self._hero_actions_host.setMinimumHeight(
            self._hero_actions_flow.heightForWidth(actions_w)
        )
        toolbar_w = max(240, self._plot_toolbar_host.width() or (self.width() - 60))
        self._plot_toolbar_host.setMinimumHeight(
            self._plot_toolbar_flow.heightForWidth(toolbar_w)
        )

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        if (
            obj in {self.voltage_plot.viewport(), self.current_plot.viewport()}
            and event.type() == QEvent.Type.Leave
        ):
            for line in (self._v_cross_x, self._v_cross_y, self._i_cross_x, self._i_cross_y):
                line.hide()
            self.hover_readout_label.setText("Hover plot for readout")
        return super().eventFilter(obj, event)

    def _on_transfer_hover(self, source_plot: pg.PlotWidget, event: tuple) -> None:
        pos = event[0]
        vb = source_plot.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            for line in (self._v_cross_x, self._v_cross_y, self._i_cross_x, self._i_cross_y):
                line.hide()
            return
        pt = vb.mapSceneToView(pos)
        x_val = float(pt.x())
        y_val = float(pt.y())
        self._v_cross_x.setPos(x_val)
        self._i_cross_x.setPos(x_val)
        self._v_cross_x.show()
        self._i_cross_x.show()
        if source_plot is self.voltage_plot:
            self._v_cross_y.setPos(y_val)
            self._v_cross_y.show()
            self._i_cross_y.hide()
            self.hover_readout_label.setText(
                f"V_set: {_fmt(x_val, DIMENSION_VOLTAGE)}   V_MTJ: {_fmt(y_val, DIMENSION_VOLTAGE)}"
            )
        else:
            self._i_cross_y.setPos(y_val)
            self._i_cross_y.show()
            self._v_cross_y.hide()
            self.hover_readout_label.setText(
                f"V_set: {_fmt(x_val, DIMENSION_VOLTAGE)}   I_MTJ: {_fmt(y_val, DIMENSION_CURRENT)}"
            )

    def apply_theme(self, theme: str) -> None:
        self._theme = str(theme).strip().lower() or "light"
        if self._theme not in {"light", "dark"}:
            self._theme = "dark" if isDarkTheme() else "light"
        tokens = tokens_for(self._theme)
        palette = plot_theme(tokens)
        cross_pen = pg.mkPen(palette.axes, width=1.0, style=Qt.PenStyle.DashLine)
        for line in (self._v_cross_x, self._v_cross_y, self._i_cross_x, self._i_cross_y):
            line.setPen(cross_pen)
        for plot in (self.voltage_plot, self.current_plot, self.ref_iv_plot, self.ref_r_plot):
            apply_scientific_style(plot.getPlotItem(), self._theme)
        self._redraw_plots()

    # ------------------------------------------------------------------
    # Mode switches and external helpers
    # ------------------------------------------------------------------
    def set_source_mode(self, mode: str) -> None:
        if mode not in {"saved", "csv", "resistor"}:
            return
        self._source_mode = mode
        self.source_segment.setCurrentItem(mode)
        self._update_source_mode_ui()
        self._refresh_pass_selector()
        self._recalculate()

    def _update_source_mode_ui(self) -> None:
        self._saved_source_widget.setVisible(self._source_mode == "saved")
        self._csv_source_widget.setVisible(self._source_mode == "csv")
        self._resistor_source_widget.setVisible(self._source_mode == "resistor")
        self.curve_footer.setVisible(self._source_mode != "resistor")

    def set_signal_mode(self, mode: str) -> None:
        if mode not in {"dc", "waveform"}:
            return
        self._signal_mode = mode
        self.signal_segment.setCurrentItem(mode)
        self._update_signal_mode_ui()
        self._recalculate()

    def _update_signal_mode_ui(self) -> None:
        self._dc_signal_widget.setVisible(self._signal_mode == "dc")
        self._waveform_signal_widget.setVisible(self._signal_mode == "waveform")

    def set_plot_view(self, mode: str) -> None:
        if mode not in {"transfer", "reference"}:
            return
        self._plot_view_mode = mode
        self.plot_view_segment.setCurrentItem(mode)
        self.plot_stack.setCurrentIndex(0 if mode == "transfer" else 1)
        self.unity_line_check.setVisible(mode == "transfer")
        self._redraw_plots()

    def set_load_mode(self, load_value: str | float) -> None:
        spec = parse_rigol_load(load_value)
        if spec.is_high_z:
            self.load_combo.setCurrentIndex(0)
        elif spec.load_ohm is not None and math.isclose(spec.load_ohm, 50.0, rel_tol=1e-9):
            self.load_combo.setCurrentIndex(1)
        else:
            self.load_combo.setCurrentIndex(2)
            self.custom_load_edit.setText(
                format_quantity_auto(float(spec.load_ohm or 50.0), DIMENSION_RESISTANCE, precision=6)
            )
        self._on_load_combo_changed()

    def _current_load_raw(self) -> str:
        data = str(self.load_combo.currentData() or "HIGHZ")
        if data == "CUSTOM":
            return self.custom_load_edit.text().strip()
        return data

    def _on_load_combo_changed(self, *_args: object) -> None:
        is_custom = str(self.load_combo.currentData()) == "CUSTOM"
        self.custom_load_edit.setVisible(is_custom)
        self._common_form.setRowVisible(self.custom_load_edit, is_custom)
        self._recalculate()

    def _on_dc_direction_changed(self, *_args: object) -> None:
        key = str(self.dc_direction_combo.currentData() or "setting")
        if key == "current":
            self.dc_value_edit.setPlaceholderText("e.g. 1 mA")
            try:
                parse_quantity(self.dc_value_edit.text(), DIMENSION_CURRENT)
            except QuantityError:
                self.dc_value_edit.setText("1 mA")
        else:
            self.dc_value_edit.setPlaceholderText("e.g. 100 mV")
            try:
                parse_quantity(self.dc_value_edit.text(), DIMENSION_VOLTAGE)
            except QuantityError:
                self.dc_value_edit.setText("100 mV")
        self._recalculate()

    def _update_waveform_controls_visibility(self, *_args: object) -> None:
        wf = self.waveform_combo.currentText().strip().upper()
        entry = str(self.level_entry_combo.currentData() or "vpp_offset")
        is_dc = wf == "DC"
        show_vpp = not is_dc and entry == "vpp_offset"
        show_hl = not is_dc and entry == "high_low"
        self.wf_form.setRowVisible(self.level_entry_combo, not is_dc)
        self.wf_form.setRowVisible(self.wf_vpp_edit, show_vpp)
        self.wf_form.setRowVisible(self.wf_offset_edit, is_dc or show_vpp)
        self.wf_form.setRowVisible(self.wf_high_edit, show_hl)
        self.wf_form.setRowVisible(self.wf_low_edit, show_hl)
        self.wf_form.setRowVisible(self.wf_freq_edit, not is_dc)
        self.wf_form.setRowVisible(self.wf_duty_edit, wf == "SQU")
        self.wf_form.setRowVisible(self.wf_symmetry_edit, wf == "RAMP")
        self.wf_form.setRowVisible(self.wf_pulse_width_edit, wf == "PULS")
        self.wf_form.setRowVisible(self.wf_pulse_lead_edit, wf == "PULS")
        self.wf_form.setRowVisible(self.wf_pulse_trail_edit, wf == "PULS")
        self._recalculate()

    def _sync_high_low_from_vpp_offset(self, *_args: object) -> None:
        if self._syncing_levels:
            return
        self._syncing_levels = True
        try:
            wf = self.waveform_combo.currentText().strip().upper()
            offset_v = parse_quantity(self.wf_offset_edit.text(), DIMENSION_VOLTAGE).si_value
            if wf == "DC":
                high_v = low_v = offset_v
            else:
                vpp_v = parse_quantity(self.wf_vpp_edit.text(), DIMENSION_VOLTAGE).si_value
                high_v = offset_v + 0.5 * vpp_v
                low_v = offset_v - 0.5 * vpp_v
            with QSignalBlocker(self.wf_high_edit), QSignalBlocker(self.wf_low_edit):
                self.wf_high_edit.setText(format_quantity_auto(high_v, DIMENSION_VOLTAGE, precision=6))
                self.wf_low_edit.setText(format_quantity_auto(low_v, DIMENSION_VOLTAGE, precision=6))
        except QuantityError:
            pass
        finally:
            self._syncing_levels = False
        self._recalculate()

    def _sync_vpp_offset_from_high_low(self, *_args: object) -> None:
        if self._syncing_levels:
            return
        self._syncing_levels = True
        try:
            high_v = parse_quantity(self.wf_high_edit.text(), DIMENSION_VOLTAGE).si_value
            low_v = parse_quantity(self.wf_low_edit.text(), DIMENSION_VOLTAGE).si_value
            vpp_v = high_v - low_v
            offset_v = 0.5 * (high_v + low_v)
            with QSignalBlocker(self.wf_vpp_edit), QSignalBlocker(self.wf_offset_edit):
                self.wf_vpp_edit.setText(format_quantity_auto(vpp_v, DIMENSION_VOLTAGE, precision=6))
                self.wf_offset_edit.setText(format_quantity_auto(offset_v, DIMENSION_VOLTAGE, precision=6))
        except QuantityError:
            pass
        finally:
            self._syncing_levels = False
        self._recalculate()

    def import_from_rigol_channel(self, channel: int) -> bool:
        """Read-only copy of the active RigolPage carrier configuration into the calculator."""
        if self._rigol_snapshot_provider is None:
            self.banner.show_message(
                "Rigol card snapshot provider is unavailable in standalone mode.",
                severity="warning",
            )
            return False
        try:
            snap = self._rigol_snapshot_provider(channel)
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            self.banner.show_message(
                f"Could not read Rigol CH{channel} form: {exc}", severity="error"
            )
            return False

        ch_index = self.channel_combo.findData(channel)
        if ch_index >= 0:
            self.channel_combo.setCurrentIndex(ch_index)
        try:
            self.set_load_mode(snap.output_load)
        except ValueError:
            self.set_load_mode("HIGHZ")

        waveform = snap.waveform.strip().upper()
        if waveform not in {"DC", "SIN", "SQU", "RAMP", "PULS"}:
            self.banner.show_message(
                f"Rigol CH{channel} uses {waveform}; calculator supports DC, SIN, SQU, RAMP, PULS.",
                severity="warning",
            )
            return False

        import_advisories: list[str] = []
        if snap.output_mode.strip().upper() != "NORM":
            import_advisories.append(
                f"CH{channel} output mode is {snap.output_mode} (gated output is not modelled; showing carrier levels)."
            )
        if self._rigol_advanced_modes_provider is not None:
            active_adv = tuple(self._rigol_advanced_modes_provider(channel))
            if active_adv:
                import_advisories.append(
                    f"CH{channel} has {', '.join(active_adv)} active (unmodulated carrier shown)."
                )
        self._waveform_inverted = snap.output_polarity.strip().upper() == "INV"
        if self._waveform_inverted and waveform == "DC":
            import_advisories.append(
                f"CH{channel} output polarity is INV; verify DC sign on the instrument."
            )
        self._import_advisories = tuple(import_advisories)

        if waveform == "DC":
            self.set_signal_mode("dc")
            self.dc_direction_combo.setCurrentIndex(0)
            self.dc_value_edit.setText(snap.high_level)
        else:
            self.set_signal_mode("waveform")
            self.waveform_combo.setCurrentText(waveform)
            self._syncing_levels = True
            try:
                high_v = parse_quantity(snap.high_level, DIMENSION_VOLTAGE).si_value
                low_v = parse_quantity(snap.low_level, DIMENSION_VOLTAGE).si_value
                with (
                    QSignalBlocker(self.wf_high_edit),
                    QSignalBlocker(self.wf_low_edit),
                    QSignalBlocker(self.wf_vpp_edit),
                    QSignalBlocker(self.wf_offset_edit),
                    QSignalBlocker(self.wf_freq_edit),
                    QSignalBlocker(self.wf_duty_edit),
                    QSignalBlocker(self.wf_symmetry_edit),
                    QSignalBlocker(self.wf_pulse_width_edit),
                    QSignalBlocker(self.wf_pulse_lead_edit),
                    QSignalBlocker(self.wf_pulse_trail_edit),
                ):
                    self.wf_high_edit.setText(snap.high_level)
                    self.wf_low_edit.setText(snap.low_level)
                    self.wf_vpp_edit.setText(
                        format_quantity_auto(high_v - low_v, DIMENSION_VOLTAGE, precision=6)
                    )
                    self.wf_offset_edit.setText(
                        format_quantity_auto(0.5 * (high_v + low_v), DIMENSION_VOLTAGE, precision=6)
                    )
                    self.wf_freq_edit.setText(snap.frequency)
                    self.wf_duty_edit.setText(snap.square_duty_percent)
                    self.wf_symmetry_edit.setText(snap.ramp_symmetry_percent)
                    self.wf_pulse_width_edit.setText(snap.pulse_width)
                    self.wf_pulse_lead_edit.setText(snap.pulse_leading)
                    self.wf_pulse_trail_edit.setText(snap.pulse_trailing)
            finally:
                self._syncing_levels = False
            self._update_waveform_controls_visibility()

        self._recalculate()
        self.status.emit(
            f"Imported Rigol CH{channel} ({waveform}, LOAD {snap.output_load}) into Load Calculator."
        )
        return True

    # ------------------------------------------------------------------
    # Results FileBrowserPanel + MOKE Box & Keithley B field selectors
    # ------------------------------------------------------------------
    def _cancel_scan(self) -> None:
        self._scan_request_id += 1
        if self._scan_task is not None:
            self._scan_task.cancel()
            self._scan_task = None
        self._is_scanning = False

    def shutdown(self) -> bool:
        self._cancel_scan()
        self.file_browser._cancel_refresh()
        ok_browser = self.file_browser._read_pool.waitForDone(3_000)
        ok_scan = self._scan_pool.waitForDone(3_000)
        return ok_browser and ok_scan

    def _on_browser_files_loaded(self, has_files: bool) -> None:
        if not has_files:
            self._saved_curves = ()
            self._populate_field_selectors(None)
            self.scan_status_label.setText(
                "No recorded measurements in this folder. Switch folder, open a CSV, or use Fixed R."
            )
            return
        count = len(self.file_browser._all_summaries) + sum(
            1
            for p in self.file_browser._catalogue_artifacts
            if p.suffix.lower() == ".csv" or p.name.lower() == "dataset.json"
        )
        if count <= _AUTO_FOLDER_SCAN_MAX_FILES:
            self.scan_current_browser_folder()
        else:
            self.scan_status_label.setText(
                f"{count} files in folder. Click a run above to load its MOKE / Keithley B curves, "
                "or click 'Scan folder conditions'."
            )

    def scan_current_browser_folder(self) -> None:
        """Scan only the currently browsed folder (Active DUT by default) for MOKE & Keithley B curves."""
        folder = self.file_browser.output_dir
        self._cancel_scan()
        self._scan_request_id += 1
        self._is_scanning = True
        self.scan_status_label.setText(f"Indexing MOKE / Keithley B curves in {folder.name}…")
        task = _LibraryScanTask(
            self._scan_request_id,
            self._scanner.scan,
            roots=(folder,),
            inventory_store=None,
        )
        self._scan_task = task
        task.signals.loaded.connect(self._on_folder_scan_loaded)
        task.signals.failed.connect(self._on_scan_failed)
        task.signals.finished.connect(self._on_scan_finished)
        self._scan_pool.start(task)

    def _on_browser_file_selected(self, path_obj: object) -> None:
        if self._syncing_browser_selection or not isinstance(path_obj, Path):
            return
        target = path_obj.resolve()
        # Fast path: if this file is already in our scanned curves, jump straight to its field condition.
        matching = [
            c for c in self._saved_curves if c.source_path is not None and c.source_path.resolve() == target
        ]
        if matching:
            self._select_saved_curve_in_combos(matching[0])
            return

        suffix = target.suffix.lower()
        if suffix not in {".h5", ".hdf5", ".csv", ".json"}:
            return
        self._load_single_browser_file(target)

    def _load_single_browser_file(self, path: Path) -> None:
        self._cancel_scan()
        self._scan_request_id += 1
        self._is_scanning = True
        self.scan_status_label.setText(f"Reading MOKE / Keithley B conditions from {path.name}…")

        def _read_one(*, cancelled: Callable[[], bool] = lambda: False) -> LibraryScanResult:
            if cancelled():
                raise InterruptedError("Cancelled")
            suffix = path.suffix.lower()
            if suffix in {".h5", ".hdf5"}:
                curves = load_hdf5_reference_curves(path)
            elif path.name.lower() == "dataset.json":
                curves = (load_characterization_json_curve(path),)
            elif suffix == ".csv":
                curves = load_csv_reference_curves(path)
            else:
                curves = ()
            return LibraryScanResult(curves=curves)

        task = _LibraryScanTask(self._scan_request_id, _read_one)
        self._scan_task = task
        task.signals.loaded.connect(self._on_single_file_loaded)
        task.signals.failed.connect(self._on_scan_failed)
        task.signals.finished.connect(self._on_scan_finished)
        self._scan_pool.start(task)

    def _on_folder_scan_loaded(self, request_id: int, payload: object) -> None:
        if request_id != self._scan_request_id or not isinstance(payload, LibraryScanResult):
            return
        self._saved_curves = payload.curves
        skipped = len(payload.skipped_files)
        extra = f" · {skipped} skipped" if skipped else ""
        if self._saved_curves:
            self.scan_status_label.setText(
                f"{len(self._saved_curves)} field-condition curve(s) ready in {self.file_browser.output_dir.name}{extra}."
            )
        else:
            self.scan_status_label.setText(
                f"No Keithley I–V curves found in {self.file_browser.output_dir.name}{extra}."
            )
        preferred = self._selected_saved_curve.curve_id if self._selected_saved_curve else None
        self._populate_field_selectors(preferred)

    def _on_single_file_loaded(self, request_id: int, payload: object) -> None:
        if request_id != self._scan_request_id or not isinstance(payload, LibraryScanResult):
            return
        if not payload.curves:
            self.scan_status_label.setText("Selected file has no Keithley MTJ I–V curve.")
            return
        self._saved_curves = payload.curves
        self.scan_status_label.setText(
            f"Loaded {len(payload.curves)} field condition(s) from selected file."
        )
        self._populate_field_selectors(payload.curves[0].curve_id)

    def _on_scan_failed(self, request_id: int, message: str) -> None:
        if request_id != self._scan_request_id:
            return
        self.scan_status_label.setText(f"Could not read reference curves: {message}")

    def _on_scan_finished(self, request_id: int) -> None:
        if request_id == self._scan_request_id:
            self._is_scanning = False
            self._scan_task = None

    def _populate_field_selectors(self, preferred_curve_id: str | None) -> None:
        moke_groups: dict[str, list[ReferenceIVCurve]] = {}
        moke_repr_v: dict[str, float | None] = {}
        for curve in self._saved_curves:
            key = _moke_group_key(curve.moke_vout_v)
            moke_groups.setdefault(key, []).append(curve)
            if key not in moke_repr_v:
                moke_repr_v[key] = curve.moke_vout_v

        # Sort MOKE groups numerically, putting "Not recorded" last.
        ordered_keys = sorted(
            moke_groups.keys(),
            key=lambda k: (1, 0.0) if k == _MOKE_NONE_KEY else (0, float(moke_repr_v[k] or 0.0)),
        )

        with QSignalBlocker(self.moke_combo):
            self.moke_combo.clear()
            if len(ordered_keys) > 1:
                self.moke_combo.addItem(
                    f"All MOKE settings ({len(self._saved_curves)})", userData=_MOKE_ALL_KEY
                )
            for key in ordered_keys:
                label = _moke_group_label(key, moke_repr_v[key])
                count = len(moke_groups[key])
                suffix = f" ({count})" if count > 1 else ""
                self.moke_combo.addItem(f"{label}{suffix}", userData=key)

            if preferred_curve_id is not None:
                pref_curve = next(
                    (c for c in self._saved_curves if c.curve_id == preferred_curve_id), None
                )
                if pref_curve is not None:
                    pref_key = _moke_group_key(pref_curve.moke_vout_v)
                    idx = self.moke_combo.findData(pref_key)
                    if idx >= 0:
                        self.moke_combo.setCurrentIndex(idx)

        self._rebuild_kb_combo(preferred_curve_id=preferred_curve_id)

    def _rebuild_kb_combo(self, *, preferred_curve_id: str | None = None) -> None:
        selected_moke_key = str(self.moke_combo.currentData() or _MOKE_ALL_KEY)
        if selected_moke_key == _MOKE_ALL_KEY:
            candidates = list(self._saved_curves)
        else:
            candidates = [
                c for c in self._saved_curves if _moke_group_key(c.moke_vout_v) == selected_moke_key
            ]

        candidates.sort(
            key=lambda c: (
                c.keithley_b_setpoint_a is None and c.keithley_b_measured_a is None,
                float(
                    c.keithley_b_setpoint_a
                    if c.keithley_b_setpoint_a is not None
                    else (c.keithley_b_measured_a or 0.0)
                ),
                c.title.casefold(),
            )
        )
        self._visible_kb_curves = tuple(candidates)

        with QSignalBlocker(self.kb_combo):
            self.kb_combo.clear()
            restore_idx = 0
            for idx, curve in enumerate(self._visible_kb_curves):
                self.kb_combo.addItem(_kb_curve_label(curve), userData=curve.curve_id)
                if preferred_curve_id is not None and curve.curve_id == preferred_curve_id:
                    restore_idx = idx
            if self._visible_kb_curves:
                self.kb_combo.setCurrentIndex(restore_idx)

        if self._visible_kb_curves:
            self._selected_saved_curve = self._visible_kb_curves[self.kb_combo.currentIndex()]
        else:
            self._selected_saved_curve = None

        if self._source_mode == "saved":
            self._refresh_pass_selector()
            self._recalculate()

    def _select_saved_curve_in_combos(self, curve: ReferenceIVCurve) -> None:
        moke_key = _moke_group_key(curve.moke_vout_v)
        with QSignalBlocker(self.moke_combo):
            idx = self.moke_combo.findData(moke_key)
            if idx >= 0:
                self.moke_combo.setCurrentIndex(idx)
        self._rebuild_kb_combo(preferred_curve_id=curve.curve_id)

    def _on_moke_selection_changed(self, *_args: object) -> None:
        prev_id = self._selected_saved_curve.curve_id if self._selected_saved_curve else None
        self._rebuild_kb_combo(preferred_curve_id=prev_id)

    def _on_kb_selection_changed(self, index: int) -> None:
        if 0 <= index < len(self._visible_kb_curves):
            curve = self._visible_kb_curves[index]
            self._selected_saved_curve = curve
            if curve.source_path is not None:
                self._syncing_browser_selection = True
                try:
                    self.file_browser._restore_selection(curve.source_path)
                finally:
                    self._syncing_browser_selection = False
            if self._source_mode == "saved":
                self._refresh_pass_selector()
                self._recalculate()

    # ------------------------------------------------------------------
    # CSV loading & pass selection
    # ------------------------------------------------------------------
    def _open_csv_dialog(self) -> None:
        start_dir = str(self.file_browser.output_dir)
        selected, _ = StationFileDialog.getOpenFileName(
            self,
            "Select Keithley CSV reference curve",
            start_dir,
            "CSV files (*.csv);;All files (*)",
        )
        if selected:
            self.load_csv_file(selected)

    def load_csv_file(self, path: str | Path) -> bool:
        """Load a sweep or characterization CSV into the CSV reference mode."""
        try:
            curves = load_csv_reference_curves(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.banner.show_message(f"Could not load CSV: {exc}", severity="error")
            return False

        self._csv_curves = curves
        self._selected_csv_curve = curves[0]
        self.csv_path_label.setText(f"Loaded: {Path(path).name} ({len(curves)} slice(s))")
        with QSignalBlocker(self.csv_slice_combo):
            self.csv_slice_combo.clear()
            for curve in curves:
                self.csv_slice_combo.addItem(curve.title)
        multi = len(curves) > 1
        self.csv_slice_label.setVisible(multi)
        self.csv_slice_combo.setVisible(multi)
        self.set_source_mode("csv")
        self.status.emit(f"Loaded MTJ reference CSV: {Path(path).name}")
        return True

    def _on_csv_slice_changed(self, index: int) -> None:
        if 0 <= index < len(self._csv_curves):
            self._selected_csv_curve = self._csv_curves[index]
            if self._source_mode == "csv":
                self._refresh_pass_selector()
                self._recalculate()

    def _refresh_pass_selector(self) -> None:
        curve = self.active_curve
        with QSignalBlocker(self.pass_combo):
            self.pass_combo.clear()
            if curve is not None:
                for p in curve.passes:
                    self.pass_combo.addItem(f"{p.label} · {len(p.points)} pts")
        if curve is None:
            self.reference_summary_label.setText("No measured curve selected.")
            self.use_curve_r_range_button.setEnabled(False)
            return

        kb = (
            curve.keithley_b_setpoint_a
            if curve.keithley_b_setpoint_a is not None
            else curve.keithley_b_measured_a
        )
        parts = [curve.title]
        if curve.coordinate_label:
            parts.append(curve.coordinate_label)
        if kb is not None:
            parts.append(f"Keithley B: {_fmt(kb, DIMENSION_CURRENT, precision=4)}")
        if curve.moke_vout_v is not None:
            parts.append(f"MOKE VOUT0: {_fmt(curve.moke_vout_v, DIMENSION_VOLTAGE, precision=5)}")
        stats = curve.resistance_stats
        if stats is not None:
            parts.append(
                f"R: {stats.min_ohm:.2f}–{stats.max_ohm:.2f} Ω (mean {stats.mean_ohm:.2f} Ω)"
            )
        if curve.excluded_point_count:
            parts.append(f"{curve.excluded_point_count} excluded")
        self.reference_summary_label.setText(" · ".join(parts))
        self.use_curve_r_range_button.setEnabled(stats is not None)

    def _copy_curve_resistance_to_fixed_r(self) -> None:
        curve = self.active_curve
        stats = curve.resistance_stats if curve is not None else None
        if stats is None:
            return
        with (
            QSignalBlocker(self.r_nominal_edit),
            QSignalBlocker(self.r_min_edit),
            QSignalBlocker(self.r_max_edit),
        ):
            self.r_nominal_edit.setText(f"{stats.mean_ohm:.4g} ohm")
            self.r_min_edit.setText(f"{stats.min_ohm:.4g} ohm")
            self.r_max_edit.setText(f"{stats.max_ohm:.4g} ohm")
        self.set_source_mode("resistor")

    # ------------------------------------------------------------------
    # Calculation engine binding
    # ------------------------------------------------------------------
    def _build_active_model(self) -> ReferenceCurvePass | ConstantResistanceModel:
        if self._source_mode == "resistor":
            r_nom = parse_quantity(self.r_nominal_edit.text(), DIMENSION_RESISTANCE).si_value
            min_txt = self.r_min_edit.text().strip()
            max_txt = self.r_max_edit.text().strip()
            if bool(min_txt) != bool(max_txt):
                raise ValueError("Enter both Min R and Max R bounds, or leave both empty.")
            r_min = parse_quantity(min_txt, DIMENSION_RESISTANCE).si_value if min_txt else None
            r_max = parse_quantity(max_txt, DIMENSION_RESISTANCE).si_value if max_txt else None
            return ConstantResistanceModel(r_nom, r_min, r_max)

        curve_pass = self.active_pass
        if curve_pass is None:
            raise ValueError(
                "Select a saved measurement / field condition, open a CSV file, or switch to Fixed R."
            )
        return curve_pass

    def _on_manual_span_edited(self, *_args: object) -> None:
        if self.auto_span_from_limit_check.isChecked():
            with QSignalBlocker(self.auto_span_from_limit_check):
                self.auto_span_from_limit_check.setChecked(False)

    def _on_auto_span_toggled(self, checked: bool) -> None:
        if checked:
            self._recalculate()

    def _on_set_rigol_limit_clicked(self) -> None:
        estimate = self.last_limit_estimate
        if estimate is None or estimate.rigol_symmetric_limit_v <= 0.0:
            self.banner.show_message(
                "Calculate a valid MTJ voltage limit before staging a Rigol voltage limit.",
                severity="warning",
            )
            return
        channel = int(self.channel_combo.currentData() or 1)
        self.rigol_voltage_limit_requested.emit(
            channel,
            estimate.rigol_symmetric_limit_v,
            estimate,
        )

    def _configured_rigol_limit_text(self, channel: int) -> str:
        ch_cfg = self._settings.rigol.safety.channels.get(str(channel))
        if ch_cfg is None:
            return "—"
        try:
            lim_v = parse_quantity(ch_cfg.lab_limits.combined_voltage_limit, DIMENSION_VOLTAGE).si_value
            return f"±{_fmt(abs(lim_v), DIMENSION_VOLTAGE)}"
        except QuantityError:
            return f"±{ch_cfg.lab_limits.combined_voltage_limit}"

    def _update_mtj_limit_estimate(
        self,
        model: ReferenceCurvePass | ConstantResistanceModel,
        load_wire_value: str | float,
        channel: int,
    ) -> list[str]:
        """Compute conservative Rigol V_set limits for the MTJ min/max voltage window."""
        self.last_limit_estimate = None
        extra_warnings: list[str] = []
        configured_txt = self._configured_rigol_limit_text(channel)
        try:
            mtj_min_v = parse_quantity(self.mtj_v_min_edit.text(), DIMENSION_VOLTAGE).si_value
            mtj_max_v = parse_quantity(self.mtj_v_max_edit.text(), DIMENSION_VOLTAGE).si_value
            estimate = compute_rigol_voltage_limit_for_mtj(
                model,
                mtj_min_v=mtj_min_v,
                mtj_max_v=mtj_max_v,
                output_load=load_wire_value,
            )
        except (ValueError, TypeError, QuantityError, CalculatorRangeError) as exc:
            self.rigol_limit_summary_label.setText(
                f"MTJ voltage bounds invalid: {exc} · Configured CH{channel}: {configured_txt}"
            )
            self._sync_set_limit_button_state()
            return extra_warnings

        self.last_limit_estimate = estimate
        if self.auto_span_from_limit_check.isChecked():
            with (
                QSignalBlocker(self.resistor_span_min_edit),
                QSignalBlocker(self.resistor_span_max_edit),
            ):
                self.resistor_span_min_edit.setText(
                    format_quantity_auto(estimate.rigol_min_v, DIMENSION_VOLTAGE, precision=4)
                )
                self.resistor_span_max_edit.setText(
                    format_quantity_auto(estimate.rigol_max_v, DIMENSION_VOLTAGE, precision=4)
                )

        parts = [
            (
                f"Safe Rigol CH{channel} ({estimate.load.raw_label}): "
                f"±{_fmt(estimate.rigol_symmetric_limit_v, DIMENSION_VOLTAGE)} "
                f"(span {_fmt_range(estimate.rigol_min_v, estimate.rigol_max_v, DIMENSION_VOLTAGE)}, "
                f"Voc ±{_fmt(estimate.open_circuit_limit_v, DIMENSION_VOLTAGE)})"
            ),
            f"I_MTJ ≤ {_fmt(estimate.estimated_current_max_a, DIMENSION_CURRENT)}",
            f"Configured CH{channel}: {configured_txt}",
        ]
        if estimate.governing_resistance_ohm is not None:
            parts.append(f"at R_max = {estimate.governing_resistance_ohm:.4g} Ω")
        if estimate.clamped_to_measured_range:
            parts.append(
                f"clamped to measured {_fmt_range(estimate.effective_mtj_min_v, estimate.effective_mtj_max_v, DIMENSION_VOLTAGE)} (no extrapolation)"
            )
        if estimate.clamped_to_hardware_limit:
            parts.append("clamped to DG1032Z ±10 V Voc")
        self.rigol_limit_summary_label.setText(" · ".join(parts))
        self._sync_set_limit_button_state()
        return extra_warnings

    def _resistor_plot_span(
        self,
        highlight_values: Sequence[float] = (),
        *,
        open_circuit_scale: float = 1.0,
    ) -> tuple[float, float]:
        try:
            low_v = parse_quantity(self.resistor_span_min_edit.text(), DIMENSION_VOLTAGE).si_value
            high_v = parse_quantity(self.resistor_span_max_edit.text(), DIMENSION_VOLTAGE).si_value
            if not (math.isfinite(low_v) and math.isfinite(high_v)) or low_v >= high_v:
                low_v, high_v = -1.0, 1.0
        except QuantityError:
            low_v, high_v = -1.0, 1.0
        finite = [v for v in highlight_values if math.isfinite(v)]
        if finite:
            low_v = min(low_v, min(finite))
            high_v = max(high_v, max(finite))
            if math.isclose(low_v, high_v):
                pad = max(abs(low_v) * 0.25, 0.1)
                low_v -= pad
                high_v += pad
        _ = open_circuit_scale
        return low_v, high_v

    def _resistor_span_warning(self, open_circuit_scale: float) -> str | None:
        if self._source_mode != "resistor":
            return None
        try:
            low_v = parse_quantity(self.resistor_span_min_edit.text(), DIMENSION_VOLTAGE).si_value
            high_v = parse_quantity(self.resistor_span_max_edit.text(), DIMENSION_VOLTAGE).si_value
        except QuantityError:
            return "Rigol V_set span requires explicit voltage units (e.g. -950 mV … 950 mV)."
        if not (math.isfinite(low_v) and math.isfinite(high_v)) or low_v >= high_v:
            return "Rigol V_set span requires min < max."
        max_oc = max(abs(low_v), abs(high_v)) * open_circuit_scale
        if max_oc > DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V + 1e-9:
            return (
                f"Rigol V_set span ({_fmt_range(low_v, high_v, DIMENSION_VOLTAGE)}) exceeds "
                f"DG1032Z ±{DG1032Z_MAX_OPEN_CIRCUIT_PEAK_V:g} V open-circuit ceiling — check V vs mV units."
            )
        return None

    def _clear_kpis(self) -> None:
        self.kpi_rigol.set_values("—", "")
        self.kpi_voltage.set_values("—", "")
        self.kpi_current.set_values("—", "")
        self.kpi_power.set_values("—", "")

    def _recalculate(self, *_args: object) -> None:
        self.last_dc_result = None
        self.last_waveform_result = None
        self.last_limit_estimate = None
        self._last_transfer_series = None
        self.banner.clear_message()
        self.dc_range_label.setText("")

        channel = int(self.channel_combo.currentData() or 1)
        try:
            model = self._build_active_model()
            load_spec = parse_rigol_load(self._current_load_raw())
        except (ValueError, TypeError, QuantityError) as exc:
            self._clear_kpis()
            self.rigol_limit_summary_label.setText(
                f"Select a load model to calculate safe Rigol limits · Configured CH{channel}: "
                f"{self._configured_rigol_limit_text(channel)}"
            )
            self._sync_set_limit_button_state()
            if self._source_mode == "resistor":
                self.banner.show_message(str(exc), severity="error")
            self._redraw_plots()
            return

        # Compute conservative Rigol voltage bounds from MTJ min/max voltage BEFORE building the resistor span.
        self._update_mtj_limit_estimate(model, load_spec.wire_value, channel)
        span_warning = self._resistor_span_warning(load_spec.open_circuit_scale)

        # Precompute transfer series even if the operating point input is temporarily out-of-range.
        try:
            if isinstance(model, ReferenceCurvePass):
                self._last_transfer_series = curve_transfer_series(model, load_spec.wire_value)
            else:
                self._last_transfer_series = curve_transfer_series(
                    model,
                    load_spec.wire_value,
                    setting_range_v=self._resistor_plot_span(
                        open_circuit_scale=load_spec.open_circuit_scale
                    ),
                )
        except (ValueError, TypeError):
            self._last_transfer_series = None

        if isinstance(model, ReferenceCurvePass) and self._last_transfer_series is not None:
            key = str(self.dc_direction_combo.currentData() or "setting")
            if key == "setting":
                lo, hi = (
                    min(self._last_transfer_series.rigol_setpoint_v),
                    max(self._last_transfer_series.rigol_setpoint_v),
                )
                dim = DIMENSION_VOLTAGE
            elif key == "voltage":
                lo, hi = (
                    min(self._last_transfer_series.mtj_voltage_v),
                    max(self._last_transfer_series.mtj_voltage_v),
                )
                dim = DIMENSION_VOLTAGE
            else:
                lo, hi = (
                    min(self._last_transfer_series.mtj_current_a),
                    max(self._last_transfer_series.mtj_current_a),
                )
                dim = DIMENSION_CURRENT
            self.dc_range_label.setText(f"Measured range (no extrapolation): {_fmt_range(lo, hi, dim)}")

        if self._signal_mode == "dc":
            self._solve_dc(model, load_spec.wire_value, channel, extra_advisory=span_warning)
        else:
            self._solve_waveform(model, load_spec.wire_value, channel, extra_advisory=span_warning)

        self._redraw_plots()

    def _solve_dc(
        self,
        model: ReferenceCurvePass | ConstantResistanceModel,
        output_load: str | float,
        channel: int,
        *,
        extra_advisory: str | None = None,
    ) -> None:
        key = str(self.dc_direction_combo.currentData() or "setting")
        dim = DIMENSION_CURRENT if key == "current" else DIMENSION_VOLTAGE
        try:
            target_si = parse_quantity(self.dc_value_edit.text(), dim).si_value
            res = solve_dc_point(
                model,
                input_key=key,
                target_si=target_si,
                output_load=output_load,
                settings=self._settings,
                channel=channel,
            )
        except (ValueError, QuantityError, CalculatorRangeError, AmbiguousBranchError) as exc:
            self._clear_kpis()
            self.banner.show_message(str(exc), severity="error")
            return

        self.last_dc_result = res
        pt = res.point
        if isinstance(model, ConstantResistanceModel):
            self._last_transfer_series = curve_transfer_series(
                model,
                res.load.wire_value,
                setting_range_v=self._resistor_plot_span((pt.rigol_setpoint_v,)),
            )

        band = res.band
        rigol_detail = f"LOAD {res.load.raw_label} · Voc {_fmt(pt.open_circuit_v, DIMENSION_VOLTAGE)}"
        if band is not None and key != "setting":
            rigol_detail += (
                f"\nBand: {_fmt_range(band.rigol_setpoint_min_v, band.rigol_setpoint_max_v, DIMENSION_VOLTAGE)}"
            )
        self.kpi_rigol.set_values(_fmt(pt.rigol_setpoint_v, DIMENSION_VOLTAGE), rigol_detail)

        ratio_txt = (
            f"{pt.voltage_transfer_ratio * 100.0:.1f}% of V_set"
            if pt.voltage_transfer_ratio is not None
            else "—"
        )
        v_detail = ratio_txt
        if band is not None and key != "voltage":
            v_detail += (
                f"\nBand: {_fmt_range(band.mtj_voltage_min_v, band.mtj_voltage_max_v, DIMENSION_VOLTAGE)}"
            )
        self.kpi_voltage.set_values(_fmt(pt.mtj_voltage_v, DIMENSION_VOLTAGE), v_detail)

        r_txt = f"R = {pt.static_resistance_ohm:.2f} Ω" if pt.static_resistance_ohm is not None else "R: —"
        i_detail = r_txt
        if band is not None and key != "current":
            i_detail += (
                f"\nBand: {_fmt_range(band.mtj_current_min_a, band.mtj_current_max_a, DIMENSION_CURRENT)}"
            )
        self.kpi_current.set_values(_fmt(pt.mtj_current_a, DIMENSION_CURRENT), i_detail)

        gamma_txt = f"Γ = {pt.reflection_gamma:+.3f}" if pt.reflection_gamma is not None else "Γ = —"
        rdiff_txt = (
            f" · dV/dI = {pt.differential_resistance_ohm:.1f} Ω"
            if pt.differential_resistance_ohm is not None
            else ""
        )
        self.kpi_power.set_values(
            _fmt(pt.mtj_power_w, DIMENSION_POWER),
            f"{gamma_txt}{rdiff_txt}",
        )

        advisories = list(pt.warnings)
        if extra_advisory:
            advisories.append(extra_advisory)
        if self.active_curve is not None and self.active_curve.sense_mode == "4wire":
            advisories.append(
                "4-wire (Kelvin) dataset (conditional_external_drops_unknown): lead/contact drops in series with Rigol 50 Ω are excluded from V_MTJ."
            )
        advisories.extend(self._import_advisories)
        if res.advisory_safety_message:
            advisories.append(f"CH{channel} station limit advisory: {res.advisory_safety_message}")
        if advisories:
            self.banner.show_message(" · ".join(dict.fromkeys(advisories)), severity="warning")

    def _solve_waveform(
        self,
        model: ReferenceCurvePass | ConstantResistanceModel,
        output_load: str | float,
        channel: int,
        *,
        extra_advisory: str | None = None,
    ) -> None:
        wf = self.waveform_combo.currentText().strip().upper()
        entry = str(self.level_entry_combo.currentData() or "vpp_offset")
        try:
            if wf == "DC":
                offset_v = parse_quantity(self.wf_offset_edit.text(), DIMENSION_VOLTAGE).si_value
                high_v = low_v = offset_v
                freq_hz = 1_000.0
            else:
                if entry == "vpp_offset":
                    vpp_v = parse_quantity(self.wf_vpp_edit.text(), DIMENSION_VOLTAGE).si_value
                    offset_v = parse_quantity(self.wf_offset_edit.text(), DIMENSION_VOLTAGE).si_value
                    high_v = offset_v + 0.5 * vpp_v
                    low_v = offset_v - 0.5 * vpp_v
                else:
                    high_v = parse_quantity(self.wf_high_edit.text(), DIMENSION_VOLTAGE).si_value
                    low_v = parse_quantity(self.wf_low_edit.text(), DIMENSION_VOLTAGE).si_value
                freq_hz = parse_quantity(self.wf_freq_edit.text(), DIMENSION_FREQUENCY).si_value

            duty = float(self.wf_duty_edit.text().strip().replace(",", "."))
            sym = float(self.wf_symmetry_edit.text().strip().replace(",", "."))
            pulse_width_s = (
                parse_quantity(self.wf_pulse_width_edit.text(), DIMENSION_TIME).si_value
                if wf == "PULS"
                else None
            )
            pulse_lead_s = (
                parse_quantity(self.wf_pulse_lead_edit.text(), DIMENSION_TIME).si_value
                if wf == "PULS" and self.wf_pulse_lead_edit.text().strip()
                else 0.0
            )
            pulse_trail_s = (
                parse_quantity(self.wf_pulse_trail_edit.text(), DIMENSION_TIME).si_value
                if wf == "PULS" and self.wf_pulse_trail_edit.text().strip()
                else 0.0
            )

            req = WaveformRequest(
                waveform=wf,
                high_v=high_v,
                low_v=low_v,
                output_load=output_load,
                frequency_hz=freq_hz,
                duty_percent=duty,
                ramp_symmetry_percent=sym,
                pulse_width_s=pulse_width_s,
                pulse_leading_s=pulse_lead_s,
                pulse_trailing_s=pulse_trail_s,
                inverted=self._waveform_inverted,
            )
            res = solve_waveform(model, req, settings=self._settings, channel=channel)
        except (ValueError, QuantityError, CalculatorRangeError, AmbiguousBranchError) as exc:
            self._clear_kpis()
            self.banner.show_message(str(exc), severity="error")
            return

        self.last_waveform_result = res
        if isinstance(model, ConstantResistanceModel):
            self._last_transfer_series = curve_transfer_series(
                model,
                res.load.wire_value,
                setting_range_v=self._resistor_plot_span((req.low_v, req.high_v)),
            )

        env = res.nominal
        band_env = res.resistance_range
        self.kpi_rigol.set_values(
            _fmt_range(req.low_v, req.high_v, DIMENSION_VOLTAGE),
            f"{wf} · LOAD {res.load.raw_label} · Vpp {_fmt(req.high_v - req.low_v, DIMENSION_VOLTAGE)}",
        )
        v_detail = (
            f"Peak {_fmt(env.mtj_voltage_peak_v, DIMENSION_VOLTAGE)} · "
            f"Vpp {_fmt(env.mtj_voltage_vpp_v, DIMENSION_VOLTAGE)}"
        )
        if band_env is not None:
            v_detail += f"\nR band peak: {_fmt(band_env.mtj_voltage_peak_v, DIMENSION_VOLTAGE)}"
        self.kpi_voltage.set_values(
            _fmt_range(env.mtj_voltage_min_v, env.mtj_voltage_max_v, DIMENSION_VOLTAGE),
            v_detail,
        )
        i_detail = (
            f"Peak {_fmt(env.mtj_current_peak_a, DIMENSION_CURRENT)} · "
            f"RMS {_fmt(env.mtj_current_rms_a, DIMENSION_CURRENT)}"
        )
        if band_env is not None:
            i_detail += f"\nR band peak: {_fmt(band_env.mtj_current_peak_a, DIMENSION_CURRENT)}"
        self.kpi_current.set_values(
            _fmt_range(env.mtj_current_min_a, env.mtj_current_max_a, DIMENSION_CURRENT),
            i_detail,
        )
        p_detail = f"Peak {_fmt(env.mtj_power_peak_w, DIMENSION_POWER)}"
        if res.high_point.reflection_gamma is not None:
            p_detail += f" · Γ = {res.high_point.reflection_gamma:+.3f}"
        self.kpi_power.set_values(
            f"Avg {_fmt(env.mtj_power_avg_w, DIMENSION_POWER)}",
            p_detail,
        )

        advisories: list[str] = []
        if extra_advisory:
            advisories.append(extra_advisory)
        if res.exceeds_hardware_oc_limit:
            advisories.append("Waveform exceeds DG1032Z ±10 V open-circuit hardware ceiling.")
        if self.active_curve is not None and self.active_curve.sense_mode == "4wire":
            advisories.append(
                "4-wire (Kelvin) dataset (conditional_external_drops_unknown): lead/contact drops in series with Rigol 50 Ω are excluded."
            )
        for w in res.warnings:
            if not w.startswith("Quasi-static resistive/I-V estimate"):
                advisories.append(w)
        advisories.extend(self._import_advisories)
        if res.advisory_safety_message:
            advisories.append(f"CH{channel} station limit advisory: {res.advisory_safety_message}")
        if advisories:
            self.banner.show_message(" · ".join(dict.fromkeys(advisories)), severity="warning")

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------
    def _reset_transfer_plots(self) -> None:
        for plot, cx, cy in (
            (self.voltage_plot, self._v_cross_x, self._v_cross_y),
            (self.current_plot, self._i_cross_x, self._i_cross_y),
        ):
            plot.clear()
            plot.addItem(cx, ignoreBounds=True)
            plot.addItem(cy, ignoreBounds=True)
            cx.hide()
            cy.hide()

    def _redraw_plots(self, *_args: object) -> None:
        tokens = tokens_for(self._theme)
        palette = plot_theme(tokens)
        if self._plot_view_mode == "transfer":
            self._draw_transfer_plots(tokens, palette)
        else:
            self._draw_reference_plots(tokens, palette)

    def _draw_transfer_plots(self, tokens, palette) -> None:
        self._reset_transfer_plots()
        series = self._last_transfer_series
        if series is None or len(series.rigol_setpoint_v) < 2:
            return

        x = np.asarray(series.rigol_setpoint_v, dtype=float)
        yv = np.asarray(series.mtj_voltage_v, dtype=float)
        yi = np.asarray(series.mtj_current_a, dtype=float)

        band_color = QColor(palette.measurement)
        band_color.setAlpha(45)
        band_brush = pg.mkBrush(band_color)
        if series.band_low_voltage_v is not None and series.band_high_voltage_v is not None:
            v_lo = pg.PlotDataItem(x, np.asarray(series.band_low_voltage_v, dtype=float))
            v_hi = pg.PlotDataItem(x, np.asarray(series.band_high_voltage_v, dtype=float))
            self.voltage_plot.addItem(pg.FillBetweenItem(v_lo, v_hi, brush=band_brush))
        if series.band_low_current_a is not None and series.band_high_current_a is not None:
            i_band_color = QColor(palette.success)
            i_band_color.setAlpha(45)
            i_lo = pg.PlotDataItem(x, np.asarray(series.band_low_current_a, dtype=float))
            i_hi = pg.PlotDataItem(x, np.asarray(series.band_high_current_a, dtype=float))
            self.current_plot.addItem(pg.FillBetweenItem(i_lo, i_hi, brush=pg.mkBrush(i_band_color)))

        if self.unity_line_check.isChecked():
            unity_pen = pg.mkPen(palette.axes, width=1.2, style=Qt.PenStyle.DashLine)
            self.voltage_plot.plot(
                [float(np.min(x)), float(np.max(x))],
                [float(np.min(x)), float(np.max(x))],
                pen=unity_pen,
                name="V_MTJ = V_set (1:1)",
            )

        self.voltage_plot.plot(
            x,
            yv,
            pen=pg.mkPen(palette.measurement, width=2.2),
            name="Estimated V_MTJ",
        )
        self.current_plot.plot(
            x,
            yi,
            pen=pg.mkPen(palette.success, width=2.2),
            name="Estimated I_MTJ",
        )

        # Draw calculated conservative Rigol ±V_set and MTJ V_min/V_max safety guide lines
        if self.last_limit_estimate is not None and self.last_limit_estimate.rigol_symmetric_limit_v > 0.0:
            est = self.last_limit_estimate
            limit_pen = pg.mkPen(palette.danger, width=1.1, style=Qt.PenStyle.DashLine)
            mtj_pen = pg.mkPen(palette.danger, width=0.9, style=Qt.PenStyle.DotLine)
            for bound_x, tag, pos in (
                (-est.rigol_symmetric_limit_v, "−V_lim", 0.90),
                (est.rigol_symmetric_limit_v, "+V_lim", 0.12),
            ):
                self.voltage_plot.addItem(
                    pg.InfiniteLine(
                        pos=bound_x,
                        angle=90,
                        movable=False,
                        pen=limit_pen,
                        label=tag,
                        labelOpts={"position": pos, "color": palette.danger},
                    ),
                    ignoreBounds=True,
                )
                self.current_plot.addItem(
                    pg.InfiniteLine(pos=bound_x, angle=90, movable=False, pen=limit_pen),
                    ignoreBounds=True,
                )
            for bound_y in (est.effective_mtj_min_v, est.effective_mtj_max_v):
                self.voltage_plot.addItem(
                    pg.InfiniteLine(pos=bound_y, angle=0, movable=False, pen=mtj_pen),
                    ignoreBounds=True,
                )

        marker_color = (
            palette.danger
            if (
                (self.last_dc_result and self.last_dc_result.point.exceeds_hardware_oc_limit)
                or (self.last_waveform_result and self.last_waveform_result.exceeds_hardware_oc_limit)
            )
            else tokens.caution
        )
        marker_pen = pg.mkPen(marker_color, width=1.4, style=Qt.PenStyle.DotLine)
        marker_brush = pg.mkBrush(marker_color)

        if self._signal_mode == "dc" and self.last_dc_result is not None:
            pt = self.last_dc_result.point
            for plot, y_val in (
                (self.voltage_plot, pt.mtj_voltage_v),
                (self.current_plot, pt.mtj_current_a),
            ):
                plot.addItem(
                    pg.InfiniteLine(pos=pt.rigol_setpoint_v, angle=90, movable=False, pen=marker_pen)
                )
                plot.plot(
                    [pt.rigol_setpoint_v],
                    [y_val],
                    pen=None,
                    symbol="o",
                    symbolSize=9,
                    symbolBrush=marker_brush,
                    symbolPen=pg.mkPen(palette.background, width=1.5),
                    name="Operating point",
                )
        elif self._signal_mode == "waveform" and self.last_waveform_result is not None:
            wf_res = self.last_waveform_result
            lo_pt, hi_pt = wf_res.low_point, wf_res.high_point
            region_color = QColor(marker_color)
            region_color.setAlpha(38)
            for plot, y_lo, y_hi in (
                (self.voltage_plot, lo_pt.mtj_voltage_v, hi_pt.mtj_voltage_v),
                (self.current_plot, lo_pt.mtj_current_a, hi_pt.mtj_current_a),
            ):
                region = pg.LinearRegionItem(
                    values=(lo_pt.rigol_setpoint_v, hi_pt.rigol_setpoint_v),
                    movable=False,
                    brush=pg.mkBrush(region_color),
                    pen=pg.mkPen(marker_color, width=1.2),
                )
                plot.addItem(region)
                plot.plot(
                    [lo_pt.rigol_setpoint_v, hi_pt.rigol_setpoint_v],
                    [y_lo, y_hi],
                    pen=None,
                    symbol="o",
                    symbolSize=8,
                    symbolBrush=marker_brush,
                    symbolPen=pg.mkPen(palette.background, width=1.5),
                    name="Waveform [Low, High]",
                )

    def _draw_reference_plots(self, _tokens, palette) -> None:
        self.ref_iv_plot.clear()
        self.ref_r_plot.clear()
        curve = self.active_curve
        if curve is None or not curve.passes:
            return
        active = self.active_pass
        for curve_pass in curve.passes:
            is_active = active is not None and curve_pass.pass_index == active.pass_index
            i_arr = np.asarray([pt.current_a for pt in curve_pass.points], dtype=float)
            v_arr = np.asarray([pt.voltage_v for pt in curve_pass.points], dtype=float)
            v_color = palette.measurement if is_active else palette.axes
            v_width = 2.2 if is_active else 1.2
            self.ref_iv_plot.plot(
                i_arr,
                v_arr,
                pen=pg.mkPen(v_color, width=v_width),
                symbol="o" if is_active and len(i_arr) <= 250 else None,
                symbolSize=4,
                symbolBrush=pg.mkBrush(v_color),
                name=curve_pass.label,
            )

            max_i = float(np.max(np.abs(i_arr))) if len(i_arr) else 0.0
            min_i = min(1e-5, 0.01 * max_i) if max_i > 0 else 1e-12
            if max_i >= 1e-3:
                min_i = max(min_i, 1e-5)
            r_x = []
            r_y = []
            for pt in curve_pass.points:
                if not pt.resistance_valid or abs(pt.current_a) < min_i:
                    continue
                r_val = (
                    pt.resistance_ohm
                    if pt.resistance_ohm is not None
                    else abs(pt.voltage_v / pt.current_a)
                )
                if math.isfinite(r_val) and r_val > 0.0:
                    r_x.append(pt.current_a)
                    r_y.append(r_val)
            if r_x:
                r_color = palette.success if is_active else palette.axes
                self.ref_r_plot.plot(
                    np.asarray(r_x, dtype=float),
                    np.asarray(r_y, dtype=float),
                    pen=pg.mkPen(r_color, width=v_width),
                    symbol="o" if is_active and len(r_x) <= 250 else None,
                    symbolSize=4,
                    symbolBrush=pg.mkBrush(r_color),
                    name=curve_pass.label,
                )

    def _fit_active_plots(self) -> None:
        if self._plot_view_mode == "transfer":
            self.voltage_plot.autoRange()
            self.current_plot.autoRange()
        else:
            self.ref_iv_plot.autoRange()
            self.ref_r_plot.autoRange()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_conversion_table(self, path: str | Path) -> Path:
        model = self._build_active_model()
        load_spec = parse_rigol_load(self._current_load_raw())
        span = None
        if isinstance(model, ConstantResistanceModel):
            pts: list[float] = []
            if self.last_dc_result is not None:
                pts.append(self.last_dc_result.point.rigol_setpoint_v)
            if self.last_waveform_result is not None:
                pts.extend(
                    (self.last_waveform_result.request.low_v, self.last_waveform_result.request.high_v)
                )
            span = self._resistor_plot_span(pts)
        return export_conversion_csv(
            Path(path),
            model,
            load_spec.wire_value,
            curve=self.active_curve,
            setting_range_v=span,
            waveform_result=self.last_waveform_result,
            dc_result=self.last_dc_result,
        )

    def _export_csv_dialog(self) -> None:
        target, _ = StationFileDialog.getSaveFileName(
            self,
            "Export Rigol-to-MTJ conversion table",
            "rigol_mtj_conversion.csv",
            "CSV files (*.csv)",
        )
        if not target:
            return
        try:
            saved = self.export_conversion_table(target)
        except (OSError, ValueError, TypeError, QuantityError) as exc:
            self.banner.show_message(f"CSV export failed: {exc}", severity="error")
            return
        self.status.emit(f"Exported conversion table to {saved}")

    def _export_plot_dialog(self) -> None:
        target, _ = StationFileDialog.getSaveFileName(
            self,
            "Export Load Calculator plot",
            "rigol_mtj_plot.png",
            "PNG image (*.png);;SVG vector (*.svg)",
        )
        if not target:
            return
        target_path = Path(target)
        plot_item = (
            self.voltage_plot.plotItem
            if self._plot_view_mode == "transfer"
            else self.ref_iv_plot.plotItem
        )
        try:
            if target_path.suffix.lower() == ".svg":
                SVGExporter(plot_item).export(str(target_path))
            else:
                exporter = ImageExporter(plot_item)
                exporter.parameters()["width"] = 1400
                exporter.export(str(target_path))
        except (OSError, ValueError, RuntimeError) as exc:
            self.banner.show_message(f"Plot export failed: {exc}", severity="error")
            return
        self.status.emit(f"Exported plot to {target_path}")
