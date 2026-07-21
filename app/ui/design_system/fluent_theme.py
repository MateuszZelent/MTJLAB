from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    Theme,
    isDarkTheme,
    qconfig,
    setTheme,
)

from app.ui.common.precision_stepper import install_precision_arrow_stepper

from .station_qss import dialog_qss, event_log_qss, notification_banner_qss
from .theme import effective_theme
from .tokens import ThemeTokens, tokens_for


@dataclass(frozen=True, slots=True)
class AppliedTheme:
    name: str
    tokens: ThemeTokens


def apply_application_theme(application: QApplication, mode: str) -> AppliedTheme:
    install_precision_arrow_stepper(application)
    name = effective_theme(mode)
    tokens = tokens_for(name)
    # Application properties describe the last requested theme, not proof
    # that Qt's palette is still synchronized (tests, dialogs and QFluent
    # repolishing can alter it independently). Always establish the native
    # palette before considering the fast path.
    _apply_application_palette(application, tokens, name)
    fluent_matches = isDarkTheme() == (name == "dark")
    accent_matches = application.property("stationAppliedAccent") == tokens.accent
    if (
        application.property("stationAppliedTheme") == name
        and fluent_matches
        and accent_matches
    ):
        _install_dialog_theme_filter(application, tokens)
        _apply_station_control_styles(application, tokens)
        return AppliedTheme(name=name, tokens=tokens)
    _stage_fluent_accent(tokens)
    # Settings contains many routes that can be hidden during a theme switch.
    # QFluent's lazy refresh only repolishes the currently visible subtree,
    # leaving hidden Pivot pages and their navigation in the previous theme.
    # Apply the global theme synchronously so every route is coherent when it
    # becomes visible.
    setTheme(Theme.DARK if name == "dark" else Theme.LIGHT, lazy=False)
    # QFluent's QSS refresh can touch the application palette;
    # native item views must receive the station palette after that refresh.
    _apply_application_palette(application, tokens, name)
    application.setProperty("stationAppliedTheme", name)
    application.setProperty("stationAppliedAccent", tokens.accent)
    _install_dialog_theme_filter(application, tokens)
    _settle_fluent_background_animations(application)
    _apply_station_control_styles(application, tokens)
    return AppliedTheme(name=name, tokens=tokens)


def _stage_fluent_accent(tokens: ThemeTokens) -> None:
    """Update QFluent's accent before the single theme QSS refresh."""

    qconfig.set(qconfig.themeColor, QColor(tokens.accent), save=False)


class _DialogThemeFilter(QObject):
    """Lazily apply station styling when a widget is actually shown."""

    def __init__(self, application: QApplication, tokens: ThemeTokens) -> None:
        super().__init__(application)
        self._tokens = tokens

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget):
            _apply_station_control_style(watched, self._tokens)
        elif (
            event.type() == QEvent.Type.DynamicPropertyChange
            and isinstance(watched, QWidget)
            and bytes(event.propertyName()) == b"validationState"
        ):
            apply_validation_style(watched, tokens=self._tokens)
        return False


def _install_dialog_theme_filter(
    application: QApplication, tokens: ThemeTokens
) -> None:
    theme_filter = getattr(application, "_station_dialog_theme_filter", None)
    if not isinstance(theme_filter, _DialogThemeFilter):
        theme_filter = _DialogThemeFilter(application, tokens)
        application.installEventFilter(theme_filter)
        application._station_dialog_theme_filter = theme_filter
    else:
        theme_filter.set_tokens(tokens)


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
        QPalette.ColorRole.Mid: tokens.border,
        QPalette.ColorRole.Midlight: tokens.border,
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
    """Retheme visible station controls; hidden widgets are handled on Show."""

    for widget in application.allWidgets():
        window = widget.window()
        if window.isVisible() and widget.isVisibleTo(window):
            _apply_station_control_style(widget, tokens)


def _apply_station_control_style(widget: QWidget, tokens: ThemeTokens) -> None:
    # Do not skip a widget merely because it saw this token before. QFluent
    # can repolish controls after that point (notably hidden Settings routes),
    # so the palette and semantic surfaces must be reasserted on every global
    # theme application and Show event.
    if isinstance(widget, QDialog):
        widget.setStyleSheet(dialog_qss(tokens))
    _apply_station_surface(widget, tokens)
    _apply_semantic_text(widget, tokens)
    if widget.property("validationState") is not None:
        apply_validation_style(widget, tokens=tokens)
    if widget.objectName() in {
        "inlineValidationWarning",
        "settingsFieldError",
        "settingsValidationBanner",
    }:
        _apply_inline_validation_warning_style(widget, tokens)
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
    widget.setProperty("stationControlTheme", tokens.background)


def apply_validation_style(
    editor: QWidget,
    warning: QWidget | None = None,
    *,
    tokens: ThemeTokens | None = None,
) -> None:
    """Apply the shared semantic validation treatment to an editor and label."""

    resolved_tokens = tokens or _current_tokens()
    marker = "/* station-validation */"
    base = editor.styleSheet().split(marker, 1)[0].rstrip()
    if editor.property("validationState") == "error":
        editor.setStyleSheet(
            f"{base}\n{marker}\n"
            "QLineEdit, LineEdit, QComboBox, ComboBox, QSpinBox, SpinBox {"
            f"border: 2px solid {resolved_tokens.danger};"
            "}"
        )
    else:
        editor.setStyleSheet(base)
    if warning is not None:
        _apply_inline_validation_warning_style(warning, resolved_tokens)


def _current_tokens() -> ThemeTokens:
    application = QApplication.instance()
    theme_name = (
        application.property("stationAppliedTheme")
        if application is not None
        else None
    )
    if theme_name not in {"light", "dark"}:
        theme_name = "dark" if isDarkTheme() else "light"
    return tokens_for(theme_name)


def _apply_inline_validation_warning_style(
    warning: QWidget, tokens: ThemeTokens
) -> None:
    marker = "/* station-inline-validation */"
    base = warning.styleSheet().split(marker, 1)[0].rstrip()
    warning.setStyleSheet(
        f"{base}\n{marker}\n"
        "QLabel, BodyLabel {"
        f"color: {tokens.danger}; font-weight: 600;"
        "}"
    )


def _apply_station_button(widget: QWidget, tokens: ThemeTokens) -> None:
    """Give buttons readable disabled and hardware-confirmed states."""

    if not isinstance(widget, QPushButton):
        return
    marker = "/* station-disabled-button */"
    base = widget.styleSheet().split(marker, 1)[0].rstrip()
    widget.setStyleSheet(
        f"{base}\n{marker}\n"
        "QPushButton:disabled, PushButton:disabled, PrimaryPushButton:disabled {"
        "color: palette(placeholder-text);"
        "background-color: palette(alternate-base);"
        "border: 1px solid palette(mid);"
        "}"
        "QPushButton[controlState=\"emergency\"] {"
        f"color: {tokens.danger}; background-color: transparent;"
        f"border: 1px solid {tokens.danger}; font-weight: 600;"
        "}"
        "QPushButton[controlState=\"emergency\"]:hover {"
        "background-color: palette(alternate-base);"
        "}"
        "QPushButton[controlState=\"confirmed\"] {"
        f"color: #ffffff; background-color: {tokens.accent};"
        f"border: 1px solid {tokens.accent};"
        "}"
        "QPushButton[controlState=\"energized\"] {"
        f"color: #ffffff; background-color: {tokens.danger};"
        f"border: 1px solid {tokens.danger}; font-weight: 600;"
        "}"
    )


def _apply_station_card_frame(widget: QWidget, tokens: ThemeTokens) -> None:
    """Give every Fluent card a stable visual boundary in both themes."""

    if not isinstance(widget, CardWidget):
        return
    marker = "/* station-card-frame */"
    set_background = getattr(widget, "setBackgroundColor", None)
    if callable(set_background):
        set_background(QColor(tokens.surface))
    notification_qss = (
        notification_banner_qss(tokens)
        if widget.objectName() == "notificationBanner"
        else ""
    )
    if marker in widget.styleSheet():
        base = widget.styleSheet().split(marker, 1)[0].rstrip()
    else:
        base = widget.styleSheet().rstrip()
    widget.setStyleSheet(
        f"{base}\n{marker}\n"
        "CardWidget {"
        "background-color: palette(base);"
        "border: 1px solid palette(mid);"
        "border-radius: 8px;"
        "}"
        f"\n{notification_qss}"
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
    if widget.objectName() == "fluentApplicationStack":
        widget.setProperty("isTransparent", True)
        widget.setStyleSheet(
            "QStackedWidget#fluentApplicationStack {"
            "border: none; border-radius: 0; background: transparent;"
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
    station_state = widget.property("stationState")
    if (
        widget.property("deviceState") == "verified"
        or widget.property("outputState") == "off"
        or station_state in {"connected", "verified", "output_off"}
    ):
        color = tokens.success
    elif (
        widget.property("deviceState") == "fault"
        or widget.property("safetyState") == "danger"
        or widget.property("outputState") == "active"
        or station_state in {"fault", "unknown", "output_on"}
    ):
        color = tokens.danger
    elif station_state == "disconnected":
        color = tokens.text_muted
    elif widget.property("safetyState") == "caution" or widget.property("deviceState") in {"compliance", "active"}:
        color = tokens.caution
    palette = widget.palette()
    foreground = QColor(color)
    palette.setColor(QPalette.ColorRole.WindowText, foreground)
    palette.setColor(QPalette.ColorRole.Text, foreground)
    palette.setColor(QPalette.ColorRole.ButtonText, foreground)
    widget.setPalette(palette)
    widget.update()
