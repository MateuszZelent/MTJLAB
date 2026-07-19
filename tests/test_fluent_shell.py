from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget
from qfluentwidgets import FluentWindow, ScrollArea

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
                profile_state="LOCKED",
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

    def test_long_actor_identity_does_not_force_desktop_width(self) -> None:
        strip = StationSafetyStrip()
        strip.update_snapshot(
            StationSafetySnapshot(
                ready=True,
                active_outputs=0,
                profile_state="APPROVED",
                simulation=False,
                actor="LAB\\operator-with-a-deliberately-long-identity",
                roles=("engineer", "operator", "service"),
            )
        )
        self.assertLessEqual(strip.minimumSizeHint().width(), 900)


class MainWindowFluentShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_main_window_uses_fluent_navigation_and_all_routes_exist(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertIsInstance(window, FluentWindow)
            self.assertEqual(tuple(window.navigation_routes), (
                "overview", "discovery", "rigol", "keithley", "anritsu",
                "moke_box", "lakeshore_gaussmeter", "sweeps", "execution",
                "results", "settings",
            ))
            self.assertIsNotNone(window.safety_strip)
            self.assertEqual(window.safety_strip.estop.text(), "E-STOP")
            self.assertLessEqual(window.safety_strip.estop.maximumWidth(), 96)
            self.assertEqual(
                window.safety_strip.estop.property("visualPriority"),
                "low",
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
            self.assertTrue(window.stackedWidget.property("isTransparent"))
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
                    for host in second.navigation_routes.values()
                )
            )
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
            self.assertFalse(panel.isCollapsed())
            self.assertIs(panel.parentWidget(), navigation)
            self.assertEqual(navigation.width(), 248)
            self.assertGreaterEqual(window.fluent_content.geometry().left(), 248)
            self.assertLess(
                navigation.geometry().right(),
                window.fluent_content.geometry().left(),
            )
            self.assertTrue(window.safety_strip._compact_layout)
            self.assertGreater(
                window.safety_strip.actor.geometry().top(),
                window.safety_strip.readiness.geometry().top(),
            )
        finally:
            window.close()
            self.application.processEvents()
