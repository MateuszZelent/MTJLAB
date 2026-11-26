"""Fluent scalar-results page: measured I/V/P, MTJ figures of merit, dY/dX, and Log Y."""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter, SVGExporter
from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    ComboBox,
    FlowLayout,
    PushButton,
    TogglePushButton,
)

from app.inventory.analysis import calculate_mtj_metrics
from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationFileDialog
from app.ui.results.scalar_data import read_scalar_columns, scalar_curves
from app.ui.results.workers import ResultReadTask
from app.ui.widgets.plot_ownership import create_plot_widget, own_signal_proxy
from app.ui.widgets.plot_style import apply_scientific_style


class ScalarResultsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns = ()
        self._curves = ()
        self._request_id = 0
        self._task = None
        self._read_pool = QThreadPool(self)
        self._read_pool.setMaxThreadCount(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        controls = CardWidget(self)
        flow = FlowLayout(controls, needAni=False, isTight=True)
        self.controls, self._flow = controls, flow
        flow.setContentsMargins(10, 6, 10, 6)
        flow.setHorizontalSpacing(8)
        flow.setVerticalSpacing(6)
        self.x_axis, self.y_axis, self.group_by = ComboBox(), ComboBox(), ComboBox()
        for text, combo in (
            ("X — recorded parameter", self.x_axis),
            ("Y — measurement", self.y_axis),
            ("Separate curves by", self.group_by),
        ):
            box = QWidget(controls)
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            lbl = CaptionLabel(text, box)
            lbl.setObjectName("muted")
            column.addWidget(lbl)
            combo.setMinimumWidth(195)
            combo.setAccessibleName(text)
            column.addWidget(combo)
            flow.addWidget(box)

        self.derivative_button = TogglePushButton("dY/dX", controls)
        self.derivative_button.setToolTip(
            "Plot numerical derivative dY/dX (e.g. differential resistance dV/dI) per pass."
        )
        self.log_y_button = TogglePushButton("Log Y", controls)
        self.log_y_button.setToolTip("Display |Y| on a logarithmic vertical scale.")
        self.reset_button = PushButton("Fit axes", controls)
        self.reset_button.setToolTip("Reset zoom to fit all plotted curves.")
        self.export_button = PushButton("Export plot / CSV…", controls)
        self.export_button.setToolTip("Export scalar curves to CSV, PNG, or SVG.")
        flow.addWidget(self.derivative_button)
        flow.addWidget(self.log_y_button)
        flow.addWidget(self.reset_button)
        flow.addWidget(self.export_button)
        layout.addWidget(controls)

        # Clean single-line MTJ Figures-of-Merit & Hover Readout Bar (OUTSIDE plot canvas!)
        self.fom_bar = QFrame(self)
        self.fom_bar.setObjectName("resultsFomBar")
        fom_layout = QHBoxLayout(self.fom_bar)
        fom_layout.setContentsMargins(10, 4, 10, 4)
        fom_layout.setSpacing(12)
        self.fom_label = CaptionLabel("", self.fom_bar)
        self.fom_label.setObjectName("resultsTelemetryPrimary")
        self.readout_label = CaptionLabel("Hover curve for readout", self.fom_bar)
        self.readout_label.setObjectName("resultsTelemetryMuted")
        fom_layout.addWidget(self.fom_label, 1)
        fom_layout.addWidget(self.readout_label)
        layout.addWidget(self.fom_bar)

        self.plot = create_plot_widget(self)
        self.plot.setMinimumHeight(220)
        self.plot.addLegend()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.plot.setClipToView(True)
        self.crosshair_x = pg.InfiniteLine(angle=90, movable=False)
        self.crosshair_y = pg.InfiniteLine(angle=0, movable=False)
        self.crosshair_x.hide()
        self.crosshair_y.hide()
        self.plot.addItem(self.crosshair_x, ignoreBounds=True)
        self.plot.addItem(self.crosshair_y, ignoreBounds=True)
        self.plot.viewport().installEventFilter(self)
        self._mouse_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved
        )
        own_signal_proxy(self._mouse_proxy, self)
        layout.addWidget(self.plot, 1)

        self.status = CaptionLabel("Select a result containing scalar measurements.", self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self._theme = "light"
        self.apply_theme("light")
        for combo in (self.x_axis, self.y_axis, self.group_by):
            combo.currentIndexChanged.connect(self._redraw)
        self.derivative_button.toggled.connect(self._redraw)
        self.log_y_button.toggled.connect(self._redraw)
        self.reset_button.clicked.connect(self.plot.autoRange)
        self.export_button.clicked.connect(self.export)
        self.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.controls.setMinimumHeight(self._flow.heightForWidth(max(120, self.controls.width())))

    def showEvent(self, event):
        super().showEvent(event)
        self.controls.setMinimumHeight(self._flow.heightForWidth(max(120, self.controls.width())))

    def eventFilter(self, obj, event):
        if obj is self.plot.viewport() and event.type() == QEvent.Type.Leave:
            self.crosshair_x.hide()
            self.crosshair_y.hide()
            self.readout_label.setText("Hover curve for readout")
        return super().eventFilter(obj, event)

    def _mouse_moved(self, event):
        position = event[0]
        if not self._curves or not self.plot.getViewBox().sceneBoundingRect().contains(position):
            self.crosshair_x.hide()
            self.crosshair_y.hide()
            return
        pt = self.plot.plotItem.vb.mapSceneToView(position)
        x_val, y_val = float(pt.x()), float(pt.y())
        self.crosshair_x.setPos(x_val)
        self.crosshair_y.setPos(y_val)
        self.crosshair_x.show()
        self.crosshair_y.show()
        x_col, y_col = self._selected(self.x_axis), self._selected(self.y_axis)
        xu = f" {x_col.unit}" if x_col and x_col.unit else ""
        yu = f" {y_col.unit}" if y_col and y_col.unit and not self.derivative_button.isChecked() else ""
        self.readout_label.setText(f"X: {x_val:.5g}{xu}   Y: {y_val:.5g}{yu}")

    def cancel_read(self):
        self._request_id += 1
        if self._task:
            self._task.cancel()
            self._task = None

    def clear(self):
        self.cancel_read()
        self._columns = ()
        self._curves = ()
        for combo in (self.x_axis, self.y_axis, self.group_by):
            combo.blockSignals(True)
            combo.clear()
            combo.setEnabled(False)
            combo.blockSignals(False)
        self.plot.clear()
        self.plot.addItem(self.crosshair_x, ignoreBounds=True)
        self.plot.addItem(self.crosshair_y, ignoreBounds=True)
        self.crosshair_x.hide()
        self.crosshair_y.hide()
        self.fom_label.setText("")
        self.readout_label.setText("Hover curve for readout")
        self.export_button.setEnabled(False)
        self.status.setText("No scalar measurements selected.")

    def load(self, path, run, points=()):
        self.clear()
        self.status.setText("Loading recorded scalar measurements…")
        task = ResultReadTask(
            self._request_id, read_scalar_columns, Path(path), run, points, cooperative_cancel=True
        )
        self._task = task
        task.signals.loaded.connect(self._loaded)
        task.signals.failed.connect(self._failed)
        self._read_pool.start(task)

    def _failed(self, request_id, error):
        if request_id == self._request_id:
            self._task = None
            self.status.setText(f"Scalar measurements could not be read: {error}")

    def _loaded(self, request_id, columns):
        if request_id != self._request_id:
            return
        self._task = None
        self._columns = columns
        for combo in (self.x_axis, self.y_axis, self.group_by):
            combo.blockSignals(True)
        self.group_by.addItem("One curve", userData=None)
        for column in columns:
            label = f"{column.label} ({column.unit})" if column.unit else column.label
            self.x_axis.addItem(label, userData=column.id)
            if column.role == "measurement":
                self.y_axis.addItem(label, userData=column.id)
            if column.role == "setpoint" and column.id != "checkpoint":
                self.group_by.addItem(label, userData=column.id)
        axes = [
            column
            for column in columns
            if column.role == "setpoint"
            and column.id != "checkpoint"
            and len(set(column.values)) > 1
        ]
        if axes:
            self.x_axis.setCurrentIndex(self.x_axis.findData(axes[0].id))
        if len(axes) > 1:
            self.group_by.setCurrentIndex(self.group_by.findData(axes[1].id))
        preferred = next((column for column in columns if column.id.endswith("current_a")), None)
        if preferred is not None:
            self.y_axis.setCurrentIndex(self.y_axis.findData(preferred.id))
        for combo in (self.x_axis, self.y_axis, self.group_by):
            combo.setEnabled(combo.count() > 0)
            combo.blockSignals(False)
        self.controls.setMinimumHeight(self._flow.heightForWidth(max(120, self.controls.width())))
        self._redraw()

    def _selected(self, combo):
        return next((column for column in self._columns if column.id == combo.currentData()), None)

    def _redraw(self, *_):
        self.plot.clear()
        self.plot.addItem(self.crosshair_x, ignoreBounds=True)
        self.plot.addItem(self.crosshair_y, ignoreBounds=True)
        self.crosshair_x.hide()
        self.crosshair_y.hide()
        self._curves = ()
        self.fom_label.setText("")

        x, y, group = (self._selected(combo) for combo in (self.x_axis, self.y_axis, self.group_by))
        if x is None or y is None:
            self.status.setText("This result contains no scalar measurement series.")
            self.export_button.setEnabled(False)
            return
        if group is not None and group.id == x.id:
            self.status.setText("Choose a grouping parameter different from the X axis.")
            self.export_button.setEnabled(False)
            return

        derivative = self.derivative_button.isChecked()
        log_y = self.log_y_button.isChecked()
        self.plot.setLogMode(x=False, y=log_y)
        self.plot.setLabel("bottom", x.label, units=x.unit or None)
        if derivative:
            y_unit = f"{y.unit}/{x.unit}" if (y.unit and x.unit) else ""
            self.plot.setLabel("left", f"d({y.label})/d({x.label})", units=y_unit or None)
        else:
            self.plot.setLabel("left", y.label, units=y.unit or None)

        curves = scalar_curves(x, y, group)
        tokens = tokens_for(self._theme)
        teal = "#94e2d5" if self._theme == "dark" else "#0e858c"
        colors = (tokens.accent, teal, tokens.success, tokens.caution, tokens.danger, tokens.neutral)

        all_x: list[float] = []
        all_y: list[float] = []
        for index, (value, pass_index, indices) in enumerate(curves):
            xv = np.asarray(x.values[indices], dtype=float)
            yv = np.asarray(y.values[indices], dtype=float)
            all_x.extend(xv.tolist())
            all_y.extend(yv.tolist())
            if derivative:
                if xv.size >= 2 and np.ptp(xv) > 0:
                    dx = np.gradient(xv)
                    dy = np.gradient(yv)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        yv = np.where(np.abs(dx) > 1e-18, dy / dx, np.nan)
                else:
                    continue
            if log_y:
                yv = np.abs(yv)
                mask = np.isfinite(xv) & np.isfinite(yv) & (yv > 0)
                xv, yv = xv[mask], yv[mask]
            else:
                mask = np.isfinite(xv) & np.isfinite(yv)
                xv, yv = xv[mask], yv[mask]
            if xv.size == 0:
                continue
            name = f"{group.label}: {value:g} {group.unit}" if group else y.label
            if pass_index > 1:
                name += f" · pass {pass_index}"
            color = colors[index % len(colors)]
            self.plot.plot(
                xv,
                yv,
                pen=pg.mkPen(color, width=1.6),
                symbol="o",
                symbolSize=5,
                symbolBrush=color,
                name=name,
            )

        self._curves = curves
        self.plot.autoRange()

        # Surface MTJ figures of merit automatically from authoritative SI units
        metrics = calculate_mtj_metrics(
            all_x, all_y, x.label, y.label, x_unit=x.unit, y_unit=y.unit
        )
        summary_items = metrics.summary_items()
        if summary_items:
            self.fom_label.setText(
                "   ·   ".join(f"{lbl}: {val}" for lbl, val in summary_items)
            )
        else:
            self.fom_label.setText(f"{len(curves)} curve(s) · {len(all_x)} point(s)")

        count = sum(len(indices) for _, _, indices in curves)
        mode_note = " Derivative dY/dX shown." if derivative else ""
        self.status.setText(
            f"{count} recorded point(s) · {len(curves)} curve(s). Acquisition order preserved; "
            f"repeated passes are separate. Values remain in stored units; axis prefixes are automatic.{mode_note}"
        )
        self.export_button.setEnabled(bool(curves))

    def apply_theme(self, theme):
        self._theme = theme
        palette = plot_theme(tokens_for(theme))
        self.plot.setBackground(palette.background)
        apply_scientific_style(self.plot.plotItem, theme)
        self.crosshair_x.setPen(pg.mkPen(palette.grid, width=1))
        self.crosshair_y.setPen(pg.mkPen(palette.grid, width=1))
        if self.plot.getPlotItem().legend is not None:
            self.plot.getPlotItem().legend.setLabelTextColor(palette.axes)
        if self._columns:
            self._redraw()

    def export_csv(self, path):
        x, y, group = (self._selected(combo) for combo in (self.x_axis, self.y_axis, self.group_by))
        with Path(path).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "checkpoint",
                    f"{x.label} [{x.unit}]",
                    f"{y.label} [{y.unit}]",
                    f"{group.label} [{group.unit}]" if group else "group",
                    "pass",
                )
            )
            for value, pass_index, indices in self._curves:
                for index in indices:
                    writer.writerow(
                        (
                            self._columns[0].values[index],
                            x.values[index],
                            y.values[index],
                            value,
                            pass_index,
                        )
                    )

    def export(self):
        path, _ = StationFileDialog.getSaveFileName(
            self, "Export scalar plot", "scalar.csv", "CSV (*.csv);;PNG (*.png);;SVG (*.svg)"
        )
        if not path:
            return
        try:
            if Path(path).suffix.lower() == ".csv":
                self.export_csv(path)
            else:
                exporter = SVGExporter if Path(path).suffix.lower() == ".svg" else ImageExporter
                exporter(self.plot.getPlotItem()).export(path)
        except Exception as exc:
            self.status.setText(f"Export failed: {exc}")
