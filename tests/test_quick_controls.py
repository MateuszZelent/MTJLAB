from __future__ import annotations

from decimal import Decimal
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QApplication, QWidget

from app.domain.quick_controls import quantity_step_si, step_quantity_text
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_FREQUENCY
from app.ui.quick_controls import QuickControlCoordinator, QuickControlsWindow


class _FakeController(QObject):
    result = Signal(str, object)
    error = Signal(str, str)
    state_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def call(self, operation: str, payload: object = None) -> None:
        self.calls.append((operation, payload))


class QuickControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        self._saved_settings = {
            key: (settings.contains(key), settings.value(key))
            for key in ("quick_controls/targets", "quick_controls/geometry")
        }

    def tearDown(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        for key, (existed, value) in self._saved_settings.items():
            if existed:
                settings.setValue(key, value)
            else:
                settings.remove(key)
        settings.sync()

    def test_written_precision_defines_arrow_step(self) -> None:
        self.assertAlmostEqual(quantity_step_si("0.01 A", DIMENSION_CURRENT), 0.01)
        self.assertAlmostEqual(quantity_step_si("0.010 A", DIMENSION_CURRENT), 0.001)
        self.assertAlmostEqual(quantity_step_si("10.0 mA", DIMENSION_CURRENT), 1e-4)
        self.assertAlmostEqual(quantity_step_si("1e-3 A", DIMENSION_CURRENT), 1e-3)
        self.assertAlmostEqual(quantity_step_si("1.00e-3 A", DIMENSION_CURRENT), 1e-5)
        text, value = step_quantity_text(
            "10.000 kHz", DIMENSION_FREQUENCY, 1
        )
        self.assertEqual(text, "10.001 kHz")
        self.assertAlmostEqual(value, 10_001.0)
        text, value = step_quantity_text(
            "0.010 A", DIMENSION_CURRENT, -1, multiplier=Decimal("0.1")
        )
        self.assertEqual(text, "0.0099 A")
        self.assertAlmostEqual(value, 0.0099)

    def test_coordinator_keeps_only_latest_pending_value_per_target(self) -> None:
        parent = QWidget()
        rigol = _FakeController()
        keithley = _FakeController()
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            {"rigol": rigol, "keithley": keithley}, parent
        )
        states: list[tuple[str, str, str]] = []
        coordinator.state_changed.connect(
            lambda target, state, detail: states.append((target, state, detail))
        )

        coordinator.submit("rigol.1.frequency", "1.000 kHz")
        coordinator.submit("rigol.1.frequency", "1.001 kHz")
        coordinator.submit("rigol.1.frequency", "1.002 kHz")

        self.assertEqual(len(rigol.calls), 1)
        self.assertEqual(rigol.calls[0][1], ("rigol.1.frequency", 1000.0))
        rigol.result.emit("quick_setpoint", 1000.0)
        self.assertEqual(len(rigol.calls), 2)
        self.assertEqual(rigol.calls[1][1], ("rigol.1.frequency", 1002.0))
        rigol.result.emit("quick_setpoint", 1002.0)
        self.assertTrue(any(state == "applied" for _target, state, _detail in states))

    def test_estop_cancels_pending_value_without_dispatching_it(self) -> None:
        parent = QWidget()
        rigol = _FakeController()
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            {"rigol": rigol, "keithley": _FakeController()}, parent
        )
        coordinator.submit("rigol.1.frequency", "1.000 kHz")
        coordinator.submit("rigol.1.frequency", "1.001 kHz")

        coordinator.cancel_all("E-STOP requested")
        rigol.result.emit("quick_setpoint", 1000.0)

        self.assertEqual(len(rigol.calls), 1)

    def test_floating_window_supports_two_keithley_and_three_rigol_controls(self) -> None:
        parent = QWidget()
        controllers = {"rigol": _FakeController(), "keithley": _FakeController()}
        coordinator = QuickControlCoordinator(controllers, parent)  # type: ignore[arg-type]
        window = QuickControlsWindow(coordinator, parent)
        targets = (
            "keithley.A.current",
            "keithley.B.voltage",
            "rigol.1.frequency",
            "rigol.1.amplitude",
            "rigol.1.offset",
        )
        try:
            window.set_targets(targets)
            window.resize(360, 420)
            window.show()
            self.application.processEvents()
            self.assertEqual(tuple(window._rows), targets)
            self.assertTrue(window.isVisible())
            self.assertTrue(
                bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            )
            self.assertTrue(all(row.value.isVisibleTo(window) for row in window._rows.values()))
            self.assertEqual(
                window.controls_scroll.horizontalScrollBar().maximum(), 0
            )
            coordinator.refresh()
            self.assertEqual(controllers["rigol"].calls[-1][0], "quick_readback")
            controllers["rigol"].result.emit(
                "quick_readback", {"rigol.1.frequency": 2_000.0}
            )
            self.assertIn("kHz", window._rows["rigol.1.frequency"].value.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
