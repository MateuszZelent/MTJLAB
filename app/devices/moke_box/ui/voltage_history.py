"""Confirmed DAC history and explicitly labelled calibration predictions."""

from __future__ import annotations

import math
import time
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, isDarkTheme, qconfig

from app.domain.errors import ConfigurationError
from app.ui.design_system import plot_theme, tokens_for


class MokeVoltageHistory(QWidget):
    WINDOW_S = 180.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mokeVoltageHistory")
        self._origin = time.monotonic()
        self._model_id = None
        self.points = deque(maxlen=2000)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(self)
        self.plot.setMinimumSize(240, 240)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setLabel("bottom", "Time relative to now", units="s")
        self.plot.setMouseEnabled(x=False, y=True)
        self.plot.setXRange(-self.WINDOW_S, 0, padding=0)
        self.plot.enableAutoRange(axis="x", enable=False)
        self.plot.setLabel("left", "Confirmed voltage", units="V")
        self.item = self.plot.getPlotItem()
        self.item.showAxis("right")
        self.item.setLabel("right", "Calculated field", units="T")
        self.field_view = pg.ViewBox()
        self.item.scene().addItem(self.field_view)
        self.item.getAxis("right").linkToView(self.field_view)
        self.field_view.setXLink(self.item)
        self.item.vb.sigResized.connect(self._sync_geometry)
        self.voltage_curve = self.plot.plot(symbol="o", symbolSize=4)
        self.ascending_curve = pg.PlotDataItem(connect="finite")
        self.descending_curve = pg.PlotDataItem(connect="finite")
        self.field_view.addItem(self.ascending_curve)
        self.field_view.addItem(self.descending_curve)
        layout.addWidget(self.plot, 1)
        self.legend = CaptionLabel(self)
        self.legend.setWordWrap(True)
        layout.addWidget(self.legend)
        note = CaptionLabel("Samples are confirmed DAC readings. Field curves are calibration predictions.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        qconfig.themeChanged.connect(self.apply_theme)
        self.apply_theme()
        self._window_timer = QTimer(self)
        self._window_timer.setInterval(250)
        self._window_timer.timeout.connect(self._refresh_visible_window)
        self._window_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_window()

    def _refresh_visible_window(self):
        if self.isVisible():
            self._refresh_window()

    def _refresh_window(self):
        now_s = time.monotonic() - self._origin
        cutoff_s = now_s - self.WINDOW_S
        while self.points and self.points[0][0] < cutoff_s:
            self.points.popleft()
        if self.points:
            elapsed, voltages, ascending, descending = zip(*self.points, strict=True)
            relative_s = [stamp_s - now_s for stamp_s in elapsed]
            self.voltage_curve.setData(relative_s, voltages)
            self.ascending_curve.setData(relative_s, ascending)
            self.descending_curve.setData(relative_s, descending)
        else:
            for curve in (self.voltage_curve, self.ascending_curve, self.descending_curve):
                curve.clear()
        self.plot.setXRange(-self.WINDOW_S, 0, padding=0)
        self._sync_geometry()

    def _sync_geometry(self):
        self.field_view.setGeometry(self.item.vb.sceneBoundingRect())
        self.field_view.linkedViewChanged(self.item.vb, self.field_view.XAxis)

    def apply_theme(self, *_args):
        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(palette.background)
        for axis in ("left", "bottom", "right"):
            self.item.getAxis(axis).setPen(pg.mkPen(palette.axes))
            self.item.getAxis(axis).setTextPen(pg.mkPen(palette.axes))
        self.voltage_curve.setPen(pg.mkPen(palette.measurement, width=2))
        self.voltage_curve.setSymbolBrush(pg.mkBrush(palette.measurement))
        self.ascending_curve.setPen(pg.mkPen(palette.axes, width=2))
        self.descending_curve.setPen(pg.mkPen(palette.axes, width=2, style=Qt.PenStyle.DashLine))
        self.legend.setText("Voltage · coloured     B↑ · solid     B↓ · dashed")

    def clear(self):
        self.points.clear()
        self._origin = time.monotonic()
        for curve in (self.voltage_curve, self.ascending_curve, self.descending_curve):
            curve.clear()
        self.plot.setXRange(-self.WINDOW_S, 0, padding=0)

    def append(self, voltage_v, model=None):
        if not math.isfinite(voltage_v):
            return
        identity = model.calibration_id if model is not None else None
        if identity != self._model_id:
            self.clear()
            self._model_id = identity
        up = down = float("nan")
        if model is not None:
            try:
                up = model.ascending.estimate(voltage_v)
                down = model.descending.estimate(voltage_v)
            except (ValueError, ConfigurationError):
                pass
        self.points.append((time.monotonic() - self._origin, voltage_v, up, down))
        if self.isVisible():
            self._refresh_window()
        else:
            # Calibration reports points while this page is hidden. Keep the
            # history, but let showEvent draw after the plot has real geometry.
            cutoff_s = time.monotonic() - self._origin - self.WINDOW_S
            while self.points and self.points[0][0] < cutoff_s:
                self.points.popleft()
