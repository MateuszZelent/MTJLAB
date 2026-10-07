"""Fluent-themed base window for complex recipe editors."""

from __future__ import annotations

from app.ui.dialogs import StationDialog
from PySide6.QtWidgets import QDialog


class FluentRecipeDialog(StationDialog):
    """Backward-compatible semantic name for complex recipe editors."""

    _active_editors: dict[tuple[int, type], "FluentRecipeDialog"] = {}

    def __init__(self, parent=None, **kwargs):
        kwargs.setdefault("resizable", True)
        super().__init__(parent, **kwargs)

    def exec(self) -> int:
        # Reentrant activation (queued tree/keyboard events) must never stack
        # two editors for the same device over each other.
        key = (id(self.parent()), type(self))
        active = self._active_editors.get(key)
        if active is not None:
            active.raise_()
            active.activateWindow()
            return QDialog.DialogCode.Rejected
        self._active_editors[key] = self
        try:
            return super().exec()
        finally:
            self._active_editors.pop(key, None)
