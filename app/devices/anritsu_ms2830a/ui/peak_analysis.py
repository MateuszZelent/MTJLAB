"""Fluent peak table and passive frequency-tracking surfaces for Anritsu."""

from __future__ import annotations

import csv
import math
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TableWidget,
    isDarkTheme,
)

from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto, parse_quantity
from app.spectrum import SpectrumPeak
from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationDialog, StationFileDialog
from app.ui.widgets.plot_ownership import create_plot_widget


def _ensure_win32_taskbar_window(window: StationDialog) -> None:
    """Apply native Windows taskbar extended styles for unowned floating tool windows."""
    if sys.platform != "win32":
        return
    try:
        import win32con
        import win32gui

        hwnd = int(window.winId())
        if hwnd:
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            updated = (ex_style | win32con.WS_EX_APPWINDOW) & ~win32con.WS_EX_TOOLWINDOW
            if updated != ex_style:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, updated)
    except (ImportError, OSError, RuntimeError, ValueError):
        return
    enable_controls = getattr(window, "_enable_platform_window_controls", None)
    if callable(enable_controls):
        enable_controls()


def _configure_floating_tool_window(window: StationDialog, owner: QWidget | None) -> None:
    """Make a non-modal tool window minimize to the Windows taskbar cleanly."""

    flags = (
        (window.windowFlags() & ~Qt.WindowType.Dialog)
        | Qt.WindowType.Window
        | Qt.WindowType.WindowMinMaxButtonsHint
        | Qt.WindowType.WindowCloseButtonHint
        | Qt.WindowType.WindowTitleHint
        | Qt.WindowType.WindowSystemMenuHint
    )
    window.setWindowFlags(flags)
    if owner is not None:
        host = owner.window()
        if host is not None and not host.windowIcon().isNull():
            window.setWindowIcon(host.windowIcon())
        owner.destroyed.connect(window.close)


def _frequency(value_hz: float | None) -> str:
    return "—" if value_hz is None else format_quantity_auto(value_hz, DIMENSION_FREQUENCY)


class PeakTableDialog(StationDialog):
    """Live-updating table of measured peaks and fit diagnostics."""

    peak_selected = Signal(int)
    track_requested = Signal(int)
    closed = Signal()

    HEADERS = (
        "#",
        "Frequency",
        "Amplitude",
        "Contrast",
        "Prominence",
        "FWHM",
        "Q",
        "Fit",
        "Fit RMSE",
    )
    INITIAL_COLUMN_WIDTHS = (
        44,
        124,
        116,
        96,
        104,
        116,
        90,
        116,
        110,
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(None, resizable=True)
        _configure_floating_tool_window(self, parent)
        self.setWindowTitle("Anritsu — detected peaks")
        self.setObjectName("anritsuPeakTableDialog")
        self.setModal(False)
        self.resize(1040, 520)
        self.setMinimumSize(720, 380)
        self._peaks: tuple[SpectrumPeak, ...] = ()

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=8)
        layout.addWidget(StrongBodyLabel("Detected spectral peaks", surface))
        explanation = CaptionLabel(
            "Measurements are derived locally from the selected display trace. "
            "Raw acquisition data remains unchanged.",
            surface,
        )
        explanation.setObjectName("muted")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.table = TableWidget(surface)
        self.table.setObjectName("anritsuPeakTable")
        self.table.setColumnCount(len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate(self.INITIAL_COLUMN_WIDTHS):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)
        self.status = CaptionLabel("No peaks detected.", surface)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.track_selected = PrimaryPushButton("Add tracked peak", surface)
        self.track_selected.setToolTip("Add an independent frequency-history window; existing tracks remain active.")
        self.track_selected.setEnabled(False)
        self.copy_table = PushButton("Copy table", surface)
        self.close_button = PushButton("Close", surface)
        actions.addWidget(self.track_selected)
        actions.addWidget(self.copy_table)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self._request_tracking())
        self.track_selected.clicked.connect(self._request_tracking)
        self.copy_table.clicked.connect(self._copy_table)
        self.close_button.clicked.connect(self.close)

    def set_peaks(self, peaks: tuple[SpectrumPeak, ...], *, method: str) -> None:
        selected = self.selected_peak_index()
        previous_peak = self._peaks[selected] if selected is not None else None
        if previous_peak is not None and peaks:
            # Rows can reorder when amplitudes cross; retain the selected frequency.
            selected = min(range(len(peaks)), key=lambda index: abs(peaks[index].frequency_hz - previous_peak.frequency_hz))
        self._peaks = peaks
        updates_enabled = self.table.updatesEnabled()
        sorting_enabled = self.table.isSortingEnabled()
        self.table.setUpdatesEnabled(False)
        if sorting_enabled:
            self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        try:
            if self.table.rowCount() != len(peaks):
                self.table.setRowCount(len(peaks))
            for row, peak in enumerate(peaks):
                values = (
                    str(row + 1),
                    _frequency(peak.frequency_hz),
                    f"{peak.amplitude_dbm:.5g} {getattr(peak, 'amplitude_unit', 'dBm')}",
                    f"{peak.snr_db:.4g} {peak.contrast_unit}",
                    f"{peak.prominence_db:.4g} {peak.contrast_unit}",
                    _frequency(peak.fit_fwhm_hz or peak.fwhm_hz),
                    "—" if peak.q_factor is None else f"{peak.q_factor:.6g}",
                    peak.fit_model,
                    "—" if peak.fit_rmse_db is None else f"{peak.fit_rmse_db:.4g} dB",
                )
                for column, text in enumerate(values):
                    item = self.table.item(row, column)
                    if item is None:
                        item = QTableWidgetItem(text)
                        item.setData(Qt.ItemDataRole.UserRole, row)
                        self.table.setItem(row, column, item)
                    else:
                        if item.text() != text:
                            item.setText(text)
                        if item.data(Qt.ItemDataRole.UserRole) != row:
                            item.setData(Qt.ItemDataRole.UserRole, row)
            target_row = (
                selected
                if selected is not None and selected < len(peaks)
                else (0 if peaks else -1)
            )
            if target_row >= 0 and self.table.currentRow() != target_row:
                self.table.selectRow(target_row)
        finally:
            self.table.blockSignals(False)
            if sorting_enabled:
                self.table.setSortingEnabled(True)
            self.table.setUpdatesEnabled(updates_enabled)
        status_text = (
            f"{len(peaks)} peak(s) · {method}"
            if peaks
            else f"No peaks meet the automatic threshold · {method}"
        )
        if self.status.text() != status_text:
            self.status.setText(status_text)
        self._selection_changed()

    def selected_peak_index(self) -> int | None:
        row = self.table.currentRow()
        return row if 0 <= row < len(self._peaks) else None

    def _selection_changed(self) -> None:
        index = self.selected_peak_index()
        self.track_selected.setEnabled(index is not None)
        if index is not None:
            self.peak_selected.emit(index)

    def _request_tracking(self) -> None:
        index = self.selected_peak_index()
        if index is not None:
            self.track_requested.emit(index)

    def _copy_table(self) -> None:
        rows = ["\t".join(self.HEADERS)]
        for row in range(self.table.rowCount()):
            rows.append(
                "\t".join(
                    item.text()
                    if (item := self.table.item(row, column)) is not None
                    else ""
                    for column in range(self.table.columnCount())
                )
            )
        QApplication.clipboard().setText("\n".join(rows))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        _ensure_win32_taskbar_window(self)

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()


class PeakTrackingWindow(StationDialog):
    """Always-on-top history of one locally tracked spectral peak."""

    closed = Signal()
    history_cleared = Signal()
    gate_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(None, resizable=True)
        _configure_floating_tool_window(self, parent)
        self.setWindowTitle("Anritsu — peak frequency tracking")
        self.setObjectName("anritsuPeakTrackingWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(760, 500)
        self.setMinimumSize(500, 460)
        self._times_s: deque[float] = deque(maxlen=2400)
        self._frequencies_hz: deque[float] = deque(maxlen=2400)
        self._amplitudes_dbm: deque[float] = deque(maxlen=2400)
        self._amplitude_units: deque[str] = deque(maxlen=2400)
        self._sources = deque(maxlen=2400)
        self._timestamps = deque(maxlen=2400)
        self._gates_hz = deque(maxlen=2400)
        self.gate_hz = 0.0
        self._initial_frequency_hz = None

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=8)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Tracked peak frequency", surface))
        header.addStretch(1)
        self.reset_view = PushButton("Reset view", surface)
        self.clear_history = PushButton("Clear history", surface)
        header.addWidget(self.reset_view)
        header.addWidget(self.clear_history)
        layout.addLayout(header)
        summary = QHBoxLayout()
        self.frequency = BodyLabel("— Hz", surface)
        self.drift = BodyLabel("Δ — Hz", surface)
        self.amplitude = BodyLabel("— dBm", surface)
        summary.addWidget(self.frequency)
        summary.addWidget(self.drift)
        summary.addWidget(self.amplitude)
        summary.addStretch(1)
        layout.addLayout(summary)
        settings = QHBoxLayout()
        settings.addWidget(CaptionLabel("Search ±", surface))
        self.gate = LineEdit(surface)
        self.gate.setPlaceholderText("e.g. 5 MHz")
        self.gate.setAccessibleName("Peak frequency search half-width with units")
        self.gate.setMinimumWidth(110)
        settings.addWidget(self.gate, 1)
        self.apply_gate = PushButton("Apply", surface)
        settings.addWidget(self.apply_gate)
        self.paused = CheckBox("Pause track", surface)
        settings.addWidget(self.paused)
        layout.addLayout(settings)
        self.apply_gate.clicked.connect(self._apply_gate)
        self.plot = create_plot_widget(surface)
        self.plot.setMinimumHeight(140)
        self.plot.setObjectName("anritsuPeakTrackingPlot")
        self.plot.setLabel("bottom", "Elapsed time", units="s")
        self.plot.setLabel("left", "Frequency", units="Hz")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setMenuEnabled(True)
        self.curve = self.plot.plot(pen=pg.mkPen("#00a6d2", width=2))
        layout.addWidget(self.plot, 1)
        self.status = CaptionLabel(
            "Choose a peak in the table to begin tracking.",
            surface,
        )
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.export_button = PushButton("Export history CSV", surface)
        self.export_button.clicked.connect(self._export)
        footer.addWidget(self.export_button)
        footer.addStretch(1)
        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.reset_view.clicked.connect(self.plot.autoRange)
        self.clear_history.clicked.connect(self._clear_requested)
        self._apply_theme()

    @property
    def point_count(self) -> int:
        return len(self._times_s)

    def append(self, elapsed_s: float, peak: SpectrumPeak, *, source: str) -> None:
        self._times_s.append(float(elapsed_s))
        self._frequencies_hz.append(float(peak.frequency_hz))
        self._amplitudes_dbm.append(float(peak.amplitude_dbm))
        self._amplitude_units.append(getattr(peak, "amplitude_unit", "dBm"))
        self._sources.append(source)
        self._timestamps.append(datetime.now(UTC).isoformat())
        self._gates_hz.append(self.gate_hz)
        self.curve.setData(tuple(self._times_s), tuple(self._frequencies_hz), connect="finite")
        if self._initial_frequency_hz is None:
            self._initial_frequency_hz = peak.frequency_hz
        first = self._initial_frequency_hz
        self.frequency.setText(_frequency(peak.frequency_hz))
        self.drift.setText(f"Δ {_frequency(peak.frequency_hz - first)}")
        self.amplitude.setText(
            f"{peak.amplitude_dbm:.5g} {getattr(peak, 'amplitude_unit', 'dBm')}"
        )
        self.status.setText(
            f"Tracking {source} · {self.point_count} point(s) · "
            f"FWHM {_frequency(peak.fit_fwhm_hz or peak.fwhm_hz)} · last 2400 samples"
        )

    def mark_lost(self, *, target_hz: float, gate_hz: float, elapsed_s: float | None = None) -> None:
        if elapsed_s is not None:
            self._times_s.append(elapsed_s)
            self._frequencies_hz.append(float("nan"))
            self._amplitudes_dbm.append(float("nan"))
            self._amplitude_units.append("")
            self._sources.append(self._sources[-1] if self._sources else "")
            self._timestamps.append(datetime.now(UTC).isoformat())
            self._gates_hz.append(gate_hz)
            self.curve.setData(tuple(self._times_s), tuple(self._frequencies_hz), connect="finite")
        self.status.setText(
            f"Peak temporarily not found near {_frequency(target_hz)} "
            f"within ±{_frequency(gate_hz)}. Waiting for the next completed spectrum."
        )

    def clear(self) -> None:
        self._times_s.clear()
        self._frequencies_hz.clear()
        self._amplitudes_dbm.clear()
        self._amplitude_units.clear()
        self._sources.clear()
        self._timestamps.clear()
        self._gates_hz.clear()
        self._initial_frequency_hz = None
        self.curve.clear()
        self.frequency.setText("— Hz")
        self.drift.setText("Δ — Hz")
        self.amplitude.setText("— dBm")
        self.status.setText("Tracking history cleared; waiting for the next Live frame.")

    def _clear_requested(self) -> None:
        self.clear()
        self.history_cleared.emit()

    def _apply_gate(self) -> None:
        try:
            gate_hz = parse_quantity(self.gate.text(), DIMENSION_FREQUENCY).si_value
            if gate_hz <= 0:
                raise ValueError("Search half-width must be positive.")
        except ValueError as exc:
            self.status.setText(f"Invalid search range: {exc}")
            return
        self.gate_changed.emit(gate_hz)
        self.gate_hz = gate_hz
        self.status.setText(f"Search range: ±{_frequency(gate_hz)}. Tracking continues near the last confirmed frequency.")

    def export_csv(self, path) -> None:
        with Path(path).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("elapsed_s", "frequency_hz", "amplitude", "amplitude_unit", "source", "analysis_time_utc", "search_half_width_hz", "status"))
            for t, frequency, amplitude, unit, source, stamp, gate in zip(
                    self._times_s, self._frequencies_hz, self._amplitudes_dbm, self._amplitude_units,
                    self._sources, self._timestamps, self._gates_hz, strict=True):
                valid = math.isfinite(frequency)
                writer.writerow((t, frequency if valid else "", amplitude if valid else "", unit,
                                 source, stamp, gate, "detected" if valid else "gap"))

    def _export(self) -> None:
        path, _ = StationFileDialog.getSaveFileName(self, "Export peak history", "peak-history.csv", "CSV (*.csv)")
        if path:
            try:
                self.export_csv(path)
            except OSError as exc:
                self.status.setText(f"Export failed: {exc}")

    def event(self, event: QEvent) -> bool:
        if event.type() in {
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        }:
            self._apply_theme()
        return super().event(event)

    def _apply_theme(self) -> None:
        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(palette.background)
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen(palette.axes))
            axis.setTextPen(pg.mkPen(palette.axes))
        self.curve.setPen(pg.mkPen(palette.measurement, width=2))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        _ensure_win32_taskbar_window(self)

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()
