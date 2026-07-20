from __future__ import annotations

import os
import inspect
import math
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QScrollArea, QTabWidget, QTreeWidgetItemIterator
from PySide6.QtTest import QTest

from app.domain.models import DeviceCapabilities
from app.devices.discovery import DiscoveredInstrument
from app.devices.moke_box.models import MokeHallVoltageReading
from app.devices.anritsu_ms2830a import (
    AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot,
    SpectrumTrace,
)
from app.domain.quantities import DIMENSION_DBM, DIMENSION_FREQUENCY, parse_quantity
from app.settings.repository import SettingsRepository
from app.devices.anritsu_ms2830a.ui import AnritsuPageState
from app.ui.shell import MainWindow
from app.ui.dashboard.device_card import DeviceCard
from app.ui.design_system import tokens_for
from qfluentwidgets import CardWidget, ComboBox, PlainTextEdit
from app.ui.widgets import LimitEditDialog
from tests.helpers import SETTINGS_TEMPLATE


TEST_ENGINEER = "LAB\\test-engineer"
TEST_SERVICE = "LAB\\test-service"


def synthetic_anritsu_peaks(
    *, primary_hz: float = 1.0e9
) -> SpectrumTrace:
    frequencies = tuple(990e6 + index * 50e3 for index in range(401))
    baseline_mw = 10.0 ** (-100.0 / 10.0)
    peaks = (
        (primary_hz, 10.0 ** (-30.0 / 10.0), 800e3),
        (1.006e9, 10.0 ** (-38.0 / 10.0), 500e3),
    )
    powers = []
    for frequency in frequencies:
        power_mw = baseline_mw
        for center_hz, amplitude_mw, fwhm_hz in peaks:
            power_mw += amplitude_mw * math.exp(
                -4.0 * math.log(2.0) * ((frequency - center_hz) / fwhm_hz) ** 2
            )
        powers.append(10.0 * math.log10(power_mw))
    return SpectrumTrace(
        frequencies,
        tuple(powers),
        datetime.now(timezone.utc),
        "TRAC1",
    )


def write_engineer_settings(path: Path) -> None:
    path.write_text(SETTINGS_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    repository = SettingsRepository(path)
    raw = repository.load().raw
    raw["access_control"]["user_roles"][TEST_ENGINEER] = ["engineer"]
    repository.save_raw(raw)


def write_service_settings(path: Path) -> None:
    path.write_text(SETTINGS_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    repository = SettingsRepository(path)
    raw = repository.load().raw
    raw["access_control"]["user_roles"][TEST_SERVICE] = ["service"]
    repository.save_raw(raw)


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
            self.assertTrue(rigol.advanced.isTabEnabled(0))

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

    def test_audit_failure_blocks_energy_but_never_output_off(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window._audit_healthy = False
            with self.assertRaisesRegex(Exception, "audit log is unavailable"):
                window._guard_manual_operation("arm", 1)
            with self.assertRaisesRegex(Exception, "audit log is unavailable"):
                window._guard_manual_operation("set_output", (1, True))
            with self.assertRaisesRegex(Exception, "audit log is unavailable"):
                window._guard_manual_operation("ramp_to_level", object())
            with self.assertRaisesRegex(Exception, "audit log is unavailable"):
                window._guard_manual_operation("arm_signal_generator", None)
            with self.assertRaisesRegex(Exception, "audit log is unavailable"):
                window._guard_manual_operation("set_signal_generator_output", True)
            window._guard_manual_operation("set_output", (1, False))
            window._guard_manual_operation("set_signal_generator_output", False)
            window._guard_manual_operation("emergency_off", None)
        finally:
            window.close()
            self.application.processEvents()

    def test_estop_uses_out_of_band_sessions_without_an_active_recipe(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertFalse(window._run_controller.running)
            window._run_controller.request_emergency_stop = Mock()
            for controller in window._controllers.values():
                controller.call = Mock()

            with patch.object(
                QMessageBox,
                "warning",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window._emergency_off_all()

            window._run_controller.request_emergency_stop.assert_called_once_with(
                window._settings,
                simulation=True,
            )
            for controller in window._controllers.values():
                controller.call.assert_called_once_with("emergency_off")
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
            self.assertEqual(settings.tabs.count(), 9)
            self.assertEqual(
                [settings.tabs.tabText(index) for index in range(9)],
                [
                    "General",
                    "Rigol",
                    "Keithley",
                    "Anritsu",
                    "MOKE Box",
                    "Lake Shore 475",
                    "Safety limits",
                    "Access roles",
                    "Diagnostics",
                ],
            )
            for name in ("general", "rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"):
                self.assertGreater(settings.trees[name].topLevelItemCount(), 0)
            self.assertGreater(settings.limits_table.rowCount(), 10)
        finally:
            window.close()
            self.application.processEvents()

    def test_run_monitor_shows_current_node_measurements_storage_and_spectrum(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            monitor = window.run_monitor
            monitor.run_started(4, 2.0)
            monitor.append_event(
                "action_started",
                {
                    "node_id": "measure-b",
                    "kind": "measure_keithley",
                    "action_index": 1,
                    "setpoints_si": {"keithley.B.current": 0.001},
                },
            )
            monitor.append_event(
                "point_stored",
                {
                    "stored_points": 1,
                    "measurements_si": {"keithley.B.voltage_v": 0.067},
                    "write_elapsed_s": 0.012,
                    "average_write_rate_points_per_s": 2.5,
                    "spectrum_points": 101,
                },
            )
            monitor.update_spectrum_preview(
                {
                    "point_index": 0,
                    "frequency_hz": (1e6, 2e6),
                    "power_dbm": (-70.0, -71.0),
                    "source_points": 101,
                }
            )
            self.assertIn("measure-b", monitor.current_path.text())
            self.assertIn("0.067", monitor.current_measurements.text())
            self.assertIn("12.0 ms", monitor.storage_rate.text())
            self.assertEqual(monitor.spectrum_preview.trace_point_count("Stored spectrum"), 2)
        finally:
            window.close()
            self.application.processEvents()

    def test_safety_limit_table_has_min_max_columns_and_syncs_with_settings_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                settings = window.settings_page
                target_row = next(
                    row
                    for row in range(settings.limits_table.rowCount())
                    if "rigol" in settings.limits_table.item(row, 0).text().lower()
                    and "1" in settings.limits_table.item(row, 0).text()
                    and settings.limits_table.item(row, 1).text() == "frequency"
                )
                maximum = settings.limits_table.item(target_row, 3)
                path = tuple(maximum.data(Qt.ItemDataRole.UserRole))
                maximum.setText("99 MHz")
                self.application.processEvents()
                draft = settings._apply_tree_values()
                self.assertEqual(settings._get_path(draft, path), "99 MHz")

                iterator = QTreeWidgetItemIterator(settings.trees["rigol"])
                matched = False
                while iterator.value() is not None:
                    item = iterator.value()
                    if tuple(item.data(0, Qt.ItemDataRole.UserRole) or ()) == path:
                        self.assertEqual(item.text(1), "99 MHz")
                        matched = True
                        break
                    iterator += 1
                self.assertTrue(matched)
            finally:
                window.settings_page._autosave_timer.stop()
                window.close()
                self.application.processEvents()

    def test_settings_use_choice_editors_and_engineers_can_manage_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(path, simulation=False, authenticated_username=TEST_ENGINEER)
            try:
                page = window.settings_page
                editor = page.editor_for_path(("devices", "rigol", "enabled"))
                self.assertIsInstance(editor, ComboBox)
                self.assertEqual(editor.currentText(), "Yes")
                self.assertTrue(page.add_role_button.isEnabled())
                self.assertTrue(page.edit_role_button.isEnabled())
            finally:
                window.close()
                self.application.processEvents()

    def test_invalid_safety_limit_is_highlighted_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(path, simulation=False, authenticated_username=TEST_ENGINEER)
            try:
                page = window.settings_page
                row = next(
                    row
                    for row in range(page.limits_table.rowCount())
                    if page.limits_table.item(row, 1).text() == "frequency"
                )
                page.limits_table.item(row, 2).setText("10 GHz")
                page.limits_table.item(row, 3).setText("1 Hz")
                with patch.object(QMessageBox, "critical"):
                    self.assertIsNone(page.validate_draft())
                self.assertIn(page.limits_table.item(row, 2), page._limit_error_items)
                self.assertIn(page.limits_table.item(row, 3), page._limit_error_items)
            finally:
                window.close()
                self.application.processEvents()

    def test_anritsu_acquisition_error_highlights_missing_frequency_and_reference_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(path, simulation=False, authenticated_username=TEST_ENGINEER)
            try:
                page = window.settings_page
                safety = page._raw["devices"]["anritsu"]["safety"]
                safety["acquisition_allowed"] = True
                safety["frequency"] = {"min": None, "max": None}
                safety["reference_level"] = {"min": None, "max": None}
                page._populate()
                with patch.object(QMessageBox, "critical"):
                    self.assertIsNone(page.validate_draft())
                expected = {
                    ("devices", "anritsu", "safety", "frequency", boundary)
                    for boundary in ("min", "max")
                } | {
                    ("devices", "anritsu", "safety", "reference_level", boundary)
                    for boundary in ("min", "max")
                }
                self.assertTrue(expected.issubset(set(page._limit_items_by_path)))
                self.assertTrue(
                    all(page._limit_items_by_path[item] in page._limit_error_items for item in expected)
                )
                self.assertTrue(
                    all(
                        page._limit_items_by_path[item].data(
                            int(Qt.ItemDataRole.UserRole) + 101
                        )
                        for item in expected
                    )
                )
                self.assertIs(page.tabs.currentWidget(), page.limits_page)
            finally:
                window.close()
                self.application.processEvents()

    def test_safety_limit_cell_rejects_wrong_unit_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(path, simulation=False, authenticated_username=TEST_ENGINEER)
            try:
                page = window.settings_page
                row = next(
                    row
                    for row in range(page.limits_table.rowCount())
                    if page.limits_table.item(row, 1).text() == "frequency"
                )
                item = page.limits_table.item(row, 2)
                item.setText("5 V")
                self.assertIn(item, page._limit_error_items)
                self.assertIn("frequency", item.toolTip().lower())
                self.assertTrue(item.data(int(Qt.ItemDataRole.UserRole) + 101))
                editor = page._safety_limit_editors[tuple(item.data(Qt.ItemDataRole.UserRole))]
                self.assertEqual(editor.property("validationState"), "error")
                self.assertFalse(page._safety_limit_error_labels[tuple(item.data(Qt.ItemDataRole.UserRole))].isHidden())
                self.assertFalse(page.limits_validation_banner.isHidden())
                item.setText("6 V")
                self.assertIn(item, page._limit_error_items)
                item.setText("5 MHz")
                self.assertNotIn(item, page._limit_error_items)
            finally:
                window.close()
                self.application.processEvents()

    def test_operator_role_is_visible_and_cannot_edit_or_assign_station_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.yml"
            settings_path.write_text(
                SETTINGS_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
            )
            window = MainWindow(
                settings_path,
                simulation=False,
                authenticated_username="LAB\\operator",
            )
            try:
                self.assertIn("LAB\\operator", window.safety_strip.actor.text())
                self.assertIn("operator", window.safety_strip.actor.text())
                # Operators may save only the dedicated Rigol manual-output
                # permission; general station configuration stays read-only.
                self.assertTrue(window.settings_page.save_button.isEnabled())
                self.assertFalse(window.settings_page.approve_button.isEnabled())
                rigol_enabled = None
                iterator = QTreeWidgetItemIterator(
                    window.settings_page.trees["rigol"]
                )
                while iterator.value() is not None:
                    candidate = iterator.value()
                    if tuple(
                        candidate.data(0, Qt.ItemDataRole.UserRole) or ()
                    ) == ("devices", "rigol", "enabled"):
                        rigol_enabled = candidate
                        break
                    iterator += 1
                self.assertIsNotNone(rigol_enabled)
                self.assertFalse(
                    rigol_enabled.flags() & Qt.ItemFlag.ItemIsEditable
                )
                self.assertFalse(
                    window.keithley_page._limit_fields["level"].edit_button.isEnabled()
                )
                denied_button = window.keithley_page._limit_fields[
                    "level"
                ].edit_button
                self.assertIn("current role(s) operator", denied_button.toolTip())
                self.assertIn("engineer or service", denied_button.toolTip())
                self.assertIn(
                    "fixed by the instrument",
                    window.keithley_page._limit_fields[
                        "nplc"
                    ].edit_button.toolTip(),
                )
                self.assertFalse(window.dashboard.save_assignments.isEnabled())
                with patch.object(QMessageBox, "warning") as warning:
                    window._save_discovered_assignments(
                        {
                            "rigol": (
                                "USB0::DENIED::INSTR",
                                "system",
                                "RIGOL TECHNOLOGIES,DG1032Z,DENIED,1",
                            )
                        }
                    )
                warning.assert_called_once()
                self.assertIsNone(
                    SettingsRepository(settings_path).load().settings.rigol.connection.resource
                )
                self.assertIn("VISA ASSIGN REJECTED", window.log.toPlainText())
            finally:
                window.close()
                self.application.processEvents()

    def test_engineer_approval_is_bound_to_authenticated_os_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.yml"
            write_engineer_settings(settings_path)
            window = MainWindow(
                settings_path,
                simulation=False,
                authenticated_username=TEST_ENGINEER,
            )
            try:
                self.assertTrue(window.settings_page.save_button.isEnabled())
                self.assertTrue(window.settings_page.approve_button.isEnabled())
                self.assertTrue(window.settings_page.add_role_button.isEnabled())
                phrase = f"APPROVE {window._settings.profile.id}"
                with patch(
                    "app.ui.settings_page.QInputDialog.getText",
                    return_value=(phrase, True),
                ):
                    window.settings_page.approve_profile()
                loaded = SettingsRepository(settings_path).load().settings
                self.assertEqual(loaded.profile.state, "approved")
                self.assertEqual(loaded.profile.approved_by, TEST_ENGINEER)
                self.assertIn("authenticated engineer", loaded.profile.approval_note)
            finally:
                window.close()
                self.application.processEvents()

    def test_operator_can_edit_only_rigol_manual_output_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.yml"
            settings_path.write_text(
                SETTINGS_TEMPLATE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            window = MainWindow(
                settings_path,
                simulation=False,
                authenticated_username="LAB\\operator",
            )
            try:
                page = window.settings_page
                self.assertTrue(page.save_button.isEnabled())
                target = None
                iterator = QTreeWidgetItemIterator(page.trees["rigol"])
                while iterator.value() is not None:
                    item = iterator.value()
                    if tuple(
                        item.data(0, Qt.ItemDataRole.UserRole) or ()
                    ) == (
                        "devices",
                        "rigol",
                        "safety",
                        "allow_output_enable",
                    ):
                        target = item
                        break
                    iterator += 1
                self.assertIsNotNone(target)
                self.assertTrue(target.flags() & Qt.ItemFlag.ItemIsEditable)
                target.setText(1, "true")
                self.application.processEvents()
                self.assertTrue(page.save_draft())
                self.assertTrue(
                    SettingsRepository(settings_path)
                    .load()
                    .settings
                    .rigol
                    .safety
                    .allow_output_enable
                )
            finally:
                window.close()
                self.application.processEvents()

    def test_service_role_can_manage_explicit_os_role_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.yml"
            write_service_settings(settings_path)
            window = MainWindow(
                settings_path,
                simulation=False,
                authenticated_username=TEST_SERVICE,
            )
            try:
                page = window.settings_page
                self.assertTrue(page.add_role_button.isEnabled())
                page._upsert_role_assignment("LAB\\new-engineer", ("engineer",))
                self.assertTrue(page._dirty)
                self.assertTrue(page.save_draft())
                assignments = SettingsRepository(settings_path).load().settings.access_control.user_roles
                self.assertEqual(assignments["LAB\\new-engineer"], ("engineer",))
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
            for label in ("OUTPUT ON", "OUTPUT OFF"):
                self.assertIn(label, buttons)
                self.assertTrue(buttons[label].toolTip())
            self.assertNotIn("ARM (30 s)", buttons)
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
                self.application.setProperty("stationAppliedTheme", None)
                with patch(
                    "app.ui.shell.main_window.apply_application_theme",
                    wraps=__import__(
                        "app.ui.shell.main_window", fromlist=["apply_application_theme"]
                    ).apply_application_theme,
                ) as apply_theme:
                    window.theme_actions["light"].trigger()
                    self.application.processEvents()
                    self.assertEqual(SettingsRepository(path).load().raw["ui"]["theme"], "light")
                    window.theme_actions["dark"].trigger()
                    self.application.processEvents()
                    self.assertEqual(SettingsRepository(path).load().raw["ui"]["theme"], "dark")
                    window.theme_actions["system"].trigger()
                    self.application.processEvents()
                self.assertEqual(apply_theme.call_count, 3)
                self.assertEqual(SettingsRepository(path).load().raw["ui"]["theme"], "system")
                self.assertEqual(themes[:2], ["light", "dark"])
                self.assertIn(themes[-1], {"light", "dark"})
            finally:
                window.close()
                self.application.processEvents()

    def test_discovered_assignment_updates_card_and_worker_adapter_before_connect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
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
                self.assertTrue(
                    window.connection_panels["rigol"].connect_button.isEnabled()
                )
                worker_adapter = window._controllers["rigol"]._worker._adapter
                self.assertEqual(worker_adapter._settings.connection.resource, resource)
                self.assertIn("VISA ASSIGN SUCCESS [rigol]", window.log.toPlainText())
            finally:
                window.close()
                self.application.processEvents()

    def test_find_visa_uses_result_cards_and_preserves_assignment_payload(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
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
            self.assertEqual(len(page.visa_results.rows), 1)
            row = page.visa_results.rows[0]
            row.assignment.setCurrentIndex(row.assignment.findData("keithley"))
            row.assign_button.click()
            self.assertEqual(
                emitted,
                [{"keithley": ("GPIB0::22::INSTR", "system", result.idn)}],
            )
        finally:
            window.close()

    def test_event_log_can_filter_and_copy_exact_instrument_tx_rx(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window._log("ordinary application status")
            window._log("LAKESHORE_GAUSSMETER VISA TX QUERY 'UNIT?'")
            window._log("LAKESHORE_GAUSSMETER VISA RX 'UNIT?' after 2.0 ms: '1'")

            window.traffic_only_button.setChecked(True)
            self.application.processEvents()

            visible = window.log.toPlainText()
            self.assertNotIn("ordinary application status", visible)
            self.assertIn("TX QUERY 'UNIT?'", visible)
            self.assertIn("VISA RX", visible)
            window.copy_traffic_button.click()
            self.assertEqual(QApplication.clipboard().text(), visible)
        finally:
            window.close()
            self.application.processEvents()

    def test_find_visa_scan_states_are_explicit(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            page = window.dashboard
            self.assertEqual(page.visa_state, "empty")
            page._scan_completed(())
            self.assertEqual(page.visa_state, "empty")
            page._scan_failed("backend unavailable")
            self.assertEqual(page.visa_state, "failed")
            self.assertIn("backend unavailable", page.discovery_info.text())
        finally:
            window.close()
            self.application.processEvents()

    def test_dashboard_exposes_separate_overview_and_discovery_routes(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertEqual(tuple(window.dashboard.navigation_pages), ("overview", "discovery"))
            self.assertIs(
                window.navigation_routes["overview"].content,
                window.dashboard.navigation_pages["overview"],
            )
            self.assertIs(
                window.navigation_routes["discovery"].content,
                window.dashboard.navigation_pages["discovery"],
            )
            self.assertFalse(window.dashboard.findChildren(QTabWidget))
            window.resize(1360, 880)
            window.show()
            window._navigate_to("discovery")
            self.application.processEvents()
            discovery = window.dashboard.navigation_pages["discovery"]
            self.assertTrue(discovery.isVisible())
            self.assertGreater(discovery.width(), 600)
            self.assertTrue(window.dashboard.visa_results.isVisible())
            self.assertGreater(window.dashboard.visa_results.height(), 200)
        finally:
            window.close()
            self.application.processEvents()

    def test_instruments_are_visible_children_of_the_apparatus_navigation_group(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            self.application.processEvents()

            apparatus = window.navigationInterface.widget("apparatusMenu")
            self.assertIsNotNone(apparatus)
            self.assertTrue(apparatus.isVisible())
            self.assertGreater(apparatus.width(), 100)
            previous_bottom = apparatus.mapTo(window, QPoint()).y() + 36
            for route in (
                "rigol",
                "keithley",
                "anritsu",
                "moke_box",
                "lakeshore_gaussmeter",
            ):
                item = window.navigationInterface.widget(
                    window.navigation_routes[route].objectName()
                )
                self.assertIsNotNone(item)
                self.assertTrue(item.isVisible())
                self.assertIs(item.parent(), apparatus)
                self.assertGreater(item.geometry().height(), 0)
                item_top = item.mapTo(window, QPoint()).y()
                self.assertGreaterEqual(item_top, previous_bottom)
                previous_bottom = item_top + item.geometry().height()
        finally:
            window.close()
            self.application.processEvents()

    def test_shell_title_is_pylab_without_transient_status_strip(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.show()
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()

            self.assertEqual(window.windowTitle(), "PyLab")
            self.assertIsNone(window.findChild(QLabel, "shellStatusMessage"))
            self.assertIs(window.fluent_content.layout().itemAt(0).widget(), window.safety_strip)
            self.assertIs(window.fluent_content.layout().itemAt(1).widget(), window.shell_splitter)
            self.assertGreater(window.shell_splitter.handleWidth(), 0)
            self.assertIn("background: transparent", window.shell_splitter.styleSheet())
        finally:
            window.close()
            self.application.processEvents()

    def test_dashboard_uses_one_concrete_label_per_registered_device(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            registry = window._composition.registry
            for module in registry.all_modules():
                card = window.dashboard.cards[module.key]
                labels = card.findChildren(QLabel)
                self.assertTrue(
                    any(label.text() == module.display_name for label in labels),
                    module.implementation_key,
                )
        finally:
            window.close()
            self.application.processEvents()

    def test_overview_uses_fluent_cards_with_visible_actions_in_both_themes(self) -> None:
        source = inspect.getsource(DeviceCard)
        for legacy_type in ("QFrame", "QLabel", "QComboBox", "QPushButton"):
            self.assertNotIn(legacy_type, source)

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("overview")
            self.application.processEvents()
            cards = tuple(window.dashboard.cards.values())
            self.assertTrue(all(isinstance(card, CardWidget) for card in cards))
            self.assertTrue(
                all(
                    card.assign_button.isVisibleTo(window)
                    for device, card in window.dashboard.cards.items()
                    if device != "moke_box"
                )
            )
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            card = window.dashboard.cards["rigol"]
            point = card.mapTo(window, QPoint(5, 5))
            light = window.grab().toImage().pixelColor(point).name()
            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark = window.grab().toImage().pixelColor(point).name()
            self.assertNotEqual(light, dark)
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_live_theme_switch_rethemes_shell_and_visa_result_surfaces(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("discovery")
            result = DiscoveredInstrument(
                "GPIB0::22::INSTR",
                "system",
                "Keithley Instruments Inc., Model 2602A, 1291342, 2.1.6",
                "keithley",
            )
            window.dashboard._scan_completed((result,))
            self.application.processEvents()
            row = window.dashboard.visa_results.rows[0]
            row_point = row.mapTo(window, QPoint(5, 5))
            shell_point = window.fluent_content.mapTo(window, QPoint(6, 400))
            log_point = window.log.viewport().mapTo(
                window,
                QPoint(96, max(20, window.log.viewport().height() - 12)),
            )
            nav_point = window.navigationInterface.panel.mapTo(window, QPoint(12, 500))

            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark_image = window.grab().toImage()
            dark = {
                "shell": dark_image.pixelColor(shell_point).name(),
                "visa": dark_image.pixelColor(row_point).name(),
                "log": dark_image.pixelColor(log_point).name(),
                "nav": dark_image.pixelColor(nav_point).name(),
                "nav_qss": window.navigationInterface.panel.styleSheet(),
            }
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light_image = window.grab().toImage()
            light = {
                "shell": light_image.pixelColor(shell_point).name(),
                "visa": light_image.pixelColor(row_point).name(),
                "log": light_image.pixelColor(log_point).name(),
                "nav": light_image.pixelColor(nav_point).name(),
                "nav_qss": window.navigationInterface.panel.styleSheet(),
            }
            self.assertNotEqual(dark["shell"], light["shell"])
            self.assertNotEqual(dark["visa"], light["visa"])
            self.assertNotEqual(dark["log"], light["log"])
            self.assertNotEqual(dark["nav"], light["nav"])
            self.assertLess(dark_image.pixelColor(row_point).lightness(), 80)
            self.assertGreater(light_image.pixelColor(row_point).lightness(), 160)
            self.assertLess(dark_image.pixelColor(log_point).lightness(), 80)
            self.assertGreater(light_image.pixelColor(log_point).lightness(), 160)
            self.assertEqual(
                dark_image.pixelColor(log_point).name(),
                tokens_for("dark").surface_raised,
            )
            self.assertEqual(
                light_image.pixelColor(log_point).name(),
                tokens_for("light").surface_raised,
            )
            self.assertLess(dark_image.pixelColor(nav_point).lightness(), 80)
            self.assertGreater(light_image.pixelColor(nav_point).lightness(), 160)
            self.assertIn("rgb(32, 32, 32)", dark["nav_qss"])
            self.assertIn("rgb(243, 243, 243)", light["nav_qss"])
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_event_log_is_a_fluent_surface_after_live_theme_switch(self) -> None:
        """The fixed log area must never retain the previous theme's canvas."""
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._log("A rendered event log entry")
            self.application.processEvents()

            self.assertIsInstance(window.log, PlainTextEdit)
            self.assertEqual(window.event_log_panel.property("stationSurface"), "surface")
            self.assertEqual(window.log.property("stationSurface"), "raised")

            viewport_point = window.log.viewport().mapTo(
                window, QPoint(96, max(20, window.log.viewport().height() - 12))
            )
            observed: dict[str, str] = {}
            for theme in ("dark", "light"):
                window._set_theme_mode(theme, persist=False)
                self.application.processEvents()
                observed[theme] = window.grab().toImage().pixelColor(viewport_point).name()

            self.assertEqual(observed["dark"], tokens_for("dark").surface_raised)
            self.assertEqual(observed["light"], tokens_for("light").surface_raised)
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_discovery_marks_persisted_resource_assigned_and_disables_duplicate(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            configured = window._settings.rigol.connection
            result = DiscoveredInstrument(
                str(configured.resource),
                configured.visa_backend,
                "RIGOL TECHNOLOGIES,DG1032Z,DG1ZA172902039,1",
                "rigol",
            )
            window.dashboard._scan_completed((result,))
            row = window.dashboard.visa_results.rows[0]
            self.assertEqual(row.status.text(), "Assigned to rigol")
            self.assertFalse(row.assignment.isEnabled())
            self.assertFalse(row.assign_button.isEnabled())
            self.assertFalse(window.dashboard.save_assignments.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_can_be_assigned_directly_from_top_card_after_scan(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
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
            self.assertFalse(hasattr(card, "test_button"))
            self.assertFalse(hasattr(card, "connect_button"))
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
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                result = DiscoveredInstrument(
                    "GPIB0::23::INSTR",
                    "system",
                    "ANRITSU,MS2830A,6201514799,7.03.00",
                    "anritsu",
                )
                window.dashboard._scan_completed((result,))
                card = window.dashboard.cards["anritsu"]
                with patch.object(
                    QMessageBox, "question", return_value=int(QMessageBox.StandardButton.Yes)
                ):
                    card.assign_button.click()
                QTest.qWait(100)
                self.application.processEvents()

                self.assertIn("GPIB0::23::INSTR", card.resource.text())
                self.assertFalse(card.detected_resources.isEnabled())
                self.assertEqual(card.assign_button.text(), "Assigned ✓")
                self.assertTrue(
                    window.connection_panels["anritsu"].test_button.isEnabled()
                )
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
            panel = window.connection_panels["keithley"]
            panel.test_button.click()
            QTest.qWait(150)
            self.application.processEvents()
            self.assertTrue(panel.test_button.isEnabled())
            self.assertEqual(panel.test_button.text(), "Test")
            self.assertIn("TEST PASS", card.identity.text())
            self.assertIn("KEITHLEY", card.identity.text().upper())
            self.assertEqual(window._controllers["keithley"]._worker._adapter.state.value, "disconnected")
        finally:
            window.close()
            self.application.processEvents()

    def test_moke_page_exposes_read_only_full_value_overview(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            page = window.moke_box_page
            self.assertEqual(page.views.count(), 2)
            self.assertEqual(len(page.vout_values), 8)
            self.assertFalse(hasattr(page, "acquire_button"))
            self.assertFalse(page.read_vouts_button.isEnabled())
            self.assertEqual(page.field_samples.value(), 1)
            self.assertEqual(page.read_fields_button.text(), "Get Hall voltage (V)")

            page._state_changed("verified")
            self.assertTrue(page.read_vouts_button.isEnabled())
            page._result("read_vouts", {channel: channel / 10 for channel in range(8)})
            self.assertEqual(page.vout_values[0].text(), "+0.000000 V")
            self.assertEqual(page.vout_values[7].text(), "+0.700000 V")
            self.assertIn("VOUT-setting commands", page.safety_note.text())
        finally:
            window.close()
            self.application.processEvents()

    def test_moke_hall_live_window_tracks_page_reading_and_stops_for_run(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            page = window.moke_box_page
            page._state_changed("verified")
            page._open_hall_live_window()
            popup = page._hall_live_window
            self.assertIsNotNone(popup)
            self.assertTrue(popup.isVisible())

            reading = MokeHallVoltageReading(
                voltage_v=-0.099453926,
                stddev_v=0.0,
                samples=1,
                raw_codes=(0x7EBA1C,),
                timestamp_utc=datetime.now(timezone.utc),
            )
            page._show_hall_reading(reading)
            self.assertEqual(popup.voltage.text(), "-0.099454 V")
            self.assertEqual(popup.field.text(), "-99.454 mT")

            page.sample_interval.setCurrentIndex(0)
            self.assertEqual(popup.interval.value(), 500)
            page._live_timer.start()
            page.stop_live("Recipe run owns the MOKE Box.")
            self.assertFalse(page._live_timer.isActive())
            self.assertFalse(page.live_hall.isChecked())
            self.assertIn("Recipe run", popup.status.text())
        finally:
            window.close()
            self.application.processEvents()

    def test_known_moke_ip_can_be_tested_without_subnet_scan(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            page = window.dashboard
            page._discovery_enabled = True
            page.tcp_network.setText("131.246.221.33")
            page.tcp_port.setValue(10001)
            with patch.object(page, "_identify_selected_moke") as identify:
                page._test_entered_moke_ip()
            identify.assert_called_once_with()
            self.assertEqual(page.tcp_results.selected_endpoint, "131.246.221.33:10001")
            self.assertEqual(page.tcp_results.selected_state, "entered")
            page.tcp_results.upsert_endpoint(
                host="131.246.221.33",
                endpoint="131.246.221.33:10001",
                state="entered",
                verification="MOKE Box verified",
            )
            page._update_tcp_identify_enabled()
            self.assertTrue(page.tcp_assign_moke_button.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_verified_moke_assignment_persists_read_only_tcp_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.yml"
            write_engineer_settings(settings_path)
            window = MainWindow(
                settings_path,
                simulation=False,
                authenticated_username=TEST_ENGINEER,
            )
            try:
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    window._save_moke_assignment("131.246.221.33:10001")
                self.application.processEvents()
                profile = SettingsRepository(settings_path).load().settings.moke_box
                self.assertTrue(profile.enabled)
                self.assertTrue(profile.protocol_qualified)
                self.assertEqual(profile.endpoint, "131.246.221.33:10001")
                self.assertFalse(profile.allow_vout_control)
                self.assertEqual(profile.allowed_vout_channels, ())
            finally:
                window.close()
                self.application.processEvents()

    def test_verified_moke_assignment_upgrades_profile_without_moke_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.yml"
            write_engineer_settings(settings_path)
            repository = SettingsRepository(settings_path)
            raw = repository.load().raw
            raw["devices"].pop("moke_box", None)
            repository._atomic_dump(raw)
            window = MainWindow(
                settings_path,
                simulation=False,
                authenticated_username=TEST_ENGINEER,
            )
            try:
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ), patch.object(QMessageBox, "critical") as critical:
                    window._save_moke_assignment("131.246.221.33:10001")

                critical.assert_not_called()
                profile = SettingsRepository(settings_path).load().settings.devices.moke_box
                self.assertTrue(profile.enabled)
                self.assertEqual(profile.endpoint, "131.246.221.33:10001")
                self.assertTrue(profile.protocol_qualified)
            finally:
                window.close()
                self.application.processEvents()

    def test_moke_connection_failure_is_visible_in_panel_and_dialog(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            with patch.object(QMessageBox, "warning") as warning:
                window._device_error(
                    "moke_box", "connect", "MOKE endpoint did not answer"
                )
            panel = window.connection_panels["moke_box"]
            self.assertIn("CONNECTION FAILED", panel.summary.text())
            self.assertIn("did not answer", panel.summary.text())
            self.assertEqual(panel.state.text(), "FAULT")
            self.assertTrue(panel.connect_button.isEnabled())
            warning.assert_called_once()
        finally:
            window.close()
            self.application.processEvents()

    def test_lakeshore_page_is_read_only_and_guards_inflight_live_reads(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            page = window.lakeshore_gaussmeter_page
            page._controller.call = Mock()

            page._read()
            page._live_tick()

            page._controller.call.assert_called_once_with("read_measurement")
            self.assertFalse(page.read_now.isEnabled())
            self.assertFalse(
                any(
                    hasattr(page, name)
                    for name in (
                        "unit_selector",
                        "range_selector",
                        "autorange_control",
                        "mode_selector",
                    )
                )
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_limit_edit_is_autosaved_after_short_idle_period(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
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
            self.assertIn("outside the approved", rigol.banner.label.text())

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
            expected_anritsu_minimum = window._settings.anritsu.safety.frequency.min
            expected_text = "NOT SET" if expected_anritsu_minimum is None else expected_anritsu_minimum
            self.assertEqual(anritsu._limit_fields["frequency0"].minimum.text(), f"MIN  {expected_text}")
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
            with patch(
                "app.devices.keithley_2600.ui.page.time.monotonic",
                side_effect=[110.0, 145.0],
            ):
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
            window._navigate_to("keithley")
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

    def test_keithley_manual_ramp_previews_then_dispatches_bounded_request(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley.level.setText("1 mA")
            keithley.ramp_target.setText("1.2 mA")
            keithley.ramp_step.setText("100 uA")
            keithley.ramp_settle.setText("1 ms")
            keithley.ramp_deadline.setText("1 s")
            keithley._preview_manual_ramp()
            self.assertIn("point(s)", keithley.ramp_preview.text())
            keithley._set_channel_output("B", True)

            with patch.object(
                QMessageBox,
                "warning",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                keithley._execute_manual_ramp()

            operation, request = keithley._controller.call.call_args.args
            self.assertEqual(operation, "ramp_to_level")
            self.assertAlmostEqual(request.target_si, 0.0012)
            self.assertAlmostEqual(request.max_step_si, 0.0001)
            self.assertTrue(keithley._ramp_pending)

            measurement = SimpleNamespace(
                channel="B",
                voltage_v=0.012,
                current_a=0.0012,
                power_w=0.0000144,
                compliance_detected=False,
            )
            keithley._result(
                "ramp_to_level",
                SimpleNamespace(
                    final_measurement=measurement,
                    levels_si=(0.0011, 0.0012),
                    target_si=0.0012,
                ),
            )
            self.assertFalse(keithley._ramp_pending)
            self.assertTrue(keithley._output_states["B"])
            self.assertEqual(keithley.level.text(), "1.2 mA")
            self.assertIn("Ramp completed", keithley.ramp_preview.text())
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

    def test_anritsu_acquires_one_fresh_reference_with_provenance(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu._device_idn = "ANRITSU,MS2830A,SERIAL,7.03"
            anritsu.set_capabilities(
                DeviceCapabilities(
                    device_name="anritsu",
                    model="MS2830A",
                    firmware="7.03",
                    features=frozenset({"spectrum_trace"}),
                    hardware_options=("041",),
                )
            )
            acquired = datetime.now(timezone.utc)
            trace = SpectrumTrace((1e6, 2e6), (-40.0, -41.0), acquired, "TRAC1")

            anritsu.acquire_reference_once()

            anritsu._controller.call.assert_called_once_with("fetch_current_trace", "TRAC1")
            self.assertEqual(anritsu._page_state, AnritsuPageState.ACQUIRING_REFERENCE)
            self.assertFalse(anritsu.live.isEnabled())
            anritsu._result("fetch_current_trace", trace)

            reference = anritsu._reference_spectrum
            self.assertIsNotNone(reference)
            self.assertIs(reference.trace, trace)
            self.assertEqual(reference.kind, "single")
            self.assertEqual(reference.average_count, 1)
            self.assertEqual(reference.source_device_idn, "ANRITSU,MS2830A,SERIAL,7.03")
            self.assertEqual(reference.hardware_options, ("041",))
            self.assertEqual(anritsu._page_state, AnritsuPageState.IDLE)
            self.assertIn("single", anritsu.reference_status.text())
            self.assertIn("2 points", anritsu.reference_status.text())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_use_current_is_local_and_cancelled_overwrite_preserves_reference(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            first = SpectrumTrace(
                (1e6, 2e6), (-50.0, -40.0), datetime.now(timezone.utc), "TRAC1"
            )
            second = SpectrumTrace(
                (1e6, 2e6), (-30.0, -20.0), datetime.now(timezone.utc), "TRAC1"
            )
            anritsu._latest_trace = first
            anritsu.capture_current_reference()
            self.assertIs(anritsu._reference_trace, first)
            anritsu._controller.call.assert_not_called()

            anritsu._latest_trace = second
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.No,
            ):
                anritsu.capture_current_reference()

            self.assertIs(anritsu._reference_trace, first)
            anritsu._controller.call.assert_not_called()
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_transport_error_is_non_modal_and_unlocks_retry_state(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._timer.start()
            anritsu._fetch_pending = True
            with patch.object(QMessageBox, "warning") as modal_warning:
                anritsu._error("fetch_current_trace", "VI_ERROR_TMO")

            modal_warning.assert_not_called()
            self.assertFalse(anritsu._timer.isActive())
            self.assertFalse(anritsu._fetch_pending)
            self.assertEqual(anritsu._page_state, AnritsuPageState.ERROR)
            self.assertFalse(anritsu.banner.isHidden())
            self.assertIn("VI_ERROR_TMO", anritsu.banner.label.text())
            self.assertTrue(anritsu.single.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_reference_save_and_load_updates_visible_status(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            trace = SpectrumTrace(
                (1e6, 2e6, 3e6),
                (-60.0, -50.0, -55.0),
                datetime.now(timezone.utc),
                "TRAC1",
            )
            anritsu._latest_trace = trace
            anritsu.capture_current_reference()
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "reference.h5"
                anritsu._save_reference_to(path)
                self.assertTrue(anritsu._reference_spectrum.saved_to_file)
                self.assertIn("saved", anritsu.reference_status.text())

                anritsu.remove_reference()
                anritsu._load_reference_from(path)

                self.assertIsNotNone(anritsu._reference_spectrum)
                self.assertEqual(anritsu._reference_spectrum.kind, "imported")
                self.assertEqual(anritsu._reference_trace.powers_dbm, trace.powers_dbm)
                self.assertIn("imported", anritsu.reference_status.text())
                self.assertTrue(anritsu.show_reference.isChecked())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_reference_processing_rejects_known_reference_level_mismatch(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            trace = SpectrumTrace(
                (1e6, 2e6), (-60.0, -50.0), datetime.now(timezone.utc), "TRAC1"
            )
            anritsu._last_configuration = AnritsuConfigurationSnapshot(
                1e6, 2e6, -10.0, 2, "SPECT"
            )
            anritsu._latest_trace = trace
            anritsu.capture_current_reference()
            anritsu._last_configuration = AnritsuConfigurationSnapshot(
                1e6, 2e6, 0.0, 2, "SPECT"
            )
            anritsu.reference_operation.setCurrentIndex(1)
            anritsu._refresh_spectrum_display()

            self.assertIn("Reference Level differs", anritsu.info.text())
            self.assertEqual(anritsu.spectrum_plot.trace_point_count("Processed"), 0)
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_reference_processing_rejects_advanced_configuration_mismatch(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            trace = SpectrumTrace(
                (1e6, 2e6), (-60.0, -50.0), datetime.now(timezone.utc), "TRAC1"
            )
            anritsu._last_configuration = AnritsuConfigurationSnapshot(
                1e6, 2e6, -10.0, 2, "SPECT"
            )
            anritsu._last_advanced_configuration = AdvancedSpectrumSnapshot(
                rbw_auto=False,
                rbw_hz=10e3,
                vbw_mode="manual",
                vbw_hz=3e3,
                detector="RMS",
                attenuation_auto=False,
                attenuation_db=20.0,
                preamplifier_enabled=False,
                sweep_time_auto=False,
                sweep_time_s=0.2,
                instrument_mode="SPECT",
            )
            anritsu._latest_trace = trace
            anritsu.capture_current_reference()
            anritsu._last_advanced_configuration = AdvancedSpectrumSnapshot(
                rbw_auto=False,
                rbw_hz=100e3,
                vbw_mode="manual",
                vbw_hz=3e3,
                detector="RMS",
                attenuation_auto=False,
                attenuation_db=20.0,
                preamplifier_enabled=False,
                sweep_time_auto=False,
                sweep_time_s=0.2,
                instrument_mode="SPECT",
            )
            anritsu.reference_operation.setCurrentIndex(1)
            anritsu._refresh_spectrum_display()

            self.assertIn("Acquisition settings differ", anritsu.info.text())
            self.assertIn("RBW", anritsu.info.text())
            self.assertEqual(anritsu.spectrum_plot.trace_point_count("Processed"), 0)
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

    def test_anritsu_plot_keeps_frequency_data_in_hz_for_si_axis_scaling(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            trace = SpectrumTrace(
                (100e6, 6e9), (-50.0, -40.0), datetime.now(timezone.utc), "TRAC1"
            )

            anritsu._latest_trace = trace
            anritsu._refresh_spectrum_display()

            self.assertEqual(anritsu.spectrum_plot._x_unit, "Hz")
            self.assertEqual(
                anritsu.spectrum_plot._traces["Raw"][0].tolist(), [100e6, 6e9]
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_live_uses_passive_polling_and_reports_frozen_frames(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            self.assertEqual(anritsu.refresh.minimum(), 10)
            trace = SpectrumTrace(
                (1e6, 2e6), (-50.0, -40.0), datetime.now(timezone.utc), "TRAC1"
            )
            anritsu._spectrogram_buffer.append(trace, now=1.0)

            anritsu.toggle_live()

            anritsu._controller.call.assert_called_once_with("start_live", False)
            self.assertTrue(anritsu._live_transition_pending)
            self.assertEqual(anritsu.live_indicator.property("liveState"), "starting")
            anritsu.toggle_live()
            anritsu._controller.call.assert_called_once_with("start_live", False)
            snapshot = AnritsuConfigurationSnapshot(1e6, 2e6, 0.0, 101, "SPECT")
            anritsu._result("start_live", snapshot)
            self.assertEqual(anritsu._spectrogram_buffer.row_count, 0)
            self.assertFalse(anritsu._live_transition_pending)
            self.assertFalse(anritsu.single.isEnabled())
            self.assertEqual(anritsu.live_indicator.property("liveState"), "on")
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

    def test_anritsu_spectrogram_reuses_completed_frames_and_processes_reference_locally(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            reference = SpectrumTrace(
                (1e6, 2e6, 3e6),
                (-50.0, -40.0, -30.0),
                datetime.now(timezone.utc),
                "REF",
            )
            anritsu._latest_trace = reference
            anritsu.capture_current_reference()
            first = SpectrumTrace(
                reference.frequencies_hz,
                (-45.0, -35.0, -25.0),
                datetime.now(timezone.utc),
                "TRAC1",
            )
            second = SpectrumTrace(
                reference.frequencies_hz,
                (-40.0, -30.0, -20.0),
                datetime.now(timezone.utc),
                "TRAC1",
            )

            anritsu._spectrogram_buffer.append(first, now=100.0)
            anritsu._spectrogram_buffer.append(second, now=131.0)
            raw = anritsu._spectrogram_matrix(source="raw", window_s=30)
            self.assertIsNotNone(raw)
            assert raw is not None
            self.assertEqual(raw[2].shape, (1, 3))
            self.assertEqual(raw[3], "dBm")

            processed = anritsu._spectrogram_matrix(
                source="processed", window_s=60
            )
            self.assertIsNotNone(processed)
            assert processed is not None
            self.assertEqual(processed[2].tolist(), [[5.0, 5.0, 5.0], [10.0, 10.0, 10.0]])
            self.assertEqual(processed[3], "dB")
            anritsu._refresh_spectrogram_display()
            anritsu._show_trace(second)
            anritsu._controller.call.assert_not_called()

            anritsu._spectrogram_buffer.clear()
            for index in range(anritsu._spectrogram_buffer.MAX_ROWS + 100):
                anritsu._spectrogram_buffer.append(
                    first, now=200.0 + index * 0.11
                )
            self.assertLessEqual(
                anritsu._spectrogram_buffer.row_count,
                anritsu._spectrogram_buffer.MAX_ROWS,
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_auto_cleanup_and_peak_detection_are_local_and_preserve_raw(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            trace = synthetic_anritsu_peaks()
            mode = anritsu.cleanup_mode.findData("denoise")
            anritsu.cleanup_mode.setCurrentIndex(mode)

            anritsu._show_trace(trace)

            anritsu._controller.call.assert_not_called()
            self.assertEqual(anritsu._latest_trace.powers_dbm, trace.powers_dbm)
            self.assertIsNotNone(anritsu._cleanup_result)
            self.assertNotEqual(
                anritsu._cleanup_result.values_dbm,
                trace.powers_dbm,
            )
            self.assertEqual(
                anritsu.spectrum_plot._traces["Raw"][1].tolist(),
                list(trace.powers_dbm),
            )
            self.assertGreater(
                anritsu.spectrum_plot.trace_point_count("Analysis"), 0
            )
            self.assertGreaterEqual(len(anritsu._detected_peaks), 2)
            self.assertEqual(
                len(anritsu.spectrum_plot.peak_markers.points()),
                len(anritsu._detected_peaks),
            )
            first = anritsu._detected_peaks[0]
            self.assertIsNotNone(first.fit_fwhm_hz)
            self.assertGreater(first.snr_db, 20.0)
            self.assertIn(first.fit_model, {"Gaussian", "Lorentzian"})
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_peak_table_and_floating_tracker_follow_frequency_without_visa_calls(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("anritsu")
            self.application.processEvents()
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu._show_trace(synthetic_anritsu_peaks(primary_hz=1.0e9))
            anritsu._open_peak_table()
            self.application.processEvents()

            table = anritsu._peak_table_dialog
            self.assertIsNotNone(table)
            assert table is not None
            self.assertTrue(table.isVisible())
            self.assertGreaterEqual(table.table.rowCount(), 2)
            self.assertIn("Hz", table.table.item(0, 1).text())
            self.assertIn("dBm", table.table.item(0, 2).text())
            self.assertIn("dB", table.table.item(0, 3).text())
            self.assertNotEqual(table.table.item(0, 5).text(), "—")

            tracked_index = min(
                range(len(anritsu._detected_peaks)),
                key=lambda index: abs(
                    anritsu._detected_peaks[index].frequency_hz - 1.0e9
                ),
            )
            table.table.selectRow(tracked_index)
            anritsu._start_peak_tracking(tracked_index)
            self.application.processEvents()
            tracking = anritsu._peak_tracking_window
            self.assertIsNotNone(tracking)
            assert tracking is not None
            self.assertTrue(tracking.isVisible())
            self.assertTrue(
                bool(tracking.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            )
            self.assertEqual(tracking.point_count, 1)

            anritsu._show_trace(
                synthetic_anritsu_peaks(primary_hz=1.0002e9)
            )
            self.application.processEvents()
            self.assertEqual(tracking.point_count, 2)
            self.assertIn("GHz", tracking.frequency.text())
            x_values, y_values = tracking.curve.getData()
            self.assertEqual(len(x_values), 2)
            self.assertGreater(float(y_values[-1]), float(y_values[0]))
            anritsu._controller.call.assert_not_called()
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_floating_spectrogram_shares_window_and_raw_processed_selection(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("anritsu")
            self.application.processEvents()
            anritsu = window.anritsu_page
            anritsu._open_spectrogram_window()
            self.application.processEvents()
            floating = anritsu._spectrogram_window
            self.assertIsNotNone(floating)
            assert floating is not None
            self.assertTrue(floating.isVisible())
            self.assertTrue(
                bool(floating.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            )
            self.assertEqual(
                [floating.window_span.itemData(index) for index in range(floating.window_span.count())],
                [30, 60, 90, 120],
            )
            floating.resize(520, 380)
            self.application.processEvents()
            for control in (
                floating.source,
                floating.window_span,
                floating.reset_view,
            ):
                right = control.mapTo(
                    floating, control.rect().bottomRight()
                ).x()
                self.assertLessEqual(right, floating.rect().right())

            floating.window_span.setCurrentIndex(
                floating.window_span.findData(120)
            )
            floating.source.setCurrentIndex(
                floating.source.findData("processed")
            )
            self.assertEqual(anritsu.spectrogram_window_span.currentData(), 120)
            self.assertEqual(anritsu.spectrogram_source.currentData(), "processed")
            self.assertIn("requires", floating.status.text())

            reference = SpectrumTrace(
                (1e6, 2e6),
                (-50.0, -40.0),
                datetime.now(timezone.utc),
                "REF",
            )
            anritsu._latest_trace = reference
            anritsu.capture_current_reference()
            frame = SpectrumTrace(
                reference.frequencies_hz,
                (-40.0, -25.0),
                datetime.now(timezone.utc),
                "TRAC1",
            )
            anritsu._spectrogram_buffer.append(frame, now=100.0)
            anritsu._refresh_spectrogram_display()
            self.assertIn("Processed", floating.status.text())
            self.assertEqual(
                floating.spectrogram.image.image.tolist(),
                [[10.0, 15.0]],
            )

            anritsu.spectrogram_source.setCurrentIndex(
                anritsu.spectrogram_source.findData("raw")
            )
            self.assertEqual(floating.source.currentData(), "raw")
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

    def test_anritsu_center_span_round_trip_configures_effective_start_stop(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            center_span = anritsu.frequency_representation.findData("center_span")
            anritsu.frequency_representation.setCurrentIndex(center_span)

            snapshot = AnritsuConfigurationSnapshot(100e6, 6e9, -10.0, 10001)
            anritsu._result("read_configuration", snapshot)

            self.assertEqual(anritsu.frequency_label_a.text(), "Center")
            self.assertEqual(anritsu.frequency_label_b.text(), "Span")
            self.assertEqual(
                parse_quantity(anritsu.start.text(), DIMENSION_FREQUENCY).si_value,
                3.05e9,
            )
            self.assertEqual(
                parse_quantity(anritsu.stop.text(), DIMENSION_FREQUENCY).si_value,
                5.9e9,
            )

            anritsu.configure()
            operation, config = anritsu._controller.call.call_args.args
            self.assertEqual(operation, "configure")
            self.assertEqual(config.start_hz, 100e6)
            self.assertEqual(config.stop_hz, 6e9)

            start_stop = anritsu.frequency_representation.findData("start_stop")
            anritsu.frequency_representation.setCurrentIndex(start_stop)
            self.assertEqual(
                parse_quantity(anritsu.start.text(), DIMENSION_FREQUENCY).si_value,
                100e6,
            )
            self.assertEqual(
                parse_quantity(anritsu.stop.text(), DIMENSION_FREQUENCY).si_value,
                6e9,
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_advanced_dialog_is_read_only_until_exact_firmware_is_qualified(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            capabilities = DeviceCapabilities(
                device_name="anritsu",
                model="MS2830A",
                firmware="sim-1.0",
                features=frozenset({"spectrum_trace", "live_trace"}),
                hardware_options=("041", "008"),
            )
            anritsu.set_capabilities(capabilities)
            self.assertFalse(anritsu.advanced_apply_button.isEnabled())

            anritsu.advanced_read_button.click()
            anritsu._controller.call.assert_called_once_with("read_advanced_spectrum")
            snapshot = AdvancedSpectrumSnapshot(
                rbw_auto=False,
                rbw_hz=3e3,
                vbw_mode="off",
                vbw_hz=None,
                detector="POS",
                attenuation_auto=False,
                attenuation_db=20,
                preamplifier_enabled=False,
                sweep_time_auto=False,
                sweep_time_s=0.2,
                instrument_mode="SPECT",
            )
            anritsu._result("read_advanced_spectrum", snapshot)
            self.assertEqual(anritsu.advanced_rbw_mode.currentData(), "manual")
            self.assertEqual(anritsu.advanced_vbw_mode.currentData(), "off")
            self.assertEqual(anritsu.advanced_detector.currentData(), "POS")
            self.assertEqual(anritsu.advanced_attenuation.value(), 20)

            raw = window._settings.model_dump(mode="python")
            raw["devices"]["anritsu"]["advanced_spectrum"] = {
                "control_protocol": "standard_scpi",
                "qualified_firmware": ["sim-1.0"],
            }
            qualified = window._settings.__class__.model_validate(raw)
            anritsu.set_settings(qualified)
            anritsu.set_capabilities(capabilities)
            self.assertTrue(anritsu.advanced_apply_button.isEnabled())
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

    def test_anritsu_signal_generator_tab_requires_detected_option_and_qualified_protocol(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            self.assertFalse(anritsu.mode_tabs.isTabVisible(anritsu.signal_generator_tab_index))
            anritsu.set_capabilities(
                DeviceCapabilities(
                    device_name="anritsu",
                    model="MS2830A",
                    firmware="sim",
                    features=frozenset({"spectrum_trace", "signal_generator"}),
                    hardware_options=("041", "020"),
                )
            )
            self.assertTrue(anritsu.mode_tabs.isTabVisible(anritsu.signal_generator_tab_index))
            self.assertIn("Protocol: unverified", anritsu.sg_limits.text())
            self.assertFalse(anritsu.sg_configure.isEnabled())
            self.assertFalse(anritsu.sg_on.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_workspace_and_event_log_are_user_resizable(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("anritsu")
            self.application.processEvents()

            splitter = window.anritsu_page.workspace_splitter
            self.assertEqual(splitter.orientation(), Qt.Orientation.Horizontal)
            self.assertEqual(splitter.count(), 2)
            self.assertGreater(splitter.widget(0).minimumWidth(), 0)
            self.assertGreater(window.log.maximumHeight(), 10000)
            self.assertEqual(
                window.shell_splitter.orientation(),
                Qt.Orientation.Vertical,
            )
            self.assertIs(window.shell_splitter.widget(1), window.event_log_panel)
            self.assertFalse(window.event_log_panel.isWindow())
            self.assertTrue(window.event_log_action.isCheckable())

            window.resize(820, 640)
            self.application.processEvents()
            self.assertEqual(splitter.orientation(), Qt.Orientation.Vertical)
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_readback_is_saved_as_defaults_without_writing_instrument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                anritsu = window.anritsu_page
                anritsu._controller.call = Mock()
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Save,
                ):
                    anritsu.read_and_save_configuration.click()
                    anritsu._controller.call.assert_called_once_with("read_configuration")
                    basic = AnritsuConfigurationSnapshot(
                        2e6, 3e9, -15.5, 2001, "SPECT"
                    )
                    anritsu._result("read_configuration", basic)
                    self.assertEqual(
                        anritsu._controller.call.call_args_list[-1].args,
                        ("read_advanced_spectrum",),
                    )
                    advanced = AdvancedSpectrumSnapshot(
                        rbw_auto=False,
                        rbw_hz=3e3,
                        vbw_mode="off",
                        vbw_hz=None,
                        detector="POS",
                        attenuation_auto=False,
                        attenuation_db=20,
                        preamplifier_enabled=False,
                        sweep_time_auto=False,
                        sweep_time_s=0.2,
                        instrument_mode="SPECT",
                    )
                    anritsu._result("read_advanced_spectrum", advanced)

                defaults = SettingsRepository(path).load().raw["devices"]["anritsu"][
                    "safety"
                ]["defaults"]
                self.assertEqual(
                    parse_quantity(defaults["start_frequency"], DIMENSION_FREQUENCY).si_value,
                    2e6,
                )
                self.assertEqual(
                    parse_quantity(defaults["stop_frequency"], DIMENSION_FREQUENCY).si_value,
                    3e9,
                )
                self.assertEqual(defaults["reference_level"], "-15.5 dBm")
                self.assertEqual(defaults["sweep_points"], 2001)
                self.assertEqual(
                    parse_quantity(defaults["rbw"], DIMENSION_FREQUENCY).si_value,
                    3e3,
                )
                self.assertEqual(defaults["detector"], "POS")
                self.assertEqual(defaults["attenuation"], "20 dB")
                self.assertEqual(
                    [call.args[0] for call in anritsu._controller.call.call_args_list],
                    ["read_configuration", "read_advanced_spectrum"],
                )
            finally:
                window.close()
                self.application.processEvents()

    def test_limit_edit_button_opens_popup_and_saves_the_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                field = window.keithley_page._limit_fields["level"]
                self.assertTrue(field.edit_button.isEnabled())
                self.assertIn(
                    "Saving revokes profile approval", field.edit_button.toolTip()
                )

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
                self.assertIsNot(
                    window.stackedWidget.currentWidget(),
                    window.navigation_routes["settings"],
                )
            finally:
                window.close()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
