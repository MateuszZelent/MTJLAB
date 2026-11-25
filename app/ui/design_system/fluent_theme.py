from __future__ import annotations

import sys
from dataclasses import dataclass
from weakref import WeakSet

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentStyleSheet,
    Theme,
    isDarkTheme,
    qconfig,
    setTheme,
)
from qfluentwidgets.common.style_sheet import styleSheetManager
from qfluentwidgets.components.widgets.label import FluentLabelBase
from qframelesswindow import TitleBarButton
from shiboken6 import isValid

from app.ui.common.precision_stepper import install_precision_arrow_stepper

from .station_qss import dialog_qss, event_log_qss, station_qss
from .theme import effective_theme
from .tokens import ThemeTokens, tokens_for

_SEMANTIC_TEXT_PROPERTIES = frozenset({
    b"deviceState",
    b"outputState",
    b"stationState",
    b"safetyState",
    b"liveState",
    b"connectionState",
    b"elabStatusState",
    b"quickOutputState",
})


def set_dynamic_property(
    widget: QWidget,
    name: str,
    value: object,
    *,
    repolish: bool = True,
) -> bool:
    """Update a Qt dynamic property and repolish only when the value actually changed."""
    if widget.property(name) == value:
        return False
    widget.setProperty(name, value)
    if repolish:
        style = widget.style()
        if style is not None:
            style.unpolish(widget)
            style.polish(widget)
    return True


@dataclass(frozen=True, slots=True)
class AppliedTheme:
    name: str
    tokens: ThemeTokens


def configure_widget_style(application: QApplication) -> None:
    """Use a handle-independent Qt base beneath Fluent's QSS on Windows.

    WindowsVista/Windows11 styles resolve BUTTON metrics through UXTheme
    HWNDs, which can be invalid during frameless-window recreation/teardown.
    Fusion paints those fallback controls in Qt; Fluent still owns the theme.
    Install once, before constructing the station windows where possible.
    """
    if sys.platform != "win32" or application.property("stationWidgetStyleConfigured"):
        return
    # Never replace the base QStyle once widgets (especially QFluent Pivot)
    # already exist; swapping QStyle mid-session corrupts their Qt style state
    # on Windows and causes access violations on the next setStyleSheet().
    if application.allWidgets():
        application.setProperty("stationWidgetStyleConfigured", True)
        return
    application.setStyle("Fusion")
    application.setProperty("stationWidgetStyleConfigured", True)


def apply_application_theme(application: QApplication, mode: str) -> AppliedTheme:
    install_precision_arrow_stepper(application)
    name = effective_theme(mode)
    tokens = tokens_for(name)
    fluent_matches = isDarkTheme() == (name == "dark")
    accent_matches = application.property("stationAppliedAccent") == tokens.accent
    theme_matches = application.property("stationAppliedTheme") == name
    palette_matches = application.property("stationAppliedPaletteTheme") == name
    controls_signature = f"{name}:{tokens.accent}"
    controls_match = (
        application.property("stationAppliedControlTheme") == controls_signature
    )
    if theme_matches and fluent_matches and accent_matches:
        # Theme application is called from every window's first show event and
        # from per-page settings.  Reapplying the global QFluent stylesheet and
        # every visible widget here can block the GUI for tens of seconds on
        # Windows.  The palette/control signatures make this fast path safe:
        # a genuinely new widget is styled by the event filter when it shows,
        # while an externally changed palette still gets repaired once.
        if not palette_matches:
            _apply_application_palette(application, tokens, name)
            application.setProperty("stationAppliedPaletteTheme", name)
        _install_dialog_theme_filter(application, tokens)
        if not controls_match:
            _apply_station_control_styles(application, tokens)
            application.setProperty("stationAppliedControlTheme", controls_signature)
        return AppliedTheme(name=name, tokens=tokens)
    _stage_fluent_accent(tokens)
    # Prune widgets whose C++ counterparts were already destroyed before
    # qfluentwidgets walks styleSheetManager during a global theme swap.
    for managed_widget, _source in list(styleSheetManager.items()):
        if not isValid(managed_widget):
            styleSheetManager.deregister(managed_widget)
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
    application.setProperty("stationAppliedPaletteTheme", name)
    _install_dialog_theme_filter(application, tokens)
    _settle_fluent_background_animations(application)
    _apply_station_control_styles(application, tokens)
    application.setProperty("stationAppliedControlTheme", controls_signature)
    return AppliedTheme(name=name, tokens=tokens)


def _stage_fluent_accent(tokens: ThemeTokens) -> None:
    """Update QFluent's accent before the single theme QSS refresh."""

    qconfig.set(qconfig.themeColor, QColor(tokens.accent), save=False)


class _DialogThemeFilter(QObject):
    """Lazily apply station styling when a widget is actually shown."""

    def __init__(self, application: QApplication, tokens: ThemeTokens) -> None:
        super().__init__(application)
        self._tokens = tokens
        self._shown_widgets: WeakSet[QWidget] = WeakSet()
        self._fluent_background_widgets: WeakSet[QWidget] = WeakSet()

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens

    def track_visible_tree(self, root: QWidget) -> None:
        """Remember visible Fluent controls without retaining deleted widgets."""

        self._track_shown_widget(root)
        for widget in root.findChildren(QWidget):
            if widget.isVisibleTo(root):
                self._track_shown_widget(widget)

    def shown_widgets(self) -> tuple[QWidget, ...]:
        return tuple(self._shown_widgets)

    def fluent_background_widgets(self) -> tuple[QWidget, ...]:
        return tuple(self._fluent_background_widgets)

    def _track_shown_widget(self, widget: QWidget) -> None:
        self._shown_widgets.add(widget)
        self._track_fluent_background(widget)

    def _track_fluent_background(self, widget: QWidget) -> None:
        if _has_fluent_background_animation(widget):
            self._fluent_background_widgets.add(widget)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        event_type = event.type()
        if event_type == QEvent.Type.Show and isinstance(watched, QWidget):
            self._track_shown_widget(watched)
            # A theme can change while this widget is hidden (including the
            # shell during startup). Commit its Fluent background before paint;
            # a delayed hover animation must not retain the previous theme.
            _settle_fluent_background(watched)
            _apply_station_control_style(watched, self._tokens)
        elif event_type == QEvent.Type.DynamicPropertyChange and isinstance(watched, QWidget):
            prop_name = bytes(event.propertyName())
            if prop_name == b"validationState":
                apply_validation_style(watched, tokens=self._tokens)
            elif prop_name in _SEMANTIC_TEXT_PROPERTIES:
                _apply_semantic_text(watched, self._tokens)
            elif prop_name == b"controlState":
                _apply_station_button(watched, self._tokens)
        return False


def _install_dialog_theme_filter(
    application: QApplication, tokens: ThemeTokens
) -> None:
    theme_filter = getattr(application, "_station_dialog_theme_filter", None)
    if not isinstance(theme_filter, _DialogThemeFilter):
        theme_filter = _DialogThemeFilter(application, tokens)
        application.installEventFilter(theme_filter)
        application._station_dialog_theme_filter = theme_filter
        # Usually the filter is installed before MainWindow.show(). Seeding the
        # already-visible trees also covers embedders and focused UI tests that
        # apply their first station theme after showing a window.
        for window in application.topLevelWidgets():
            if window.isVisible():
                theme_filter.track_visible_tree(window)
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
            tokens.on_accent
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

    theme_filter = getattr(application, "_station_dialog_theme_filter", None)
    if not isinstance(theme_filter, _DialogThemeFilter):
        return
    for widget in theme_filter.fluent_background_widgets():
        try:
            visible = widget.isVisible()
        except RuntimeError:
            # A Python wrapper can briefly outlive its deleted Qt object.
            continue
        if not visible:
            continue
        _settle_fluent_background(widget)


def _settle_fluent_background(widget: QWidget) -> None:
    if not _has_fluent_background_animation(widget):
        return
    widget.backgroundColorAni.stop()
    background = (
        widget._normalBackgroundColor()
        if widget.isEnabled()
        else widget._disabledBackgroundColor()
    )
    widget.setBackgroundColor(background)
    widget.update()


def _has_fluent_background_animation(widget: QWidget) -> bool:
    return (
        getattr(widget, "backgroundColorAni", None) is not None
        and hasattr(widget, "bgColorObject")
        and callable(getattr(widget, "_normalBackgroundColor", None))
        and callable(getattr(widget, "setBackgroundColor", None))
    )


def _apply_station_control_styles(application: QApplication, tokens: ThemeTokens) -> None:
    """Retheme visible station controls; hidden widgets are handled on Show."""

    theme_filter = getattr(application, "_station_dialog_theme_filter", None)
    if not isinstance(theme_filter, _DialogThemeFilter):
        return
    for widget in theme_filter.shown_widgets():
        try:
            window = widget.window()
            visible = window.isVisible() and widget.isVisibleTo(window)
        except RuntimeError:
            # Weak tracking does not keep widgets alive, but another Python
            # owner can briefly retain a wrapper after Qt deletes the object.
            continue
        if visible:
            _apply_station_control_style(widget, tokens)


def _set_style_if_changed(widget: QWidget, stylesheet: str) -> None:
    """Avoid Qt repolishing a subtree when Show reasserts identical rules."""
    if widget.styleSheet() != stylesheet:
        widget.setStyleSheet(stylesheet)


def _apply_station_control_style(widget: QWidget, tokens: ThemeTokens) -> None:
    # Do not skip a widget merely because it saw this token before. QFluent
    # can repolish controls after that point (notably hidden Settings routes),
    # so the palette and semantic surfaces must be reasserted on every global
    # theme application and Show event.
    if isinstance(widget, TitleBarButton):
        # Hidden FluentTitleBar children can retain cached qproperty colours
        # from their construction theme. Give the buttons the native Fluent
        # stylesheet directly, as FluentWidgetTitleBar already does.
        FluentStyleSheet.FLUENT_WINDOW.apply(widget)
    if isinstance(widget, QDialog):
        _set_style_if_changed(widget, dialog_qss(tokens))
    elif widget.objectName() == "fluentShellContent":
        _set_style_if_changed(widget, station_qss(tokens))
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
        _set_style_if_changed(widget, event_log_qss(tokens))
        viewport = getattr(widget, "viewport", lambda: None)()
        if viewport is not None:
            _set_widget_background(
                viewport, tokens.surface_raised, tokens.text_primary
            )
            _set_style_if_changed(viewport, 
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
        _set_style_if_changed(editor, 
            f"{base}\n{marker}\n"
            "QLineEdit, LineEdit, QComboBox, ComboBox, QSpinBox, SpinBox {"
            f"border: 2px solid {resolved_tokens.danger};"
            "}"
        )
    else:
        _set_style_if_changed(editor, base)
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
    _set_style_if_changed(warning, 
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
    _set_style_if_changed(widget, 
        f"{base}\n{marker}\n"
        "QPushButton:disabled, PushButton:disabled, PrimaryPushButton:disabled {"
        "color: palette(placeholder-text);"
        "background-color: palette(alternate-base);"
        "border: 1px solid palette(mid);"
        "}"
        "QPushButton[controlState=\"emergency\"] {"
        f"color: {tokens.on_emergency}; background-color: {tokens.emergency};"
        f"border: 2px solid {tokens.emergency}; font-weight: 700;"
        "}"
        "QPushButton[controlState=\"emergency\"]:hover {"
        f"color: {tokens.on_emergency}; background-color: {tokens.danger};"
        f"border-color: {tokens.text_primary};"
        "}"
        "QPushButton[controlState=\"emergency\"]:focus {"
        f"border-color: {tokens.focus};"
        "}"
        "QPushButton[controlState=\"confirmed\"] {"
        f"color: {tokens.success}; background-color: {tokens.surface};"
        f"border: 1px solid {tokens.success}; font-weight: 600;"
        "}"
        "QPushButton[controlState=\"confirmed\"]:hover {"
        f"background-color: {tokens.surface_raised}; border-color: {tokens.success};"
        "}"
        "QPushButton[controlState=\"warning\"], QPushButton#warningButton {"
        f"color: {tokens.caution}; background-color: {tokens.surface};"
        f"border: 1px solid {tokens.caution}; font-weight: 600;"
        "}"
        "QPushButton[controlState=\"warning\"]:hover, QPushButton#warningButton:hover {"
        f"background-color: {tokens.surface_raised}; border-color: {tokens.caution};"
        "}"
        "QPushButton[controlState=\"output-neutral\"] {"
        f"color: {tokens.on_output_inactive}; background-color: {tokens.output_inactive};"
        f"border: 1px solid {tokens.border};"
        "}"
        "QPushButton[controlState=\"output-neutral\"]:disabled {"
        f"color: {tokens_for('light').text_muted};"
        "}"
        "QPushButton[controlState=\"output-neutral\"]:hover {"
        f"border-color: {tokens.neutral};"
        "}"
        "QPushButton[controlState=\"output-neutral\"]:focus {"
        f"border-color: {tokens.focus};"
        "}"
        "QPushButton[controlState=\"energized\"], QPushButton#outputOnButton {"
        f"color: {tokens.on_emergency}; background-color: {tokens.output_active};"
        f"border: 1px solid {tokens.output_active}; font-weight: 600;"
        "}"
    )


def _apply_station_card_frame(widget: QWidget, tokens: ThemeTokens) -> None:
    """Give every Fluent card a stable visual boundary in both themes."""

    if not isinstance(widget, CardWidget):
        return
    marker = "/* station-card-frame */"
    is_dialog_surface = widget.property("stationSurface") == "dialog"
    color = tokens.dialog_surface if is_dialog_surface else tokens.surface
    set_background = getattr(widget, "setBackgroundColor", None)
    if callable(set_background):
        set_background(QColor(color))
    if marker in widget.styleSheet():
        base = widget.styleSheet().split(marker, 1)[0].rstrip()
    else:
        base = widget.styleSheet().rstrip()
    _set_style_if_changed(widget, 
        f"{base}\n{marker}\n"
        "CardWidget {"
        f"background-color: {color if is_dialog_surface else 'palette(base)'};"
        "border: 1px solid palette(mid);"
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
        "dialog": tokens.dialog_surface,
        "dialogChrome": tokens.dialog_chrome,
    }.get(str(surface))
    if widget.property("stationDialog"):
        color = tokens.dialog_chrome
    if widget.objectName() in {"fluentShellContent", "fluentShellSplitter"}:
        color = tokens.background
    if color is not None:
        _set_widget_background(widget, color, tokens.text_primary)
    if widget.objectName() == "fluentApplicationStack":
        widget.setProperty("isTransparent", True)
        _set_style_if_changed(widget, 
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


def _semantic_text_color(widget: QLabel, tokens: ThemeTokens) -> tuple[str, bool]:
    """Return ``(hex_color, is_semantic_override)`` for a station label."""
    is_muted_role = isinstance(widget, CaptionLabel) or widget.objectName() == "muted"
    color = tokens.text_muted if is_muted_role else tokens.text_primary
    station_state = widget.property("stationState")
    device_state = widget.property("deviceState")
    output_state = widget.property("outputState")
    safety_state = widget.property("safetyState")
    live_state = widget.property("liveState")
    connection_state = widget.property("connectionState")
    elab_status_state = widget.property("elabStatusState")
    quick_output_state = widget.property("quickOutputState")

    semantic = False
    if (
        device_state in {"verified", "connected"}
        or output_state == "off"
        or safety_state == "ready"
        or live_state == "on"
        or elab_status_state == "success"
        or quick_output_state in {"on", "off"}
        or station_state in {"connected", "verified", "output_off"}
    ):
        color = tokens.success
        semantic = True
    elif (
        device_state == "fault"
        or safety_state == "danger"
        or output_state == "active"
        or live_state in {"error", "fault"}
        or connection_state == "error"
        or elab_status_state == "danger"
        or widget.objectName() == "errorText"
        or station_state in {"fault", "unknown", "output_on"}
    ):
        color = tokens.danger
        semantic = True
    elif (
        safety_state == "caution"
        or device_state in {"compliance", "active"}
        or output_state == "unknown"
        or live_state in {"starting", "paused"}
        or elab_status_state == "caution"
    ):
        color = tokens.caution
        semantic = True
    elif (
        station_state == "disconnected"
        or device_state == "disconnected"
        or output_state == "neutral"
        or live_state == "off"
        or elab_status_state == "neutral"
        or quick_output_state == "unknown"
    ):
        color = tokens.text_muted
        semantic = True
    elif is_muted_role:
        semantic = True
    return color, semantic


def _apply_semantic_text(widget: QWidget, tokens: ThemeTokens) -> None:
    if not isinstance(widget, QLabel):
        return
    # Plain Qt labels keep their explicit palette when QFluent changes theme.
    # FluentLabelBase installs a local custom QSS `color:` rule that overrides
    # both ancestor stylesheets and QPalette, so semantic Fluent labels must
    # also update via Fluent's native setTextColor(light, dark) API.
    color, semantic = _semantic_text_color(widget, tokens)
    if isinstance(widget, FluentLabelBase) and (
        semantic or widget.property("_stationSemanticStyled")
    ):
        light_hex, _ = _semantic_text_color(widget, tokens_for("light"))
        dark_hex, _ = _semantic_text_color(widget, tokens_for("dark"))
        light_color = QColor(light_hex)
        dark_color = QColor(dark_hex)
        if (
            widget.lightColor.name(QColor.NameFormat.HexArgb)
            != light_color.name(QColor.NameFormat.HexArgb)
            or widget.darkColor.name(QColor.NameFormat.HexArgb)
            != dark_color.name(QColor.NameFormat.HexArgb)
        ):
            widget.setTextColor(light_color, dark_color)
        widget.setProperty("_stationSemanticStyled", semantic)

    foreground = QColor(color)
    palette = widget.palette()
    if (
        palette.color(QPalette.ColorRole.WindowText).name().lower() == foreground.name().lower()
        and palette.color(QPalette.ColorRole.Text).name().lower() == foreground.name().lower()
    ):
        return
    palette.setColor(QPalette.ColorRole.WindowText, foreground)
    palette.setColor(QPalette.ColorRole.Text, foreground)
    palette.setColor(QPalette.ColorRole.ButtonText, foreground)
    widget.setPalette(palette)
    widget.update()
