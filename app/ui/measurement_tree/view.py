"""Fluent TreeView host with bounded active-row following."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import time

from PySide6.QtCore import QMimeData, QSize, QTimer, Signal, Qt
from PySide6.QtGui import QDrag, QDragMoveEvent, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QHeaderView
from qfluentwidgets import TreeView

from app.ui.measurement_tree.model import MeasurementTreeModel, MeasurementTreeRole
from app.ui.measurement_tree.delegate import MeasurementTreeDelegate
from app.ui.design_system.tokens import tokens_for
from qfluentwidgets import isDarkTheme


class TreeInteractionMode(StrEnum):
    EDITABLE = "editable"
    READ_ONLY = "read_only"


class TreeDropPlacement(StrEnum):
    """Explicit location of a semantic-tree move request."""

    BEFORE = "before"
    AFTER = "after"
    INSIDE = "inside"
    ROOT_END = "root_end"


@dataclass(slots=True)
class MeasurementTreeMoveRequest:
    """A synchronous request to move a recipe-backed semantic node.

    The view never mutates the immutable semantic model.  The owning page must
    validate and commit a replacement YAML document before marking this request
    accepted, otherwise Qt rejects the drag transaction.
    """

    source_semantic_id: str
    destination_semantic_id: str
    placement: TreeDropPlacement
    accepted: bool = False


@dataclass(slots=True)
class MeasurementTreeLibraryDropRequest:
    """Request to insert a library block at a semantic-tree destination."""

    drag_kind: str
    destination_semantic_id: str
    placement: TreeDropPlacement
    accepted: bool = False


class MeasurementTreeView(TreeView):
    semantic_activated = Signal(str)
    semantic_selected = Signal(str)
    semantic_context_requested = Signal(str, object)
    move_requested = Signal(object)
    library_drop_requested = Signal(object)
    drag_status_changed = Signal(str, bool)
    _semantic_mime_type = "application/x-mtjlab-semantic-measurement-node"
    _library_mime_type = "application/x-lab-control-sweep-block"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setItemDelegate(MeasurementTreeDelegate(self))
        self.setAlternatingRowColors(False)
        self.setUniformRowHeights(True)
        self.setIndentation(24)
        self.setIconSize(QSize(18, 18))
        self.setRootIsDecorated(True)
        self.setItemsExpandable(True)
        self.setExpandsOnDoubleClick(False)
        # Recipe rows are semantic projections, not an inline text editor.
        # Their labels are derived from the authored YAML and must only be
        # changed through the type-specific modal (ROI, device, WAIT, action,
        # ...).  QTreeView's default edit triggers include a double click and
        # can also include a selected click depending on the platform style;
        # leaving them enabled exposes a misleading line editor which cannot
        # commit a safe recipe transaction.
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(self.SelectionBehavior.SelectRows)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self._interaction_mode = TreeInteractionMode.EDITABLE
        self._last_follow_s = 0.0
        self._pending_follow_semantic_id: str | None = None
        self._follow_timer = QTimer(self)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(0)
        self._follow_timer.timeout.connect(self._flush_pending_follow)
        self._dragged_semantic_id: str | None = None
        self._drop_target: tuple[str, TreeDropPlacement] | None = None
        self._selection_model = None
        self.doubleClicked.connect(self._emit_semantic_activation)
        self.customContextMenuRequested.connect(self._emit_semantic_context_menu)

    def _emit_semantic_activation(self, index) -> None:
        model = self.tree_model
        if model is None or not index.isValid():
            return
        semantic_id = model.data(index, MeasurementTreeRole.SEMANTIC_ID)
        if isinstance(semantic_id, str):
            self.semantic_activated.emit(semantic_id)

    @property
    def tree_model(self) -> MeasurementTreeModel | None:
        model = self.model()
        return model if isinstance(model, MeasurementTreeModel) else None

    def setModel(self, model) -> None:  # noqa: N802
        if self._selection_model is not None:
            self._selection_model.currentChanged.disconnect(self._emit_semantic_selection)
        super().setModel(model)
        if isinstance(model, MeasurementTreeModel):
            model.set_read_only(self._interaction_mode is TreeInteractionMode.READ_ONLY)
            model.modelReset.connect(self._expand_all_after_reset)
        self._selection_model = self.selectionModel()
        if self._selection_model is not None:
            self._selection_model.currentChanged.connect(self._emit_semantic_selection)
        # Keep all semantic columns available in the default pane.  Fixed
        # 420/310 px columns forced the progress and state columns behind a
        # horizontal scrollbar on ordinary desktop widths, making the active
        # operation look incomplete.  The Fluent header now gives labels the
        # remaining space while keeping progress/state compact and readable.
        header = self.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(58)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Progress and state change at sweep cadence.  ResizeToContents here
        # would rescan the model on every dataChanged() and was the remaining
        # source of multi-hundred-millisecond GUI gaps on 1000-point runs.
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(2, 92)
        header.resizeSection(3, 96)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.expandAll()
        QTimer.singleShot(0, self.expandAll)

    def _expand_all_after_reset(self) -> None:
        """Expand now and once more after Qt has recalculated model geometry."""

        self.expandAll()
        QTimer.singleShot(0, self.expandAll)

    def set_interaction_mode(self, mode: TreeInteractionMode) -> None:
        self._interaction_mode = TreeInteractionMode(mode)
        model = self.tree_model
        if model is not None:
            model.set_read_only(self._interaction_mode is TreeInteractionMode.READ_ONLY)
            model.layoutChanged.emit()
        editable = self._interaction_mode is TreeInteractionMode.EDITABLE
        self.setDragEnabled(editable)
        self.setAcceptDrops(editable)
        self.setDropIndicatorShown(editable)

    def _emit_semantic_selection(self, current, _previous) -> None:
        model = self.tree_model
        if model is None or not current.isValid():
            return
        semantic_id = model.data(current, MeasurementTreeRole.SEMANTIC_ID)
        if isinstance(semantic_id, str):
            self.semantic_selected.emit(semantic_id)

    def _emit_semantic_context_menu(self, position) -> None:
        model = self.tree_model
        index = self.indexAt(position)
        if model is None or not index.isValid():
            return
        semantic_id = model.data(index, MeasurementTreeRole.SEMANTIC_ID)
        if isinstance(semantic_id, str):
            self.setCurrentIndex(index)
            self.semantic_context_requested.emit(
                semantic_id, self.viewport().mapToGlobal(position)
            )

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        if self._interaction_mode is TreeInteractionMode.READ_ONLY:
            return
        model = self.tree_model
        index = self.currentIndex()
        if model is None or not index.isValid() or not index.parent().isValid():
            return
        semantic_id = model.data(index, MeasurementTreeRole.SEMANTIC_ID)
        draggable = model.data(index, MeasurementTreeRole.DRAGGABLE)
        if not isinstance(semantic_id, str) or not draggable:
            return
        mime = QMimeData()
        mime.setData(self._semantic_mime_type, semantic_id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        self._dragged_semantic_id = semantic_id
        try:
            actions = supported_actions & Qt.DropAction.MoveAction
            if actions:
                drag.exec(actions)
        finally:
            self._dragged_semantic_id = None
            self._drop_target = None
            self.drag_status_changed.emit("", True)
            self.viewport().update()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if (
            self._interaction_mode is TreeInteractionMode.EDITABLE
            and (
                (event.source() is self and event.mimeData().hasFormat(self._semantic_mime_type))
                or event.mimeData().hasFormat(self._library_mime_type)
            )
        ):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        self._drop_target = None
        self.viewport().update()
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._interaction_mode is TreeInteractionMode.READ_ONLY:
            self._drop_target = None
            self.viewport().update()
            event.ignore()
            return
        library_drag = event.mimeData().hasFormat(self._library_mime_type)
        if not library_drag and (event.source() is not self or self._dragged_semantic_id is None):
            self._drop_target = None
            self.viewport().update()
            event.ignore()
            return
        destination = self._drop_destination(event.position().toPoint())
        if destination is None:
            self._drop_target = None
            self.viewport().update()
            self.drag_status_changed.emit(
                "Drop before, after, or inside a recipe block.", False
            )
            event.ignore()
            return
        target_id, placement = destination
        verb = "Insert block" if library_drag else "Move block"
        self.drag_status_changed.emit(
            f"{verb} {placement.value.replace('_', ' ')} {target_id}.", True
        )
        self._drop_target = destination
        self.viewport().update()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        # Let QTreeView update its native drop-indicator bookkeeping as well.
        # The semantic model has no ``dropMimeData`` mutation path, so the
        # actual commit still happens exclusively through ``dropEvent``.
        if isinstance(event, QDragMoveEvent):
            super().dragMoveEvent(event)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        if self._interaction_mode is TreeInteractionMode.READ_ONLY:
            self._drop_target = None
            self.viewport().update()
            event.ignore()
            return
        destination = self._drop_destination(event.position().toPoint())
        if event.mimeData().hasFormat(self._library_mime_type):
            if destination is None:
                self._drop_target = None
                self.viewport().update()
                self.drag_status_changed.emit("", True)
                event.ignore()
                return
            try:
                drag_kind = bytes(event.mimeData().data(self._library_mime_type)).decode(
                    "utf-8"
                ).strip()
            except UnicodeDecodeError:
                drag_kind = ""
            if not drag_kind:
                self._drop_target = None
                self.viewport().update()
                self.drag_status_changed.emit("Invalid library block payload.", False)
                event.ignore()
                return
            target_id, placement = destination
            request = MeasurementTreeLibraryDropRequest(
                drag_kind, target_id, placement
            )
            self.library_drop_requested.emit(request)
            self.drag_status_changed.emit("", True)
            self._drop_target = None
            self.viewport().update()
            if request.accepted:
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        if (
            event.source() is not self
            or self._dragged_semantic_id is None
            or destination is None
        ):
            self._drop_target = None
            self.viewport().update()
            self.drag_status_changed.emit("", True)
            event.ignore()
            return
        target_id, placement = destination
        request = MeasurementTreeMoveRequest(
            self._dragged_semantic_id, target_id, placement
        )
        self.move_requested.emit(request)
        self.drag_status_changed.emit("", True)
        self._drop_target = None
        self.viewport().update()
        if not request.accepted:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_target = None
        self.viewport().update()
        self.drag_status_changed.emit("", True)
        super().dragLeaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        """Paint a deterministic Fluent drop cue over the native indicator.

        QFluentWidgets delegates the row rendering, while this cue makes the
        three insertion modes visible even on platforms whose native tree
        indicator is not themed (notably the offscreen test backend).
        """

        super().paintEvent(event)
        target = self._drop_target
        model = self.tree_model
        if target is None or model is None:
            return
        semantic_id, placement = target
        index = model.index_for_semantic_id(semantic_id)
        if not index.isValid():
            return
        rect = self.visualRect(index)
        if not rect.isValid():
            return
        painter = QPainter(self.viewport())
        try:
            color = tokens_for("dark" if isDarkTheme() else "light").accent
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            left = max(2, rect.left() + 2)
            right = max(left + 1, self.viewport().rect().right() - 2)
            if placement is TreeDropPlacement.BEFORE:
                painter.drawLine(left, rect.top(), right, rect.top())
            elif placement is TreeDropPlacement.AFTER:
                painter.drawLine(left, rect.bottom(), right, rect.bottom())
            elif placement is TreeDropPlacement.INSIDE:
                painter.drawRoundedRect(rect.adjusted(1, 1, -2, -2), 4, 4)
            else:
                painter.drawLine(left, rect.bottom(), right, rect.bottom())
        finally:
            painter.end()

    def _drop_destination(self, position) -> tuple[str, TreeDropPlacement] | None:
        model = self.tree_model
        if model is None:
            return None
        index = self.indexAt(position)
        if not index.isValid():
            root = model.index(0, 0)
            semantic_id = model.data(root, MeasurementTreeRole.SEMANTIC_ID)
            return (
                (semantic_id, TreeDropPlacement.ROOT_END)
                if isinstance(semantic_id, str)
                else None
            )
        semantic_id = model.data(index, MeasurementTreeRole.SEMANTIC_ID)
        if not isinstance(semantic_id, str):
            return None
        rect = self.visualRect(index)
        if position.y() < rect.top() + rect.height() / 3:
            placement = TreeDropPlacement.BEFORE
        elif position.y() > rect.bottom() - rect.height() / 3:
            placement = TreeDropPlacement.AFTER
        else:
            accepts_children = bool(
                model.data(index, MeasurementTreeRole.DROP_TARGET)
            )
            if accepts_children:
                placement = TreeDropPlacement.INSIDE
            else:
                # A leaf has no inside target.  Split its middle band into
                # deterministic before/after halves so the feedback matches
                # the eventual recipe transaction.
                placement = (
                    TreeDropPlacement.BEFORE
                    if position.y() < rect.center().y()
                    else TreeDropPlacement.AFTER
                )
        return semantic_id, placement

    def follow_semantic_id(self, semantic_id: str, *, force: bool = False) -> None:
        model = self.tree_model
        if model is None:
            return
        index = model.index_for_semantic_id(semantic_id)
        if not index.isValid():
            return
        node_kind = model.data(index, MeasurementTreeRole.NODE_KIND)
        is_axis_anchor = node_kind == "set_roi_value"
        now = time.monotonic()
        if not force and now - self._last_follow_s < 0.1:
            return
        self._last_follow_s = now
        # Ensure a nested same-device/multi-device point is reachable without
        # collapsing or rebuilding the rest of the tree.  Expanding only the
        # immediate parent leaves an inner operation hidden behind a collapsed
        # outer axis.
        needs_deferred_reveal = False
        ancestor = index.parent()
        while ancestor.isValid():
            if not self.isExpanded(ancestor):
                self.expand(ancestor)
                needs_deferred_reveal = True
            ancestor = ancestor.parent()
        # Builder selection is operator-owned. Execution is read-only and its
        # active highlight comes from semantic state; moving Qt's selection on
        # every action would repaint two full rows at the sweep cadence.
        rect = self.visualRect(index)
        if not rect.isValid() or not self.viewport().rect().contains(rect):
            needs_deferred_reveal = True
        if (
            (
                self._interaction_mode is TreeInteractionMode.EDITABLE
                or is_axis_anchor
                or needs_deferred_reveal
            )
            and self.currentIndex() != index
        ):
            self.setCurrentIndex(index)
        if needs_deferred_reveal:
            self.scrollTo(index, self.ScrollHint.PositionAtCenter)
        # Expanding a deep branch schedules geometry work inside QTreeView.
        # Repeat the reveal after that layout pass; without it a valid current
        # index can still leave the active row below the visible viewport.
        if needs_deferred_reveal:
            self._pending_follow_semantic_id = semantic_id
            if not self._follow_timer.isActive():
                self._follow_timer.start()

    def _flush_pending_follow(self) -> None:
        semantic_id = self._pending_follow_semantic_id
        self._pending_follow_semantic_id = None
        if semantic_id is not None:
            self._reveal_semantic_id(semantic_id)

    def _reveal_semantic_id(self, semantic_id: str) -> None:
        model = self.tree_model
        if model is None:
            return
        index = model.index_for_semantic_id(semantic_id)
        if not index.isValid():
            return
        node_kind = model.data(index, MeasurementTreeRole.NODE_KIND)
        ancestor = index.parent()
        while ancestor.isValid():
            self.expand(ancestor)
            ancestor = ancestor.parent()
        rect = self.visualRect(index)
        if not rect.isValid() or not self.viewport().rect().contains(rect):
            if self.currentIndex() != index:
                self.setCurrentIndex(index)
            self.scrollTo(index, self.ScrollHint.PositionAtCenter)
        elif node_kind == "set_roi_value" and self.currentIndex() != index:
            # Keep one stable ROI anchor selected while acquisition/wait rows
            # animate through semantic state. Repeated points reuse this index.
            self.setCurrentIndex(index)
