"""Interactive, peak-preserving scientific spectrum plot."""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter, SVGExporter
from PySide6.QtCore import QEvent, QSize, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, TransparentPushButton, isDarkTheme
from app.ui.dialogs import StationFileDialog as QFileDialog

from app.ui.design_system import plot_theme, tokens_for


class SpectrumPlotWidget(QWidget):
    """Reusable live/results plot with markers, holds and data export."""

    status_changed = Signal(str)
    peak_selected = Signal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        legend: bool = True,
        compact_toolbar: bool = False,
        preferred_height: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumPlot")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._compact_toolbar = compact_toolbar
        self._preferred_height = (
            preferred_height
            if preferred_height is not None
            else (180 if compact_toolbar else None)
        )
        self._traces: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._token_owned_primary_curves: set[str] = set()
        self._hold_source: str | None = None
        self._max_hold: np.ndarray | None = None
        self._min_hold: np.ndarray | None = None
        self._marker_x: float | None = None
        self._x_label = "Frequency"
        self._x_unit = "Hz"
        self._theme_name = "dark" if isDarkTheme() else "light"
        self._user_curve_visibility: dict[str, bool] = {}
        self.toolbar_buttons: list[TransparentPushButton] = []

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
            button = TransparentPushButton(text, self)
            button.setObjectName("plotToolButton")
            button.setToolTip(tooltip)
            button.setAccessibleName(text)
            button.clicked.connect(callback)
            self.toolbar_buttons.append(button)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        self.readout = BodyLabel("X: —   Y: —")
        self.readout.setObjectName("plotReadout")
        toolbar.addWidget(self.readout)
        root.addLayout(toolbar)

        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Power", units="dBm")
        self.plot.setMenuEnabled(True)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        self.plot.getPlotItem().setClipToView(True)
        if legend:
            self.plot.addLegend(offset=(10, 10))
        root.addWidget(self.plot, 1)

        initial_tokens = tokens_for(self._theme_name)
        self.crosshair_x = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen(initial_tokens.plot_grid, width=1)
        )
        self.crosshair_y = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(initial_tokens.plot_grid, width=1)
        )
        self.marker = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen(initial_tokens.plot_reference, width=2), label="M1"
        )
        self.delta_marker = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen(initial_tokens.plot_reference, width=2), label="Δ"
        )
        for item in (self.crosshair_x, self.crosshair_y, self.marker, self.delta_marker):
            self.plot.addItem(item, ignoreBounds=True)
        self.marker.hide()
        self.delta_marker.hide()
        self.peak_markers = pg.ScatterPlotItem(
            size=10,
            symbol="o",
            pxMode=True,
            hoverable=True,
        )
        self.plot.addItem(self.peak_markers)
        self.peak_markers.sigClicked.connect(self._peak_marker_clicked)
        self.compliance_markers = pg.ScatterPlotItem(
            size=10,
            symbol="d",
            pxMode=True,
            hoverable=False,
        )
        self.plot.addItem(self.compliance_markers)
        self._selected_peak_index: int | None = None
        self._last_mouse_x: float | None = None
        self._last_readout_position: tuple[float, float] | None = None
        self._mouse_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved
        )
        self.marker.sigPositionChanged.connect(self._marker_changed)
        self.delta_marker.sigPositionChanged.connect(self._marker_changed)
        self.apply_theme(self._theme_name)

    def set_labels(
        self,
        *,
        x: str = "Frequency",
        x_unit: str = "Hz",
        y: str = "Power",
        y_unit: str = "dBm",
    ) -> None:
        self._x_label = x
        self._x_unit = x_unit
        self._last_readout_position = None
        self.plot.setLabel("bottom", x, units=x_unit)
        self.plot.setLabel("left", y, units=y_unit)

    def set_title(self, title: str) -> None:
        self.plot.setTitle(title)

    def set_preferred_height(self, height: int | None) -> None:
        self._preferred_height = height
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        default = super().sizeHint()
        if self._preferred_height is not None:
            return QSize(default.width(), self._preferred_height)
        return default

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        default = super().minimumSizeHint()
        min_h = 100 if self._compact_toolbar else 120
        return QSize(min(default.width(), 200), min_h)

    def apply_theme(self, theme: str) -> None:
        self._theme_name = theme
        palette = plot_theme(tokens_for(theme))
        self.plot.setBackground(palette.background)
        for axis in ("left", "bottom"):
            item = self.plot.getAxis(axis)
            item.setPen(pg.mkPen(palette.axes))
            item.setTextPen(pg.mkPen(palette.axes))
        self.crosshair_x.setPen(pg.mkPen(palette.grid, width=1))
        self.crosshair_y.setPen(pg.mkPen(palette.grid, width=1))
        self.marker.setPen(pg.mkPen(palette.reference, width=2))
        self.delta_marker.setPen(pg.mkPen(palette.reference, width=2))
        self._style_peak_markers()
        tokens = tokens_for(theme)
        danger_color = tokens.danger
        self.compliance_markers.setPen(pg.mkPen(danger_color, width=1.5))
        self.compliance_markers.setBrush(pg.mkBrush(danger_color))
        for name in self._token_owned_primary_curves:
            curve = self._curves.get(name)
            if curve is not None:
                curve.setPen(pg.mkPen(palette.measurement, width=1.6))
        for name, color in (
            ("Max hold", tokens_for(theme).danger),
            ("Min hold", tokens_for(theme).success),
        ):
            curve = self._curves.get(name)
            if curve is not None:
                curve.setPen(pg.mkPen(color, width=1.3))

    def set_trace(
        self,
        name: str,
        x: object,
        y: object,
        *,
        color: str | None = None,
        visible: bool = True,
        primary: bool = False,
        show_points: bool = False,
    ) -> None:
        caller_supplied_color = color is not None
        token_owned_primary = not caller_supplied_color and primary
        if color is None:
            palette = plot_theme(tokens_for(self._theme_name))
            color = palette.measurement if primary else palette.reference
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
        elif token_owned_primary:
            curve.setPen(pg.mkPen(color, width=1.6))
        if token_owned_primary:
            self._token_owned_primary_curves.add(name)
        elif caller_supplied_color:
            self._token_owned_primary_curves.discard(name)
        curve.setData(x_values, y_values)
        curve.setSymbol("o" if show_points else None)
        if show_points:
            curve.setSymbolSize(6)
            curve.setSymbolPen(pg.mkPen(color, width=1))
            curve.setSymbolBrush(pg.mkBrush(color))
        effective_visible = self._user_curve_visibility.get(name, visible)
        curve.setVisible(effective_visible)
        self._sync_legend(name, curve, effective_visible)
        if primary or self._hold_source is None:
            self._hold_source = name
            self._update_holds(x_values, y_values)

    def _sync_legend(self, name: str, curve: pg.PlotDataItem, visible: bool) -> None:
        plot_item = self.plot.getPlotItem()
        legend = plot_item.legend
        if legend is None:
            return
        in_legend = any(label.text == name for _, label in tuple(legend.items))
        if visible:
            if not in_legend:
                legend.addItem(curve, name)
        else:
            if in_legend:
                legend.removeItem(name)
        if len(legend.items) == 0:
            legend.hide()
        else:
            legend.show()

    def set_trace_visibility(self, name: str, visible: bool) -> None:
        self._user_curve_visibility[name] = visible
        curve = self._curves.get(name)
        if curve is not None:
            curve.setVisible(visible)
            self._sync_legend(name, curve, visible)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        }:
            target_theme = "dark" if isDarkTheme() else "light"
            if target_theme != self._theme_name:
                self.apply_theme(target_theme)

    def clear_trace(self, name: str) -> None:
        self._traces.pop(name, None)
        self._token_owned_primary_curves.discard(name)
        curve = self._curves.get(name)
        if curve is not None:
            self._sync_legend(name, curve, False)
            curve.clear()
            curve.hide()
            self._user_curve_visibility.pop(name, None)

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
        self.clear_peak_markers()
        self.clear_compliance_points()
        self._last_mouse_x = None
        self._last_readout_position = None
        self.readout.setText("X: -   Y: -")

    def set_compliance_points(self, x: object, y: object) -> None:
        """Render distinct red diamond markers on points that reached compliance."""
        x_vals = np.asarray(x, dtype=float)
        y_vals = np.asarray(y, dtype=float)
        if x_vals.size == 0 or y_vals.size == 0:
            self.compliance_markers.clear()
            return
        finite = np.isfinite(x_vals) & np.isfinite(y_vals)
        x_clean = x_vals[finite]
        y_clean = y_vals[finite]
        if x_clean.size == 0:
            self.compliance_markers.clear()
            return
        tokens = tokens_for(self._theme_name)
        danger_color = tokens.danger
        self.compliance_markers.setData(
            x=x_clean,
            y=y_clean,
            pen=pg.mkPen(danger_color, width=1.5),
            brush=pg.mkBrush(danger_color),
            size=10,
            symbol="d",
        )

    def clear_compliance_points(self) -> None:
        self.compliance_markers.clear()

    def set_peak_markers(
        self,
        frequencies_hz: object,
        amplitudes: object,
    ) -> None:
        frequencies = np.asarray(frequencies_hz, dtype=float)
        values = np.asarray(amplitudes, dtype=float)
        if frequencies.ndim != 1 or values.ndim != 1 or frequencies.size != values.size:
            raise ValueError("Peak marker frequencies and amplitudes must be equally-sized vectors.")
        finite = np.isfinite(frequencies) & np.isfinite(values)
        spots = [
            {"pos": (float(frequency), float(value)), "data": int(index)}
            for index, (frequency, value) in enumerate(
                zip(frequencies[finite], values[finite], strict=True)
            )
        ]
        self.peak_markers.setData(spots)
        if self._selected_peak_index is not None and self._selected_peak_index >= len(spots):
            self._selected_peak_index = None
        self._style_peak_markers()

    def clear_peak_markers(self) -> None:
        self.peak_markers.clear()
        self._selected_peak_index = None

    def select_peak_marker(self, index: int | None) -> None:
        self._selected_peak_index = index
        self._style_peak_markers()

    def _style_peak_markers(self) -> None:
        if not hasattr(self, "peak_markers"):
            return
        palette = plot_theme(tokens_for(self._theme_name))
        points = self.peak_markers.points()
        for point_index, point in enumerate(points):
            selected = point_index == self._selected_peak_index
            point.setSize(14 if selected else 10)
            point.setPen(
                pg.mkPen(palette.reference if selected else palette.axes, width=2)
            )
            point.setBrush(
                pg.mkBrush(palette.reference if selected else palette.measurement)
            )

    def _peak_marker_clicked(self, _item: object, points: list[object], _event: object) -> None:
        if not points:
            return
        index = int(points[0].data())
        self.select_peak_marker(index)
        self.peak_selected.emit(index)

    def auto_range(self) -> None:
        """Fit visible finite traces once without enabling a growing auto-range.

        ``PlotItem.enableAutoRange()`` remains active after one click.  With a
        live Keithley history that means every subsequent redraw re-applies
        pyqtgraph's padding to the previous range, so repeated Reset clicks
        appear to zoom out forever.  A Reset is intentionally a one-shot,
        deterministic fit to the data that is visible now.
        """

        visible = [
            data
            for name, data in self._traces.items()
            if name in self._curves and self._curves[name].isVisible()
        ]
        if not visible:
            if self._traces:
                visible = list(self._traces.values())
            else:
                self.status_changed.emit("Reset unavailable: no visible finite trace data.")
                return
        x_values = np.concatenate([data[0] for data in visible])
        y_values = np.concatenate([data[1] for data in visible])
        if not x_values.size or not y_values.size:
            self.status_changed.emit("Reset unavailable: no visible finite trace data.")
            return
        x_range = self._stable_data_range(x_values)
        y_range = self._stable_data_range(y_values)
        if x_range is None or y_range is None:
            self.status_changed.emit("Reset unavailable: no visible finite trace data.")
            return
        view_box = self.plot.getViewBox()
        view_box.disableAutoRange()
        view_box.setRange(xRange=x_range, yRange=y_range, padding=0)

    @staticmethod
    def _stable_data_range(values: np.ndarray) -> tuple[float, float] | None:
        """Return a fixed five-percent margin around finite values."""

        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return None
        lower = float(np.min(finite))
        upper = float(np.max(finite))
        if np.isclose(lower, upper, rtol=0.0, atol=1e-15):
            margin = max(abs(lower) * 0.05, 1.0)
        else:
            margin = (upper - lower) * 0.05
        return lower - margin, upper + margin

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
            f"Peak: {self._format_x_value(float(x_values[index]))}, {y_values[index]:.6g}"
        )

    def place_delta_marker(self) -> None:
        if self._last_mouse_x is None:
            self.status_changed.emit("Move the crosshair over the plot before placing a delta marker.")
            return
        self.delta_marker.setPos(self._last_mouse_x)
        self.delta_marker.show()
        self._marker_changed()

    def toggle_max_hold(self) -> None:
        self._toggle_hold("Max hold", tokens_for(self._theme_name).danger)

    def toggle_min_hold(self) -> None:
        self._toggle_hold("Min hold", tokens_for(self._theme_name).success)

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
        try:
            if "PNG" in selected or suffix == ".png":
                ImageExporter(self.plot.plotItem).export(path)
            elif "SVG" in selected or suffix == ".svg":
                SVGExporter(self.plot.plotItem).export(path)
            else:
                self._export_csv(Path(path))
        except Exception as exc:
            self.status_changed.emit(f"Spectrum export failed: {exc}")
            return
        self.status_changed.emit(f"Spectrum exported to {path}")

    def _export_csv(self, path: Path) -> None:
        visible = [
            (name, *self._traces[name])
            for name, curve in self._curves.items()
            if curve.isVisible() and name in self._traces
        ]
        # The save dialog already obtains explicit overwrite consent.  Honour
        # that decision instead of failing afterwards with FileExistsError.
        with path.open("w", newline="", encoding="utf-8") as handle:
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
            ("Max hold", np.maximum, "_max_hold", tokens_for(self._theme_name).danger),
            ("Min hold", np.minimum, "_min_hold", tokens_for(self._theme_name).success),
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
        if not self._readout_position_changed(float(point.x()), float(point.y())):
            return
        self.readout.setText(f"X: {self._format_x_value(float(point.x()))}   Y: {point.y():.6g}")

    def _readout_position_changed(self, x: float, y: float) -> bool:
        """Limit text formatting while keeping the crosshair motion one-to-one."""

        previous = self._last_readout_position
        x_range, y_range = self.plot.viewRange()
        x_per_pixel = abs(float(x_range[1]) - float(x_range[0])) / max(
            self.plot.width(), 1
        )
        y_per_pixel = abs(float(y_range[1]) - float(y_range[0])) / max(
            self.plot.height(), 1
        )
        changed = (
            previous is None
            or abs(x - previous[0]) >= 2.0 * x_per_pixel
            or abs(y - previous[1]) >= 2.0 * y_per_pixel
        )
        if changed:
            self._last_readout_position = (x, y)
        return changed

    def _marker_changed(self) -> None:
        if not self.marker.isVisible():
            return
        delta = ""
        if self.delta_marker.isVisible():
            difference = float(self.delta_marker.value() - self.marker.value())
            delta = f"   ΔX: {self._format_x_value(difference)}"
        self.readout.setText(f"M1 X: {self._format_x_value(float(self.marker.value()))}{delta}")

    def _format_x_value(self, value: float) -> str:
        """Format an axis value with an appropriate engineering SI prefix."""

        if not np.isfinite(value):
            return str(value)
        if not self._x_unit:
            return f"{value:.9g}"
        magnitude = abs(value)
        if magnitude == 0:
            return f"0 {self._x_unit}"
        for scale, prefix in (
            (1e12, "T"),
            (1e9, "G"),
            (1e6, "M"),
            (1e3, "k"),
            (1.0, ""),
            (1e-3, "m"),
            (1e-6, "µ"),
            (1e-9, "n"),
            (1e-12, "p"),
        ):
            if magnitude >= scale:
                return f"{value / scale:.9g} {prefix}{self._x_unit}"
        return f"{value:.9g} {self._x_unit}"
