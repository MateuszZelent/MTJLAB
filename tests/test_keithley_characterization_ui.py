"""UI tests for Keithley sample characterization card and tab integration in KeithleyPage."""

from __future__ import annotations

from copy import deepcopy
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.ui.page import KeithleyPage
from app.devices.keithley_2600.ui.characterization_card import KeithleyCharacterizationCard
from app.devices.simulators import simulated_station_settings
from app.settings.models import StationSettings
from app.ui.shell.page_host import FluentPageHost
from app.ui.widgets import LimitField
from tests.helpers import loaded_settings


class KeithleyCharacterizationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        self.settings = StationSettings.model_validate(raw)
        self.controller = Mock()
        self.page = KeithleyPage(self.controller, self.settings)

    def tearDown(self) -> None:
        try:
            if hasattr(self, "page") and self.page is not None:
                self.page.deleteLater()
        except RuntimeError:
            pass
        self.app.processEvents()

    def test_keithley_page_characterization_integration(self) -> None:
        """Verify KeithleyPage exposes characterization card and quick-access action."""
        self.assertIsInstance(self.page.characterization_card, KeithleyCharacterizationCard)
        self.assertIsNotNone(self.page.characterization_button)
        self.assertEqual(self.page.characterization_button.text(), "Characterization…")

        emitted: list[bool] = []
        self.page.characterization_requested.connect(lambda: emitted.append(True))
        self.page.characterization_button.click()
        self.assertEqual(emitted, [True])

        self.page.jump_to_characterization()
        self.assertEqual(emitted, [True, True])

    def test_characterization_card_elements_and_geometry(self) -> None:
        """Verify characterization page controls are rendered with non-zero geometry."""
        card = self.page.characterization_card
        host = FluentPageHost(card)
        try:
            host.resize(1366, 768)
            host.show()
            self.app.processEvents()

            self.assertTrue(card.isVisible())
            self.assertGreater(card.width(), 400)
            self.assertGreater(card.height(), 300)

            # Check essential controls
            self.assertTrue(card.start_button.isVisible())
            self.assertTrue(card.stop_button.isVisible())
            self.assertTrue(card.pdf_button.isVisible())
            self.assertTrue(card.csv_button.isVisible())
            self.assertTrue(card.plot_widget.isVisible())

            # Start button should be enabled, stop and export initially disabled
            self.assertTrue(card.start_button.isEnabled())
            self.assertFalse(card.stop_button.isEnabled())
            self.assertFalse(card.pdf_button.isEnabled())
            self.assertFalse(card.csv_button.isEnabled())
        finally:
            host.close()

    def test_channel_change_autofills_limits(self) -> None:
        """Verify selecting Channel B autofills limits from station settings."""
        card = self.page.characterization_card
        card.channel_combo.setCurrentText("Channel B")
        self.app.processEvents()

        # Channel B in simulated settings has voltage compliance 67 mV and max current 10 mA
        self.assertEqual(card.compliance_edit.text(), "67 mV")
        self.assertEqual(card.stop_level_edit.text(), "10 mA")

    def test_preflight_rejection_shows_banner(self) -> None:
        """Verify invalid or out-of-limits parameters display a warning message in banner."""
        card = self.page.characterization_card
        card.channel_combo.setCurrentText("Channel B")
        # Set level far exceeding channel limit
        card.stop_level_edit.setText("500 mA")
        card._on_start_clicked()
        self.app.processEvents()

        # Banner should display safety violation in English
        self.assertIn("safety preflight rejection", card.banner.last_message.lower())

    def test_mode_change_updates_limits_and_labels(self) -> None:
        """Verify switching mode updates units, fields, and plot axis labels."""
        card = self.page.characterization_card
        # Initially in current mode
        self.assertIn("Demanded Current", card.plot_widget.getAxis("bottom").labelText)
        self.assertIn("Voltage Response", card.plot_widget.getAxis("left").labelText)

        # Switch to voltage mode
        card.mode_combo.setCurrentText("Voltage Sweep (V → I)")
        self.app.processEvents()

        # Plot labels should update to English voltage sweep titles
        self.assertIn("Demanded Voltage", card.plot_widget.getAxis("bottom").labelText)
        self.assertIn("Current Response", card.plot_widget.getAxis("left").labelText)

        # Limits should update to voltage levels and current compliance
        self.assertIn("V", card.stop_level_edit.text())
        self.assertIn("A", card.compliance_edit.text())

    def test_set_settings_updates_characterization_card(self) -> None:
        """Verify updating station settings on KeithleyPage propagates to characterization card."""
        card = self.page.characterization_card
        # Deepcopy and modify limits for Channel A (50 mV is safely within measured_voltage_trip)
        updated = deepcopy(self.settings.model_dump(mode="python"))
        updated["devices"]["keithley"]["safety"]["channels"]["A"]["lab_limits"]["voltage_compliance"]["max"] = "50 mV"
        new_settings = StationSettings.model_validate(updated)

        self.page.set_settings(new_settings)
        self.app.processEvents()

        # Switch to Channel A
        card.channel_combo.setCurrentText("Channel A")
        self.app.processEvents()
        self.assertEqual(card.compliance_edit.text(), "50 mV")

    def test_live_compliance_marker_plotting(self) -> None:
        """Verify that acquired points with active compliance update the clamped curve."""
        card = self.page.characterization_card
        from app.devices.keithley_2600.characterization.models import CharacterizationPoint
        pt = CharacterizationPoint(
            index=0,
            demanded_si=0.005,
            measured_voltage_v=0.670,
            measured_current_a=0.0014,
            true_resistance_ohm=450.0,
            apparent_resistance_ohm=134.0,
            power_w=0.0009,
            compliance_active=True,
            timestamp_epoch=1.0,
        )
        card._on_point_acquired(pt)
        self.app.processEvents()

        # Clamped curve should contain the point
        x_data, y_data = card.curve_clamped.getData()
        self.assertEqual(len(x_data), 1)
        self.assertAlmostEqual(x_data[0], 0.005)
        self.assertAlmostEqual(y_data[0], 0.670)

        # Resistance curve should also contain the point
        xr_data, yr_data = card.curve_r_true.getData()
        self.assertEqual(len(xr_data), 1)
        self.assertAlmostEqual(xr_data[0], 0.005)
        self.assertAlmostEqual(yr_data[0], 450.0)

        # Switch to resistance view and verify curve visibility
        card._set_plot_view(1)
        self.app.processEvents()
        self.assertTrue(card.curve_r_true.isVisible())
        self.assertFalse(card.curve_iv.isVisible())
        self.assertIn("Resistance", card.plot_widget.getAxis("left").labelText)

        # Switch back to IV view
        card._set_plot_view(0)
        self.app.processEvents()
        self.assertTrue(card.curve_iv.isVisible())
        self.assertFalse(card.curve_r_true.isVisible())

    def test_disconnected_device_shows_banner(self) -> None:
        """Verify that starting a sweep when device is disconnected shows an error banner."""
        card = self.page.characterization_card
        card.channel_combo.setCurrentText("Channel B")
        self.app.processEvents()

        proxy = Mock()
        proxy.connected = False
        self.controller.adapter_for_run.return_value = proxy

        card._on_start_clicked()
        self.app.processEvents()
        self.assertIn("not connected", card.banner.last_message.lower())

    def test_main_window_keithley_characterization_navigation(self) -> None:
        """Verify MainWindow hosts characterization route under apparatus navigation."""
        from app.ui.shell.main_window import MainWindow

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1366, 768)
            window.show()
            self.app.processEvents()

            self.assertIn("keithley_characterization", window.navigation_routes)
            host = window.navigation_routes["keithley_characterization"]
            self.assertIs(host.content, window.keithley_page.characterization_card)

            char_nav_item = window.navigationInterface.widget(host.objectName())
            keithley_nav_item = window.navigationInterface.widget("keithleyPageHost")
            devices_nav_item = window.navigationInterface.widget("apparatusMenu")
            self.assertIs(char_nav_item.parent(), keithley_nav_item)
            self.assertIs(char_nav_item.treeParent, keithley_nav_item)
            self.assertEqual(char_nav_item.nodeDepth, 2)
            self.assertEqual(keithley_nav_item.nodeDepth, 1)
            self.assertEqual(devices_nav_item.nodeDepth, 0)
            self.assertEqual(char_nav_item.text(), "Characterization")

            # Request jump from Keithley dashboard
            window._navigate_to("keithley")
            self.app.processEvents()
            self.assertIs(window.stackedWidget.currentWidget(), window.navigation_routes["keithley"])

            window.keithley_page.characterization_button.click()
            self.app.processEvents()
            self.assertIs(
                window.stackedWidget.currentWidget(),
                window.navigation_routes["keithley_characterization"],
            )
        finally:
            window.close()

    def test_characterization_card_limit_fields_and_badges(self) -> None:
        """Verify characterization inputs are wrapped in LimitField with MIN/MAX badges."""
        card = self.page.characterization_card
        self.assertIsInstance(card.start_level_field, LimitField)
        self.assertIsInstance(card.stop_level_field, LimitField)
        self.assertIsInstance(card.compliance_field, LimitField)
        self.assertIsInstance(card.dwell_field, LimitField)

        # Default is Channel B in Current mode
        self.assertIn("MIN", card.start_level_field.minimum.text())
        self.assertIn("MAX", card.start_level_field.maximum.text())
        self.assertIn("0 mA", card.start_level_field.minimum.text())
        self.assertIn("10 mA", card.start_level_field.maximum.text())

        self.assertIn("0 mA", card.stop_level_field.minimum.text())
        self.assertIn("10 mA", card.stop_level_field.maximum.text())

        self.assertIn("67 mV", card.compliance_field.maximum.text())
        self.assertIn("ms", card.dwell_field.minimum.text().lower())

    def test_channel_bidirectional_synchronization(self) -> None:
        """Verify KeithleyPage and CharacterizationCard channel selection stays synchronized."""
        card = self.page.characterization_card

        # Change on KeithleyPage -> propagates to card
        self.page.channel.setCurrentText("A")
        self.app.processEvents()
        self.assertEqual(card.channel_combo.currentText(), "Channel A")

        # Change on card -> propagates to KeithleyPage
        card.channel_combo.setCurrentText("Channel B")
        self.app.processEvents()
        self.assertEqual(self.page.channel.currentText(), "B")

    def test_limits_synchronization_across_pages(self) -> None:
        """Verify updating station safety limits synchronizes both cards in lockstep."""
        card = self.page.characterization_card
        card.channel_combo.setCurrentText("Channel B")
        self.app.processEvents()

        # Update Channel B limits in settings
        updated = deepcopy(self.settings.model_dump(mode="python"))
        ch_limits = updated["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
        ch_limits["voltage_compliance"]["max"] = "45 mV"
        ch_limits["source_current"]["min"] = "0 mA"
        ch_limits["source_current"]["max"] = "8 mA"
        new_settings = StationSettings.model_validate(updated)

        self.page.set_settings(new_settings)
        self.app.processEvents()

        # Check badges on KeithleyPage
        page_compliance = self.page.configuration_panel.limit_fields["compliance"]
        page_level = self.page.configuration_panel.limit_fields["level"]
        self.assertIn("45 mV", page_compliance.maximum.text())
        self.assertIn("8 mA", page_level.maximum.text())
        self.assertIn("0 mA", page_level.minimum.text())

        # Check badges on Characterization card - must be 100% synchronized
        self.assertIn("45 mV", card.compliance_field.maximum.text())
        self.assertIn("8 mA", card.stop_level_field.maximum.text())
        self.assertIn("0 mA", card.start_level_field.minimum.text())

    def test_characterization_field_clamp_on_limit_violation(self) -> None:
        """Verify that entering an out-of-limits value clamps to MIN/MAX badge."""
        card = self.page.characterization_card
        card.channel_combo.setCurrentText("Channel B")
        self.app.processEvents()

        # Set stop level beyond the 10 mA maximum
        card.stop_level_edit.setText("50 mA")
        clamped = card.stop_level_field.validate_and_clamp()
        self.assertFalse(clamped)
        self.assertEqual(card.stop_level_edit.text(), "10 mA")
        self.assertFalse(card.stop_level_field.validation_warning.isHidden())
        self.assertIn("exceeded max", card.stop_level_field.validation_warning.text().lower())

    def test_main_window_limit_edit_spec_for_characterization(self) -> None:
        """Verify MainWindow _limit_edit_spec correctly identifies characterization fields."""
        from app.ui.shell.main_window import MainWindow

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            char_page = window.keithley_characterization_page
            char_page.channel_combo.setCurrentText("Channel B")
            char_page.mode_combo.setCurrentText("Current Sweep (I → V)")
            self.app.processEvents()

            title, path, max_enabled = window._limit_edit_spec("keithley", char_page.start_level_field)
            self.assertIn("CHB", title)
            self.assertIn("source current", title.lower())
            self.assertEqual(
                path,
                ("devices", "keithley", "safety", "channels", "B", "lab_limits", "source_current"),
            )
            self.assertTrue(max_enabled)

            # Test compliance
            title_comp, path_comp, _ = window._limit_edit_spec("keithley", char_page.compliance_field)
            self.assertIn("CHB", title_comp)
            self.assertIn("voltage compliance", title_comp.lower())
            self.assertEqual(
                path_comp,
                ("devices", "keithley", "safety", "channels", "B", "lab_limits", "voltage_compliance"),
            )
        finally:
            window.close()

    def test_inventory_store_samples_and_cascading_devices(self) -> None:
        """Verify sample list from InventoryStore is loaded and cascading devices populate correctly."""
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from app.inventory.models import Sample
        from app.inventory.store import InventoryStore

        with TemporaryDirectory() as tmpdir:
            store = InventoryStore(Path(tmpdir) / "test_inventory.db")
            try:
                sample1 = Sample(
                    sample_id="SAMPLE-A1",
                    name="Wafer 4 Wedge",
                    rows=("1", "2"),
                    cols=("A", "B"),
                    device_labels={"1,A": "Pillar 100nm", "2,B": "Pillar 250nm"},
                )
                sample2 = Sample(
                    sample_id="SAMPLE-B2",
                    name="Wafer 5 Flat",
                    rows=("10",),
                    cols=("1", "2"),
                )
                store.save_sample(sample1)
                store.save_sample(sample2)

                card = self.page.characterization_card
                card.set_inventory_store(store)
                self.app.processEvents()

                # Verify sample combo items
                sample_ids = [card.sample_combo.itemData(i) for i in range(card.sample_combo.count())]
                self.assertIn("SAMPLE-A1", sample_ids)
                self.assertIn("SAMPLE-B2", sample_ids)
                self.assertIn("", sample_ids)  # (Custom / Manual)

                # Select SAMPLE-A1
                idx1 = card._find_sample_index("SAMPLE-A1")
                card.sample_combo.setCurrentIndex(idx1)
                self.app.processEvents()

                self.assertEqual(card.sample_id_edit.text(), "SAMPLE-A1")
                # Devices combo should have 4 cells: 1:A, 1:B, 2:A, 2:B
                self.assertEqual(card.device_combo.count(), 4)

                # Device 0: R1:CA — Pillar 100nm
                dev0_text = card.device_combo.itemText(0)
                self.assertIn("R1:CA", dev0_text)
                self.assertIn("Pillar 100nm", dev0_text)
                self.assertIn("R1:CA · Pillar 100nm", card.structure_edit.text())
            finally:
                store.close()

    def test_persistence_of_last_selected_sample_and_device(self) -> None:
        """Verify that last chosen sample, device, operator, and area persist and are restored."""
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from PySide6.QtCore import QSettings
        from app.devices.keithley_2600.ui.characterization_card import KeithleyCharacterizationCard
        from app.inventory.models import Sample
        from app.inventory.store import InventoryStore

        with TemporaryDirectory() as tmpdir:
            store = InventoryStore(Path(tmpdir) / "test_inventory.db")
            try:
                sample = Sample(
                    sample_id="PERSIST-SMPL",
                    name="Persistence Test Wafer",
                    rows=("1", "2"),
                    cols=("X", "Y"),
                    device_labels={"2,Y": "Persisted Junction 45"},
                    device_notes={"2,Y": "area: 3.14 um2, barrier: 1.35 nm"},
                )
                store.save_sample(sample)

                card = self.page.characterization_card
                card.set_inventory_store(store)
                self.app.processEvents()

                # Select sample
                idx = card._find_sample_index("PERSIST-SMPL")
                card.sample_combo.setCurrentIndex(idx)
                self.app.processEvents()

                # Select device R2:CY
                dev_idx = card._find_device_index("2", "Y")
                self.assertGreaterEqual(dev_idx, 0)
                card.device_combo.setCurrentIndex(dev_idx)
                card.operator_edit.setText("MZ")
                self.app.processEvents()

                # Verify auto-parsed hints from cell notes
                self.assertEqual(card.area_edit.text(), "3.14")
                self.assertEqual(card.thickness_edit.text(), "1.35")
                self.assertIn("R2:CY", card.structure_edit.text())

                # Check QSettings were written
                settings = QSettings("LabControl", "LabControl")
                self.assertEqual(settings.value("keithley_characterization/last_sample_id"), "PERSIST-SMPL")
                self.assertEqual(settings.value("keithley_characterization/last_row"), "2")
                self.assertEqual(settings.value("keithley_characterization/last_col"), "Y")
                self.assertEqual(settings.value("keithley_characterization/last_operator"), "MZ")

                # Also check InventoryStore active target was set
                active = store.get_active_target()
                self.assertEqual(active.sample_id, "PERSIST-SMPL")
                self.assertEqual(active.row, "2")
                self.assertEqual(active.col, "Y")

                # Create a brand new card instance with this store and ensure it restores automatically!
                new_card = KeithleyCharacterizationCard(
                    controller=self.controller,
                    settings=self.settings,
                    inventory_store=store,
                )
                try:
                    self.app.processEvents()
                    self.assertEqual(new_card.selected_sample_id(), "PERSIST-SMPL")
                    row, col, _ = new_card.selected_device_coord()
                    self.assertEqual(row, "2")
                    self.assertEqual(col, "Y")
                    self.assertEqual(new_card.sample_id_edit.text(), "PERSIST-SMPL")
                    self.assertIn("R2:CY", new_card.structure_edit.text())
                    self.assertEqual(new_card.operator_edit.text(), "MZ")
                finally:
                    new_card.deleteLater()
            finally:
                store.close()

    def test_active_target_bidirectional_synchronization(self) -> None:
        """Verify set_active_sample_target synchronizes card and active_target_changed signal emits."""
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from app.inventory.models import ActiveSampleTarget, Sample
        from app.inventory.store import InventoryStore

        with TemporaryDirectory() as tmpdir:
            store = InventoryStore(Path(tmpdir) / "test_inventory.db")
            try:
                sample = Sample(
                    sample_id="SYNC-01",
                    name="Sync Sample",
                    rows=("1", "2"),
                    cols=("A", "B"),
                    device_labels={"1,B": "Device 1B"},
                )
                store.save_sample(sample)

                card = self.page.characterization_card
                card.set_inventory_store(store)
                self.app.processEvents()

                emitted_targets: list[ActiveSampleTarget] = []
                card.active_target_changed.connect(emitted_targets.append)

                # 1. Update from external active target (e.g. from InventoryPage)
                target = ActiveSampleTarget(
                    sample_id="SYNC-01",
                    row="1",
                    col="B",
                    device_label="Device 1B",
                )
                card.set_active_sample_target(target)
                self.app.processEvents()

                self.assertEqual(card.selected_sample_id(), "SYNC-01")
                r, c, _label = card.selected_device_coord()
                self.assertEqual(r, "1")
                self.assertEqual(c, "B")
                self.assertIn("Device 1B", card.structure_edit.text())

                # 2. Select different cell on card -> emits active_target_changed
                dev_idx = card._find_device_index("2", "A")
                card.device_combo.setCurrentIndex(dev_idx)
                self.app.processEvents()

                self.assertGreaterEqual(len(emitted_targets), 1)
                last = emitted_targets[-1]
                self.assertEqual(last.sample_id, "SYNC-01")
                self.assertEqual(last.row, "2")
                self.assertEqual(last.col, "A")
            finally:
                store.close()

    def test_sweep_finished_records_run_in_inventory(self) -> None:
        """Verify completed sweep records a run record in InventoryStore and marks cell measured."""
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from app.devices.keithley_2600.characterization.models import (
            CharacterizationDataset,
            CharacterizationPoint,
            CharacterizationSweepConfig,
            SampleMetadata,
        )
        from app.inventory.models import Sample
        from app.inventory.store import InventoryStore

        with TemporaryDirectory() as tmpdir:
            store = InventoryStore(Path(tmpdir) / "test_inventory.db")
            try:
                sample = Sample(
                    sample_id="RUN-SAMPLE",
                    name="Run Test Sample",
                    rows=("1",),
                    cols=("1",),
                    device_labels={"1,1": "Cell 11"},
                )
                store.save_sample(sample)

                card = self.page.characterization_card
                card.set_inventory_store(store)
                self.app.processEvents()

                # Select sample and cell
                card.sample_combo.setCurrentIndex(card._find_sample_index("RUN-SAMPLE"))
                card.device_combo.setCurrentIndex(card._find_device_index("1", "1"))
                self.app.processEvents()

                # Check initial state
                sample_before = store.get_sample("RUN-SAMPLE")
                self.assertEqual(sample_before.cell_state("1", "1"), "untested")

                # Simulate finished dataset
                pts = [
                    CharacterizationPoint(
                        index=0,
                        demanded_si=0.0,
                        measured_voltage_v=0.0,
                        measured_current_a=0.0,
                        true_resistance_ohm=100.0,
                        apparent_resistance_ohm=100.0,
                        power_w=0.0,
                        compliance_active=False,
                        timestamp_epoch=1700000000.0,
                    ),
                    CharacterizationPoint(
                        index=1,
                        demanded_si=0.001,
                        measured_voltage_v=0.1,
                        measured_current_a=0.001,
                        true_resistance_ohm=100.0,
                        apparent_resistance_ohm=100.0,
                        power_w=0.0001,
                        compliance_active=False,
                        timestamp_epoch=1700000001.0,
                    ),
                ]
                cfg = CharacterizationSweepConfig(
                    channel="A",
                    mode="current",
                    start_level_si=0.0,
                    stop_level_si=0.001,
                    points_count=2,
                    compliance_si=1.0,
                    dwell_time_s=0.01,
                    sense_mode="4wire",
                    metadata=SampleMetadata(sample_id="RUN-SAMPLE", structure_name="Cell 11"),
                )
                dataset = CharacterizationDataset(
                    config=cfg,
                    points=tuple(pts),
                    started_at_iso="2026-09-06T12:00:00Z",
                    completed_at_iso="2026-09-06T12:00:05Z",
                )

                card._on_sweep_finished(dataset)
                self.app.processEvents()

                # Verify runs were logged in store
                runs = store.list_runs_for_sample("RUN-SAMPLE")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0].row, "1")
                self.assertEqual(runs[0].col, "1")
                self.assertEqual(runs[0].status, "completed")
                self.assertEqual(runs[0].point_count, 2)

                # Verify cell state was marked as 'measured'
                sample_after = store.get_sample("RUN-SAMPLE")
                self.assertEqual(sample_after.cell_state("1", "1"), "measured")

                # And device combo label now contains [measured]
                dev_text = card.device_combo.currentText()
                self.assertIn("[measured]", dev_text)
            finally:
                store.close()

    def test_keithley_page_configuration_panel_range_mode(self) -> None:
        """Verify KeithleyPage configuration panel fields use SafetyRangePill with widened Edit buttons."""
        panel = self.page.configuration_panel
        self.assertTrue(panel.level_field._range_mode)
        self.assertTrue(panel.compliance_field._range_mode)
        self.assertTrue(panel.nplc_field._range_mode)
        self.assertTrue(panel.source_range_field._range_mode)
        self.assertTrue(panel.measure_voltage_range_field._range_mode)
        self.assertTrue(panel.measure_current_range_field._range_mode)

        # Range pills are shown, legacy text badges are hidden
        self.assertFalse(panel.level_field.range_pill.isHidden())
        self.assertTrue(panel.level_field.minimum.isHidden())
        self.assertTrue(panel.level_field.maximum.isHidden())

        # Formatted intervals
        self.assertEqual(panel.level_field.range_pill._interval_text(), "[0 mA … 10 mA]")
        self.assertEqual(panel.compliance_field.range_pill._interval_text(), "[10 mV … 67 mV]")
        self.assertEqual(panel.nplc_field.range_pill._interval_text(), "[0.001 … 25]")
        self.assertEqual(panel.source_range_field.range_pill._interval_text(), "[> 0 … 3 A]")

        # Widened Edit buttons
        self.assertEqual(panel.level_field.edit_button.width(), 78)
        self.assertEqual(panel.level_field.edit_button.height(), 30)
        self.assertEqual(panel.level_field.edit_button.text(), "Edit")

        # Hardware fixed ranges have hidden edit buttons and arrow cursors
        self.assertTrue(panel.source_range_field.edit_button.isHidden())
        self.assertEqual(panel.source_range_field.range_pill.cursor().shape(), Qt.CursorShape.ArrowCursor)

    def test_keithley_page_max_abs_power_range_mode(self) -> None:
        """Verify Maximum source x compliance power uses SafetyRangePill and computes power gauge."""
        power_field = self.page.max_abs_power_field
        self.assertTrue(power_field._range_mode)
        self.assertFalse(power_field.range_pill.isHidden())
        self.assertTrue(power_field.minimum.isHidden())
        self.assertTrue(power_field.maximum.isHidden())
        self.assertEqual(power_field.edit_button.width(), 78)
        self.assertEqual(power_field.edit_button.height(), 30)

        # Interval text shows inequality min and power max
        self.assertEqual(power_field.range_pill._interval_text(), "[> 0 … 670 uW]")

        # Micro gauge value calculation with DIMENSION_POWER
        parsed = power_field._quantity_values()
        self.assertIsNotNone(parsed)
        cur, mn, mx, dim = parsed
        self.assertEqual(dim, "power")
        self.assertEqual(mn, 0.0)
        self.assertAlmostEqual(mx, 0.00067)

    def test_diameter_to_area_bidirectional_calculation(self) -> None:
        """Verify entering diameter recalculates area and expected resistance, and vice versa."""
        card = self.page.characterization_card

        # 1. Enter 1000 nm -> Area should become ~0.7854 um2
        card.diameter_edit.setText("1000 nm")
        self.app.processEvents()
        self.assertIn("0.7854", card.area_edit.text())
        # Expected resistance: RA = 8 / 0.7854 = 10.19 Ω (+ 25 Ω line -> 35.2 Ω)
        self.assertIn("10.2 Ω", card.expected_resistance_label.text())
        self.assertIn("35.2 Ω", card.expected_resistance_label.text())
        self.assertIn("1000 nm", card.diameter_preset_combo.currentText())

        # 2. Enter 600 nm -> Area should become ~0.2827 um2
        card.diameter_edit.setText("600 nm")
        self.app.processEvents()
        self.assertIn("0.2827", card.area_edit.text())
        self.assertIn("28.3 Ω", card.expected_resistance_label.text())
        self.assertIn("600 nm", card.diameter_preset_combo.currentText())

        # 3. Enter 0.2 um (micrometers unit) -> Diameter should be 200 nm, Area ~0.0314 um2
        card.diameter_edit.setText("0.2 um")
        self.app.processEvents()
        self.assertIn("0.0314", card.area_edit.text())
        self.assertIn("200 nm", card.diameter_preset_combo.currentText())

        # 4. Reverse: enter area 0.1257 um2 -> Diameter should become ~400 nm (P7)
        card.area_edit.setText("0.1257")
        self.app.processEvents()
        self.assertIn("400 nm", card.diameter_edit.text())
        self.assertIn("400 nm", card.diameter_preset_combo.currentText())

        # 5. Selecting preset from combo updates diameter and area
        # Find 800 nm (P9) in combo
        idx_p9 = -1
        for i in range(card.diameter_preset_combo.count()):
            if "800 nm" in card.diameter_preset_combo.itemText(i):
                idx_p9 = i
                break
        self.assertGreater(idx_p9, 0)
        card.diameter_preset_combo.setCurrentIndex(idx_p9)
        self.app.processEvents()
        self.assertIn("800 nm", card.diameter_edit.text())
        self.assertIn("0.5027", card.area_edit.text())

    def test_diameter_cell_hint_and_metadata_export(self) -> None:
        """Verify cell hints detect P1-P10 pillar designs and pass diameter to sweep config."""
        card = self.page.characterization_card

        # Cell notes mentioning "P8" (600 nm pillar)
        card._try_parse_and_fill_cell_hints(notes="Tested structure P8 with MgO barrier", label="R3C2")
        self.app.processEvents()
        self.assertEqual(card.diameter_edit.text(), "600 nm")
        self.assertIn("0.2827", card.area_edit.text())

        # Verify _build_config contains diameter_nm
        cfg = card._build_config()
        self.assertEqual(cfg.metadata.diameter_nm, 600.0)
        self.assertAlmostEqual(cfg.metadata.junction_area_um2 or 0.0, 0.2827, places=3)


