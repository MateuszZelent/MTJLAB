from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from app.ui.design_system import (
    SPACING,
    ThemeTokens,
    apply_application_theme,
    motion_enabled,
    plot_theme,
    station_qss,
    tokens_for,
)


class DesignSystemTests(unittest.TestCase):
    def test_light_and_dark_tokens_expose_all_station_semantics(self) -> None:
        for mode in ("light", "dark"):
            tokens = tokens_for(mode)
            self.assertIsInstance(tokens, ThemeTokens)
            for name in (
                "background", "surface", "surface_raised", "border",
                "text_primary", "text_muted", "accent", "focus",
                "success", "caution", "danger", "neutral",
                "output_active", "compliance", "interlock", "emergency",
                "plot_background", "plot_axes", "plot_grid",
                "plot_reference", "plot_measurement",
            ):
                self.assertRegex(getattr(tokens, name), r"^#[0-9a-fA-F]{6}$")

    def test_spacing_scale_is_monotonic_and_shared(self) -> None:
        self.assertEqual(tuple(SPACING), ("xs", "sm", "md", "lg", "xl"))
        self.assertEqual(sorted(SPACING.values()), list(SPACING.values()))

    def test_station_qss_contains_only_semantic_property_selectors(self) -> None:
        qss = station_qss(tokens_for("dark"))
        for selector in (
            '[safetyState="danger"]',
            '[outputState="active"]',
            '[validationState="error"]',
            '[deviceState="verified"]',
        ):
            self.assertIn(selector, qss)
        self.assertNotIn("QPushButton {", qss)
        self.assertNotIn("QLineEdit {", qss)

    def test_invalid_theme_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "light or dark"):
            tokens_for("sepia")


class ThemeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_theme_bridge_applies_fluent_and_semantic_qss(self) -> None:
        with patch("app.ui.design_system.fluent_theme.setTheme") as set_theme:
            applied = apply_application_theme(self.application, "dark")
        set_theme.assert_called_once()
        self.assertEqual(applied.name, "dark")
        self.assertIn('[safetyState="danger"]', self.application.styleSheet())

    def test_offscreen_platform_disables_motion(self) -> None:
        with patch.dict("os.environ", {"QT_QPA_PLATFORM": "offscreen"}):
            self.assertFalse(motion_enabled())

    def test_plot_theme_comes_from_semantic_tokens(self) -> None:
        tokens = tokens_for("light")
        palette = plot_theme(tokens)
        self.assertEqual(palette.background, tokens.plot_background)
        self.assertEqual(palette.measurement, tokens.plot_measurement)
