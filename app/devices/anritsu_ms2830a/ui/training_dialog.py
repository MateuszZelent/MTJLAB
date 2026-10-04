"""Fluent preparation of a reference-only local interference calibration."""

import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, LineEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, SpinBox, TitleLabel

from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_POWER, parse_quantity
from app.spectrum.interference_training import InterferenceTrainingConfig
from app.ui.dialogs import StationDialog, StationFileDialog
from .correction_controller import SpectrumCorrectionController, SpectrumInterferenceTrainingRequest


def parse_region_text(text, *, required=False):
    """Semicolon-separated quantity .. quantity pairs, without decimal-comma ambiguity."""
    if not text.strip():
        if required:
            raise ValueError("Provide at least one frequency region.")
        return []
    entries = text.split(";")
    if len(entries) > 32:
        raise ValueError("Specify at most 32 frequency regions.")
    regions = []
    for entry in entries:
        pair = [value.strip() for value in entry.split("..")]
        if len(pair) != 2:
            raise ValueError("Use start .. stop for each region; separate regions with semicolons.")
        low, high = [parse_quantity(value, DIMENSION_FREQUENCY).si_value for value in pair]
        if low < 0 or high <= low:
            raise ValueError("Frequency regions must have nonnegative start and greater stop.")
        regions.append(pair)
    return regions


class SpectrumTrainingDialog(StationDialog):
    def __init__(self, parent=None):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Prepare background model")
        self.resize(850, 850)
        self._busy = self._closing = False
        self._controller = SpectrumCorrectionController(self)
        self._controller.completed.connect(self._completed)
        self._controller.failed.connect(self._failed)
        self._controller.cancelled.connect(self._cancelled)
        layout = self.modal_content_layout(spacing=12)
        layout.addWidget(TitleLabel("Prepare a model from recorded REF", self))
        hint = BodyLabel("Train local interference components from a completed raw reference archive. "
                         "The reference remains unchanged; SIGNAL is never used to train this model.", self)
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
        self.reference = self._path_row(form, "Completed raw REF", False)
        self.output = self._path_row(form, "New model archive", True)
        self.model_id = LineEdit(content)
        self.model_id.setPlaceholderText("e.g. local-emi-model")
        form.addRow("Model identifier", self.model_id)
        syntax = BodyLabel("Regions: start .. stop; start .. stop. Include units, e.g. 1 MHz .. 2 MHz. "
                           "Protected regions must include possible signal tails and the RBW margin.", content)
        syntax.setWordWrap(True)
        form.addRow(syntax)
        self.nuisance, self.controls, self.protected = [LineEdit(content) for _ in range(3)]
        for label, editor in (("Local interference regions", self.nuisance), ("Control fitting regions", self.controls),
                              ("Protected signal regions", self.protected)):
            editor.setAccessibleName(label)
            form.addRow(label, editor)
        self.sigma_input = LineEdit(content)
        self.sigma_input.setPlaceholderText("e.g. 1 pW")
        self.sigma_input.setToolTip("Positive power scale used in the control fit; training does not qualify it as measurement uncertainty.")
        form.addRow("Control fitting scale", self.sigma_input)
        self.components = SpinBox(content)
        self.components.setRange(1, 8)
        self.components.setValue(2)
        form.addRow("Local components", self.components)
        self.frame_cap = SpinBox(content)
        self.frame_cap.setRange(3, 256)
        self.frame_cap.setValue(64)
        form.addRow("Maximum training frames", self.frame_cap)
        self.controls_qualified = CheckBox("Control regions independently qualified to exclude signal", content)
        form.addRow(self.controls_qualified)
        self.evidence = LineEdit(content)
        self.evidence.setPlaceholderText("Recorded evidence supporting signal-free control regions")
        form.addRow("Qualification evidence", self.evidence)
        note = BodyLabel("Training does not infer signal-free controls, independence or confidence intervals. "
                         "Without qualified controls the model cannot be used for live correction. "
                         "Validate it on separate reference recordings and test signal preservation.", content)
        note.setWordWrap(True)
        form.addRow(note)
        self._editors.extend((self.model_id, self.nuisance, self.controls, self.protected, self.sigma_input,
                              self.components, self.frame_cap, self.controls_qualified, self.evidence))
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.status = BodyLabel("Ready. Choose a completed REF and define explicit frequency regions.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.result_label = BodyLabel("No training result.", self)
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        actions = QHBoxLayout()
        self.start = PrimaryPushButton("Train and save model", self)
        self.cancel = PushButton("Cancel training", self)
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
        button.clicked.connect(lambda: self._choose(editor, output))
        layout.addWidget(editor, 1)
        layout.addWidget(button)
        form.addRow(label, row)
        self._editors.extend((editor, button))
        return editor

    def _choose(self, editor, output):
        chooser = StationFileDialog.getSaveFileName if output else StationFileDialog.getOpenFileName
        path, _ = chooser(self, "New model archive" if output else "Completed raw REF", "model.h5" if output else "", "HDF5 (*.h5)")
        if path:
            editor.setText(path)

    def _request(self):
        if not self.reference.text().strip() or not self.output.text().strip() or not self.model_id.text().strip():
            raise ValueError("Choose REF, new model archive and a model identifier.")
        config = InterferenceTrainingConfig(components=self.components.value(), maximum_training_frames=self.frame_cap.value())
        sigma_w = parse_quantity(self.sigma_input.text(), DIMENSION_POWER).si_value
        if sigma_w <= 0:
            raise ValueError("Control fitting scale must be a positive power quantity.")
        if self.controls_qualified.isChecked() and not self.evidence.text().strip():
            raise ValueError("Qualified control regions require recorded evidence.")
        specification = {"model_id": self.model_id.text().strip(),
            "nuisance_regions": parse_region_text(self.nuisance.text(), required=True),
            "control_regions": parse_region_text(self.controls.text(), required=True),
            "protected_regions": parse_region_text(self.protected.text()), "control_sigma": self.sigma_input.text().strip(),
            "components": config.components, "maximum_training_frames": config.maximum_training_frames,
            "signal_control_regions_qualified": self.controls_qualified.isChecked(), "qualification_evidence": self.evidence.text()}
        return SpectrumInterferenceTrainingRequest(Path(self.reference.text().strip()), Path(self.output.text().strip()),
                                                   json.dumps(specification, allow_nan=False))

    def _set_busy(self, busy):
        self._busy = busy
        self.start.setEnabled(not busy and not self._closing)
        self.cancel.setEnabled(busy and not self._closing)
        for control in self._editors:
            control.setEnabled(not busy and not self._closing)

    def _start(self):
        try:
            self._controller.train_interference_archive(self._request())
        except (ValueError, BufferError) as exc:
            self.status.setText(f"Cannot start: {exc}")
            return
        self._set_busy(True)
        self.status.setText("Reading REF, fitting local components and saving the model…")
        self.result_label.setText("Waiting for this training result.")
        self.progress.setRange(0, 0)
        self.progress.show()

    def _completed(self, operation, payload):
        if operation != "train_interference" or self._closing:
            return
        self._set_busy(False)
        self.progress.hide()
        path, model = payload
        self.status.setText(f"Model saved: {path}")
        provenance = json.loads(model.training_provenance_json)
        self.result_label.setText(f"Model {model.model_id}: {model.basis_w.shape[1]} local components; "
            f"{provenance['reference_sweeps']} reference sweeps.\n"
            + ("Control qualification recorded from your evidence. " if model.signal_control_regions_qualified else
               "Controls unqualified: model cannot be used for live correction. ")
            + "Held-out validation and signal-preservation checks remain required. "
            "Training does not qualify uncertainty or laboratory performance.")

    def _failed(self, operation, error):
        if operation == "train_interference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText(f"Training failed: {error}")
            self.result_label.setText("No completed model published. Any interrupted artifact remains aborted or faulted.")

    def _cancel(self):
        self._controller.cancel_processing()
        self.cancel.setEnabled(False)
        self.status.setText("Canceling training; waiting for the current checkpoint…")

    def _cancelled(self, operation):
        if operation == "train_interference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText("Training canceled. REF unchanged; any started artifact is retained as aborted.")

    def reject(self):
        self._closing = True
        self._set_busy(self._busy)
        self.status.setText("Closing training worker…")
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
