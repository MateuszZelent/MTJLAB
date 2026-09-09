"""Operator-owned field list; hardware parameters still come from channel B."""

import re

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CheckBox, ComboBox, LineEdit, SpinBox
import numpy as np

from app.devices.keithley_2600.characterization.field_series import (
    FieldSeriesConfig,
    planned_field_ramp_step_counts,
)
from app.domain.quantities import format_quantity_auto, parse_quantity


_BARE_NUMBER = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?$")


def _parse_with_default_unit(text: str, unit: str) -> float:
    value = text.strip()
    if _BARE_NUMBER.fullmatch(value):
        value = f"{value} {unit}"
    return parse_quantity(value, "current").si_value


class FieldSeriesPanel(QWidget):
    validation_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._safe_ramp_default = "1 mA"
        self._safe_ramp_step_a = parse_quantity(self._safe_ramp_default, "current").si_value
        self._ramp_settle_s = 0.001
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.enabled_box = CheckBox("Field-line series (sample A · field line B)", self)
        layout.addWidget(self.enabled_box)
        self.controls = QWidget(self)
        form = QFormLayout(self.controls)
        form.setContentsMargins(0, 0, 0, 0)
        self.form = form
        self.input_mode = ComboBox(self.controls)
        self.input_mode.addItems(["Current list", "Current interval"])
        form.addRow("B sequence:", self.input_mode)
        self.currents = LineEdit(self.controls)
        self.currents.setText("0 mA")
        self.currents.setPlaceholderText("Example: -5, 0, 5  (values default to mA)")
        self.currents.setToolTip(
            "Comma followed by a space or a semicolon separates values. Bare numbers are mA; "
            "explicit current units such as uA, mA and A are also accepted."
        )
        form.addRow("B current list [mA]:", self.currents)
        self.interval_start = LineEdit(self.controls)
        self.interval_start.setPlaceholderText("e.g. -10  (mA)")
        self.interval_stop = LineEdit(self.controls)
        self.interval_stop.setPlaceholderText("e.g. 10  (mA)")
        self.interval_points = SpinBox(self.controls)
        self.interval_points.setRange(2, 100000)
        self.interval_points.setValue(5)
        self.interval_points.setToolTip("Number of values, including both endpoints; not number of transitions.")
        form.addRow("B start current [mA]:", self.interval_start)
        form.addRow("B stop current [mA]:", self.interval_stop)
        form.addRow("B number of points:", self.interval_points)
        self.input_mode.currentIndexChanged.connect(self._update_input_mode)
        self._update_input_mode()
        self.ramp_step = LineEdit(self.controls)
        self.ramp_step.setText(self._safe_ramp_default)
        self.ramp_step.setReadOnly(True)
        self.ramp_step.setPlaceholderText("Loaded automatically from the safe B limit")
        self.ramp_step.setToolTip(
            "Automatic maximum current change in one B update. It comes from Channel B's "
            "reviewed safety limit and cannot be overridden by this procedure."
        )
        form.addRow("B ramp step (automatic):", self.ramp_step)
        self.stabilization = LineEdit(self.controls)
        self.stabilization.setText("1 s")
        self.stabilization.setToolTip("Delay after Channel B reaches each target, before sweep A starts.")
        form.addRow("B stabilization:", self.stabilization)
        self.hold = LineEdit(self.controls)
        self.hold.setText("120 s")
        self.hold.setToolTip(
            "Safety timeout for the B ramp, stabilization and the complete A sweep at one B value."
        )
        form.addRow("Safety timeout per B value:", self.hold)
        self.continue_a = CheckBox("Continue next field target after A compliance", self.controls)
        self.continue_a.setChecked(True)
        form.addRow(self.continue_a)
        self.analysis_min = LineEdit(self.controls)
        self.analysis_min.setPlaceholderText("Optional current, with units")
        self.analysis_max = LineEdit(self.controls)
        self.analysis_max.setPlaceholderText("Optional current, with units")
        form.addRow("Fit window I min:", self.analysis_min)
        form.addRow("Fit window I max:", self.analysis_max)
        self.analysis_reference = SpinBox(self.controls)
        self.analysis_reference.setRange(0, 100000)
        self.analysis_reference.setSpecialValueText("No reference")
        form.addRow("Reference field item:", self.analysis_reference)
        self.analysis_min.textChanged.connect(self._sync_analysis_reference)
        self.analysis_max.textChanged.connect(self._sync_analysis_reference)
        self.currents.textChanged.connect(self._sync_analysis_reference)
        self.interval_start.textChanged.connect(self._sync_analysis_reference)
        self.interval_stop.textChanged.connect(self._sync_analysis_reference)
        self.interval_points.valueChanged.connect(self._sync_analysis_reference)
        self.input_mode.currentIndexChanged.connect(self._sync_analysis_reference)
        self._sync_analysis_reference()
        self.bounds = CaptionLabel(self.controls)
        self.bounds.setWordWrap(True)
        form.addRow(self.bounds)
        self.ramp_summary = CaptionLabel(self.controls)
        self.ramp_summary.setWordWrap(True)
        self.ramp_summary.setText("Ramp timing will appear after validation.")
        form.addRow("Ramp estimate:", self.ramp_summary)
        self.validation_status = CaptionLabel(self.controls)
        self.validation_status.setWordWrap(True)
        self.validation_status.setText("Reviewing field-series parameters…")
        form.addRow("Validation:", self.validation_status)
        layout.addWidget(self.controls)
        self.controls.hide()
        self.enabled_box.toggled.connect(self.controls.setVisible)
        for control in (*self.draft_text_controls().values(), self.interval_points,
                        self.analysis_reference):
            if hasattr(control, "textChanged"):
                control.textChanged.connect(lambda *_: self.validation_requested.emit())
            else:
                control.valueChanged.connect(lambda *_: self.validation_requested.emit())
        self.enabled_box.toggled.connect(lambda *_: self.validation_requested.emit())
        self.continue_a.toggled.connect(lambda *_: self.validation_requested.emit())
        self.input_mode.currentIndexChanged.connect(lambda *_: self.validation_requested.emit())

    def show_validation(self, valid: bool | None, message: str) -> None:
        self.validation_status.setText(message)
        color = "palette(placeholderText)" if valid is None else ("#059669" if valid else "#dc2626")
        self.validation_status.setStyleSheet(f"color: {color}; font-weight: 600;")

    def refresh_bounds(self, settings):
        limits = settings.keithley.safety.channels["B"].lab_limits
        self._safe_ramp_default = limits.ramp_current_step_max
        self._safe_ramp_step_a = parse_quantity(self._safe_ramp_default, "current").si_value
        self._ramp_settle_s = parse_quantity(
            limits.point_settle_time.min, "time"
        ).si_value
        self.ramp_step.setText(self._safe_ramp_default)
        self.bounds.setText(
            f"Channel B bounds: {limits.source_current.min} … {limits.source_current.max}; "
            f"automatic ramp step ≤ {limits.ramp_current_step_max}; automatic wait "
            f"{limits.point_settle_time.min} per update. B compliance, ranges, sense and NPLC "
            "are inherited from the normal B card. A sweep uses its own channel draft.")

    def show_ramp_summary(
        self, config: FieldSeriesConfig | None, *, max_points: int = 0
    ) -> None:
        if config is None:
            self.ramp_summary.setText("Ramp timing will appear after validation.")
            return
        updates = sum(planned_field_ramp_step_counts(config, max_points=max_points))
        programmed_s = updates * config.ramp_settle_s
        self.ramp_summary.setText(
            f"Step ≤ {format_quantity_auto(config.ramp_step_a, 'current', precision=6)}; "
            f"wait {format_quantity_auto(config.ramp_settle_s, 'time', precision=6)} per update; "
            f"planned path: {updates} B updates and at least "
            f"{format_quantity_auto(programmed_s, 'time', precision=6)}. Device communication "
            f"adds time. After every target: "
            f"{format_quantity_auto(config.stabilization_s, 'time', precision=6)} stabilization."
        )

    def draft_text_controls(self):
        return {name: getattr(self, name) for name in (
            "currents", "interval_start", "interval_stop", "ramp_step", "stabilization", "hold",
            "analysis_min", "analysis_max")}

    def draft_state(self):
        return {
            "text": {name: control.text() for name, control in self.draft_text_controls().items()},
            "enabled": self.enabled_box.isChecked(),
            "continue_a": self.continue_a.isChecked(),
            "input_mode": self.input_mode.currentIndex(),
            "interval_points": self.interval_points.value(),
            "analysis_reference": self.analysis_reference.value(),
        }

    def validate_draft_state(self, state):
        if not isinstance(state, dict) or not isinstance(state.get("text"), dict):
            raise ValueError("Invalid field draft")
        expected = set(self.draft_text_controls())
        legacy = expected - {"interval_start", "interval_stop"} | {"tolerance", "relative"}
        if set(state["text"]) not in (expected, legacy) or not all(
            isinstance(value, str) for value in state["text"].values()
        ):
            raise ValueError("Invalid field draft text")
        if any(type(state.get(key)) is not bool for key in ("enabled", "continue_a")):
            raise ValueError("Invalid field draft switches")
        for key, low, high in (("analysis_reference", 0, 100000),):
            if type(state.get(key)) is not int or not low <= state[key] <= high:
                raise ValueError("Invalid field draft count")
        if set(state["text"]) == expected:
            if type(state.get("input_mode")) is not int or state["input_mode"] not in (0, 1):
                raise ValueError("Invalid B sequence mode")
            if type(state.get("interval_points")) is not int or not 2 <= state["interval_points"] <= 100000:
                raise ValueError("Invalid B interval point count")

    def restore_draft_state(self, state):
        self.validate_draft_state(state)
        defaults = {"currents": "0 mA", "ramp_step": self._safe_ramp_default,
                    "stabilization": "1 s", "hold": "120 s"}
        for name, control in self.draft_text_controls().items():
            control.setText(state["text"].get(name, "") or defaults.get(name, ""))
        self.ramp_step.setText(self._safe_ramp_default)
        self.input_mode.setCurrentIndex(state.get("input_mode", 0))
        self.interval_points.setValue(state.get("interval_points", 5))
        self.analysis_reference.setValue(state["analysis_reference"])
        self.continue_a.setChecked(state["continue_a"])
        self.enabled_box.setChecked(state["enabled"])
        self._sync_analysis_reference()

    def build_config(self, sweep, source):
        window_text = (self.analysis_min.text().strip(), self.analysis_max.text().strip())
        if any(window_text) and not all(window_text):
            raise ValueError("Provide both analysis window endpoints, or leave both blank.")
        window = tuple(parse_quantity(value, "current").si_value for value in window_text) if all(window_text) else None
        return FieldSeriesConfig(
            sweep=sweep, field_source=source,
            currents_a=self.current_values(),
            ramp_step_a=self._safe_ramp_step_a,
            ramp_settle_s=self._ramp_settle_s,
            stabilization_s=parse_quantity(self.stabilization.text(), "time").si_value,
            stable_readings=1, current_tolerance_a=0.0, current_tolerance_relative=0.0,
            verify_current_stability=False,
            max_field_hold_s=parse_quantity(self.hold.text(), "time").si_value,
            continue_after_sample_compliance=self.continue_a.isChecked(),
            analysis_current_window_a=window,
            analysis_reference_index=(self.analysis_reference.value() - 1
                                      if window is not None and self.analysis_reference.value() else None),
        )

    def _sync_analysis_reference(self, *_args) -> None:
        has_window = bool(self.analysis_min.text().strip() and self.analysis_max.text().strip())
        self.analysis_reference.setEnabled(has_window)
        if not has_window:
            self.analysis_reference.setValue(0)
            return
        try:
            count = len(self.current_values())
        except ValueError:
            return
        old_value = self.analysis_reference.value()
        self.analysis_reference.setMaximum(max(0, count))
        if old_value > count:
            self.analysis_reference.setValue(0)

    def _update_input_mode(self, *_):
        interval = self.input_mode.currentIndex() == 1
        self.form.setRowVisible(self.currents, not interval)
        for control in (self.interval_start, self.interval_stop, self.interval_points):
            self.form.setRowVisible(control, interval)

    def current_values(self):
        if self.input_mode.currentIndex() == 1:
            start = _parse_with_default_unit(self.interval_start.text(), "mA")
            stop = _parse_with_default_unit(self.interval_stop.text(), "mA")
            return tuple(float(value) for value in np.linspace(start, stop, self.interval_points.value()))
        tokens = re.split(r";\s*|,\s+(?=[+-]?(?:\d|[.,]\d))", self.currents.text())
        if not all(token.strip() for token in tokens):
            raise ValueError("Enter a non-empty B current list separated by commas or semicolons.")
        return tuple(_parse_with_default_unit(token, "mA") for token in tokens)
