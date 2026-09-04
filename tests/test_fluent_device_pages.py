from __future__ import annotations

import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel
from qfluentwidgets import (
    CardWidget,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    TransparentPushButton,
)

from app.devices.keithley_2600 import KeithleySourceRequest
from app.devices.rigol_dg1000z import RigolChannelConfig
from app.domain.errors import SafetyViolation
from app.domain.quick_controls import QuickConfigureCommand
from app.ui.shell import MainWindow
from app.ui.design_system import tokens_for
from app.domain.models import DeviceCapabilities


class FluentDevicePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_rigol_and_keithley_expose_visible_fluent_cards_and_actions(self) -> None:
        """Device workspaces use theme-aware Fluent cards, not legacy framed panels."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("rigol")
            self.application.processEvents()

            rigol = window.rigol_page
            self.assertIsInstance(rigol.hero_card, CardWidget)
            self.assertIsInstance(rigol.safety_card, CardWidget)
            self.assertIsInstance(rigol.waveform_apply_button, PrimaryPushButton)
            self.assertTrue(rigol.waveform_apply_button.isVisibleTo(window))
            self.assertIsInstance(rigol.channel, ComboBox)
            self.assertTrue(rigol._limit_fields)
            self.assertTrue(
                all(
                    isinstance(limit.edit_button, PushButton)
                    for limit in rigol._limit_fields.values()
                )
            )
            self.assertTrue(rigol.preview_plot.toolbar_buttons)
            self.assertTrue(
                all(
                    isinstance(button, TransparentPushButton)
                    for button in rigol.preview_plot.toolbar_buttons
                )
            )
            visible_text = "\n".join(
                label.text() for label in rigol.findChildren(QLabel)
            )
            for broken_marker in ("Â", "â", "Ã", "�"):
                self.assertNotIn(broken_marker, visible_text)
            self.assertEqual(rigol.device_led.text(), "●")
            plot_title = rigol.preview_plot.plot.getPlotItem().titleLabel.text
            self.assertIn("· SIN ·", plot_title)

            window._navigate_to("keithley")
            self.application.processEvents()
            keithley = window.keithley_page
            self.assertIsInstance(keithley.hero_card, CardWidget)
            self.assertTrue(
                all(isinstance(card["card"], CardWidget) for card in keithley.channel_cards.values())
            )
            self.assertTrue(
                all(isinstance(card["measure"], PushButton) for card in keithley.channel_cards.values())
            )
            self.assertTrue(keithley.channel_cards["A"]["measure"].isVisibleTo(window))
            for channel in ("A", "B"):
                card = keithley.channel_cards[channel]
                self.assertTrue(card["output_on_action"].isVisibleTo(window))
                self.assertTrue(card["output_off_action"].isVisibleTo(window))
                self.assertEqual(card["output_on_action"].text(), "OUTPUT ON")
                self.assertEqual(card["output_off_action"].text(), "OUTPUT OFF")
                self.assertFalse(card["output_on_action"].isEnabled())
                self.assertFalse(card["output_off_action"].isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_output_and_configuration_controls_render_for_rigol_and_anritsu(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("rigol")
            self.application.processEvents()
            rigol = window.rigol_page
            self.assertTrue(rigol.live_control_switch.isVisibleTo(window))
            self.assertFalse(rigol.live_control_switch.isChecked())
            self.assertEqual(rigol.live_control_switch.text, "Live control")
            self.assertTrue(rigol.waveform_apply_button.isVisibleTo(window))
            self.assertGreater(rigol.waveform_apply_button.width(), 80)
            for control in (rigol.output_on, rigol.output_off):
                self.assertTrue(control.isVisibleTo(window))
                self.assertGreater(control.width(), 80)
                self.assertGreater(control.height(), 24)
            self.assertFalse(rigol.output_on.isEnabled())
            self.assertFalse(rigol.output_off.isEnabled())
            self.assertTrue(rigol.output_action_bar.isVisibleTo(window))
            rigol.control_tabs.setCurrentIndex(2)
            self.application.processEvents()
            self.assertTrue(rigol.output_on.isVisibleTo(window))
            self.assertTrue(rigol.output_off.isVisibleTo(window))

            window._navigate_to("anritsu")
            anritsu = window.anritsu_page
            self.application.processEvents()
            self.assertTrue(anritsu.configure_button.isVisibleTo(window))
            self.assertGreater(anritsu.configure_button.width(), 80)
            anritsu.set_capabilities(
                DeviceCapabilities(
                    device_name="anritsu",
                    model="MS2830A",
                    firmware="sim",
                    features=frozenset({"spectrum_trace", "signal_generator"}),
                    hardware_options=("041", "020"),
                )
            )
            anritsu.mode_tabs.setCurrentIndex(anritsu.signal_generator_tab_index)
            self.application.processEvents()
            for control in (
                anritsu.sg_configure,
                anritsu.sg_on,
                anritsu.sg_off,
            ):
                self.assertTrue(control.isVisibleTo(window))
                self.assertGreater(control.width(), 80)
                self.assertGreater(control.height(), 24)
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_workspace_stacks_without_clipping_at_minimum_window_size(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(820, 560)
            window.show()
            window._navigate_to("keithley")
            self.application.processEvents()

            page = window.keithley_page
            host = window.navigation_routes["keithley"]
            self.assertEqual(page.workspace_splitter.orientation(), Qt.Orientation.Vertical)
            self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)
            self.assertEqual(page.width(), host.scroll_area.viewport().width())
        finally:
            window.close()
            self.application.processEvents()

    def test_device_card_surface_rethemes_in_the_same_visible_window(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("keithley")
            self.application.processEvents()
            card = window.keithley_page.channel_cards["A"]["card"]
            # Card corners are intentionally transparent for rounded Fluent
            # geometry; sample its opaque interior instead.
            point = card.mapTo(window, QPoint(40, 40))
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light = window.grab().toImage().pixelColor(point).name()
            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark = window.grab().toImage().pixelColor(point).name()
            self.assertNotEqual(light, dark)
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_quick_control_builder_uses_offline_form_dry_run_and_live_modes(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            operation, payload = keithley.quick_control_hardware_request(
                "keithley.A.current", "1.00 mA", "output_off"
            )
            self.assertEqual(operation, "quick_configure")
            self.assertIsInstance(payload, QuickConfigureCommand)
            assert isinstance(payload, QuickConfigureCommand)
            self.assertIsInstance(payload.configuration, KeithleySourceRequest)
            assert isinstance(payload.configuration, KeithleySourceRequest)
            self.assertEqual(payload.configuration.channel, "A")
            self.assertEqual(payload.configuration.mode, "current")
            self.assertAlmostEqual(payload.configuration.level_si, 0.001)

            with self.assertRaises(SafetyViolation):
                keithley.quick_control_hardware_request(
                    "keithley.A.current", "1.00 mA", "output_on"
                )

            keithley._output_state_known["A"] = True
            keithley._output_states["A"] = False
            operation, payload = keithley.quick_control_hardware_request(
                "keithley.A.current", "1.00 mA", "output_on"
            )
            self.assertEqual(operation, "quick_configure")
            self.assertIsInstance(payload, QuickConfigureCommand)

            keithley._configured_channels.add("A")
            keithley._output_states["A"] = True
            operation, payload = keithley.quick_control_hardware_request(
                "keithley.A.current", "1.00 mA", "output_on"
            )
            self.assertEqual(operation, "quick_setpoint")
            self.assertEqual(payload.target, "keithley.A.current")
            self.assertEqual(payload.quantity_text, "1.00 mA")

            rigol = window.rigol_page
            operation, payload = rigol.quick_control_hardware_request(
                "rigol.1.frequency", "2.000 kHz", "output_off"
            )
            self.assertEqual(operation, "quick_configure")
            self.assertIsInstance(payload, QuickConfigureCommand)
            assert isinstance(payload, QuickConfigureCommand)
            self.assertIsInstance(payload.configuration, RigolChannelConfig)
            assert isinstance(payload.configuration, RigolChannelConfig)
            self.assertEqual(payload.configuration.channel, 1)
            self.assertAlmostEqual(payload.configuration.frequency_hz, 2_000.0)
        finally:
            window.close()
            self.application.processEvents()

    def test_light_theme_gives_cards_a_visible_token_border_across_device_pages(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()

            tokens = tokens_for("light")
            for route, card in (
                ("rigol", window.rigol_page.hero_card),
                ("keithley", window.keithley_page.channel_cards["A"]["card"]),
                ("anritsu", window.anritsu_page.setup_card),
                ("moke_box", window.moke_box_page.vout_card),
                ("lakeshore_gaussmeter", window.lakeshore_gaussmeter_page.values_card),
            ):
                window._navigate_to(route)
                self.application.processEvents()
                self.assertIn("border: 1px solid palette(mid)", card.styleSheet())
                self.assertEqual(
                    card.palette().color(QPalette.ColorRole.Mid).name(),
                    tokens.border,
                )
                self.assertEqual(
                    card.palette().color(QPalette.ColorRole.Window).name(),
                    tokens.surface,
                )
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_rigol_form_editing_does_not_send_when_disconnected_or_output_off(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("rigol")
            self.application.processEvents()

            rigol = window.rigol_page
            # Initially Rigol is disconnected and live control switch is off
            self.assertFalse(rigol._is_device_connected())
            self.assertFalse(rigol.live_control_enabled)
            self.assertFalse(rigol.live_control_switch.isChecked())

            emitted_signals: list[tuple[str, str]] = []
            rigol.quick_setpoint_requested.connect(
                lambda target, text: emitted_signals.append((target, text))
            )

            # Editing frequency and voltages while disconnected (live control off)
            rigol.frequency.setText("2.5 kHz")
            rigol.frequency.editingFinished.emit()
            rigol.vpp.setText("10 mV")
            rigol.vpp.editingFinished.emit()
            rigol.offset.setText("5 mV")
            rigol.offset.editingFinished.emit()
            self.application.processEvents()

            # No quick setpoints emitted and no configure operations dispatched
            self.assertEqual(emitted_signals, [])
            self.assertEqual(len(rigol._queued_ui_operations["configure"]), 0)

            # Even if user toggles live control ON while disconnected, offline edits do not send or crash
            rigol.set_live_control_enabled(True)
            self.assertTrue(rigol.live_control_enabled)
            rigol.frequency.setText("3.0 kHz")
            rigol.frequency.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals, [])

            # Reset live control to OFF
            rigol.set_live_control_enabled(False)
            self.assertFalse(rigol.live_control_enabled)

            # Clicking Validate while disconnected validates locally and shows info banner, not popup error
            rigol.waveform_apply_button.click()
            self.application.processEvents()
            self.assertIn("disconnected", rigol.banner.last_message.lower())

            # Now simulate connected with output OFF
            rigol._device_state_changed("output_off")
            self.assertTrue(rigol._is_device_connected())
            self.assertFalse(rigol._active_output_selected())

            # With live control OFF, edits do not send
            rigol.vpp.setText("15 mV")
            rigol.vpp.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals, [])

            # Now simulate output ON
            rigol._device_state_changed("output_on")
            rigol._set_rigol_channel_output(1, True)
            rigol._record_visible_quick_readback(1)
            self.assertTrue(rigol._active_output_selected())

            # With output ON but live control OFF, edits still do not send immediately
            rigol.vpp.setText("18 mV")
            rigol.vpp.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals, [])

            # Once live control is toggled ON, edits are immediately sent
            rigol.set_live_control_enabled(True)
            self.assertTrue(rigol.live_control_enabled)
            rigol.vpp.setText("20 mV")
            rigol.vpp.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals, [("rigol.1.amplitude", "20 mV")])
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_form_editing_live_control_channel_a_and_b(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("keithley")
            self.application.processEvents()

            keithley = window.keithley_page
            self.assertTrue(keithley.live_control_switch.isVisibleTo(window))
            self.assertFalse(keithley.live_control_switch.isChecked())
            self.assertEqual(keithley.live_control_switch.text, "Live control")
            self.assertFalse(keithley.live_control_enabled)
            self.assertFalse(keithley._is_device_connected())

            emitted_signals: list[tuple[str, str]] = []
            keithley.quick_setpoint_requested.connect(
                lambda target, text: emitted_signals.append((target, text))
            )

            # Disconnected: edits with live control OFF do not emit
            keithley.level.setText("2 mA")
            keithley.level.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals, [])

            # Disconnected: edits with live control ON do not emit or crash
            keithley.set_live_control_enabled(True)
            self.assertTrue(keithley.live_control_enabled)
            keithley.level.setText("3 mA")
            keithley.level.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals, [])

            # Reset live control to OFF
            keithley.set_live_control_enabled(False)
            self.assertFalse(keithley.live_control_enabled)

            # Simulate connected with OUTPUT OFF
            keithley._device_state_changed("OUTPUT_OFF")
            self.assertTrue(keithley._is_device_connected())

            # With live control OFF and OUTPUT OFF, edits do not send
            keithley.level.setText("1.2 mA")
            keithley.level.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals, [])

            # Turn live control ON with OUTPUT OFF: Channel B edit immediately dispatches
            keithley.set_live_control_enabled(True)
            self.assertTrue(keithley.live_control_enabled)
            keithley.level.setText("1.5 mA")
            keithley.level.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals[-1], ("keithley.B.current", "1.5 mA"))

            # Simulate OUTPUT ON on Channel B: live control edit dispatches live setpoint
            keithley._device_state_changed("OUTPUT_ON")
            keithley._set_channel_output("B", True)
            keithley.level.setText("2.2 mA")
            keithley.level.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals[-1], ("keithley.B.current", "2.2 mA"))

            # Switch to Channel A: OUTPUT is OFF
            keithley.channel.setCurrentText("A")
            self.application.processEvents()
            self.assertEqual(keithley.channel.currentText(), "A")

            # Channel A edit with OUTPUT OFF immediately dispatches
            keithley.level.setText("800 uA")
            keithley.level.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals[-1], ("keithley.A.current", "800 uA"))

            # Simulate Channel A OUTPUT ON: Channel A edit dispatches live setpoint
            keithley._set_channel_output("A", True)
            keithley.level.setText("950 uA")
            keithley.level.editingFinished.emit()
            self.application.processEvents()
            self.assertEqual(emitted_signals[-1], ("keithley.A.current", "950 uA"))

            # Compliance edit with OUTPUT ON: dispatches update_source_compliance
            original_call = keithley._controller.call
            keithley._controller.call = Mock()
            try:
                keithley.compliance.setText("60 mV")
                keithley.compliance.editingFinished.emit()
                self.application.processEvents()
                keithley._controller.call.assert_called_with(
                    "update_source_compliance", ("A", "current", 0.06)
                )
                self.assertIn("update_source_compliance", keithley._pending_channels)
            finally:
                keithley._controller.call = original_call

            # Switch back to Channel B with OUTPUT OFF: compliance edit dispatches configure
            keithley.channel.setCurrentText("B")
            keithley._set_channel_output("B", False)
            self.application.processEvents()
            before_count = len(emitted_signals)
            keithley.compliance.setText("45 mV")
            keithley.compliance.editingFinished.emit()
            self.application.processEvents()
            self.assertGreater(len(emitted_signals), before_count)
            self.assertEqual(emitted_signals[-1][0], "keithley.B.current")
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_read_configuration_button_remains_stable_and_does_not_blink(self) -> None:
        """Read configuration button must stay enabled and never flicker during live measurements."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("keithley")
            self.application.processEvents()

            keithley = window.keithley_page
            keithley._device_state_changed("OUTPUT_OFF")
            self.application.processEvents()
            self.assertTrue(keithley.read_configuration_button.isEnabled())

            # Track calls to setEnabled on read_configuration_button
            disabled_calls = []
            orig_set_enabled = keithley.read_configuration_button.setEnabled

            def tracking_set_enabled(val: bool) -> None:
                if not val:
                    disabled_calls.append(val)
                orig_set_enabled(val)

            keithley.read_configuration_button.setEnabled = tracking_set_enabled

            # Simulate live measurement running: _measure_pending becomes True
            keithley._measure_pending = True
            keithley._update_output_readiness()
            self.assertTrue(keithley.read_configuration_button.isEnabled())
            self.assertEqual(len(disabled_calls), 0, "Button was disabled when measure was pending!")

            # Simulate arrow stepping while measurement is pending
            from PySide6.QtCore import QEvent
            from PySide6.QtGui import QKeyEvent
            from app.ui.common.precision_stepper import install_precision_arrow_stepper

            install_precision_arrow_stepper(self.application)
            keithley.level.setText("1 mA")
            down = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
            self.application.sendEvent(keithley.level, down)
            self.application.processEvents()

            # Verify button never disabled or flickered
            self.assertEqual(len(disabled_calls), 0, "Button flickered during arrow stepping!")
            self.assertTrue(keithley.read_configuration_button.isEnabled())
        finally:
            window.close()
            self.application.processEvents()


