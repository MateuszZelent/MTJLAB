"""PySide6 desktop user interface for the local measurement station.

The shell is imported lazily so a device-owned UI module can safely import a
shared widget without pulling the complete application window back in.
"""

from typing import Any

__all__ = ["MainWindow"]


def __getattr__(name: str) -> Any:
    if name == "MainWindow":
        from app.ui.main_window import MainWindow

        return MainWindow
    raise AttributeError(name)
