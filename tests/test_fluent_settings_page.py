from __future__ import annotations

import os
import inspect
import unittest
from copy import deepcopy
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QBoxLayout, QFormLayout, QTabWidget
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
from app.settings.models import StationSettings
from app.ui.settings_page import SettingsPage
from app.ui.settings_page import _SafetyLimitValidationDelegate
from app.devices.keithley_2600.ui.page import KeithleyConfigurationPanel
from app.ui.shell import MainWindow
from tests.helpers import SETTINGS_TEMPLATE
from tests.shell_test_isolation import (  # noqa: F401
    isolated_shell_persistence, shell_qt_application,
)


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
            self.assertFalse(page.tabs.navigation.isVisible())
            self.assertTrue(page.tabs.compact_navigation.isVisible())
            self.assertTrue(page.page_stack.isVisible())
            self.assertGreater(page.page_stack.geometry().height(), 450)
            self.assertTrue(
                all(isinstance(card, CardWidget) for card in page.findChildren(CardWidget))
            )
            self.assertEqual(
                page._title("defaults"), "Startup form values (not safety limits)"
            )
            safety_labels = {
                page.limits_table.item(row, 1).text()
                for row in range(page.limits_table.rowCount())
                if page.limits_table.item(row, 1) is not None
            }
            self.assertIn(
                "Emergency measured-voltage cutoff (forces A+B OFF)",
                safety_labels,
            )
            self.assertTrue(
                all(
                    isinstance(editor, (CheckBox, ComboBox, LineEdit, SpinBox))
                    for editor in page._form_editors.values()
                )
            )
        finally:
            page.close()

    def test_keithley_panel_explains_working_allowed_and_emergency_limits(self) -> None:
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        panel = KeithleyConfigurationPanel(settings)
        try:
            panel.resize(980, 620)
            panel.show()
            self.application.processEvents()

            summary = panel.safety_boundary_summary
            self.assertTrue(summary.isVisible())
            self.assertGreater(summary.geometry().width(), 0)
            self.assertGreater(summary.geometry().height(), 0)
            self.assertIn("Working voltage compliance", summary.text())
            self.assertIn("allowed compliance setting", summary.text())
            self.assertIn("emergency measured-voltage cutoff", summary.text())
            self.assertIn("forces A+B OFF", summary.text())

            panel.compliance.setText("70 mV")
            self.application.processEvents()
            self.assertTrue(summary.property("safetyWarning"))
            self.assertIn("WARNING — no trip headroom", summary.text())
            self.assertIn("both cutoff sides outside ±70 mV", summary.text())

            updated_raw = settings.model_dump(mode="python")
            trip = updated_raw["devices"]["keithley"]["safety"]["channels"]["B"][
                "lab_limits"
            ]["measured_voltage_trip"]
            trip["min"] = "-75 mV"
            trip["max"] = "75 mV"
            panel.set_settings(StationSettings.model_validate(updated_raw))
            self.application.processEvents()
            self.assertFalse(summary.property("safetyWarning"))
            self.assertIn("-75 mV…75 mV", summary.text())

            panel.resize(620, 520)
            self.application.processEvents()
            self.assertTrue(summary.isVisible())
            self.assertGreater(summary.geometry().height(), 0)
        finally:
            panel.close()

    def test_every_safety_limit_row_has_a_persisted_disable_control(self) -> None:
        page = SettingsPage(SettingsRepository(".config/settings.yml"))
        try:
            page.resize(1360, 880)
            page.show()
            self.application.processEvents()
            checks = [
                check
                for check in page.limits_scroll.widget().findChildren(CheckBox)
                if check.text() == "Disable limit"
            ]
            self.assertEqual(len(checks), page.limits_table.rowCount())
            target_path = (
                "devices",
                "keithley",
                "safety",
                "channels",
                "A",
                "lab_limits",
                "measured_current_trip",
                "enabled",
            )
            target = next(
                check
                for check in checks
                if tuple(check.property("limitEnabledPath")) == target_path
            )
            target.setChecked(True)
            self.application.processEvents()
            self.assertFalse(page._get_path(page._raw, target_path))
            self.assertTrue(page._dirty)
        finally:
            page.close()

    def test_scalar_max_power_uses_the_same_explicit_unit_contract_as_yaml(self) -> None:
        page = SettingsPage(SettingsRepository(".config/settings.yml"))
        path = (
            "devices",
            "keithley",
            "safety",
            "channels",
            "A",
            "lab_limits",
            "max_abs_power",
        )
        try:
            item = page._limit_items_by_path[path]

            item.setText("6700 uW")
            self.application.processEvents()
            self.assertNotIn(item, page._limit_error_items)

            item.setText("6.7 mW")
            self.application.processEvents()
            self.assertNotIn(item, page._limit_error_items)
            settings = StationSettings.model_validate(page._apply_tree_values())
            self.assertEqual(
                settings.keithley.safety.channels["A"].lab_limits.max_abs_power,
                "6.7 mW",
            )
            self.assertEqual(page.limits_table.item(item.row(), 4).text(), "explicit unit")

            item.setText("6700")
            self.application.processEvents()
            self.assertIn(item, page._limit_error_items)
            message = str(item.data(256 + 101))
            self.assertIn("power unit", message)
        finally:
            page.close()

    def test_staging_limit_snapshot_updates_only_changed_safety_editors(self) -> None:
        repository = SettingsRepository(SETTINGS_TEMPLATE)
        page = SettingsPage(repository)
        source_max_path = (
            "devices",
            "keithley",
            "safety",
            "channels",
            "B",
            "lab_limits",
            "source_current",
            "max",
        )
        power_path = (
            "devices",
            "keithley",
            "safety",
            "channels",
            "B",
            "lab_limits",
            "max_abs_power",
        )
        try:
            raw = deepcopy(repository.load().raw)
            limits = raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
            limits["source_current"]["max"] = "150 mA"
            limits["source_current"]["max_abs"] = "150 mA"
            limits["measured_current_trip"]["max"] = "150 mA"
            limits["max_abs_power"] = "10.05 mW"
            settings = StationSettings.model_validate(raw)
            changed_paths = {
                source_max_path,
                source_max_path[:-1] + ("max_abs",),
                source_max_path[:-2] + ("measured_current_trip", "max"),
                power_path,
            }

            with (
                patch.object(page, "_populate") as populate,
                patch.object(page, "_refresh_diagnostics") as refresh_diagnostics,
            ):
                page.stage_limit_snapshot(settings, raw, changed_paths)

            populate.assert_not_called()
            refresh_diagnostics.assert_not_called()
            self.assertEqual(page._safety_limit_editors[source_max_path].text(), "150 mA")
            self.assertEqual(page._safety_limit_editors[power_path].text(), "10.05 mW")
            self.assertTrue(page._dirty)
        finally:
            page.close()

    def test_keithley_safety_card_accepts_dependent_limit_proposal(self) -> None:
        repository = SettingsRepository(SETTINGS_TEMPLATE)
        page = SettingsPage(repository)
        source_max_path = (
            "devices",
            "keithley",
            "safety",
            "channels",
            "B",
            "lab_limits",
            "source_current",
            "max",
        )
        current_trip_path = source_max_path[:-2] + ("measured_current_trip", "max")
        power_path = source_max_path[:-2] + ("max_abs_power",)
        try:
            editor = page._safety_limit_editors[source_max_path]
            editor.setText("150 mA")
            with (
                patch(
                    "app.ui.settings_page.KeithleyLimitProposalDialog",
                    create=True,
                ) as proposal_dialog,
                patch.object(page, "_populate") as populate,
                patch.object(page, "_refresh_diagnostics") as refresh_diagnostics,
            ):
                proposal_dialog.return_value.exec.return_value = 1
                page._commit_safety_limit_editor(source_max_path, editor)

            proposal_dialog.assert_called_once()
            populate.assert_not_called()
            refresh_diagnostics.assert_not_called()
            self.assertEqual(editor.text(), "150 mA")
            self.assertEqual(page._safety_limit_editors[current_trip_path].text(), "150 mA")
            self.assertEqual(page._safety_limit_editors[power_path].text(), "10.05 mW")
            self.assertTrue(page._dirty)
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

            self.assertLessEqual(
                page.minimumSizeHint().width(), host.scroll_area.viewport().width()
            )
            self.assertTrue(page.save_button.isVisibleTo(window))
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
            self.assertFalse(page.tabs.navigation.isVisible())
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

    def test_settings_form_keeps_readable_label_column_without_wrapping_rows(self) -> None:
        page = SettingsPage(SettingsRepository(".config/settings.yml"))
        try:
            page.resize(1360, 880)
            page.show()
            self.application.processEvents()
            form = next(child for child in page.forms["anritsu"].widget().findChildren(QFormLayout))
            self.assertEqual(form.rowWrapPolicy(), QFormLayout.RowWrapPolicy.DontWrapRows)
            labels = [
                label
                for label in page.forms["anritsu"].widget().findChildren(type(page._subtitle))
                if label.objectName() != "settingsFieldError"
            ]
            self.assertTrue(any(label.minimumWidth() >= 240 for label in labels))
        finally:
            page.close()

    def test_wide_settings_rethemes_all_routes_without_navigation_artifacts(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1900, 1030)
            window.show()
            window._navigate_to("settings")
            page = window.settings_page

            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark_surface = page.tabs.grab().toImage().pixelColor(4, 4).lightness()
            dark_form_margin = (
                page.forms["general"].widget().grab().toImage().pixelColor(4, 4).lightness()
            )
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light_surface = page.tabs.grab().toImage().pixelColor(4, 4).lightness()
            light_form_margin = (
                page.forms["general"].widget().grab().toImage().pixelColor(4, 4).lightness()
            )

            self.assertTrue(page.tabs.navigation.isVisibleTo(window))
            self.assertFalse(page.tabs.compact_navigation.isVisible())
            self.assertGreater(light_surface, dark_surface + 40)
            self.assertGreater(light_form_margin, dark_form_margin + 40)
            self.assertLess(
                page.tabs.navigation.geometry().bottom(),
                page.tabs.stack.geometry().top(),
            )
            self.assertFalse(page._draft_model_host.isVisibleTo(window))
            self.assertTrue(
                all(tree.parentWidget() is page._draft_model_host for tree in page.trees.values())
            )
            self.assertTrue(all(not tree.isVisibleTo(window) for tree in page.trees.values()))
            self.assertIs(page.limits_table.parentWidget(), page._draft_model_host)
            self.assertFalse(page.limits_table.isVisibleTo(window))
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()


if __name__ == "__main__":
    unittest.main()
