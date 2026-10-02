from __future__ import annotations

from copy import deepcopy
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.adapter import (
    KeithleyAdapter,
    KeithleyMeasurement,
)
from app.devices.keithley_2600.ui.page import (
    DEFAULT_KEITHLEY_LIVE_INTERVAL_MS,
    DEFAULT_KEITHLEY_PLOT_HISTORY_WINDOW_S,
    KeithleyPage,
    KeithleyPlotSettingsDialog,
)
from app.devices.keithley_2600.ui.twin_axis_plot import KeithleyTwinAxisPlotWidget
from app.domain.errors import SafetyViolation
from app.settings.models import StationSettings
from tests.helpers import loaded_settings
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.ui.dashboard.device_card import DeviceConnectionPanel
from app.ui.shell.page_host import FluentPageHost
from app.ui.widgets import SpectrumPlotWidget


class KeithleyDualPlotsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_spectrum_plot_compliance_markers(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            plot.set_compliance_points([1.0, 2.0], [3000.0, 3100.0])
            self.assertIsNotNone(plot.compliance_markers)
            data = plot.compliance_markers.getData()
            self.assertEqual(len(data[0]), 2)
            self.assertEqual(data[0].tolist(), [1.0, 2.0])
            self.assertEqual(data[1].tolist(), [3000.0, 3100.0])

            plot.clear_compliance_points()
            data_cleared = plot.compliance_markers.getData()
            self.assertEqual(len(data_cleared[0]), 0)

            plot.set_compliance_points([5.0], [2500.0])
            self.assertEqual(len(plot.compliance_markers.getData()[0]), 1)
            plot.clear()
            self.assertEqual(len(plot.compliance_markers.getData()[0]), 0)
        finally:
            plot.close()

    def test_keithley_default_history_and_live_refresh_timing(self) -> None:
        settings_store = Mock()
        settings_store.value.side_effect = lambda _key, default=None: default
        with patch(
            "app.devices.keithley_2600.ui.page.QSettings",
            return_value=settings_store,
        ):
            page = KeithleyPage(Mock(), simulated_station_settings(loaded_settings()))
        try:
            page.resize(1360, 880)
            page.show()
            self.application.processEvents()

            self.assertEqual(
                page._history_window_s, DEFAULT_KEITHLEY_PLOT_HISTORY_WINDOW_S
            )
            self.assertEqual(
                page.live_interval.value(), DEFAULT_KEITHLEY_LIVE_INTERVAL_MS
            )
            self.assertEqual(
                page._live_timer.interval(), DEFAULT_KEITHLEY_LIVE_INTERVAL_MS
            )
            self.assertIn("Rolling 120 s history", page._history_notes["A"].text())
            self.assertGreater(page.geometry().width(), 0)
            self.assertGreater(page.geometry().height(), 0)
        finally:
            page.close()

    def test_keithley_channel_switch_keeps_a_and_b_drafts_separate(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        channels = raw["devices"]["keithley"]["safety"]["channels"]
        channels["A"]["defaults"]["source_mode"] = "current"
        channels["A"]["defaults"]["source_current"] = "1 mA"
        channels["A"]["defaults"]["voltage_compliance"] = "670 mV"
        channels["A"]["lab_limits"]["voltage_compliance"] = {
            "min": "-650 mV",
            "max": "670 mV",
        }
        channels["A"]["lab_limits"]["measured_voltage_trip"] = {
            "min": "-701 mV",
            "max": "701 mV",
        }
        channels["B"]["defaults"]["source_mode"] = "current"
        channels["B"]["defaults"]["source_current"] = "0.1 mA"
        channels["B"]["defaults"]["voltage_compliance"] = "700 mV"
        channels["B"]["lab_limits"]["voltage_compliance"] = {
            "min": "-700 mV",
            "max": "700 mV",
        }
        channels["B"]["lab_limits"]["measured_voltage_trip"] = {
            "min": "-705 mV",
            "max": "705 mV",
        }
        settings = StationSettings.model_validate(raw)
        page = KeithleyPage(Mock(), settings)
        try:
            page.resize(1360, 880)
            page.show()
            self.application.processEvents()

            self.assertEqual(page.channel.currentText(), "B")
            self.assertEqual(page.compliance.text(), "700 mV")
            self.assertIn("-705 mV…705 mV", page.configuration_panel.safety_boundary_summary.text())

            page.channel.setCurrentText("A")
            self.application.processEvents()
            self.assertEqual(page.compliance.text(), "670 mV")
            self.assertIn("-701 mV…701 mV", page.configuration_panel.safety_boundary_summary.text())

            page.compliance.setText("660 mV")
            page.channel.setCurrentText("B")
            self.application.processEvents()
            self.assertEqual(page.compliance.text(), "700 mV")
            page.channel.setCurrentText("A")
            self.application.processEvents()
            self.assertEqual(page.compliance.text(), "660 mV")
        finally:
            page.close()

    def test_twin_axis_plot_construction_and_data(self) -> None:
        iv_plot = KeithleyTwinAxisPlotWidget("A")
        try:
            iv_plot.show()
            self.application.processEvents()
            self.assertEqual(iv_plot.p1.getAxis("left").labelText, "Voltage")
            self.assertEqual(iv_plot.p1.getAxis("right").labelText, "Current")

            x = [1.0, 2.0, 3.0]
            v = [0.1, 0.5, 0.67]
            i = [0.0001, 0.0005, 0.00021]
            mask = [False, False, True]
            iv_plot.set_data(x, v, i, compliance_mask=mask)

            self.assertEqual(len(iv_plot._voltage_curve.getData()[0]), 3)
            self.assertEqual(len(iv_plot._current_curve.getData()[0]), 3)

            v_comp = iv_plot._voltage_compliance_scatter.getData()
            i_comp = iv_plot._current_compliance_scatter.getData()
            self.assertEqual(len(v_comp[0]), 1)
            self.assertEqual(v_comp[0][0], 3.0)
            self.assertAlmostEqual(v_comp[1][0], 0.67)
            self.assertEqual(len(i_comp[0]), 1)
            self.assertEqual(i_comp[0][0], 3.0)
            self.assertAlmostEqual(i_comp[1][0], 0.00021)

            self.assertTrue(iv_plot.compliance_badge.isVisible())
            self.assertIn("COMPLIANCE", iv_plot.compliance_badge.text())

            readout = iv_plot.readout_label.text()
            self.assertIn("V:", readout)
            self.assertIn("I:", readout)

            iv_plot.clear()
            v_data = iv_plot._voltage_curve.getData()
            self.assertTrue(v_data[0] is None or len(v_data[0]) == 0)
            self.assertFalse(iv_plot.compliance_badge.isVisible())
        finally:
            iv_plot.close()

    def test_twin_axis_plot_theme_and_range(self) -> None:
        iv_plot = KeithleyTwinAxisPlotWidget("B")
        try:
            iv_plot.apply_theme("dark")
            self.assertEqual(iv_plot._theme_name, "dark")
            iv_plot.apply_theme("light")
            self.assertEqual(iv_plot._theme_name, "light")

            iv_plot.set_x_range(0.0, 45.0)
            view_range = iv_plot.p1.viewRange()
            self.assertAlmostEqual(view_range[0][0], 0.0)
            self.assertAlmostEqual(view_range[0][1], 45.0)
        finally:
            iv_plot.close()

    def test_adapter_three_position_compliance_policy(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        adapter = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory("keithley"),
        )
        adapter.connect()

        self.assertEqual(adapter.compliance_policy("A"), "warn_clamp")

        res = adapter.set_compliance_policy("A", "stop")
        self.assertEqual(res, "stop")
        self.assertEqual(adapter.compliance_policy("A"), "stop")

        res = adapter.set_compliance_policy("A", "warn_clamp")
        self.assertEqual(res, "warn_clamp")
        self.assertEqual(adapter.compliance_policy("A"), "warn_clamp")

        res = adapter.set_compliance_policy("A", "skip")
        self.assertEqual(res, "skip")
        self.assertEqual(adapter.compliance_policy("A"), "skip")

        res_bool_true = adapter.set_compliance_policy("A", True)
        self.assertTrue(res_bool_true)
        self.assertEqual(adapter.compliance_policy("A"), "stop")

        res_bool_false = adapter.set_compliance_policy("A", False)
        self.assertFalse(res_bool_false)
        self.assertEqual(adapter.compliance_policy("A"), "warn_clamp")

        adapter.set_compliance_policy("A", "skip")
        adapter._compliance_block_levels["A"] = 0.001
        adapter._compliance_block_modes["A"] = "current"
        adapter._compliance_warnings.add("A")
        adapter._assert_compliance_increase_allowed("A", 0.002, mode="current")

        adapter.set_compliance_policy("A", "warn_clamp")
        adapter._compliance_block_levels["A"] = 0.001
        adapter._compliance_block_modes["A"] = "current"
        with self.assertRaises(SafetyViolation):
            adapter._assert_compliance_increase_allowed("A", 0.002, mode="current")

    def test_keithley_page_has_dual_plots_and_three_position_policy(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        controller = Mock()
        page = KeithleyPage(controller, settings)
        try:
            page.resize(1360, 880)
            page.show()
            self.application.processEvents()

            for ch in ("A", "B"):
                widgets = page.history_widgets[ch]
                self.assertIn("plot", widgets)
                self.assertIn("iv_plot", widgets)
                self.assertIsInstance(widgets["plot"], SpectrumPlotWidget)
                self.assertIsInstance(widgets["iv_plot"], KeithleyTwinAxisPlotWidget)

                card = page.channel_cards[ch]
                self.assertIn("compliance_policy_combo", card)
                combo = card["compliance_policy_combo"]
                self.assertEqual(combo.count(), 3)
                self.assertEqual(combo.itemData(0), "stop")
                self.assertEqual(combo.itemData(1), "warn_clamp")
                self.assertEqual(combo.itemData(2), "skip")
                self.assertEqual(combo.currentData(), "warn_clamp")

                toggle = card["stop_compliance_toggle"]
                self.assertFalse(toggle.isChecked())
                toggle.setChecked(True)
                self.assertEqual(combo.currentData(), "stop")
                toggle.setChecked(False)
                self.assertEqual(combo.currentData(), "warn_clamp")

            m = KeithleyMeasurement(
                channel="A",
                voltage_v=0.67,
                current_a=0.00021,
                power_w=0.67 * 0.00021,
                output_enabled=True,
                compliance_detected=True,
                compliance_stop_required=False,
            )
            page._update_channel_measurement(m)
            self.application.processEvents()

            plot_a = page.history_widgets["A"]["plot"]
            iv_plot_a = page.history_widgets["A"]["iv_plot"]

            self.assertIsNotNone(plot_a.compliance_markers)
            comp_pts = plot_a.compliance_markers.getData()
            self.assertEqual(len(comp_pts[0]), 1)

            v_comp = iv_plot_a._voltage_compliance_scatter.getData()
            self.assertEqual(len(v_comp[0]), 1)
            self.assertTrue(iv_plot_a.compliance_badge.isVisible())

        finally:
            page.close()

    def test_keithley_advanced_ranges_open_in_modal(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        controller = Mock()
        page = KeithleyPage(controller, settings)
        try:
            page.show()
            self.application.processEvents()

            panel = page.configuration_panel
            self.assertFalse(panel._advanced_ranges_expanded)
            self.assertEqual(
                page.advanced_ranges_button.text(), "Advanced source settings…"
            )
            self.assertFalse(page.source_range.isVisible())
            self.assertFalse(page.measure_voltage_autorange.isVisible())
            self.assertFalse(page.max_abs_power_field.isVisible())

            with patch.object(panel.advanced_ranges_dialog, "exec") as execute:
                page.advanced_ranges_button.click()
                execute.assert_called_once_with()

            panel.advanced_ranges_dialog.show()
            self.application.processEvents()
            self.assertGreater(panel.advanced_ranges_dialog.geometry().width(), 0)
            self.assertGreater(panel.advanced_ranges_dialog.geometry().height(), 0)
            self.assertTrue(page.source_autorange.isVisible())
            self.assertTrue(page.measure_voltage_autorange.isVisible())
            self.assertTrue(page.max_abs_power_field.isVisible())

            page.source_autorange.setChecked(False)
            self.application.processEvents()
            self.assertTrue(page.source_range.isEnabled())
            self.assertIn("source", panel.advanced_ranges_summary.text())
            self.assertIn("power", panel.advanced_ranges_summary.text())

            panel.advanced_ranges_dialog.close()
            page.resize(760, 720)
            self.application.processEvents()
            self.assertTrue(page.advanced_ranges_button.isVisible())
            self.assertGreater(page.advanced_ranges_button.geometry().width(), 0)

        finally:
            page.close()

    def test_plot_size_hints_and_preferred_height(self) -> None:
        plot = SpectrumPlotWidget(compact_toolbar=True)
        self.assertEqual(plot.sizeHint().height(), 180)
        plot.set_preferred_height(170)
        self.assertEqual(plot.sizeHint().height(), 170)
        self.assertLessEqual(plot.minimumSizeHint().height(), 110)
        plot.close()

        iv_plot = KeithleyTwinAxisPlotWidget("A", preferred_height=140)
        self.assertEqual(iv_plot.sizeHint().height(), 140)
        iv_plot.set_preferred_height(155)
        self.assertEqual(iv_plot.sizeHint().height(), 155)
        self.assertLessEqual(iv_plot.minimumSizeHint().height(), 100)
        iv_plot.close()

    def test_keithley_page_single_screen_responsiveness_and_geometry(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        controller = Mock()
        page = KeithleyPage(controller, settings)
        connection_panel = DeviceConnectionPanel("Keithley 2600", "TCPIP0::192.168.1.10::inst0::INSTR")
        page.layout().insertWidget(2, connection_panel)

        host = FluentPageHost(page)
        try:
            for w, h in ((1600, 900), (1366, 768), (1280, 720)):
                host.resize(w, h)
                host.show()
                self.application.processEvents()

                # Zero scrollbar maximum means the page fits on screen without vertical scrolling
                self.assertEqual(
                    host.scroll_area.verticalScrollBar().maximum(),
                    0,
                    f"Expected zero scroll at {w}x{h}, got {host.scroll_area.verticalScrollBar().maximum()}",
                )

                for ch in ("A", "B"):
                    panel = page._panel_widgets[f"plot_{ch}"]
                    plot = page.history_widgets[ch]["plot"]
                    iv_plot = page.history_widgets[ch]["iv_plot"]

                    self.assertTrue(plot.isVisible())
                    self.assertTrue(iv_plot.isVisible())
                    self.assertGreaterEqual(plot.height(), 110)
                    self.assertGreaterEqual(iv_plot.height(), 100)

                    # Ensure both plots fit inside their enclosing panel
                    self.assertLessEqual(iv_plot.geometry().bottom(), panel.height())

            # Expanding to a tall screen should dynamically expand both plots
            host.resize(1600, 1080)
            self.application.processEvents()
            self.assertEqual(host.scroll_area.verticalScrollBar().maximum(), 0)

            plot_1080 = page.history_widgets["A"]["plot"].height()
            iv_1080 = page.history_widgets["A"]["iv_plot"].height()
            self.assertGreater(plot_1080, 260)
            self.assertGreater(iv_1080, 260)

        finally:
            host.close()
            page.close()

    def test_keithley_source_form_keeps_values_visible_in_narrow_splitter(self) -> None:
        """The source form must not render outside its hidden horizontal scrollbar."""
        settings = simulated_station_settings(loaded_settings())
        page = KeithleyPage(Mock(), settings)
        host = FluentPageHost(page)
        try:
            for width, height in ((1366, 880), (532, 700)):
                host.resize(width, height)
                host.show()
                self.application.processEvents()

                source_scroll = page.source_scroll
                source_content = source_scroll.widget()
                panel = page.configuration_panel
                self.assertEqual(source_scroll.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(source_content.width(), source_scroll.viewport().width())
                self.assertLessEqual(panel.geometry().right(), source_content.width())

                for field in (
                    panel.level_field,
                    panel.compliance_field,
                    panel.limit_fields["settle"],
                ):
                    editor = field.editor
                    self.assertTrue(field.isVisible())
                    self.assertTrue(editor.isVisible())
                    self.assertGreaterEqual(editor.width(), 140)
                    self.assertGreaterEqual(
                        editor.width(), editor.fontMetrics().horizontalAdvance(editor.text())
                    )
                    # Keep the Keithley safety action visually identical to
                    # the shared Rigol/Anritsu LimitField control.
                    self.assertEqual(field.edit_button.width(), 78)
                    self.assertEqual(field.edit_button.height(), 30)
                    self.assertEqual(field.edit_button.text(), "Edit")
                    self.assertFalse(field.edit_button.icon().isNull())

                for button in (
                    page.apply_configuration_button,
                    page.read_configuration_button,
                    page.measure_selected_button,
                ):
                    self.assertLessEqual(button.geometry().right(), source_content.width())
                    self.assertGreaterEqual(
                        button.width(), button.fontMetrics().horizontalAdvance(button.text())
                    )
        finally:
            host.close()
            page.close()

    def test_keithley_value_edit_keeps_caret_when_limits_refresh(self) -> None:
        """Refreshing safety limits must not move the caret during A/B editing."""
        settings = simulated_station_settings(loaded_settings())
        page = KeithleyPage(Mock(), settings)
        try:
            page.resize(1366, 880)
            page.show()
            self.application.processEvents()

            values = {
                # Channel A has the narrower default source envelope in the
                # test station profile; keep both edits inside their limits.
                "A": ("100 uA", "10 uA"),
                "B": ("10 mA", "1 mA"),
            }
            for channel in ("A", "B"):
                page.channel.setCurrentText(channel)
                page.mode.setCurrentText("current")
                self.application.processEvents()

                editor = page.level
                original, after_backspace = values[channel]
                editor.setText(original)
                editor.setFocus()
                editor.setCursorPosition(2)  # immediately after the zero

                # This is the refresh path used after form callbacks and
                # settings updates while the value editor can still be active.
                page._refresh_keithley_limits()
                self.assertEqual(editor.cursorPosition(), 2, channel)

                QTest.keyClick(editor, Qt.Key.Key_Backspace)
                self.application.processEvents()
                self.assertEqual(editor.text(), after_backspace, channel)
                self.assertEqual(editor.cursorPosition(), 1, channel)
        finally:
            page.close()

    def test_keithley_readback_renders_hardware_timing_rows(self) -> None:
        """Hardware delay readback is visible and remains read-only in the dialog."""
        settings = simulated_station_settings(loaded_settings())
        adapter = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        page = KeithleyPage(Mock(), settings)
        dialog = None
        try:
            adapter.connect()
            page._show_configuration_readback(adapter.read_configuration())
            dialog = page._readback_dialog
            self.assertIsNotNone(dialog)
            assert dialog is not None
            dialog.show()
            self.application.processEvents()

            self.assertGreater(dialog.table.width(), 0)
            self.assertEqual(dialog.tabs.count(), 2)
            self.assertEqual(
                [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())],
                ["All parameters", "Set by PyLab"],
            )
            self.assertEqual(dialog.table.rowCount(), 16)
            self.assertEqual(dialog.pylab_table.rowCount(), 12)
            pylab_rows = {
                dialog.pylab_table.item(row, 0).text()
                for row in range(dialog.pylab_table.rowCount())
            }
            self.assertIn("Settling time", pylab_rows)
            self.assertNotIn("Hardware source delay", pylab_rows)
            self.assertNotIn("Hardware measure delay", pylab_rows)
            self.assertNotIn("Measure delay factor", pylab_rows)
            self.assertGreater(dialog.pylab_table.width(), 0)
            rows = {
                dialog.table.item(row, 0).text(): row
                for row in range(dialog.table.rowCount())
            }
            expected = {
                "Hardware source delay": ("0 s", "0 s"),
                "Hardware measure delay": (
                    "AUTO (range-dependent)",
                    "AUTO (range-dependent)",
                ),
                "Measure delay factor": ("1", "1"),
            }
            for parameter, values in expected.items():
                row = rows[parameter]
                self.assertEqual(
                    (dialog.table.item(row, 1).text(), dialog.table.item(row, 4).text()),
                    values,
                )
                self.assertEqual(dialog.table.item(row, 2).text(), "Not controlled by form")
                self.assertIsNone(dialog.table.cellWidget(row, 3))
                self.assertIsNone(dialog.table.cellWidget(row, 6))

            settling_row = next(
                row
                for row in range(dialog.pylab_table.rowCount())
                if dialog.pylab_table.item(row, 0).text() == "Settling time"
            )
            self.assertEqual(
                dialog.pylab_table.item(settling_row, 1).text(),
                "APPLICATION ONLY",
            )
            self.assertIn("Form:", dialog.pylab_table.item(settling_row, 2).text())
            self.assertIsNone(dialog.pylab_table.cellWidget(settling_row, 3))

            dialog.tabs.setCurrentIndex(1)
            self.application.processEvents()
            self.assertTrue(dialog.pylab_table.isVisible())
            self.assertFalse(dialog.table.isVisible())
        finally:
            if dialog is not None:
                dialog.close()
            page.close()
            adapter.disconnect()

    def test_keithley_plot_settings_dialog_and_persistence(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        controller = Mock()
        page = KeithleyPage(controller, settings)
        try:
            page.show()
            self.application.processEvents()

            # Dialog widget verification
            dialog = KeithleyPlotSettingsDialog(page._history_window_s, page)
            self.assertEqual(dialog.window_seconds(), page._history_window_s)
            self.assertEqual(dialog.spin_box.minimum(), 10)
            self.assertEqual(dialog.spin_box.maximum(), 3600)

            # Test presets
            for preset_val, btn in dialog.preset_buttons.items():
                btn.click()
                self.assertEqual(dialog.window_seconds(), float(preset_val))

            # Test setting history window directly on page
            settings_store = Mock()
            persisted: dict[str, object] = {}
            settings_store.setValue.side_effect = persisted.__setitem__
            with patch(
                "app.devices.keithley_2600.ui.page.QSettings",
                return_value=settings_store,
            ):
                page.set_plot_history_window(45.0)
                self.assertEqual(page._history_window_s, 45.0)
                self.assertIn("Rolling 45 s history", page._history_notes["A"].text())
                self.assertIn("Rolling 45 s history", page._history_notes["B"].text())
                self.assertEqual(
                    persisted["keithley/plot_history_window_s"], 45.0
                )

                # Test minimum 10s clamp
                page.set_plot_history_window(3.0)
                self.assertEqual(page._history_window_s, 10.0)
                self.assertIn("Rolling 10 s history", page._history_notes["A"].text())

                # Reset back to default
                page.set_plot_history_window(DEFAULT_KEITHLEY_PLOT_HISTORY_WINDOW_S)
        finally:
            page.close()

    def test_keithley_live_control_warn_clamp_stepping(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        controller = Mock()
        page = KeithleyPage(controller, settings)
        try:
            page.show()
            self.application.processEvents()

            page._device_state_changed("OUTPUT_ON")
            page._set_channel_output("A", True)
            page.channel.setCurrentText("A")
            page.set_live_control_enabled(True)
            page._configured_channels.add("A")

            # In compliance state under warn_clamp
            page._device_state_changed("COMPLIANCE")
            page._compliance_warning_channels.add("A")
            page._compliance_block_levels["A"] = 0.003
            page._compliance_block_modes["A"] = "current"

            # Stepping down from 3 mA to 2 mA is allowed
            self.assertFalse(page._compliance_increase_is_blocked("A", 0.002, mode="current"))

            # Stepping up to 4 mA is blocked
            self.assertTrue(page._compliance_increase_is_blocked("A", 0.004, mode="current"))

            # Same level is not blocked (not an increase)
            self.assertFalse(page._compliance_increase_is_blocked("A", 0.003, mode="current"))

            # Different mode (e.g. voltage) is not blocked
            self.assertFalse(page._compliance_increase_is_blocked("A", 1.0, mode="voltage"))

            # Zero level block does not prevent normal operation
            page._compliance_block_levels["A"] = 0.0
            self.assertFalse(page._compliance_increase_is_blocked("A", 0.001, mode="current"))
        finally:
            page.close()

    def test_stop_compliance_keeps_live_readout_running_with_output_off(self) -> None:
        """A stop latch disables OUTPUT but must not silently deselect Live."""
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        controller = Mock()
        page = KeithleyPage(controller, settings)
        try:
            page.show()
            self.application.processEvents()
            page._device_state_changed("OUTPUT_ON")
            page._set_channel_output("B", True)
            page.live_channel_b.setChecked(True)
            page._measure_pending = False
            page._live_timer.start()

            measurement = KeithleyMeasurement(
                channel="B",
                voltage_v=0.067,
                current_a=0.001,
                power_w=0.000067,
                output_enabled=False,
                compliance_detected=True,
                compliance_stop_required=True,
                source_level_si=0.001,
                source_mode="current",
            )
            page._set_channel_output("B", False)
            page._mark_channel_compliance("B", measurement)

            self.assertTrue(page.live_channel_b.isChecked())
            self.assertTrue(page.live_channel_b.isEnabled())
            self.assertIn("B", page._selected_live_channels())
            self.assertTrue(page._live_timer.isActive())

            controller.call.reset_mock()
            page._measure_pending = False
            page._request_live_measurement()
            controller.call.assert_called_once_with("measure", "B")
        finally:
            page._live_timer.stop()
            page.close()


if __name__ == "__main__":
    unittest.main()
