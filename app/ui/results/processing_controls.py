"""Fluent post-acquisition controls for both result visualizations."""

from dataclasses import replace

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, CheckBox, ComboBox, FlowLayout, PushButton

from app.recipes.spectrum_processing import REFERENCE_OPERATIONS
from app.ui.results.processing import ResultProcessing
from app.ui.results.baseline_controls import BaselineSampleCombo


class ResultProcessingControls(CardWidget):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("resultProcessingControls")
        self._state = ResultProcessing()
        self._reference_purposes = {}
        self._references = {}
        self._baseline_view = False
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        strip = QWidget(self)
        flow = FlowLayout(strip, needAni=False)
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setHorizontalSpacing(8)
        flow.setVerticalSpacing(6)
        self._strip = strip
        self._flow = flow
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
        self.reference_sample = BaselineSampleCombo(strip)
        self.reference_sample.setAccessibleName("Baseline mean or repeat used for post-processing")
        flow.addWidget(self.reference_sample)
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
        self.reference_sample.currentIndexChanged.connect(self._changed)

    @property
    def state(self):
        return self._state

    def set_references(self, references):
        references = tuple(references)
        self._reference_purposes = {reference.index: reference.purpose for reference in references}
        self._references = {reference.index: reference for reference in references}
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
        controls = (self.operation, self.reference, self.reference_sample, *self.filters.values())
        for control in controls:
            control.blockSignals(True)
        self.operation.setCurrentIndex(self.operation.findData(state.operation))
        index = self.reference.findData(state.reference_index)
        self.reference.setCurrentIndex(max(0, index))
        self.reference_sample.set_reference(self._references.get(state.reference_index), state.reference_sweep)
        for key, box in self.filters.items():
            box.setChecked(key in state.modes)
        for control in controls:
            control.blockSignals(False)
        self.operation.setToolTip(self.operation.currentText())
        self.summary.setText(
            f"Preview - source data unchanged - output: {state.output_unit} - "
            f"power average: {state.parameters.temporal_average_frames} same-point sweeps · "
            + (f"baseline: individual repeat {state.reference_sweep + 1}" if state.reference_sweep is not None
               else "baseline: stored mean")
        )
        self.operation.setEnabled(not self._baseline_view)
        self.reference.setEnabled(state.operation != "none" and not self._baseline_view)
        self.reference_sample.setEnabled(state.operation != "none" and not self._baseline_view
                                         and state.reference_index in self._references)
        if self._baseline_view:
            self.summary.setText("Standalone baseline spectrum · filters apply to its raw power. Checkpoint corrections are retained and resume when a measurement checkpoint is selected.")
        self.reference.setItemText(0, f"Automatic {state.baseline_purpose}")
        for i in range(1, self.reference.count()):
            purpose = self._reference_purposes.get(self.reference.itemData(i))
            self.reference.setItemEnabled(i, state.operation not in {
                "subtract_power_signed", "subtract_reference_signed"
            } or purpose == state.baseline_purpose)
        self.reset.setEnabled(state.active)
        self.settings.setToolTip(
            f"Power average: {state.parameters.temporal_average_frames} same-point sweeps; edit thresholds and protected signal bands"
        )
        if emit:
            self.changed.emit(state)

    def _sync_height(self):
        self._strip.setMinimumHeight(self._flow.heightForWidth(max(200, self.width() - 20)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._sync_height)

    def _changed(self, *_args):
        operation = self.operation.currentData() or "none"
        reference_index = self.reference.currentData()
        reference_sweep = self.reference_sample.currentData()
        if reference_index != self._state.reference_index:
            reference_sweep = None
        if operation != self._state.operation and operation in {
            "subtract_power_signed", "subtract_reference_signed"
        }:
            purpose = "background" if operation == "subtract_power_signed" else "reference"
            if self._reference_purposes.get(reference_index) != purpose:
                # Switching correction purpose must not retain the other baseline.
                reference_index = None
                reference_sweep = None
        self.set_state(
            replace(
                self._state,
                operation=operation,
                reference_index=reference_index,
                reference_sweep=reference_sweep,
                modes=tuple(key for key, box in self.filters.items() if box.isChecked()),
            ),
            emit=True,
        )

    def set_baseline_view(self, enabled):
        self._baseline_view = enabled
        self.set_state(self._state)

    def _settings(self):
        from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import (
            SpectrumAnalysisSettingsDialog,
        )

        dialog = SpectrumAnalysisSettingsDialog(
            self,
            current_parameters=self._state.parameters,
            section="filters",
            allow_temporal_average=not self._baseline_view,
            source_unit="dBm" if self._baseline_view else self._state.output_unit,
        )
        dialog.parameters_applied.connect(
            lambda parameters: self.set_state(
                replace(self._state, parameters=parameters), emit=True
            )
        )
        dialog.exec()
        dialog.deleteLater()
