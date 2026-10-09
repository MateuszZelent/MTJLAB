"""Visible, opt-in controls for differential heatmap analysis."""

from dataclasses import replace

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (CaptionLabel, CardWidget, CheckBox, ComboBox,
                           DoubleSpinBox, FlowLayout, LineEdit, PrimaryPushButton, PushButton)

from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto, parse_quantity
from app.ui.dialogs import StationDialog
from .filter_choices import FilterComboBox
from .map_processing import MapProcessing


class MapSettingsDialog(StationDialog):
    def __init__(self, parent, state):
        super().__init__(parent)
        self.setWindowTitle("Heatmap common component and stationary lines")
        self.set_resizable(True)
        self.resize(760, 680)
        root = self.modal_content_layout(spacing=10)
        note = CaptionLabel("Classify narrow lines on recorded raw dBm, then mask the corresponding bins in the current map. No smoothing across the sweep parameter; masked bins remain gaps.", self)
        note.setWordWrap(True)
        root.addWidget(note)
        form = QFormLayout()
        self.numbers = {}
        for key, label, lo, hi, value, suffix in (
            ("quantile", "Lower quantile", 0.01, 0.49, state.quantile, ""),
            ("minimum_coverage", "Minimum readable coverage", 50, 100, state.minimum_coverage * 100, " %"),
            ("line_contrast_db", "Minimum line contrast above local median", 0.1, 100, state.line_contrast_db, " dB"),
            ("line_mad_db", "Maximum sweep MAD of line amplitude", 0.01, 10, state.line_mad_db, " dB"),
            ("line_deviation_db", "Maximum deviation for a stable line sample", 0.1, 20, state.line_deviation_db, " dB"),
            ("line_stability_fraction", "Minimum fraction of stable line samples", 50, 100, state.line_stability_fraction * 100, " %"),
        ):
            spin = DoubleSpinBox(self)
            spin.setRange(lo, hi)
            spin.setDecimals(2)
            spin.setValue(value)
            spin.setSuffix(suffix)
            spin.setAccessibleName(label)
            form.addRow(label, spin)
            self.numbers[key] = spin
        self.widths = {}
        for key, label in (("line_max_width_hz", "Maximum full line-lobe width"),
                           ("line_neighbourhood_hz", "Local frequency neighbourhood width")):
            edit = LineEdit(self)
            edit.setText(format_quantity_auto(getattr(state, key), DIMENSION_FREQUENCY))
            edit.setAccessibleName(label)
            form.addRow(label, edit)
            self.widths[key] = edit
        self.bands = LineEdit(self)
        self.bands.setText("; ".join(f"{format_quantity_auto(lo, DIMENSION_FREQUENCY)} .. {format_quantity_auto(hi, DIMENSION_FREQUENCY)}"
                                    for lo, hi in state.protected_bands_hz))
        self.bands.setPlaceholderText("600 MHz .. 800 MHz; 1 GHz .. 1.2 GHz")
        self.bands.setToolTip("Protected bands prevent stationary-line masking. Common-component subtraction still acts on the entire selected slice.")
        form.addRow("Protect signal bands from line masking", self.bands)
        root.addLayout(form)
        self.error = CaptionLabel("", self)
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        root.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        save = PrimaryPushButton("Apply map settings", self)
        save.clicked.connect(self._save)
        footer.addWidget(save)
        root.addLayout(footer)
        self.state = state

    def _save(self):
        try:
            bands = []
            for text in filter(str.strip, self.bands.text().split(";")):
                bounds = text.split("..")
                if len(bounds) != 2:
                    raise ValueError("Use '600 MHz .. 800 MHz' for each protected band.")
                bands.append(tuple(parse_quantity(v.strip(), DIMENSION_FREQUENCY).si_value for v in bounds))
            self.state = replace(self.state,
                **{key: control.value() / (100 if key in {"minimum_coverage", "line_stability_fraction"} else 1)
                   for key, control in self.numbers.items()},
                **{key: parse_quantity(control.text(), DIMENSION_FREQUENCY).si_value for key, control in self.widths.items()},
                protected_bands_hz=tuple(bands))
        except (ValueError, TypeError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class MapProcessingControls(CardWidget):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = MapProcessing()
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        self.strip = QWidget(self)
        self.flow = FlowLayout(self.strip, needAni=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(8)
        self.flow.setVerticalSpacing(6)
        self.flow.addWidget(CaptionLabel("Map analysis", self.strip))
        self.operation = ComboBox(self.strip)
        self.operation.setFixedWidth(260)
        for label, key in (("Off — preserve input", "none"), ("Subtract median component — W", "median_power"),
                           ("Subtract lower-quantile component — W", "quantile_power"),
                           ("Subtract selected coordinate — W", "reference_power"),
                           ("Contrast vs median — relative dB", "median_db")):
            self.operation.addItem(label, userData=key)
        self.operation.setAccessibleName("Heatmap common-component operation")
        self.operation.setToolTip("Use only this selected map slice. Median/quantile removal can suppress a physical signal that occupies most parameter values.")
        self.flow.addWidget(self.operation)
        self.reference = FilterComboBox(self.strip)
        self.reference.setMinimumWidth(150)
        self.reference.setMaximumWidth(230)
        self.reference.setAccessibleName("Reference sweep coordinate for differential map")
        self.reference.set_options(("Lowest selected coordinate", None))
        self.flow.addWidget(self.reference)
        self.lines = CheckBox("Mask stationary narrow lines", self.strip)
        self.lines.setToolTip("Requires stable dBm amplitude, local contrast, full-lobe width, and readable coverage. Real stationary lines can also satisfy these criteria.")
        self.flow.addWidget(self.lines)
        self.view = ComboBox(self.strip)
        for label, key in (("Result", "result"), ("Input before map filters", "input"), ("Common component", "component")):
            self.view.addItem(label, userData=key)
        self.view.setAccessibleName("Heatmap input result or common component")
        self.flow.addWidget(self.view)
        self.colour = ComboBox(self.strip)
        for label, key in (("Full colour range", "full"), ("1–99% colour range", "robust"), ("Symmetric about zero", "symmetric")):
            self.colour.addItem(label, userData=key)
        self.colour.setToolTip("Changes colour limits only; never clips or rescales exported data.")
        self.flow.addWidget(self.colour)
        settings = PushButton("Map settings…", self.strip)
        settings.clicked.connect(self._settings)
        self.flow.addWidget(settings)
        self.reset = PushButton("Reset map", self.strip)
        self.reset.clicked.connect(lambda: self.set_state(MapProcessing(), emit=True))
        self.flow.addWidget(self.reset)
        root.addWidget(self.strip)
        self.note = CaptionLabel("Map filters are off. A stationary line can be real signal; compare Input / Common component before interpreting the residual.", self)
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        for control in (self.operation, self.reference, self.view, self.colour):
            control.currentIndexChanged.connect(self._changed)
        self.lines.toggled.connect(self._changed)
        self.set_state(self.state)

    def set_coordinates(self, values, formatter):
        current = self.state.reference_value
        self.reference.blockSignals(True)
        self.reference.set_options(("Lowest selected coordinate", None),
                                   tuple(float(value) for value in values), formatter)
        self.reference.setCurrentIndex(max(0, self.reference.findData(current)))
        self.reference.blockSignals(False)

    def set_state(self, state, *, emit=False):
        self.state = state
        controls = (self.operation, self.reference, self.lines, self.view, self.colour)
        for control in controls:
            control.blockSignals(True)
        self.operation.setCurrentIndex(self.operation.findData(state.component))
        self.reference.setCurrentIndex(max(0, self.reference.findData(state.reference_value)))
        self.lines.setChecked(state.mask_lines)
        self.view.setCurrentIndex(self.view.findData(state.view))
        self.colour.setCurrentIndex(self.colour.findData(state.colour_range))
        for control in controls:
            control.blockSignals(False)
        self.reference.setEnabled(state.component == "reference_power")
        self.view.setItemEnabled(2, state.component != "none")
        self.reset.setEnabled(state.active)
        self.note.setText(
            f"Common component: {state.component} · coverage ≥ {state.minimum_coverage:.0%} · "
            f"lines: contrast ≥ {state.line_contrast_db:g} dB, MAD ≤ {state.line_mad_db:g} dB, "
            f"{state.line_stability_fraction:.0%} within ±{state.line_deviation_db:g} dB, "
            f"width ≤ {format_quantity_auto(state.line_max_width_hz, DIMENSION_FREQUENCY)} · "
            f"{len(state.protected_bands_hz)} protected band(s). Original HDF5 unchanged."
            if state.active else "Map filters are off. A stationary line can be real signal; compare Input / Common component before interpreting the residual.")
        if emit:
            self.changed.emit(state)

    def _changed(self, *_args):
        component = self.operation.currentData()
        view = self.view.currentData()
        if component == "none" and view == "component":
            view = "result"
        self.set_state(replace(self.state, component=component, reference_value=self.reference.currentData(),
                               mask_lines=self.lines.isChecked(), view=view,
                               colour_range=self.colour.currentData()), emit=True)

    def _settings(self):
        dialog = MapSettingsDialog(self, self.state)
        if dialog.exec():
            self.set_state(dialog.state, emit=True)
        dialog.deleteLater()

    def _sync_height(self):
        self.strip.setMinimumHeight(self.flow.heightForWidth(max(200, self.width() - 20)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._sync_height)
