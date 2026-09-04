from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

from app.settings import SettingsRepository
from app.ui.measurement_tree.view import MeasurementTreeView
from app.ui.recipes.page import (
    BoundPlainTextEdit,
    BoundScrollArea,
    RecipePage,
)
from app.ui.execution.page import RunMonitorPage
from app.ui.results.page import ResultsPage
from app.ui.settings_page import SettingsPage
from app.ui.shell.page_host import FluentPageHost
from tests.helpers import SETTINGS_TEMPLATE


class RecipePageScrollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_pages_declare_owns_viewport(self) -> None:
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        recipe_page = RecipePage(settings)
        try:
            self.assertTrue(getattr(recipe_page, "owns_viewport", False))
        finally:
            recipe_page.close()

        execution_page = RunMonitorPage()
        try:
            self.assertTrue(getattr(execution_page, "owns_viewport", False))
        finally:
            execution_page.close()

        settings_repo = SettingsRepository(SETTINGS_TEMPLATE)
        settings_page = SettingsPage(settings_repo)
        try:
            self.assertTrue(getattr(settings_page, "owns_viewport", False))
        finally:
            settings_page.close()

        results_page = ResultsPage(".")
        try:
            self.assertTrue(getattr(results_page, "owns_viewport", False))
        finally:
            results_page.close()

    def test_fluent_page_host_sets_expanding_for_viewport_owning_pages(self) -> None:
        standard_content = QWidget()
        standard_host = FluentPageHost(standard_content)
        self.assertEqual(
            standard_content.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Preferred,
        )

        workspace_content = QWidget()
        workspace_content.owns_viewport = True
        workspace_host = FluentPageHost(workspace_content)
        self.assertEqual(
            workspace_content.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding,
        )

    def test_measurement_tree_view_confines_wheel_events(self) -> None:
        tree = MeasurementTreeView()
        try:
            # Create a wheel event (angleDelta y=120)
            event = QWheelEvent(
                QPoint(50, 50),
                QPoint(50, 50),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            event.ignore()
            tree.wheelEvent(event)
            # The wheel event must be accepted so it does not bubble to the shell
            self.assertTrue(event.isAccepted())
        finally:
            tree.close()

    def test_bound_plain_text_edit_confines_wheel_events(self) -> None:
        editor = BoundPlainTextEdit()
        try:
            event = QWheelEvent(
                QPoint(50, 50),
                QPoint(50, 50),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            event.ignore()
            editor.wheelEvent(event)
            self.assertTrue(event.isAccepted())
        finally:
            editor.close()

    def test_bound_scroll_area_confines_wheel_events(self) -> None:
        scroll = BoundScrollArea()
        try:
            event = QWheelEvent(
                QPoint(50, 50),
                QPoint(50, 50),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            event.ignore()
            scroll.wheelEvent(event)
            self.assertTrue(event.isAccepted())
        finally:
            scroll.close()

    def test_recipe_page_minimum_height_fits_normal_viewport(self) -> None:
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = RecipePage(settings)
        try:
            # Check relaxed minimum heights
            self.assertLessEqual(page.workspace_card.minimumHeight(), 250)
            self.assertLessEqual(page.workspace_splitter.minimumHeight(), 250)
            self.assertLessEqual(page.builder_container.minimumHeight(), 250)
            self.assertLessEqual(page.library_panel.minimumHeight(), 250)
            self.assertLessEqual(page.inspector_panel.minimumHeight(), 250)

            # Check that editor and inspector are BoundPlainTextEdit instances
            self.assertIsInstance(page.editor, BoundPlainTextEdit)
            self.assertIsInstance(page.inspector, BoundPlainTextEdit)
            self.assertIsInstance(page.library_panel, BoundScrollArea)
        finally:
            page.close()

    def test_results_page_minimum_height_fits_normal_viewport(self) -> None:
        page = ResultsPage(".")
        try:
            self.assertLessEqual(page.results_splitter.minimumHeight(), 250)
            self.assertLessEqual(page.heatmap_tab.minimumHeight(), 250)
            self.assertLessEqual(page.spectrum_tab.minimumHeight(), 250)
            self.assertLessEqual(page.sweep_tree.minimumHeight(), 250)
            self.assertLessEqual(page.minimumSizeHint().height(), 400)
        finally:
            page.close()
