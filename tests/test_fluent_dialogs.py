from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMessageBox,
)
from qfluentwidgets import PrimaryPushButton, PushButton, TransparentPushButton

from app.safety.keithley_limit_reconciliation import (
    KeithleyLimitAdjustment,
    KeithleyLimitProposal,
)
from app.ui.design_system import apply_application_theme, tokens_for
from app.ui.dialogs import (
    StationAlertDialog,
    StationDialog,
    StationFileDialog,
    SweepDeviceReadinessDialog,
)
from app.ui.recipes.fluent_dialog import FluentRecipeDialog
from app.ui.widgets import (
    KeithleyLimitProposalDialog,
    LimitEditDialog,
    LimitField,
    SpectrumPlotWidget,
)
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

    def test_keithley_limit_proposal_dialog_renders_adjustments_in_narrow_parent(self) -> None:
        proposal = KeithleyLimitProposal(
            ("source_current", "max"),
            "150 mA",
            (
                KeithleyLimitAdjustment(
                    ("source_current", "max_abs"),
                    "10 mA",
                    "150 mA",
                    "Synchronise the source envelope.",
                ),
                KeithleyLimitAdjustment(
                    ("measured_current_trip", "max"),
                    "10.5 mA",
                    "150 mA",
                    "Cover the current source envelope.",
                ),
                KeithleyLimitAdjustment(
                    ("max_abs_power",),
                    "670 uW",
                    "10.05 mW",
                    "Cover the source x compliance power.",
                ),
            ),
        )
        parent = StationDialog()
        parent.resize(820, 560)
        dialog = KeithleyLimitProposalDialog(proposal, parent)
        try:
            parent.show()
            dialog.show()
            self.application.processEvents()

            accept = dialog.findChild(PrimaryPushButton, "acceptKeithleyLimitChanges")
            self.assertIsNotNone(accept)
            self.assertTrue(accept.isVisible())
            self.assertGreater(dialog.width(), 430)
            self.assertLessEqual(dialog.width(), parent.width())
            self.assertTrue(dialog.adjustments_text.isVisible())
            self.assertIn("10.05 mW", dialog.adjustments_text.toPlainText())

            dialog.reject()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        finally:
            dialog.close()
            parent.close()

    def test_unitless_numeric_limit_accepts_valid_nplc_and_clamps_outside_value(self) -> None:
        editor = line_edit("1")
        limit = LimitField(editor, 0.001, 25.0)
        try:
            self.assertTrue(limit.validate_and_clamp())
            self.assertEqual(editor.text(), "1")
            self.assertTrue(limit.validation_warning.isHidden())

            editor.setText("26")
            self.assertFalse(limit.validate_and_clamp())
            self.assertEqual(editor.text(), "25.0")
            self.assertFalse(limit.validation_warning.isHidden())
        finally:
            limit.close()

    def test_disabled_limit_badge_does_not_clamp_the_editor(self) -> None:
        editor = line_edit("4 mA")
        limit = LimitField(editor, "DISABLED", "DISABLED")
        try:
            self.assertTrue(limit.validate_and_clamp())
            self.assertEqual(editor.text(), "4 mA")
            self.assertTrue(limit.validation_warning.isHidden())
        finally:
            limit.close()

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

    def test_station_message_box_keeps_long_error_inside_narrow_parent(self) -> None:
        parent = StationDialog()
        parent.resize(520, 400)
        box = StationAlertDialog(
            parent,
            "Keithley measurement stopped",
            "Channel A measured current 3.83496 mA exceeded the configured "
            "maximum 1.1 mA. Both outputs were commanded OFF and confirmed OFF.",
            QMessageBox.StandardButton.Ok,
            None,
            QMessageBox.StandardButton.Ok,
        )
        try:
            parent.show()
            box.show()
            self.application.processEvents()
            self.assertLessEqual(box.width(), parent.width() - 48)
            self.assertGreater(box.content_label.height(), 14)
            self.assertTrue(box.title_label.isVisible())
            self.assertTrue(box.content_label.isVisible())
            self.assertIn("3.83496 mA", box.content_label.text())
            self.assertTrue(box.primary_button.isVisible())
        finally:
            box.close()
            parent.close()

    def test_sweep_readiness_dialog_gates_start_on_all_required_devices(self) -> None:
        dialog = SweepDeviceReadinessDialog(
            ("anritsu", "keithley"),
            {"anritsu": "Anritsu MS2830A", "keithley": "Keithley 2600"},
        )
        try:
            dialog.show()
            self.application.processEvents()
            self.assertGreater(dialog.geometry().width(), 0)
            self.assertGreater(dialog.geometry().height(), 0)
            self.assertFalse(dialog.start_button.isEnabled())
            self.assertEqual(dialog.connect_missing_button.text(), "Connect missing devices")

            dialog.update_device("anritsu", "verified", True)
            dialog.update_device("keithley", "output_off", True)

            self.assertTrue(dialog.start_button.isEnabled())
            dialog.resize(820, dialog.height())
            self.application.processEvents()
            self.assertTrue(dialog.start_button.isVisible())
            self.assertTrue(dialog.rows["anritsu"].isVisible())
        finally:
            dialog.close()

    def test_sweep_readiness_dialog_does_not_offer_connect_for_an_unsafe_device(self) -> None:
        dialog = SweepDeviceReadinessDialog(("anritsu",), {"anritsu": "Anritsu MS2830A"})
        try:
            dialog.update_device("anritsu", "unknown", True)

            self.assertFalse(dialog.start_button.isEnabled())
            self.assertFalse(dialog.connect_missing_button.isEnabled())
        finally:
            dialog.close()
