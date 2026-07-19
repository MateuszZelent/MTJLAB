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


def dialog_qss(tokens: ThemeTokens) -> str:
    """Theme native Qt popup infrastructure without overriding Fluent controls."""

    return f"""
QDialog, QMessageBox, QInputDialog, QFileDialog {{
    background: {tokens.background};
    color: {tokens.text_primary};
}}
QDialog QLabel, QMessageBox QLabel, QInputDialog QLabel, QFileDialog QLabel {{
    color: {tokens.text_primary};
}}
QDialogButtonBox QPushButton {{
    min-width: 88px;
    min-height: 30px;
    padding: 0 14px;
    color: {tokens.text_primary};
    background: {tokens.surface_raised};
    border: 1px solid {tokens.border};
    border-radius: 6px;
}}
QDialogButtonBox QPushButton:hover {{
    background: {tokens.surface};
    border-color: {tokens.focus};
}}
QDialogButtonBox QPushButton:pressed {{
    background: {tokens.border};
}}
QDialogButtonBox QPushButton:disabled {{
    color: {tokens.text_muted};
    background: {tokens.surface_raised};
    border-color: {tokens.border};
}}
QDialogButtonBox QPushButton:default {{
    color: #ffffff;
    background: {tokens.accent};
    border-color: {tokens.accent};
}}
QDialogButtonBox QPushButton:default:hover {{
    border-color: {tokens.focus};
}}
QFileDialog QTreeView, QFileDialog QListView, QFileDialog QLineEdit,
QInputDialog QLineEdit, QInputDialog QComboBox {{
    color: {tokens.text_primary};
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    selection-background-color: {tokens.accent};
    selection-color: #ffffff;
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
