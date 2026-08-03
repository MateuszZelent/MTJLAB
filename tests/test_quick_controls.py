from __future__ import annotations

from decimal import Decimal
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QApplication, QWidget

from app.domain.quick_controls import (
    QuickControlCommand,
    quantity_step_si,
    render_quantity_si_like,
    step_quantity_text,
)
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_FREQUENCY,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
    parse_quantity,
)
from app.recipes.parameter_registry import QUICK_CONTROLS_BY_TARGET
from app.ui.quick_controls import QuickControlCoordinator, QuickControlsWindow
from tests.helpers import simulation_settings


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
        self.assertEqual(
            render_quantity_si_like("9.999 mA", DIMENSION_CURRENT, 0.01),
            "10.000 mA",
        )

    def test_user_safety_limits_block_typed_values_for_both_devices(self) -> None:
        parent = QWidget()
        rigol = _FakeController()
        keithley = _FakeController()
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            {"rigol": rigol, "keithley": keithley},
            parent,
            settings=simulation_settings(approved=True),
        )

        rigol_max = coordinator._bounds["rigol.1.frequency"][1]
        keithley_max = coordinator._bounds["keithley.B.current"][1]
        coordinator.submit(
            "rigol.1.frequency",
            format_quantity_auto(rigol_max * 2, DIMENSION_FREQUENCY),
        )
        coordinator.submit(
            "keithley.B.current",
            format_quantity_auto(keithley_max * 2, DIMENSION_CURRENT),
        )

        self.assertEqual(rigol.calls, [])
        self.assertEqual(keithley.calls, [])

    def test_arrows_stop_at_every_user_limit_for_rigol_and_keithley(self) -> None:
        parent = QWidget()
        controllers = {"rigol": _FakeController(), "keithley": _FakeController()}
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            controllers,
            parent,
            settings=simulation_settings(approved=True),
        )
        window = QuickControlsWindow(coordinator, parent)
        window.set_targets(tuple(coordinator._bounds))
        try:
            for target, (minimum, maximum) in coordinator._bounds.items():
                dimension = QUICK_CONTROLS_BY_TARGET[target].dimension
                row = window._rows[target]
                minimum_text, maximum_text = coordinator._bound_texts[target]
                self.assertEqual(
                    row.limits.text(),
                    f"MIN  {minimum_text}    MAX  {maximum_text}",
                )
                for boundary, direction in ((minimum, -1), (maximum, 1)):
                    row.value.setText(format_quantity_auto(boundary, dimension))
                    row.step(direction, Decimal(1))
                    self.assertAlmostEqual(
                        parse_quantity(row.value.text(), dimension).si_value,
                        boundary,
                    )
                    self.assertEqual(
                        controllers[target.split(".")[0]].calls, []
                    )
        finally:
            window.close()

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
        self.assertEqual(
            rigol.calls[0][1],
            QuickControlCommand("rigol.1.frequency", "1.000 kHz"),
        )
        rigol.result.emit("quick_setpoint", 1000.0)
        self.assertEqual(len(rigol.calls), 2)
        self.assertEqual(
            rigol.calls[1][1],
            QuickControlCommand("rigol.1.frequency", "1.002 kHz"),
        )
        rigol.result.emit("quick_setpoint", 1002.0)
        self.assertTrue(any(state == "applied" for _target, state, _detail in states))

    def test_device_card_draft_is_available_to_quick_controls_state(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        changes: list[tuple[str, str, str]] = []
        coordinator.draft_changed.connect(
            lambda target, text, source: changes.append((target, text, source))
        )

        coordinator.publish_draft("rigol.1.frequency", "12 kHz")

        self.assertEqual(coordinator.draft_text("rigol.1.frequency"), "12 kHz")
        self.assertEqual(
            changes[-1], ("rigol.1.frequency", "12 kHz", "device_card")
        )

    def test_readback_does_not_overwrite_newer_device_card_draft(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        coordinator.publish_draft("rigol.1.frequency", "12 kHz")

        coordinator.confirmed_snapshot(
            "rigol.1.frequency", 10_000.0, adopt_draft=False
        )

        self.assertEqual(coordinator.draft_text("rigol.1.frequency"), "12 kHz")

    def test_confirmed_quick_setpoint_adopts_quantized_readback(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        coordinator.publish_draft(
            "rigol.1.frequency", "12 kHz", source="quick_controls"
        )

        coordinator.confirmed_snapshot(
            "rigol.1.frequency", 12_001.0, adopt_draft=True
        )

        self.assertAlmostEqual(
            parse_quantity(
                coordinator.draft_text("rigol.1.frequency"), DIMENSION_FREQUENCY
            ).si_value,
            12_001.0,
        )

    def test_rigol_level_readback_adopts_every_coupled_quick_draft(self) -> None:
        parent = QWidget()
        rigol = _FakeController()
        coordinator = QuickControlCoordinator(
            {"rigol": rigol, "keithley": _FakeController()}, parent
        )
        for target, text in {
            "rigol.1.amplitude": "0.200 V",
            "rigol.1.high_level": "100 mV",
            "rigol.1.low_level": "-100 mV",
            "rigol.1.offset": "0 V",
        }.items():
            coordinator.publish_draft(target, text, source="quick_controls")

        coordinator.submit("rigol.1.amplitude", "0.200 V")
        rigol.result.emit("quick_setpoint", 0.201)
        self.assertEqual(rigol.calls[-1][0], "quick_readback")
        rigol.result.emit(
            "quick_readback",
            {
                "rigol.1.amplitude": 0.201,
                "rigol.1.high_level": 0.1005,
                "rigol.1.low_level": -0.1005,
                "rigol.1.offset": 0.0,
            },
        )

        self.assertAlmostEqual(
            parse_quantity(
                coordinator.draft_text("rigol.1.high_level"), DIMENSION_VOLTAGE
            ).si_value,
            0.1005,
        )
        self.assertAlmostEqual(
            parse_quantity(
                coordinator.draft_text("rigol.1.amplitude"), DIMENSION_VOLTAGE
            ).si_value,
            0.201,
        )

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
            self.assertTrue(
                all(
                    row.slider.slider.isVisibleTo(window)
                    and row.slider.slider.width() > 0
                    and row.height() > 0
                    for row in window._rows.values()
                )
            )
            for target, row in window._rows.items():
                bound = coordinator.bound(target)
                if bound is not None:
                    self.assertEqual(row.slider.minimum_label.text(), f"MIN  {bound.minimum_text}")
                    self.assertEqual(row.slider.maximum_label.text(), f"MAX  {bound.maximum_text}")
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
