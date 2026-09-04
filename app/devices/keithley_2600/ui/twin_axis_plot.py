"""Twin-axis (Voltage and Current) time plot for Keithley SMU channel history."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QSize
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, isDarkTheme

from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_VOLTAGE, format_quantity_auto
from app.ui.design_system import tokens_for
from app.ui.design_system.plot_theme import plot_theme


class KeithleyTwinAxisPlotWidget(QWidget):
    """A dual Y-axis plot widget displaying Voltage (left) and Current (right) over time."""

    def __init__(
        self,
        channel: str,
        parent: QWidget | None = None,
        *,
        preferred_height: int = 140,
    ) -> None:
        super().__init__(parent)
        self.channel = channel
        self._preferred_height = preferred_height
        self._theme_name = "dark" if isDarkTheme() else "light"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(100)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Header with title, metric readout and compliance badge
        header = QHBoxLayout()
        header.setContentsMargins(4, 2, 4, 2)
        self.title_label = CaptionLabel(f"CH {channel} — Voltage & Current", self)
        self.title_label.setObjectName("muted")
        header.addWidget(self.title_label)
        header.addStretch(1)

        self.readout_label = CaptionLabel("V: —   I: —", self)
        self.readout_label.setObjectName("muted")
        header.addWidget(self.readout_label)

        self.compliance_badge = CaptionLabel("", self)
        self.compliance_badge.setStyleSheet("color: #ef4444; font-weight: bold;")
        self.compliance_badge.hide()
        header.addWidget(self.compliance_badge)

        layout.addLayout(header)

        # Main plot widget
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=True, y=True)

        self.p1 = self.plot.getPlotItem()
        self.p1.setLabel("bottom", "Elapsed time", units="s")
        self.p1.setLabel("left", "Voltage", units="V")
        self.p1.showAxis("right")
        self.p1.setLabel("right", "Current", units="A")

        # Secondary ViewBox for the right axis (Current)
        self.p2 = pg.ViewBox()
        self.p1.scene().addItem(self.p2)
        self.p1.getAxis("right").linkToView(self.p2)
        self.p2.setXLink(self.p1)
        self.p2.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

        # Curves
        self._voltage_curve = self.p1.plot(name="Voltage", pen=pg.mkPen("#f59e0b", width=1.6))
        self._current_curve = pg.PlotDataItem(name="Current", pen=pg.mkPen("#06b6d4", width=1.6))
        self.p2.addItem(self._current_curve)

        # Compliance markers
        self._voltage_compliance_scatter = pg.ScatterPlotItem(
            size=8,
            symbol="d",
            pxMode=True,
        )
        self.p1.addItem(self._voltage_compliance_scatter)

        self._current_compliance_scatter = pg.ScatterPlotItem(
            size=8,
            symbol="d",
            pxMode=True,
        )
        self.p2.addItem(self._current_compliance_scatter)

        # Keep p2 geometry synchronized with p1
        self.p1.vb.sigResized.connect(self._sync_view_geometry)

        layout.addWidget(self.plot, 1)
        self.apply_theme(self._theme_name)

    def _sync_view_geometry(self) -> None:
        """Keep the secondary ViewBox strictly aligned with the primary ViewBox."""
        self.p2.setGeometry(self.p1.vb.sceneBoundingRect())
        self.p2.linkedViewChanged(self.p1.vb, self.p2.XAxis)

    def set_x_link(self, target: object) -> None:
        """Link X axis to another plot widget or PlotItem."""
        if hasattr(target, "plot") and isinstance(target.plot, pg.PlotWidget):
            self.p1.setXLink(target.plot.getPlotItem())
        elif hasattr(target, "getPlotItem"):
            self.p1.setXLink(target.getPlotItem())
        elif isinstance(target, (pg.PlotItem, pg.ViewBox)):
            self.p1.setXLink(target)

    def apply_theme(self, theme: str) -> None:
        """Style background, axes, grid, and curves according to theme."""
        self._theme_name = theme
        palette = plot_theme(tokens_for(theme))
        tokens = tokens_for(theme)

        self.plot.setBackground(palette.background)

        # Bottom axis
        bottom_axis = self.p1.getAxis("bottom")
        bottom_axis.setPen(pg.mkPen(palette.axes))
        bottom_axis.setTextPen(pg.mkPen(palette.axes))

        # Left axis (Voltage) - Amber color
        voltage_color = "#b45309" if theme == "light" else "#fbbf24"
        left_axis = self.p1.getAxis("left")
        left_axis.setPen(pg.mkPen(voltage_color))
        left_axis.setTextPen(pg.mkPen(voltage_color))
        self._voltage_curve.setPen(pg.mkPen(voltage_color, width=1.6))

        # Right axis (Current) - Cyan color
        current_color = "#0284c7" if theme == "light" else "#38bdf8"
        right_axis = self.p1.getAxis("right")
        right_axis.setPen(pg.mkPen(current_color))
        right_axis.setTextPen(pg.mkPen(current_color))
        self._current_curve.setPen(pg.mkPen(current_color, width=1.6))

        # Compliance markers
        danger_color = tokens.danger
        self._voltage_compliance_scatter.setPen(pg.mkPen(danger_color, width=1.5))
        self._voltage_compliance_scatter.setBrush(pg.mkBrush(danger_color))
        self._current_compliance_scatter.setPen(pg.mkPen(danger_color, width=1.5))
        self._current_compliance_scatter.setBrush(pg.mkBrush(danger_color))

    def set_data(
        self,
        elapsed_s: Sequence[float],
        voltages: Sequence[float],
        currents: Sequence[float],
        compliance_mask: Sequence[bool] | None = None,
    ) -> None:
        """Update time series for Voltage and Current and render compliance markers."""
        x_vals = np.asarray(elapsed_s, dtype=float)
        v_vals = np.asarray(voltages, dtype=float)
        i_vals = np.asarray(currents, dtype=float)

        if x_vals.size == 0 or v_vals.size == 0 or i_vals.size == 0:
            self.clear()
            return

        finite = np.isfinite(x_vals) & np.isfinite(v_vals) & np.isfinite(i_vals)
        x_clean = x_vals[finite]
        v_clean = v_vals[finite]
        i_clean = i_vals[finite]

        self._voltage_curve.setData(x_clean, v_clean)
        self._current_curve.setData(x_clean, i_clean)

        # Scale the secondary ViewBox Y range with padding
        if i_clean.size > 0:
            i_min = float(np.min(i_clean))
            i_max = float(np.max(i_clean))
            if math.isclose(i_min, i_max, abs_tol=1e-15):
                pad = max(abs(i_min) * 0.1, 1e-6)
                self.p2.setYRange(i_min - pad, i_max + pad, padding=0)
            else:
                pad = (i_max - i_min) * 0.08
                self.p2.setYRange(i_min - pad, i_max + pad, padding=0)

        # Update compliance markers
        has_compliance = False
        if compliance_mask is not None and len(compliance_mask) == len(x_vals):
            mask_arr = np.asarray(compliance_mask, dtype=bool)[finite]
            if np.any(mask_arr):
                has_compliance = True
                self._voltage_compliance_scatter.setData(
                    x=x_clean[mask_arr],
                    y=v_clean[mask_arr],
                )
                self._current_compliance_scatter.setData(
                    x=x_clean[mask_arr],
                    y=i_clean[mask_arr],
                )
            else:
                self._voltage_compliance_scatter.clear()
                self._current_compliance_scatter.clear()
        else:
            self._voltage_compliance_scatter.clear()
            self._current_compliance_scatter.clear()

        # Update readout and compliance badge
        if x_clean.size > 0:
            latest_v = v_clean[-1]
            latest_i = i_clean[-1]
            v_text = format_quantity_auto(latest_v, DIMENSION_VOLTAGE, precision=4)
            i_text = format_quantity_auto(latest_i, DIMENSION_CURRENT, precision=4)
            self.readout_label.setText(f"V: {v_text}   I: {i_text}")
        else:
            self.readout_label.setText("V: —   I: —")

        if has_compliance:
            self.compliance_badge.setText("⚠ COMPLIANCE")
            self.compliance_badge.show()
        else:
            self.compliance_badge.hide()

        self._sync_view_geometry()

    def set_x_range(self, min_x: float, max_x: float) -> None:
        """Explicitly set the visible X range."""
        self.p1.setXRange(min_x, max_x, padding=0)
        self._sync_view_geometry()

    def clear(self) -> None:
        """Clear all curves, markers, and readouts."""
        self._voltage_curve.clear()
        self._current_curve.clear()
        self._voltage_compliance_scatter.clear()
        self._current_compliance_scatter.clear()
        self.readout_label.setText("V: —   I: —")
        self.compliance_badge.hide()

    def set_preferred_height(self, height: int) -> None:
        self._preferred_height = height
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(360, self._preferred_height)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(200, 100)

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
