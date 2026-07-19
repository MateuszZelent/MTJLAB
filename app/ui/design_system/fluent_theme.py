from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme

from .station_qss import event_log_qss, station_qss
from .theme import effective_theme
from .tokens import ThemeTokens, tokens_for


@dataclass(frozen=True, slots=True)
class AppliedTheme:
    name: str
    tokens: ThemeTokens


def apply_application_theme(application: QApplication, mode: str) -> AppliedTheme:
    name = effective_theme(mode)
    tokens = tokens_for(name)
    setTheme(Theme.DARK if name == "dark" else Theme.LIGHT)
    application.setStyleSheet(station_qss(tokens))
    _settle_fluent_background_animations(application)
    _apply_station_control_styles(application, tokens)
    return AppliedTheme(name=name, tokens=tokens)


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
        if animation is None or not callable(normal_background) or not callable(set_background):
            continue
        animation.stop()
        set_background(normal_background())
        widget.update()


def _apply_station_control_styles(application: QApplication, tokens: ThemeTokens) -> None:
    """Override Fluent controls that install a higher-precedence local QSS."""

    for widget in application.allWidgets():
        if widget.objectName() == "eventLogText":
            widget.setStyleSheet(event_log_qss(tokens))
            widget.update()
