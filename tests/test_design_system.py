from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel
from qfluentwidgets import PrimaryPushButton, Theme

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

    def test_global_tokens_use_catppuccin_latte_and_mocha_foundations(self) -> None:
        light = tokens_for("light")
        dark = tokens_for("dark")
        self.assertEqual(
            (light.background, light.text_primary, light.accent),
            ("#e6e9ef", "#4c4f69", "#1e66f5"),
        )
        self.assertEqual(
            (dark.background, dark.text_primary, dark.accent),
            ("#1e1e2e", "#cdd6f4", "#89b4fa"),
        )

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

    def setUp(self) -> None:
        self.application.setProperty("stationAppliedTheme", None)

    def test_theme_bridge_never_repolishes_the_application_stylesheet(self) -> None:
        with (
            patch("app.ui.design_system.fluent_theme.setTheme") as set_theme,
            patch("app.ui.design_system.fluent_theme.setThemeColor") as set_color,
            patch.object(self.application, "setStyleSheet") as set_stylesheet,
        ):
            applied = apply_application_theme(self.application, "dark")
        set_theme.assert_called_once_with(Theme.DARK, lazy=False)
        set_color.assert_called_once_with(
            tokens_for("dark").accent, save=False, lazy=False
        )
        set_stylesheet.assert_not_called()
        self.assertEqual(applied.name, "dark")

    def test_reapplying_the_effective_theme_is_a_no_op(self) -> None:
        with patch("app.ui.design_system.fluent_theme.setTheme") as set_theme:
            apply_application_theme(self.application, "light")
            apply_application_theme(self.application, "light")
        set_theme.assert_called_once_with(Theme.LIGHT, lazy=False)

    def test_real_window_system_uses_lazy_theme_update(self) -> None:
        with (
            patch.object(self.application, "platformName", return_value="windows"),
            patch("app.ui.design_system.fluent_theme.setTheme") as set_theme,
            patch("app.ui.design_system.fluent_theme.setThemeColor") as set_color,
        ):
            apply_application_theme(self.application, "dark")
        set_theme.assert_called_once_with(Theme.DARK, lazy=True)
        set_color.assert_called_once_with(
            tokens_for("dark").accent, save=False, lazy=True
        )

    def test_offscreen_platform_disables_motion(self) -> None:
        with patch.dict("os.environ", {"QT_QPA_PLATFORM": "offscreen"}):
            self.assertFalse(motion_enabled())

    def test_plot_theme_comes_from_semantic_tokens(self) -> None:
        tokens = tokens_for("light")
        palette = plot_theme(tokens)
        self.assertEqual(palette.background, tokens.plot_background)
        self.assertEqual(palette.measurement, tokens.plot_measurement)

    def test_light_theme_resets_plain_label_left_over_from_dark_palette(self) -> None:
        label = QLabel("Field")
        palette = label.palette()
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#ffffff"))
        label.setPalette(palette)

        apply_application_theme(self.application, "light")

        self.assertEqual(
            label.palette().color(QPalette.ColorRole.WindowText).name(),
            tokens_for("light").text_primary,
        )
        label.deleteLater()

    def test_light_theme_gives_disabled_primary_button_dark_readable_text(self) -> None:
        button = PrimaryPushButton("Read now")
        button.setEnabled(False)

        apply_application_theme(self.application, "dark")
        apply_application_theme(self.application, "light")

        patch_qss = button.styleSheet().split("/* station-disabled-button */", 1)[1]
        tokens = tokens_for("light")
        self.assertIn(f"color: {tokens.text_muted}", patch_qss)
        self.assertIn(f"background-color: {tokens.surface_raised}", patch_qss)
        self.assertNotIn("rgba(255, 255, 255, 0.9)", patch_qss)
        button.deleteLater()
