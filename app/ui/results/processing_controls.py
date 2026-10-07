"""Fluent post-acquisition controls for both result visualizations."""

from dataclasses import replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, CheckBox, ComboBox, FlowLayout, PushButton

from app.recipes.spectrum_processing import REFERENCE_OPERATIONS
from app.ui.results.processing import ResultProcessing


class ResultProcessingControls(CardWidget):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("resultProcessingControls")
        self._state = ResultProcessing()
        self._reference_purposes = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        strip = QWidget(self)
        flow = FlowLayout(strip, needAni=False)
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setHorizontalSpacing(8)
        flow.setVerticalSpacing(6)
        flow.addWidget(CaptionLabel("Post-processing", strip))
        self.operation = ComboBox(strip)
        self.operation.setAccessibleName("Post-acquisition reference or background operation")
        for label, key in REFERENCE_OPERATIONS:
            if key == "subtract_power_signed":
                label = "Raw − background — signed W"
            self.operation.addItem(label, userData=key)
            if key == "subtract_power_signed":
                self.operation.addItem(
                    "Raw − reference — signed W", userData="subtract_reference_signed"
                )
        self.operation.setFixedWidth(270)
        flow.addWidget(self.operation)
        self.reference = ComboBox(strip)
        self.reference.setAccessibleName("Recorded reference or background")
        self.reference.setFixedWidth(210)
        self.reference.addItem("Automatic baseline", userData=None)
        self.reference.setToolTip(
            "Use the checkpoint's linked reference, or the only recorded background/reference matching the operation. Select explicitly when several baselines are recorded."
        )
        flow.addWidget(self.reference)
        self.filters = {}
        for key, label in (
            ("narrow_reject", "Narrow peaks"),
            ("emi_reject", "EMI lines"),
            ("denoise", "Denoise"),
        ):
            box = CheckBox(label, strip)
            box.toggled.connect(self._changed)
            flow.addWidget(box)
            self.filters[key] = box
        self.settings = PushButton("Filter settings…", strip)
        self.settings.clicked.connect(self._settings)
        flow.addWidget(self.settings)
        self.reset = PushButton("Reset", strip)
        self.reset.clicked.connect(lambda: self.set_state(ResultProcessing(), emit=True))
        flow.addWidget(self.reset)
        root.addWidget(strip)
        self.summary = CaptionLabel(
            "Read-only preview · full recorded grids · same-point sweeps only", self
        )
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.operation.currentIndexChanged.connect(self._changed)
        self.reference.currentIndexChanged.connect(self._changed)

    @property
    def state(self):
        return self._state

    def set_references(self, references):
        references = tuple(references)
        self._reference_purposes = {reference.index: reference.purpose for reference in references}
        self.reference.blockSignals(True)
        self.reference.clear()
        self.reference.addItem("Automatic baseline", userData=None)
        for reference in references:
            self.reference.addItem(
                f"{reference.purpose.title()} {reference.index} · {reference.average_count} sweeps",
                userData=reference.index,
            )
        self.reference.blockSignals(False)
        self.set_state(ResultProcessing())

    def set_state(self, state, *, emit=False):
        self._state = state
        controls = (self.operation, self.reference, *self.filters.values())
        for control in controls:
            control.blockSignals(True)
        self.operation.setCurrentIndex(self.operation.findData(state.operation))
        index = self.reference.findData(state.reference_index)
        self.reference.setCurrentIndex(max(0, index))
        for key, box in self.filters.items():
            box.setChecked(key in state.modes)
        for control in controls:
            control.blockSignals(False)
        self.operation.setToolTip(self.operation.currentText())
        self.summary.setText(
            f"Preview - source data unchanged - output: {state.output_unit} - "
            f"power average: {state.parameters.temporal_average_frames} same-point sweeps"
        )
        self.reference.setEnabled(state.operation != "none")
        self.reference.setItemText(0, f"Automatic {state.baseline_purpose}")
        self.reset.setEnabled(state.active)
        self.settings.setToolTip(
            f"Power average: {state.parameters.temporal_average_frames} same-point sweeps; edit thresholds and protected signal bands"
        )
        if emit:
            self.changed.emit(state)

    def _changed(self, *_args):
        operation = self.operation.currentData() or "none"
        reference_index = self.reference.currentData()
        if operation != self._state.operation and operation in {
            "subtract_power_signed", "subtract_reference_signed"
        }:
            purpose = "background" if operation == "subtract_power_signed" else "reference"
            if self._reference_purposes.get(reference_index) != purpose:
                # Switching correction purpose must not retain the other baseline.
                reference_index = None
        self.set_state(
            replace(
                self._state,
                operation=operation,
                reference_index=reference_index,
                modes=tuple(key for key, box in self.filters.items() if box.isChecked()),
            ),
            emit=True,
        )

    def _settings(self):
        from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import (
            SpectrumAnalysisSettingsDialog,
        )

        dialog = SpectrumAnalysisSettingsDialog(
            self,
            current_parameters=self._state.parameters,
            section="filters",
            allow_temporal_average=True,
            source_unit=self._state.output_unit,
        )
        dialog.parameters_applied.connect(
            lambda parameters: self.set_state(
                replace(self._state, parameters=parameters), emit=True
            )
        )
        dialog.exec()
        dialog.deleteLater()
