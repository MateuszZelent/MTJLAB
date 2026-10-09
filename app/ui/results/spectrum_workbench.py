"""Recorded-spectrum inspector reusing Anritsu's scientific view tools."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QWidget
from qfluentwidgets import FlowLayout
from pyqtgraph.exporters import ImageExporter, SVGExporter
from pathlib import Path

from app.devices.anritsu_ms2830a.ui.spectrum_workbench import SpectrumWorkbench
from app.ui.dialogs import StationDialog, StationFileDialog
from .analysis_export import ensure_derived_destination, write_analysis_manifest


class ResultSpectrumWorkbench(SpectrumWorkbench):
    def __init__(self, parent=None):
        self._hold_frames = {}
        self._frame_identity = None
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
        grid.deleteLater()
        root.insertWidget(0, toolbar)
        self._toolbar_host, self._toolbar_flow = toolbar, flow
        for axis, controls in self.axis_controls.items():
            controls[0].setToolTip("Keep this range while navigating recorded checkpoints; Reset fits only unlocked axes.")
        self._trace_units = {}

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

    def set_trace(self, name, x, y, **kwargs):
        self.set_trace_unit(name, self.amplitude_unit)
        super().set_trace(name, x, y, **kwargs)

    def prepare_traces(self, names):
        """Keep compatible holds and measurement selection across checkpoints."""
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
                                 primary=name == self._hold_source)
        mirror.export_metadata = dict(self.export_metadata)
        mirror.register_frame(self._frame_identity)
        mirror.auto_range()

    def clear(self):
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
        return {**self.export_metadata, "value_unit": self._y_unit,
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
            if "PNG" in selected or suffix == ".png":
                ImageExporter(self.plot.plotItem).export(path)
                write_analysis_manifest(path, self.analysis_manifest())
            elif "SVG" in selected or suffix == ".svg":
                SVGExporter(self.plot.plotItem).export(path)
                write_analysis_manifest(path, self.analysis_manifest())
            else:
                self._export_csv(Path(path))
        except Exception as exc:
            self.status_changed.emit(f"Spectrum export failed: {exc}")
            return
        self.status_changed.emit(f"Spectrum and analysis metadata exported to {path}")
