from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QStackedWidget, QTabWidget, QToolBar
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CommandBar,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
)

from app.ui.shell import MainWindow
from app.ui.recipes import SweepGeneratorDialog
from tests.helpers import simulation_settings


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
            self.assertIsInstance(page.document_card, CardWidget)
            self.assertIsInstance(page.selection_card, CardWidget)
            self.assertIsInstance(page.status_card, CardWidget)
            self.assertIsInstance(page.path, LineEdit)
            self.assertIsInstance(page.summary, BodyLabel)
            self.assertIsInstance(page.run_button, PrimaryPushButton)
            self.assertIsInstance(page.open_editor_button, PrimaryPushButton)
            self.assertFalse(page.findChildren(QTabWidget))
            self.assertFalse(page.findChildren(QToolBar))
            self.assertTrue(page.hero_card.isVisibleTo(window))
            self.assertGreater(page.hero_card.geometry().width(), 300)
            self.assertTrue(page.workflow_tabs.isVisibleTo(window))
            self.assertGreater(page.builder_container.geometry().height(), 180)
            self.assertGreater(page.workspace_splitter.geometry().width(), 500)
            for button in page._library_action_buttons:
                self.assertIsInstance(button, PushButton)
                # The offscreen Qt platform uses a square fallback font whose
                # metrics substantially overestimate normal Windows text.
                # Guard the actual compact layout contract and require the
                # complete action description to remain available on hover.
                self.assertGreaterEqual(button.width(), 180)
                self.assertTrue(button.toolTip().strip())
                self.assertGreaterEqual(button.height(), 34)
            self.assertFalse(
                page.selection_title.geometry().intersects(
                    page.selection_context.geometry()
                )
            )
            self.assertEqual(page.path.accessibleName(), "Recipe file path")
            page.set_settings(
                simulation_settings(approved=page._settings.outputs_locked)
            )
            expected_locked = page._settings.outputs_locked
            self.assertEqual(
                page.recipe_profile_badge.text(),
                "LOCKED PROFILE" if expected_locked else "APPROVED PROFILE",
            )
            self.assertEqual(
                page.recipe_profile_badge.property("safetyState"),
                "caution" if expected_locked else "verified",
            )

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

    def test_sweep_builder_keeps_primary_workspace_rendered_at_narrow_desktop_width(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1024, 720)
            window.show()
            window._navigate_to("sweeps")
            self.application.processEvents()

            page = window.recipe_page
            self.assertTrue(page.document_card.isVisibleTo(window))
            self.assertTrue(page.selection_card.isVisibleTo(window))
            self.assertTrue(page.builder_container.isVisibleTo(window))
            self.assertTrue(page.run_button.isVisibleTo(window))
            self.assertGreater(page.builder_container.geometry().width(), 390)
            self.assertGreater(page.builder_container.geometry().height(), 150)
            self.assertFalse(page.inspector_panel.isVisibleTo(window))

            item = page.tree.topLevelItem(0)
            self.assertIsNotNone(item)
            page.tree.setCurrentItem(item)
            self.application.processEvents()
            self.assertNotEqual(
                page.selection_context.text(),
                "Select a block in the measurement tree",
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_sweep_workspace_and_roi_dialog_retheme_together(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        dialog = None
        try:
            window.resize(1440, 900)
            window.show()
            window._navigate_to("sweeps")
            page = window.recipe_page
            dialog = SweepGeneratorDialog(
                {
                    "device": "Keithley",
                    "label": "Channel B · source current",
                    "target": "keithley.B.current",
                    "dimension": "current",
                },
                page,
            )
            dialog.resize(980, 700)
            dialog.show()

            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light_library = page.library_panel.viewport().grab().toImage().pixelColor(8, 8)
            light_tree = page.tree.viewport().grab().toImage().pixelColor(8, 8)
            light_dialog = dialog.grab().toImage().pixelColor(8, 8)

            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark_library = page.library_panel.viewport().grab().toImage().pixelColor(8, 8)
            dark_tree = page.tree.viewport().grab().toImage().pixelColor(8, 8)
            dark_dialog = dialog.grab().toImage().pixelColor(8, 8)

            self.assertNotEqual(light_library.name(), dark_library.name())
            self.assertNotEqual(light_tree.name(), dark_tree.name())
            self.assertNotEqual(light_dialog.name(), dark_dialog.name())
            self.assertGreater(light_library.lightness(), dark_library.lightness())
            self.assertGreater(light_dialog.lightness(), dark_dialog.lightness())
        finally:
            if dialog is not None:
                dialog.close()
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
