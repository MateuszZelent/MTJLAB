from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from app.ui.shell import FluentPageHost


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
