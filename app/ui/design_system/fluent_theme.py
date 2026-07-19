from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme

from .station_qss import station_qss
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
    return AppliedTheme(name=name, tokens=tokens)
