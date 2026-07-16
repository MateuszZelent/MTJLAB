from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea

from app.domain.models import DeviceCapabilities
from app.ui.main_window import MainWindow


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_rigol_exposes_all_supported_advanced_controls_only_after_capability_probe(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            self.assertEqual(rigol.sweep_start_hold.text(), "0 s")
            self.assertEqual(rigol.sweep_stop_hold.text(), "0 s")
            self.assertEqual(rigol.sweep_return_time.text(), "0 s")
            self.assertEqual(rigol.sweep_trigger_slope.currentText(), "POS")
            self.assertEqual(rigol.burst_phase.text(), "0")
            self.assertEqual(rigol.burst_trigger_slope.currentText(), "POS")
            self.assertFalse(rigol.sync_phases_button.isEnabled())
            self.assertFalse(rigol.advanced.isTabEnabled(0))

            rigol.set_capabilities(
                DeviceCapabilities(
                    device_name="rigol",
                    model="DG1032Z",
                    firmware="sim",
                    features=frozenset({"modulation", "frequency_sweep", "burst", "phase_sync"}),
                )
            )
            self.assertTrue(rigol.sync_phases_button.isEnabled())
            self.assertTrue(all(rigol.advanced.isTabEnabled(index) for index in range(3)))
        finally:
            window.close()
            self.application.processEvents()

    def test_rigol_control_sections_are_tabbed_scrollable_and_have_live_preview(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            self.assertEqual(rigol.waveform.currentText(), "SIN")
            self.assertEqual(rigol.time_mode.currentText(), "Frequency")
            self.assertEqual(rigol.level_mode.currentText(), "Amplitude / Offset")
            self.assertEqual(rigol.frequency.text(), "1 kHz")
            self.assertEqual(rigol.vpp.text(), "2 mV")
            self.assertEqual(rigol.offset.text(), "0 V")
            self.assertEqual(rigol.phase.text(), "0")
            window.resize(820, 560)
            window.show()
            self.application.processEvents()

            self.assertEqual(rigol.control_tabs.count(), 4)
            self.assertEqual(
                [rigol.control_tabs.tabText(index) for index in range(4)],
                ["Basic", "Shape", "Output", "Advanced"],
            )
            for page in (rigol.basic_scroll, rigol.shape_scroll, rigol.output_scroll):
                self.assertIsInstance(page, QScrollArea)
                self.assertEqual(page.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.assertTrue(all(isinstance(rigol.advanced.widget(index), QScrollArea) for index in range(3)))
            self.assertGreater(rigol.preview_series.count(), 200)
        finally:
            window.close()
            self.application.processEvents()

    def test_rigol_form_adapts_to_dc_level_and_time_representations(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page

            rigol.waveform.setCurrentText("DC")
            self.application.processEvents()
            self.assertTrue(rigol.basic_form.isRowVisible(rigol.offset))
            for hidden in (
                rigol.time_mode,
                rigol.frequency,
                rigol.period,
                rigol.level_mode,
                rigol.high_level,
                rigol.low_level,
                rigol.vpp,
                rigol.phase,
            ):
                self.assertFalse(rigol.basic_form.isRowVisible(hidden))
            self.assertFalse(rigol.control_tabs.isTabVisible(1))
            self.assertFalse(rigol.control_tabs.isTabVisible(3))
            rigol.offset.setText("7 mV")
            self.assertEqual(rigol._effective_levels(), (0.007, 0.007))

            rigol.waveform.setCurrentText("SIN")
            rigol.level_mode.setCurrentText("Amplitude / Offset")
            rigol.vpp.setText("4 mV")
            rigol.offset.setText("1 mV")
            self.assertFalse(rigol.basic_form.isRowVisible(rigol.high_level))
            self.assertTrue(rigol.basic_form.isRowVisible(rigol.vpp))
            self.assertEqual(rigol._effective_levels(), (0.003, -0.001))

            rigol.time_mode.setCurrentText("Period")
            rigol.period.setText("2 ms")
            rigol._sync_frequency_from_period()
            self.assertFalse(rigol.basic_form.isRowVisible(rigol.frequency))
            self.assertTrue(rigol.basic_form.isRowVisible(rigol.period))
            self.assertEqual(rigol.frequency.text(), "500 Hz")
        finally:
            window.close()
            self.application.processEvents()

    def test_settings_are_split_into_general_and_per_instrument_tabs(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            settings = window.settings_page
            self.assertEqual(settings.tabs.count(), 4)
            self.assertEqual(
                [settings.tabs.tabText(index) for index in range(4)],
                ["General", "Rigol", "Keithley", "Anritsu"],
            )
            for name in ("general", "rigol", "keithley", "anritsu"):
                self.assertGreater(settings.trees[name].topLevelItemCount(), 0)
        finally:
            window.close()
            self.application.processEvents()

    def test_every_rigol_parameter_and_control_tab_has_context_help(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            parameters = (
                rigol.channel, rigol.waveform, rigol.time_mode, rigol.frequency, rigol.period,
                rigol.level_mode, rigol.high_level, rigol.low_level, rigol.vpp, rigol.offset,
                rigol.dut_impedance, rigol.phase, rigol.duty, rigol.ramp_symmetry,
                rigol.pulse_width, rigol.pulse_leading, rigol.pulse_trailing, rigol.load,
                rigol.output_polarity, rigol.output_mode, rigol.gate_polarity,
                rigol.sync_enabled, rigol.sync_polarity, rigol.sync_delay, rigol.mod_enabled,
                rigol.mod_type, rigol.mod_source, rigol.mod_rate, rigol.mod_parameter,
                rigol.mod_shape, rigol.mod_polarity, rigol.sweep_enabled, rigol.sweep_start,
                rigol.sweep_stop, rigol.sweep_duration, rigol.sweep_start_hold,
                rigol.sweep_stop_hold, rigol.sweep_return_time, rigol.sweep_spacing,
                rigol.sweep_steps, rigol.sweep_trigger, rigol.sweep_trigger_slope,
                rigol.sweep_trigger_out, rigol.burst_enabled, rigol.burst_mode,
                rigol.burst_cycles, rigol.burst_phase, rigol.burst_period, rigol.burst_delay,
                rigol.burst_trigger, rigol.burst_trigger_slope, rigol.burst_trigger_out,
                rigol.burst_gate_polarity, rigol.burst_idle,
            )
            self.assertTrue(all(parameter.toolTip() for parameter in parameters))
            self.assertTrue(all(rigol.control_tabs.tabToolTip(index) for index in range(4)))
            buttons = {button.text(): button for button in rigol.findChildren(QPushButton)}
            for label in ("ARM (30 s)", "OUTPUT ON", "OUTPUT OFF"):
                self.assertIn(label, buttons)
                self.assertTrue(buttons[label].toolTip())
        finally:
            window.close()
            self.application.processEvents()

    def test_theme_switch_emits_light_and_dark_and_updates_charts(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        themes: list[str] = []
        window.theme_changed.connect(themes.append)
        try:
            self.assertFalse(window.theme_action.isChecked())
            window.theme_action.setChecked(True)
            self.application.processEvents()
            window.theme_action.setChecked(False)
            self.application.processEvents()
            self.assertEqual(themes, ["light", "dark"])
        finally:
            window.close()
            self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
