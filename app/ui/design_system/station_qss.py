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
    background: {tokens.dialog_surface};
    color: {tokens.text_primary};
}}
QDialog[stationDialog="true"] {{
    background: {tokens.dialog_chrome};
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
QWidget#recipeWorkspaceCard {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 10px;
}}
QWidget#recipeWorkspaceCard QLabel#recipeWorkspaceState,
QWidget#recipeExecutionLockBanner QLabel#recipeExecutionLockTitle {{
    font-weight: 600;
}}
QWidget#recipeBuilderPanel,
QWidget#recipeInspectorPanel,
QScrollArea#recipeLibraryScroll {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 8px;
}}
QSplitter#recipeWorkspaceSplitter::handle {{
    background: transparent;
}}
QSplitter#recipeWorkspaceSplitter::handle:hover {{
    background: {tokens.border};
}}
QWidget#recipeExecutionLockBanner {{
    background: {tokens.surface_raised};
    border: 1px solid {tokens.border};
    border-left: 3px solid {tokens.caution};
    border-radius: 7px;
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
QWidget#executionCurrentOperationCard {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-left: 3px solid {tokens.accent};
    border-radius: 8px;
}}
QWidget#executionWorkspaceCard,
QWidget#executionControlCard {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 8px;
}}
QLabel#executionActivityIndicator {{
    color: {tokens.accent};
    font-size: 20px;
    font-weight: 700;
}}
QLabel#executionActivityIndicator[activityPulse="off"] {{
    color: {tokens.text_muted};
}}
QLabel#executionOperationPhase {{
    color: {tokens.text_muted};
    font-weight: 600;
}}
QLabel#executionOperationValue {{
    color: {tokens.accent};
    font-size: 20px;
    font-weight: 700;
}}
QLabel#executionOperationState {{
    font-weight: 600;
}}
QLabel#executionSectionTitle {{
    font-weight: 600;
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
    background: {tokens.background};
    border: none;
}}
QScrollArea#quickControlsScroll,
QScrollArea#quickControlsScroll > QWidget,
QScrollArea#quickControlsScroll > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QLabel#quickControlLimits {{
    color: {tokens.text_muted};
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
QWidget#elabPage QLabel#elabStatus[elabStatusState="success"] {{
    color: {tokens.success};
}}
QWidget#elabPage QLabel#elabStatus[elabStatusState="caution"] {{
    color: {tokens.caution};
}}
QWidget#elabPage QLabel#elabStatus[elabStatusState="danger"] {{
    color: {tokens.danger};
}}
QWidget#elabPage QLabel#elabStatus[elabStatusState="neutral"] {{
    color: {tokens.text_muted};
}}
QWidget#elabPage QTableWidget#elabHistoryTable {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
    alternate-background-color: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 6px;
    selection-background-color: {tokens.accent};
    selection-color: #ffffff;
}}
QLabel[deviceState="verified"], QLabel[deviceState="connected"], QLabel[outputState="off"],
QLabel[safetyState="ready"], QLabel[liveState="on"],
QLabel#quickOutputState[quickOutputState="on"],
QLabel#quickOutputState[quickOutputState="off"],
QLabel[stationState="connected"], QLabel[stationState="verified"],
QLabel[stationState="output_off"] {{
    color: {tokens.success};
}}
QLabel#anritsuLiveIndicator[liveState="on"] {{
    color: {tokens.success};
    font-weight: 600;
}}
QLabel[outputState="neutral"], QLabel[liveState="off"],
QLabel[deviceState="disconnected"] {{
    color: {tokens.text_muted};
}}
QLabel#quickOutputState[quickOutputState="unknown"] {{
    color: {tokens.text_muted};
}}
QLabel[deviceState="fault"], QLabel[safetyState="danger"],
QLabel[outputState="active"], QLabel[stationState="fault"],
QLabel[stationState="unknown"], QLabel[stationState="output_on"],
QLabel[liveState="error"], QLabel[liveState="fault"],
QLabel[connectionState="error"], QLabel#errorText {{
    color: {tokens.danger};
}}
QLabel[stationState="disconnected"] {{
    color: {tokens.text_muted};
}}
QLabel[safetyState="caution"], QLabel[deviceState="compliance"],
QLabel[deviceState="active"], QLabel[outputState="unknown"],
QLabel[liveState="starting"], QLabel[liveState="paused"] {{
    color: {tokens.caution};
}}
QLabel#anritsuLiveIndicator[liveState="starting"],
QLabel#anritsuLiveIndicator[liveState="paused"] {{
    color: {tokens.caution};
    font-weight: 600;
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
QWidget#resultsPage {{
    background: {tokens.background};
}}
QWidget#resultsHeaderCard {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 8px;
}}
QFrame#resultsBreadcrumbBar {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 7px;
}}
QWidget#resultsWorkspaceHost {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 8px;
}}
QWidget#resultsTelemetryBar {{
    background: {tokens.plot_background};
    border-bottom: 1px solid {tokens.border};
}}
QWidget#resultsCommandStrip, QWidget#resultsScrubberBar, QWidget#resultsFomBar {{
    background: {tokens.surface};
    border-top: 1px solid {tokens.border};
    border-bottom: 1px solid {tokens.border};
}}
QWidget#resultsKpiTile {{
    background: {tokens.surface_raised};
    border: 1px solid {tokens.border};
    border-radius: 7px;
}}
QScrollArea#resultsInspectorScroll,
QScrollArea#resultsInspectorScroll > QWidget,
QScrollArea#resultsInspectorScroll > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QWidget#resultsInspectorDrawer {{
    background: {tokens.surface};
    border-left: 1px solid {tokens.border};
}}
QSplitter#resultsMainSplitter::handle,
QSplitter#resultsTabSplitter::handle {{
    background: transparent;
}}
QSplitter#resultsMainSplitter::handle:hover,
QSplitter#resultsTabSplitter::handle:hover {{
    background: {tokens.border};
}}
QLabel#resultsRunTitle {{
    color: {tokens.text_primary};
    font-weight: 600;
}}
QLabel#resultsTelemetryPrimary {{
    color: {tokens.text_primary};
    font-family: "Cascadia Code", "Consolas", monospace;
}}
QLabel#resultsTelemetryMuted {{
    color: {tokens.text_muted};
    font-family: "Cascadia Code", "Consolas", monospace;
}}
QLabel#resultsRunStatus[runStatus="completed"] {{
    color: {tokens.success};
    font-weight: 600;
}}
QLabel#resultsRunStatus[runStatus="aborted"] {{
    color: {tokens.caution};
    font-weight: 600;
}}
QLabel#resultsRunStatus[runStatus="faulted"],
QLabel#resultsRunStatus[runStatus="incomplete"] {{
    color: {tokens.danger};
    font-weight: 600;
}}
QLabel#resultsRunStatus[runStatus="neutral"] {{
    color: {tokens.text_muted};
    font-weight: 600;
}}
QComboBox#resultFilterCombo {{
    background: {tokens.surface_raised};
    color: {tokens.text_primary};
    border: 1px solid {tokens.border};
    border-radius: 6px;
    padding: 3px 24px 3px 8px;
}}
QComboBox#resultFilterCombo:hover {{
    border-color: {tokens.focus};
}}
QComboBox#resultFilterCombo:disabled {{
    color: {tokens.text_muted};
    background: {tokens.surface};
}}
QComboBox#resultFilterCombo::drop-down {{
    border: none;
    width: 20px;
}}
"""
