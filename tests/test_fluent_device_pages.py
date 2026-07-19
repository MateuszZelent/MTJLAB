from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from qfluentwidgets import CardWidget, ComboBox, PrimaryPushButton, PushButton

from app.ui.shell import MainWindow


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
