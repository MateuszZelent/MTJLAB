from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_FREQUENCY, parse_quantity
from app.safety.quick_controls import QuickControlSafetyBound
from app.ui.widgets.quick_quantity_slider import (
    QuantitySliderMapping,
    QuickQuantitySlider,
)
from app.recipes.parameter_registry import QUICK_CONTROLS_BY_TARGET


class QuickQuantitySliderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    @staticmethod
    def _bound(minimum: float, maximum: float, minimum_text: str, maximum_text: str) -> QuickControlSafetyBound:
        return QuickControlSafetyBound(minimum, maximum, minimum_text, maximum_text)

    def test_frequency_mapping_is_logarithmic_and_round_trips(self) -> None:
        mapping = QuantitySliderMapping(
            minimum_si=1.0,
            maximum_si=30_000_000.0,
            step_si=0.000001,
            logarithmic=True,
        )

        midpoint = mapping.value_for_position(mapping.maximum_position // 2)

        self.assertGreater(midpoint, 1_000.0)
        self.assertLess(midpoint, 1_000_000.0)
        self.assertAlmostEqual(
            mapping.position_for_value(midpoint),
            mapping.maximum_position // 2,
            delta=1,
        )

    def test_slider_step_preserves_written_decimal_precision(self) -> None:
        parent = QWidget()
        widget = QuickQuantitySlider(
            target="keithley.A.current",
            descriptor=QUICK_CONTROLS_BY_TARGET["keithley.A.current"],
            parent=parent,
        )
        widget.set_bounds(self._bound(-0.002, 0.002, "-2 mA", "2 mA"))
        widget.set_value_text("0.00100 A")
        starting_position = widget.slider.value()

        widget.slider.setValue(starting_position + 1)
        self.application.processEvents()

        self.assertAlmostEqual(widget.step_si, 0.00001)
        self.assertAlmostEqual(
            parse_quantity(widget.value.text(), DIMENSION_CURRENT).si_value,
            0.00101,
        )

    def test_frequency_text_step_uses_written_unit_and_precision(self) -> None:
        parent = QWidget()
        widget = QuickQuantitySlider(
            target="rigol.1.frequency",
            descriptor=QUICK_CONTROLS_BY_TARGET["rigol.1.frequency"],
            parent=parent,
        )
        widget.set_bounds(self._bound(1_000.0, 20_000.0, "1 kHz", "20 kHz"))
        widget.set_value_text("10.000 kHz")

        widget.slider.setValue(widget.slider.value() + 1)
        self.application.processEvents()

        self.assertAlmostEqual(widget.step_si, 1.0)
        self.assertAlmostEqual(
            parse_quantity(widget.value.text(), DIMENSION_FREQUENCY).si_value,
            10_001.0,
        )

    def test_slider_commit_emits_explicit_unit_text(self) -> None:
        parent = QWidget()
        widget = QuickQuantitySlider(
            target="keithley.A.current",
            descriptor=QUICK_CONTROLS_BY_TARGET["keithley.A.current"],
            parent=parent,
        )
        widget.set_bounds(self._bound(-0.001, 0.001, "-1 mA", "1 mA"))
        widget.set_value_text("0.00100 A")
        committed: list[tuple[str, str]] = []
        widget.commit_requested.connect(
            lambda target, text: committed.append((target, text))
        )

        widget.slider.sliderReleased.emit()

        self.assertEqual(committed[-1][0], "keithley.A.current")
        self.assertTrue(committed[-1][1].endswith("A"))


if __name__ == "__main__":
    unittest.main()
