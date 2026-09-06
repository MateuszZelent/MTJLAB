"""Interactive pyqtgraph plot widget for single and multi-curve measurement inspection."""

from __future__ import annotations

from typing import Sequence

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    PushButton,
    SubtitleLabel,
    ToolButton,
    isDarkTheme,
)

from app.storage.hdf5_series_reader import MeasurementSeries
from app.ui.design_system import plot_theme, tokens_for


class MeasurementPlotWidget(QWidget):
    """Interactive curve display with autoscale, cursor tracking, and multi-curve overlay."""

    y_channel_changed = Signal(str)

    _OVERLAY_COLORS = (
        QColor(30, 102, 245),    # Blue
        QColor(64, 160, 43),     # Green
        QColor(220, 38, 38),     # Red
        QColor(136, 57, 239),    # Purple
        QColor(223, 142, 29),    # Orange
        QColor(4, 165, 229),     # Cyan
        QColor(234, 118, 203),   # Pink
        QColor(23, 146, 153),    # Teal
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_series: MeasurementSeries | None = None
        self._multi_series: list[MeasurementSeries] = []
        self._plot_items: list[pg.PlotDataItem] = []
        self._log_y = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 2, 4, 2)
        toolbar.setSpacing(6)

        self.title_label = SubtitleLabel("Measurement Plot", self)
        self.title_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        toolbar.addWidget(self.title_label)

        self.channel_combo = ComboBox(self)
        self.channel_combo.setToolTip("Select measurement channel for Y axis")
        self.channel_combo.currentIndexChanged.connect(self._on_channel_combo_changed)
        self.channel_combo.hide()
        toolbar.addWidget(self.channel_combo)

        toolbar.addStretch(1)

        self.autoscale_btn = ToolButton(FluentIcon.SYNC, self)
        self.autoscale_btn.setToolTip("Autoscale / Reset View")
        self.autoscale_btn.clicked.connect(self.reset_view)
        toolbar.addWidget(self.autoscale_btn)

        self.grid_btn = ToolButton(FluentIcon.TILES, self)
        self.grid_btn.setToolTip("Toggle Grid")
        self.grid_btn.clicked.connect(self.toggle_grid)
        toolbar.addWidget(self.grid_btn)

        self.log_btn = PushButton("Log Y", self)
        self.log_btn.setCheckable(True)
        self.log_btn.setFixedHeight(28)
        self.log_btn.setToolTip("Toggle Logarithmic Y Scale")
        self.log_btn.clicked.connect(self.toggle_log_y)
        toolbar.addWidget(self.log_btn)

        self.readout_label = CaptionLabel("X: —   Y: —", self)
        self.readout_label.setStyleSheet("font-family: monospace; font-size: 11px;")
        toolbar.addWidget(self.readout_label)

        layout.addLayout(toolbar)

        # Stack: 0 -> Empty state, 1 -> Plot canvas
        self.stack = QStackedWidget(self)

        # Empty State
        empty_card = QWidget(self.stack)
        empty_layout = QVBoxLayout(empty_card)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = BodyLabel("Select a measurement to view curve", empty_card)
        self.empty_title.setStyleSheet("font-weight: 600; color: palette(placeholderText);")
        self.empty_desc = CaptionLabel("Single clicks open curve preview; check multiple items to overlay.", empty_card)
        self.empty_desc.setStyleSheet("color: palette(placeholderText);")
        empty_layout.addWidget(self.empty_title, 0, Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_desc, 0, Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(empty_card)

        # Plot Widget
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        self.plot.getPlotItem().setClipToView(True)

        self.legend = self.plot.addLegend(offset=(10, 10))
        self.legend.hide()

        # Crosshairs
        theme_name = "dark" if isDarkTheme() else "light"
        tokens = tokens_for(theme_name)
        self.crosshair_x = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen(tokens.plot_grid, width=1, style=Qt.PenStyle.DashLine)
        )
        self.crosshair_y = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(tokens.plot_grid, width=1, style=Qt.PenStyle.DashLine)
        )
        self.plot.addItem(self.crosshair_x, ignoreBounds=True)
        self.plot.addItem(self.crosshair_y, ignoreBounds=True)
        self.crosshair_x.hide()
        self.crosshair_y.hide()

        self._mouse_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._on_mouse_moved
        )

        self.stack.addWidget(self.plot)
        layout.addWidget(self.stack, 1)

        self._grid_enabled = True
        self.apply_theme()

    def apply_theme(self) -> None:
        """Sync plot colors with Fluent dark/light theme tokens."""
        theme_name = "dark" if isDarkTheme() else "light"
        tokens = tokens_for(theme_name)
        palette = plot_theme(tokens)

        self.plot.setBackground(None)
        plot_item = self.plot.getPlotItem()

        axis_pen = pg.mkPen(palette.axes, width=1)

        for axis_name in ("bottom", "left", "top", "right"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(axis_pen)
            axis.setTextPen(axis_pen)

        self.crosshair_x.setPen(pg.mkPen(tokens.plot_grid, width=1, style=Qt.PenStyle.DashLine))
        self.crosshair_y.setPen(pg.mkPen(tokens.plot_grid, width=1, style=Qt.PenStyle.DashLine))

    def reset_view(self) -> None:
        self.plot.enableAutoRange()
        self.plot.autoRange()

    def toggle_grid(self) -> None:
        self._grid_enabled = not self._grid_enabled
        self.plot.showGrid(x=self._grid_enabled, y=self._grid_enabled, alpha=0.2 if self._grid_enabled else 0.0)

    def toggle_log_y(self) -> None:
        self._log_y = not self._log_y
        self.log_btn.setChecked(self._log_y)
        self.plot.setLogMode(x=False, y=self._log_y)
        self.reset_view()

    def clear(self) -> None:
        """Clear all plot curves and return to empty placeholder."""
        self._current_series = None
        self._multi_series.clear()
        self._log_y = False
        self.log_btn.setChecked(False)
        self.plot.setLogMode(x=False, y=False)
        self.plot.clear()
        self._plot_items.clear()
        if self.legend:
            self.legend.clear()
            self.legend.hide()
        self.plot.addItem(self.crosshair_x, ignoreBounds=True)
        self.plot.addItem(self.crosshair_y, ignoreBounds=True)
        self.crosshair_x.hide()
        self.crosshair_y.hide()
        self.channel_combo.hide()
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.blockSignals(False)
        self.title_label.setText("Measurement Plot")
        self.readout_label.setText("X: —   Y: —")
        self.stack.setCurrentIndex(0)

    def set_series(self, series: MeasurementSeries) -> None:
        """Display a single measurement curve with title, labels, and channel options."""
        self.clear()
        if series.is_empty:
            self.empty_title.setText(f"No numeric curve data in {series.title}")
            self.stack.setCurrentIndex(0)
            return

        self._current_series = series
        self.stack.setCurrentIndex(1)
        self.title_label.setText(series.title)

        # Setup Axes labels
        self.plot.setLabel("bottom", series.x_label, units=series.x_unit or None)
        self.plot.setLabel("left", series.y_label, units=series.y_unit or None)

        # Populate available channels if multiple
        if len(series.available_y_channels) > 1:
            self.channel_combo.blockSignals(True)
            self.channel_combo.clear()
            self.channel_combo.addItems(list(series.available_y_channels))
            for idx, ch in enumerate(series.available_y_channels):
                if series.y_label.lower() in ch.lower() or ch.lower() in series.y_label.lower():
                    self.channel_combo.setCurrentIndex(idx)
                    break
            self.channel_combo.blockSignals(False)
            self.channel_combo.show()
        else:
            self.channel_combo.hide()

        # Plot curve
        theme_name = "dark" if isDarkTheme() else "light"
        tokens = tokens_for(theme_name)
        pen = pg.mkPen(tokens.accent, width=2)

        curve = self.plot.plot(
            series.x_values,
            series.y_values,
            pen=pen,
            symbol="o",
            symbolSize=4,
            symbolBrush=pg.mkBrush(tokens.accent),
            symbolPen=None,
            name=series.y_label,
        )
        self._plot_items.append(curve)
        self.crosshair_x.show()
        self.crosshair_y.show()
        self.reset_view()

    def set_multi_series(self, series_list: Sequence[MeasurementSeries]) -> None:
        """Overlay multiple curves with distinct palette colors and a visible legend."""
        self.clear()
        valid = [s for s in series_list if not s.is_empty]
        if not valid:
            self.empty_title.setText("Selected measurements contain no numeric data")
            self.stack.setCurrentIndex(0)
            return

        self._multi_series = list(valid)
        self.stack.setCurrentIndex(1)
        self.title_label.setText(f"Multi-Curve Comparison ({len(valid)} series)")
        self.channel_combo.hide()

        if self.legend is None:
            self.legend = self.plot.addLegend(offset=(10, 10))
        self.legend.clear()
        self.legend.show()

        # Determine shared axis labels from first series
        first = valid[0]
        self.plot.setLabel("bottom", first.x_label, units=first.x_unit or None)
        self.plot.setLabel("left", first.y_label, units=first.y_unit or None)

        for idx, s in enumerate(valid):
            color = self._OVERLAY_COLORS[idx % len(self._OVERLAY_COLORS)]
            pen = pg.mkPen(color, width=2)
            brush = pg.mkBrush(color)

            name = s.title[:24] if len(s.title) > 24 else s.title
            curve = self.plot.plot(
                s.x_values,
                s.y_values,
                pen=pen,
                symbol="o",
                symbolSize=4,
                symbolBrush=brush,
                symbolPen=None,
                name=name,
            )
            self._plot_items.append(curve)

        self.crosshair_x.show()
        self.crosshair_y.show()
        self.reset_view()

    def _on_channel_combo_changed(self, _idx: int) -> None:
        ch = self.channel_combo.currentText()
        if ch:
            self.y_channel_changed.emit(ch)

    def _on_mouse_moved(self, evt: object) -> None:
        pos = evt[0] if isinstance(evt, (tuple, list)) else evt
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mouse_point = self.plot.getPlotItem().vb.mapSceneToView(pos)
        x_val = mouse_point.x()
        y_val = mouse_point.y()

        self.crosshair_x.setPos(x_val)
        self.crosshair_y.setPos(y_val)

        x_str = f"{x_val:.4g}"
        y_str = f"{y_val:.4g}"
        if self._current_series and self._current_series.x_unit:
            x_str += f" {self._current_series.x_unit}"
        if self._current_series and self._current_series.y_unit:
            y_str += f" {self._current_series.y_unit}"

        self.readout_label.setText(f"X: {x_str}   Y: {y_str}")
