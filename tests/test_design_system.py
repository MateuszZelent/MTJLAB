from __future__ import annotations

import unittest

from app.ui.design_system import SPACING, ThemeTokens, station_qss, tokens_for


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
