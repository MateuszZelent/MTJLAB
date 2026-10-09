"""Fluent, read-only sample estimates for the editable Rigol carrier form."""

from collections.abc import Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QGridLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, StrongBodyLabel

from app.devices.rigol_dg1000z.load_estimate import SampleLoadEstimate, estimate_sample_load
from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_RESISTANCE,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
    parse_quantity,
)
from app.ui.common import line_edit


class SampleLoadEstimateWidget(QWidget):
    inputs_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rigolSampleLoadEstimate")
        self.estimate: SampleLoadEstimate | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel("Estimated sample current and voltage", self))
        intro = CaptionLabel(
            "Prediction for the current form with output ON. Enter a measured resistance; "
            "optionally add its expected minimum and maximum.", self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.resistance = line_edit("")
        self.minimum = line_edit("")
        self.maximum = line_edit("")
        for label, field, placeholder in (
            ("Measured resistance", self.resistance, "e.g. 175 ohm"),
            ("Minimum resistance (optional)", self.minimum, "e.g. 150 ohm"),
            ("Maximum resistance (optional)", self.maximum, "e.g. 200 ohm"),
        ):
            field.setMinimumWidth(0)
            field.setPlaceholderText(placeholder)
            field.setAccessibleName(label)
            field.textChanged.connect(lambda _text: self.inputs_changed.emit())
            form.addRow(BodyLabel(label, self), field)
        layout.addLayout(form)
        self.status = CaptionLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.results = QWidget(self)
        grid = QGridLayout(self.results)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        self.nominal_heading = CaptionLabel("At measured R", self.results)
        self.range_heading = CaptionLabel("Across R range", self.results)
        for col, heading in enumerate((self.nominal_heading, self.range_heading), start=1):
            heading.setWordWrap(True)
            grid.addWidget(heading, 0, col)
            grid.setColumnStretch(col, 1)
        self.values: dict[tuple[int, int], BodyLabel] = {}
        for row, title in enumerate((
            "Current range", "Peak |current|", "Sample voltage range", "Peak |voltage|",
        ), start=1):
            label = CaptionLabel(title, self.results)
            label.setWordWrap(True)
            grid.addWidget(label, row, 0)
            for col in (1, 2):
                value = BodyLabel("—", self.results)
                value.setMinimumWidth(0)
                value.setWordWrap(True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(value, row, col)
                self.values[row, col] = value
        layout.addWidget(self.results)
        note = CaptionLabel(
            "Model: direct resistive load + Rigol's internal 50 Ω. "
            "I = Vopen / (R + 50 Ω); Vsample = I × R. "
            "HIGHZ uses the displayed voltage as Vopen; LOAD 50 Ω doubles it. "
            "AC estimates ignore reactance and transients. Extra circuitry and nonlinear "
            "sample resistance can change the result. This is not a measurement or a current limit.", self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.clear("Enter measured resistance with a unit, e.g. 175 ohm.")

    def input_defaults(self) -> dict[str, str]:
        return {
            "estimate_resistance": self.resistance.text().strip(),
            "estimate_resistance_min": self.minimum.text().strip(),
            "estimate_resistance_max": self.maximum.text().strip(),
        }

    def load_defaults(self, defaults: Mapping[str, object]) -> None:
        for key, field in (
            ("estimate_resistance", self.resistance),
            ("estimate_resistance_min", self.minimum),
            ("estimate_resistance_max", self.maximum),
        ):
            field.setText(str(defaults.get(key, "")))
        self.inputs_changed.emit()

    def clear(self, reason: str) -> None:
        self.estimate = None
        self.status.setText(reason)
        for label in self.values.values():
            label.setText("—")

    def update_estimate(self, *, high_v: float, low_v: float, output_load: str, waveform: str) -> None:
        if not self.resistance.text().strip():
            self.clear("Enter measured resistance with a unit, e.g. 175 ohm.")
            return
        try:
            resistance = parse_quantity(self.resistance.text(), DIMENSION_RESISTANCE).si_value
            lower, upper = self.minimum.text().strip(), self.maximum.text().strip()
            if bool(lower) != bool(upper):
                raise ValueError("Enter both resistance bounds, or leave both empty.")
            minimum = parse_quantity(lower, DIMENSION_RESISTANCE).si_value if lower else resistance
            maximum = parse_quantity(upper, DIMENSION_RESISTANCE).si_value if upper else resistance
            estimate = estimate_sample_load(
                high_v=high_v, low_v=low_v, output_load=output_load, waveform=waveform,
                resistance_ohm=resistance, minimum_ohm=minimum, maximum_ohm=maximum,
            )
        except (ValueError, SafetyViolation) as exc:
            self.clear(str(exc))
            return
        self.estimate = estimate
        self.status.setText(
            f"{waveform} · LOAD {output_load} · "
            "Cycle extrema, including offset; peak means maximum absolute value."
        )
        self.nominal_heading.setText(f"At {self._format(resistance, DIMENSION_RESISTANCE)}")
        self.range_heading.setText(
            self._range(minimum, maximum, DIMENSION_RESISTANCE) if lower else "Same R (no range entered)"
        )
        for col, envelope in enumerate((estimate.nominal, estimate.resistance_range), start=1):
            texts = (
                self._range(envelope.current_min_a, envelope.current_max_a, DIMENSION_CURRENT),
                self._format(envelope.peak_current_a, DIMENSION_CURRENT),
                self._range(envelope.voltage_min_v, envelope.voltage_max_v, DIMENSION_VOLTAGE),
                self._format(envelope.peak_voltage_v, DIMENSION_VOLTAGE),
            )
            for row, text in enumerate(texts, start=1):
                self.values[row, col].setText(text)

    @staticmethod
    def _format(value: float, dimension: str) -> str:
        return format_quantity_auto(value, dimension, precision=4)

    @classmethod
    def _range(cls, low: float, high: float, dimension: str) -> str:
        if low == high:
            return cls._format(low, dimension)
        return f"{cls._format(low, dimension)} … {cls._format(high, dimension)}"
