"""Interactive, peak-preserving scientific spectrum plot."""

from __future__ import annotations

import csv
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, FlowLayout, TransparentPushButton, isDarkTheme
from shiboken6 import isValid

from app.ui.design_system import plot_theme, tokens_for
from app.ui.design_system.paper_plot_style import PAPER_STYLE
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.widgets.plot_ownership import (
    create_plot_widget,
    own_signal_proxy,
    set_plot_export_action,
)
from app.ui.widgets.plot_style import apply_scientific_style

_UNIT_CSV_COLUMNS = {
    "dBm": "power_dbm",
    "dB": "delta_db",
    "W": "power_w",
    "ratio": "ratio_linear",
    "linear ratio": "ratio_linear",
    "mW²": "power_mw2",
    "W²": "power_w2",
}


class SpectrumPlotWidget(QWidget):
    """Reusable live/results plot with markers, holds and data export."""

    status_changed = Signal(str)
    peak_selected = Signal(int)
    traces_updated = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        legend: bool = True,
        compact_toolbar: bool = False,
        responsive_toolbar: bool = False,
        preferred_height: int | None = None,
        csv_value_column: str = "value",
        plot_widget_factory=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumPlot")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._compact_toolbar = compact_toolbar
        self._csv_value_column = csv_value_column
        self._preferred_height = (
            preferred_height
            if preferred_height is not None
            else (180 if compact_toolbar else None)
        )
        self._traces: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._legend_labels: dict[str, str] = {}
        self._estimated_bands_hz: tuple[tuple[float, float], ...] = ()
        self._estimated_band_regions: list[pg.LinearRegionItem] = []
        self._plot_title: str | None = None
        self._token_owned_primary_curves: set[str] = set()
        self._hold_source: str | None = None
        self._max_hold: np.ndarray | None = None
        self._min_hold: np.ndarray | None = None
        self._marker_x: float | None = None
        self._x_label = "Frequency"
        self._x_unit = "Hz"
        self._y_label = "Power"
        self._y_unit = "dBm"
        self._theme_name = "dark" if isDarkTheme() else "light"
        self._user_curve_visibility: dict[str, bool] = {}
        self._batch_depth = 0
        self._batch_dirty = False
        self._batch_legend_dirty = False
        self._cleaned_up = False
        self.toolbar_buttons: list[TransparentPushButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        toolbar = FlowLayout() if responsive_toolbar else QHBoxLayout()
        self.toolbar_layout = toolbar
        if responsive_toolbar:
            toolbar.setContentsMargins(0, 0, 0, 0)
            toolbar.setHorizontalSpacing(4)
            toolbar.setVerticalSpacing(4)
        else:
            toolbar.setSpacing(4)
        actions = (
            ("Reset", "Reset zoom and show all finite data", self.auto_range),
            ("Peak", "Move the primary marker to the highest visible point", self.peak_search),
            ("Δ marker", "Place a delta marker at the current crosshair position", self.place_delta_marker),
            ("Max hold", "Toggle a maximum hold of the primary trace", self.toggle_max_hold),
            ("Min hold", "Toggle a minimum hold of the primary trace", self.toggle_min_hold),
            ("Clear hold", "Clear maximum and minimum hold traces", self.clear_holds),
            ("Export", f"{PAPER_STYLE.description}. Export visible traces to CSV, PNG or SVG.", self.export),
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
        if not responsive_toolbar:
            toolbar.addStretch(1)
        self.readout = BodyLabel("X: —   Y: —")
        self.readout.setObjectName("plotReadout")
        toolbar.addWidget(self.readout)
        root.addLayout(toolbar)

        self.plot = create_plot_widget(factory=plot_widget_factory)
        set_plot_export_action(self.plot, self.export)
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
        marker_label_opts = self._marker_label_opts(self._theme_name)
        self.crosshair_x = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen(initial_tokens.plot_grid, width=1)
        )
        self.crosshair_y = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(initial_tokens.plot_grid, width=1)
        )
        self.marker = pg.InfiniteLine(
            angle=90,
            movable=True,
            pen=pg.mkPen(initial_tokens.plot_reference, width=2),
            label="M1",
            labelOpts=dict(marker_label_opts),
        )
        self.delta_marker = pg.InfiniteLine(
            angle=90,
            movable=True,
            pen=pg.mkPen(initial_tokens.plot_reference, width=2),
            label="Δ",
            labelOpts={**marker_label_opts, "position": 0.84},
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
        self.replacement_markers = pg.ScatterPlotItem(size=7, symbol="x", pxMode=True)
        self.plot.addItem(self.replacement_markers)
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
        own_signal_proxy(self._mouse_proxy, self)
        self.marker.sigPositionChanged.connect(self._marker_changed)
        self.delta_marker.sigPositionChanged.connect(self._marker_changed)
        self.apply_theme(self._theme_name)

    @staticmethod
    def _marker_label_opts(theme: str) -> dict[str, object]:
        tokens = tokens_for(theme)
        fill = pg.mkColor(tokens.surface_raised if theme == "dark" else tokens.surface)
        fill.setAlpha(235)
        border = pg.mkColor(tokens.plot_reference)
        return {
            "color": tokens.text_primary,
            "fill": pg.mkBrush(fill),
            "border": pg.mkPen(border, width=1),
            "position": 0.92,
        }

    def _apply_marker_label_style(self, theme: str) -> None:
        opts = self._marker_label_opts(theme)
        for line in (self.marker, self.delta_marker):
            label = getattr(line, "label", None)
            if label is None:
                continue
            label.setColor(opts["color"])
            label.fill = opts["fill"]
            label.border = opts["border"]
            label.update()

    @property
    def in_batch(self) -> bool:
        return self._batch_depth > 0

    def begin_batch(self) -> None:
        if self._batch_depth == 0:
            self._batch_dirty = False
            self._batch_legend_dirty = False
            self.plot.setUpdatesEnabled(False)
        self._batch_depth += 1

    def end_batch(self) -> None:
        if self._batch_depth == 0:
            return
        self._batch_depth -= 1
        if self._batch_depth > 0:
            return
        if self._batch_legend_dirty:
            legend = self.plot.getPlotItem().legend
            if legend is not None:
                legend.updateSize()
                legend.setVisible(len(legend.items) > 0)
            self._batch_legend_dirty = False
        self.plot.setUpdatesEnabled(True)
        if self._batch_dirty:
            self._batch_dirty = False
            self.traces_updated.emit()

    @contextmanager
    def batch_updates(self):
        """Coalesce multiple trace/label mutations into one repaint and update signal."""
        self.begin_batch()
        try:
            yield
        finally:
            self.end_batch()

    def _notify_traces_updated(self) -> None:
        if self._batch_depth > 0:
            self._batch_dirty = True
        else:
            self.traces_updated.emit()

    def set_labels(
        self,
        *,
        x: str = "Frequency",
        x_unit: str = "Hz",
        y: str = "Power",
        y_unit: str = "dBm",
    ) -> None:
        if (
            x == self._x_label
            and x_unit == self._x_unit
            and y == self._y_label
            and y_unit == self._y_unit
        ):
            return
        units_changed = x_unit != self._x_unit or y_unit != self._y_unit
        if units_changed:
            self.clear_holds()
        self._x_label = x
        self._x_unit = x_unit
        self._y_label = y
        self._y_unit = y_unit
        self._last_readout_position = None
        self.plot.setLabel("bottom", x, units=x_unit)
        self.plot.setLabel("left", y, units=y_unit)
        if units_changed:
            apply_scientific_style(self.plot.plotItem, self._theme_name)

    def set_title(self, title: str) -> None:
        if title != self._plot_title:
            self._plot_title = title
            self.plot.setTitle(title)

    def set_preferred_height(self, height: int | None) -> None:
        self._preferred_height = height
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        default = super().sizeHint()
        if self._preferred_height is not None:
            return QSize(default.width(), self._preferred_height)
        return default

    def minimumSizeHint(self) -> QSize:
        default = super().minimumSizeHint()
        min_h = 100 if self._compact_toolbar else 120
        return QSize(min(default.width(), 200), min_h)

    def apply_theme(self, theme: str) -> None:
        self._theme_name = theme
        if getattr(self, "_cleaned_up", False):
            return
        plot = getattr(self, "plot", None)
        if plot is None or not isValid(plot):
            return
        plot_item = getattr(plot, "plotItem", None)
        if plot_item is None or not isValid(plot_item):
            return
        palette = plot_theme(tokens_for(theme))
        plot.setBackground(palette.background)
        apply_scientific_style(plot_item, theme)
        self.crosshair_x.setPen(pg.mkPen(palette.grid, width=1))
        self.crosshair_y.setPen(pg.mkPen(palette.grid, width=1))
        self.marker.setPen(pg.mkPen(palette.reference, width=2))
        self.delta_marker.setPen(pg.mkPen(palette.reference, width=2))
        self._apply_marker_label_style(theme)
        self._style_peak_markers()
        self.replacement_markers.setPen(pg.mkPen(palette.reference))
        estimate_brush = self._estimated_band_brush()
        for region in self._estimated_band_regions:
            region.setBrush(estimate_brush)
            region.setHoverBrush(estimate_brush)
        danger_color = palette.danger
        self.compliance_markers.setPen(pg.mkPen(danger_color, width=1.5))
        self.compliance_markers.setBrush(pg.mkBrush(danger_color))
        for name in self._token_owned_primary_curves:
            curve = self._curves.get(name)
            if curve is not None:
                curve.setPen(pg.mkPen(palette.measurement, width=self._trace_line_width(name)))
        for name, color in (
            ("Max hold", palette.danger),
            ("Min hold", palette.success),
        ):
            curve = self._curves.get(name)
            if curve is not None:
                curve.setPen(pg.mkPen(color, width=1.3))

    def _estimated_band_brush(self):
        color = pg.mkColor(plot_theme(tokens_for(self._theme_name)).reference)
        color.setAlpha(45)
        return pg.mkBrush(color)

    def set_estimated_bands(self, bands_hz: tuple[tuple[float, float], ...]) -> None:
        """Shade estimated samples without affecting zoom, markers or data export."""
        if bands_hz == self._estimated_bands_hz:
            return
        for region in self._estimated_band_regions:
            self.plot.removeItem(region)
        self._estimated_band_regions.clear()
        self._estimated_bands_hz = bands_hz
        brush = self._estimated_band_brush()
        pen = pg.mkPen(None)
        for bounds in bands_hz:
            region = pg.LinearRegionItem(bounds, movable=False, brush=brush, hoverBrush=brush, pen=pen, hoverPen=pen)
            region.setToolTip("Estimate from neighbouring frequencies")
            region.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            region.setZValue(-5)
            self.plot.addItem(region, ignoreBounds=True)
            self._estimated_band_regions.append(region)

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
        legend_label: str | None = None,
        opacity: float | None = None,
        z_value: float | None = None,
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
        all_finite = bool(np.all(finite))
        # Missing samples are gaps, not permission to join their neighbours.
        if not all_finite:
            y_values = np.where(finite, y_values, np.nan)
        self._traces[name] = (x_values, y_values)
        self._legend_labels[name] = legend_label or name
        line_width = self._trace_line_width(name)
        pen = pg.mkPen(color, width=line_width)
        data_kwargs: dict[str, object] = {
            "connect": "finite",
            "symbol": "o" if show_points else None,
        }
        if show_points:
            data_kwargs["symbolSize"] = 6
            data_kwargs["symbolPen"] = pg.mkPen(color, width=1)
            data_kwargs["symbolBrush"] = pg.mkBrush(color)
        curve = self._curves.get(name)
        if curve is None:
            curve = pg.PlotDataItem(name=name)
            data_kwargs["pen"] = pen
            self.plot.addItem(curve)
            self._curves[name] = curve
        elif token_owned_primary or caller_supplied_color or curve.opts["pen"].widthF() != line_width:
            data_kwargs["pen"] = pen
        # Clipping needs the parent ViewBox, which is only set after addItem.
        curve.opts["autoDownsample"] = all_finite
        curve.opts["downsampleMethod"] = "peak"
        curve.opts["clipToView"] = all_finite
        curve.setData(x_values, y_values, **data_kwargs)
        if opacity is not None:
            curve.setOpacity(opacity)
        if z_value is not None:
            curve.setZValue(z_value)
        if token_owned_primary:
            self._token_owned_primary_curves.add(name)
        elif caller_supplied_color:
            self._token_owned_primary_curves.discard(name)
        effective_visible = self._user_curve_visibility.get(name, visible)
        if curve.isVisible() != effective_visible:
            curve.setVisible(effective_visible)
        self._sync_legend(name, curve, effective_visible)
        if primary or self._hold_source is None:
            if self._hold_source is not None and self._hold_source != name:
                self.clear_holds()
            self._hold_source = name
            self._update_holds(x_values, y_values)
        self._notify_traces_updated()

    def _trace_line_width(self, name: str) -> float:
        # A one-pixel pen enables Qt's fast path for dense scientific traces.
        # Full-resolution arrays, extrema, markers and exports remain intact.
        data = self._traces.get(name)
        return 1.0 if data is not None and data[0].size > 2000 else 1.6

    def _sync_legend(self, name: str, curve: pg.PlotDataItem, visible: bool) -> None:
        plot_item = self.plot.getPlotItem()
        legend = plot_item.legend
        if legend is None:
            return
        entry = next((label for sample, label in tuple(legend.items) if sample.item is curve), None)
        if visible:
            text = self._legend_labels.get(name, name)
            if entry is None:
                legend.addItem(curve, text)
                if self._batch_depth > 0:
                    self._batch_legend_dirty = True
            elif entry.text != text:
                entry.setText(text, color=legend.opts["labelTextColor"], size=legend.opts["labelTextSize"])
                if self._batch_depth > 0:
                    self._batch_legend_dirty = True
                else:
                    legend.updateSize()
        else:
            if entry is not None:
                legend.removeItem(curve)
                if self._batch_depth > 0:
                    self._batch_legend_dirty = True
        if self._batch_depth == 0:
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

    def set_legend_order(self, names: list[str]) -> None:
        """Keep the processing stages in order when comparison curves return."""
        legend = self.plot.getPlotItem().legend
        if legend is None:
            return
        entries = [(sample.item, label.text) for sample, label in tuple(legend.items)]
        positions = {self._curves[name]: index for index, name in enumerate(names) if name in self._curves}
        ordered = sorted(entries, key=lambda entry: positions.get(entry[0], len(positions)))
        if ordered == entries:
            return
        legend.clear()
        for curve, label in ordered:
            legend.addItem(curve, label)

    def changeEvent(self, event: QEvent) -> None:
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
        had_trace = name in self._traces
        self._traces.pop(name, None)
        self._legend_labels.pop(name, None)
        self._token_owned_primary_curves.discard(name)
        curve = self._curves.get(name)
        if curve is not None:
            self._sync_legend(name, curve, False)
            curve.clear()
            curve.hide()
            self._user_curve_visibility.pop(name, None)
        if had_trace:
            self._notify_traces_updated()

    def trace_point_count(self, name: str) -> int:
        """Return the finite point count for GUI tests and status reporting."""

        data = self._traces.get(name)
        return 0 if data is None else int(np.count_nonzero(np.isfinite(data[0]) & np.isfinite(data[1])))

    def clear(self) -> None:
        for name in tuple(self._curves):
            self.clear_trace(name)
        self.set_estimated_bands(())
        self.clear_holds()
        self.marker.hide()
        self.delta_marker.hide()
        self.clear_peak_markers()
        self.clear_replacement_markers()
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
        danger_color = plot_theme(tokens_for(self._theme_name)).danger
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
        *,
        interactive: bool = True,
    ) -> None:
        frequencies = np.asarray(frequencies_hz, dtype=float)
        values = np.asarray(amplitudes, dtype=float)
        if frequencies.ndim != 1 or values.ndim != 1 or frequencies.size != values.size:
            raise ValueError("Peak marker frequencies and amplitudes must be equally-sized vectors.")
        finite = np.isfinite(frequencies) & np.isfinite(values)
        valid_indices = np.flatnonzero(finite)
        spots = [
            {
                "pos": (float(frequencies[orig_index]), float(values[orig_index])),
                "data": int(orig_index) if interactive else None,
            }
            for orig_index in valid_indices
        ]
        self.peak_markers.setData(spots)
        if not interactive or self._selected_peak_index is not None and (
            self._selected_peak_index < 0
            or self._selected_peak_index >= frequencies.size
            or not bool(finite[self._selected_peak_index])
        ):
            self._selected_peak_index = None
        self._style_peak_markers()

    def clear_peak_markers(self) -> None:
        self.peak_markers.clear()
        self._selected_peak_index = None

    def set_replacement_markers(self, frequencies_hz: object, amplitudes: object) -> None:
        frequencies = np.asarray(frequencies_hz, dtype=float)
        values = np.asarray(amplitudes, dtype=float)
        if frequencies.ndim != 1 or values.ndim != 1 or frequencies.size != values.size:
            raise ValueError("Replacement marker coordinates must be equally-sized vectors.")
        finite = np.isfinite(frequencies) & np.isfinite(values)
        self.replacement_markers.setData(frequencies[finite], values[finite])

    def clear_replacement_markers(self) -> None:
        self.replacement_markers.clear()

    def select_peak_marker(self, index: int | None) -> None:
        self._selected_peak_index = index
        self._style_peak_markers()

    def _style_peak_markers(self) -> None:
        if not hasattr(self, "peak_markers"):
            return
        palette = plot_theme(tokens_for(self._theme_name))
        points = self.peak_markers.points()
        for point_index, point in enumerate(points):
            point_data = point.data()
            selected = self._selected_peak_index is not None and (
                point_data == self._selected_peak_index
                if point_data is not None
                else point_index == self._selected_peak_index
            )
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
        if points[0].data() is None:
            return  # Replacement locations are informational, not peak-table rows.
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
        y_range = self._stable_data_range(
            y_values, zero_margin=1e-15 if self._y_unit == "W" else 1.0,
            constant_relative=self._y_unit == "W",
        )
        if x_range is None or y_range is None:
            self.status_changed.emit("Reset unavailable: no visible finite trace data.")
            return
        view_box = self.plot.getViewBox()
        view_box.disableAutoRange()
        view_box.setRange(xRange=x_range, yRange=y_range, padding=0)

    @staticmethod
    def _stable_data_range(values: np.ndarray, *, zero_margin: float = 1.0,
                           constant_relative: bool = False) -> tuple[float, float] | None:
        """Return the paper style's fixed margin around finite values."""

        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return None
        lower = float(np.min(finite))
        upper = float(np.max(finite))
        if lower == upper:
            margin = abs(lower) * PAPER_STYLE.number("axes.ymargin") if lower != 0 else zero_margin
            if not constant_relative:
                margin = max(margin, zero_margin)
        else:
            margin = (upper - lower) * PAPER_STYLE.number("axes.ymargin")
        return lower - margin, upper + margin

    def peak_search(self) -> None:
        data = self._primary_data()
        if data is None or data[0].size == 0:
            self.status_changed.emit("Peak search unavailable: no finite trace data.")
            return
        x_values, y_values = data
        valid = np.flatnonzero(np.isfinite(x_values) & np.isfinite(y_values))
        if not valid.size:
            self.status_changed.emit("Peak search unavailable: no finite trace data.")
            return
        index = int(valid[np.argmax(y_values[valid])])
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
        self._toggle_hold("Max hold", plot_theme(tokens_for(self._theme_name)).danger)

    def toggle_min_hold(self) -> None:
        self._toggle_hold("Min hold", plot_theme(tokens_for(self._theme_name)).success)

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
            if suffix in {".png", ".svg"} or "PNG" in selected or "SVG" in selected:
                self._export_figure(path)
            else:
                self._export_csv(Path(path))
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            self.status_changed.emit(f"Spectrum export failed: {exc}")
            return
        self.status_changed.emit(f"Spectrum exported to {path}")

    def _export_figure(self, path):
        from app.ui.widgets.figure_export import export_spectrum_figure

        screen_palette = plot_theme(tokens_for(self._theme_name))
        pub_palette = plot_theme(tokens_for("light"), publication=True)
        screen_to_pub = {
            screen_palette.measurement.lower(): pub_palette.measurement,
            screen_palette.reference.lower(): pub_palette.reference,
            screen_palette.danger.lower(): pub_palette.danger,
            screen_palette.success.lower(): pub_palette.success,
        }

        def publication_color(raw_color: str, *, curve_name: str | None = None) -> str:
            if curve_name in self._token_owned_primary_curves:
                return pub_palette.measurement
            if curve_name == "Max hold":
                return pub_palette.danger
            if curve_name == "Min hold":
                return pub_palette.success
            return screen_to_pub.get(raw_color.lower(), raw_color)

        traces = []
        for name, (x, y) in self._traces.items():
            curve = self._curves.get(name)
            if curve is not None and curve.isVisible():
                pen = pg.mkPen(curve.opts["pen"])
                traces.append({
                    "name": self._legend_labels.get(name, name),
                    "x": x,
                    "y": y,
                    "color": publication_color(pen.color().name(), curve_name=name),
                    "width": pen.widthF(),
                    "symbol": curve.opts.get("symbol"),
                    "symbol_size": curve.opts.get("symbolSize", 6),
                })
        markers = []
        marker_pairs = [(self.marker, "M1"), (self.delta_marker, "Δ")]
        marker_pairs.extend((line, name) for name, line in getattr(self, "frequency_markers", {}).items())
        for line, label in marker_pairs:
            if line.isVisible():
                markers.append({
                    "position": float(line.value()),
                    "label": label,
                    "color": publication_color(line.pen.color().name()),
                })
        band = (tuple(self.band.getRegion()) if getattr(self, "band_enabled", None) is not None
                and self.band_enabled.isChecked() else None)
        points = []
        for item in (self.peak_markers, self.replacement_markers, self.compliance_markers):
            if item.isVisible():
                groups = {}
                for point in item.points():
                    symbol = point.symbol()
                    # Open crosses are drawn entirely by their pen.
                    paint = point.pen() if symbol in {"x", "+"} else point.brush()
                    resolved_color = publication_color(paint.color().name())
                    key = (resolved_color, symbol, point.size())
                    group = groups.setdefault(key, {"x": [], "y": [], "color": key[0],
                                                    "symbol": key[1], "size": key[2]})
                    group["x"].append(point.pos().x())
                    group["y"].append(point.pos().y())
                points.extend(groups.values())
        return export_spectrum_figure(path, traces=traces, view_ranges=self.plot.viewRange(),
            x_label=self._x_label, x_unit=self._x_unit,
            y_label=self.plot.getAxis("left").labelText, y_unit=self._y_unit,
            title=self.plot.plotItem.titleLabel.item.toPlainText(), markers=markers, band=band, points=points,
            log_mode=(self.plot.plotItem.ctrl.logXCheck.isChecked(), self.plot.plotItem.ctrl.logYCheck.isChecked()),
            inverted_axes=(bool(self.plot.getViewBox().state["xInverted"]),
                           bool(self.plot.getViewBox().state["yInverted"])))

    def _resolved_csv_value_column(self) -> str:
        if self._csv_value_column and self._csv_value_column != "value":
            return self._csv_value_column
        if self._y_unit in _UNIT_CSV_COLUMNS:
            return _UNIT_CSV_COLUMNS[self._y_unit]
        if self._y_unit:
            sanitized_unit = "".join(
                ch if ch.isalnum() else "_" for ch in self._y_unit
            ).strip("_")
            if sanitized_unit:
                return f"value_{sanitized_unit}"
        return self._csv_value_column or "value"

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
            writer.writerow(["trace", x_header, self._resolved_csv_value_column()])
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
        palette = plot_theme(tokens_for(self._theme_name))
        for name, operation, attribute, color in (
            ("Max hold", np.fmax, "_max_hold", palette.danger),
            ("Min hold", np.fmin, "_min_hold", palette.success),
        ):
            curve = self._curves.get(name)
            if curve is None or not curve.isVisible():
                continue
            previous = getattr(self, attribute)
            previous_trace = self._traces.get(name)
            same_grid = previous_trace is not None and np.array_equal(previous_trace[0], x_values, equal_nan=True)
            held = y_values.copy() if previous is None or not same_grid else operation(previous, y_values)
            setattr(self, attribute, held)
            self._traces[name] = (x_values, held)
            finite = np.isfinite(x_values) & np.isfinite(held)
            all_finite = bool(np.all(finite))
            curve.opts["autoDownsample"] = all_finite
            curve.opts["downsampleMethod"] = "peak"
            curve.opts["clipToView"] = all_finite
            curve.setData(
                x_values,
                held,
                connect="finite",
                pen=pg.mkPen(color, width=1.3),
            )

    def _stop_mouse_proxy_timer(self) -> None:
        proxy = getattr(self, "_mouse_proxy", None)
        if proxy is None or not isValid(proxy):
            return
        timer = getattr(proxy, "timer", None)
        if timer is not None and isValid(timer):
            timer.stop()

    def cleanup_plot(self) -> None:
        """Explicitly disconnect scene signals and release graphics items on teardown."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._stop_mouse_proxy_timer()
        proxy = getattr(self, "_mouse_proxy", None)
        if proxy is not None and isValid(proxy):
            try:
                proxy.disconnect()
            except (RuntimeError, TypeError):
                pass
        plot = getattr(self, "plot", None)
        if plot is not None and isValid(plot):
            plot.hide()
            plot.setUpdatesEnabled(False)
            plot_item = getattr(plot, "plotItem", None)
            if plot_item is not None and isValid(plot_item):
                grid = getattr(plot_item, "_paper_grid", None)
                if grid is not None and isValid(grid):
                    try:
                        plot_item.removeItem(grid)
                        grid.deleteLater()
                    except (RuntimeError, TypeError):
                        pass
                plot_item._paper_grid = None
                try:
                    plot.close()
                except RuntimeError:
                    pass
            # PyQtGraph drops the QWidget parent in PlotWidget.close().
            # Release the detached view while the Qt event dispatcher is alive.
            if isValid(plot):
                plot.deleteLater()

    def hideEvent(self, event) -> None:
        self._stop_mouse_proxy_timer()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._stop_mouse_proxy_timer()
        if self.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose):
            self.cleanup_plot()
        super().closeEvent(event)

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
