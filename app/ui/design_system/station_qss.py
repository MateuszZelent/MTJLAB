from __future__ import annotations

from .tokens import ThemeTokens


def event_log_qss(tokens: ThemeTokens) -> str:
    """Direct token styling for Fluent's locally styled event-log editor."""

    return f"""
PlainTextEdit {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
    border: 1px solid {tokens.border};
    border-radius: 6px;
}}
"""


def station_qss(tokens: ThemeTokens) -> str:
    return f"""
QWidget#fluentShellContent, QWidget#fluentShellSplitter {{
    background: {tokens.background};
}}
QWidget[stationSurface="page"] {{
    background: {tokens.background};
}}
QWidget[stationSurface="surface"] {{
    background: {tokens.surface};
}}
QWidget[stationSurface="raised"] {{
    background: {tokens.surface_raised};
}}
QPlainTextEdit[stationSurface="raised"], QPlainTextEdit#eventLogText {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
    border: 1px solid {tokens.border};
    border-radius: 6px;
}}
QPlainTextEdit#eventLogText QWidget {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
}}
QLabel[deviceState="verified"], QLabel[outputState="off"] {{
    color: {tokens.success};
}}
QLabel[deviceState="fault"], QLabel[safetyState="danger"],
QLabel[outputState="active"] {{
    color: {tokens.danger};
}}
QLabel[safetyState="caution"], QLabel[deviceState="compliance"],
QLabel[deviceState="active"] {{
    color: {tokens.caution};
}}
QLineEdit[validationState="error"], QComboBox[validationState="error"],
QSpinBox[validationState="error"] {{
    border: 2px solid {tokens.danger};
}}
"""
