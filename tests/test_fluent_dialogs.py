from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
)
from qfluentwidgets import PushButton, TransparentPushButton

from app.ui.design_system import apply_application_theme, tokens_for
from app.ui.dialogs import StationDialog, StationFileDialog
from app.ui.recipes.fluent_dialog import FluentRecipeDialog
from app.ui.widgets import LimitEditDialog, LimitField, SpectrumPlotWidget
from app.ui.common import line_edit


class FluentDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_station_dialogs_and_recipe_dialogs_share_the_same_surface_contract(self) -> None:
        station = StationDialog()
        recipe = FluentRecipeDialog()
        try:
            self.assertEqual(station.property("stationSurface"), "page")
            self.assertEqual(recipe.property("stationSurface"), "page")
            self.assertTrue(station.testAttribute(Qt.WidgetAttribute.WA_StyledBackground))
        finally:
            station.close()
            recipe.close()

    def test_limit_and_plot_popup_actions_are_fluent_controls(self) -> None:
        limit = LimitField(line_edit("1 V"), "0 V", "2 V")
        dialog = LimitEditDialog("Voltage", "0 V", "2 V")
        plot = SpectrumPlotWidget(legend=False)
        try:
            self.assertIsInstance(limit.edit_button, PushButton)
            self.assertIsInstance(dialog, StationDialog)
            self.assertTrue(plot.toolbar_buttons)
            self.assertTrue(
                all(isinstance(button, TransparentPushButton) for button in plot.toolbar_buttons)
            )
        finally:
            limit.close()
            dialog.close()
            plot.close()

    def test_native_dialog_buttons_receive_readable_light_and_dark_styles(self) -> None:
        box = QMessageBox()
        try:
            for theme in ("light", "dark"):
                apply_application_theme(self.application, theme)
                box.show()
                self.application.processEvents()
                qss = box.styleSheet()
                tokens = tokens_for(theme)
                self.assertIn(f"color: {tokens.text_primary}", qss)
                self.assertIn(f"background: {tokens.surface_raised}", qss)
                self.assertIn("QDialogButtonBox QPushButton:disabled", qss)
        finally:
            box.close()

    def test_file_dialog_facade_forces_qt_rendered_themeable_picker(self) -> None:
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")) as call:
            StationFileDialog.getOpenFileName(None, "Open", "", "YAML (*.yml)")
        options = call.call_args.args[-1]
        self.assertTrue(options & QFileDialog.Option.DontUseNativeDialog)

    def test_dialog_qss_does_not_target_every_push_button(self) -> None:
        box = QMessageBox()
        apply_application_theme(self.application, "light")
        try:
            box.show()
            self.application.processEvents()
            qss = box.styleSheet()
            self.assertNotIn("\nQPushButton {", qss)
            self.assertIn("QDialogButtonBox QPushButton", qss)
            self.assertNotIn("QDialogButtonBox", self.application.styleSheet())
        finally:
            box.close()

    def test_message_box_renders_visible_geometry_in_both_themes(self) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Safety confirmation",
            "Saving this change revokes profile approval.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        observed: dict[str, str] = {}
        try:
            for theme in ("light", "dark"):
                apply_application_theme(self.application, theme)
                box.show()
                self.application.processEvents()
                self.assertGreater(box.width(), 300)
                self.assertGreater(box.height(), 100)
                self.assertTrue(box.button(QMessageBox.StandardButton.Cancel).isVisible())
                point = QPoint(12, 12)
                observed[theme] = box.grab().toImage().pixelColor(point).name()
            self.assertNotEqual(observed["light"], observed["dark"])
        finally:
            box.close()
