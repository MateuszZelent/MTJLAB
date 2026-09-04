from __future__ import annotations

from decimal import Decimal
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QSettings, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import FluentTitleBar, FluentWidget, ScrollArea

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
from app.ui.design_system.fluent_theme import apply_application_theme
from app.ui.design_system.tokens import tokens_for
from app.ui.quick_controls import (
    QUICK_OUTPUT_TARGETS,
    QuickControlCoordinator,
    QuickControlPicker,
    QuickControlsWindow,
)
from tests.helpers import simulation_settings


class _FakeController(QObject):
    result = Signal(str, object)
    error = Signal(str, str)
    state_changed = Signal(str)

    def __init__(self, state: str = "output_off") -> None:
        super().__init__()
        self.state_value = state
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
            for key in (
                "quick_controls/targets",
                "quick_controls/outputs",
                "quick_controls/geometry",
            )
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

    def test_disconnected_submit_updates_only_the_shared_draft(self) -> None:
        parent = QWidget()
        rigol = _FakeController("disconnected")
        keithley = _FakeController("disconnected")
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            {"rigol": rigol, "keithley": keithley}, parent
        )
        states: list[tuple[str, str, str]] = []
        coordinator.state_changed.connect(
            lambda target, state, detail: states.append((target, state, detail))
        )

        coordinator.submit("keithley.A.current", "1.00 mA")

        self.assertEqual(rigol.calls, [])
        self.assertEqual(keithley.calls, [])
        self.assertEqual(coordinator.draft_text("keithley.A.current"), "1.00 mA")
        self.assertEqual(states[-1][1], "draft")

    def test_output_off_uses_the_device_dry_run_builder(self) -> None:
        parent = QWidget()
        keithley = _FakeController("output_off")
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            {"rigol": _FakeController("disconnected"), "keithley": keithley},
            parent,
        )

        coordinator.set_hardware_request_builder(
            "keithley",
            lambda request, state: (
                "quick_configure",
                {"target": request.target, "state": state},
            ),
        )
        coordinator.submit("keithley.A.current", "1.00 mA")

        self.assertEqual(
            keithley.calls,
            [
                (
                    "quick_configure",
                    {"target": "keithley.A.current", "state": "output_off"},
                )
            ],
        )

        verified: list[tuple[str, object]] = []
        coordinator.configuration_verified.connect(
            lambda target, command: verified.append((target, command))
        )
        keithley.result.emit("quick_configure", 0.001)
        self.assertEqual(verified[0][0], "keithley.A.current")

    def test_output_on_keeps_the_live_quick_setpoint_path(self) -> None:
        parent = QWidget()
        keithley = _FakeController("output_on")
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            {"rigol": _FakeController("disconnected"), "keithley": keithley},
            parent,
        )

        coordinator.set_hardware_request_builder(
            "keithley",
            lambda request, _state: (
                "quick_setpoint",
                QuickControlCommand(request.target, request.text),
            ),
        )
        coordinator.submit("keithley.A.current", "1.00 mA")

        self.assertEqual(
            keithley.calls,
            [
                (
                    "quick_setpoint",
                    QuickControlCommand("keithley.A.current", "1.00 mA"),
                )
            ],
        )

    def test_compliance_state_allows_live_quick_setpoint_dispatch(self) -> None:
        parent = QWidget()
        keithley = _FakeController("compliance")
        coordinator = QuickControlCoordinator(  # type: ignore[arg-type]
            {"rigol": _FakeController("disconnected"), "keithley": keithley},
            parent,
        )

        coordinator.set_hardware_request_builder(
            "keithley",
            lambda request, _state: (
                "quick_setpoint",
                QuickControlCommand(request.target, request.text),
            ),
        )
        self.assertTrue(coordinator._device_can_apply("keithley"))
        coordinator.submit("keithley.B.current", "10.0 mA")

        self.assertEqual(
            keithley.calls,
            [
                (
                    "quick_setpoint",
                    QuickControlCommand("keithley.B.current", "10.0 mA"),
                )
            ],
        )

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
            self.assertEqual(window.controls_scroll.verticalScrollBar().value(), 0)
            coordinator.refresh()
            self.assertEqual(controllers["rigol"].calls[-1][0], "quick_readback")
            controllers["rigol"].result.emit(
                "quick_readback", {"rigol.1.frequency": 2_000.0}
            )
            self.assertIn("kHz", window._rows["rigol.1.frequency"].value.text())
        finally:
            window.close()

    def test_floating_window_is_fluent_and_exposes_independent_output_rows(self) -> None:
        parent = QWidget()
        controllers = {"rigol": _FakeController(), "keithley": _FakeController()}
        coordinator = QuickControlCoordinator(controllers, parent)  # type: ignore[arg-type]
        window = QuickControlsWindow(coordinator, parent)
        output_requests: list[tuple[str, str, bool]] = []
        group_requests: list[tuple[str, bool]] = []
        window.output_requested.connect(
            lambda device, channel, enabled: output_requests.append(
                (device, channel, enabled)
            )
        )
        window.output_group_requested.connect(
            lambda device, enabled: group_requests.append((device, enabled))
        )
        try:
            self.assertIsInstance(window, FluentWidget)
            window.show()
            self.application.processEvents()
            self.assertGreater(window.width(), 0)
            self.assertGreater(window.height(), 0)

            window._output_rows[("keithley", "A")].on_button.click()
            window._output_rows[("keithley", "B")].on_button.click()
            window._output_group_buttons["keithley"].click()

            self.assertEqual(
                output_requests,
                [("keithley", "A", True), ("keithley", "B", True)],
            )
            self.assertEqual(group_requests, [("keithley", True)])

            window.set_output_state("keithley", "A", "on")
            window.set_output_state("keithley", "B", "unknown")
            self.assertEqual(window._output_rows[("keithley", "A")].state.text(), "ON")
            self.assertEqual(
                window._output_rows[("keithley", "B")].state.text(), "UNKNOWN"
            )
        finally:
            window.close()

    def test_quick_controls_uses_shared_modal_shell_and_larger_default_size(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        window = QuickControlsWindow(coordinator, parent)
        try:
            self.assertEqual(window.size().toTuple(), (550, 720))
            self.assertIs(window.surface, window.modal_shell.surface)
            self.assertIs(window.backdrop, window.modal_shell.backdrop)
            self.assertIs(window.surface_layout, window.modal_shell.surface_layout)
        finally:
            window.close()

    def test_output_channels_are_configurable_from_choose_and_collapse_cleanly(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        window = QuickControlsWindow(coordinator, parent)
        try:
            self.assertEqual(window.output_targets(), QUICK_OUTPUT_TARGETS)
            window.set_output_targets(("output.keithley.B", "output.keithley.group"))
            self.assertEqual(
                window.output_targets(),
                ("output.keithley.B", "output.keithley.group"),
            )
            window.show()
            self.application.processEvents()
            self.assertFalse(window._output_rows[("rigol", "1")].isVisible())
            self.assertFalse(window._output_rows[("keithley", "A")].isVisible())
            self.assertTrue(window._output_rows[("keithley", "B")].isVisibleTo(window))
            self.assertTrue(window._output_group_containers["keithley"].isVisibleTo(window))
            self.assertLessEqual(window._output_rows[("keithley", "B")].on_button.width(), 42)
            self.assertLessEqual(window._output_rows[("keithley", "B")].off_button.width(), 42)
        finally:
            window.close()

    def test_choose_picker_exposes_output_visibility_choices(self) -> None:
        parent = QWidget()
        picker = QuickControlPicker(
            ("keithley.A.current",),
            ("output.keithley.A", "output.keithley.group"),
            parent,
        )
        self.assertTrue(picker.output_checkboxes["output.keithley.A"].isChecked())
        self.assertFalse(picker.output_checkboxes["output.keithley.B"].isChecked())
        self.assertTrue(picker.output_checkboxes["output.keithley.group"].isChecked())
        self.assertEqual(
            picker.selected_output_targets(),
            ("output.keithley.A", "output.keithley.group"),
        )
        picker.close()

    def test_choose_picker_uses_fluent_titlebar_and_modal_surface(self) -> None:
        parent = QWidget()
        picker = QuickControlPicker(("keithley.A.current",), parent=parent)
        try:
            parent.show()
            picker.resize(620, 560)
            picker.show()
            self.application.processEvents()

            self.assertTrue(
                bool(picker.windowFlags() & Qt.WindowType.FramelessWindowHint)
            )
            self.assertIsInstance(picker.titleBar, FluentTitleBar)
            self.assertTrue(picker.titleBar.closeBtn.isVisible())
            picker_scroll = picker.findChild(ScrollArea, "quickPickerScroll")
            self.assertIsNotNone(picker_scroll)
            assert picker_scroll is not None
            self.assertIs(picker_scroll.parentWidget(), picker.modal_shell.surface)
            self.assertTrue(picker.modal_shell.surface.isVisibleTo(picker))

            picker.titleBar.closeBtn.click()
            self.application.processEvents()
            self.assertFalse(picker.isVisible())
            self.assertTrue(parent.isVisible())
        finally:
            picker.close()
            parent.close()

    def test_panel_has_theme_contrast_and_expands_with_the_window(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        window = QuickControlsWindow(coordinator, parent)
        previous_theme = self.application.property("stationAppliedTheme")
        try:
            self.assertGreater(
                window.maximumSize().width(), window.minimumSize().width()
            )
            self.assertTrue(
                bool(window.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
            )
            window.show()
            self.application.processEvents()
            self.assertTrue(window.titleBar.maxBtn.isVisible())
            self.assertTrue(window.titleBar.closeBtn.isVisible())
            self.assertEqual(window.property("stationSurface"), "raised")
            self.assertEqual(window.backdrop.property("stationSurface"), "raised")
            self.assertEqual(window.surface.property("stationHover"), "disabled")
            self.assertEqual(window.backdrop.property("stationHover"), "disabled")

            for theme in ("light", "dark"):
                apply_application_theme(self.application, theme)
                window.show()
                self.application.processEvents()
                self.assertEqual(
                    window.backdrop.palette()
                    .color(QPalette.ColorRole.Window)
                    .name(),
                    tokens_for(theme).surface_raised,
                )

            initial_width = window.surface.width()
            window.resize(520, 680)
            self.application.processEvents()
            self.assertEqual(window._output_layout_mode, "wide")
            window.resize(420, 620)
            self.application.processEvents()
            self.assertEqual(window._output_layout_mode, "narrow")
            self.assertEqual(window.controls_scroll.horizontalScrollBar().maximum(), 0)
            window.resize(760, 820)
            self.application.processEvents()
            self.assertGreater(window.surface.width(), initial_width)
            self.assertGreater(window.controls_scroll.viewport().width(), 500)
        finally:
            window.close()
            if previous_theme in {"light", "dark"}:
                apply_application_theme(self.application, previous_theme)

    def test_quick_controls_close_button_closes_only_the_panel(self) -> None:
        parent = QWidget()
        parent.show()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        window = QuickControlsWindow(coordinator, parent)
        try:
            window.show()
            self.application.processEvents()
            window.titleBar.closeBtn.click()
            self.application.processEvents()
            self.assertFalse(window.isVisible())
            self.assertTrue(parent.isVisible())
        finally:
            window.close()
            parent.close()

    def test_quick_controls_auto_fit_height_and_caps_at_seventy_percent(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
            settings=simulation_settings(approved=True),
        )
        window = QuickControlsWindow(coordinator, parent)
        # Keep the rendering assertion deterministic on both offscreen CI and
        # a developer workstation with a different monitor resolution.
        window._screen_available_height = lambda: 1000  # type: ignore[method-assign]
        try:
            window.set_targets(())
            window.show()
            self.application.processEvents()
            empty_height = window.height()

            window.set_targets(("keithley.A.current",))
            self.application.processEvents()
            one_control_height = window.height()

            window.set_targets(tuple(coordinator._bounds))
            self.application.processEvents()
            many_controls_height = window.height()

            self.assertGreater(one_control_height, empty_height)
            self.assertGreater(many_controls_height, one_control_height)
            self.assertLessEqual(many_controls_height, 700)
            self.assertGreater(
                window.controls_content.sizeHint().height(),
                window.controls_scroll.viewport().height(),
            )

            # The cap belongs to automatic fitting; an explicit user resize
            # remains available so the existing resize handles stay useful.
            window.resize(window.width(), 760)
            self.application.processEvents()
            self.assertGreater(window.height(), 700)

            window.set_targets(())
            self.application.processEvents()
            self.assertLess(window.height(), 760)
        finally:
            window.close()

    def test_resize_handles_expose_cursor_zones_and_resize_the_frameless_window(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        window = QuickControlsWindow(coordinator, parent)
        try:
            window.show()
            self.application.processEvents()
            expected_cursors = {
                "left": Qt.CursorShape.SizeHorCursor,
                "right": Qt.CursorShape.SizeHorCursor,
                "top": Qt.CursorShape.SizeVerCursor,
                "bottom": Qt.CursorShape.SizeVerCursor,
                "top_left": Qt.CursorShape.SizeFDiagCursor,
                "bottom_right": Qt.CursorShape.SizeFDiagCursor,
                "top_right": Qt.CursorShape.SizeBDiagCursor,
                "bottom_left": Qt.CursorShape.SizeBDiagCursor,
            }
            self.assertEqual(set(window._resize_handles), set(expected_cursors))
            for name, cursor in expected_cursors.items():
                handle = window._resize_handles[name]
                self.assertTrue(handle.isVisible())
                self.assertEqual(handle.cursor().shape(), cursor)

            initial_width = window.width()
            window._resize_handles["right"]._resize_from_delta(QPoint(80, 0))
            self.assertGreaterEqual(window.width(), initial_width + 80)
        finally:
            window.close()

    def test_confirmed_snapshot_preserves_prefixed_units_at_zero(self) -> None:
        parent = QWidget()
        coordinator = QuickControlCoordinator(
            {"rigol": _FakeController(), "keithley": _FakeController()},
            parent,
        )
        target_current = "keithley.B.current"
        coordinator.publish_draft(target_current, "0 mA")
        coordinator.confirmed_snapshot(target_current, 0.0, adopt_draft=True)
        self.assertEqual(coordinator.draft_text(target_current), "0 mA")

        target_voltage = "rigol.1.offset"
        coordinator.publish_draft(target_voltage, "0 mV")
        coordinator.confirmed_snapshot(target_voltage, 0.0, adopt_draft=True)
        self.assertEqual(coordinator.draft_text(target_voltage), "0 mV")

    def test_keithley_arrow_stepping_preserves_unit_at_zero_and_on_clamp(self) -> None:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from app.devices.keithley_2600.ui.page import KeithleyConfigurationPanel
        from app.ui.common.precision_stepper import install_precision_arrow_stepper

        install_precision_arrow_stepper(self.application)
        panel = KeithleyConfigurationPanel(simulation_settings())
        panel.channel.setCurrentText("B")
        panel.mode.setCurrentText("current")
        panel.level.setText("10 mA")

        down = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        up = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)

        for _ in range(10):
            self.application.sendEvent(panel.level, down)
        self.assertEqual(panel.level.text(), "0 mA")

        # Stepping down past 0 mA hits the minimum limit (0 A / 0 mA) and must clamp to 0 mA, NOT 0 A
        self.application.sendEvent(panel.level, down)
        self.assertEqual(panel.level.text(), "0 mA")

        # Stepping up from 0 mA must step to 1 mA, NOT 1 A
        self.application.sendEvent(panel.level, up)
        self.assertEqual(panel.level.text(), "1 mA")

        # Step back down to 0 mA and verify editingFinished preserves 0 mA on focus loss
        self.application.sendEvent(panel.level, down)
        self.assertEqual(panel.level.text(), "0 mA")
        panel.level.editingFinished.emit()
        self.assertEqual(panel.level.text(), "0 mA")

    def test_render_quantity_si_like_cleans_binary_float_noise(self) -> None:
        # Binary float noise like 0.8 + 0.1 = 0.9000000000000001
        self.assertEqual(
            render_quantity_si_like("0.8 V", DIMENSION_VOLTAGE, 0.8 + 0.1),
            "0.9 V",
        )
        # 0.015 - 0.005 = 0.009999999999999998
        self.assertEqual(
            render_quantity_si_like("15 mV", DIMENSION_VOLTAGE, 0.015 - 0.005),
            "10 mV",
        )
        self.assertEqual(
            render_quantity_si_like("0.015 V", DIMENSION_VOLTAGE, 0.015 - 0.005),
            "0.010 V",
        )
        self.assertEqual(
            render_quantity_si_like("0.02 V", DIMENSION_VOLTAGE, 0.015 - 0.005),
            "0.01 V",
        )

    def test_format_quantity_auto_preserves_preferred_unit_at_zero(self) -> None:
        self.assertEqual(format_quantity_auto(0.0, DIMENSION_CURRENT, preferred_unit="mA"), "0 mA")
        self.assertEqual(format_quantity_auto(0.0, DIMENSION_CURRENT, preferred_unit="uA"), "0 uA")
        self.assertEqual(format_quantity_auto(0.0, DIMENSION_VOLTAGE, preferred_unit="mV"), "0 mV")
        self.assertEqual(format_quantity_auto(0.0, DIMENSION_CURRENT), "0 A")

    def test_render_quantity_si_like_with_bare_number_and_preferred_unit(self) -> None:
        self.assertEqual(
            render_quantity_si_like("0", DIMENSION_CURRENT, 0.0, preferred_unit="mA"),
            "0 mA",
        )
        self.assertEqual(
            render_quantity_si_like("0.0", DIMENSION_CURRENT, 0.0, preferred_unit="mA"),
            "0.0 mA",
        )
        self.assertEqual(
            render_quantity_si_like("-5", DIMENSION_CURRENT, -0.005, preferred_unit="mA"),
            "-5 mA",
        )

    def test_keithley_arrow_stepping_crosses_zero_into_negative_without_losing_unit(self) -> None:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from app.devices.keithley_2600.ui.page import KeithleyConfigurationPanel
        from app.ui.common.precision_stepper import install_precision_arrow_stepper

        install_precision_arrow_stepper(self.application)
        panel = KeithleyConfigurationPanel(simulation_settings())
        panel.channel.setCurrentText("A")
        panel.mode.setCurrentText("current")
        panel.level.setText("1 mA")

        down = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        up = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)

        # Step down to 0 mA
        self.application.sendEvent(panel.level, down)
        self.assertEqual(panel.level.text(), "0 mA")

        # Step down past zero into negative: must become -1 mA, NEVER -1 A or 0 A
        self.application.sendEvent(panel.level, down)
        self.assertEqual(panel.level.text(), "-1 mA")

        # Step back up to 0 mA
        self.application.sendEvent(panel.level, up)
        self.assertEqual(panel.level.text(), "0 mA")

        # Step back up to 1 mA
        self.application.sendEvent(panel.level, up)
        self.assertEqual(panel.level.text(), "1 mA")

    def test_limit_field_bare_zero_entry_preserves_active_unit(self) -> None:
        from PySide6.QtWidgets import QLineEdit
        from app.ui.widgets.limit_field import LimitField

        editor = QLineEdit("5 mA")
        field = LimitField(editor, minimum="-10 mA", maximum="10 mA")
        field.validate_and_clamp()
        self.assertEqual(field.editor.text(), "5 mA")

        # User types bare '0'
        field.editor.setText("0")
        field.validate_and_clamp()
        self.assertEqual(field.editor.text(), "0 mA")

        # User types bare '0.0'
        field.editor.setText("0.0")
        field.validate_and_clamp()
        self.assertEqual(field.editor.text(), "0.0 mA")


if __name__ == "__main__":
    unittest.main()
