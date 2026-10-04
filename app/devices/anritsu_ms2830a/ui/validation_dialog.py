"""Fluent diagnostics of a model on temporally disjoint raw reference spectra."""

import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, LineEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, TitleLabel

from app.ui.dialogs import StationDialog, StationFileDialog
from .correction_controller import SpectrumCorrectionController, SpectrumInterferenceValidationRequest
from .training_dialog import parse_region_text


class SpectrumValidationDialog(StationDialog):
    def __init__(self, parent=None):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Validate background model")
        self.resize(850, 750)
        self._busy = self._closing = False
        self._controller = SpectrumCorrectionController(self)
        self._controller.completed.connect(self._completed)
        self._controller.failed.connect(self._failed)
        self._controller.cancelled.connect(self._cancelled)
        layout = self.modal_content_layout(spacing=12)
        layout.addWidget(TitleLabel("Validate on a separate recorded REF", self))
        hint = BodyLabel("Use a completed reference recording outside the training interval. "
                         "Model and reference archives remain unchanged. SIGNAL is not used in this diagnostic.", self)
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
        self.model = self._path_row(form, "Closed model archive", False)
        self.reference = self._path_row(form, "Separate raw REF", False)
        self.output = self._path_row(form, "New diagnostic report", True)
        self.model_id = LineEdit(content)
        self.model_id.setPlaceholderText("Optional if the archive contains exactly one model")
        form.addRow("Model identifier", self.model_id)
        self.regions = LineEdit(content)
        self.regions.setPlaceholderText("Empty: recorded protected bins; or start .. stop; start .. stop")
        self.regions.setAccessibleName("Held-out frequency regions")
        form.addRow("Held-out frequency regions", self.regions)
        self._editors.extend((self.model_id, self.regions))
        syntax = BodyLabel("Include frequency units, e.g. 1.48 MHz .. 1.52 MHz. "
                           "These bins must be excluded from coefficient fitting. Empty uses the model's protected bins.", content)
        syntax.setWordWrap(True)
        form.addRow(syntax)
        note = BodyLabel("Errors are measured only on accepted fits, with rejected fits counted separately. "
                         "Temporal separation does not establish statistical independence. "
                         "This diagnostic does not qualify confidence intervals, approve the model for Live "
                         "or prove preservation of an unknown magnetic signal.", content)
        note.setWordWrap(True)
        form.addRow(note)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.status = BodyLabel("Ready. Choose a model and a separate completed raw REF.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.result_label = BodyLabel("No validation result.", self)
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        actions = QHBoxLayout()
        self.start = PrimaryPushButton("Validate and save report", self)
        self.cancel = PushButton("Cancel validation", self)
        self.cancel.setEnabled(False)
        self.close_button = PushButton("Close", self)
        for button in (self.start, self.cancel, self.close_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.start.clicked.connect(self._start)
        self.cancel.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.reject)

    def _path_row(self, form, label, output):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        editor = LineEdit(row)
        editor.setAccessibleName(label)
        button = PushButton("Choose…", row)

        def choose():
            if output:
                path, _ = StationFileDialog.getSaveFileName(self, label, "validation.json", "JSON (*.json)")
            else:
                path, _ = StationFileDialog.getOpenFileName(self, label, "", "HDF5 (*.h5)")
            if path:
                editor.setText(path)

        button.clicked.connect(choose)
        layout.addWidget(editor, 1)
        layout.addWidget(button)
        form.addRow(label, row)
        self._editors.extend((editor, button))
        return editor

    def _request(self):
        if not all(editor.text().strip() for editor in (self.model, self.reference, self.output)):
            raise ValueError("Choose model, separate REF and a new report.")
        regions = parse_region_text(self.regions.text()) if self.regions.text().strip() else None
        return SpectrumInterferenceValidationRequest(Path(self.model.text().strip()), Path(self.reference.text().strip()),
            Path(self.output.text().strip()), model_id=self.model_id.text().strip() or None,
            validation_regions_json=json.dumps(regions, allow_nan=False) if regions is not None else None)

    def _set_busy(self, busy):
        self._busy = busy
        self.start.setEnabled(not busy and not self._closing)
        self.cancel.setEnabled(busy and not self._closing)
        for control in self._editors:
            control.setEnabled(not busy and not self._closing)

    def _start(self):
        try:
            self._controller.validate_interference_archive(self._request())
        except (ValueError, BufferError) as exc:
            self.status.setText(f"Cannot start: {exc}")
            return
        self._set_busy(True)
        self.status.setText("Reading closed sources, checking disjointness and measuring prediction errors…")
        self.result_label.setText("Waiting for this validation result.")
        self.progress.setRange(0, 0)
        self.progress.show()

    def _completed(self, operation, payload):
        if operation != "validate_interference" or self._closing:
            return
        self._set_busy(False)
        self.progress.hide()
        path, report = payload
        self.status.setText(f"Report saved: {path}")
        model_error = report["model_error_on_accepted_fits"]
        static_error = report["static_error_on_same_accepted_fits"]
        errors = ("RMS unavailable: no accepted fits." if model_error is None else
                  f"Held-out RMS on accepted fits: model {model_error['unused_region_rms_w']:.6g} W; "
                  f"static reference {static_error['unused_region_rms_w']:.6g} W.")
        self.result_label.setText(f"Model {report['model_id']}: {report['reference_sweeps']} REF sweeps; "
            f"{report['accepted_fits']} accepted / {report['rejected_fits']} rejected fits.\n"
            f"{len(report['validation_bin_indices'])} held-out frequency bins. {errors}\n"
            "Diagnostics only. No automatic model approval, confidence interval or SIGNAL-preservation qualification.")

    def _failed(self, operation, error):
        if operation == "validate_interference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText(f"Validation failed: {error}")
            self.result_label.setText("No report published by this validation. Source archives unchanged.")

    def _cancel(self):
        self._controller.cancel_processing()
        self.cancel.setEnabled(False)
        self.status.setText("Canceling validation; waiting for the current checkpoint…")

    def _cancelled(self, operation):
        if operation == "validate_interference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText("Validation canceled. No report published; source archives unchanged.")
            self.result_label.setText("No validation result.")

    def reject(self):
        self._closing = True
        self._set_busy(self._busy)
        self.status.setText("Closing validation worker…")
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
