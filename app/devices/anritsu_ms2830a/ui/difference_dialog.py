"""Fluent offline global difference comparison of closed raw archives."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, LineEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, SpinBox, TitleLabel

from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_RATIO, parse_quantity
from app.spectrum.spectral_difference_test import SpectralDifferenceTestConfig
from app.ui.dialogs import StationDialog, StationFileDialog
from .correction_controller import SpectrumCorrectionController, SpectrumDifferenceRequest


class SpectrumDifferenceDialog(StationDialog):
    def __init__(self, parent=None):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Global spectrum difference")
        self.resize(850, 850)
        self._busy = False
        self._closing = False
        self._controller = SpectrumCorrectionController(self)
        self._controller.completed.connect(self._completed)
        self._controller.failed.connect(self._failed)
        self._controller.cancelled.connect(self._cancelled)
        self._controller.processing_progress.connect(self._progress)
        layout = self.modal_content_layout(spacing=12)
        layout.addWidget(TitleLabel("Compare recorded REF and SIGNAL", self))
        hint = BodyLabel("Compare closed raw archives over one fixed frequency range. "
                         "A global difference does not identify a magnetic resonance. Source spectra remain unchanged.", self)
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
        self.search_start, self.search_stop = LineEdit(content), LineEdit(content)
        self.search_start.setPlaceholderText("e.g. 1 MHz")
        self.search_stop.setPlaceholderText("e.g. 2 MHz")
        form.addRow("Search start (with unit)", self.search_start)
        form.addRow("Search stop (with unit)", self.search_stop)
        self.blocks = SpinBox(content)
        self.blocks.setRange(1, 1000000)
        self.blocks.setValue(10)
        form.addRow("Accepted sweeps per block", self.blocks)
        self.permutations = SpinBox(content)
        self.permutations.setRange(99, 99999)
        self.permutations.setValue(999)
        form.addRow("Whole-block permutations", self.permutations)
        self.alpha_input = LineEdit(content)
        self.alpha_input.setText("0.5 %")
        form.addRow("Alarm significance level", self.alpha_input)
        self.discard_tail = CheckBox("Allow and report partial final block discard", content)
        self.independent = CheckBox("Block independence qualified", content)
        self.exchangeable = CheckBox("REF/SIGNAL null distributions match", content)
        self.exchangeable.setToolTip("Under absence of a signal, complete blocks must be exchangeable between REF and SIGNAL. "
                                    "Drift or state-dependent noise can invalidate this assumption.")
        for control in (self.discard_tail, self.independent, self.exchangeable):
            form.addRow(control)
        self.evidence = LineEdit(content)
        self.evidence.setPlaceholderText("Recorded evidence supporting the checked assumptions")
        form.addRow("Qualification evidence", self.evidence)
        note = BodyLabel("Without both assumptions, the report records unqualified status without a p-value. "
                         "This is one fixed comparison; repeated Live comparisons require separate false-alarm control. "
                         "Synthetic validation does not qualify laboratory measurements.", content)
        note.setWordWrap(True)
        form.addRow(note)
        self._editors.extend((self.search_start, self.search_stop, self.blocks, self.permutations, self.alpha_input,
                              self.discard_tail, self.independent, self.exchangeable, self.evidence))
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.status = BodyLabel("Ready. Choose archives and enter the search range with units.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.result_label = BodyLabel("No comparison result.", self)
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        actions = QHBoxLayout()
        self.start = PrimaryPushButton("Compare and save report", self)
        self.cancel = PushButton("Cancel comparison", self)
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
            path, _ = StationFileDialog.getSaveFileName(self, "New global difference report", "difference.json", "JSON (*.json)")
        else:
            path, _ = StationFileDialog.getOpenFileName(self, f"Closed raw {role.upper()} archive", "", "HDF5 (*.h5)")
        if path:
            editor.setText(path)

    def _request(self):
        if not all(control.text().strip() for control in (self.reference, self.signal, self.output)):
            raise ValueError("Choose REF, SIGNAL and a new report destination.")
        start = parse_quantity(self.search_start.text(), DIMENSION_FREQUENCY).si_value
        stop = parse_quantity(self.search_stop.text(), DIMENSION_FREQUENCY).si_value
        if start < 0 or stop <= start:
            raise ValueError("Search stop must exceed a nonnegative search start.")
        return SpectrumDifferenceRequest(Path(self.reference.text().strip()), Path(self.signal.text().strip()),
            Path(self.output.text().strip()), self.blocks.value(), start, stop,
            discard_partial_tail=self.discard_tail.isChecked(), config=SpectralDifferenceTestConfig(
                permutations=self.permutations.value(), alpha=parse_quantity(self.alpha_input.text(), DIMENSION_RATIO).si_value,
                independent_blocks_qualified=self.independent.isChecked(),
                null_exchangeability_qualified=self.exchangeable.isChecked(), qualification_evidence=self.evidence.text()))

    def _set_busy(self, busy):
        self._busy = busy
        self.start.setEnabled(not busy and not self._closing)
        self.cancel.setEnabled(busy and not self._closing)
        for editor in self._editors:
            editor.setEnabled(not busy and not self._closing)

    def _start(self):
        try:
            request = self._request()
            self._controller.difference_archives(request)
        except (ValueError, BufferError) as exc:
            self.status.setText(f"Cannot start: {exc}")
            return
        self._set_busy(True)
        self.status.setText("Reading closed archives and checking source identity…")
        self.result_label.setText("Waiting for this comparison result.")
        self.progress.setRange(0, 0)
        self.progress.show()

    def _progress(self, operation, done, total):
        if operation != "spectral_difference" or self._closing or not done:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.setText(f"Comparison: {done}/{total} permutations. Report has not yet been published.")

    def _completed(self, operation, payload):
        if operation != "spectral_difference" or self._closing:
            return
        self._set_busy(False)
        self.progress.hide()
        path, report = payload
        self.status.setText(f"Report saved: {path}")
        p_value = report["p_value"]
        outcome = (f"No quantitative test ({report['status']}); p-value unavailable." if p_value is None else
                   f"{'Global difference detected' if report['global_difference_detected'] else 'No global difference detected'}; "
                   f"p = {p_value:.6g}, alpha = {report['config']['alpha']:.6g}.")
        self.result_label.setText(
            f"{outcome}\n{report['reference_blocks']} REF / {report['signal_blocks']} SIGNAL blocks; "
            f"{len(report['search_bin_indices'])} frequency bins searched.\n"
            "This does not identify a magnetic resonance or qualify laboratory false-alarm control. "
            "A non-detection does not prove absence of a signal."
        )

    def _failed(self, operation, error):
        if operation == "spectral_difference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText(f"Comparison failed: {error}")
            self.result_label.setText("No report was published by this comparison.")

    def _cancel(self):
        self._controller.cancel_processing()
        self.cancel.setEnabled(False)
        self.status.setText("Canceling comparison; waiting for the current checkpoint…")

    def _cancelled(self, operation):
        if operation == "spectral_difference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText("Comparison canceled. No report published; source archives unchanged.")

    def reject(self):
        self._closing = True
        self._set_busy(self._busy)
        self.status.setText("Closing comparison worker…")
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
