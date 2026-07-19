from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    Theme,
    isDarkTheme,
    setTheme,
    setThemeColor,
)

from .station_qss import dialog_qss, event_log_qss
from .theme import effective_theme
from .tokens import ThemeTokens, tokens_for


@dataclass(frozen=True, slots=True)
class AppliedTheme:
    name: str
    tokens: ThemeTokens


def apply_application_theme(application: QApplication, mode: str) -> AppliedTheme:
    name = effective_theme(mode)
    tokens = tokens_for(name)
    _apply_application_palette(application, tokens, name)
    application.setStyleSheet(dialog_qss(tokens))
    fluent_matches = isDarkTheme() == (name == "dark")
    accent_matches = application.property("stationAppliedAccent") == tokens.accent
    if (
        application.property("stationAppliedTheme") == name
        and fluent_matches
        and accent_matches
    ):
        _apply_station_control_styles(application, tokens)
        return AppliedTheme(name=name, tokens=tokens)
    # A synchronous swap prevents stale dark text/icons and avoids QFluent's
    # stock disabled-button QSS overwriting the station contrast patch later.
    setTheme(Theme.DARK if name == "dark" else Theme.LIGHT, lazy=False)
    setThemeColor(tokens.accent, save=False, lazy=False)
    # QFluent's synchronous QSS refresh can touch the application palette;
    # native item views must receive the station palette after that refresh.
    _apply_application_palette(application, tokens, name)
    application.setProperty("stationAppliedTheme", name)
    application.setProperty("stationAppliedAccent", tokens.accent)
    _settle_fluent_background_animations(application)
    _apply_station_control_styles(application, tokens)
    return AppliedTheme(name=name, tokens=tokens)


def _apply_application_palette(
    application: QApplication, tokens: ThemeTokens, theme_name: str
) -> None:
    """Keep native Qt views and dialogs in lockstep with Fluent's theme.

    QFluentWidgets themes its own controls through QSS, while item views,
    splitters and complex recipe editors still resolve colours from QPalette.
    Updating both systems in the same event prevents mixed light/dark windows.
    """

    palette = application.palette()
    colors = {
        QPalette.ColorRole.Window: tokens.background,
        QPalette.ColorRole.WindowText: tokens.text_primary,
        QPalette.ColorRole.Base: tokens.surface,
        QPalette.ColorRole.AlternateBase: tokens.surface_raised,
        QPalette.ColorRole.ToolTipBase: tokens.surface_raised,
        QPalette.ColorRole.ToolTipText: tokens.text_primary,
        QPalette.ColorRole.Text: tokens.text_primary,
        QPalette.ColorRole.Button: tokens.surface_raised,
        QPalette.ColorRole.ButtonText: tokens.text_primary,
        QPalette.ColorRole.BrightText: tokens.danger,
        QPalette.ColorRole.Highlight: tokens.accent,
        QPalette.ColorRole.HighlightedText: (
            tokens.background if theme_name == "dark" else "#ffffff"
        ),
        QPalette.ColorRole.Link: tokens.accent,
        QPalette.ColorRole.PlaceholderText: tokens.text_muted,
    }
    for role, value in colors.items():
        palette.setColor(QPalette.ColorGroup.All, role, QColor(value))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor(tokens.text_muted),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(tokens.text_muted),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Button,
        QColor(tokens.surface_raised),
    )
    application.setPalette(palette)


def _settle_fluent_background_animations(application: QApplication) -> None:
    """Commit Fluent background state during a global theme transition.

    QFluent's hover/background widgets animate from the previous palette. A
    station-wide light/dark change must not expose a transient light surface in
    an otherwise dark safety UI, so theme application commits their new normal
    colour in the same event turn.
    """

    for widget in application.allWidgets():
        animation = getattr(widget, "backgroundColorAni", None)
        normal_background = getattr(widget, "_normalBackgroundColor", None)
        set_background = getattr(widget, "setBackgroundColor", None)
        if (
            animation is None
            or not hasattr(widget, "bgColorObject")
            or not callable(normal_background)
            or not callable(set_background)
        ):
            continue
        animation.stop()
        set_background(normal_background())
        widget.update()


def _apply_station_control_styles(application: QApplication, tokens: ThemeTokens) -> None:
    """Retheme station-owned surfaces without repolishing the entire widget tree."""

    for widget in application.allWidgets():
        _apply_station_surface(widget, tokens)
        _apply_semantic_text(widget, tokens)
        _apply_station_card_frame(widget, tokens)
        _apply_station_button(widget, tokens)
        if widget.objectName() == "eventLogText":
            widget.setStyleSheet(event_log_qss(tokens))
            viewport = getattr(widget, "viewport", lambda: None)()
            if viewport is not None:
                _set_widget_background(
                    viewport, tokens.surface_raised, tokens.text_primary
                )
                viewport.setStyleSheet(
                    f"background: {tokens.surface_raised}; color: {tokens.text_primary};"
                )
            widget.update()


def _apply_station_button(widget: QWidget, tokens: ThemeTokens) -> None:
    """Give every native and Fluent disabled button readable contrast."""

    if not isinstance(widget, QPushButton):
        return
    marker = "/* station-disabled-button */"
    base = widget.styleSheet().split(marker, 1)[0].rstrip()
    widget.setStyleSheet(
        f"{base}\n{marker}\n"
        "QPushButton:disabled, PushButton:disabled, PrimaryPushButton:disabled {"
        f"color: {tokens.text_muted};"
        f"background-color: {tokens.surface_raised};"
        f"border: 1px solid {tokens.border};"
        "}"
    )


def _apply_station_card_frame(widget: QWidget, tokens: ThemeTokens) -> None:
    """Give every Fluent card a stable visual boundary in both themes."""

    if not isinstance(widget, CardWidget):
        return
    marker = "/* station-card-frame */"
    base = widget.styleSheet().split(marker, 1)[0].rstrip()
    set_background = getattr(widget, "setBackgroundColor", None)
    if callable(set_background):
        set_background(QColor(tokens.surface))
    widget.setStyleSheet(
        f"{base}\n{marker}\n"
        "CardWidget {"
        f"background-color: {tokens.surface};"
        f"border: 1px solid {tokens.border};"
        "border-radius: 8px;"
        "}"
    )


def _apply_station_surface(widget: QWidget, tokens: ThemeTokens) -> None:
    surface = widget.property("stationSurface")
    color = {
        "page": tokens.background,
        "surface": tokens.surface,
        "raised": tokens.surface_raised,
        "card": tokens.surface,
    }.get(str(surface))
    if widget.objectName() in {"fluentShellContent", "fluentShellSplitter"}:
        color = tokens.background
    if color is not None:
        _set_widget_background(widget, color, tokens.text_primary)
    if widget.objectName() == "fluentShellSplitter":
        widget.setStyleSheet(
            "QSplitter#fluentShellSplitter::handle {"
            "background: transparent; "
            f"border-top: 1px solid {tokens.border};"
            "}"
            "QSplitter#fluentShellSplitter::handle:hover {"
            f"border-top-color: {tokens.focus};"
            "}"
        )


def _set_widget_background(
    widget: QWidget, color: str, text_color: str | None = None
) -> None:
    palette = widget.palette()
    resolved = QColor(color)
    palette.setColor(QPalette.ColorRole.Window, resolved)
    palette.setColor(QPalette.ColorRole.Base, resolved)
    if text_color is not None:
        foreground = QColor(text_color)
        palette.setColor(QPalette.ColorRole.WindowText, foreground)
        palette.setColor(QPalette.ColorRole.Text, foreground)
        palette.setColor(QPalette.ColorRole.ButtonText, foreground)
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)
    widget.update()


def _apply_semantic_text(widget: QWidget, tokens: ThemeTokens) -> None:
    if not isinstance(widget, QLabel):
        return
    # Plain Qt labels keep their explicit palette when QFluent changes theme.
    # Start every label from a readable station foreground, then layer semantic
    # status colours on top.  This also covers labels created by QFormLayout.
    color = (
        tokens.text_muted
        if isinstance(widget, CaptionLabel) or widget.objectName() == "muted"
        else tokens.text_primary
    )
    if widget.property("deviceState") == "verified" or widget.property("outputState") == "off":
        color = tokens.success
    elif widget.property("deviceState") == "fault" or widget.property("safetyState") == "danger" or widget.property("outputState") == "active":
        color = tokens.danger
    elif widget.property("safetyState") == "caution" or widget.property("deviceState") in {"compliance", "active"}:
        color = tokens.caution
    palette = widget.palette()
    foreground = QColor(color)
    palette.setColor(QPalette.ColorRole.WindowText, foreground)
    palette.setColor(QPalette.ColorRole.Text, foreground)
    palette.setColor(QPalette.ColorRole.ButtonText, foreground)
    widget.setPalette(palette)
    widget.update()
