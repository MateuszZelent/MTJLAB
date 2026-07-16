from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QScrollArea, QTreeWidgetItemIterator
from PySide6.QtTest import QTest

from app.domain.models import DeviceCapabilities
from app.devices.anritsu import SpectrumTrace
from app.settings.repository import SettingsRepository
from app.ui.main_window import LimitEditDialog, MainWindow


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
            self.assertGreater(rigol.preview_plot.trace_point_count("Waveform"), 200)
        finally:
            window.close()
            self.application.processEvents()

    def test_rigol_form_adapts_to_dc_level_and_time_representations(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page

            rigol.waveform.setCurrentText("DC")
            self.application.processEvents()
            self.assertTrue(rigol.basic_form.isRowVisible(rigol._row_widget(rigol.offset)))
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
                self.assertFalse(rigol.basic_form.isRowVisible(rigol._row_widget(hidden)))
            self.assertFalse(rigol.control_tabs.isTabVisible(1))
            self.assertFalse(rigol.control_tabs.isTabVisible(3))
            rigol.offset.setText("7 mV")
            self.assertEqual(rigol._effective_levels(), (0.007, 0.007))

            rigol.waveform.setCurrentText("SIN")
            rigol.level_mode.setCurrentText("Amplitude / Offset")
            rigol.vpp.setText("4 mV")
            rigol.offset.setText("1 mV")
            self.assertFalse(rigol.basic_form.isRowVisible(rigol._row_widget(rigol.high_level)))
            self.assertTrue(rigol.basic_form.isRowVisible(rigol._row_widget(rigol.vpp)))
            self.assertEqual(rigol._effective_levels(), (0.003, -0.001))

            rigol.time_mode.setCurrentText("Period")
            rigol.period.setText("2 ms")
            rigol._sync_frequency_from_period()
            self.assertFalse(rigol.basic_form.isRowVisible(rigol._row_widget(rigol.frequency)))
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

    def test_theme_switch_supports_light_dark_and_system_persistence(self) -> None:
        source = Path(".config/settings.yml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            path.write_text(source, encoding="utf-8")
            window = MainWindow(path, simulation=True)
            themes: list[str] = []
            window.theme_changed.connect(themes.append)
            try:
                window.theme_actions["light"].trigger()
                self.application.processEvents()
                self.assertEqual(SettingsRepository(path).load().raw["ui"]["theme"], "light")
                window.theme_actions["dark"].trigger()
                self.application.processEvents()
                self.assertEqual(SettingsRepository(path).load().raw["ui"]["theme"], "dark")
                window.theme_actions["system"].trigger()
                self.application.processEvents()
                self.assertEqual(SettingsRepository(path).load().raw["ui"]["theme"], "system")
                self.assertEqual(themes[:2], ["light", "dark"])
                self.assertIn(themes[-1], {"light", "dark"})
            finally:
                window.close()
                self.application.processEvents()

    def test_discovered_assignment_updates_card_and_worker_adapter_before_connect(self) -> None:
        source = Path(".config/settings.yml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            path.write_text(source, encoding="utf-8")
            window = MainWindow(path, simulation=False)
            try:
                resource = "USB0::NEW_RIGOL::INSTR"
                with patch.object(
                    QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
                ):
                    window._save_discovered_assignments(
                        {"rigol": (resource, "@py", "RIGOL TECHNOLOGIES,DG1032Z,NEW,1")}
                    )
                QTest.qWait(100)
                self.application.processEvents()

                loaded = SettingsRepository(path).load().settings
                self.assertEqual(loaded.rigol.connection.resource, resource)
                self.assertIn(resource, window.dashboard.cards["rigol"].resource.text())
                self.assertTrue(window.dashboard.cards["rigol"].connect_button.isEnabled())
                worker_adapter = window._controllers["rigol"]._worker._adapter
                self.assertEqual(worker_adapter._settings.connection.resource, resource)
            finally:
                window.close()
                self.application.processEvents()

    def test_limit_edit_is_autosaved_after_short_idle_period(self) -> None:
        source = Path(".config/settings.yml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            path.write_text(source, encoding="utf-8")
            window = MainWindow(path, simulation=False)
            try:
                tree = window.settings_page.trees["rigol"]
                iterator = QTreeWidgetItemIterator(tree)
                target_path = ("devices", "rigol", "safety", "channels", "1", "lab_limits", "frequency", "max")
                target = None
                while iterator.value() is not None:
                    item = iterator.value()
                    if item.data(0, Qt.ItemDataRole.UserRole) == target_path:
                        target = item
                        break
                    iterator += 1
                self.assertIsNotNone(target)
                target.setText(1, "900 kHz")
                QTest.qWait(900)
                loaded = SettingsRepository(path).load()
                self.assertEqual(
                    loaded.raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["frequency"]["max"],
                    "900 kHz",
                )
                self.assertFalse(window.settings_page._dirty)
            finally:
                window.close()
                self.application.processEvents()

    def test_manual_waveform_above_amplitude_limit_is_not_queued(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            rigol.channel.setCurrentText("1")
            rigol.waveform.setCurrentText("SIN")
            rigol.level_mode.setCurrentText("Amplitude / Offset")
            rigol.vpp.setText("805 mV")
            rigol.offset.setText("0 V")
            rigol._controller.call = Mock()
            rigol.configure()
            rigol._controller.call.assert_not_called()
            self.assertFalse(rigol.banner.isHidden())
            self.assertIn("poza zatwierdzonym zakresem", rigol.banner.label.text())

        finally:
            window.close()
            self.application.processEvents()

    def test_limit_field_clamps_amplitude_on_focus_loss_and_shows_warning(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            rigol.channel.setCurrentText("1")
            rigol.vpp.setText("802 mV")
            rigol.vpp.editingFinished.emit()
            field = rigol._limit_fields[rigol.vpp]
            self.assertEqual(rigol.vpp.text(), "800 mV")
            self.assertFalse(field.validation_warning.isHidden())
            self.assertIn("exceeded MAX", field.validation_warning.text())
        finally:
            window.close()
            self.application.processEvents()

    def test_limit_field_clamps_sweep_endpoint_with_converted_units(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            rigol.channel.setCurrentText("1")
            maximum = window._settings.rigol.safety.channels["1"].lab_limits.frequency.max
            rigol.sweep_stop.setText("999 GHz")
            rigol.sweep_stop.editingFinished.emit()
            field = rigol._limit_fields[rigol.sweep_stop]
            self.assertEqual(rigol.sweep_stop.text(), maximum)
            self.assertFalse(field.validation_warning.isHidden())
        finally:
            window.close()
            self.application.processEvents()

    def test_limit_field_normalizes_units_and_scientific_notation(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            field = rigol._limit_fields[rigol.frequency]
            rigol.frequency.setText("100000 kHz")
            rigol.frequency.editingFinished.emit()
            self.assertEqual(rigol.frequency.text(), "100 MHz")
            self.assertTrue(field.validation_warning.isHidden())

            rigol.frequency.setText("1e9")
            rigol.frequency.editingFinished.emit()
            maximum = window._settings.rigol.safety.channels["1"].lab_limits.frequency.max
            self.assertEqual(rigol.frequency.text(), maximum)
            self.assertIn("exceeded MAX", field.validation_warning.text())

            rigol.frequency.setText("1000000000 kHZ")
            rigol.frequency.editingFinished.emit()
            self.assertEqual(rigol.frequency.text(), maximum)
            self.assertIn("exceeded MAX", field.validation_warning.text())
        finally:
            window.close()
            self.application.processEvents()

    def test_frequency_sweep_endpoint_above_limit_is_not_queued(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            rigol.channel.setCurrentText("1")
            rigol.sweep_start.setText("1 MHz")
            rigol.sweep_stop.setText("501 MHz")
            rigol._controller.call = Mock()
            with patch.object(QMessageBox, "warning") as warning:
                rigol.configure_sweep()
            rigol._controller.call.assert_not_called()
            self.assertIn("sweep_stop", warning.call_args.args[2])
        finally:
            window.close()
            self.application.processEvents()

    def test_device_fields_show_profile_limits_and_keithley_updates_them_by_mode(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=False)
        try:
            rigol = window.rigol_page
            self.assertEqual(rigol._limit_fields[rigol.frequency].minimum.text(), "MIN  1 Hz")
            expected_frequency_max = window._settings.rigol.safety.channels["1"].lab_limits.frequency.max
            self.assertEqual(rigol._limit_fields[rigol.frequency].maximum.text(), f"MAX  {expected_frequency_max}")

            keithley = window.keithley_page
            self.assertEqual(keithley._limit_fields["level"].minimum.text(), "MIN  0 A")
            self.assertEqual(keithley._limit_fields["level"].maximum.text(), "MAX  10 mA")
            keithley.mode.setCurrentText("voltage")
            self.application.processEvents()
            self.assertEqual(keithley._limit_fields["level"].minimum.text(), "MIN  -67 mV")
            self.assertEqual(keithley._limit_fields["compliance"].maximum.text(), "MAX  10 mA")

            anritsu = window.anritsu_page
            self.assertEqual(anritsu._limit_fields["frequency0"].minimum.text(), "MIN  NOT SET")
            self.assertEqual(anritsu._limit_fields["sweep_points3"].maximum.text(), "MAX  10001")
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_dual_channel_dashboard_updates_ivrp_and_compliance(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            self.assertEqual(set(keithley.channel_cards), {"A", "B"})
            measurement = SimpleNamespace(
                channel="B",
                voltage_v=0.067,
                current_a=0.001,
                power_w=0.000067,
                compliance_detected=True,
            )
            keithley._result("measure", measurement)
            card = keithley.channel_cards["B"]
            self.assertEqual(card["voltage"].text(), "67 mV")
            self.assertEqual(card["current"].text(), "1 mA")
            self.assertEqual(card["resistance"].text(), "67 Ω")
            self.assertEqual(card["power"].text(), "67 µW")
            self.assertIn("ACTIVE", card["compliance"].text())
            self.assertEqual(card["output"].text(), "OUTPUT OFF")
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_source_fields_follow_current_voltage_and_measure_only_modes(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            self.assertEqual(keithley.keithley_form.labelForField(keithley.level_field).text(), "Source current")
            self.assertIn("Voltage compliance", keithley.keithley_form.labelForField(keithley.compliance_field).text())
            keithley.level.setText("2 mA")

            keithley.mode.setCurrentText("voltage")
            self.assertEqual(keithley.keithley_form.labelForField(keithley.level_field).text(), "Source voltage")
            self.assertIn("Current compliance", keithley.keithley_form.labelForField(keithley.compliance_field).text())
            self.assertTrue(keithley.level.text().endswith("V"))
            self.assertTrue(keithley.compliance.text().endswith("A"))

            keithley.mode.setCurrentText("measure_only")
            self.assertFalse(keithley.keithley_form.isRowVisible(keithley.level_field))
            self.assertFalse(keithley.keithley_form.isRowVisible(keithley.compliance_field))
            self.assertIn("measurement-only", keithley.configure_button.text())

            keithley.mode.setCurrentText("current")
            self.assertEqual(keithley.level.text(), "2 mA")
            self.assertTrue(keithley.keithley_form.isRowVisible(keithley.level_field))
        finally:
            window.close()
            self.application.processEvents()

    def test_every_keithley_setting_meter_and_action_has_context_help(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            controls = (
                keithley.channel, keithley.mode, keithley.level, keithley.compliance,
                keithley.nplc, keithley.settle, keithley.sense_mode,
                keithley.source_autorange, keithley.source_range,
                keithley.measure_voltage_autorange, keithley.measure_voltage_range,
                keithley.measure_current_autorange, keithley.measure_current_range,
                keithley.live_measurements, keithley.device_led, keithley.device_state,
            )
            self.assertTrue(all(widget.toolTip() for widget in controls))
            for card in keithley.channel_cards.values():
                self.assertTrue(all(widget.toolTip() for widget in card.values()))
            action_labels = {
                "Configure current source while OUTPUT is OFF",
                "Measure selected channel",
                "ARM (30 s)",
                "OUTPUT ON",
                "Ramp to zero + OFF",
            }
            actions = [button for button in keithley.findChildren(QPushButton) if button.text() in action_labels]
            self.assertEqual(len(actions), len(action_labels))
            self.assertTrue(all(button.toolTip() for button in actions))
            self.assertTrue(keithley.control_tabs.tabToolTip(0))
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_averaging_reference_and_processed_views_preserve_raw_trace(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu.average_count.setValue(2)
            trace_1 = SpectrumTrace((1e6, 2e6), (-10.0, -20.0), datetime.now(timezone.utc), "TRAC1")
            trace_2 = SpectrumTrace((1e6, 2e6), (0.0, -20.0), datetime.now(timezone.utc), "TRAC1")
            anritsu.start_averaging()
            anritsu._result("fetch_trace", trace_1)
            anritsu._result("fetch_trace", trace_2)
            self.assertIs(anritsu._latest_trace, trace_2)
            self.assertIsNotNone(anritsu._averaged_trace)
            self.assertNotEqual(anritsu._averaged_trace.powers_dbm[0], -5.0)
            anritsu.capture_current_reference()
            self.assertIs(anritsu._reference_trace, trace_2)
            anritsu.reference_operation.setCurrentIndex(1)
            anritsu._refresh_spectrum_display()
            self.assertGreater(anritsu.spectrum_plot.trace_point_count("Processed"), 0)
            anritsu.remove_reference()
            self.assertIsNone(anritsu._reference_trace)
            self.assertIs(anritsu._latest_trace, trace_2)
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_workspace_and_event_log_are_user_resizable(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            splitter = window.anritsu_page.workspace_splitter
            self.assertEqual(splitter.orientation(), Qt.Orientation.Horizontal)
            self.assertEqual(splitter.count(), 2)
            self.assertGreater(splitter.widget(0).minimumWidth(), 0)
            self.assertGreater(window.log.maximumHeight(), 10000)
            self.assertTrue(
                window.event_log_dock.features()
                & window.event_log_dock.DockWidgetFeature.DockWidgetMovable
            )
            self.assertIsNotNone(window.event_log_dock.toggleViewAction())
        finally:
            window.close()
            self.application.processEvents()

    def test_limit_edit_button_opens_popup_and_saves_the_range(self) -> None:
        source = Path(".config/settings.yml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            path.write_text(source, encoding="utf-8")
            window = MainWindow(path, simulation=False)
            try:
                field = window.keithley_page._limit_fields["level"]
                self.assertTrue(field.edit_button.isEnabled())

                def complete_dialog() -> None:
                    dialog = QApplication.activeModalWidget()
                    self.assertIsInstance(dialog, LimitEditDialog)
                    dialog.minimum.setText("0 A")
                    dialog.maximum.setText("9 mA")
                    dialog.accept()

                QTimer.singleShot(0, complete_dialog)
                field.edit_button.click()
                loaded = SettingsRepository(path).load()
                limits = loaded.raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
                self.assertEqual(limits["source_current"]["max"], "9 mA")
                self.assertEqual(field.maximum.text(), "MAX  9 mA")
                self.assertIsNot(window.tabs.currentWidget(), window.settings_page)
            finally:
                window.close()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
