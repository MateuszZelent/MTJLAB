"""Interactive, peak-preserving scientific spectrum plot."""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter, SVGExporter
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class SpectrumPlotWidget(QWidget):
    """Reusable live/results plot with markers, holds and data export."""

    status_changed = Signal(str)

    def __init__(
        self, parent: QWidget | None = None, *, legend: bool = True, compact_toolbar: bool = False
    ) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumPlot")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._traces: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._hold_source: str | None = None
        self._max_hold: np.ndarray | None = None
        self._min_hold: np.ndarray | None = None
        self._marker_x: float | None = None
        self._x_label = "Frequency"
        self._x_unit = "MHz"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        actions = (
            ("Reset", "Reset zoom and show all finite data", self.auto_range),
            ("Peak", "Move the primary marker to the highest visible point", self.peak_search),
            ("Δ marker", "Place a delta marker at the current crosshair position", self.place_delta_marker),
            ("Max hold", "Toggle a maximum hold of the primary trace", self.toggle_max_hold),
            ("Min hold", "Toggle a minimum hold of the primary trace", self.toggle_min_hold),
            ("Clear hold", "Clear maximum and minimum hold traces", self.clear_holds),
            ("Export", "Export visible traces to CSV, PNG or SVG", self.export),
        )
        if compact_toolbar:
            actions = tuple(action for action in actions if action[0] in {"Reset", "Peak", "Export"})
        for text, tooltip, callback in actions:
            button = QToolButton()
            button.setObjectName("plotToolButton")
            button.setText(text)
            button.setToolTip(tooltip)
            button.setAccessibleName(text)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        self.readout = QLabel("X: —   Y: —")
        self.readout.setObjectName("plotReadout")
        toolbar.addWidget(self.readout)
        root.addLayout(toolbar)

        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setLabel("bottom", "Frequency", units="MHz")
        self.plot.setLabel("left", "Power", units="dBm")
        self.plot.setMenuEnabled(True)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        self.plot.getPlotItem().setClipToView(True)
        if legend:
            self.plot.addLegend(offset=(10, 10))
        root.addWidget(self.plot, 1)

        self.crosshair_x = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#78909c", width=1))
        self.crosshair_y = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#78909c", width=1))
        self.marker = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#ffb300", width=2), label="M1")
        self.delta_marker = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen("#ab47bc", width=2), label="Δ"
        )
        for item in (self.crosshair_x, self.crosshair_y, self.marker, self.delta_marker):
            self.plot.addItem(item, ignoreBounds=True)
        self.marker.hide()
        self.delta_marker.hide()
        self._last_mouse_x: float | None = None
        self._mouse_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=45, slot=self._mouse_moved
        )
        self.marker.sigPositionChanged.connect(self._marker_changed)
        self.delta_marker.sigPositionChanged.connect(self._marker_changed)

    def set_labels(self, *, x: str = "Frequency", x_unit: str = "MHz", y: str = "Power", y_unit: str = "dBm") -> None:
        self._x_label = x
        self._x_unit = x_unit
        self.plot.setLabel("bottom", x, units=x_unit)
        self.plot.setLabel("left", y, units=y_unit)

    def set_title(self, title: str) -> None:
        self.plot.setTitle(title)

    def apply_theme(self, theme: str) -> None:
        foreground = "#17212b" if theme == "light" else "#dce6ef"
        grid = "#607d8b"
        self.plot.setBackground(None)
        for axis in ("left", "bottom"):
            item = self.plot.getAxis(axis)
            item.setPen(pg.mkPen(foreground))
            item.setTextPen(pg.mkPen(foreground))
        self.crosshair_x.setPen(pg.mkPen(grid, width=1))
        self.crosshair_y.setPen(pg.mkPen(grid, width=1))

    def set_trace(
        self,
        name: str,
        x: object,
        y: object,
        *,
        color: str = "#2196f3",
        visible: bool = True,
        primary: bool = False,
    ) -> None:
        x_values = np.asarray(x, dtype=float)
        y_values = np.asarray(y, dtype=float)
        if x_values.ndim != 1 or y_values.ndim != 1 or x_values.size != y_values.size:
            raise ValueError("Spectrum X and Y must be equally-sized one-dimensional arrays.")
        finite = np.isfinite(x_values) & np.isfinite(y_values)
        x_values, y_values = x_values[finite], y_values[finite]
        self._traces[name] = (x_values, y_values)
        curve = self._curves.get(name)
        if curve is None:
            curve = self.plot.plot(name=name, pen=pg.mkPen(color, width=1.6))
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self._curves[name] = curve
        curve.setData(x_values, y_values)
        curve.setVisible(visible)
        if primary or self._hold_source is None:
            self._hold_source = name
            self._update_holds(x_values, y_values)

    def clear_trace(self, name: str) -> None:
        self._traces.pop(name, None)
        if name in self._curves:
            self._curves[name].clear()
            self._curves[name].hide()

    def trace_point_count(self, name: str) -> int:
        """Return the finite point count for GUI tests and status reporting."""

        data = self._traces.get(name)
        return 0 if data is None else int(data[0].size)

    def clear(self) -> None:
        for name in tuple(self._curves):
            self.clear_trace(name)
        self.clear_holds()
        self.marker.hide()
        self.delta_marker.hide()

    def auto_range(self) -> None:
        self.plot.enableAutoRange()
        self.plot.autoRange()

    def peak_search(self) -> None:
        data = self._primary_data()
        if data is None or data[0].size == 0:
            self.status_changed.emit("Peak search unavailable: no finite trace data.")
            return
        x_values, y_values = data
        index = int(np.nanargmax(y_values))
        self.marker.setPos(float(x_values[index]))
        self.marker.show()
        self._marker_x = float(x_values[index])
        self.status_changed.emit(
            f"Peak: {x_values[index]:.9g} {self._x_unit}, {y_values[index]:.6g}"
        )

    def place_delta_marker(self) -> None:
        if self._last_mouse_x is None:
            self.status_changed.emit("Move the crosshair over the plot before placing a delta marker.")
            return
        self.delta_marker.setPos(self._last_mouse_x)
        self.delta_marker.show()
        self._marker_changed()

    def toggle_max_hold(self) -> None:
        self._toggle_hold("Max hold", "#ef5350")

    def toggle_min_hold(self) -> None:
        self._toggle_hold("Min hold", "#66bb6a")

    def clear_holds(self) -> None:
        self._max_hold = None
        self._min_hold = None
        for name in ("Max hold", "Min hold"):
            self.clear_trace(name)

    def export(self) -> None:
        path, selected = QFileDialog.getSaveFileName(
            self, "Export spectrum", "spectrum.csv", "CSV (*.csv);;PNG (*.png);;SVG (*.svg)"
        )
        if not path:
            return
        suffix = Path(path).suffix.lower()
        if "PNG" in selected or suffix == ".png":
            ImageExporter(self.plot.plotItem).export(path)
        elif "SVG" in selected or suffix == ".svg":
            SVGExporter(self.plot.plotItem).export(path)
        else:
            self._export_csv(Path(path))
        self.status_changed.emit(f"Spectrum exported to {path}")

    def _export_csv(self, path: Path) -> None:
        visible = [
            (name, *self._traces[name])
            for name, curve in self._curves.items()
            if curve.isVisible() and name in self._traces
        ]
        with path.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            x_header = self._x_label.lower().replace(" ", "_")
            if self._x_unit:
                x_header += f"_{self._x_unit}"
            writer.writerow(["trace", x_header, "value"])
            for name, x_values, y_values in visible:
                writer.writerows(zip([name] * x_values.size, x_values, y_values, strict=True))

    def _primary_data(self) -> tuple[np.ndarray, np.ndarray] | None:
        if self._hold_source and self._hold_source in self._traces:
            return self._traces[self._hold_source]
        return next(iter(self._traces.values()), None)

    def _toggle_hold(self, name: str, color: str) -> None:
        curve = self._curves.get(name)
        if curve is not None and curve.isVisible():
            curve.hide()
            self.status_changed.emit(f"{name} disabled")
            return
        data = self._primary_data()
        if data is None:
            self.status_changed.emit(f"{name} unavailable: no primary trace.")
            return
        if name == "Max hold":
            self._max_hold = data[1].copy()
        else:
            self._min_hold = data[1].copy()
        self.set_trace(name, *data, color=color, visible=True)
        self.status_changed.emit(f"{name} enabled")

    def _update_holds(self, x_values: np.ndarray, y_values: np.ndarray) -> None:
        for name, operation, attribute, color in (
            ("Max hold", np.maximum, "_max_hold", "#ef5350"),
            ("Min hold", np.minimum, "_min_hold", "#66bb6a"),
        ):
            curve = self._curves.get(name)
            if curve is None or not curve.isVisible():
                continue
            previous = getattr(self, attribute)
            held = y_values.copy() if previous is None or previous.size != y_values.size else operation(previous, y_values)
            setattr(self, attribute, held)
            self._traces[name] = (x_values, held)
            curve.setData(x_values, held)
            curve.setPen(pg.mkPen(color, width=1.3))

    def _mouse_moved(self, event: tuple[object, ...]) -> None:
        position = event[0]
        if not self.plot.sceneBoundingRect().contains(position):
            return
        point = self.plot.plotItem.vb.mapSceneToView(position)
        self._last_mouse_x = float(point.x())
        self.crosshair_x.setPos(point.x())
        self.crosshair_y.setPos(point.y())
        self.readout.setText(f"X: {point.x():.9g}   Y: {point.y():.6g}")

    def _marker_changed(self) -> None:
        if not self.marker.isVisible():
            return
        delta = ""
        if self.delta_marker.isVisible():
            delta = f"   ΔX: {self.delta_marker.value() - self.marker.value():.9g} MHz"
        self.readout.setText(f"M1 X: {self.marker.value():.9g} MHz{delta}")
