from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QStackedWidget, QTabWidget, QToolBar
from qfluentwidgets import CardWidget, CommandBar, PrimaryPushButton, PushButton, SegmentedWidget

from app.ui.shell import MainWindow


class FluentRecipeAndExecutionPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_sweep_builder_exposes_fluent_workspace_surfaces_at_desktop_size(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1280, 720)
            window.show()
            window._navigate_to("sweeps")
            self.application.processEvents()

            page = window.recipe_page
            self.assertIsInstance(page.hero_card, CardWidget)
            self.assertIsInstance(page.workflow_tabs, SegmentedWidget)
            self.assertIsInstance(page.builder_stack, QStackedWidget)
            self.assertIsInstance(page.recipe_command_bar, CommandBar)
            self.assertIsInstance(page.run_button, PrimaryPushButton)
            self.assertIsInstance(page.open_editor_button, PrimaryPushButton)
            self.assertFalse(page.findChildren(QTabWidget))
            self.assertFalse(page.findChildren(QToolBar))
            self.assertTrue(page.hero_card.isVisibleTo(window))
            self.assertGreater(page.hero_card.geometry().width(), 300)
            self.assertTrue(page.workflow_tabs.isVisibleTo(window))

            sample = page.hero_card.mapTo(window, QPoint(40, 40))
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light = window.grab().toImage().pixelColor(sample).name()
            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark = window.grab().toImage().pixelColor(sample).name()
            self.assertNotEqual(light, dark)
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_execution_monitor_rethemes_fluent_cards_without_losing_visible_actions(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("execution")
            self.application.processEvents()

            page = window.run_monitor
            self.assertIsInstance(page.hero_card, CardWidget)
            self.assertIsInstance(page.monitor_card, CardWidget)
            self.assertIsInstance(page.pause_button, PushButton)
            self.assertIsInstance(page.stop_button, PrimaryPushButton)
            self.assertTrue(page.stop_button.isVisibleTo(window))

            sample = page.hero_card.mapTo(window, QPoint(40, 40))
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light = window.grab().toImage().pixelColor(sample).name()
            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark = window.grab().toImage().pixelColor(sample).name()
            self.assertNotEqual(light, dark)
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()
