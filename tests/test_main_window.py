from __future__ import annotations

import os
import inspect
import math
import time
from dataclasses import replace
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
from app.engine import ExecutionPlan, PlanAction
from app.domain.quick_controls import QuickControlCommand
from app.devices.discovery import DiscoveredInstrument
from app.devices.moke_box.models import MokeHallVoltageReading
from app.devices.anritsu_ms2830a import (
    AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot,
    SpectrumTrace,
)
from app.devices.keithley_2600 import (
    KeithleyChannelConfigurationReadback,
    KeithleyConfigurationReadback,
)
from app.devices.keithley_2600.ui.page import KeithleyConfigurationSnapshot
from app.domain.quantities import DIMENSION_DBM, DIMENSION_FREQUENCY, parse_quantity
from app.settings.repository import SettingsRepository
from app.settings.models import StationSettings
from app.safety.quick_controls import quick_control_safety_bounds
from app.devices.anritsu_ms2830a.ui import AnritsuPageState
from app.ui.shell import MainWindow
from app.ui.dashboard.device_card import DeviceCard
from app.ui.design_system import tokens_for
from qfluentwidgets import CardWidget, ComboBox, PlainTextEdit
from app.ui.widgets import LimitEditDialog
from tests.helpers import SETTINGS_TEMPLATE


TEST_ENGINEER = "LAB\\test-engineer"
TEST_SERVICE = "LAB\\test-service"


def wait_for_ui(predicate: object, *, timeout_ms: int = 5_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1_000.0
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if callable(predicate) and predicate():
            return True
        QTest.qWait(10)
    return bool(callable(predicate) and predicate())


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

    def test_rigol_basic_form_keeps_complete_values_per_channel(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            rigol.level_mode.setCurrentText(rigol.LEVEL_MODE_HIGH_LOW)
            rigol.frequency.setText("3 kHz")
            rigol.high_level.setText("7 mV")
            rigol.low_level.setText("1 mV")
            rigol.output_polarity.setCurrentText("INV")
            rigol.channel.setCurrentText("2")
            self.application.processEvents()
            self.assertEqual(rigol.frequency.text(), "1 kHz")
            rigol.frequency.setText("8 kHz")
            rigol.waveform.setCurrentText("SQU")
            rigol.duty.setText("25")
            rigol.channel.setCurrentText("1")
            self.application.processEvents()
            self.assertEqual(rigol.frequency.text(), "3 kHz")
            self.assertEqual(rigol.high_level.text(), "7 mV")
            self.assertEqual(rigol.low_level.text(), "1 mV")
            self.assertEqual(rigol.output_polarity.currentText(), "INV")
            channel_two = rigol.configuration_snapshot_for(2)
            self.assertEqual(channel_two.frequency, "8 kHz")
            self.assertEqual(channel_two.waveform, "SQU")
            self.assertEqual(channel_two.square_duty_percent, "25")
        finally:
            window.close()
            self.application.processEvents()

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
                window._guard_manual_operation("set_output", (1, True))
            with self.assertRaisesRegex(Exception, "audit log is unavailable"):
                window._guard_manual_operation("ramp_to_level", object())
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

            with patch(
                "app.ui.shell.main_window.QMessageBox.warning",
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
            monitor.run_started(
                4,
                2.0,
                plan_actions=(
                    SimpleNamespace(
                        node_id="measure-b",
                        kind="measure_keithley",
                        is_finally=False,
                    ),
                    SimpleNamespace(
                        node_id="off-b",
                        kind="set_keithley_output",
                        is_finally=True,
                    ),
                ),
            )
            self.assertTrue(monitor.stop_button.isEnabled())
            self.assertIn("Plan estimate:", monitor.total_estimate.text())
            self.assertIn("0 of 4 actions", monitor.progress_summary.text())
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
            monitor.append_event(
                "action_finished",
                {"node_id": "measure-b", "kind": "measure_keithley"},
            )
            monitor.append_event(
                "safe_finally_started",
                {"node_id": "off-b", "kind": "set_keithley_output"},
            )
            monitor.append_event(
                "safe_finally_finished",
                {"node_id": "off-b", "kind": "set_keithley_output"},
            )
            monitor.append_event(
                "shutdown_action_started",
                {"action": "keithley.outputs_off"},
            )
            monitor.append_event(
                "shutdown_action_finished",
                {"action": "keithley.outputs_off"},
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
            self.assertIn("2 of 4 actions", monitor.progress_summary.text())
            self.assertIn("estimated total:", monitor.eta.text())
            self.assertEqual(monitor._step_items["measure-b"].text(2), "✓ DONE")
            self.assertEqual(monitor._step_items["off-b"].text(2), "✓ DONE")
            self.assertEqual(
                monitor._step_items["shutdown:keithley.outputs_off"].text(2),
                "✓ DONE",
            )

            stop_requests: list[bool] = []
            monitor.run_started(1, 1.0)
            monitor.stop_requested.connect(lambda: stop_requests.append(True))
            monitor.stop_button.click()
            self.assertEqual(stop_requests, [True])
            self.assertFalse(monitor.stop_button.isEnabled())
            self.assertEqual(monitor.stop_button.text(), "Stopping safely…")
            self.assertEqual(monitor.state.text(), "STOPPING")
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
                with patch("app.ui.settings_page.QMessageBox.critical"):
                    self.assertIsNone(page.validate_draft())
                self.assertIn(page.limits_table.item(row, 2), page._limit_error_items)
                self.assertIn(page.limits_table.item(row, 3), page._limit_error_items)
            finally:
                window.close()
                self.application.processEvents()

    def test_anritsu_acquisition_error_highlights_only_missing_frequency_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(path, simulation=False, authenticated_username=TEST_ENGINEER)
            try:
                page = window.settings_page
                safety = page._raw["devices"]["anritsu"]["safety"]
                safety["acquisition_allowed"] = True
                safety["frequency"] = {"min": None, "max": None}
                page._populate()
                with patch("app.ui.settings_page.QMessageBox.critical"):
                    self.assertIsNone(page.validate_draft())
                expected = {
                    ("devices", "anritsu", "safety", "frequency", boundary)
                    for boundary in ("min", "max")
                }
                self.assertTrue(expected.issubset(set(page._limit_items_by_path)))
                self.assertFalse(
                    any(
                        path[:4]
                        == ("devices", "anritsu", "safety", "reference_level")
                        for path in page._limit_items_by_path
                    )
                )
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
                with patch("app.ui.shell.main_window.QMessageBox.warning") as warning:
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

    def test_engineer_can_edit_station_settings_and_roles(self) -> None:
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
                self.assertTrue(window.settings_page.add_role_button.isEnabled())
                loaded = SettingsRepository(settings_path).load().settings
                self.assertEqual(loaded.profile.id, "default-lab-profile")
                self.assertEqual(loaded.profile.name, "Default station profile")
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
                rigol.phase, rigol.duty, rigol.ramp_symmetry,
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

    def test_settings_autosave_repairs_missing_optional_anritsu_rf_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                page = window.settings_page
                safety = page._raw["devices"]["anritsu"]["safety"]
                safety["acquisition_allowed"] = True
                safety["require_rf_input_limit_definition"] = True
                safety["rf_input"]["max_expected_power_at_connector"] = None
                safety["frequency"] = {"min": "10 MHz", "max": "3 GHz"}
                page._populate()
                page._dirty = True

                self.assertTrue(page.save_draft(silent=True))

                persisted = SettingsRepository(path).load().raw["devices"][
                    "anritsu"
                ]["safety"]
                self.assertTrue(persisted["acquisition_allowed"])
                self.assertFalse(persisted["require_rf_input_limit_definition"])
                self.assertIsNone(
                    persisted["rf_input"]["max_expected_power_at_connector"]
                )
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
                with patch(
                    "app.ui.shell.main_window.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
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

    def test_dry_run_selection_reaches_controller_and_execution_monitor(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            plan = ExecutionPlan(
                recipe_name="dry-run-ui-contract",
                actions=(
                    PlanAction(
                        node_id="dry-run-wait",
                        kind="wait",
                        payload={"duration_s": 0.1},
                        setpoints_si={},
                    ),
                ),
                total_points=0,
                sha256="dry-run-ui-contract",
                recipe_source="schema_version: 1\nname: dry-run-ui-contract\n",
            )
            window._run_controller.start = Mock()

            window._start_run(plan, True)

            window._run_controller.start.assert_called_once()
            self.assertTrue(
                window._run_controller.start.call_args.kwargs[
                    "outputs_forced_off"
                ]
            )
            self.assertEqual(window.run_monitor.state.text(), "DRY RUN — OUTPUTS OFF")
            self.assertIn("forced OFF", window.run_monitor.state.toolTip())
        finally:
            window.close()
            self.application.processEvents()

    def test_hardware_run_asks_before_connecting_required_sweep_devices(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window._simulation = False
            plan = ExecutionPlan(
                recipe_name="connection-consent",
                actions=(),
                total_points=0,
                sha256="connection-consent",
                recipe_source="schema_version: 1\nname: connection-consent\n",
                required_devices=frozenset({"anritsu", "keithley"}),
            )
            with patch(
                "app.ui.shell.main_window.QMessageBox.action_guidance",
                return_value=False,
            ) as guidance:
                accepted = window._confirm_run_engine_connections(plan)

            self.assertFalse(accepted)
            guidance.assert_called_once()
            title = guidance.call_args.args[1]
            message = guidance.call_args.args[2]
            self.assertEqual(title, "Connect devices for Sweep")
            self.assertIn("Anritsu MS2830A", message)
            self.assertIn("Keithley 2600", message)
            self.assertIn("does not enable any output", message)
            self.assertEqual(guidance.call_args.args[3], "Connect and run")
        finally:
            window.close()
            self.application.processEvents()

    def test_each_device_and_spectrum_analysis_have_distinct_background_threads(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        device_threads = [controller._thread for controller in window._controllers.values()]
        analysis_thread = window.anritsu_page._analysis_controller._thread
        try:
            self.assertEqual(len(device_threads), 5)
            self.assertEqual(len({id(thread) for thread in device_threads}), 5)
            self.assertTrue(all(thread.isRunning() for thread in device_threads))
            self.assertTrue(analysis_thread.isRunning())
            self.assertNotIn(analysis_thread, device_threads)
            self.assertTrue(
                all(thread is not self.application.thread() for thread in device_threads)
            )
            self.assertIsNot(analysis_thread, self.application.thread())
        finally:
            window.close()
            self.application.processEvents()
        self.assertTrue(all(not thread.isRunning() for thread in device_threads))
        self.assertFalse(analysis_thread.isRunning())

    def test_anritsu_opens_composed_quick_controls_and_arrows_send_immediately(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            targets = (
                "keithley.A.current",
                "keithley.B.voltage",
                "rigol.1.frequency",
                "rigol.1.amplitude",
                "rigol.1.offset",
            )
            window.quick_controls_window.set_targets(targets)
            window._controllers["rigol"].call = Mock()

            window.anritsu_page.quick_controls_button.click()
            self.application.processEvents()

            floating = window.quick_controls_window
            self.assertTrue(floating.isVisible())
            self.assertEqual(tuple(floating._rows), targets)
            self.assertEqual(
                window.anritsu_page.quick_controls_button.text(),
                "Quick controls...",
            )
            self.assertEqual(
                window.rigol_page.quick_controls_button.text(),
                "Quick controls...",
            )
            self.assertEqual(
                window.keithley_page.quick_controls_button.text(),
                "Quick controls...",
            )
            self.assertEqual(floating.choose.text(), "Choose...")
            window._controllers["rigol"].call.reset_mock()
            frequency = floating._rows["rigol.1.frequency"]
            frequency.value.setText("10.000 kHz")
            frequency.increase.click()
            window._controllers["rigol"].call.assert_called_once_with(
                "quick_setpoint",
                QuickControlCommand("rigol.1.frequency", "10.001 kHz"),
            )
            self.assertEqual(frequency.value.text(), "10.001 kHz")

            window._set_run_ui_locked(True)
            self.assertFalse(floating.isEnabled())
            window._set_run_ui_locked(False)
            self.assertTrue(floating.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_device_pages_remain_visible_but_read_only_while_run_owns_instruments(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1280, 800)
            window.show()
            window._navigate_to("rigol")
            self.application.processEvents()
            rigol = window.rigol_page
            original_frequency_enabled = rigol.frequency.isEnabled()
            original_button_enabled = rigol.quick_controls_button.isEnabled()

            window._set_run_ui_locked(True)

            self.assertTrue(window.navigation_routes["rigol"].isEnabled())
            self.assertTrue(rigol.isVisibleTo(window))
            self.assertFalse(rigol.frequency.isEnabled())
            self.assertFalse(rigol.quick_controls_button.isEnabled())
            self.assertTrue(rigol.isEnabled())

            window._set_run_ui_locked(False)

            self.assertEqual(rigol.frequency.isEnabled(), original_frequency_enabled)
            self.assertEqual(
                rigol.quick_controls_button.isEnabled(), original_button_enabled
            )
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
            self.assertEqual(apparatus.itemWidget._text, "Devices")
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

    def test_narrow_shell_compacts_navigation_and_keeps_estop_prominent(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(820, 560)
            window.show()
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()

            panel = window.navigationInterface.panel
            estop = window.safety_strip.estop
            self.assertTrue(panel.isCollapsed())
            self.assertLessEqual(panel.width(), 48)
            self.assertTrue(estop.isVisible())
            self.assertGreaterEqual(estop.width(), 184)
            self.assertGreaterEqual(estop.height(), 36)
            self.assertEqual(estop.property("visualPriority"), "high")
            self.assertEqual(estop.property("controlState"), "emergency")
            self.assertIn("disable all outputs", estop.accessibleName().lower())
            self.assertIn(tokens_for("light").emergency, estop.styleSheet())
            self.assertIn(tokens_for("light").on_emergency, estop.styleSheet())
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
                with patch(
                    "app.ui.shell.main_window.QMessageBox.question",
                    return_value=int(QMessageBox.StandardButton.Yes),
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
                with patch(
                    "app.ui.shell.main_window.QMessageBox.question",
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
                with patch(
                    "app.ui.shell.main_window.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ), patch(
                    "app.ui.shell.main_window.QMessageBox.critical"
                ) as critical:
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
            with patch("app.ui.shell.main_window.QMessageBox.warning") as warning:
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

    def test_limit_edit_requires_global_save_settings(self) -> None:
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
                before = SettingsRepository(path).load()
                self.assertNotEqual(
                    before.raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["frequency"]["max"],
                    "900 kHz",
                )
                self.assertTrue(window.settings_page._dirty)
                window.safety_strip.save_settings.click()
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
            self.assertIn("outside the configured", rigol.banner.label.text())

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
            rigol.frequency.setText("10000 kHz")
            rigol.frequency.editingFinished.emit()
            self.assertEqual(rigol.frequency.text(), "10 MHz")
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
            with patch(
                "app.devices.rigol_dg1000z.ui.page.QMessageBox.warning"
            ) as warning:
                rigol.configure_sweep()
            rigol._controller.call.assert_not_called()
            self.assertIn("sweep_stop", warning.call_args.args[2])
        finally:
            window.close()
            self.application.processEvents()

    def test_device_fields_show_profile_limits_and_keithley_updates_them_by_mode(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=False)
        try:
            shared_bounds = quick_control_safety_bounds(window._settings)
            rigol = window.rigol_page
            for editor, target in (
                (rigol.frequency, "rigol.1.frequency"),
                (rigol.vpp, "rigol.1.amplitude"),
                (rigol.offset, "rigol.1.offset"),
            ):
                bound = shared_bounds[target]
                self.assertEqual(
                    rigol._limit_fields[editor].minimum.text(),
                    f"MIN  {bound.minimum_text}",
                )
                self.assertEqual(
                    rigol._limit_fields[editor].maximum.text(),
                    f"MAX  {bound.maximum_text}",
                )
                self.assertEqual(
                    window.quick_control_coordinator._bounds[target],
                    (bound.minimum_si, bound.maximum_si),
                )

            keithley = window.keithley_page
            keithley.channel.setCurrentText("A")
            self.application.processEvents()
            self.assertEqual(keithley._limit_fields["settle"].minimum.text(), "MIN  1 ms")
            self.assertEqual(keithley._limit_fields["settle"].maximum.text(), "MAX  10 s")
            keithley.channel.setCurrentText("B")
            keithley.mode.setCurrentText("current")
            self.application.processEvents()
            current_bound = shared_bounds["keithley.B.current"]
            self.assertEqual(
                keithley._limit_fields["level"].minimum.text(),
                f"MIN  {current_bound.minimum_text}",
            )
            self.assertEqual(
                keithley._limit_fields["level"].maximum.text(),
                f"MAX  {current_bound.maximum_text}",
            )
            self.assertEqual(
                window.quick_control_coordinator._bounds["keithley.B.current"],
                (current_bound.minimum_si, current_bound.maximum_si),
            )
            keithley.mode.setCurrentText("voltage")
            self.application.processEvents()
            voltage_bound = shared_bounds["keithley.B.voltage"]
            self.assertEqual(
                keithley._limit_fields["level"].minimum.text(),
                f"MIN  {voltage_bound.minimum_text}",
            )
            self.assertEqual(
                keithley._limit_fields["level"].maximum.text(),
                f"MAX  {voltage_bound.maximum_text}",
            )
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
            window.resize(1600, 900)
            window.show()
            window._navigate_to("keithley")
            keithley.mode.setCurrentText("current")
            self.application.processEvents()

            self.assertEqual(keithley.keithley_form.labelForField(keithley.level_field).text(), "Source current")
            self.assertEqual(
                keithley.keithley_form.labelForField(keithley.compliance_field).text(),
                "Voltage limit (compliance)",
            )
            self.assertTrue(keithley.keithley_form.isRowVisible(keithley.compliance_field))
            self.assertTrue(keithley.compliance.isVisible())
            self.assertIn("V", keithley.compliance_field.minimum.text())
            self.assertIn("V", keithley.compliance_field.maximum.text())
            self.assertFalse(
                keithley.keithley_form.isRowVisible(
                    keithley.configuration_panel.nplc_field
                )
            )
            keithley.level.setText("2 mA")

            keithley.mode.setCurrentText("voltage")
            self.assertEqual(keithley.keithley_form.labelForField(keithley.level_field).text(), "Source voltage")
            self.assertEqual(
                keithley.keithley_form.labelForField(keithley.compliance_field).text(),
                "Current limit (compliance)",
            )
            self.assertTrue(keithley.keithley_form.isRowVisible(keithley.compliance_field))
            self.assertIn("A", keithley.compliance_field.minimum.text())
            self.assertIn("A", keithley.compliance_field.maximum.text())
            self.assertTrue(keithley.level.text().endswith("V"))
            self.assertTrue(keithley.compliance.text().endswith("A"))

            keithley.mode.setCurrentText("measure_only")
            self.assertFalse(keithley.keithley_form.isRowVisible(keithley.level_field))
            self.assertFalse(keithley.keithley_form.isRowVisible(keithley.compliance_field))
            self.assertEqual(keithley.output_toggle.text(), "OUTPUT OFF")

            keithley.mode.setCurrentText("current")
            self.assertEqual(keithley.level.text(), "2 mA")
            self.assertTrue(keithley.keithley_form.isRowVisible(keithley.level_field))
            self.assertTrue(keithley.keithley_form.isRowVisible(keithley.compliance_field))

            window.resize(1050, 800)
            self.application.processEvents()
            self.assertTrue(keithley.compliance.isVisible())
            self.assertGreater(keithley.compliance_field.width(), 480)
            self.assertGreater(keithley.compliance.height(), 0)
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
                keithley.live_channel_a, keithley.live_channel_b,
                keithley.live_interval, keithley.live_timing,
                keithley.apply_configuration_button,
                keithley.read_configuration_button,
                keithley.measure_selected_button,
            )
            self.assertTrue(all(widget.toolTip() for widget in controls))
            for card in keithley.channel_cards.values():
                self.assertTrue(all(widget.toolTip() for widget in card.values()))
            output_on_actions = [keithley.channel_cards[channel]["output_on_action"] for channel in ("A", "B")]
            output_off_actions = [keithley.channel_cards[channel]["output_off_action"] for channel in ("A", "B")]
            self.assertTrue(all(button.text() == "OUTPUT ON" for button in output_on_actions))
            self.assertTrue(all(button.text() == "OUTPUT OFF" for button in output_off_actions))
            self.assertTrue(all(button.toolTip() for button in (*output_on_actions, *output_off_actions)))
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
            self.assertEqual(
                keithley.history_widgets["A"]["plot"]
                ._curves["CH A Resistance"]
                .opts["symbol"],
                "o",
            )
            self.assertIn("updated", keithley.last_update_labels["A"].text())
            self.assertIn("updated", keithley.last_update_labels["B"].text())

            keithley._clear_keithley_history("A")
            self.assertEqual(keithley._measurement_history["A"], [])
            self.assertEqual(len(keithley._measurement_history["B"]), 1)
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_live_selects_channels_independently_and_shows_effective_timing(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            safety = keithley._station_settings.keithley.safety.model_copy(
                update={"output_off_mode": "zero"}
            )
            keithley_settings = keithley._station_settings.keithley.model_copy(
                update={"safety": safety}
            )
            devices = keithley._station_settings.devices.model_copy(
                update={"keithley": keithley_settings}
            )
            keithley._station_settings = keithley._station_settings.model_copy(
                update={"devices": devices}
            )
            keithley._device_state_changed("verified")
            keithley.live_interval.setValue(500)

            keithley.live_channel_a.setChecked(True)
            self.assertTrue(keithley._live_timer.isActive())
            self.assertEqual(
                keithley._controller.call.call_args.args,
                ("measure", "A"),
            )
            self.assertIn("A • each", keithley.live_timing.text())
            self.assertIn("500 ms", keithley.live_timing.text())

            keithley._measure_pending = False
            keithley.live_channel_b.setChecked(True)
            self.assertIn("A + B", keithley.live_timing.text())
            self.assertIn("1 s", keithley.live_timing.text())
            keithley._request_live_measurement()
            self.assertEqual(
                keithley._controller.call.call_args.args,
                ("measure", "A"),
            )
            keithley._measure_pending = False
            keithley._request_live_measurement()
            self.assertEqual(
                keithley._controller.call.call_args.args,
                ("measure", "B"),
            )

            keithley.live_channel_a.setChecked(False)
            keithley._measure_pending = False
            keithley._request_live_measurement()
            self.assertEqual(
                keithley._controller.call.call_args.args,
                ("measure", "B"),
            )
            self.assertTrue(keithley.live_channel_b.isChecked())
            self.assertFalse(keithley.live_channel_a.isChecked())
            self.assertTrue(
                all(
                    call.args[0] == "measure"
                    for call in keithley._controller.call.call_args_list
                )
            )
        finally:
            window.keithley_page._live_timer.stop()
            window.close()
            self.application.processEvents()

    def test_keithley_live_remains_available_and_warns_for_output_off_high_impedance(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")

            self.assertTrue(keithley.live_channel_a.isEnabled())
            self.assertTrue(keithley.live_channel_b.isEnabled())
            self.assertIn("HIGH-Z relay open", keithley.live_timing.text())
            self.assertIn("never", keithley.live_channel_a.toolTip())
            keithley._controller.call.assert_not_called()
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_manual_output_uses_explicit_output_permission(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")
            button = keithley.channel_cards["B"]["output_on_action"]

            self.assertTrue(button.isEnabled())
            self.assertIn("output permission enabled", keithley.output_readiness.text())
            with patch(
                "app.devices.keithley_2600.ui.page.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                button.click()

            self.assertEqual(keithley._controller.call.call_args.args[0], "configure")
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_measure_does_not_toggle_or_disable_output_control(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")
            button = keithley.channel_cards["A"]["output_on_action"]
            self.assertTrue(button.isEnabled())

            keithley.channel_cards["A"]["measure"].click()
            keithley._update_output_readiness()

            keithley._controller.call.assert_called_once_with("measure", "A")
            self.assertTrue(button.isEnabled())
            self.assertEqual(keithley.channel_cards["A"]["output"].text(), "OUTPUT OFF")
            self.assertFalse(keithley._output_states["A"])
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_output_buttons_and_led_follow_confirmed_readback_state(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._device_state_changed("verified")

            for channel in ("A", "B"):
                card = keithley.channel_cards[channel]
                self.assertEqual(card["led"].property("outputState"), "neutral")
                self.assertEqual(card["output"].text(), "OUTPUT OFF")
                self.assertEqual(
                    card["output_on_action"].property("controlState"),
                    "available",
                )
                self.assertFalse(card["output_on_action"].isChecked())
                self.assertTrue(card["output_on_action"].isEnabled())
                self.assertFalse(card["output_off_action"].isEnabled())

            keithley._set_channel_output("A", True)
            channel_a = keithley.channel_cards["A"]
            channel_b = keithley.channel_cards["B"]
            self.assertEqual(channel_a["led"].property("outputState"), "active")
            self.assertEqual(channel_a["output"].text(), "OUTPUT ON")
            self.assertEqual(
                channel_a["output_on_action"].property("controlState"),
                "energized",
            )
            self.assertTrue(channel_a["output_on_action"].isChecked())
            self.assertTrue(channel_a["output_off_action"].isEnabled())
            self.assertEqual(channel_b["led"].property("outputState"), "neutral")
            self.assertFalse(channel_b["output_off_action"].isEnabled())

            keithley._device_state_changed("disconnected")
            for channel in ("A", "B"):
                card = keithley.channel_cards[channel]
                self.assertEqual(card["output"].text(), "OUTPUT UNKNOWN")
                self.assertEqual(card["led"].property("outputState"), "neutral")
                self.assertFalse(card["output_off_action"].isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_connection_buttons_distinguish_disconnected_and_connected_states(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            panel = window.connection_panels["keithley"]

            panel.update_state("disconnected")
            self.assertEqual(panel.connect_button.property("controlState"), "available")
            self.assertTrue(panel.connect_button.isEnabled())
            self.assertFalse(panel.disconnect_button.isEnabled())

            panel.update_state("verified")
            self.assertEqual(panel.connect_button.property("controlState"), "confirmed")
            self.assertTrue(panel.connect_button.isEnabled())
            self.assertTrue(panel.disconnect_button.isEnabled())
            self.assertEqual(panel.state.property("deviceState"), "verified")

            panel.set_connecting(True)
            panel.update_state("output_off")
            self.assertFalse(panel.connect_button.isEnabled())
            self.assertFalse(panel.disconnect_button.isEnabled())
            self.assertFalse(panel.test_button.isEnabled())
            panel.set_connecting(False)
            self.assertEqual(panel.state.text(), "OUTPUT OFF")
            self.assertEqual(panel.connect_button.property("controlState"), "confirmed")
            self.assertTrue(panel.disconnect_button.isEnabled())

            panel.update_state("disconnected")
            self.assertEqual(panel.connect_button.property("controlState"), "available")
            self.assertFalse(panel.disconnect_button.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_every_device_connection_panel_uses_the_same_confirmed_state_contract(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertGreaterEqual(len(window.connection_panels), 5)
            for panel in window.connection_panels.values():
                panel.update_state("disconnected")
                self.assertEqual(
                    panel.connect_button.property("controlState"), "available"
                )
                self.assertTrue(panel.connect_button.isEnabled())
                self.assertFalse(panel.disconnect_button.isEnabled())

                panel.update_state("verified")
                self.assertEqual(
                    panel.connect_button.property("controlState"), "confirmed"
                )
                self.assertTrue(panel.disconnect_button.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_rigol_output_state_is_confirmed_and_independent_per_channel(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            rigol = window.rigol_page
            rigol._controller.call = Mock()
            rigol._device_state_changed("verified")

            self.assertEqual(rigol.output_channel_state.text(), "CH1 OUTPUT OFF")
            self.assertEqual(rigol.output_on.property("controlState"), "available")
            self.assertTrue(rigol.output_on.isEnabled())
            self.assertFalse(rigol.output_off.isEnabled())

            rigol._pending_output_channel = 1
            rigol._device_state_changed("output_on")
            self.assertEqual(rigol.output_channel_state.text(), "CH1 OUTPUT UNKNOWN")
            self.assertNotEqual(rigol.output_on.property("controlState"), "energized")
            self.assertFalse(rigol.output_on.isChecked())
            rigol._result("set_output", True)
            self.assertEqual(rigol.output_channel_state.text(), "CH1 OUTPUT ON")
            self.assertEqual(rigol.output_on.property("controlState"), "energized")
            self.assertTrue(rigol.output_on.isChecked())
            self.assertTrue(rigol.output_off.isEnabled())

            rigol.channel.setCurrentText("2")
            self.assertEqual(rigol.output_channel_state.text(), "CH2 OUTPUT OFF")
            self.assertEqual(rigol.output_on.property("controlState"), "available")
            self.assertFalse(rigol.output_on.isChecked())
            self.assertFalse(rigol.output_off.isEnabled())

            rigol.channel.setCurrentText("1")
            self.assertEqual(rigol.output_channel_state.text(), "CH1 OUTPUT ON")
            self.assertTrue(rigol.output_on.isChecked())

            rigol._device_state_changed("unknown")
            self.assertEqual(rigol.output_channel_state.text(), "CH1 OUTPUT UNKNOWN")
            self.assertFalse(rigol.output_on.isEnabled())
            self.assertTrue(rigol.output_off.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_rf_buttons_follow_confirmed_output_state(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu.set_capabilities(
                DeviceCapabilities(
                    device_name="anritsu",
                    model="MS2830A",
                    firmware="sim",
                    features=frozenset({"spectrum_trace", "signal_generator"}),
                    hardware_options=("020",),
                )
            )
            anritsu._device_state_changed("verified")
            self.assertEqual(anritsu.sg_status.text(), "●  RF OUTPUT OFF")
            self.assertEqual(anritsu.sg_status.property("outputState"), "neutral")
            self.assertFalse(anritsu.sg_on.isChecked())
            self.assertFalse(anritsu.sg_off.isEnabled())

            anritsu._set_sg_output_state(True)
            anritsu._apply_page_state()
            self.assertEqual(anritsu.sg_status.text(), "●  RF OUTPUT ON")
            self.assertEqual(anritsu.sg_status.property("outputState"), "active")
            self.assertEqual(anritsu.sg_on.property("controlState"), "energized")
            self.assertTrue(anritsu.sg_on.isChecked())
            # The active state remains visibly checked, while the redundant
            # ON command is disabled and the safe OFF action stays available.
            self.assertFalse(anritsu.sg_on.isEnabled())
            self.assertTrue(anritsu.sg_off.isEnabled())

            anritsu._set_sg_output_state(None)
            anritsu._apply_page_state()
            self.assertIn("RF OUTPUT UNKNOWN", anritsu.sg_status.text())
            self.assertFalse(anritsu.sg_on.isEnabled())
            self.assertTrue(anritsu.sg_off.isEnabled())

            anritsu._device_state_changed("disconnected")
            self.assertFalse(anritsu.sg_on.isEnabled())
            self.assertFalse(anritsu.sg_off.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_ui_rejects_five_volts_when_profile_allows_millivolts(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")
            keithley.mode.setCurrentText("voltage")
            keithley.level.setText("5 V")
            keithley.compliance.setText("1 mA")

            with patch(
                "app.devices.keithley_2600.ui.page.QMessageBox.warning"
            ) as warning:
                keithley._output_toggled(True)

            keithley._controller.call.assert_not_called()
            self.assertIn("Invalid Keithley settings", keithley.banner.label.text())
            self.assertFalse(keithley._output_states["B"])
            self.assertIn("outside", warning.call_args.args[2])
            self.assertIn("No command was sent to Keithley", warning.call_args.args[2])
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_output_on_explains_unmet_preconditions_without_dispatch(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")
            channel = keithley._station_settings.keithley.safety.channels["B"]
            channels = dict(keithley._station_settings.keithley.safety.channels)
            channels["B"] = channel.model_copy(update={"enabled": False})
            safety = keithley._station_settings.keithley.safety.model_copy(
                update={"channels": channels}
            )
            keithley._station_settings = keithley._station_settings.model_copy(
                update={
                    "devices": keithley._station_settings.devices.model_copy(
                        update={
                            "keithley": keithley._station_settings.keithley.model_copy(
                                update={"safety": safety}
                            )
                        }
                    )
                }
            )
            keithley._update_output_readiness()
            button = keithley.channel_cards["B"]["output_on_action"]

            self.assertTrue(button.isEnabled())
            with patch(
                "app.devices.keithley_2600.ui.page.QMessageBox.warning"
            ) as warning:
                button.click()

            keithley._controller.call.assert_not_called()
            self.assertTrue(warning.called)
            message = warning.call_args.args[2]
            self.assertIn("channel B enabled", message)
            self.assertIn("No command was sent to Keithley", message)
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
            plot = keithley.history_widgets["A"]["plot"]
            x_range = plot.plot.viewRange()[0]
            self.assertLessEqual(x_range[0], 15.0)
            self.assertGreaterEqual(x_range[1], 45.0)
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
            self.assertTrue(keithley.apply_configuration_button.isVisible())
            self.assertGreater(keithley.apply_configuration_button.width(), 0)
            self.assertIs(keithley.live_channel_a.parentWidget(), keithley.hero_card)
            self.assertIs(keithley.live_channel_b.parentWidget(), keithley.hero_card)
            self.assertIs(keithley.live_interval.parentWidget(), keithley.hero_card)
            page_layout = keithley.layout()
            self.assertLess(
                page_layout.indexOf(keithley.hero_card),
                page_layout.indexOf(window.connection_panels["keithley"]),
            )

            window.resize(1050, 800)
            self.application.processEvents()
            self.assertEqual(
                keithley.workspace_splitter.orientation(),
                Qt.Orientation.Vertical,
            )
            self.assertGreater(keithley.level_field.width(), 480)
            self.assertTrue(keithley.apply_configuration_button.isVisible())
            self.assertGreater(keithley.apply_configuration_button.height(), 0)
            self.assertLess(
                keithley.level_field.editor.geometry().right(),
                keithley.level_field.minimum.geometry().left(),
            )
            self.assertLess(
                keithley.level_field.minimum.geometry().right(),
                keithley.level_field.maximum.geometry().left(),
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_high_z_off_keeps_derived_resistance_visible(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            measurement = SimpleNamespace(
                channel="A",
                voltage_v=50e-3,
                current_a=1e-3,
                power_w=50e-6,
                output_enabled=False,
                measurement_path_connected=False,
                compliance_detected=False,
            )

            keithley._result("measure", measurement)

            card = keithley.channel_cards["A"]
            self.assertEqual(card["resistance"].text(), "50 Ω")
            self.assertEqual(card["power"].text(), "50 µW")
            self.assertIn("HIGH-Z / FLOATING", card["compliance"].text())
            self.assertEqual(
                keithley._measurement_history["A"][-1]["resistance"], 50.0
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_all_four_keithley_live_panels_can_float_and_redock(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1600, 900)
            window.show()
            window._navigate_to("keithley")
            self.application.processEvents()
            keithley = window.keithley_page
            panel_keys = ("channel_A", "channel_B", "plot_A", "plot_B")

            for key in panel_keys:
                keithley._panel_float_buttons[key].click()
            self.application.processEvents()

            self.assertEqual(set(keithley._floating_panels), set(panel_keys))
            for key in panel_keys:
                floating = keithley._floating_panels[key]
                panel = keithley._panel_widgets[key]
                self.assertTrue(floating.isVisible())
                self.assertTrue(
                    bool(floating.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
                )
                self.assertIs(panel.window(), floating)
                self.assertGreater(panel.width(), 0)
                self.assertGreater(panel.height(), 0)
                self.assertTrue(keithley._panel_placeholders[key].isVisible())

            first_window = keithley._floating_panels["channel_A"]
            first_window.close()
            self.application.processEvents()
            self.assertNotIn("channel_A", keithley._floating_panels)
            self.assertIs(
                keithley._panel_widgets["channel_A"].parentWidget(),
                keithley._panel_slots["channel_A"],
            )
            self.assertTrue(keithley._panel_widgets["channel_A"].isVisible())

            keithley._panel_placeholders["plot_A"].findChild(QPushButton).click()
            self.application.processEvents()
            self.assertNotIn("plot_A", keithley._floating_panels)
            self.assertIs(
                keithley._panel_widgets["plot_A"].parentWidget(),
                keithley._panel_slots["plot_A"],
            )
        finally:
            for floating in list(window.keithley_page._floating_panels.values()):
                floating.close()
            window.close()
            self.application.processEvents()

    def test_keithley_output_switch_runs_configure_and_direct_enable_sequence(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")
            keithley._output_prerequisites = Mock(return_value=(True, ["✓ ready"]))

            keithley._output_toggled(True)
            self.assertEqual(keithley._controller.call.call_args.args[0], "configure")
            self.assertFalse(keithley.apply_configuration_button.isEnabled())

            keithley._result("configure", None)
            self.assertEqual(keithley._controller.call.call_args.args, ("set_output", ("B", True)))
            self.assertFalse(keithley.apply_configuration_button.isEnabled())

            keithley._result("set_output", True)
            self.assertTrue(keithley._output_states["B"])
            self.assertTrue(keithley.output_toggle.isChecked())
            self.assertEqual(keithley.output_toggle.text(), "OUTPUT ON")
            self.assertEqual(keithley.channel_cards["B"]["output_on_action"].text(), "OUTPUT ON")
            self.assertEqual(keithley.channel_cards["B"]["output_off_action"].text(), "OUTPUT OFF")
            self.assertTrue(keithley.apply_configuration_button.isEnabled())

            keithley._request_channel_output("B", False)
            self.assertEqual(keithley._controller.call.call_args.args, ("set_output", ("B", False)))
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_manual_ramp_is_hidden_and_cannot_dispatch(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()

            self.assertTrue(keithley.manual_ramp_panel.isHidden())
            self.assertFalse(keithley.ramp_preview_button.isEnabled())
            self.assertFalse(keithley.ramp_execute_button.isEnabled())

            keithley._preview_manual_ramp()
            keithley._set_channel_output("B", True)
            keithley._execute_manual_ramp()

            keithley._controller.call.assert_not_called()
            self.assertFalse(keithley.ramp_execute_button.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_manual_read_dispatches_a_fresh_single_sweep(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()

            anritsu.read_once()

            anritsu._controller.call.assert_called_once_with("single_sweep", "TRAC1")
            self.assertEqual(anritsu._page_state, AnritsuPageState.ACQUIRING_SPECTRUM)
            self.assertFalse(anritsu.single.isEnabled())
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
            trace_3 = SpectrumTrace(
                (1e6, 2e6), (-3.0, -17.0), datetime.now(timezone.utc), "TRAC1"
            )
            anritsu._show_trace(trace_3)
            anritsu.spectrum_plot.plot.setYRange(-100.0, -50.0, padding=0.0)
            anritsu.reference_operation.setCurrentIndex(1)
            anritsu._refresh_spectrum_display()
            self.assertTrue(anritsu.show_processed.isChecked())
            self.assertGreater(anritsu.spectrum_plot.trace_point_count("Processed"), 0)
            self.assertEqual(
                anritsu.spectrum_plot._traces["Processed"][1].tolist(),
                [-3.0, 3.0],
            )
            processed_y_range = anritsu.spectrum_plot.plot.viewRange()[1]
            self.assertLessEqual(processed_y_range[0], -3.0)
            self.assertGreaterEqual(processed_y_range[1], 3.0)
            anritsu.show_processed.setChecked(False)
            anritsu._refresh_spectrum_display()
            self.assertFalse(anritsu.show_processed.isChecked())
            self.assertEqual(anritsu.spectrum_plot.trace_point_count("Processed"), 0)
            anritsu.remove_reference()
            self.assertIsNone(anritsu._reference_trace)
            self.assertIs(anritsu._latest_trace, trace_3)
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

            anritsu._controller.call.assert_called_once_with("single_sweep", "TRAC1")
            self.assertEqual(anritsu._page_state, AnritsuPageState.ACQUIRING_REFERENCE)
            self.assertFalse(anritsu.live.isEnabled())
            anritsu._result("single_sweep", trace)

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
            anritsu.reference_operation.setCurrentIndex(1)
            anritsu._refresh_spectrum_display()
            self.assertEqual(anritsu.spectrum_plot.trace_point_count("Processed"), 0)
            self.assertIn("acquire the next spectrum", anritsu.analysis_status.text())

            anritsu._latest_trace = second
            with patch(
                "app.devices.anritsu_ms2830a.ui.page.QMessageBox.question",
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
            with patch(
                "app.devices.anritsu_ms2830a.ui.page.QMessageBox.warning"
            ) as modal_warning:
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

    def test_anritsu_temporal_average_uses_fresh_sweeps_and_updates_at_target(self) -> None:
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

            anritsu._controller.call.assert_called_once_with("single_sweep", "TRAC1")
            self.assertEqual(anritsu.average_progress.format(), "0 / 2")
            anritsu._result("single_sweep", first)
            self.assertEqual(anritsu.average_progress.value(), 1)
            self.assertEqual(anritsu.average_progress.format(), "1 / 2")
            self.assertIsNone(anritsu._averaged_trace)

            anritsu._result("single_sweep", second)
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

    def test_anritsu_live_passively_polls_current_trace_and_accepts_stable_frames(self) -> None:
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

            anritsu._controller.call.assert_called_once_with("start_live", True)
            self.assertTrue(anritsu._live_transition_pending)
            self.assertEqual(anritsu.live_indicator.property("liveState"), "starting")
            self.assertFalse(anritsu.configuration_panel.isEnabled())
            anritsu.toggle_live()
            self.assertEqual(anritsu._controller.call.call_count, 1)
            snapshot = AnritsuConfigurationSnapshot(1e6, 2e6, 0.0, 101, "SPECT")
            anritsu._result("start_live", snapshot)
            self.assertEqual(anritsu._spectrogram_buffer.row_count, 0)
            self.assertFalse(anritsu._live_transition_pending)
            self.assertFalse(anritsu.single.isEnabled())
            self.assertEqual(anritsu.live_indicator.property("liveState"), "on")
            self.assertFalse(anritsu.configuration_panel.isEnabled())
            anritsu._result("fetch_current_trace", trace)
            self.assertEqual(
                anritsu.spectrum_plot._traces["Raw"][1].tolist(),
                [-50.0, -40.0],
            )
            for _ in range(3):
                anritsu._result("fetch_current_trace", trace)

            self.assertEqual(anritsu._live_frame_count, 4)
            self.assertIn("FRAME 4", anritsu.live_indicator.text())
            self.assertEqual(anritsu._identical_live_frames, 3)
            self.assertIn("unchanged ×3", anritsu.info.text())
            self.assertTrue(anritsu.banner.isHidden())
            anritsu._controller.call.reset_mock()
            anritsu.toggle_live()
            anritsu._controller.call.assert_called_once_with("stop_live")
            self.assertEqual(anritsu.live_indicator.property("liveState"), "stopping")
            anritsu._result("stop_live", None)
            self.assertTrue(anritsu.single.isEnabled())
            self.assertEqual(anritsu.live.text(), "Start Live")
            self.assertEqual(anritsu.live_indicator.property("liveState"), "off")
            self.assertTrue(anritsu.configuration_panel.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_manual_read_retries_unmeasured_trace_without_new_sweep(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            with (
                patch(
                    "app.devices.anritsu_ms2830a.ui.page.time.monotonic",
                    return_value=100.0,
                ),
                patch(
                    "app.devices.anritsu_ms2830a.ui.page.QTimer.singleShot"
                ) as single_shot,
            ):
                anritsu.read_once()
                anritsu._error(
                    "fetch_current_trace",
                    "Anritsu returned the documented -999.0 "
                    "unmeasured/error sentinel for 10001 of 10001 trace points",
                )

            anritsu._controller.call.assert_called_once_with(
                "fetch_current_trace", "TRAC1"
            )
            single_shot.assert_called_once()
            self.assertFalse(anritsu._fetch_pending)
            self.assertEqual(anritsu._page_state, AnritsuPageState.IDLE)
            self.assertTrue(anritsu.banner.isHidden())
            self.assertIn("still being measured", anritsu.info.text())
            self.assertNotIn("INIT", str(anritsu._controller.call.call_args_list))
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_live_does_not_apply_form_configuration(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu.toggle_live()

            anritsu._controller.call.assert_called_once_with("start_live", True)
            self.assertTrue(anritsu._live_transition_pending)
            self.assertIsNone(anritsu._pending_after_spectrum_configuration)
            self.assertEqual(anritsu._page_state, AnritsuPageState.STARTING_LIVE)
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_applies_and_verifies_settings_without_enabling_output(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")
            keithley.channel.setCurrentText("B")
            keithley.mode.setCurrentText("current")
            keithley.level.setText("500 uA")
            keithley.compliance.setText("50 mV")
            keithley.settle.setText("200 ms")
            keithley.sense_mode.setCurrentText("4wire")

            self.assertTrue(keithley.apply_configuration_button.isEnabled())
            keithley.apply_configuration_button.click()

            keithley._controller.call.assert_called_once()
            operation, request = keithley._controller.call.call_args.args
            self.assertEqual(operation, "configure")
            self.assertEqual(request.channel, "B")
            self.assertEqual(request.mode, "current")
            self.assertEqual(request.level_si, 500e-6)
            self.assertEqual(request.compliance_si, 50e-3)
            self.assertEqual(request.settle_time_s, 0.2)
            self.assertEqual(request.sense_mode, "4wire")
            self.assertTrue(request.source_autorange)
            self.assertTrue(request.measure_voltage_autorange)
            self.assertTrue(request.measure_current_autorange)
            self.assertIsNone(keithley._auto_enable_channel)
            self.assertFalse(keithley.apply_configuration_button.isEnabled())
            self.assertEqual(
                keithley.apply_configuration_button.text(),
                "Applying & verifying…",
            )

            keithley._result("configure", None)

            keithley._controller.call.assert_called_once()
            self.assertFalse(keithley._output_states["B"])
            self.assertTrue(keithley.apply_configuration_button.isEnabled())
            self.assertIn("all instrument settings applied", keithley.banner.label.text())
            self.assertIn("settling time validated locally", keithley.banner.label.text())
            self.assertIn("OUTPUT remains OFF", keithley.banner.label.text())
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_read_once_passively_queries_current_trace(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            first = SpectrumTrace(
                (1e6, 2e6), (-50.0, -40.0), datetime.now(timezone.utc), "TRAC1"
            )
            second = SpectrumTrace(
                (3e6, 4e6), (-30.0, -20.0), datetime.now(timezone.utc), "TRAC1"
            )

            anritsu.hardware_info_button.click()
            diagnostics = anritsu._trace_diagnostics_dialog
            self.assertIsNotNone(diagnostics)
            anritsu._controller.call.assert_not_called()
            anritsu.single.click()

            anritsu._controller.call.assert_called_once_with(
                "fetch_current_trace", "TRAC1"
            )
            self.assertTrue(anritsu._fetch_pending)
            anritsu._result("fetch_current_trace", first)
            self.assertIs(anritsu._latest_trace, first)
            self.assertFalse(anritsu._fetch_pending)
            first_preview = diagnostics.raw_text.toPlainText()
            self.assertIn("received_frame: 1", first_preview)
            self.assertIn("0\t1000000\t-50", first_preview)
            self.assertIn("processing: none", first_preview)

            anritsu._controller.call.reset_mock()
            anritsu.single.click()

            anritsu._controller.call.assert_called_once_with(
                "fetch_current_trace", "TRAC1"
            )
            anritsu._result("fetch_current_trace", second)
            self.assertIs(anritsu._latest_trace, second)
            self.assertEqual(
                anritsu.spectrum_plot._traces["Raw"][0].tolist(), [3e6, 4e6]
            )
            self.assertEqual(
                anritsu.spectrum_plot._traces["Raw"][1].tolist(), [-30.0, -20.0]
            )
            second_preview = diagnostics.raw_text.toPlainText()
            self.assertIn("received_frame: 2", second_preview)
            self.assertIn("0\t3000000\t-30", second_preview)
            self.assertNotEqual(first_preview, second_preview)
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_trace_diagnostics_is_visible_and_updates_during_live(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1600, 900)
            window.show()
            window._navigate_to("anritsu")
            self.application.processEvents()
            anritsu = window.anritsu_page
            anritsu._controller.call = Mock()
            anritsu.refresh.setValue(5000)

            anritsu.hardware_info_button.click()
            self.application.processEvents()
            diagnostics = anritsu._trace_diagnostics_dialog
            self.assertIsNotNone(diagnostics)
            self.assertTrue(diagnostics.isVisible())
            self.assertGreaterEqual(diagnostics.width(), 540)
            self.assertGreaterEqual(diagnostics.height(), 400)
            self.assertTrue(diagnostics.raw_text.isVisible())
            self.assertGreater(diagnostics.raw_text.width(), 300)
            self.assertGreater(diagnostics.raw_text.height(), 180)
            self.assertTrue(diagnostics.raw_text.isReadOnly())
            anritsu._controller.call.assert_not_called()

            anritsu.toggle_live()
            frequencies = tuple(1e6 + index * 10e3 for index in range(101))
            anritsu._result(
                "start_live",
                AnritsuConfigurationSnapshot(1e6, 2e6, 0.0, 101, "SPECT"),
            )
            first = SpectrumTrace(
                frequencies,
                tuple(-51.25 + index / 100 for index in range(101)),
                datetime.now(timezone.utc),
                "TRAC1",
            )
            second = SpectrumTrace(
                frequencies,
                tuple(-31.75 + index / 100 for index in range(101)),
                datetime.now(timezone.utc),
                "TRAC1",
            )
            anritsu._result("fetch_current_trace", first)
            first_preview = diagnostics.raw_text.toPlainText()
            anritsu._result("fetch_current_trace", second)
            second_preview = diagnostics.raw_text.toPlainText()

            self.assertIn("received_frame: 1", first_preview)
            self.assertIn("0\t1000000\t-51.25", first_preview)
            self.assertIn("received_frame: 2", second_preview)
            self.assertIn("0\t1000000\t-31.75", second_preview)
            self.assertNotEqual(first_preview, second_preview)
            self.assertIn("Received frame 2", diagnostics.raw_status.text())
            self.assertEqual(
                [call.args[0] for call in anritsu._controller.call.call_args_list],
                ["start_live"],
            )

            diagnostics.resize(560, 420)
            self.application.processEvents()
            self.assertGreater(diagnostics.raw_text.width(), 300)
            self.assertGreater(diagnostics.raw_text.height(), 150)
            anritsu._timer.stop()
        finally:
            dialog = window.anritsu_page._trace_diagnostics_dialog
            if dialog is not None:
                dialog.close()
                self.application.processEvents()
            window.close()
            self.application.processEvents()

    def test_anritsu_controller_delivers_new_manual_and_live_frames_end_to_end(self) -> None:
        """Prove VISA -> worker thread -> page -> plot/diagnostics freshness."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            anritsu = window.anritsu_page
            connection = window.connection_panels["anritsu"]
            connection.connect_button.click()
            self.assertTrue(
                wait_for_ui(
                    lambda: connection.state.property("deviceState") == "verified"
                )
            )

            point_index = anritsu.points.findData(101)
            self.assertGreaterEqual(point_index, 0)
            anritsu.points.setCurrentIndex(point_index)
            anritsu.refresh.setValue(20)
            anritsu.configure_button.click()
            self.assertTrue(
                wait_for_ui(
                    lambda: anritsu._last_configuration is not None
                    and anritsu._last_configuration.points == 101
                    and anritsu._page_state == AnritsuPageState.IDLE
                )
            )

            anritsu.hardware_info_button.click()
            self.application.processEvents()
            diagnostics = anritsu._trace_diagnostics_dialog
            self.assertIsNotNone(diagnostics)

            anritsu.single.click()
            self.assertTrue(wait_for_ui(lambda: anritsu._received_trace_count == 1))
            first_values = anritsu.spectrum_plot._traces["Raw"][1].copy()
            first_preview = diagnostics.raw_text.toPlainText()

            anritsu.single.click()
            self.assertTrue(wait_for_ui(lambda: anritsu._received_trace_count == 2))
            second_values = anritsu.spectrum_plot._traces["Raw"][1].copy()
            second_preview = diagnostics.raw_text.toPlainText()
            self.assertNotEqual(first_values.tolist(), second_values.tolist())
            self.assertNotEqual(first_preview, second_preview)
            self.assertIn("received_frame: 2", second_preview)

            anritsu.live.click()
            self.assertTrue(wait_for_ui(lambda: anritsu._timer.isActive()))
            self.assertTrue(wait_for_ui(lambda: anritsu._received_trace_count >= 4))
            live_values = anritsu.spectrum_plot._traces["Raw"][1].copy()
            live_preview = diagnostics.raw_text.toPlainText()
            self.assertNotEqual(second_values.tolist(), live_values.tolist())
            self.assertNotEqual(second_preview, live_preview)
            self.assertIn(
                f"received_frame: {anritsu._received_trace_count}", live_preview
            )

            anritsu.live.click()
            self.assertTrue(
                wait_for_ui(
                    lambda: not anritsu._timer.isActive()
                    and anritsu._page_state == AnritsuPageState.IDLE
                )
            )
        finally:
            dialog = window.anritsu_page._trace_diagnostics_dialog
            if dialog is not None:
                dialog.close()
                self.application.processEvents()
            window.close()
            self.application.processEvents()

    def test_keithley_apply_settings_rejects_invalid_units_before_dispatch(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            keithley._device_state_changed("verified")
            keithley.mode.setCurrentText("current")
            keithley.level.setText("5 V")

            keithley.apply_configuration_button.click()

            keithley._controller.call.assert_not_called()
            self.assertIn("Invalid Keithley settings", keithley.banner.label.text())
            self.assertTrue(keithley.apply_configuration_button.isEnabled())
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_reads_both_channels_and_shows_modal_configuration(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley._controller.call = Mock()
            window.resize(1600, 900)
            window.show()
            window._navigate_to("keithley")
            keithley._device_state_changed("verified")
            self.application.processEvents()

            self.assertTrue(keithley.read_configuration_button.isVisible())
            self.assertTrue(keithley.read_configuration_button.isEnabled())
            keithley.read_configuration_button.click()

            keithley._controller.call.assert_called_once_with("read_configuration")
            self.assertTrue(keithley._readback_pending)
            self.assertFalse(keithley.read_configuration_button.isEnabled())
            self.assertEqual(
                keithley.read_configuration_button.text(), "Reading device…"
            )
            readback = KeithleyConfigurationReadback(
                (
                    KeithleyChannelConfigurationReadback(
                        channel="A",
                        output_enabled=False,
                        output_off_mode="high_impedance",
                        source_mode="current",
                        source_level_si=500e-6,
                        compliance_si=50e-3,
                        source_autorange=True,
                        source_range_si=1e-3,
                        nplc=0.5,
                        sense_mode="4wire",
                        measure_voltage_autorange=True,
                        measure_voltage_range_v=100e-3,
                        measure_current_autorange=False,
                        measure_current_range_a=1e-3,
                    ),
                    KeithleyChannelConfigurationReadback(
                        channel="B",
                        output_enabled=True,
                        output_off_mode="zero",
                        source_mode="voltage",
                        source_level_si=10e-3,
                        compliance_si=1e-3,
                        source_autorange=False,
                        source_range_si=67e-3,
                        nplc=2.0,
                        sense_mode="2wire",
                        measure_voltage_autorange=False,
                        measure_voltage_range_v=70e-3,
                        measure_current_autorange=True,
                        measure_current_range_a=10e-3,
                    ),
                )
            )
            with patch(
                "app.devices.keithley_2600.ui.page._KeithleyReadbackDialog.exec",
                return_value=0,
            ):
                keithley._result("read_configuration", readback)

            dialog = keithley._readback_dialog
            self.assertTrue(dialog.isModal())
            self.assertEqual(dialog.table.rowCount(), 13)
            table_values = {
                dialog.table.item(row, 0).text(): (
                    dialog.table.item(row, 1).text(),
                    dialog.table.item(row, 4).text(),
                )
                for row in range(dialog.table.rowCount())
            }
            self.assertEqual(table_values["OUTPUT state"], ("OFF", "ON"))
            self.assertEqual(table_values["OUTPUT OFF mode"], ("HIGH-Z", "ZERO"))
            self.assertEqual(table_values["Source mode"], ("CURRENT", "VOLTAGE"))
            self.assertEqual(table_values["Source level"], ("500 uA", "10 mV"))
            self.assertEqual(table_values["Compliance limit"], ("50 mV", "1 mA"))
            self.assertEqual(table_values["Sense mode"], ("4-wire", "2-wire"))
            status_values = {
                dialog.table.item(row, 0).text(): (
                    dialog.table.item(row, 2).text(),
                    dialog.table.item(row, 5).text(),
                )
                for row in range(dialog.table.rowCount())
            }
            self.assertTrue(status_values["Source level"][0])
            self.assertEqual(status_values["Active source range"][0], "MATCH")
            self.assertIsNotNone(dialog.table.cellWidget(2, 3))
            self.assertIsNotNone(dialog.table.cellWidget(2, 6))

            keithley.settings_assignment_requested.disconnect()
            dialog.assign_requested.emit("A", "Source autorange")
            keithley.channel.setCurrentText("A")
            self.assertEqual(keithley.mode.currentText(), "current")
            self.assertEqual(keithley.level.text(), "500 uA")
            self.assertEqual(keithley.compliance.text(), "50 mV")
            self.assertTrue(keithley.source_autorange.isChecked())
            self.assertEqual(keithley.source_range.text(), "AUTO")

            dialog.assign_requested.emit("ALL", "ALL")
            keithley.channel.setCurrentText("B")
            self.assertEqual(keithley.mode.currentText(), "voltage")
            self.assertEqual(keithley.level.text(), "10 mV")
            self.assertEqual(keithley.compliance.text(), "1 mA")
            self.assertEqual(keithley.nplc.text(), "2")
            self.assertFalse(keithley.source_autorange.isChecked())
            self.assertEqual(keithley.source_range.text(), "67 mV")
            self.assertFalse(keithley.measure_voltage_autorange.isChecked())
            self.assertEqual(keithley.measure_voltage_range.text(), "70 mV")

            keithley.channel.setCurrentText("A")
            self.assertEqual(keithley.mode.currentText(), "current")
            self.assertEqual(keithley.level.text(), "500 uA")
            self.assertEqual(keithley.compliance.text(), "50 mV")
            self.assertEqual(keithley.nplc.text(), "0.5")
            self.assertEqual(keithley.sense_mode.currentText(), "4wire")
            self.assertTrue(keithley.source_autorange.isChecked())
            self.assertEqual(keithley.source_range.text(), "AUTO")
            self.assertFalse(keithley.measure_current_autorange.isChecked())
            self.assertEqual(keithley.measure_current_range.text(), "1 mA")
            self.assertFalse(keithley._readback_pending)
            self.assertTrue(keithley.read_configuration_button.isEnabled())
            self.assertFalse(keithley._output_states["A"])
            self.assertTrue(keithley._output_states["B"])

            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.isVisible())
            self.assertGreater(dialog.table.width(), 600)
            self.assertGreater(dialog.table.height(), 300)
            dialog.close()
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_assigned_readback_defaults_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            repository = SettingsRepository(path)
            raw = repository.load().raw
            raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
            repository.save_raw(raw)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                snapshots = {
                    "A": KeithleyConfigurationSnapshot(
                        channel="A",
                        source_mode="current",
                        source_level="500 uA",
                        compliance="50 mV",
                        nplc="0.5",
                        settling_time="100 ms",
                        sense_mode="4wire",
                        source_autorange=True,
                        source_range="AUTO",
                        measure_voltage_autorange=True,
                        measure_voltage_range="AUTO",
                        measure_current_autorange=False,
                        measure_current_range="1 mA",
                    ),
                    "B": KeithleyConfigurationSnapshot(
                        channel="B",
                        source_mode="current",
                        source_level="2 mA",
                        compliance="60 mV",
                        nplc="2",
                        settling_time="200 ms",
                        sense_mode="2wire",
                        source_autorange=False,
                        source_range="10 mA",
                        measure_voltage_autorange=False,
                        measure_voltage_range="70 mV",
                        measure_current_autorange=True,
                        measure_current_range="AUTO",
                    ),
                }
                with patch(
                    "app.ui.shell.main_window.QMessageBox.critical",
                    return_value=None,
                ) as error:
                    window._save_keithley_readback_defaults(snapshots)
                self.assertFalse(error.called, window.keithley_page.banner.label.text())
                saved = SettingsRepository(path).load().raw["devices"]["keithley"][
                    "safety"
                ]["channels"]
                self.assertEqual(saved["A"]["defaults"]["source_current"], "500 uA")
                self.assertEqual(saved["A"]["defaults"]["sense_mode"], "4wire")
                self.assertEqual(saved["B"]["defaults"]["source_current"], "2 mA")
                self.assertEqual(saved["B"]["defaults"]["source_range"], "10 mA")
                before_invalid_save = path.read_text(encoding="utf-8")
                invalid = dict(snapshots)
                invalid["B"] = replace(snapshots["B"], compliance="670 mV")
                with patch(
                    "app.ui.shell.main_window.QMessageBox.critical",
                    return_value=None,
                ) as error:
                    window._save_keithley_readback_defaults(invalid)
                self.assertTrue(error.called)
                self.assertIn(
                    "outside", window.keithley_page.banner.label.text().lower()
                )
                self.assertEqual(path.read_text(encoding="utf-8"), before_invalid_save)
            finally:
                window.close()
                self.application.processEvents()

            restarted = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                keithley = restarted.keithley_page
                self.assertEqual(keithley.channel.currentText(), "B")
                self.assertEqual(keithley.level.text(), "2 mA")
                self.assertEqual(keithley.compliance.text(), "60 mV")
                self.assertEqual(keithley.nplc.text(), "2")
                self.assertFalse(keithley.source_autorange.isChecked())
                self.assertEqual(keithley.source_range.text(), "10 mA")
                keithley.channel.setCurrentText("A")
                self.assertEqual(keithley.level.text(), "500 uA")
                self.assertEqual(keithley.sense_mode.currentText(), "4wire")
                self.assertFalse(keithley.measure_current_autorange.isChecked())
                self.assertEqual(keithley.measure_current_range.text(), "1 mA")
            finally:
                restarted.close()
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
            self.assertEqual(len(anritsu._cleanup_history()), 24)
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

            self.assertTrue(wait_for_ui(lambda: anritsu._cleanup_result is not None))

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
            self.assertTrue(
                wait_for_ui(
                    lambda: anritsu._peak_table_dialog is not None
                    and anritsu._peak_table_dialog.table.rowCount() >= 2
                )
            )

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
            self.assertTrue(wait_for_ui(lambda: tracking.point_count == 2))
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
            anritsu._result("single_sweep", first)
            self.assertIsNone(anritsu._reference_trace)
            anritsu._result("single_sweep", second)

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
                with patch(
                    "app.devices.anritsu_ms2830a.ui.page.QMessageBox.question",
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

    def test_anritsu_form_defaults_survive_save_and_application_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                panel = window.anritsu_page.configuration_panel
                panel.start.setText("400 MHz")
                panel.stop.setText("5 GHz")
                panel.reference.setText("-12.5 dBm")
                panel.points.setCurrentIndex(panel.points.findData(2001))
                advanced = window.anritsu_page.advanced_configuration_panel
                advanced.rbw_mode.setCurrentIndex(
                    advanced.rbw_mode.findData("manual")
                )
                advanced.rbw.setText("2 MHz")
                advanced.vbw_mode.setCurrentIndex(
                    advanced.vbw_mode.findData("off")
                )
                advanced.detector.setCurrentIndex(
                    advanced.detector.findData("POS")
                )
                advanced.attenuation_mode.setCurrentIndex(
                    advanced.attenuation_mode.findData("manual")
                )
                advanced.attenuation.setValue(12)
                advanced.sweep_time_mode.setCurrentIndex(
                    advanced.sweep_time_mode.findData("manual")
                )
                advanced.sweep_time.setText("20 ms")
                window.anritsu_page.sg_frequency.setText("2 GHz")
                window.anritsu_page.sg_power.setText("-20 dBm")
                window.anritsu_page.average_count.setValue(321)
                window.anritsu_page.refresh.setValue(750)

                window._save_all_settings()

                defaults = SettingsRepository(path).load().raw["devices"][
                    "anritsu"
                ]["safety"]["defaults"]
                self.assertEqual(defaults["start_frequency"], "400 MHz")
                self.assertEqual(defaults["stop_frequency"], "5 GHz")
                self.assertEqual(defaults["reference_level"], "-12.5 dBm")
                self.assertEqual(defaults["sweep_points"], 2001)
                self.assertEqual(defaults["rbw"], "2 MHz")
                self.assertFalse(defaults["rbw_auto"])
                self.assertEqual(defaults["detector"], "POS")
                self.assertEqual(defaults["attenuation"], "12 dB")
                self.assertEqual(defaults["sweep_time"], "20 ms")
                generator = SettingsRepository(path).load().raw["devices"][
                    "anritsu"
                ]["signal_generator"]
                self.assertEqual(generator["default_frequency"], "2 GHz")
                self.assertEqual(generator["default_power"], "-20 dBm")
                acquisition = SettingsRepository(path).load().raw["devices"][
                    "anritsu"
                ]["acquisition"]
                self.assertEqual(acquisition["application_average_count"], 321)
                self.assertEqual(acquisition["live_refresh_interval"], "750 ms")
            finally:
                window.close()
                self.application.processEvents()

            restarted = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                panel = restarted.anritsu_page.configuration_panel
                self.assertEqual(panel.start.text(), "400 MHz")
                self.assertEqual(panel.stop.text(), "5 GHz")
                self.assertEqual(panel.reference.text(), "-12.5 dBm")
                self.assertEqual(panel.points.currentData(), 2001)
                advanced = restarted.anritsu_page.advanced_configuration_panel
                self.assertEqual(advanced.rbw_mode.currentData(), "manual")
                self.assertEqual(advanced.rbw.text(), "2 MHz")
                self.assertEqual(advanced.vbw_mode.currentData(), "off")
                self.assertEqual(advanced.detector.currentData(), "POS")
                self.assertEqual(advanced.attenuation.value(), 12)
                self.assertEqual(advanced.sweep_time.text(), "20 ms")
                self.assertEqual(restarted.anritsu_page.sg_frequency.text(), "2 GHz")
                self.assertEqual(restarted.anritsu_page.sg_power.text(), "-20 dBm")
                self.assertEqual(restarted.anritsu_page.average_count.value(), 321)
                self.assertEqual(restarted.anritsu_page.refresh.value(), 750)
            finally:
                restarted.close()
                self.application.processEvents()

    def test_rigol_channel_forms_survive_save_and_application_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                rigol = window.rigol_page
                rigol.channel.setCurrentText("1")
                rigol.level_mode.setCurrentText(rigol.LEVEL_MODE_HIGH_LOW)
                rigol.frequency.setText("2 kHz")
                rigol.high_level.setText("3 mV")
                rigol.low_level.setText("-2 mV")
                rigol.phase.setText("15")
                rigol.mod_enabled.setChecked(True)
                rigol.mod_type.setCurrentText("FM")
                rigol.mod_rate.setText("3 kHz")
                rigol.mod_parameter.setText("250 Hz")
                rigol.counter_coupling.setCurrentText("DC")
                rigol.counter_level.setText("20 mV")
                rigol.channel.setCurrentText("2")
                rigol.waveform.setCurrentText("SQU")
                rigol.level_mode.setCurrentText(rigol.LEVEL_MODE_HIGH_LOW)
                rigol.frequency.setText("4 kHz")
                rigol.high_level.setText("5 mV")
                rigol.low_level.setText("-4 mV")
                rigol.duty.setText("40")
                rigol.sweep_enabled.setChecked(True)
                rigol.sweep_start.setText("2 kHz")
                rigol.sweep_stop.setText("8 kHz")
                rigol.sweep_steps.setValue(23)
                rigol.burst_cycles.setValue(7)
                window.settings_page._raw["ui"]["theme"] = "dark"
                window.settings_page._dirty = True

                window._save_all_settings()
                saved_channels = SettingsRepository(path).load().settings.rigol.safety.channels
                self.assertTrue(saved_channels["1"].defaults["modulation_enabled"])
                self.assertTrue(saved_channels["2"].defaults["sweep_enabled"])
            finally:
                window.close()
                self.application.processEvents()

            restarted = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                rigol = restarted.rigol_page
                rigol.channel.setCurrentText("1")
                self.assertEqual(rigol.frequency.text(), "2 kHz")
                self.assertEqual(rigol.high_level.text(), "3 mV")
                self.assertEqual(rigol.low_level.text(), "-2 mV")
                self.assertEqual(rigol.phase.text(), "15")
                self.assertTrue(rigol.mod_enabled.isChecked())
                self.assertEqual(rigol.mod_type.currentText(), "FM")
                self.assertEqual(rigol.mod_rate.text(), "3 kHz")
                self.assertEqual(rigol.mod_parameter.text(), "250 Hz")
                self.assertEqual(rigol.counter_coupling.currentText(), "DC")
                self.assertEqual(rigol.counter_level.text(), "20 mV")
                rigol.channel.setCurrentText("2")
                self.assertEqual(rigol.waveform.currentText(), "SQU")
                self.assertEqual(rigol.frequency.text(), "4 kHz")
                self.assertEqual(rigol.high_level.text(), "5 mV")
                self.assertEqual(rigol.low_level.text(), "-4 mV")
                self.assertEqual(rigol.duty.text(), "40")
                self.assertTrue(rigol.sweep_enabled.isChecked())
                self.assertEqual(rigol.sweep_start.text(), "2 kHz")
                self.assertEqual(rigol.sweep_stop.text(), "8 kHz")
                self.assertEqual(rigol.sweep_steps.value(), 23)
                self.assertEqual(rigol.burst_cycles.value(), 7)
            finally:
                restarted.close()
                self.application.processEvents()

    def test_lakeshore_poll_interval_survives_global_settings_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                combo = window.lakeshore_gaussmeter_page.sample_interval
                combo.setCurrentIndex(combo.findData(2000))
                window.settings_page._raw["ui"]["theme"] = "dark"
                window.settings_page._dirty = True
                window._save_all_settings()
            finally:
                window.close()
                self.application.processEvents()

            restarted = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                self.assertEqual(
                    restarted.lakeshore_gaussmeter_page.sample_interval.currentData(),
                    2000,
                )
            finally:
                restarted.close()
                self.application.processEvents()

    def test_moke_display_preferences_survive_global_save_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                page = window.moke_box_page
                page.sample_interval.setCurrentIndex(
                    page.sample_interval.findData(2000)
                )
                page.refresh_interval.setCurrentIndex(
                    page.refresh_interval.findData(250)
                )
                page.history_window.setCurrentIndex(
                    page.history_window.findData(600)
                )
                window._save_all_settings()
            finally:
                window.close()
                self.application.processEvents()

            restarted = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                page = restarted.moke_box_page
                self.assertEqual(page.sample_interval.currentData(), 2000)
                self.assertEqual(page.refresh_interval.currentData(), 250)
                self.assertEqual(page.history_window.currentData(), 600)
            finally:
                restarted.close()
                self.application.processEvents()

    def test_keithley_manual_forms_survive_global_save_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                keithley = window.keithley_page
                keithley.channel.setCurrentText("A")
                keithley.nplc.setText("2")
                keithley.settle.setText("150 ms")
                keithley.channel.setCurrentText("B")
                keithley.nplc.setText("3")
                keithley.settle.setText("250 ms")
                window._save_all_settings()
                for _attempt in range(50):
                    if not window._keithley_defaults_in_flight:
                        break
                    QTest.qWait(100)
                self.assertFalse(window._keithley_defaults_in_flight)
            finally:
                window.close()
                self.application.processEvents()

            restarted = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                keithley = restarted.keithley_page
                keithley.channel.setCurrentText("A")
                self.assertEqual(keithley.nplc.text(), "2")
                self.assertEqual(keithley.settle.text(), "150 ms")
                keithley.channel.setCurrentText("B")
                self.assertEqual(keithley.nplc.text(), "3")
                self.assertEqual(keithley.settle.text(), "250 ms")
            finally:
                restarted.close()
                self.application.processEvents()

    def test_limit_only_changes_hot_apply_without_reconnecting_any_device(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=False)
        try:
            for controller in window._controllers.values():
                controller.call = Mock()
                controller.reconfigure = Mock()

            cases = (
                (
                    "rigol",
                    ("devices", "rigol", "safety", "channels", "1", "lab_limits", "frequency", "max"),
                    "900 kHz",
                ),
                (
                    "keithley",
                    ("devices", "keithley", "safety", "channels", "B", "lab_limits", "source_current", "max"),
                    "9 mA",
                ),
                (
                    "anritsu",
                    ("devices", "anritsu", "safety", "frequency", "max"),
                    "19 GHz",
                ),
            )
            for changed_device, path, value in cases:
                with self.subTest(device=changed_device):
                    window._device_states[changed_device] = "verified"
                    raw = window._settings.model_dump(mode="python")
                    target = raw
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    updated = StationSettings.model_validate(raw)
                    for controller in window._controllers.values():
                        controller.call.reset_mock()
                        controller.reconfigure.reset_mock()

                    window._settings_saved(updated)

                    for name, controller in window._controllers.items():
                        controller.reconfigure.assert_not_called()
                        expected_operation = (
                            "apply_limit_settings"
                            if name == changed_device
                            else "refresh_station_context"
                        )
                        self.assertEqual(
                            controller.call.call_args.args[0],
                            expected_operation,
                        )
        finally:
            window.close()
            self.application.processEvents()

    def test_global_save_uses_one_yaml_transaction_and_no_adapter_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                for controller in window._controllers.values():
                    controller.call = Mock()
                    controller.reconfigure = Mock()

                window.rigol_page.frequency.setText("2 kHz")
                window.anritsu_page.average_count.setValue(123)
                window.keithley_page.channel.setCurrentText("B")
                window.keithley_page.nplc.setText("2")
                with patch.object(
                    window._repository,
                    "save_raw",
                    wraps=window._repository.save_raw,
                ) as save_raw:
                    started = time.perf_counter()
                    window._save_all_settings()
                    elapsed = time.perf_counter() - started

                self.assertEqual(save_raw.call_count, 1)
                self.assertLess(elapsed, 1.0)
                self.assertFalse(window._keithley_defaults_in_flight)
                for controller in window._controllers.values():
                    controller.reconfigure.assert_not_called()
                    controller.call.assert_called_with(
                        "refresh_station_context", window._settings
                    )
                persisted = SettingsRepository(path).load().settings
                self.assertEqual(
                    persisted.rigol.safety.channels["1"].defaults["frequency"],
                    "2 kHz",
                )
                self.assertEqual(
                    persisted.anritsu.acquisition.application_average_count,
                    123,
                )
                self.assertEqual(
                    persisted.keithley.safety.channels["B"].defaults["nplc"],
                    2.0,
                )
            finally:
                window.close()
                self.application.processEvents()

    def test_limit_edit_button_opens_popup_and_stages_the_range(self) -> None:
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
                    "Values are validated before saving", field.edit_button.toolTip()
                )

                def complete_dialog() -> None:
                    dialog = QApplication.activeModalWidget()
                    self.assertIsInstance(dialog, LimitEditDialog)
                    dialog.minimum.setText("0 A")
                    dialog.maximum.setText("9 mA")
                    dialog.accept()

                QTimer.singleShot(0, complete_dialog)
                field.edit_button.click()
                before_save = SettingsRepository(path).load()
                self.assertNotEqual(
                    before_save.raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]["source_current"]["max"],
                    "9 mA",
                )
                window.safety_strip.save_settings.click()
                loaded = SettingsRepository(path).load()
                limits = loaded.raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
                self.assertEqual(limits["source_current"]["max"], "9 mA")
                self.assertEqual(limits["source_current"]["max_abs"], "9 mA")
                self.assertEqual(field.maximum.text(), "MAX  9 mA")
                self.assertFalse(field.validation_warning.isVisible())
                self.assertIsNot(
                    window.stackedWidget.currentWidget(),
                    window.navigation_routes["settings"],
                )
            finally:
                window.close()
                self.application.processEvents()

    def test_limit_edit_synchronizes_hidden_absolute_bound(self) -> None:
        self.assertEqual(
            MainWindow._synchronised_max_abs("-10 mA", "10 mA", "1 mA"),
            "10 mA",
        )

    def test_failed_limit_hot_apply_shows_error_and_restores_saved_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                original = window._settings.keithley.safety.channels[
                    "B"
                ].lab_limits.source_current
                raw = window._repository.load().raw
                changed = raw["devices"]["keithley"]["safety"]["channels"][
                    "B"
                ]["lab_limits"]["source_current"]
                changed["max"] = "9 mA"
                changed["max_abs"] = "9 mA"
                updated = window._repository.save_raw(raw)
                for controller in window._controllers.values():
                    controller.call = Mock()

                window._device_states["keithley"] = "verified"
                window._settings_saved(updated)
                self.assertEqual(
                    window.keithley_page._limit_fields["level"].maximum.text(),
                    "MAX  9 mA",
                )

                with patch(
                    "app.ui.shell.main_window.QMessageBox.critical"
                ) as critical:
                    window._device_error(
                        "keithley",
                        "apply_limit_settings",
                        "OUTPUT A and OUTPUT B must be confirmed OFF.",
                    )

                critical.assert_called_once()
                self.assertIn("rolled back", critical.call_args.args[2])
                restored = SettingsRepository(path).load().settings
                self.assertEqual(
                    restored.keithley.safety.channels[
                        "B"
                    ].lab_limits.source_current,
                    original,
                )
                self.assertEqual(
                    window.keithley_page._limit_fields["level"].maximum.text(),
                    f"MAX  {original.max_abs}",
                )
            finally:
                window.close()
                self.application.processEvents()

    def test_keithley_maximum_power_limit_is_staged_then_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                window.resize(1600, 900)
                window.show()
                window._navigate_to("keithley")
                self.application.processEvents()
                field = window.keithley_page._limit_fields["max_abs_power"]
                self.assertTrue(field.isVisible())
                self.assertEqual(field.editor.text(), "670 uW")

                def complete_dialog() -> None:
                    dialog = QApplication.activeModalWidget()
                    self.assertIsInstance(dialog, LimitEditDialog)
                    dialog.minimum.setText("4 mW")
                    dialog.accept()

                QTimer.singleShot(0, complete_dialog)
                field.edit_button.click()
                self.assertEqual(
                    SettingsRepository(path).load().settings.keithley.safety.channels[
                        "B"
                    ].lab_limits.max_abs_power,
                    "670 uW",
                )
                window.safety_strip.save_settings.click()
                saved = SettingsRepository(path).load().settings
                self.assertEqual(
                    saved.keithley.safety.channels["B"].lab_limits.max_abs_power,
                    "4 mW",
                )
                self.assertEqual(field.editor.text(), "4 mW")
            finally:
                window.close()
                self.application.processEvents()

    def test_keithley_template_declares_every_manual_form_default(self) -> None:
        raw = SettingsRepository(SETTINGS_TEMPLATE).load().raw
        channels = raw["devices"]["keithley"]["safety"]["channels"]
        expected_defaults = {
            "source_mode",
            "output_enabled",
            "sense_mode",
            "nplc",
            "settling_time",
            "source_autorange",
            "source_range",
            "measure_voltage_autorange",
            "measure_voltage_range",
            "measure_current_autorange",
            "measure_current_range",
            "measure_delay",
        }
        for channel in ("A", "B"):
            self.assertTrue(
                expected_defaults <= channels[channel]["defaults"].keys(),
                channel,
            )
            self.assertIn("max_abs_power", channels[channel]["lab_limits"])

    def test_keithley_working_values_do_not_stage_or_write_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            repository = SettingsRepository(path)
            raw = repository.load().raw
            raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
            repository.save_raw(raw)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                keithley = window.keithley_page
                keithley.level.setText("2 mA")
                keithley.level.editingFinished.emit()
                keithley.compliance.setText("60 mV")
                keithley.compliance.editingFinished.emit()
                before_save = SettingsRepository(path).load().raw["devices"][
                    "keithley"
                ]["safety"]["channels"]["B"]["defaults"]
                self.assertEqual(before_save["source_current"], "1 mA")
                QTest.qWait(2_600)
                still_unsaved = SettingsRepository(path).load().raw["devices"][
                    "keithley"
                ]["safety"]["channels"]["B"]["defaults"]
                self.assertEqual(still_unsaved["source_current"], "1 mA")
                self.assertIsNone(window._pending_keithley_defaults)
                window.safety_strip.save_settings.click()
                for _attempt in range(30):
                    if not window._keithley_defaults_in_flight:
                        break
                    QTest.qWait(100)
                defaults = SettingsRepository(path).load().raw["devices"]["keithley"][
                    "safety"
                ]["channels"]["B"]["defaults"]
                self.assertEqual(defaults["source_current"], "1 mA")
                self.assertNotEqual(defaults["voltage_compliance"], "60 mV")
                self.assertEqual(keithley.level.text(), "2 mA")
                self.assertEqual(keithley.compliance.text(), "60 mV")
            finally:
                window.close()
                self.application.processEvents()

    def test_keithley_settings_refresh_preserves_the_active_form_draft(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            keithley.mode.setCurrentText("current")
            keithley.level.setText("3 mA")
            keithley.compliance.setText("60 mV")
            keithley._remember_source_values()

            keithley.set_settings(window._settings)

            self.assertEqual(keithley.level.text(), "3 mA")
            self.assertEqual(keithley.compliance.text(), "60 mV")
        finally:
            window.close()
            self.application.processEvents()

    def test_anritsu_frequency_limit_edit_updates_both_range_badges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                first = window.anritsu_page._limit_fields["frequency0"]
                second = window.anritsu_page._limit_fields["frequency1"]

                def complete_dialog() -> None:
                    dialog = QApplication.activeModalWidget()
                    self.assertIsInstance(dialog, LimitEditDialog)
                    dialog.minimum.setText("100 MHz")
                    dialog.maximum.setText("20 GHz")
                    dialog.accept()

                QTimer.singleShot(0, complete_dialog)
                with patch.object(
                    window.anritsu_page.banner, "show_message"
                ) as show_message:
                    first.edit_button.click()

                self.assertEqual(first.minimum.text(), "MIN  100 MHz")
                self.assertEqual(second.minimum.text(), "MIN  100 MHz")
                self.assertIn("SAVE SETTINGS", show_message.call_args.args[0])
                persisted = SettingsRepository(path).load().settings
                self.assertNotEqual(
                    persisted.anritsu.safety.frequency.min, "100 MHz"
                )
            finally:
                window.close()
                self.application.processEvents()

    def test_keithley_ranges_are_direct_manual_fields_not_trip_limit_editors(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            keithley = window.keithley_page
            for key in (
                "source_range",
                "measure_voltage_range",
                "measure_current_range",
            ):
                self.assertTrue(keithley._limit_fields[key].edit_button.isHidden())

            self.assertFalse(keithley.measure_voltage_range.isEnabled())
            keithley.measure_voltage_autorange.setChecked(False)
            self.assertTrue(keithley.measure_voltage_range.isEnabled())
            keithley.measure_voltage_range.setText("1 V")
            keithley.measure_voltage_range.editingFinished.emit()
            self.assertIsNone(window._pending_keithley_defaults)
            self.assertEqual(keithley.measure_voltage_range.text(), "1 V")
        finally:
            window.close()
            self.application.processEvents()

    def test_keithley_background_save_does_not_block_the_gui_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                from app.ui import settings_workers

                original_save = settings_workers.persist_keithley_default_snapshots

                def slow_save(*args: object, **kwargs: object) -> object:
                    time.sleep(0.4)
                    return original_save(*args, **kwargs)

                gui_ticks: list[bool] = []
                snapshots = dict(window.keithley_page._channel_form_snapshots)
                with patch(
                    "app.ui.settings_workers.persist_keithley_default_snapshots",
                    side_effect=slow_save,
                ):
                    started = time.perf_counter()
                    window._queue_keithley_assignment_save(snapshots)
                    self.assertLess(time.perf_counter() - started, 0.2)
                    self.assertFalse(window._keithley_defaults_in_flight)
                    window.safety_strip.save_settings.click()
                    QTimer.singleShot(20, lambda: gui_ticks.append(True))
                    QTest.qWait(120)
                    self.assertEqual(gui_ticks, [True])
                    self.assertTrue(window._keithley_defaults_in_flight)
                    for _attempt in range(30):
                        if not window._keithley_defaults_in_flight:
                            break
                        QTest.qWait(100)
                self.assertFalse(window._keithley_defaults_in_flight)
            finally:
                window.close()
                self.application.processEvents()

    def test_settings_page_exposes_and_manually_saves_keithley_maximum_power(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            try:
                settings = window.settings_page
                limit_path = (
                    "devices", "keithley", "safety", "channels", "B",
                    "lab_limits", "max_abs_power",
                )
                editor = settings._safety_limit_editors[limit_path]
                self.assertEqual(editor.text(), "670 uW")
                editor.setText("4 mW")
                editor.editingFinished.emit()
                QTest.qWait(900)
                self.assertEqual(
                    SettingsRepository(path).load().raw["devices"]["keithley"]
                    ["safety"]["channels"]["B"]["lab_limits"]["max_abs_power"],
                    "670 uW",
                )
                window.safety_strip.save_settings.click()
                self.assertEqual(
                    SettingsRepository(path).load().raw["devices"]["keithley"]
                    ["safety"]["channels"]["B"]["lab_limits"]["max_abs_power"],
                    "4 mW",
                )
            finally:
                window.close()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
