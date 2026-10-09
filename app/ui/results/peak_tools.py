"""Peak inspection and gated trajectories over immutable recorded checkpoints."""

import csv
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtWidgets import QHBoxLayout
from pyqtgraph.exporters import ImageExporter, SVGExporter
from qfluentwidgets import CaptionLabel, ComboBox, LineEdit, PrimaryPushButton, PushButton

from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.devices.anritsu_ms2830a.ui.peak_analysis import PeakTableDialog
from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto, parse_quantity
from app.spectrum.analysis import SpectrumAnalysisParameters, detect_spectrum_peaks
from app.storage import StoredPoint
from app.ui.dialogs import StationDialog, StationFileDialog
from app.ui.widgets import SpectrumPlotWidget
from .analysis_export import ensure_derived_destination, processing_manifest, write_analysis_manifest
from .read_session import ResultReadSession
from .spectrum_views import read_private_view, read_public_view
from .workers import ResultReadTask


def recorded_peaks(frequencies, values, unit, parameters, *, cancelled=None):
    """Never bridge a missing bin when measuring prominence or lobe width."""
    f, y = np.asarray(frequencies), np.asarray(values)
    finite = np.isfinite(f) & np.isfinite(y)
    edges = np.diff(np.r_[False, finite, False].astype(int))
    peaks = []
    for low, high in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True):
        if cancelled is not None and cancelled():
            raise InterruptedError("Peak analysis cancelled.")
        if high - low < 5:
            continue
        measured = detect_spectrum_peaks(f[low:high], y[low:high], unit=unit, parameters=parameters)
        peaks.extend(replace(peak, index=peak.index + int(low)) for peak in measured)
    return tuple(sorted(peaks, key=lambda p: abs(p.prominence_db), reverse=True)[:parameters.peak_max_count])


@dataclass(frozen=True, slots=True)
class TrackedPoint:
    checkpoint: int
    coordinate: float
    frequency_hz: float | None
    amplitude: float | None
    fwhm_hz: float | None
    q_factor: float | None
    state: str


def _series_trace(session, record, source, processing, view, points, cancelled):
    label, variant, row_id, trace_index = source
    if isinstance(record, StoredPoint):
        if not view.active and not processing.active and variant == "processed":
            raw = session.spectrum(session.path, record.index)
            if raw is None or raw.processed_values is None:
                raise ValueError("Stored processed spectrum missing.")
            return raw.frequencies_hz, raw.processed_values, raw.processed_unit
        if label == "Reference spectrum" and not view.active and not processing.active:
            raw = session.spectrum(session.path, record.index)
            reference = session.reference(session.path, raw.reference_index) if raw and raw.reference_index is not None else None
            if reference is None:
                raise ValueError("Linked reference missing.")
            return reference.frequencies_hz, reference.powers_dbm, "dBm"
        payload = read_private_view(session, record, processing, view, cancelled=cancelled)
    else:
        spectrum = session.public_spectrum(session.path, row_id, record.index)
        payload = read_public_view(session, spectrum, trace_index, points, processing, view, cancelled=cancelled)
    label = {"Stored spectrum": "Raw", "Post-processed spectrum": "Analysis"}.get(label, label)
    # Public trace names are defined by the archive, while the derived view calls
    # the primary raw trace Raw. Corrected/compare labels must match exactly.
    candidates = [trace for trace in payload.traces if trace.label == label]
    if not candidates and len(payload.traces) == 1:
        candidates = list(payload.traces)
    if len(candidates) != 1:
        raise ValueError("The selected measurement trace is not in this spectrum view.")
    trace = candidates[0]
    return trace.frequencies_hz, trace.values, trace.unit


def track_result_series(path, records, *, axis, source, processing, view, points,
                        parameters, target_hz, gate_hz, cancelled=None):
    """Nearest detected peak within a moving gate; losses remain explicit gaps."""
    if not np.isfinite(target_hz) or target_hz < 0 or not np.isfinite(gate_hz) or gate_hz <= 0:
        raise ValueError("Peak target and tracking gate must be finite frequencies.")
    session = ResultReadSession(path)
    result, unit = [], None
    target = target_hz
    for record in records:
        if cancelled is not None and cancelled():
            raise InterruptedError("Recorded peak tracking cancelled.")
        values = record.setpoints if isinstance(record, StoredPoint) else (record.setpoints or {})
        coordinate = float(values[axis]) if axis is not None else float(record.index)
        try:
            f, y, row_unit = _series_trace(session, record, source, processing, view, points, cancelled)
            if unit is not None and unit != row_unit:
                raise ValueError("Trajectory amplitude units changed between checkpoints.")
            unit = row_unit
            f, y = np.asarray(f), np.asarray(y)
            within = (f >= target - gate_hz) & (f <= target + gate_hz)
            peaks = recorded_peaks(f[within], y[within], unit, parameters, cancelled=cancelled)
        except InterruptedError:
            raise
        except Exception as exc:
            result.append(TrackedPoint(record.index, coordinate, None, None, None, None, f"unavailable: {exc}"))
            continue
        if not peaks:
            result.append(TrackedPoint(record.index, coordinate, None, None, None, None, "lost within gate"))
            continue
        peak = min(peaks, key=lambda peak: abs(peak.frequency_hz - target))
        target = peak.frequency_hz
        result.append(TrackedPoint(record.index, coordinate, peak.frequency_hz, peak.amplitude_dbm,
                                   peak.fit_fwhm_hz or peak.fwhm_hz, peak.q_factor, "detected"))
    return tuple(result), unit or ""


class _TrajectoryPlot(SpectrumPlotWidget):
    """The plot toolbar exports the same immutable source and analysis context."""

    def __init__(self, window):
        super().__init__(window, responsive_toolbar=True)
        self.window_owner = window

    def analysis_manifest(self):
        return {**self.window_owner.provenance,
                "plotted_quantity": self.window_owner.quantity.currentData(),
                "axes": {"x_unit": self._x_unit, "y_unit": self._y_unit},
                "view_ranges": self.plot.viewRange(),
                "csv_coverage": "Every recorded trajectory sample; lost peaks remain NaN gaps."}

    def _export_csv(self, path):
        ensure_derived_destination(path, self.window_owner.provenance.get("source_file"))
        super()._export_csv(Path(path))
        write_analysis_manifest(path, self.analysis_manifest())

    def export(self):
        path, selected = StationFileDialog.getSaveFileName(self, "Export recorded peak plot",
            "peak-plot.csv", "CSV (*.csv);;PNG (*.png);;SVG (*.svg)")
        if not path:
            return
        try:
            ensure_derived_destination(path, self.window_owner.provenance.get("source_file"))
            suffix = Path(path).suffix.lower()
            if "PNG" in selected or suffix == ".png":
                ImageExporter(self.plot.plotItem).export(path)
                write_analysis_manifest(path, self.analysis_manifest())
            elif "SVG" in selected or suffix == ".svg":
                SVGExporter(self.plot.plotItem).export(path)
                write_analysis_manifest(path, self.analysis_manifest())
            else:
                self._export_csv(path)
        except Exception as exc:
            self.window_owner.status.setText(f"Plot export failed: {exc}")


class PeakSeriesWindow(StationDialog):
    def __init__(self, parent, *, target_hz, gate_hz, axes, selected_axis):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Results — tracked peak across a sweep series")
        self.resize(1020, 660)
        self.setMinimumSize(660, 480)
        root = self.modal_content_layout(spacing=10)
        note = CaptionLabel("Tracks the currently filtered checkpoints in acquisition order. Fix other setpoints in Browse along to follow one physical branch. Peak losses stay gaps; crossing peaks can exchange identity.", self)
        note.setWordWrap(True)
        root.addWidget(note)
        controls = QHBoxLayout()
        self.axis = ComboBox(self)
        self.axis.addItem("Checkpoint", userData=None)
        for label, key in axes:
            self.axis.addItem(label, userData=key)
        self.axis.setCurrentIndex(max(0, self.axis.findData(selected_axis)))
        controls.addWidget(self.axis, 1)
        self.target = LineEdit(self)
        self.target.setText(format_quantity_auto(target_hz, DIMENSION_FREQUENCY))
        self.target.setAccessibleName("Initial tracked peak frequency")
        self.target.setToolTip("Initial peak frequency; the nearest accepted peak updates the gate centre each step.")
        controls.addWidget(self.target, 1)
        self.gate = LineEdit(self)
        self.gate.setText(format_quantity_auto(gate_hz, DIMENSION_FREQUENCY))
        self.gate.setAccessibleName("Peak tracking gate half width")
        self.gate.setToolTip("Maximum frequency deviation from the last detected peak; half width in Hz.")
        controls.addWidget(self.gate, 1)
        self.run = PrimaryPushButton("Track series", self)
        controls.addWidget(self.run)
        root.addLayout(controls)
        self.quantity = ComboBox(self)
        for label, key in (("Peak frequency", "frequency_hz"), ("Peak amplitude", "amplitude"),
                           ("FWHM", "fwhm_hz"), ("Q factor", "q_factor")):
            self.quantity.addItem(label, userData=key)
        root.addWidget(self.quantity)
        self.plot = _TrajectoryPlot(self)
        root.addWidget(self.plot, 1)
        self.status = CaptionLabel("Choose the axis and frequency gate, then Track series.", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        footer = QHBoxLayout()
        self.export = PushButton("Export trajectory CSV", self)
        self.export.clicked.connect(self._export)
        self.export.setEnabled(False)
        footer.addWidget(self.export)
        footer.addStretch(1)
        close = PushButton("Close", self)
        close.clicked.connect(self.close)
        footer.addWidget(close)
        root.addLayout(footer)
        self.quantity.currentIndexChanged.connect(self._render)
        self.records = ()
        self.unit = ""
        self.provenance = {}
        self.axis_label, self.axis_unit = "Checkpoint", ""

    def set_data(self, records, unit, axis_label, axis_unit, provenance):
        self.records, self.unit = records, unit
        self.axis_label, self.axis_unit = axis_label, axis_unit
        self.provenance = provenance
        detected = sum(record.state == "detected" for record in records)
        self.status.setText(f"{detected} detected / {len(records)} recorded checkpoints; {len(records) - detected} lost or unavailable. No interpolation across gaps.")
        self.run.setEnabled(True)
        self.export.setEnabled(True)
        self._render()

    def _render(self, *_args):
        if not self.records:
            return
        key = self.quantity.currentData()
        unit = self.unit if key == "amplitude" else "" if key == "q_factor" else "Hz"
        self.plot.clear()
        self.plot.set_labels(x=self.axis_label, x_unit=self.axis_unit, y=self.quantity.currentText(), y_unit=unit)
        self.plot.set_trace(self.quantity.currentText(), [r.coordinate for r in self.records],
            [getattr(r, key) if getattr(r, key) is not None else np.nan for r in self.records], primary=True, show_points=True)
        self.plot.auto_range()

    def export_csv(self, path):
        ensure_derived_destination(path, self.provenance.get("source_file"))
        with Path(path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["checkpoint", f"{self.axis_label} [{self.axis_unit}]", "frequency_hz",
                             f"amplitude [{self.unit}]", "fwhm_hz", "q_factor", "state"])
            writer.writerows((r.checkpoint, r.coordinate, r.frequency_hz, r.amplitude, r.fwhm_hz, r.q_factor, r.state)
                            for r in self.records)
        write_analysis_manifest(path, self.provenance)

    def _export(self):
        path, _ = StationFileDialog.getSaveFileName(self, "Export recorded peak trajectory", "peak-trajectory.csv", "CSV (*.csv)")
        if path:
            try:
                self.export_csv(path)
            except Exception as exc:
                self.status.setText(f"Export failed: {exc}")


class ResultPeakTools(QObject):
    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._tasks, self._contexts = {}, {}
        self._request = self._peak_request = 0
        self.parameters = SpectrumAnalysisParameters(peak_measure_filtered=True)
        self.table = None
        self.peaks = ()
        self.windows = []
        tab.spectrum_plot.trace_selector.currentTextChanged.connect(lambda _text: self.spectrum_changed())
        tab.spectrum_plot.peak_selected.connect(self._plot_selected)

    def open(self):
        if self.table is None:
            self.table = PeakTableDialog(self.tab.window())
            self.table.setWindowTitle("Results — detected peaks and sweep trajectories")
            self.table.track_selected.setText("Track across sweep…")
            self.table.track_selected.setToolTip("Open an independent trajectory for this peak using the currently filtered checkpoint series.")
            self.table.track_requested.connect(self._track_requested)
            self.table.peak_selected.connect(self.tab.spectrum_plot.select_peak_marker)
            settings = PushButton("Peak settings…", self.table)
            settings.clicked.connect(self._settings)
            self.table.modal_content_layout().itemAt(self.table.modal_content_layout().count() - 1).layout().insertWidget(1, settings)
        self.table.show()
        self.table.raise_()
        self.table.activateWindow()
        self.spectrum_changed()

    def _submit(self, context, operation, *args, **kwargs):
        self._request += 1
        request = self._request
        task = ResultReadTask(request, operation, *args, cooperative_cancel=True, **kwargs)
        self._tasks[request] = task
        self._contexts[request] = context
        task.signals.loaded.connect(self._loaded)
        task.signals.failed.connect(self._failed)
        task.signals.finished.connect(self._finished)
        self.pool.start(task)
        return request

    def spectrum_changed(self):
        self._peak_request = -1
        for request, context in tuple(self._contexts.items()):
            if context == "peaks" and request in self._tasks:
                self._tasks[request].cancel()
        self.peaks = ()
        self.tab.spectrum_plot.clear_peak_markers()
        if self.table is None or not self.table.isVisible():
            return
        self.table.set_peaks((), method="Analysing selected measurement trace…")
        plot = self.tab.spectrum_plot
        data = plot._measurement_data()
        if data is None:
            return
        self._peak_request = self._submit("peaks", recorded_peaks, data[0].copy(), data[1].copy(),
                                          plot._measurement_unit(), self.parameters)

    def _loaded(self, request, payload):
        context = self._contexts.get(request)
        if context == "peaks" and request == self._peak_request:
            self.peaks = payload
            self.tab.spectrum_plot.set_peak_markers([p.frequency_hz for p in payload], [p.amplitude_dbm for p in payload])
            if self.table is not None:
                self.table.set_peaks(payload, method="Selected recorded trace · shared Anritsu peak detector")
        elif isinstance(context, tuple):
            window, label, unit, provenance = context
            if window.isVisible():
                window.set_data(*payload, label, unit, provenance)

    def _failed(self, request, message):
        context = self._contexts.get(request)
        if context == "peaks" and self.table is not None and request == self._peak_request:
            self.table.status.setText(f"Cannot analyse peaks: {message}")
        elif isinstance(context, tuple):
            context[0].status.setText(f"Cannot track series: {message}")
            context[0].run.setEnabled(True)

    def _finished(self, request):
        self._tasks.pop(request, None)
        self._contexts.pop(request, None)

    def _plot_selected(self, index):
        if self.table is not None and 0 <= index < len(self.peaks):
            self.table.table.selectRow(index)

    def _settings(self):
        dialog = SpectrumAnalysisSettingsDialog(self.table, current_parameters=self.parameters,
            section="peaks", source_unit=self.tab.spectrum_plot._measurement_unit(), selected_trace_only=True)
        dialog.parameters_applied.connect(self._apply_parameters)
        dialog.exec()
        dialog.deleteLater()

    def _apply_parameters(self, parameters):
        self.parameters = parameters
        self.spectrum_changed()

    def _track_requested(self, index):
        if not 0 <= index < len(self.peaks):
            return
        if self.tab._selected_private_point is None and self.tab._public_spectrum is None:
            self.table.status.setText("Choose a measurement checkpoint to track a peak across sweep parameters.")
            return
        records = tuple(record for record in self.tab.points_model.records
                        if not isinstance(record, StoredPoint) or record.has_spectrum)
        if not records:
            self.table.status.setText("Choose a recorded checkpoint series to track this peak.")
            return
        keys = set.intersection(*(set(record.setpoints or {}) for record in records))
        from app.recipes.parameter_registry import parameter_descriptor
        axes = []
        descriptors = {}
        for key in sorted(keys):
            try:
                descriptor = parameter_descriptor(key)
            except KeyError:
                descriptor = None
            descriptors[key] = (descriptor.ui_label, descriptor.unit) if descriptor else (key, "")
            label, unit = descriptors[key]
            axes.append((f"{label} [{unit}]" if unit else label, key))
        data = self.tab.spectrum_plot._measurement_data()
        span = float(np.ptp(data[0])) if data is not None else 1e6
        window = PeakSeriesWindow(self.tab.window(), target_hz=self.peaks[index].frequency_hz,
            gate_hz=max(span / 10, 1.), axes=axes, selected_axis=self.tab.series_controls.axis.currentData())
        self.windows.append(window)
        # An immutable snapshot keeps each trajectory independent of subsequent
        # file selection, filter changes or newly opened tracking windows.
        path = self.tab._selected_path
        processing, view = self.tab.processing_controls.state, self.tab.view_controls.state
        source = (self.tab.spectrum_plot.trace_selector.currentText(),
                  self.tab.spectrum_variant_combo.currentData(),
                  self.tab._public_spectrum.row_id if self.tab._public_spectrum else None,
                  int(self.tab.thatec_trace_combo.currentData() or 0))
        points = self.tab._stored_points
        provenance = dict(self.tab.spectrum_plot.export_metadata)
        parameters = self.parameters
        def start():
            try:
                target = parse_quantity(window.target.text(), DIMENSION_FREQUENCY).si_value
                gate = parse_quantity(window.gate.text(), DIMENSION_FREQUENCY).si_value
                if target < 0 or gate <= 0:
                    raise ValueError("Target must be non-negative and gate half width positive.")
            except (ValueError, TypeError) as exc:
                window.status.setText(str(exc))
                return
            for request, context in tuple(self._contexts.items()):
                if isinstance(context, tuple) and context[0] is window:
                    self._tasks[request].cancel()
            axis = window.axis.currentData()
            label, unit = descriptors.get(axis, ("Checkpoint", ""))
            manifest = {**provenance, "peak_parameters": processing_manifest(parameters),
                        "trajectory": {"axis": axis, "initial_frequency_hz": target,
                                       "gate_half_width_hz": gate, "source": list(source),
                                       "checkpoints": [record.index for record in records]}}
            window.run.setEnabled(False)
            window.status.setText("Tracking recorded spectra in a background worker…")
            self._submit((window, label, unit, manifest), track_result_series, path, records,
                axis=axis, source=source, processing=processing, view=view, points=points,
                parameters=parameters, target_hz=target, gate_hz=gate)
        window.run.clicked.connect(start)
        window.finished.connect(lambda _result, window=window: self._close_track(window))
        window.show()

    def _close_track(self, window):
        for request, context in tuple(self._contexts.items()):
            if isinstance(context, tuple) and context[0] is window:
                self._tasks[request].cancel()
                self._contexts.pop(request, None)
        if window in self.windows:
            self.windows.remove(window)
        window.deleteLater()

    def cancel(self):
        for task in self._tasks.values():
            task.cancel()
        self.pool.clear()
        self._tasks.clear()
        self._contexts.clear()
        self._peak_request = -1

    def close(self):
        self.cancel()
        if self.table is not None:
            self.table.close()
        for window in tuple(self.windows):
            window.close()
