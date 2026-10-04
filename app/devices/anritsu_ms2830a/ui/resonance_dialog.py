"""Fluent offline resonance analysis of closed raw REF/SIGNAL archives."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, SpinBox, TitleLabel

from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_POWER, format_quantity_auto, parse_quantity
from app.spectrum.resonance_bootstrap import ResonanceBootstrapConfig
from app.ui.dialogs import StationDialog, StationFileDialog
from .correction_controller import SpectrumCorrectionController, SpectrumResonanceBootstrapRequest


class SpectrumResonanceDialog(StationDialog):
    def __init__(self, parent=None):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Resonance analysis")
        self.resize(850, 850)
        self._busy = False
        self._closing = False
        self._controller = SpectrumCorrectionController(self)
        self._controller.completed.connect(self._completed)
        self._controller.failed.connect(self._failed)
        self._controller.cancelled.connect(self._cancelled)
        self._controller.processing_progress.connect(self._progress)
        layout = self.modal_content_layout(spacing=12)
        layout.addWidget(TitleLabel("Analyze a recorded resonance", self))
        hint = BodyLabel("Choose closed raw REF and SIGNAL archives. The source files remain unchanged. "
                         "Known single Gaussian or Lorentzian hypothesis; this is not a peak detector.", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._editors = []
        self.reference = self._path_row(form, "Raw REF archive", "reference")
        self.signal = self._path_row(form, "Raw SIGNAL archive", "signal")
        self.output = self._path_row(form, "New JSON report", "output")
        self.center, self.fwhm_input = LineEdit(content), LineEdit(content)
        self.center.setPlaceholderText("e.g. 1.5 GHz")
        self.fwhm_input.setPlaceholderText("e.g. 100 MHz")
        form.addRow("Initial center (with unit)", self.center)
        form.addRow("Initial FWHM (with unit)", self.fwhm_input)
        self.shape = ComboBox(content)
        self.shape.addItem("Gaussian", userData="gaussian")
        self.shape.addItem("Lorentzian", userData="lorentzian")
        form.addRow("Resonance hypothesis", self.shape)
        self.blocks = SpinBox(content)
        self.blocks.setRange(1, 1000000)
        self.blocks.setValue(10)
        form.addRow("Accepted sweeps per block", self.blocks)
        self.resamples = SpinBox(content)
        self.resamples.setRange(200, 10000)
        self.resamples.setValue(1000)
        form.addRow("Bootstrap replicas", self.resamples)
        self.method = ComboBox(content)
        self.method.addItem("Studentized · coverage unqualified", userData="studentized")
        self.method.addItem("Percentile · coverage unqualified", userData="percentile")
        form.addRow("Experimental interval method", self.method)
        self.discard_tail = CheckBox("Allow and report partial final block discard", content)
        form.addRow(self.discard_tail)
        self.independent = CheckBox("Block independence qualified", content)
        self.equivalent = CheckBox("REF/SIGNAL background equivalence qualified", content)
        self.stationary = CheckBox("Signal stationarity qualified", content)
        for control in (self.independent, self.equivalent, self.stationary):
            form.addRow(control)
        self.evidence = LineEdit(content)
        self.evidence.setPlaceholderText("Recorded evidence supporting the checked assumptions")
        form.addRow("Qualification evidence", self.evidence)
        note = BodyLabel("Without all three assumptions, only the point fit is reported. "
                         "Checking them does not qualify CI coverage. Synthetic validation covers limited scenarios; "
                         "results remain experimental, not laboratory-qualified.", content)
        note.setWordWrap(True)
        form.addRow(note)
        self._editors.extend((self.center, self.fwhm_input, self.shape, self.blocks, self.resamples, self.method,
                              self.discard_tail, self.independent, self.equivalent, self.stationary, self.evidence))
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.status = BodyLabel("Ready. Choose archives and provide the center and width with units.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.result_label = BodyLabel("No analysis result.", self)
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        actions = QHBoxLayout()
        self.start = PrimaryPushButton("Analyze and save report", self)
        self.cancel = PushButton("Cancel analysis", self)
        self.cancel.setEnabled(False)
        self.close_button = PushButton("Close", self)
        for button in (self.start, self.cancel, self.close_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.start.clicked.connect(self._start)
        self.cancel.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.reject)

    def _path_row(self, form, label, role):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        editor = LineEdit(row)
        editor.setAccessibleName(label)
        button = PushButton("Choose…", row)
        button.clicked.connect(lambda: self._choose(editor, role))
        layout.addWidget(editor, 1)
        layout.addWidget(button)
        form.addRow(label, row)
        self._editors.extend((editor, button))
        return editor

    def _choose(self, editor, role):
        if role == "output":
            path, _ = StationFileDialog.getSaveFileName(self, "New resonance report", "resonance.json", "JSON (*.json)")
        else:
            path, _ = StationFileDialog.getOpenFileName(self, f"Closed raw {role.upper()} archive", "", "HDF5 (*.h5)")
        if path:
            editor.setText(path)

    def _request(self):
        if not all(control.text().strip() for control in (self.reference, self.signal, self.output)):
            raise ValueError("Choose REF, SIGNAL and a new report destination.")
        return SpectrumResonanceBootstrapRequest(Path(self.reference.text().strip()), Path(self.signal.text().strip()),
            Path(self.output.text().strip()), self.blocks.value(),
            parse_quantity(self.center.text(), DIMENSION_FREQUENCY).si_value,
            parse_quantity(self.fwhm_input.text(), DIMENSION_FREQUENCY).si_value,
            shape=self.shape.currentData(), discard_partial_tail=self.discard_tail.isChecked(),
            config=ResonanceBootstrapConfig(resamples=self.resamples.value(), interval_method=self.method.currentData(),
                independent_blocks_qualified=self.independent.isChecked(),
                reference_equivalence_qualified=self.equivalent.isChecked(), stationary_signal_qualified=self.stationary.isChecked(),
                qualification_evidence=self.evidence.text()))

    def _set_busy(self, busy):
        self._busy = busy
        self.start.setEnabled(not busy and not self._closing)
        self.cancel.setEnabled(busy and not self._closing)
        for editor in self._editors:
            editor.setEnabled(not busy and not self._closing)

    def _start(self):
        try:
            request = self._request()
            self._controller.bootstrap_archives(request)
        except (ValueError, BufferError) as exc:
            self.status.setText(f"Cannot start: {exc}")
            return
        self._set_busy(True)
        self.status.setText("Reading closed archives and checking source identity…")
        self.result_label.setText("Waiting for this analysis result.")
        self.progress.setRange(0, 0)
        self.progress.show()

    def _progress(self, operation, done, total):
        if operation != "bootstrap_resonance" or self._closing:
            return
        if done:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.status.setText(f"Bootstrap: {done}/{total} replicas. Report has not yet been published.")

    def _completed(self, operation, payload):
        if operation != "bootstrap_resonance" or self._closing:
            return
        self._set_busy(False)
        self.progress.hide()
        path, report = payload
        self.status.setText(f"Report saved: {path}")
        estimate = report["estimate"]
        self.result_label.setText(
            f"Amplitude: {format_quantity_auto(estimate['amplitude_w'], DIMENSION_POWER)}\n"
            f"Center: {format_quantity_auto(estimate['center_hz'], DIMENSION_FREQUENCY)}\n"
            f"FWHM: {format_quantity_auto(estimate['fwhm_hz'], DIMENSION_FREQUENCY)}\n"
            f"Finite-window area: {estimate['finite_window_area_w_hz']:.6g} W·Hz\n"
            f"{report['reference_blocks']} REF / {report['signal_blocks']} SIGNAL blocks. "
            + ("Experimental intervals are recorded in the report; coverage remains unqualified."
               if report["confidence_intervals"] is not None else f"No confidence intervals ({report['status']}).")
        )

    def _failed(self, operation, error):
        if operation == "bootstrap_resonance" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText(f"Analysis failed: {error}")
            self.result_label.setText("No report was published by this analysis.")

    def _cancel(self):
        self._controller.cancel_processing()
        self.cancel.setEnabled(False)
        self.status.setText("Canceling analysis; waiting for the current checkpoint…")

    def _cancelled(self, operation):
        if operation == "bootstrap_resonance" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText("Analysis canceled. No report published; source archives unchanged.")

    def reject(self):
        self._closing = True
        self._set_busy(self._busy)
        self.status.setText("Closing analysis worker…")
        if self._controller.close(wait_ms=0):
            super().reject()
        else:
            QTimer.singleShot(50, self, self.reject)

    def closeEvent(self, event):
        event.ignore()
        self.reject()

    def shutdown(self):
        self._closing = True
        # The owning window must stay alive while this worker drains. Retain
        # ownership so a subsequent close can safely inspect the controller.
        return self._controller.close(wait_ms=0)
