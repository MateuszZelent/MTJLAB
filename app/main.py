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
QTabBar::tab { background: #18212c; color: #91a0b2; padding: 10px 18px; margin-right: 2px; }
QTabBar::tab:selected { background: #26384a; color: #e8edf3; }
QLineEdit, QPlainTextEdit, QTreeWidget, QComboBox, QSpinBox { background: #18212c; border: 1px solid #344659; border-radius: 4px; padding: 6px; color: #e8edf3; }
QPushButton { background: #26384a; border: 0; border-radius: 4px; padding: 8px 12px; color: #e8edf3; font-weight: 600; }
QPushButton:hover { background: #34516a; }
QPushButton#emergencyButton { background: #8f2638; color: white; }
QPushButton#emergencyButton:hover { background: #b7334b; }
QFrame#deviceCard { background: #18212c; border: 1px solid #344659; border-radius: 8px; padding: 10px; min-height: 170px; }
QLabel#pageTitle { font-size: 20pt; font-weight: 700; }
QLabel#cardTitle { font-size: 14pt; font-weight: 700; }
QLabel#muted { color: #91a0b2; }
QLabel#readout { font-family: Consolas; font-size: 13pt; padding: 10px; background: #18212c; }
QLabel#stateDisconnected { color: #91a0b2; font-weight: 700; }
QLabel#stateVerified, QLabel#stateOutputOff { color: #38d996; font-weight: 700; }
QLabel#stateOutputOn, QLabel#stateCompliance { color: #ffcc66; font-weight: 700; }
QLabel#stateFault, QLabel#stateUnknown { color: #ff657a; font-weight: 700; }
QLabel#checklist { background: #18212c; border-radius: 8px; padding: 14px; }
QHeaderView::section { background: #26384a; color: #e8edf3; padding: 6px; border: 0; }
"""


def parse_args() -> argparse.Namespace:
    # Keep command-line help ASCII-only: many Windows VISA installations still
    # expose a legacy CP1252 console where Polish glyphs make argparse fail.
    parser = argparse.ArgumentParser(description="Local measurement-station control GUI")
    parser.add_argument("--settings", default=".config/settings.yml", help="path to station profile YAML")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("Lab Control")
    app.setStyleSheet(STYLE)
    window = MainWindow(Path(args.settings))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
