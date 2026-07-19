from __future__ import annotations

import unittest


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
