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
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QScrollArea, QTreeWidgetItemIterator
from PySide6.QtTest import QTest

from app.domain.models import DeviceCapabilities
from app.devices.discovery import DiscoveredInstrument
from app.devices.anritsu import AnritsuConfigurationSnapshot, SpectrumTrace
from app.domain.quantities import DIMENSION_DBM, DIMENSION_FREQUENCY, parse_quantity
from app.settings.repository import SettingsRepository
from app.ui.main_window import LimitEditDialog, MainWindow
from tests.helpers import SETTINGS_TEMPLATE


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
        source = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
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
        source = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
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
                self.assertIn("VISA ASSIGN SUCCESS [rigol]", window.log.toPlainText())
            finally:
                window.close()
                self.application.processEvents()

    def test_discovery_row_assign_button_emits_selected_resource_immediately(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=False)
        try:
            page = window.dashboard
            result = DiscoveredInstrument(
                "GPIB0::22::INSTR",
                "system",
                "Keithley Instruments Inc., Model 2602A, 1291342, 2.1.6",
                "keithley",
            )
            emitted: list[object] = []
            page.assignments_requested.disconnect(window._save_discovered_assignments)
            page.assignments_requested.connect(emitted.append)
            page._scan_completed((result,))
            button = page.discovery_table.cellWidget(0, 1)
            self.assertIsInstance(button, QPushButton)
            button.click()
            self.assertEqual(
                emitted,
                [{"keithley": ("GPIB0::22::INSTR", "system", result.idn)}],
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_discovery_marks_persisted_resource_assigned_and_disables_duplicate(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=False)
        try:
            configured = window._settings.rigol.connection
            result = DiscoveredInstrument(
                str(configured.resource),
                configured.visa_backend,
                "RIGOL TECHNOLOGIES,DG1032Z,DG1ZA172902039,1",
                "rigol",
            )
            window.dashboard._scan_completed((result,))
            badge = window.dashboard.discovery_table.cellWidget(0, 0)
            button = window.dashboard.discovery_table.cellWidget(0, 1)
            self.assertIsInstance(badge, QLabel)
            self.assertIn("Assigned to Rigol", badge.text())
            self.assertIsInstance(button, QPushButton)
            self.assertFalse(button.isEnabled())
            self.assertEqual(button.text(), "Assigned ✓")
            self.assertFalse(window.dashboard.save_assignments.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_can_be_assigned_directly_from_top_card_after_scan(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=False)
        try:
            page = window.dashboard
            page.assignments_requested.disconnect(window._save_discovered_assignments)
            emitted: list[object] = []
            page.assignments_requested.connect(emitted.append)
            result = DiscoveredInstrument(
                "GPIB0::23::INSTR",
                "system",
                "ANRITSU,MS2830A,6201514799,7.03.00",
                "anritsu",
            )
            page._scan_completed((result,))
            card = page.cards["anritsu"]
            self.assertTrue(card.detected_resources.isEnabled())
            self.assertIn("GPIB0::23::INSTR", card.detected_resources.currentText())
            self.assertFalse(card.test_button.isEnabled())
            self.assertFalse(card.connect_button.isEnabled())
            self.assertIn("not active", card.assignment_hint.text())
            card.assign_button.click()
            self.assertEqual(
                emitted,
                [{"anritsu": ("GPIB0::23::INSTR", "system", result.idn)}],
            )
            self.assertIn("VISA ASSIGN CLICK [anritsu]", window.log.toPlainText())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_test_unlocks_only_after_selected_resource_is_saved(self) -> None:
        source = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            path.write_text(source, encoding="utf-8")
            window = MainWindow(path, simulation=False)
            try:
                result = DiscoveredInstrument(
                    "GPIB0::23::INSTR",
                    "system",
                    "ANRITSU,MS2830A,6201514799,7.03.00",
                    "anritsu",
                )
                window.dashboard._scan_completed((result,))
                card = window.dashboard.cards["anritsu"]
                self.assertFalse(card.test_button.isEnabled())
                with patch.object(
                    QMessageBox, "question", return_value=int(QMessageBox.StandardButton.Yes)
                ):
                    card.assign_button.click()
                QTest.qWait(100)
                self.application.processEvents()

                self.assertIn("GPIB0::23::INSTR", card.resource.text())
                self.assertFalse(card.detected_resources.isEnabled())
                self.assertEqual(card.assign_button.text(), "Assigned ✓")
                self.assertTrue(card.test_button.isEnabled())
                self.assertEqual(
                    SettingsRepository(path).load().settings.anritsu.connection.resource,
                    "GPIB0::23::INSTR",
                )
                self.assertIn("VISA ASSIGN SUCCESS [anritsu]", window.log.toPlainText())
            finally:
                window.close()
                self.application.processEvents()

    def test_device_card_communication_test_reports_protocol_and_disconnects(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            card = window.dashboard.cards["keithley"]
            card.test_button.click()
            QTest.qWait(150)
            self.application.processEvents()
            self.assertTrue(card.test_button.isEnabled())
            self.assertEqual(card.test_button.text(), "Test")
            self.assertIn("TEST PASS", card.identity.text())
            self.assertIn("KEITHLEY", card.identity.text().upper())
            self.assertEqual(window._controllers["keithley"]._worker._adapter.state.value, "disconnected")
        finally:
            window.close()
            self.application.processEvents()

    def test_top_ribbon_replaces_side_tabs_and_status_lives_in_menu_corner(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertTrue(window.tabs.tabBar().isHidden())
            self.assertEqual([action.text() for action in window.ribbon_actions], [
                "Dashboard", "Rigol", "Keithley", "Anritsu", "Recipes", "Execution", "Results", "Settings"
            ])
            self.assertTrue(all(not action.icon().isNull() for action in window.ribbon_actions))
            window.ribbon_actions[2].trigger()
            self.application.processEvents()
            self.assertEqual(window.tabs.currentIndex(), 2)
            self.assertTrue(window.ribbon_actions[2].isChecked())
            self.assertIs(window.menuBar().cornerWidget(Qt.Corner.TopRightCorner), window.menu_status_area)
            stop = window.menu_status_area.findChild(QPushButton, "compactEmergencyButton")
            self.assertIsNotNone(stop)
            self.assertLessEqual(stop.maximumWidth(), 74)
        finally:
            window.close()
            self.application.processEvents()

    def test_limit_edit_is_autosaved_after_short_idle_period(self) -> None:
        source = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
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
            self.assertNotIn("sweep_points3", anritsu._limit_fields)
            self.assertGreater(anritsu.points.count(), 1)
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
            self.assertEqual(keithley.output_toggle.text(), "OUTPUT OFF")

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
            output_actions = [keithley.channel_cards[channel]["output_action"] for channel in ("A", "B")]
            self.assertTrue(all(button.text() == "OUTPUT OFF" for button in output_actions))
            self.assertTrue(all(button.toolTip() for button in output_actions))
            self.assertTrue(keithley.workspace_splitter.toolTip())
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_keeps_independent_channel_histories_and_plots_dc_resistance(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._update_channel_measurement(
                SimpleNamespace(
                    channel="A", voltage_v=-2.0, current_a=0.5,
                    power_w=-1.0, compliance_detected=False,
                )
            )
            keithley._update_channel_measurement(
                SimpleNamespace(
                    channel="B", voltage_v=3.0, current_a=-0.25,
                    power_w=-0.75, compliance_detected=False,
                )
            )

            self.assertEqual(len(keithley._measurement_history["A"]), 1)
            self.assertEqual(len(keithley._measurement_history["B"]), 1)
            self.assertEqual(keithley._measurement_history["A"][0]["resistance"], 4.0)
            self.assertEqual(keithley._measurement_history["B"][0]["resistance"], 12.0)
            self.assertEqual(
                keithley.history_widgets["A"]["plot"].trace_point_count("CH A Resistance"), 1
            )
            self.assertEqual(
                keithley.history_widgets["B"]["plot"].trace_point_count("CH B Resistance"), 1
            )

            keithley._clear_keithley_history("A")
            self.assertEqual(keithley._measurement_history["A"], [])
            self.assertEqual(len(keithley._measurement_history["B"]), 1)
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_history_is_a_rolling_thirty_second_window(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._history_started_at = 100.0
            measurement = SimpleNamespace(
                channel="A", voltage_v=1.0, current_a=0.1,
                power_w=0.1, compliance_detected=False,
            )
            with patch("app.ui.main_window.time.monotonic", side_effect=[110.0, 145.0]):
                keithley._update_channel_measurement(measurement)
                keithley._update_channel_measurement(measurement)

            self.assertEqual(len(keithley._measurement_history["A"]), 1)
            self.assertEqual(keithley._measurement_history["A"][0]["elapsed_s"], 45.0)
            self.assertEqual(
                keithley.history_widgets["A"]["plot"].trace_point_count("CH A Resistance"), 1
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_compact_workspace_keeps_controls_and_both_plots_visible(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            window.resize(1600, 900)
            window.show()
            self.application.processEvents()

            self.assertEqual(keithley.workspace_splitter.count(), 2)
            self.assertLessEqual(keithley.channel_cards["A"]["card"].maximumHeight(), 160)
            self.assertLessEqual(keithley.channel_cards["B"]["card"].maximumHeight(), 160)
            self.assertTrue(keithley.history_widgets["A"]["plot"].isVisible())
            self.assertTrue(keithley.history_widgets["B"]["plot"].isVisible())
            self.assertTrue(keithley.level.isVisible())
            self.assertTrue(keithley.compliance.isVisible())
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_output_switch_runs_configure_unlock_and_enable_sequence(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._output_prerequisites = Mock(return_value=(True, ["✓ ready"]))

            with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes):
                keithley._output_toggled(True)
            self.assertEqual(keithley._controller.call.call_args.args[0], "configure")

            keithley._result("configure", None)
            self.assertEqual(keithley._controller.call.call_args.args, ("arm", "B"))

            keithley._result("arm", 123.0)
            self.assertEqual(keithley._controller.call.call_args.args, ("set_output", ("B", True)))

            keithley._result("set_output", None)
            self.assertTrue(keithley._output_states["B"])
            self.assertTrue(keithley.output_toggle.isChecked())
            self.assertEqual(keithley.output_toggle.text(), "OUTPUT ON")
            self.assertEqual(keithley.channel_cards["B"]["output_action"].text(), "OUTPUT ON")

            keithley._request_channel_output("B")
            self.assertEqual(keithley._controller.call.call_args.args, ("ramp_to_zero", "B"))
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

    def test_anritsu_temporal_average_uses_passive_reads_and_updates_at_target(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu.average_count.setValue(2)
            first = SpectrumTrace(
                (1e6, 2e6), (-10.0, -20.0), datetime.now(timezone.utc), "TRAC1"
            )
            second = SpectrumTrace(
                (1e6, 2e6), (0.0, -20.0), datetime.now(timezone.utc), "TRAC1"
            )

            anritsu.start_averaging()

            anritsu._controller.call.assert_called_once_with("fetch_current_trace", "TRAC1")
            self.assertEqual(anritsu.average_progress.format(), "0 / 2")
            anritsu._result("fetch_current_trace", first)
            self.assertEqual(anritsu.average_progress.value(), 1)
            self.assertEqual(anritsu.average_progress.format(), "1 / 2")
            self.assertIsNone(anritsu._averaged_trace)

            anritsu._result("fetch_current_trace", second)
            self.assertEqual(anritsu.average_progress.value(), 2)
            self.assertEqual(anritsu.average_progress.format(), "2 / 2")
            self.assertIsNotNone(anritsu._averaged_trace)
            self.assertFalse(anritsu._averaging_active)
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_live_uses_passive_polling_and_reports_frozen_frames(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            self.assertEqual(anritsu.refresh.minimum(), 10)

            anritsu.toggle_live()

            anritsu._controller.call.assert_called_once_with("start_live", False)
            self.assertTrue(anritsu._live_transition_pending)
            self.assertEqual(anritsu.live_indicator.property("liveState"), "starting")
            anritsu.toggle_live()
            anritsu._controller.call.assert_called_once_with("start_live", False)
            snapshot = AnritsuConfigurationSnapshot(1e6, 2e6, 0.0, 101, "SPECT")
            anritsu._result("start_live", snapshot)
            self.assertFalse(anritsu._live_transition_pending)
            self.assertFalse(anritsu.single.isEnabled())
            self.assertEqual(anritsu.live_indicator.property("liveState"), "on")
            trace = SpectrumTrace(
                (1e6, 2e6), (-50.0, -40.0), datetime.now(timezone.utc), "TRAC1"
            )
            for _ in range(4):
                anritsu._result("fetch_current_trace", trace)

            self.assertEqual(anritsu._live_frame_count, 4)
            self.assertIn("FRAME 4", anritsu.live_indicator.text())
            self.assertEqual(anritsu._identical_live_frames, 3)
            self.assertIn("unchanged ×3", anritsu.info.text())
            self.assertFalse(anritsu.banner.isHidden())
            anritsu._controller.call.reset_mock()
            anritsu.toggle_live()
            anritsu._controller.call.assert_called_once_with("stop_live")
            self.assertEqual(anritsu.live_indicator.property("liveState"), "stopping")
            anritsu._result("stop_live", None)
            self.assertTrue(anritsu.single.isEnabled())
            self.assertEqual(anritsu.live.text(), "Start Live")
            self.assertEqual(anritsu.live_indicator.property("liveState"), "off")
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_reference_is_temporally_averaged_before_display(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu.average_count.setValue(2)
            first = SpectrumTrace(
                (1e6, 2e6), (-10.0, -20.0), datetime.now(timezone.utc), "TRAC1"
            )
            second = SpectrumTrace(
                (1e6, 2e6), (0.0, -20.0), datetime.now(timezone.utc), "TRAC1"
            )

            anritsu.start_reference_averaging()
            anritsu._result("fetch_current_trace", first)
            self.assertIsNone(anritsu._reference_trace)
            anritsu._result("fetch_current_trace", second)

            self.assertIsNotNone(anritsu._reference_trace)
            self.assertTrue(anritsu.show_reference.isChecked())
            self.assertTrue(anritsu.clear_reference.isEnabled())
            self.assertIn("REFAVG2", anritsu._reference_trace.trace_name)
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_read_configuration_populates_form_without_applying(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu.read_configuration.click()
            anritsu._controller.call.assert_called_once_with("read_configuration")

            snapshot = AnritsuConfigurationSnapshot(2e6, 3e9, -15.5, 2001)
            anritsu._result("read_configuration", snapshot)

            self.assertEqual(parse_quantity(anritsu.start.text(), DIMENSION_FREQUENCY).si_value, 2e6)
            self.assertEqual(parse_quantity(anritsu.stop.text(), DIMENSION_FREQUENCY).si_value, 3e9)
            self.assertEqual(parse_quantity(anritsu.reference.text(), DIMENSION_DBM).si_value, -15.5)
            self.assertEqual(anritsu.points.currentData(), 2001)
            self.assertEqual(
                [anritsu.points.itemData(index) for index in range(anritsu.points.count())],
                [101, 201, 251, 401, 501, 1001, 2001, 5001, 10001],
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_hardware_options_update_documented_limits_card(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu.set_capabilities(
                DeviceCapabilities(
                    device_name="anritsu",
                    model="MS2830A",
                    firmware="7.03",
                    features=frozenset({"spectrum_trace", "live_trace"}),
                    hardware_options=("041", "008"),
                )
            )

            self.assertIn("041", anritsu.hardware_option_info.text())
            self.assertIn("Preamplifier", anritsu.hardware_option_info.text())
            self.assertIn("6.1 GHz", anritsu.hardware_range_info.text())
            self.assertIn("2 ms", anritsu.hardware_range_info.text())
            self.assertIn("RBW: 1 Hz to 31.25 MHz", anritsu.hardware_range_info.text())
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
        source = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
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
