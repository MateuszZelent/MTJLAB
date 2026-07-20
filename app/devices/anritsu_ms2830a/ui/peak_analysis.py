"""Fluent peak table and passive frequency-tracking surfaces for Anritsu."""

from __future__ import annotations

from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TableWidget,
    isDarkTheme,
)

from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto
from app.spectrum import SpectrumPeak
from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationDialog


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
        "SNR",
        "Prominence",
        "FWHM",
        "Q",
        "Fit",
        "Fit RMSE",
    )

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Anritsu — detected peaks")
        self.setObjectName("anritsuPeakTableDialog")
        self.setModal(False)
        self.resize(1040, 520)
        self.setMinimumSize(720, 380)
        self._peaks: tuple[SpectrumPeak, ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(StrongBodyLabel("Detected spectral peaks"))
        explanation = CaptionLabel(
            "Measurements are derived locally from the selected display trace. "
            "Raw acquisition data remains unchanged."
        )
        explanation.setObjectName("muted")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.table = TableWidget(self)
        self.table.setObjectName("anritsuPeakTable")
        self.table.setColumnCount(len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.status = CaptionLabel("No peaks detected.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.track_selected = PrimaryPushButton("Track selected peak", self)
        self.track_selected.setEnabled(False)
        self.copy_table = PushButton("Copy table", self)
        self.close_button = PushButton("Close", self)
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
        self._peaks = peaks
        self.table.setRowCount(len(peaks))
        for row, peak in enumerate(peaks):
            values = (
                str(row + 1),
                _frequency(peak.frequency_hz),
                f"{peak.amplitude_dbm:.5g} dBm",
                f"{peak.snr_db:.4g} dB",
                f"{peak.prominence_db:.4g} dB",
                _frequency(peak.fit_fwhm_hz or peak.fwhm_hz),
                "—" if peak.q_factor is None else f"{peak.q_factor:.6g}",
                peak.fit_model,
                "—" if peak.fit_rmse_db is None else f"{peak.fit_rmse_db:.4g} dB",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, column, item)
        if selected is not None and selected < len(peaks):
            self.table.selectRow(selected)
        elif peaks:
            self.table.selectRow(0)
        self.status.setText(
            f"{len(peaks)} peak(s) · {method}"
            if peaks
            else f"No peaks meet the automatic threshold · {method}"
        )
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
                    self.table.item(row, column).text()
                    for column in range(self.table.columnCount())
                )
            )
        QApplication.clipboard().setText("\n".join(rows))

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()


class PeakTrackingWindow(StationDialog):
    """Always-on-top history of one locally tracked spectral peak."""

    closed = Signal()
    history_cleared = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Anritsu — peak frequency tracking")
        self.setObjectName("anritsuPeakTrackingWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(760, 500)
        self.setMinimumSize(500, 360)
        self._times_s: deque[float] = deque(maxlen=2400)
        self._frequencies_hz: deque[float] = deque(maxlen=2400)
        self._amplitudes_dbm: deque[float] = deque(maxlen=2400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Tracked peak frequency"))
        header.addStretch(1)
        self.reset_view = PushButton("Reset view", self)
        self.clear_history = PushButton("Clear history", self)
        header.addWidget(self.reset_view)
        header.addWidget(self.clear_history)
        layout.addLayout(header)
        summary = QHBoxLayout()
        self.frequency = BodyLabel("— Hz", self)
        self.drift = BodyLabel("Δ — Hz", self)
        self.amplitude = BodyLabel("— dBm", self)
        summary.addWidget(self.frequency)
        summary.addWidget(self.drift)
        summary.addWidget(self.amplitude)
        summary.addStretch(1)
        layout.addLayout(summary)
        self.plot = pg.PlotWidget(self)
        self.plot.setObjectName("anritsuPeakTrackingPlot")
        self.plot.setLabel("bottom", "Elapsed time", units="s")
        self.plot.setLabel("left", "Frequency", units="Hz")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setMenuEnabled(True)
        self.curve = self.plot.plot(pen=pg.mkPen("#00a6d2", width=2))
        layout.addWidget(self.plot, 1)
        self.status = CaptionLabel("Choose a peak in the table to begin tracking.", self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
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
        self.curve.setData(tuple(self._times_s), tuple(self._frequencies_hz))
        first = self._frequencies_hz[0]
        self.frequency.setText(_frequency(peak.frequency_hz))
        self.drift.setText(f"Δ {_frequency(peak.frequency_hz - first)}")
        self.amplitude.setText(f"{peak.amplitude_dbm:.5g} dBm")
        self.status.setText(
            f"Tracking {source} · {self.point_count} point(s) · "
            f"FWHM {_frequency(peak.fit_fwhm_hz or peak.fwhm_hz)}"
        )

    def mark_lost(self, *, target_hz: float, gate_hz: float) -> None:
        self.status.setText(
            f"Peak temporarily not found near {_frequency(target_hz)} "
            f"within ±{_frequency(gate_hz)}. Raw Live acquisition continues."
        )

    def clear(self) -> None:
        self._times_s.clear()
        self._frequencies_hz.clear()
        self._amplitudes_dbm.clear()
        self.curve.clear()
        self.frequency.setText("— Hz")
        self.drift.setText("Δ — Hz")
        self.amplitude.setText("— dBm")
        self.status.setText("Tracking history cleared; waiting for the next Live frame.")

    def _clear_requested(self) -> None:
        self.clear()
        self.history_cleared.emit()

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

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()
