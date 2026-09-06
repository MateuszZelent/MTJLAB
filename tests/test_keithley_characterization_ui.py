"""UI tests for Keithley sample characterization card and tab integration in KeithleyPage."""

from __future__ import annotations

from copy import deepcopy
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.ui.page import KeithleyPage
from app.devices.keithley_2600.ui.characterization_card import KeithleyCharacterizationCard
from app.devices.simulators import simulated_station_settings
from app.settings.models import StationSettings
from app.ui.shell.page_host import FluentPageHost
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

    def test_fluent_tab_view_structure(self) -> None:
        """Verify FluentTabView contains both control and characterization tabs."""
        self.assertIsNotNone(self.page.tab_view)
        self.assertEqual(self.page.tab_view.count(), 2)
        self.assertEqual(self.page.tab_view.tabText(0), "Sterowanie i monitorowanie")
        self.assertEqual(self.page.tab_view.tabText(1), "Charakterystyka próbki i raport")
        self.assertIsInstance(self.page.characterization_card, KeithleyCharacterizationCard)

    def test_characterization_card_elements_and_geometry(self) -> None:
        """Verify characterization tab controls are rendered with non-zero geometry."""
        card = self.page.characterization_card
        host = FluentPageHost(self.page)
        host.resize(1366, 768)
        host.show()
        self.app.processEvents()

        # Switch to Characterization tab
        self.page.tab_view.setCurrentIndex(1)
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

    def test_channel_change_autofills_limits(self) -> None:
        """Verify selecting Channel B autofills limits from station settings."""
        card = self.page.characterization_card
        card.channel_combo.setCurrentText("Kanał B")
        self.app.processEvents()

        # Channel B in simulated settings has voltage compliance 67 mV and max current 10 mA
        self.assertEqual(card.compliance_edit.text(), "67 mV")
        self.assertEqual(card.stop_level_edit.text(), "10 mA")

    def test_preflight_rejection_shows_banner(self) -> None:
        """Verify invalid or out-of-limits parameters display a warning message in banner."""
        card = self.page.characterization_card
        card.channel_combo.setCurrentText("Kanał B")
        # Set level far exceeding channel limit
        card.stop_level_edit.setText("500 mA")
        card._on_start_clicked()
        self.app.processEvents()

        # Banner should display safety violation
        self.assertIn("Odrzucenie bezpieczeństwa", card.banner.last_message)

    def test_mode_change_updates_limits_and_labels(self) -> None:
        """Verify switching mode updates units, fields, and plot axis labels."""
        card = self.page.characterization_card
        # Initially in current mode
        self.assertIn("Zadany prąd", card.plot_widget.getAxis("bottom").labelText)
        self.assertIn("Odpowiedź napięciowa", card.plot_widget.getAxis("left").labelText)

        # Switch to voltage mode
        card.mode_combo.setCurrentText("Charakterystyka napięciowa (V → I)")
        self.app.processEvents()

        # Plot labels should update
        self.assertIn("Zadane napięcie", card.plot_widget.getAxis("bottom").labelText)
        self.assertIn("Odpowiedź prądowa", card.plot_widget.getAxis("left").labelText)

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
        card.channel_combo.setCurrentText("Kanał A")
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
