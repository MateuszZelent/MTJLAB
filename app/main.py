"""Desktop entry point for the safe local instrument-control application."""

from __future__ import annotations

import argparse
import faulthandler
import os
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from app.settings import repair_settings_file
from app.ui.design_system.fluent_theme import configure_widget_style
from app.ui.shell import MainWindow
from app.version import APP_NAME


def _enable_native_fault_log(audit_path: Path) -> None:
    """Record fatal native faults in the file paired with this audit session."""
    try:
        descriptor = os.open(
            audit_path.with_suffix(".fault.log"),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except OSError:
        return
    try:
        faulthandler.enable(file=descriptor, all_threads=True)
    except (OSError, RuntimeError):
        os.close(descriptor)
    # The process keeps the descriptor open through Qt and Python finalization.


def parse_args() -> argparse.Namespace:
    # Keep command-line help ASCII-only: many Windows VISA installations still
    # expose a legacy CP1252 console where Polish glyphs make argparse fail.
    parser = argparse.ArgumentParser(description="Local measurement-station control GUI")
    parser.add_argument("--settings", default=".config/settings.yml", help="path to station profile YAML")
    parser.add_argument("--simulate", action="store_true", help="use simulated VISA instruments; do not access hardware")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings_path = Path(args.settings)
    repair_settings_file(settings_path)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication(sys.argv)
    configure_widget_style(app)
    app.setApplicationName(APP_NAME)
    window = MainWindow(settings_path, simulation=args.simulate)
    _enable_native_fault_log(window._audit.path)

    window._set_theme_mode(str(window._settings.ui.get("theme", "system")), persist=False)
    app.styleHints().colorSchemeChanged.connect(window.refresh_system_theme)
    window.show()
    try:
        return app.exec()
    finally:
        if isValid(window) and window._closed:
            window.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    raise SystemExit(main())
