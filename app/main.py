"""Desktop entry point for the safe local instrument-control application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow


STYLE = """
QMainWindow { background: #10151d; color: #e8edf3; }
QWidget { background: #10151d; color: #e8edf3; font-family: Segoe UI; font-size: 10pt; }
QTabWidget::pane { border: 1px solid #26384a; }
QScrollArea#settingsForm { background: transparent; }
QGroupBox#settingsCard { background: #18212c; border: 1px solid #344659; border-radius: 10px; margin-top: 14px; padding: 12px; font-weight: 700; color: #dcecff; }
QGroupBox#settingsCard::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; }
QFrame#settingsTableCard { background: #18212c; border: 1px solid #344659; border-radius: 10px; }
QFrame#safetyDeviceCard { background: #202c39; border: 1px solid #40556b; border-radius: 10px; }
QLabel#safetyDeviceTitle { color: #dcecff; font-size: 13pt; font-weight: 700; }
QFrame#safetyLimitRow { background: #17212c; border: 1px solid #304457; border-radius: 7px; }
QLabel#safetyLimitLabel { color: #e8edf3; font-weight: 600; }
QLabel#safetyLimitTag { color: #9fb4c8; font-size: 8pt; font-weight: 700; }
QLabel#safetyLimitUnit { color: #9fb4c8; }
QLineEdit#safetyLimitInput { background: #111a23; border: 1px solid #526a81; border-radius: 5px; padding: 6px 8px; color: #f5f9ff; }
QLabel#settingsValidationBanner { color: #ffb3bf; background: #3a1d27; border: 1px solid #9d3449; border-radius: 6px; padding: 9px 11px; font-weight: 600; }
QLineEdit[validationState="error"], QComboBox[validationState="error"], QSpinBox[validationState="error"] { border: 2px solid #ff657a; background: #30202a; }
QLabel#settingsFieldError { color: #ff9ba8; font-size: 9pt; font-weight: 600; }
QTabBar::tab { background: #18212c; color: #91a0b2; padding: 10px 18px; margin-right: 2px; }
QTabBar::tab:selected { background: #26384a; color: #e8edf3; }
QTabBar::tab:hover { color: #ffffff; background: #213043; }
QToolBar#applicationRibbon { background: #151d27; border: 0; border-bottom: 1px solid #2c3d50; spacing: 3px; padding: 4px 8px; }
QToolBar#applicationRibbon QToolButton { background: transparent; color: #aebccc; border: 0; border-radius: 7px; padding: 5px 13px; min-width: 62px; }
QToolBar#applicationRibbon QToolButton:hover { background: #213043; color: white; }
QToolBar#applicationRibbon QToolButton:checked { background: #244f70; color: white; }
QToolBar#applicationRibbon::separator { background: #344659; width: 1px; margin: 7px 8px; }
QWidget#menuStatusArea { background: transparent; }
QLabel#compactDeviceStatus { color: #91a0b2; font-size: 9pt; }
QLabel#compactDeviceStatus[deviceState="verified"], QLabel#compactDeviceStatus[deviceState="output_off"] { color: #38d996; }
QLabel#compactDeviceStatus[deviceState="output_on"], QLabel#compactDeviceStatus[deviceState="compliance"] { color: #ffcc66; }
QLabel#compactDeviceStatus[deviceState="fault"], QLabel#compactDeviceStatus[deviceState="unknown"] { color: #ff657a; }
QLabel#profileLocked { color: #ffcc66; font-weight: 700; }
QLabel#profileApproved { color: #38d996; font-weight: 700; }
QPushButton#compactEmergencyButton { background: #7f2938; color: white; border-radius: 5px; padding: 3px 8px; font-size: 9pt; font-weight: 700; }
QPushButton#compactEmergencyButton:hover { background: #a43548; }
QLineEdit, QPlainTextEdit, QTreeWidget, QTableWidget, QComboBox, QSpinBox { background: #18212c; border: 1px solid #344659; border-radius: 6px; padding: 7px; color: #e8edf3; }
QTreeWidget { alternate-background-color: #141c26; outline: 0; }
QTreeWidget::item { color: #e8edf3; padding: 3px; }
QTreeWidget::item:selected { background: #28577b; color: white; }
QTableWidget { alternate-background-color: #141c26; selection-background-color: #28577b; selection-color: white; }
QTableWidget::item { padding: 6px; border-bottom: 1px solid #26384a; }
QLabel#assignmentConfirmed { color: #38d996; font-weight: 700; padding: 5px 8px; }
QLabel#assignmentPendingHint { color: #ffcc66; background: #302817; border-radius: 5px; padding: 5px 8px; }
QPushButton#assignmentCompleteButton { background: #173227; color: #38d996; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #4ba3ff; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #607084; background: #141c26; border-color: #26384a; }
QPushButton { background: #26384a; border: 0; border-radius: 5px; padding: 6px 10px; color: #e8edf3; font-weight: 600; }
QPushButton[compact="true"] { padding: 5px 8px; font-weight: 600; }
QPushButton:hover { background: #34516a; }
QPushButton:disabled { background: #1b2632; color: #58687a; }
QPushButton#primaryButton { background: #1769aa; color: white; }
QPushButton#primaryButton:hover { background: #2385d1; }
QPushButton#warningButton { background: #76561b; color: #ffe1a3; }
QPushButton#outputOnButton { background: #8f2638; color: white; }
QPushButton#outputOnButton:hover { background: #b7334b; }
QPushButton#outputOffButton { background: #17684c; color: white; }
QPushButton#outputOffButton:hover { background: #218967; }
QPushButton#emergencyButton { background: #8f2638; color: white; }
QPushButton#emergencyButton:hover { background: #b7334b; }
QFrame#deviceCard { background: #18212c; border: 1px solid #344659; border-radius: 8px; padding: 10px; min-height: 170px; }
QFrame#connectionPanel { background: #151f2b; border: 1px solid #30455b; border-radius: 10px; padding: 10px; }
QTabWidget#dashboardWorkspace::pane { border: 1px solid #30455b; border-radius: 10px; background: #111923; top: -1px; }
QTabWidget#dashboardWorkspace QTabBar::tab { background: transparent; color: #91a0b2; padding: 9px 18px; margin-right: 4px; border-bottom: 2px solid transparent; }
QTabWidget#dashboardWorkspace QTabBar::tab:selected { color: #72b7f2; border-bottom-color: #4ba3ff; }
QTabWidget#dashboardWorkspace QTabBar::tab:hover { color: #e8edf3; }
QLabel#pageTitle { font-size: 20pt; font-weight: 700; }
QLabel#cardTitle { font-size: 14pt; font-weight: 700; }
QLabel#sectionTitle { font-size: 13pt; font-weight: 700; color: #f2f6fa; }
QLabel#muted { color: #91a0b2; }
QLabel#readout { font-family: Consolas; font-size: 13pt; padding: 10px; background: #18212c; }
QLabel#stateDisconnected { color: #91a0b2; font-weight: 700; }
QLabel#stateVerified, QLabel#stateOutputOff { color: #38d996; font-weight: 700; }
QLabel#stateOutputOn, QLabel#stateCompliance { color: #ffcc66; font-weight: 700; }
QLabel#stateFault, QLabel#stateUnknown { color: #ff657a; font-weight: 700; }
QLabel#checklist { background: #18212c; border-radius: 8px; padding: 14px; }
QHeaderView::section { background: #26384a; color: #e8edf3; padding: 6px; border: 0; }
QFrame#rigolHero { background: #151f2b; border: 1px solid #30455b; border-radius: 10px; padding: 10px; }
QFrame#rigolSafetyCard { background: #18212c; border: 1px solid #344659; border-radius: 10px; padding: 10px; }
QLabel#rigolLed { color: #91a0b2; font-size: 18pt; }
QLabel#rigolState { color: #e8edf3; font-weight: 700; }
QLabel#rigolBadge { color: #72b7f2; background: #13283a; border-radius: 8px; padding: 5px 9px; }
QLabel#rigolWarning { color: #ffcc66; background: #302817; border-radius: 6px; padding: 9px; }
QFrame#keithleyHero, QFrame#keithleyChannelCard { background: #151f2b; border: 1px solid #30455b; border-radius: 10px; padding: 9px; }
QLabel#keithleyPageTitle { font-size: 16pt; font-weight: 700; }
QLabel#keithleyCardTitle { font-size: 11pt; font-weight: 700; }
QLabel#keithleyHistoryTitle { font-size: 11pt; font-weight: 700; }
QLabel#keithleyHistoryNote { color: #8293a6; font-size: 8pt; letter-spacing: 0.3px; }
QLabel#keithleyInterlockStatus { color: #9fb0c2; font-size: 8pt; }
QLabel#keithleyLastUpdate { color: #8293a6; font-size: 8pt; padding: 0 8px; }
QScrollArea#keithleyControlPanel QWidget { font-size: 9pt; }
QScrollArea#keithleyControlPanel QLineEdit, QScrollArea#keithleyControlPanel QComboBox, QScrollArea#keithleyControlPanel QSpinBox { padding: 4px 6px; }
QSplitter#keithleyWorkspace::handle { background: #26384a; width: 5px; margin: 4px 1px; border-radius: 2px; }
QToolButton#plotToolButton { background: transparent; color: #9fb0c2; border: 1px solid #30455b; border-radius: 4px; padding: 3px 7px; font-size: 8pt; }
QToolButton#plotToolButton:hover { background: #213043; color: white; border-color: #4b6680; }
QFrame#keithleyChannelCard[selected="true"] { border: 2px solid #4ba3ff; }
QFrame#keithleyMeterTile { background: #182838; border: 1px solid #2d455b; border-radius: 8px; }
QLabel#keithleyLed, QLabel#keithleyOutputLed { color: #91a0b2; font-size: 17pt; }
QLabel#keithleyState, QLabel#keithleyOutputState { font-weight: 700; }
QLabel#keithleyMeterValue { color: #eaf5ff; font-family: Consolas; font-size: 13pt; font-weight: 700; }
QLabel#keithleyComplianceClear { color: #38d996; background: #173227; border-radius: 6px; padding: 5px 8px; font-weight: 700; }
QLabel#keithleyComplianceActive { color: #ff657a; background: #3a1b24; border-radius: 6px; padding: 5px 8px; font-weight: 700; }
QTabWidget#keithleyControlTabs > QTabBar::tab { padding: 10px 15px; }
QFrame#anritsuProcessingCard { background: #151f2b; border: 1px solid #30455b; border-radius: 10px; padding: 9px; }
QLabel#anritsuLiveIndicator { color: #91a0b2; background: #18212c; border: 1px solid #344659; border-radius: 10px; padding: 5px 10px; font-size: 9pt; font-weight: 700; }
QLabel#anritsuLiveIndicator[liveState="on"] { color: #38d996; background: #173227; border-color: #28634d; }
QLabel#anritsuLiveIndicator[liveState="starting"], QLabel#anritsuLiveIndicator[liveState="stopping"], QLabel#anritsuLiveIndicator[liveState="paused"] { color: #ffcc66; background: #302817; border-color: #76561b; }
QLabel#anritsuSgIndicator { color: #91a0b2; background: #18212c; border: 1px solid #344659; border-radius: 10px; padding: 6px 10px; font-weight: 700; }
QLabel#anritsuSgIndicator[liveState="off"] { color: #38d996; background: #173227; border-color: #28634d; }
QLabel#anritsuSgIndicator[liveState="starting"] { color: #ffcc66; background: #302817; border-color: #76561b; }
QLabel#anritsuSgIndicator[liveState="on"] { color: #ff657a; background: #3a1b24; border-color: #8f2638; }
QWidget#anritsuControlPanel { background: #121a24; border-radius: 10px; }
QSplitter#anritsuWorkspaceSplitter::handle { background: #26384a; width: 5px; margin: 4px 1px; border-radius: 2px; }
QLabel#limitBadge { color: #9ecbff; background: #17283a; border: 1px solid #31506d; border-radius: 6px; padding: 6px; font-family: Consolas; font-size: 9pt; }
QLabel#limitBadge[limitState="undefined"] { color: #ffcc66; background: #302817; border-color: #76561b; }
QLabel#limitBadge[keithleyCompact="true"] { padding: 4px; font-size: 8pt; }
QPushButton#limitEditButton { padding: 6px 8px; background: #30465a; }
QPushButton#infoButton { background: #26384a; color: #9ecbff; border: 1px solid #3e5a73; border-radius: 14px; font-size: 13pt; font-weight: 700; padding: 0; }
QPushButton#infoButton:hover { background: #34516a; color: white; }
QTabWidget#rigolControlTabs > QTabBar::tab { padding: 11px 16px; }
QTabWidget#rigolAdvancedTabs > QTabBar::tab { padding: 8px 12px; }
QScrollArea { border: 0; }
QScrollBar:vertical { background: #111923; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #3b536b; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4ba3ff; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QFrame#notificationBanner { background: #302817; border: 1px solid #76561b; border-radius: 6px; }
QFrame#notificationBanner[severity="error"] { background: #3a1b24; border-color: #8f2638; }
"""

LIGHT_STYLE = """
QMainWindow { background: #f4f7fb; color: #17212b; }
QWidget { background: #f4f7fb; color: #17212b; font-family: Segoe UI; font-size: 10pt; }
QMenuBar, QMenu { background: #ffffff; color: #17212b; }
QMenu::item:selected { background: #dbeafe; }
QTabWidget::pane { border: 1px solid #c8d3df; }
QScrollArea#settingsForm { background: transparent; }
QGroupBox#settingsCard { background: #ffffff; border: 1px solid #c8d3df; border-radius: 10px; margin-top: 14px; padding: 12px; font-weight: 700; color: #17324d; }
QGroupBox#settingsCard::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; }
QFrame#settingsTableCard { background: #ffffff; border: 1px solid #c8d3df; border-radius: 10px; }
QFrame#safetyDeviceCard { background: #f7faff; border: 1px solid #c8d8e8; border-radius: 10px; }
QLabel#safetyDeviceTitle { color: #17324d; font-size: 13pt; font-weight: 700; }
QFrame#safetyLimitRow { background: #ffffff; border: 1px solid #d8e2ec; border-radius: 7px; }
QLabel#safetyLimitLabel { color: #17324d; font-weight: 600; }
QLabel#safetyLimitTag { color: #58708a; font-size: 8pt; font-weight: 700; }
QLabel#safetyLimitUnit { color: #58708a; }
QLineEdit#safetyLimitInput { background: #ffffff; border: 1px solid #9eb4c9; border-radius: 5px; padding: 6px 8px; color: #17212b; }
QLabel#settingsValidationBanner { color: #9e1d33; background: #fde7eb; border: 1px solid #e6a9b4; border-radius: 6px; padding: 9px 11px; font-weight: 600; }
QLineEdit[validationState="error"], QComboBox[validationState="error"], QSpinBox[validationState="error"] { border: 2px solid #c7364e; background: #fff0f2; }
QLabel#settingsFieldError { color: #b4233a; font-size: 9pt; font-weight: 600; }
QTabBar::tab { background: #e8eef5; color: #526273; padding: 10px 18px; margin-right: 2px; }
QTabBar::tab:selected { background: #ffffff; color: #17212b; }
QTabBar::tab:hover { color: #0b5da7; background: #dce9f6; }
QToolBar#applicationRibbon { background: #ffffff; border: 0; border-bottom: 1px solid #c8d3df; spacing: 3px; padding: 4px 8px; }
QToolBar#applicationRibbon QToolButton { background: transparent; color: #526273; border: 0; border-radius: 7px; padding: 5px 13px; min-width: 62px; }
QToolBar#applicationRibbon QToolButton:hover { background: #e8f1f9; color: #0b5da7; }
QToolBar#applicationRibbon QToolButton:checked { background: #dbeafe; color: #0b5da7; }
QToolBar#applicationRibbon::separator { background: #c8d3df; width: 1px; margin: 7px 8px; }
QWidget#menuStatusArea { background: transparent; }
QLabel#compactDeviceStatus { color: #607284; font-size: 9pt; }
QLabel#compactDeviceStatus[deviceState="verified"], QLabel#compactDeviceStatus[deviceState="output_off"] { color: #087f5b; }
QLabel#compactDeviceStatus[deviceState="output_on"], QLabel#compactDeviceStatus[deviceState="compliance"] { color: #9a6700; }
QLabel#compactDeviceStatus[deviceState="fault"], QLabel#compactDeviceStatus[deviceState="unknown"] { color: #b4233a; }
QLabel#profileLocked { color: #9a6700; font-weight: 700; }
QLabel#profileApproved { color: #087f5b; font-weight: 700; }
QPushButton#compactEmergencyButton { background: #a53345; color: white; border-radius: 5px; padding: 3px 8px; font-size: 9pt; font-weight: 700; }
QPushButton#compactEmergencyButton:hover { background: #c43c52; }
QLineEdit, QPlainTextEdit, QTreeWidget, QTableWidget, QComboBox, QSpinBox { background: #ffffff; border: 1px solid #b9c7d5; border-radius: 6px; padding: 7px; color: #17212b; }
QTreeWidget { alternate-background-color: #eef3f8; outline: 0; }
QTreeWidget::item { color: #17212b; padding: 3px; }
QTreeWidget::item:selected { background: #b8dcfa; color: #102a43; }
QTableWidget { alternate-background-color: #eef3f8; selection-background-color: #b8dcfa; selection-color: #102a43; }
QTableWidget::item { padding: 6px; border-bottom: 1px solid #d9e2ec; }
QLabel#assignmentConfirmed { color: #087f5b; font-weight: 700; padding: 5px 8px; }
QLabel#assignmentPendingHint { color: #765500; background: #fff2c7; border-radius: 5px; padding: 5px 8px; }
QPushButton#assignmentCompleteButton { background: #dff5eb; color: #087f5b; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #1976bd; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #8996a3; background: #edf1f5; border-color: #d4dce4; }
QPushButton { background: #dce5ee; border: 0; border-radius: 5px; padding: 6px 10px; color: #17212b; font-weight: 600; }
QPushButton[compact="true"] { padding: 5px 8px; font-weight: 600; }
QPushButton:hover { background: #c8d9e8; }
QPushButton:disabled { background: #edf1f5; color: #9aa6b2; }
QPushButton#primaryButton { background: #1769aa; color: white; }
QPushButton#primaryButton:hover { background: #0f7bc9; }
QPushButton#warningButton { background: #f4dca4; color: #664a0d; }
QPushButton#outputOnButton, QPushButton#emergencyButton { background: #b52d43; color: white; }
QPushButton#outputOnButton:hover, QPushButton#emergencyButton:hover { background: #d23b53; }
QPushButton#outputOffButton { background: #19805c; color: white; }
QPushButton#outputOffButton:hover { background: #209a70; }
QFrame#deviceCard { background: #ffffff; border: 1px solid #c8d3df; border-radius: 8px; padding: 10px; min-height: 170px; }
QFrame#connectionPanel { background: #ffffff; border: 1px solid #c8d3df; border-radius: 10px; padding: 10px; }
QTabWidget#dashboardWorkspace::pane { border: 1px solid #c8d3df; border-radius: 10px; background: #f7f9fb; top: -1px; }
QTabWidget#dashboardWorkspace QTabBar::tab { background: transparent; color: #607284; padding: 9px 18px; margin-right: 4px; border-bottom: 2px solid transparent; }
QTabWidget#dashboardWorkspace QTabBar::tab:selected { color: #0b5da7; border-bottom-color: #1976bd; }
QTabWidget#dashboardWorkspace QTabBar::tab:hover { color: #17212b; }
QLabel#pageTitle { font-size: 20pt; font-weight: 700; }
QLabel#cardTitle { font-size: 14pt; font-weight: 700; }
QLabel#sectionTitle { font-size: 13pt; font-weight: 700; color: #17212b; }
QLabel#muted { color: #607284; }
QLabel#readout { font-family: Consolas; font-size: 13pt; padding: 10px; background: #ffffff; }
QLabel#stateDisconnected { color: #6b7b8c; font-weight: 700; }
QLabel#stateVerified, QLabel#stateOutputOff { color: #087f5b; font-weight: 700; }
QLabel#stateOutputOn, QLabel#stateCompliance { color: #9a6700; font-weight: 700; }
QLabel#stateFault, QLabel#stateUnknown { color: #b4233a; font-weight: 700; }
QLabel#checklist { background: #ffffff; border-radius: 8px; padding: 14px; }
QHeaderView::section { background: #dce5ee; color: #17212b; padding: 6px; border: 0; }
QFrame#rigolHero { background: #ffffff; border: 1px solid #c8d3df; border-radius: 10px; padding: 10px; }
QFrame#rigolSafetyCard { background: #ffffff; border: 1px solid #c8d3df; border-radius: 10px; padding: 10px; }
QLabel#rigolLed { color: #6b7b8c; font-size: 18pt; }
QLabel#rigolState { color: #17212b; font-weight: 700; }
QLabel#rigolBadge { color: #0b5da7; background: #e2f1fc; border-radius: 8px; padding: 5px 9px; }
QLabel#rigolWarning { color: #765500; background: #fff2c7; border-radius: 6px; padding: 9px; }
QFrame#keithleyHero, QFrame#keithleyChannelCard { background: #ffffff; border: 1px solid #c8d3df; border-radius: 10px; padding: 9px; }
QLabel#keithleyPageTitle { font-size: 16pt; font-weight: 700; }
QLabel#keithleyCardTitle { font-size: 11pt; font-weight: 700; }
QLabel#keithleyHistoryTitle { font-size: 11pt; font-weight: 700; }
QLabel#keithleyHistoryNote { color: #687b8e; font-size: 8pt; letter-spacing: 0.3px; }
QLabel#keithleyInterlockStatus { color: #607284; font-size: 8pt; }
QLabel#keithleyLastUpdate { color: #687b8e; font-size: 8pt; padding: 0 8px; }
QScrollArea#keithleyControlPanel QWidget { font-size: 9pt; }
QScrollArea#keithleyControlPanel QLineEdit, QScrollArea#keithleyControlPanel QComboBox, QScrollArea#keithleyControlPanel QSpinBox { padding: 4px 6px; }
QSplitter#keithleyWorkspace::handle { background: #c8d3df; width: 5px; margin: 4px 1px; border-radius: 2px; }
QToolButton#plotToolButton { background: transparent; color: #52677b; border: 1px solid #c5d2df; border-radius: 4px; padding: 3px 7px; font-size: 8pt; }
QToolButton#plotToolButton:hover { background: #e7f0f8; color: #0b5da7; border-color: #9abbd5; }
QFrame#keithleyChannelCard[selected="true"] { border: 2px solid #1976bd; }
QFrame#keithleyMeterTile { background: #f3f8fc; border: 1px solid #d1dee9; border-radius: 8px; }
QLabel#keithleyLed, QLabel#keithleyOutputLed { color: #6b7b8c; font-size: 17pt; }
QLabel#keithleyState, QLabel#keithleyOutputState { font-weight: 700; }
QLabel#keithleyMeterValue { color: #12354f; font-family: Consolas; font-size: 13pt; font-weight: 700; }
QLabel#keithleyComplianceClear { color: #087f5b; background: #dff5eb; border-radius: 6px; padding: 5px 8px; font-weight: 700; }
QLabel#keithleyComplianceActive { color: #b4233a; background: #fde7eb; border-radius: 6px; padding: 5px 8px; font-weight: 700; }
QTabWidget#keithleyControlTabs > QTabBar::tab { padding: 10px 15px; }
QFrame#anritsuProcessingCard { background: #ffffff; border: 1px solid #c8d3df; border-radius: 10px; padding: 9px; }
QLabel#anritsuLiveIndicator { color: #607284; background: #eef3f8; border: 1px solid #c8d3df; border-radius: 10px; padding: 5px 10px; font-size: 9pt; font-weight: 700; }
QLabel#anritsuLiveIndicator[liveState="on"] { color: #087f5b; background: #dff5eb; border-color: #9bd8bf; }
QLabel#anritsuLiveIndicator[liveState="starting"], QLabel#anritsuLiveIndicator[liveState="stopping"], QLabel#anritsuLiveIndicator[liveState="paused"] { color: #765500; background: #fff2c7; border-color: #e2c66f; }
QLabel#anritsuSgIndicator { color: #607284; background: #eef3f8; border: 1px solid #c8d3df; border-radius: 10px; padding: 6px 10px; font-weight: 700; }
QLabel#anritsuSgIndicator[liveState="off"] { color: #087f5b; background: #dff5eb; border-color: #9bd8bf; }
QLabel#anritsuSgIndicator[liveState="starting"] { color: #765500; background: #fff2c7; border-color: #e2c66f; }
QLabel#anritsuSgIndicator[liveState="on"] { color: #b4233a; background: #fde7eb; border-color: #d23b53; }
QWidget#anritsuControlPanel { background: #f8fafc; border-radius: 10px; }
QSplitter#anritsuWorkspaceSplitter::handle { background: #c8d3df; width: 5px; margin: 4px 1px; border-radius: 2px; }
QLabel#limitBadge { color: #174f7a; background: #e4f1fb; border: 1px solid #a9c9e2; border-radius: 6px; padding: 6px; font-family: Consolas; font-size: 9pt; }
QLabel#limitBadge[limitState="undefined"] { color: #765500; background: #fff2c7; border-color: #e2c66f; }
QLabel#limitBadge[keithleyCompact="true"] { padding: 4px; font-size: 8pt; }
QPushButton#limitEditButton { padding: 6px 8px; background: #d6e3ee; }
QPushButton#infoButton { background: #e2edf6; color: #1769aa; border: 1px solid #b5cadc; border-radius: 14px; font-size: 13pt; font-weight: 700; padding: 0; }
QPushButton#infoButton:hover { background: #d3e5f4; color: #0b5da7; }
QTabWidget#rigolControlTabs > QTabBar::tab { padding: 11px 16px; }
QTabWidget#rigolAdvancedTabs > QTabBar::tab { padding: 8px 12px; }
QScrollArea { border: 0; }
QScrollBar:vertical { background: #edf2f7; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #a9bac9; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #3488c8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #ffffff; color: #17212b; border: 1px solid #8ca0b3; padding: 6px; }
QFrame#notificationBanner { background: #fff2c7; border: 1px solid #e2c66f; border-radius: 6px; }
QFrame#notificationBanner[severity="error"] { background: #fde7eb; border-color: #d23b53; }
"""


def parse_args() -> argparse.Namespace:
    # Keep command-line help ASCII-only: many Windows VISA installations still
    # expose a legacy CP1252 console where Polish glyphs make argparse fail.
    parser = argparse.ArgumentParser(description="Local measurement-station control GUI")
    parser.add_argument("--settings", default=".config/settings.yml", help="path to station profile YAML")
    parser.add_argument("--simulate", action="store_true", help="use simulated VISA instruments; do not access hardware")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("Lab Control")
    window = MainWindow(Path(args.settings), simulation=args.simulate)
    def apply_theme(theme: str) -> None:
        app.setStyleSheet(LIGHT_STYLE if theme == "light" else STYLE)

    window.theme_changed.connect(apply_theme)
    window._set_theme_mode(str(window._settings.ui.get("theme", "system")), persist=False)
    app.styleHints().colorSchemeChanged.connect(lambda _scheme: window.refresh_system_theme())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
