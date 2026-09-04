"""Unit-aware Fluent slider used by the floating Quick Controls window."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, LineEdit, Slider, StrongBodyLabel

from app.domain.quick_controls import (
    _QUANTITY,
    quantity_step_si,
    render_quantity_si_like,
)
from app.domain.quantities import format_quantity_auto, parse_quantity
from app.recipes.parameter_registry import QuickControlDescriptor
from app.safety.quick_controls import QuickControlSafetyBound


_MAX_SLIDER_POSITIONS = 2_000_000_000


@dataclass(frozen=True, slots=True)
class QuantitySliderMapping:
    """Map a bounded SI quantity onto an integer Fluent slider."""

    minimum_si: float
    maximum_si: float
    step_si: float
    logarithmic: bool = False

    def __post_init__(self) -> None:
        values = (self.minimum_si, self.maximum_si, self.step_si)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Slider bounds and step must be finite.")
        if self.minimum_si >= self.maximum_si:
            raise ValueError("Slider minimum must be smaller than maximum.")
        if self.step_si <= 0:
            raise ValueError("Slider step must be positive.")
        if self.logarithmic and self.minimum_si <= 0:
            raise ValueError("Logarithmic slider bounds must be positive.")

    @property
    def exact_step_count(self) -> int:
        return max(
            1,
            int(math.ceil((self.maximum_si - self.minimum_si) / self.step_si)),
        )

    @property
    def maximum_position(self) -> int:
        return min(self.exact_step_count, _MAX_SLIDER_POSITIONS)

    @property
    def uses_exact_steps(self) -> bool:
        return self.exact_step_count <= _MAX_SLIDER_POSITIONS

    def value_for_position(self, position: int) -> float:
        position = max(0, min(self.maximum_position, int(position)))
        if position == 0:
            return self.minimum_si
        if position == self.maximum_position:
            return self.maximum_si
        if self.uses_exact_steps:
            raw = self.minimum_si + position * self.step_si
        else:
            fraction = position / self.maximum_position
            if self.logarithmic:
                lower = math.log10(self.minimum_si)
                upper = math.log10(self.maximum_si)
                raw = 10 ** (lower + fraction * (upper - lower))
            else:
                raw = self.minimum_si + fraction * (
                    self.maximum_si - self.minimum_si
                )
        return self._quantize_and_clamp(raw)

    def position_for_value(self, value_si: float) -> int:
        value_si = min(max(float(value_si), self.minimum_si), self.maximum_si)
        if self.uses_exact_steps:
            position = int(
                (
                    (Decimal(str(value_si)) - Decimal(str(self.minimum_si)))
                    / Decimal(str(self.step_si))
                ).to_integral_value(rounding=ROUND_HALF_UP)
            )
            return max(0, min(self.maximum_position, position))
        if self.logarithmic:
            lower = math.log10(self.minimum_si)
            upper = math.log10(self.maximum_si)
            fraction = (math.log10(value_si) - lower) / (upper - lower)
        else:
            fraction = (value_si - self.minimum_si) / (
                self.maximum_si - self.minimum_si
            )
        return max(
            0,
            min(self.maximum_position, round(fraction * self.maximum_position)),
        )

    def _quantize_and_clamp(self, value_si: float) -> float:
        minimum = Decimal(str(self.minimum_si))
        maximum = Decimal(str(self.maximum_si))
        step = Decimal(str(self.step_si))
        value = Decimal(str(value_si))
        steps = int(
            ((value - minimum) / step + Decimal("0.5")).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        quantized = minimum + steps * step
        return float(min(max(quantized, minimum), maximum))


class QuickQuantitySlider(QWidget):
    """Fluent slider/editor pair that preserves explicit-unit precision."""

    draft_value_changed = Signal(str, str)
    commit_requested = Signal(str, str)

    def __init__(
        self,
        *,
        target: str,
        descriptor: QuickControlDescriptor,
        editor: LineEdit | None = None,
        show_title: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.target = target
        self.descriptor = descriptor
        self._bound: QuickControlSafetyBound | None = None
        self._mapping: QuantitySliderMapping | None = None
        self._step_si = 1.0
        self._syncing = False
        self.last_committed_text = descriptor.default_text

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        value_row = QHBoxLayout()
        value_row.setSpacing(8)
        self.title_label = StrongBodyLabel(descriptor.label, self)
        self.title_label.setMinimumWidth(0)
        self.title_label.setVisible(show_title)
        value_row.addWidget(self.title_label, 1)
        self.value = editor or LineEdit(self)
        if editor is None:
            self.value.setText(descriptor.default_text)
        self.value.setAccessibleName(f"{descriptor.label} value")
        self.value.setMinimumWidth(0)
        value_row.addWidget(self.value)
        layout.addLayout(value_row)

        self.slider = Slider(self)
        self.slider.setOrientation(Qt.Orientation.Horizontal)
        self.slider.setAccessibleName(f"{descriptor.label} slider")
        self.slider.setToolTip(
            "Drag to change the draft. The card and Quick Controls stay synchronized."
        )
        layout.addWidget(self.slider)

        bounds_row = QHBoxLayout()
        bounds_row.setSpacing(12)
        self.minimum_label = CaptionLabel("MIN  —", self)
        self.maximum_label = CaptionLabel("MAX  —", self)
        self.minimum_label.setObjectName("quickControlLimits")
        self.maximum_label.setObjectName("quickControlLimits")
        bounds_row.addWidget(self.minimum_label)
        bounds_row.addStretch(1)
        bounds_row.addWidget(self.maximum_label)
        layout.addLayout(bounds_row)

        self.value.textChanged.connect(self._text_changed)
        self.value.editingFinished.connect(self._commit)
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.sliderReleased.connect(self._commit)
        self.set_value_text(descriptor.default_text)

    @property
    def step_si(self) -> float:
        return self._step_si

    def set_bounds(self, bound: QuickControlSafetyBound) -> None:
        self._bound = bound
        self.minimum_label.setText(f"MIN  {bound.minimum_text}")
        self.maximum_label.setText(f"MAX  {bound.maximum_text}")
        self._rebuild_mapping()
        if self._mapping is not None:
            try:
                value_si = self.value_si()
            except ValueError:
                return
            self._syncing = True
            try:
                self.slider.setValue(self._mapping.position_for_value(value_si))
            finally:
                self._syncing = False

    def clear_bounds(self) -> None:
        self._bound = None
        self._mapping = None
        self.minimum_label.setText("MIN  —")
        self.maximum_label.setText("MAX  —")
        self.slider.setEnabled(False)

    def set_value_text(self, text: str) -> None:
        try:
            parsed = parse_quantity(text, self.descriptor.dimension)
            step_si = quantity_step_si(text, self.descriptor.dimension)
        except ValueError:
            self.value.setText(text)
            self._mapping = None
            self.slider.setEnabled(False)
            return
        self._step_si = step_si
        self._syncing = True
        try:
            self.value.setText(text)
            self._rebuild_mapping()
            if self._mapping is not None:
                self.slider.setValue(self._mapping.position_for_value(parsed.si_value))
        finally:
            self._syncing = False

    def set_value_si(self, value_si: float) -> None:
        preferred_unit: str | None = None
        match = _QUANTITY.fullmatch(self.value.text())
        if match is not None:
            preferred_unit = match.group("unit").strip()
        if not preferred_unit:
            default_match = _QUANTITY.fullmatch(self.descriptor.default_text)
            if default_match is not None:
                preferred_unit = default_match.group("unit").strip()
        try:
            text = render_quantity_si_like(
                self.value.text(),
                self.descriptor.dimension,
                value_si,
                preferred_unit=preferred_unit,
            )
        except ValueError:
            text = format_quantity_auto(
                value_si,
                self.descriptor.dimension,
                preferred_unit=preferred_unit,
            )
        self.set_value_text(text)

    def value_si(self) -> float:
        return parse_quantity(self.value.text(), self.descriptor.dimension).si_value

    def _text_changed(self, text: str) -> None:
        if self._syncing:
            return
        try:
            parsed = parse_quantity(text, self.descriptor.dimension)
            self._step_si = quantity_step_si(text, self.descriptor.dimension)
        except ValueError:
            self.slider.setEnabled(False)
            return
        self._rebuild_mapping()
        if self._mapping is not None:
            self._syncing = True
            try:
                self.slider.setValue(self._mapping.position_for_value(parsed.si_value))
            finally:
                self._syncing = False
        self.draft_value_changed.emit(self.target, text)

    def _rebuild_mapping(self) -> None:
        bound = self._bound
        try:
            logarithmic = (
                self.descriptor.dimension == "frequency"
                and bound is not None
                and bound.minimum_si > 0
            )
            self._mapping = (
                None
                if bound is None
                else QuantitySliderMapping(
                    bound.minimum_si,
                    bound.maximum_si,
                    self._step_si,
                    logarithmic=logarithmic,
                )
            )
        except ValueError:
            self._mapping = None
        if self._mapping is None:
            self.slider.setEnabled(False)
            return
        self.slider.setEnabled(True)
        self.slider.setRange(0, self._mapping.maximum_position)

    def _slider_changed(self, position: int) -> None:
        if self._syncing or self._mapping is None:
            return
        value_si = self._mapping.value_for_position(position)
        try:
            text = render_quantity_si_like(
                self.value.text(), self.descriptor.dimension, value_si
            )
        except ValueError:
            return
        self._syncing = True
        try:
            self.value.setText(text)
        finally:
            self._syncing = False
        self.draft_value_changed.emit(self.target, text)

    def _commit(self) -> None:
        try:
            parse_quantity(self.value.text(), self.descriptor.dimension)
        except ValueError:
            return
        self.last_committed_text = self.value.text()
        self.commit_requested.emit(self.target, self.value.text())
