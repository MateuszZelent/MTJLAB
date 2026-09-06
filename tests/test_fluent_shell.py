from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget
from qfluentwidgets import FluentWindow, ScrollArea
from qfluentwidgets.common.style_sheet import styleSheetManager

from app.settings import SettingsRepository
from app.ui.shell import FluentPageHost, StationSafetySnapshot, StationSafetyStrip
from app.ui.shell import MainWindow
from tests.helpers import SETTINGS_TEMPLATE


class FluentDependencyTests(unittest.TestCase):
    def test_required_pyside6_fluent_surface_is_importable(self) -> None:
        from qfluentwidgets import (
            FluentWindow,
            NavigationItemPosition,
            Theme,
            setTheme,
        )

        self.assertTrue(issubclass(FluentWindow, object))
        self.assertTrue(hasattr(NavigationItemPosition, "BOTTOM"))
        self.assertTrue(hasattr(Theme, "LIGHT"))
        self.assertTrue(callable(setTheme))


class FluentPageHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_host_preserves_legacy_widget_and_exposes_scroll_area(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(QLabel("Legacy page"))
        host = FluentPageHost(content)
        self.assertIs(host.content, content)
        self.assertIs(host.scroll_area.widget(), content)
        self.assertTrue(host.scroll_area.widgetResizable())
        self.assertEqual(host.objectName(), "fluentPageHost")


class StationSafetyStripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_snapshot_updates_text_and_semantic_properties(self) -> None:
        strip = StationSafetyStrip()
        strip.update_snapshot(
            StationSafetySnapshot(
                ready=False,
                active_outputs=2,
                simulation=True,
                actor="operator",
                roles=("operator",),
            )
        )
        self.assertIn("2 outputs active", strip.outputs.text())
        self.assertEqual(strip.outputs.property("outputState"), "active")
        self.assertEqual(strip.readiness.property("safetyState"), "danger")
        self.assertIn("SIMULATION", strip.mode.text())

    def test_estop_button_emits_without_animation_or_delay(self) -> None:
        strip = StationSafetyStrip()
        emissions: list[bool] = []
        strip.estop_requested.connect(lambda: emissions.append(True))
        strip.estop.click()
        self.assertEqual(emissions, [True])

    def test_save_settings_button_is_adjacent_and_emits_explicit_request(self) -> None:
        strip = StationSafetyStrip()
        emissions: list[bool] = []
        strip.save_settings_requested.connect(lambda: emissions.append(True))
        strip.save_settings.click()
        self.assertEqual(strip.save_settings.text(), "SAVE SETTINGS")
        self.assertEqual(emissions, [True])

    def test_long_actor_identity_does_not_force_desktop_width(self) -> None:
        strip = StationSafetyStrip()
        strip.update_snapshot(
            StationSafetySnapshot(
                ready=True,
                active_outputs=0,
                simulation=False,
                actor="LAB\\operator-with-a-deliberately-long-identity",
                roles=("engineer", "operator", "service"),
            )
        )
        self.assertLessEqual(strip.minimumSizeHint().width(), 900)

    def test_save_and_estop_never_overlap_in_responsive_layouts(self) -> None:
        strip = StationSafetyStrip()
        strip.estop.setText("E-STOP")
        strip.estop.setMaximumWidth(96)
        strip.show()
        for width, expected_mode in ((240, "narrow"), (700, "compact"), (1000, "wide")):
            strip.resize(width, 140)
            self.application.processEvents()
            self.assertEqual(strip._layout_mode, expected_mode)
            self.assertFalse(
                strip.save_settings.geometry().intersects(strip.estop.geometry())
            )
            for button in (strip.save_settings, strip.estop):
                self.assertGreater(button.width(), 0)
                self.assertGreaterEqual(button.geometry().left(), 0)
                self.assertLessEqual(button.geometry().right(), strip.width())
        strip.close()


class MainWindowFluentShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_main_window_uses_fluent_navigation_and_all_routes_exist(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertIsInstance(window, FluentWindow)
            self.assertEqual(tuple(window.navigation_routes), (
                "overview", "discovery", "rigol", "keithley", "keithley_characterization",
                "anritsu", "moke_box", "lakeshore_gaussmeter", "sweeps", "execution",
                "results", "inventory", "elab", "settings",
            ))
            self.assertIsNotNone(window.safety_strip)
            self.assertEqual(
                window.safety_strip.estop.text(), "E-STOP  |  ALL OUTPUTS OFF"
            )
            self.assertGreaterEqual(window.safety_strip.estop.minimumWidth(), 184)
            self.assertEqual(
                window.safety_strip.estop.property("visualPriority"),
                "high",
            )
            self.assertFalse(hasattr(window, "ribbon"))
            self.assertFalse(hasattr(window, "tabs"))
        finally:
            window.close()

    def test_navigation_changes_current_page_without_recreating_controller(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            controller = window._controllers["keithley"]
            window._navigate_to("keithley")
            self.assertIs(window._controllers["keithley"], controller)
            self.assertIs(
                window.stackedWidget.currentWidget(),
                window.navigation_routes["keithley"],
            )
        finally:
            window.close()

    def test_fluent_shell_uses_an_embedded_widget_for_shared_content(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertIs(window.fluent_content.parentWidget(), window)
            self.assertNotIsInstance(window.fluent_content, QMainWindow)
            self.assertFalse(window.fluent_content.isWindow())
            window.show()
            self.application.processEvents()
            visible_top_levels = [
                widget
                for widget in self.application.topLevelWidgets()
                if widget.isVisible()
                and widget.windowType() != Qt.WindowType.ToolTip
                and type(widget).__name__ != "ToolTip"
            ]
            self.assertEqual(visible_top_levels, [window])
            self.assertFalse(
                any(
                    isinstance(widget, QMainWindow)
                    for widget in visible_top_levels
                )
            )
        finally:
            window.close()

    def test_station_page_stack_is_not_globally_repolished_by_qfluent(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            managed = {widget for widget, _source in styleSheetManager.items()}
            self.assertNotIn(window.stackedWidget, managed)
            self.assertIn("background: transparent", window.stackedWidget.styleSheet())
        finally:
            window.close()

    def test_fluent_shell_gives_the_current_page_visible_width(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            self.application.processEvents()
            self.assertLessEqual(window.minimumWidth(), 1360)
            self.assertEqual(window.width(), 1360)
            self.assertGreater(window.stackedWidget.width(), 600)
            self.assertTrue(window.stackedWidget.currentWidget().isVisible())
            self.assertTrue(
                window.stackedWidget.property("isTransparent")
                or "station-borderless-stack" in window.stackedWidget.styleSheet()
            )
            host = window.navigation_routes["overview"]
            self.assertIsInstance(host.scroll_area, ScrollArea)
            self.assertEqual(
                host.scroll_area.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
        finally:
            window.close()

    def test_safety_strip_tracks_dashboard_readiness_changes(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertEqual(window.safety_strip.readiness.text(), "Station ready")
            window.dashboard.update_audit_health(False)
            self.assertEqual(window.safety_strip.readiness.text(), "Station blocked")
        finally:
            window.close()

    def test_application_actions_live_in_a_fluent_title_bar_menu(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertIs(
                window.application_menu_button.parentWidget(),
                window.titleBar,
            )
            self.assertIsNotNone(window.application_menu_button.menu())
            self.assertEqual(
                tuple(window.theme_actions),
                ("system", "light", "dark"),
            )
            self.assertFalse(hasattr(window, "theme_action"))
            self.assertTrue(
                all(
                    action not in window.application_menu_button.menu().actions()
                    for action in window.theme_actions.values()
                )
            )
        finally:
            window.close()

    def test_theme_menu_is_in_bottom_navigation_before_settings_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            path.write_text(
                SETTINGS_TEMPLATE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            window = MainWindow(path, simulation=True)
            try:
                theme_item = window.navigationInterface.widget("themeMenu")
                settings_item = window.navigationInterface.widget(
                    window.navigation_routes["settings"].objectName()
                )
                bottom = window.navigationInterface.panel.bottomLayout
                self.assertGreaterEqual(bottom.indexOf(theme_item), 0)
                self.assertEqual(
                    bottom.indexOf(settings_item),
                    bottom.indexOf(theme_item) + 1,
                )
                window.show()
                self.application.processEvents()
                self.assertTrue(theme_item.isVisible())
                self.assertFalse(hasattr(window, "theme_selector"))
                self.assertEqual(
                    window.theme_navigation_menu.actions(),
                    list(window.theme_actions.values()),
                )
                window.theme_actions["dark"].trigger()
                self.assertTrue(window.theme_actions["dark"].isChecked())
                self.assertEqual(
                    SettingsRepository(path).load().raw["ui"]["theme"],
                    "dark",
                )
            finally:
                window.close()

    def test_navigation_defaults_expanded_on_every_launch_after_user_collapse(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        key = "main_window/navigation_expanded"
        had_previous = settings.contains(key)
        previous = settings.value(key)
        settings.remove(key)
        first = None
        second = None
        try:
            first = MainWindow(".config/settings.yml", simulation=True)
            first.resize(1360, 880)
            first.show()
            self.application.processEvents()
            self.assertFalse(first.navigationInterface.panel.isCollapsed())
            self.assertEqual(first.navigationInterface.width(), 248)
            expanded_navigation_width = first.navigationInterface.width()
            expanded_content_width = first.fluent_content.width()
            expanded_page_width = first.stackedWidget.currentWidget().width()

            first.navigationInterface.panel.collapse()
            QTest.qWait(300)
            self.application.processEvents()
            self.assertTrue(first.navigationInterface.panel.isCollapsed())
            self.assertLess(
                first.navigationInterface.width(),
                expanded_navigation_width,
            )
            self.assertGreater(
                first.fluent_content.width(),
                expanded_content_width,
            )
            self.assertGreater(
                first.stackedWidget.currentWidget().width(),
                expanded_page_width,
            )
            first.close()
            first = None

            second = MainWindow(".config/settings.yml", simulation=True)
            second.resize(1360, 880)
            second.show()
            QTest.qWait(300)
            self.application.processEvents()
            self.assertFalse(second.navigationInterface.panel.isCollapsed())
            self.assertEqual(second.navigationInterface.width(), 248)
            self.assertTrue(
                all(
                    second.navigationInterface.widget(host.objectName()).isVisible()
                    for route, host in second.navigation_routes.items()
                    if route != "keithley_characterization"
                )
            )
            keithley_item = second.navigationInterface.widget("keithleyPageHost")
            char_item = second.navigationInterface.widget("keithley_characterizationPageHost")
            self.assertFalse(char_item.isVisible())
            keithley_item.setExpanded(True, ani=False)
            self.application.processEvents()
            self.assertTrue(char_item.isVisible())
        finally:
            if first is not None:
                first.close()
            if second is not None:
                second.close()
            if had_previous:
                settings.setValue(key, previous)
            else:
                settings.remove(key)

    def test_expanded_navigation_never_overlays_content_at_minimum_window_size(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(820, 560)
            window.show()
            self.application.processEvents()

            navigation = window.navigationInterface
            panel = navigation.panel
            self.assertTrue(panel.isCollapsed())
            self.assertIs(panel.parentWidget(), navigation)
            self.assertLessEqual(navigation.width(), 64)
            self.assertGreaterEqual(
                window.fluent_content.geometry().left(), navigation.width()
            )
            self.assertLess(
                navigation.geometry().right(),
                window.fluent_content.geometry().left(),
            )
            self.assertIn(window.safety_strip._layout_mode, {"narrow", "compact"})
            self.assertGreater(
                window.safety_strip.actor.geometry().top(),
                window.safety_strip.readiness.geometry().top(),
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_apparatus_group_auto_collapses_before_navigation_rows_overlap(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            def visible_route_rows() -> list[tuple[str, int, int]]:
                panel = window.navigationInterface.panel
                rows = []
                for route, host in window.navigation_routes.items():
                    item = window.navigationInterface.widget(host.objectName())
                    if item.isVisibleTo(window):
                        row_widget = getattr(item, "itemWidget", item)
                        top = row_widget.mapTo(panel, QPoint()).y()
                        rows.append((route, top, top + row_widget.height() - 1))
                return sorted(rows, key=lambda row: row[1])

            window.resize(900, 600)
            window.show()
            self.application.processEvents()
            self.application.processEvents()
            self.assertFalse(window.apparatus_navigation_item.isExpanded)
            compact_rows = visible_route_rows()
            self.assertTrue(
                all(first[2] < second[1] for first, second in zip(compact_rows, compact_rows[1:]))
            )

            window.resize(900, 880)
            self.application.processEvents()
            self.application.processEvents()
            self.assertTrue(window.apparatus_navigation_item.isExpanded)
            expanded_rows = visible_route_rows()
            self.assertTrue(
                all(first[2] < second[1] for first, second in zip(expanded_rows, expanded_rows[1:]))
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_event_log_navigation_toggle_above_theme_controls_visibility_and_state(self) -> None:
        """The sidebar must host an Event Log toggle button above Theme that toggles footer visibility."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            self.application.processEvents()

            log_nav_item = window.navigationInterface.widget("eventLogToggle")
            self.assertIsNotNone(log_nav_item)
            theme_item = window.navigationInterface.widget("themeMenu")
            settings_item = window.navigationInterface.widget(
                window.navigation_routes["settings"].objectName()
            )
            bottom = window.navigationInterface.panel.bottomLayout
            log_index = bottom.indexOf(log_nav_item)
            theme_index = bottom.indexOf(theme_item)
            settings_index = bottom.indexOf(settings_item)

            self.assertGreaterEqual(log_index, 0)
            self.assertEqual(theme_index, log_index + 1)
            self.assertEqual(settings_index, theme_index + 1)

            # Default launch state: event log is hidden, freeing vertical space
            self.assertFalse(window._event_log_requested_visible)
            self.assertFalse(window.event_log_panel.isVisible())
            self.assertFalse(window.event_log_action.isChecked())
            self.assertFalse(log_nav_item.isSelected)
            self.assertFalse(log_nav_item.itemWidget.isSelected)
            self.assertEqual(log_nav_item.toolTip(), "Show event log")
            self.assertEqual(window.shell_splitter.sizes()[1], 0)

            # Toggle ON via navigation button click
            log_nav_item.clicked.emit(True)
            self.application.processEvents()

            self.assertTrue(window._event_log_requested_visible)
            self.assertTrue(window.event_log_panel.isVisible())
            self.assertTrue(window.event_log_action.isChecked())
            self.assertTrue(log_nav_item.isSelected)
            self.assertTrue(log_nav_item.itemWidget.isSelected)
            self.assertEqual(log_nav_item.toolTip(), "Hide event log")
            self.assertEqual(window.shell_splitter.sizes()[1], 125)

            # Toggle OFF via navigation button click
            log_nav_item.clicked.emit(True)
            self.application.processEvents()

            self.assertFalse(window._event_log_requested_visible)
            self.assertFalse(window.event_log_panel.isVisible())
            self.assertFalse(window.event_log_action.isChecked())
            self.assertFalse(log_nav_item.isSelected)
            self.assertFalse(log_nav_item.itemWidget.isSelected)
            self.assertEqual(log_nav_item.toolTip(), "Show event log")
            self.assertEqual(window.shell_splitter.sizes()[1], 0)

            # Sync from application menu action
            window.event_log_action.setChecked(True)
            self.application.processEvents()

            self.assertTrue(window.event_log_panel.isVisible())
            self.assertTrue(log_nav_item.isSelected)
            self.assertTrue(log_nav_item.itemWidget.isSelected)
            self.assertEqual(log_nav_item.toolTip(), "Hide event log")
        finally:
            window.close()
            self.application.processEvents()
