from __future__ import annotations

import os
import inspect
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QBoxLayout, QTabWidget
from qfluentwidgets import (
    CardWidget,
    CheckBox,
    ComboBox,
    LineEdit,
    Pivot,
    PrimaryPushButton,
    SpinBox,
)

from app.settings import SettingsRepository
from app.ui.settings_page import SettingsPage
from app.ui.settings_page import _SafetyLimitValidationDelegate
from app.ui.shell import MainWindow


class FluentSettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_settings_page_renders_fluent_route_navigation_and_actions(self) -> None:
        page = SettingsPage(SettingsRepository(".config/settings.yml"))
        try:
            page.resize(1360, 880)
            page.show()
            self.application.processEvents()

            self.assertNotIsInstance(page.tabs, QTabWidget)
            self.assertIsInstance(page.section_navigation, Pivot)
            self.assertIsInstance(page.action_card, CardWidget)
            self.assertIsInstance(page.profile_card, CardWidget)
            self.assertIsInstance(page.save_button, PrimaryPushButton)
            self.assertFalse(page.tabs.navigation_scroll.isVisible())
            self.assertTrue(page.tabs.compact_navigation.isVisible())
            self.assertTrue(page.page_stack.isVisible())
            self.assertGreater(page.page_stack.geometry().height(), 450)
            self.assertTrue(
                all(isinstance(card, CardWidget) for card in page.findChildren(CardWidget))
            )
            self.assertTrue(
                all(
                    isinstance(editor, (CheckBox, ComboBox, LineEdit, SpinBox))
                    for editor in page._form_editors.values()
                )
            )
        finally:
            page.close()

    def test_embedded_settings_actions_fit_and_remain_reachable_at_1280_by_720(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1280, 720)
            window.show()
            window._navigate_to("settings")
            self.application.processEvents()
            page = window.settings_page
            host = window.navigation_routes["settings"]

            self.assertLessEqual(page.minimumSizeHint().width(), host.scroll_area.viewport().width())
            self.assertTrue(page.save_button.isVisibleTo(window))
            self.assertTrue(page.approve_button.isVisibleTo(window))
            self.assertLessEqual(
                page.action_card.mapTo(window, page.action_card.rect().bottomRight()).x(),
                window.rect().right(),
            )
        finally:
            window.close()

    def test_embedded_settings_reflows_navigation_and_actions_at_minimum_size(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(820, 560)
            window.show()
            window._navigate_to("settings")
            self.application.processEvents()
            page = window.settings_page
            host = window.navigation_routes["settings"]

            self.assertTrue(page.tabs.compact_navigation.isVisibleTo(window))
            self.assertFalse(page.tabs.navigation_scroll.isVisible())
            self.assertEqual(
                page.action_layout.direction(),
                QBoxLayout.Direction.TopToBottom,
            )
            self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)
            self.assertEqual(page.width(), host.scroll_area.viewport().width())
        finally:
            window.close()

    def test_validation_delegate_uses_semantic_widget_state_not_forced_light_qss(self) -> None:
        source = inspect.getsource(_SafetyLimitValidationDelegate)
        self.assertNotIn("setStyleSheet", source)
        self.assertNotIn("#ffffff", source)


if __name__ == "__main__":
    unittest.main()
