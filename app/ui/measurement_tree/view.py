"""Fluent TreeView host with bounded active-row following."""

from __future__ import annotations

from enum import StrEnum
import time

from PySide6.QtCore import QPoint, Signal, Qt
from qfluentwidgets import TreeView

from app.ui.measurement_tree.model import MeasurementTreeModel
from app.ui.measurement_tree.delegate import MeasurementTreeDelegate


class TreeInteractionMode(StrEnum):
    EDITABLE = "editable"
    READ_ONLY = "read_only"


class MeasurementTreeView(TreeView):
    semantic_activated = Signal(str)
    move_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setItemDelegate(MeasurementTreeDelegate(self))
        self.setAlternatingRowColors(False)
        self.setUniformRowHeights(True)
        self.setRootIsDecorated(True)
        self.setItemsExpandable(True)
        self.setExpandsOnDoubleClick(False)
        self.setSelectionBehavior(self.SelectionBehavior.SelectRows)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._interaction_mode = TreeInteractionMode.EDITABLE
        self._last_follow_s = 0.0

    @property
    def tree_model(self) -> MeasurementTreeModel | None:
        model = self.model()
        return model if isinstance(model, MeasurementTreeModel) else None

    def setModel(self, model) -> None:  # noqa: N802
        super().setModel(model)
        if isinstance(model, MeasurementTreeModel):
            model.set_read_only(self._interaction_mode is TreeInteractionMode.READ_ONLY)
        self.setColumnWidth(0, 300)
        self.setColumnWidth(1, 350)
        self.setColumnWidth(2, 210)
        self.setColumnWidth(3, 120)

    def set_interaction_mode(self, mode: TreeInteractionMode) -> None:
        self._interaction_mode = TreeInteractionMode(mode)
        model = self.tree_model
        if model is not None:
            model.set_read_only(self._interaction_mode is TreeInteractionMode.READ_ONLY)
            model.layoutChanged.emit()

    def follow_semantic_id(self, semantic_id: str, *, force: bool = False) -> None:
        model = self.tree_model
        if model is None:
            return
        index = model.index_for_semantic_id(semantic_id)
        if not index.isValid():
            return
        now = time.monotonic()
        if not force and now - self._last_follow_s < 0.1:
            return
        self._last_follow_s = now
        self.expand(index.parent())
        self.setCurrentIndex(index)
        self.scrollTo(index, self.ScrollHint.EnsureVisible)
