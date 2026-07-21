from __future__ import annotations

from .tokens import ThemeTokens


def event_log_qss(tokens: ThemeTokens) -> str:
    """Direct token styling for Fluent's locally styled event-log editor."""

    return f"""
QPlainTextEdit, PlainTextEdit {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
    border: 1px solid {tokens.border};
    border-radius: 6px;
}}
QPlainTextEdit QWidget, PlainTextEdit QWidget {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
}}
"""


def dialog_qss(tokens: ThemeTokens) -> str:
    """Theme native Qt popup infrastructure without overriding Fluent controls."""

    accent = tokens.accent.lstrip("#")
    red, green, blue = (int(accent[index:index + 2], 16) for index in (0, 2, 4))
    accent_luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    default_text = tokens.background if accent_luma > 0.55 else "#ffffff"
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
    color: {default_text};
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
QWidget#settingsPage {{
    background: {tokens.background};
}}
QWidget#settingsPage QLabel {{
    color: {tokens.text_primary};
}}
QWidget#settingsPage QLabel#settingsProfileSummary,
QWidget#settingsPage QLabel#muted {{
    color: {tokens.text_muted};
}}
QWidget#settingsPage QScrollArea#settingsForm,
QWidget#settingsPage QScrollArea#settingsForm > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QWidget#settingsPage QTableWidget,
QWidget#settingsPage QPlainTextEdit {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
    alternate-background-color: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 6px;
    selection-background-color: {tokens.accent};
    selection-color: #ffffff;
}}
QWidget#settingsPage QHeaderView::section {{
    background: {tokens.surface};
    color: {tokens.text_primary};
    border: none;
    border-bottom: 1px solid {tokens.border};
    padding: 7px 9px;
}}
QWidget#settingsPage QLabel#settingsValidationBanner,
QWidget#settingsPage QLabel#settingsFieldError {{
    color: {tokens.danger};
}}
QLabel[deviceState="verified"], QLabel[outputState="off"],
QLabel[stationState="connected"], QLabel[stationState="verified"],
QLabel[stationState="output_off"] {{
    color: {tokens.success};
}}
QLabel[outputState="neutral"] {{
    color: {tokens.text_muted};
}}
QLabel[deviceState="fault"], QLabel[safetyState="danger"],
QLabel[outputState="active"], QLabel[stationState="fault"],
QLabel[stationState="unknown"], QLabel[stationState="output_on"] {{
    color: {tokens.danger};
}}
QLabel[stationState="disconnected"] {{
    color: {tokens.text_muted};
}}
QLabel[safetyState="caution"], QLabel[deviceState="compliance"],
QLabel[deviceState="active"] {{
    color: {tokens.caution};
}}
QLabel#inlineValidationWarning {{
    color: {tokens.danger};
    font-weight: 600;
}}
QLineEdit[validationState="error"], LineEdit[validationState="error"],
QComboBox[validationState="error"], ComboBox[validationState="error"],
QSpinBox[validationState="error"], SpinBox[validationState="error"] {{
    border: 2px solid {tokens.danger};
}}
"""
