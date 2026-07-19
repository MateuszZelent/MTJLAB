from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import FluentWindow

from app.ui.main_window import MainWindow
from app.ui.shell import FluentPageHost, StationSafetySnapshot, StationSafetyStrip


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


class MainWindowFluentShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_main_window_uses_fluent_navigation_and_all_routes_exist(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertIsInstance(window, FluentWindow)
            self.assertEqual(tuple(window.navigation_routes), (
                "dashboard", "rigol", "keithley", "anritsu",
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
