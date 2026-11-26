"""Recorded-spectrum inspector reusing Anritsu's scientific view tools."""

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSplitter, QWidget
from qfluentwidgets import CheckBox, FlowLayout, PushButton

from app.devices.anritsu_ms2830a.ui.spectrum_workbench import SpectrumWorkbench
from app.ui.design_system import tokens_for
from app.ui.dialogs import StationDialog, StationFileDialog

from .analysis_export import ensure_derived_destination, write_analysis_manifest


class ResultSpectrumWorkbench(SpectrumWorkbench):
    absolute_changed = Signal()

    def __init__(self, parent=None):
        self._hold_frames = {}
        self._frame_identity = None
        self._signed_sources = {}
        super().__init__(parent)
        # Live places this panel beside its workbench. Results owns it until
        # an inspector/floating window explicitly hosts it; an unlaid-out
        # sibling otherwise appears over the page when the shell is shown.
        self.tools.setParent(self)
        self.tools.hide()
        self.export_metadata = {}
        self._tools_window = None
        self._floating_window = None
        self.freeze.hide()  # The floating mirror exposes the preview freeze.
        root = self.layout()
        grid = root.takeAt(0).layout()
        toolbar = QWidget(self)
        flow = FlowLayout(toolbar, needAni=False)
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setHorizontalSpacing(4)
        flow.setVerticalSpacing(4)
        for button in self.toolbar_buttons:
            grid.removeWidget(button)
            flow.addWidget(button)
        self.show_absolute = CheckBox("Abs", toolbar)
        self.show_absolute.setAccessibleName("Show absolute spectrum difference")
        self.show_absolute.setToolTip("Show the magnitude of signed dB or W differences.")
        self.show_absolute.setEnabled(False)
        self.show_absolute.toggled.connect(self._absolute_view_changed)
        flow.addWidget(self.show_absolute)
        self.batch_button = PushButton("Postprocess all measurements…", toolbar)
        self.batch_button.setEnabled(False)
        self.batch_button.setToolTip("Apply this spectrum view and processing to every recorded checkpoint.")
        flow.addWidget(self.batch_button)
        grid.deleteLater()
        root.insertWidget(0, toolbar)
        self._toolbar_host, self._toolbar_flow = toolbar, flow
        for axis, controls in self.axis_controls.items():
            controls[0].setToolTip("Keep this range while navigating recorded checkpoints; Reset fits only unlocked axes.")
        self._trace_units = {}
        self._fit_curve = pg.PlotDataItem()
        self._fit_curve.setVisible(False)
        self.plot.addItem(self._fit_curve)
        self._ab_enabled = False
        self._ab_raw: tuple[np.ndarray, np.ndarray] | None = None
        self._ab_proc: tuple[np.ndarray, np.ndarray] | None = None
        self._ab_raw_curve = pg.PlotDataItem()
        self._ab_proc_curve = pg.PlotDataItem()
        self._ab_raw_curve.setVisible(False)
        self._ab_proc_curve.setVisible(False)
        self._ab_line = pg.InfiniteLine(angle=90, movable=True)
        self._ab_line.setVisible(False)
        self._ab_line.sigPositionChanged.connect(self._update_ab_split)
        self.plot.addItem(self._ab_raw_curve)
        self.plot.addItem(self._ab_proc_curve)
        self.plot.addItem(self._ab_line, ignoreBounds=True)
        self._style_analysis_overlays()

    def _style_analysis_overlays(self) -> None:
        theme = getattr(self, "_theme_name", "dark")
        tokens = tokens_for(theme)
        teal = "#94e2d5" if theme == "dark" else "#0e858c"
        self._fit_curve.setPen(
            pg.mkPen(teal, width=1.8, style=Qt.PenStyle.DashLine)
        )
        self._ab_raw_curve.setPen(
            pg.mkPen(tokens.text_muted, width=1.5, style=Qt.PenStyle.SolidLine)
        )
        self._ab_proc_curve.setPen(pg.mkPen(tokens.accent, width=2.0))
        self._ab_line.setPen(pg.mkPen(tokens.caution, width=2.0))

    def apply_theme(self, theme: str) -> None:
        super().apply_theme(theme)
        if hasattr(self, "_fit_curve"):
            self._style_analysis_overlays()

    def set_fit_overlay(self, frequencies_hz, values) -> None:
        """Overlay a dashed resonance fit curve without altering recorded trace dicts."""
        self._fit_curve.setData(
            np.asarray(frequencies_hz, dtype=float),
            np.asarray(values, dtype=float),
        )
        self._fit_curve.setVisible(True)

    def clear_fit_overlay(self) -> None:
        self._fit_curve.setData([], [])
        self._fit_curve.setVisible(False)

    def set_ab_wipe(
        self,
        enabled: bool,
        *,
        raw_x=None,
        raw_y=None,
        proc_x=None,
        proc_y=None,
    ) -> None:
        """Split the canvas at a draggable vertical divider: Raw HDF5 (left) vs Processed (right)."""
        self._ab_enabled = bool(enabled)
        if (
            not self._ab_enabled
            or raw_x is None
            or raw_y is None
            or proc_x is None
            or proc_y is None
        ):
            self.clear_ab_wipe()
            return
        rx = np.asarray(raw_x, dtype=float)
        ry = np.asarray(raw_y, dtype=float)
        px = np.asarray(proc_x, dtype=float)
        py = np.asarray(proc_y, dtype=float)
        if rx.size < 2 or px.size < 2:
            self.clear_ab_wipe()
            return
        self._ab_raw = (rx, ry)
        self._ab_proc = (px, py)
        x_lo = float(min(rx[0], px[0]))
        x_hi = float(max(rx[-1], px[-1]))
        current_pos = float(self._ab_line.value())
        if not (x_lo < current_pos < x_hi):
            self._ab_line.setValue(0.5 * (x_lo + x_hi))
        self._ab_line.setBounds((x_lo, x_hi))
        self._ab_line.setVisible(True)
        self._ab_raw_curve.setVisible(True)
        self._ab_proc_curve.setVisible(True)
        for curve in self._curves.values():
            curve.setVisible(False)
        self._update_ab_split()

    def _update_ab_split(self) -> None:
        if not self._ab_enabled or self._ab_raw is None or self._ab_proc is None:
            return
        split_x = float(self._ab_line.value())
        rx, ry = self._ab_raw
        px, py = self._ab_proc
        left = rx <= split_x
        right = px >= split_x
        self._ab_raw_curve.setData(rx[left], ry[left])
        self._ab_proc_curve.setData(px[right], py[right])

    def clear_ab_wipe(self) -> None:
        self._ab_enabled = False
        self._ab_raw = None
        self._ab_proc = None
        if hasattr(self, "_ab_line"):
            self._ab_line.setVisible(False)
            self._ab_raw_curve.setData([], [])
            self._ab_raw_curve.setVisible(False)
            self._ab_proc_curve.setData([], [])
            self._ab_proc_curve.setVisible(False)
        for name, curve in getattr(self, "_curves", {}).items():
            if name in getattr(self, "_traces", {}):
                curve.setVisible(True)

    def register_frame(self, checkpoint):
        self._frame_identity = checkpoint
        for name, values in (("Max hold", self._max_hold), ("Min hold", self._min_hold)):
            if values is not None:
                frames = self._hold_frames.setdefault(name, [])
                if checkpoint not in frames:
                    frames.append(checkpoint)
            else:
                self._hold_frames.pop(name, None)

    def clear_holds(self):
        super().clear_holds()
        self._hold_frames.clear()

    def toggle_max_hold(self):
        super().toggle_max_hold()
        self.register_frame(self._frame_identity)

    def toggle_min_hold(self):
        super().toggle_min_hold()
        self.register_frame(self._frame_identity)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_toolbar_host"):
            self._toolbar_host.setMinimumHeight(self._toolbar_flow.heightForWidth(max(100, self.width())))

    def set_labels(self, **kwargs):
        if kwargs.get("y_unit", "dBm") != self._y_unit:
            self.clear_holds()
        super().set_labels(**kwargs)
        self._trace_units.update({name: self.amplitude_unit for name in self._traces})

    def set_trace(self, name, x, y, *, signed_difference=False, absolute_capable=True, **kwargs):
        self.set_trace_unit(name, self.amplitude_unit)
        if absolute_capable and (self.amplitude_unit == "dB" or (self.amplitude_unit == "W" and signed_difference)):
            self._signed_sources[name] = (x, y, kwargs)
        else:
            self._signed_sources.pop(name, None)
        self.show_absolute.setEnabled(bool(self._signed_sources))
        self._draw_trace(name, x, y, kwargs)

    def _draw_trace(self, name, x, y, options):
        if name in self._signed_sources and self.show_absolute.isChecked():
            y = np.abs(np.asarray(y, dtype=float))
            options = {**options, "legend_label": f"Abs · {options.get('legend_label') or name}"}
        super().set_trace(name, x, y, **options)

    def clear_trace(self, name):
        self._signed_sources.pop(name, None)
        if hasattr(self, "show_absolute"):
            self.show_absolute.setEnabled(bool(self._signed_sources))
        super().clear_trace(name)

    def _absolute_view_changed(self):
        if not self._signed_sources:
            return
        x_range = self.plot.viewRange()[0]
        self.clear_holds()
        for name, (x, y, options) in self._signed_sources.items():
            self._draw_trace(name, x, y, options)
        self.auto_range()
        self.plot.setXRange(*x_range, padding=0)
        self.sync_floating()
        self.absolute_changed.emit()

    def _absolute_trace_names(self):
        return list(self._signed_sources) if self.show_absolute.isChecked() else []

    def prepare_traces(self, names):
        """Keep compatible holds and measurement selection across checkpoints."""
        self.clear_fit_overlay()
        self.clear_ab_wipe()
        self.set_estimated_bands(())
        for name in tuple(self._traces):
            if name not in names and name not in {"Max hold", "Min hold"}:
                self.clear_trace(name)

    def show_tools(self):
        if self._tools_window is None:
            dialog = StationDialog(self.window(), resizable=True)
            dialog.setWindowTitle("Recorded spectrum — axes, markers and band")
            dialog.resize(440, 730)
            dialog.setMinimumSize(340, 450)
            dialog.modal_content_layout().addWidget(self.tools)
            self.tools.show()
            self._tools_window = dialog
        self._tools_window.show()
        self._tools_window.raise_()
        self._tools_window.activateWindow()

    def show_floating(self):
        if self._floating_window is None:
            dialog = StationDialog(self.window(), resizable=True)
            dialog.setWindowTitle("Results — floating recorded spectrum")
            dialog.resize(1200, 760)
            dialog.setMinimumSize(640, 480)
            split = QSplitter(Qt.Orientation.Horizontal, dialog)
            mirror = ResultSpectrumWorkbench(split)
            mirror.show_absolute.hide()
            mirror.freeze.show()
            mirror.freeze.setToolTip("Keep this recorded spectrum while browsing other checkpoints in Results.")
            split.addWidget(mirror)
            split.addWidget(mirror.tools)
            mirror.tools.show()
            split.setSizes([800, 340])
            dialog.modal_content_layout().addWidget(split)
            dialog.mirror = mirror
            self._floating_window = dialog
            mirror.display_resumed.connect(self.sync_floating)
        self.sync_floating()
        self._floating_window.show()
        self._floating_window.raise_()
        self._floating_window.activateWindow()

    def sync_floating(self):
        if self._floating_window is None:
            return
        mirror = self._floating_window.mirror
        if mirror.frozen:
            return
        names = [name for name in self._traces if name not in {"Max hold", "Min hold"}]
        mirror.prepare_traces(names)
        mirror.set_labels(x=self._x_label, x_unit=self._x_unit, y="Amplitude", y_unit=self._y_unit)
        mirror.set_title(self._plot_title or "Recorded spectrum")
        for name in names:
            x, y = self._traces[name]
            curve = self._curves.get(name)
            if curve is not None and curve.isVisible():
                mirror.set_trace(name, x, y, color=curve.opts["pen"].color().name(),
                                 primary=name == self._hold_source,
                                 legend_label=self._legend_labels.get(name))
        mirror.set_estimated_bands(self._estimated_bands_hz)
        mirror.export_metadata = {**self.export_metadata, "absolute_traces": self._absolute_trace_names()}
        mirror.register_frame(self._frame_identity)
        mirror.auto_range()

    def clear(self):
        self.clear_fit_overlay()
        self.clear_ab_wipe()
        super().clear()
        self.export_metadata = {}

    def close_windows(self):
        for window in (self._tools_window, self._floating_window):
            if window is not None:
                window.close()

    def _export_csv(self, path):
        ensure_derived_destination(path, self.export_metadata.get("source_file"))
        super()._export_csv(path)
        write_analysis_manifest(path, self.analysis_manifest())

    def analysis_manifest(self):
        return {"absolute_traces": self._absolute_trace_names(),
                **self.export_metadata, "value_unit": self._y_unit,
                "log_mode": [self.plot.plotItem.ctrl.logXCheck.isChecked(), self.plot.plotItem.ctrl.logYCheck.isChecked()],
                "inverted_axes": [bool(self.plot.getViewBox().state["xInverted"]),
                                  bool(self.plot.getViewBox().state["yInverted"])],
                "visible_traces": [name for name, curve in self._curves.items()
                                   if curve.isVisible() and name in self._traces],
                "measurement_trace": self.trace_selector.currentText(),
                "fixed_ranges": self.fixed_ranges, "view_ranges": self.plot.viewRange(),
                "frequency_markers_hz": {name: float(line.value()) for name, line in self.frequency_markers.items()},
                "band_hz": list(self.band.getRegion()) if self.band_enabled.isChecked() else None,
                "hold_checkpoints": self._hold_frames,
                "csv_coverage": "Full recorded grids of visible traces; zoom does not crop the exported arrays."}

    def export(self):
        path, selected = StationFileDialog.getSaveFileName(self, "Export recorded spectrum", "spectrum.csv",
                                                         "CSV (*.csv);;PNG (*.png);;SVG (*.svg)")
        if not path:
            return
        try:
            ensure_derived_destination(path, self.export_metadata.get("source_file"))
            suffix = Path(path).suffix.lower()
            if suffix in {".png", ".svg"} or "PNG" in selected or "SVG" in selected:
                presentation = self._export_figure(path)
                write_analysis_manifest(path, {**self.analysis_manifest(), "figure_presentation": presentation})
            else:
                self._export_csv(Path(path))
        except Exception as exc:
            self.status_changed.emit(f"Spectrum export failed: {exc}")
            return
        self.status_changed.emit(f"Spectrum and analysis metadata exported to {path}")
