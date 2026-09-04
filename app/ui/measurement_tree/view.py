"""Fluent TreeView host with bounded active-row following."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import time

from PySide6.QtCore import (
    QEvent,
    QMimeData,
    QModelIndex,
    QRect,
    QSize,
    QTimer,
    Signal,
    Qt,
)
from PySide6.QtGui import (
    QDrag,
    QDragMoveEvent,
    QPainter,
    QPen,
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeView
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
    source_semantic_ids: tuple[str, ...] = ()
    accepted: bool = False

    def __post_init__(self) -> None:
        if not self.source_semantic_ids and self.source_semantic_id:
            object.__setattr__(self, "source_semantic_ids", (self.source_semantic_id,))


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
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self._interaction_mode = TreeInteractionMode.EDITABLE
        self._last_follow_s = 0.0
        self._pending_follow_semantic_id: str | None = None
        self._follow_timer = QTimer(self)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(0)
        self._follow_timer.timeout.connect(self._flush_pending_follow)
        self._dragged_semantic_ids: tuple[str, ...] = ()
        self._dragged_semantic_id: str | None = None
        self._drop_target: tuple[str, TreeDropPlacement] | None = None
        self._selection_model = None
        self._user_resized_columns = False
        self._user_column_widths: dict[int, int] = {}
        self._updating_column_widths = False
        header = self.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(50)
        header.setSectionsMovable(False)
        header.sectionResized.connect(self._on_section_resized)
        self.clicked.connect(self._on_item_clicked)
        self.doubleClicked.connect(self._emit_semantic_activation)
        self.customContextMenuRequested.connect(self._emit_semantic_context_menu)

    def _on_section_resized(
        self, logical_index: int, _old_size: int, new_size: int
    ) -> None:
        if self._updating_column_widths:
            return
        self._user_resized_columns = True
        header = self.header()
        for col in range(header.count()):
            self._user_column_widths[col] = header.sectionSize(col)
        self._user_column_widths[logical_index] = new_size

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override
        super().wheelEvent(event)
        # Prevent wheel event bubbling to the global application shell scroll area:
        event.accept()

    def _apply_column_widths(self) -> None:
        header = self.header()
        if header.count() < 4:
            return

        if self._user_resized_columns:
            self._updating_column_widths = True
            try:
                for col, width in self._user_column_widths.items():
                    if col < header.count():
                        header.resizeSection(col, width)
            finally:
                self._updating_column_widths = False
            return

        available = self.viewport().width()
        if available <= 0:
            available = self.width()
        if available <= 0:
            available = 720

        progress_w = 92
        state_w = 96
        remaining = max(200, available - progress_w - state_w)
        op_w = max(140, int(remaining * 0.55))
        val_w = max(100, remaining - op_w)

        self._updating_column_widths = True
        try:
            header.resizeSection(0, op_w)
            header.resizeSection(1, val_w)
            header.resizeSection(2, progress_w)
            header.resizeSection(3, state_w)
        finally:
            self._updating_column_widths = False

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if not self._user_resized_columns:
            self._apply_column_widths()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._user_resized_columns:
            QTimer.singleShot(0, self._apply_column_widths)

    def drawBranches(
        self, painter: QPainter, rect: QRect, index: QModelIndex
    ) -> None:  # noqa: N802 - Qt override
        branch_rect = QRect(rect)
        vrect_left = self.visualRect(index).left()
        if vrect_left > 0:
            # Keep branch drawing strictly in the branch gutter to the left of the cell,
            # so it never collides with the semantic accent bar at vrect.left() + 4.
            branch_rect.setRight(vrect_left)
            if branch_rect.left() == 0 and vrect_left - branch_rect.left() >= 20:
                branch_rect.setLeft(2)
        return QTreeView.drawBranches(self, painter, branch_rect, index)

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802 - Qt override
        """Handle branch expand/collapse clicks in their true layout gutter without colliding with the item cell."""
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            pos = (
                event.position().toPoint()
                if hasattr(event, "position")
                else event.pos()
            )
            index = self.indexAt(pos)
            if (
                index.isValid()
                and self.model() is not None
                and self.model().hasChildren(index)
            ):
                level = 0
                parent = index.parent()
                while parent.isValid():
                    level += 1
                    parent = parent.parent()
                branch_left = level * self.indentation()
                branch_right = (level + 1) * self.indentation()
                if branch_left <= pos.x() < branch_right:
                    if self.isExpanded(index):
                        self.collapse(index)
                    else:
                        self.expand(index)
                    return True
        return QTreeView.viewportEvent(self, event)

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
            try:
                self._selection_model.currentChanged.disconnect(self._emit_semantic_selection)
                self._selection_model.selectionChanged.disconnect(self._on_selection_changed)
            except Exception:
                pass
        super().setModel(model)
        if isinstance(model, MeasurementTreeModel):
            model.set_read_only(self._interaction_mode is TreeInteractionMode.READ_ONLY)
            model.modelReset.connect(self._expand_all_after_reset)
        self._selection_model = self.selectionModel()
        if self._selection_model is not None:
            self._selection_model.currentChanged.connect(self._emit_semantic_selection)
            self._selection_model.selectionChanged.connect(self._on_selection_changed)
        header = self.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(50)
        header.setSectionsMovable(False)
        for i in range(4):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self._apply_column_widths()
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

    def _emit_semantic_selection(self, current, _previous=None) -> None:
        model = self.tree_model
        if model is None:
            return
        if not current.isValid():
            self.semantic_selected.emit("")
            return
        semantic_id = model.data(current, MeasurementTreeRole.SEMANTIC_ID)
        if isinstance(semantic_id, str):
            self.semantic_selected.emit(semantic_id)
        else:
            self.semantic_selected.emit("")

    def _on_item_clicked(self, index) -> None:
        if not index.isValid():
            self.semantic_selected.emit("")
            return
        model = self.tree_model
        if model is None:
            return
        semantic_id = model.data(index, MeasurementTreeRole.SEMANTIC_ID)
        if isinstance(semantic_id, str):
            self.semantic_selected.emit(semantic_id)

    def _on_selection_changed(self, _selected=None, _deselected=None) -> None:
        if self._selection_model is not None and not self._selection_model.hasSelection():
            self.semantic_selected.emit("")
        else:
            selected_ids = self.selected_semantic_ids()
            if selected_ids:
                curr = self.currentIndex()
                if curr.isValid() and self.tree_model is not None:
                    sid = self.tree_model.data(curr, MeasurementTreeRole.SEMANTIC_ID)
                    if isinstance(sid, str) and sid in selected_ids:
                        self.semantic_selected.emit(sid)
                        return
                self.semantic_selected.emit(selected_ids[0])

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

    def selected_semantic_ids(self) -> list[str]:
        """Return distinct selected semantic IDs in visual row order."""
        model = self.tree_model
        if model is None or self._selection_model is None:
            return []
        ids: list[str] = []
        seen: set[tuple[int, int]] = set()
        for index in self._selection_model.selectedIndexes():
            row_key = (index.row(), index.internalId() if hasattr(index, "internalId") else id(index.internalPointer()))
            if row_key in seen:
                continue
            seen.add(row_key)
            col0 = index.siblingAtColumn(0)
            sid = model.data(col0, MeasurementTreeRole.SEMANTIC_ID)
            if isinstance(sid, str) and sid and sid not in ids:
                ids.append(sid)
        return ids

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        if self._interaction_mode is TreeInteractionMode.READ_ONLY:
            return
        model = self.tree_model
        if model is None:
            return
        selected_indexes: list[QModelIndex] = []
        if self._selection_model is not None:
            seen: set[tuple[int, int]] = set()
            for idx in self._selection_model.selectedIndexes():
                row_key = (idx.row(), idx.internalId() if hasattr(idx, "internalId") else id(idx.internalPointer()))
                if row_key not in seen:
                    seen.add(row_key)
                    selected_indexes.append(idx.siblingAtColumn(0))
        if not selected_indexes:
            curr = self.currentIndex()
            if curr.isValid():
                selected_indexes = [curr.siblingAtColumn(0)]

        valid_semantic_ids: list[str] = []
        for index in selected_indexes:
            if not index.isValid() or not index.parent().isValid():
                continue
            semantic_id = model.data(index, MeasurementTreeRole.SEMANTIC_ID)
            draggable = model.data(index, MeasurementTreeRole.DRAGGABLE)
            if isinstance(semantic_id, str) and draggable and semantic_id not in valid_semantic_ids:
                valid_semantic_ids.append(semantic_id)

        if not valid_semantic_ids:
            return

        mime = QMimeData()
        payload = json.dumps(valid_semantic_ids)
        mime.setData(self._semantic_mime_type, payload.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        self._dragged_semantic_ids = tuple(valid_semantic_ids)
        self._dragged_semantic_id = valid_semantic_ids[0]
        try:
            actions = (supported_actions & Qt.DropAction.MoveAction) or Qt.DropAction.MoveAction
            drag.exec(actions)
        finally:
            self._dragged_semantic_ids = ()
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
        if not library_drag:
            if event.source() is not self:
                self._drop_target = None
                self.viewport().update()
                event.ignore()
                return
            if not self._dragged_semantic_ids and self._dragged_semantic_id:
                self._dragged_semantic_ids = (self._dragged_semantic_id,)
            elif not self._dragged_semantic_ids and event.mimeData().hasFormat(self._semantic_mime_type):
                try:
                    raw_data = bytes(event.mimeData().data(self._semantic_mime_type)).decode("utf-8").strip()
                    if raw_data.startswith("["):
                        self._dragged_semantic_ids = tuple(json.loads(raw_data))
                    elif raw_data:
                        self._dragged_semantic_ids = (raw_data,)
                except Exception:
                    self._dragged_semantic_ids = ()
            if not self._dragged_semantic_ids:
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
        count = len(self._dragged_semantic_ids)
        verb = "Insert block" if library_drag else (
            f"Move {count} blocks" if count > 1 else "Move block"
        )
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

        source_ids = self._dragged_semantic_ids
        if not source_ids and self._dragged_semantic_id:
            source_ids = (self._dragged_semantic_id,)
        elif not source_ids and event.mimeData().hasFormat(self._semantic_mime_type):
            try:
                raw_data = bytes(event.mimeData().data(self._semantic_mime_type)).decode("utf-8").strip()
                if raw_data.startswith("["):
                    source_ids = tuple(json.loads(raw_data))
                elif raw_data:
                    source_ids = (raw_data,)
            except Exception:
                source_ids = ()

        if (
            event.source() is not self
            or not source_ids
            or destination is None
        ):
            self._drop_target = None
            self.viewport().update()
            self.drag_status_changed.emit("", True)
            event.ignore()
            return
        target_id, placement = destination
        request = MeasurementTreeMoveRequest(
            source_ids[0],
            target_id,
            placement,
            source_semantic_ids=source_ids,
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
