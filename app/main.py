"""Desktop entry point for the safe local instrument-control application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.settings import repair_settings_file
from app.ui.shell import MainWindow


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
    app = QApplication(sys.argv)
    app.setApplicationName("PyLab")
    window = MainWindow(settings_path, simulation=args.simulate)

    window._set_theme_mode(str(window._settings.ui.get("theme", "system")), persist=False)
    app.styleHints().colorSchemeChanged.connect(lambda _scheme: window.refresh_system_theme())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
