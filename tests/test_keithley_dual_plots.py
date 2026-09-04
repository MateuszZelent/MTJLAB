from __future__ import annotations

from copy import deepcopy
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.adapter import KeithleyAdapter, KeithleyMeasurement
from app.devices.keithley_2600.ui.page import KeithleyPage, KeithleyPlotSettingsDialog
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

    def test_keithley_advanced_ranges_expand_collapse(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        controller = Mock()
        page = KeithleyPage(controller, settings)
        try:
            page.show()
            self.application.processEvents()

            panel = page.configuration_panel
            self.assertFalse(panel._advanced_ranges_expanded)
            self.assertIn("All AUTO", page.advanced_ranges_button.text())
            self.assertFalse(page.keithley_form.isRowVisible(page.source_autorange))
            self.assertFalse(page.keithley_form.isRowVisible(page.measure_voltage_autorange))

            page.advanced_ranges_button.click()
            self.application.processEvents()
            self.assertTrue(panel._advanced_ranges_expanded)
            self.assertEqual(page.advanced_ranges_button.text(), "Hide advanced range settings")
            self.assertTrue(page.keithley_form.isRowVisible(page.source_autorange))
            self.assertTrue(page.keithley_form.isRowVisible(page.measure_voltage_autorange))

            page.source_autorange.setChecked(False)
            self.application.processEvents()
            self.assertTrue(page.source_range.isEnabled())

            page.advanced_ranges_button.click()
            self.application.processEvents()
            self.assertFalse(panel._advanced_ranges_expanded)
            self.assertIn("Manual", page.advanced_ranges_button.text())

            page.source_autorange.setChecked(True)
            self.application.processEvents()
            self.assertIn("All AUTO", page.advanced_ranges_button.text())

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
            page.set_plot_history_window(45.0)
            self.assertEqual(page._history_window_s, 45.0)
            self.assertIn("Rolling 45 s history", page._history_notes["A"].text())
            self.assertIn("Rolling 45 s history", page._history_notes["B"].text())
            persisted = float(QSettings("LabControl", "LabControl").value("keithley/plot_history_window_s"))
            self.assertEqual(persisted, 45.0)

            # Test minimum 10s clamp
            page.set_plot_history_window(3.0)
            self.assertEqual(page._history_window_s, 10.0)
            self.assertIn("Rolling 10 s history", page._history_notes["A"].text())

            # Reset back to default
            page.set_plot_history_window(30.0)
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


if __name__ == "__main__":
    unittest.main()
