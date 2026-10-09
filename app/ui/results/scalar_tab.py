"""Fluent scalar-results page: measured I/V/P versus recorded sweep values."""
import csv
from pathlib import Path

import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter, SVGExporter
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, ComboBox, FlowLayout, PushButton

from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationFileDialog
from app.ui.results.scalar_data import read_scalar_columns, scalar_curves
from app.ui.results.workers import ResultReadTask
from app.ui.widgets.plot_ownership import create_plot_widget


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
        controls = CardWidget(self)
        flow = FlowLayout(controls, needAni=False, isTight=True)
        self.controls, self._flow = controls, flow
        flow.setContentsMargins(12, 8, 12, 8)
        flow.setHorizontalSpacing(8)
        flow.setVerticalSpacing(8)
        self.x_axis, self.y_axis, self.group_by = ComboBox(), ComboBox(), ComboBox()
        for text, combo in (("X — recorded parameter", self.x_axis), ("Y — measurement", self.y_axis), ("Separate curves by", self.group_by)):
            box = QWidget(controls)
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(CaptionLabel(text, box))
            combo.setMinimumWidth(210)
            combo.setAccessibleName(text)
            column.addWidget(combo)
            flow.addWidget(box)
        self.reset_button = PushButton("Fit axes", controls)
        self.export_button = PushButton("Export plot / CSV…", controls)
        flow.addWidget(self.reset_button)
        flow.addWidget(self.export_button)
        layout.addWidget(controls)
        self.status = CaptionLabel("Select a result containing scalar measurements.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.plot = create_plot_widget(self)
        self.plot.setMinimumHeight(220)
        self.plot.addLegend()
        self.plot.showGrid(x=True, y=True, alpha=.2)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.plot.setClipToView(True)
        layout.addWidget(self.plot, 1)
        self._theme = "light"
        self.apply_theme("light")
        for combo in (self.x_axis, self.y_axis, self.group_by):
            combo.currentIndexChanged.connect(self._redraw)
        self.reset_button.clicked.connect(self.plot.autoRange)
        self.export_button.clicked.connect(self.export)
        self.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.controls.setMinimumHeight(self._flow.heightForWidth(self.controls.width()))

    def showEvent(self, event):
        super().showEvent(event)
        self.controls.setMinimumHeight(self._flow.heightForWidth(self.controls.width()))

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
        self.export_button.setEnabled(False)
        self.status.setText("No scalar measurements selected.")

    def load(self, path, run, points=()):
        self.clear()
        self.status.setText("Loading recorded scalar measurements…")
        task = ResultReadTask(self._request_id, read_scalar_columns, Path(path), run, points, cooperative_cancel=True)
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
        axes = [column for column in columns if column.role == "setpoint" and column.id != "checkpoint"
                and len(set(column.values)) > 1]
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
        self.controls.setMinimumHeight(self._flow.heightForWidth(self.controls.width()))
        self._redraw()

    def _selected(self, combo):
        return next((column for column in self._columns if column.id == combo.currentData()), None)

    def _redraw(self, *_):
        self.plot.clear()
        self._curves = ()
        x, y, group = (self._selected(combo) for combo in (self.x_axis, self.y_axis, self.group_by))
        if x is None or y is None:
            self.status.setText("This result contains no scalar measurement series.")
            self.export_button.setEnabled(False)
            return
        if group is not None and group.id == x.id:
            self.status.setText("Choose a grouping parameter different from the X axis.")
            self.export_button.setEnabled(False)
            return
        self.plot.setLabel("bottom", x.label, units=x.unit or None)
        self.plot.setLabel("left", y.label, units=y.unit or None)
        curves = scalar_curves(x, y, group)
        tokens = tokens_for(self._theme)
        colors = (tokens.accent, tokens.success, tokens.caution, tokens.danger, tokens.focus, tokens.neutral)
        for index, (value, pass_index, indices) in enumerate(curves):
            name = f"{group.label}: {value:g} {group.unit}" if group else y.label
            if pass_index > 1:
                name += f" · pass {pass_index}"
            self.plot.plot(x.values[indices], y.values[indices], pen=pg.mkPen(colors[index % len(colors)], width=1.5),
                           symbol="o", symbolSize=5, symbolBrush=colors[index % len(colors)], name=name)
        self._curves = curves
        self.plot.autoRange()
        count = sum(len(indices) for _, _, indices in curves)
        self.status.setText(f"{count} recorded point(s) · {len(curves)} curve(s). Acquisition order preserved; repeated passes are separate. Values remain in stored units; axis prefixes are automatic.")
        self.export_button.setEnabled(bool(curves))

    def apply_theme(self, theme):
        self._theme = theme
        palette = plot_theme(tokens_for(theme))
        self.plot.setBackground(palette.background)
        for name in ("left", "bottom"):
            self.plot.getAxis(name).setPen(pg.mkPen(palette.axes))
            self.plot.getAxis(name).setTextPen(pg.mkPen(palette.axes))
        self.plot.getPlotItem().legend.setLabelTextColor(palette.axes)
        if self._columns:
            self._redraw()

    def export_csv(self, path):
        x, y, group = (self._selected(combo) for combo in (self.x_axis, self.y_axis, self.group_by))
        with Path(path).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("checkpoint", f"{x.label} [{x.unit}]", f"{y.label} [{y.unit}]",
                             f"{group.label} [{group.unit}]" if group else "group", "pass"))
            for value, pass_index, indices in self._curves:
                for index in indices:
                    writer.writerow((self._columns[0].values[index], x.values[index], y.values[index], value, pass_index))

    def export(self):
        path, _ = StationFileDialog.getSaveFileName(self, "Export scalar plot", "scalar.csv", "CSV (*.csv);;PNG (*.png);;SVG (*.svg)")
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
