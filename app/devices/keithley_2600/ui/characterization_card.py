"""Fluent UI card for Keithley sample characterization and reporting."""

from __future__ import annotations

import math
import os
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, Slot
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
    SimpleCardWidget,
    SpinBox,
    StrongBodyLabel,
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
from app.settings.models import StationSettings
from app.ui.dialogs import StationMessageBox
from app.ui.widgets import NotificationBanner


class KeithleyCharacterizationCard(QWidget):
    """Integrated workspace for Keithley IV sweep, real-time plotting, and PDF reporting."""

    def __init__(
        self,
        controller: Any,
        settings: StationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._settings = settings
        self._worker: CharacterizationWorker | None = None
        self._current_dataset: CharacterizationDataset | None = None
        self._current_parameters: ExtractedScientificParameters | None = None

        self._live_v_points: list[float] = []
        self._live_i_points: list[float] = []
        self._live_r_points: list[float] = []
        self._live_comp_x: list[float] = []
        self._live_comp_y: list[float] = []

        self._init_ui()
        self._update_limits_from_settings()
        self._update_plot_labels()

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

        config_title = StrongBodyLabel("Parametry charakterystyki próbki")
        config_layout.addWidget(config_title)

        form_layout = QFormLayout()
        form_layout.setSpacing(6)

        self.mode_combo = ComboBox()
        self.mode_combo.addItems(["Charakterystyka prądowa (I → V)", "Charakterystyka napięciowa (V → I)"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        form_layout.addRow("Tryb pomiaru:", self.mode_combo)

        self.channel_combo = ComboBox()
        self.channel_combo.addItems(["Kanał A", "Kanał B"])
        self.channel_combo.currentTextChanged.connect(self._on_channel_changed)
        form_layout.addRow("Kanał Keithley:", self.channel_combo)

        self.start_level_edit = LineEdit()
        self.start_level_edit.setText("-1 mA")
        form_layout.addRow("Poziom początkowy:", self.start_level_edit)

        self.stop_level_edit = LineEdit()
        self.stop_level_edit.setText("1 mA")
        form_layout.addRow("Poziom końcowy:", self.stop_level_edit)

        self.points_spin = SpinBox()
        self.points_spin.setRange(3, 1001)
        self.points_spin.setValue(101)
        form_layout.addRow("Liczba punktów:", self.points_spin)

        self.compliance_edit = LineEdit()
        self.compliance_edit.setText("670 mV")
        form_layout.addRow("Limit compliance:", self.compliance_edit)

        self.dwell_edit = LineEdit()
        self.dwell_edit.setText("50 ms")
        form_layout.addRow("Czas ustalania (dwell):", self.dwell_edit)

        self.sense_combo = ComboBox()
        self.sense_combo.addItems(["4-przewodowy (Kelvin)", "2-przewodowy"])
        form_layout.addRow("Sonda napięciowa:", self.sense_combo)

        config_layout.addLayout(form_layout)

        # Sample Metadata Section
        meta_title = StrongBodyLabel("Metadane złącza / próbki")
        config_layout.addWidget(meta_title)

        meta_form = QFormLayout()
        meta_form.setSpacing(6)

        self.sample_id_edit = LineEdit()
        self.sample_id_edit.setText("MTJ-Sample-01")
        meta_form.addRow("Identyfikator (ID):", self.sample_id_edit)

        self.structure_edit = LineEdit()
        self.structure_edit.setPlaceholderText("np. Waf-3 / Złącze B4")
        meta_form.addRow("Struktura / chip:", self.structure_edit)

        self.area_edit = LineEdit()
        self.area_edit.setText("2.0")
        self.area_edit.setPlaceholderText("Pole w um^2")
        meta_form.addRow("Powierzchnia [um²]:", self.area_edit)

        self.thickness_edit = LineEdit()
        self.thickness_edit.setText("1.0")
        self.thickness_edit.setPlaceholderText("Bariera w nm")
        meta_form.addRow("Grubość bariery [nm]:", self.thickness_edit)

        self.operator_edit = LineEdit()
        self.operator_edit.setPlaceholderText("Inicjały operatora")
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
        plot_title = StrongBodyLabel("Krzywa pomiarowa na żywo: V(I) oraz R(I)")
        self.status_label = CaptionLabel("Gotowy do pomiaru")
        plot_header.addWidget(plot_title)
        plot_header.addStretch(1)
        plot_header.addWidget(self.status_label)
        plot_layout.addLayout(plot_header)

        # pyqtgraph setup
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#1e1e1e" if isDarkTheme() else "#ffffff")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel("bottom", "Zadany poziom [SI]")
        self.plot_widget.setLabel("left", "Odpowiedź napięciowa [V]")

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

        metrics_title = StrongBodyLabel("Wyniki analizy naukowej złącza")
        summary_layout.addWidget(metrics_title)

        grid = QGridLayout()
        grid.setSpacing(8)

        self.metric_r0 = BodyLabel("R₀: —")
        self.metric_g0 = BodyLabel("G₀: —")
        self.metric_ra = BodyLabel("R·A: —")
        self.metric_comp = BodyLabel("Compliance: Nie wykryto")
        self.metric_pmax = BodyLabel("P_max: —")
        self.metric_r2 = BodyLabel("Liniowość R²: —")

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

        splitter.addWidget(left_scroll)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)
        main_layout.addWidget(splitter, 1)

        # --- BOTTOM ACTION BAR ---
        actions_card = SimpleCardWidget()
        actions_layout = QHBoxLayout(actions_card)
        actions_layout.setContentsMargins(12, 6, 12, 6)
        actions_layout.setSpacing(10)

        self.start_button = PrimaryPushButton("Rozpocznij charakterystykę", self)
        self.start_button.setIcon(FluentIcon.PLAY)
        self.start_button.clicked.connect(self._on_start_clicked)

        self.stop_button = PushButton("Zatrzymaj", self)
        self.stop_button.setIcon(FluentIcon.CANCEL)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)

        self.pdf_button = PushButton("Generuj raport PDF...", self)
        self.pdf_button.setIcon(FluentIcon.DOCUMENT)
        self.pdf_button.setEnabled(False)
        self.pdf_button.clicked.connect(self._on_generate_pdf_clicked)

        self.csv_button = PushButton("Eksportuj CSV...", self)
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

    def _on_channel_changed(self) -> None:
        self._update_limits_from_settings()

    def _on_mode_changed(self) -> None:
        self._update_limits_from_settings()
        self._update_plot_labels()

    def _update_plot_labels(self) -> None:
        is_current = "prądowa" in self.mode_combo.currentText()
        if is_current:
            self.plot_widget.setLabel("bottom", "Zadany prąd [A]")
            self.plot_widget.setLabel("left", "Odpowiedź napięciowa [V]")
        else:
            self.plot_widget.setLabel("bottom", "Zadane napięcie [V]")
            self.plot_widget.setLabel("left", "Odpowiedź prądowa [A]")

    def _update_limits_from_settings(self) -> None:
        ch = self._selected_channel()
        try:
            channel_settings = self._settings.keithley.safety.channels[ch]
            limits = channel_settings.lab_limits
            # Autofill compliance and levels based on mode
            if "prądowa" in self.mode_combo.currentText():
                self.compliance_edit.setText(limits.voltage_compliance.max)
                self.start_level_edit.setText(limits.source_current.min)
                self.stop_level_edit.setText(limits.source_current.max)
            else:
                self.compliance_edit.setText(limits.current_compliance.max)
                self.start_level_edit.setText(limits.source_voltage.min)
                self.stop_level_edit.setText(limits.source_voltage.max)
        except Exception:
            pass

    def _build_config(self) -> CharacterizationSweepConfig:
        ch = self._selected_channel()
        is_current = "prądowa" in self.mode_combo.currentText()
        mode = "current" if is_current else "voltage"

        dim_sweep = DIMENSION_CURRENT if is_current else DIMENSION_VOLTAGE
        dim_comp = DIMENSION_VOLTAGE if is_current else DIMENSION_CURRENT

        start_si = parse_quantity(self.start_level_edit.text(), dim_sweep).si_value
        stop_si = parse_quantity(self.stop_level_edit.text(), dim_sweep).si_value
        comp_si = abs(parse_quantity(self.compliance_edit.text(), dim_comp).si_value)
        dwell_si = max(0.0, parse_quantity(self.dwell_edit.text(), DIMENSION_TIME).si_value)

        if comp_si <= 0:
            raise ValueError("Limit compliance musi być większy od zera.")
        if abs(stop_si - start_si) < 1e-15:
            raise ValueError("Poziom początkowy i końcowy nie mogą być identyczne.")

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
            self.banner.show_message(f"Błędne parametry wejściowe: {exc}")
            return

        # Preflight safety check
        try:
            from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
            KeithleyCharacterizationRunner.validate_preflight(config, self._settings)
        except SafetyViolation as exc:
            self.banner.show_message(f"Odrzucenie bezpieczeństwa stacji: {exc}")
            return

        # Reset plots and data
        self._live_v_points.clear()
        self._live_i_points.clear()
        self._live_r_points.clear()
        self._live_comp_x.clear()
        self._live_comp_y.clear()
        self.curve_iv.setData([], [])
        self.curve_clamped.setData([], [])
        self.progress_bar.setValue(0)
        self.status_label.setText("Pomiar w toku...")

        self._update_plot_labels()
        comp_val = config.compliance_si
        self.compliance_line_pos.setValue(comp_val)
        self.compliance_line_neg.setValue(-comp_val)

        try:
            device_proxy = self._controller.adapter_for_run()
        except Exception as exc:
            self.banner.show_message(f"Przyrząd Keithley niedostępny: {exc}")
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
            self.status_label.setText("Zatrzymywanie i rampowanie do zera...")
            self._worker.request_stop()

    @Slot(object)
    def _on_point_acquired(self, point: CharacterizationPoint) -> None:
        is_current = self._worker is None or self._worker._config.mode == "current"
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

    @Slot(int, int)
    def _on_progress_changed(self, current: int, total: int) -> None:
        pct = int((current / total) * 100)
        self.progress_bar.setValue(pct)
        self.status_label.setText(f"Punkt {current} z {total} ({pct}%)")

    @Slot(str)
    def _on_compliance_event(self, msg: str) -> None:
        self.metric_comp.setText("Compliance: AKTYWNY (clamping)")
        self.metric_comp.setStyleSheet("color: #ef4444; font-weight: bold;")

    @Slot(object)
    def _on_sweep_finished(self, dataset: CharacterizationDataset) -> None:
        self._current_dataset = dataset
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.pdf_button.setEnabled(True)
        self.csv_button.setEnabled(True)
        self.status_label.setText("Pomiar zakończony pomyślnie")

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
            self.metric_ra.setText("R·A: brak danych")

        if params.compliance_detected and params.compliance_onset_point:
            ci, cv = params.compliance_onset_point
            if dataset.config.mode == "current":
                self.metric_comp.setText(f"Compliance: Onset |I|={abs(ci)*1e3:.2f} mA ({params.clamped_points_fraction*100:.0f}% nasycenia)")
            else:
                self.metric_comp.setText(f"Compliance: Onset |V|={abs(cv)*1e3:.1f} mV ({params.clamped_points_fraction*100:.0f}% nasycenia)")
            self.metric_comp.setStyleSheet("color: #ef4444; font-weight: bold;")
        else:
            self.metric_comp.setText("Compliance: Brak (obszar liniowy)")
            self.metric_comp.setStyleSheet("color: #059669; font-weight: bold;")

        self.metric_pmax.setText(f"P_max: {params.max_power_dissipated_w * 1e3:.2f} mW")
        self.metric_r2.setText(f"Liniowość R²: {params.linearity_r2:.4f}")

    @Slot(str)
    def _on_sweep_failed(self, error_msg: str) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText(f"Błąd: {error_msg}")
        self.banner.show_message(f"Błąd wykonania charakterystyki: {error_msg}")

    @Slot()
    def _on_generate_pdf_clicked(self) -> None:
        if self._current_dataset is None or self._current_parameters is None:
            return

        default_name = f"Raport_{self._current_dataset.config.metadata.sample_id}.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Zapisz raport PDF z charakterystyki próbki",
            default_name,
            "Dokument PDF (*.pdf)",
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
                "Raport PDF wygenerowany",
                f"Raport laboratoryjny został pomyślnie utworzony:\n{res_path}\n\nCzy chcesz otworzyć go teraz?",
            )
            # Try opening in default viewer on Windows
            if os.name == "nt":
                try:
                    os.startfile(str(res_path))
                except Exception:
                    pass
        except Exception as exc:
            StationMessageBox.critical(self, "Błąd generowania PDF", f"Nie udało się wygenerować raportu: {exc}")

    @Slot()
    def _on_export_csv_clicked(self) -> None:
        if self._current_dataset is None:
            return

        default_name = f"Dane_{self._current_dataset.config.metadata.sample_id}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Eksportuj surowe dane pomiarowe do CSV",
            default_name,
            "Plik CSV (*.csv)",
        )
        if not path:
            return

        try:
            res_path = KeithleyDataExporter.export_csv(self._current_dataset, path)
            StationMessageBox.information(
                self,
                "Eksport zakończony",
                f"Dane pomiarowe zapisano pomyślnie:\n{res_path}",
            )
        except Exception as exc:
            StationMessageBox.critical(self, "Błąd eksportu CSV", f"Nie udało się wyeksportować danych: {exc}")

    def set_settings(self, settings: StationSettings) -> None:
        """Update station settings and refresh limit fields."""
        self._settings = settings
        self._update_limits_from_settings()

    def closeEvent(self, event) -> None:
        """Safely terminate background acquisition worker on card close."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(2000)
        super().closeEvent(event)
