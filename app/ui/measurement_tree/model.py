"""Qt model for the immutable semantic measurement tree.

The model is deliberately the only mutable presentation surface.  Recipe and
execution code update a semantic snapshot or a single operation state; they do
not manufacture or mutate thousands of QTreeWidgetItem instances.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Mapping

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QPersistentModelIndex, Qt

from app.domain.quantities import format_quantity_auto
from app.recipes.semantic_tree import SemanticMeasurementTree, SemanticNodeKind, SemanticTreeNode


class MeasurementTreeRole(IntEnum):
    SEMANTIC_ID = int(Qt.ItemDataRole.UserRole) + 1
    NODE_KIND = int(Qt.ItemDataRole.UserRole) + 2
    SOURCE_NODE_ID = int(Qt.ItemDataRole.UserRole) + 3
    AXIS_CONTEXT = int(Qt.ItemDataRole.UserRole) + 4
    EDITABLE = int(Qt.ItemDataRole.UserRole) + 5
    DRAGGABLE = int(Qt.ItemDataRole.UserRole) + 6
    EXECUTION_PHASE = int(Qt.ItemDataRole.UserRole) + 7
    REQUESTED_VALUE = int(Qt.ItemDataRole.UserRole) + 8
    APPLIED_VALUE = int(Qt.ItemDataRole.UserRole) + 9
    READBACK_VALUE = int(Qt.ItemDataRole.UserRole) + 10


class _NodeRef:
    __slots__ = ("node", "parent", "row")

    def __init__(self, node: SemanticTreeNode, parent: "_NodeRef | None", row: int) -> None:
        self.node = node
        self.parent = parent
        self.row = row


def _state_value(state: object, name: str, default: object = None) -> object:
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


class MeasurementTreeModel(QAbstractItemModel):
    COLUMN_COUNT = 4
    HEADERS = ("Measurement sequence", "Role / expansion", "Current value", "Status")

    def __init__(self, tree: SemanticMeasurementTree | None = None, parent=None) -> None:
        super().__init__(parent)
        self.tree = tree or SemanticMeasurementTree((), {}, source_text="")
        self._roots: tuple[_NodeRef, ...] = ()
        self._by_id: dict[str, _NodeRef] = {}
        self._states: dict[str, object] = {}
        self._read_only = False
        self._reindex()

    def _reindex(self) -> None:
        self._roots = tuple(_NodeRef(node, None, index) for index, node in enumerate(self.tree.roots))
        self._by_id = {}

        def visit(ref: _NodeRef) -> None:
            self._by_id[ref.node.semantic_id] = ref
            for row, child in enumerate(ref.node.children):
                child_ref = _NodeRef(child, ref, row)
                visit(child_ref)

        for root in self._roots:
            visit(root)

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = bool(read_only)

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if row < 0 or column < 0 or column >= self.COLUMN_COUNT:
            return QModelIndex()
        ref: _NodeRef | None
        if parent.isValid():
            parent_ref = parent.internalPointer()
            if not isinstance(parent_ref, _NodeRef) or row >= len(parent_ref.node.children):
                return QModelIndex()
            ref = self._by_id.get(parent_ref.node.children[row].semantic_id)
        else:
            if row >= len(self._roots):
                return QModelIndex()
            ref = self._roots[row]
        return self.createIndex(row, column, ref) if ref is not None else QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        ref = index.internalPointer()
        if not isinstance(ref, _NodeRef) or ref.parent is None:
            return QModelIndex()
        return self.createIndex(ref.parent.row, 0, ref.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid() and parent.column() > 0:
            return 0
        if not parent.isValid():
            return len(self._roots)
        ref = parent.internalPointer()
        return len(ref.node.children) if isinstance(ref, _NodeRef) else 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        del parent
        return self.COLUMN_COUNT

    def _state_for(self, semantic_id: str) -> object | None:
        return self._states.get(semantic_id)

    def _descendant_state(self, ref: _NodeRef) -> object | None:
        state = self._state_for(ref.node.semantic_id)
        if state is not None:
            return state
        for child in ref.node.children:
            child_ref = self._by_id.get(child.semantic_id)
            if child_ref is not None:
                state = self._descendant_state(child_ref)
                if state is not None:
                    return state
        return None

    def _value_text(self, ref: _NodeRef) -> str:
        state = self._descendant_state(ref)
        if state is None:
            return "—"
        applied = _state_value(state, "applied_si")
        requested = _state_value(state, "requested_si")
        readback = _state_value(state, "readback_si")
        axis = ref.node.axis
        dimension = axis.dimension if axis is not None else None
        if applied is not None and dimension:
            text = format_quantity_auto(float(applied), dimension)
            if readback is not None and readback != applied:
                text += f" · readback {format_quantity_auto(float(readback), dimension)}"
            return text
        if requested is not None and dimension:
            return f"requested {format_quantity_auto(float(requested), dimension)}"
        context = _state_value(state, "axis_context")
        if context is not None:
            value = _state_value(context, "value_si")
            if value is not None and dimension:
                return format_quantity_auto(float(value), dimension)
        return "—"

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if not index.isValid():
            return None
        ref = index.internalPointer()
        if not isinstance(ref, _NodeRef):
            return None
        node = ref.node
        state = self._descendant_state(ref)
        if role == int(Qt.ItemDataRole.DisplayRole):
            if index.column() == 0:
                return node.label
            if index.column() == 1:
                if node.kind is SemanticNodeKind.SWEEP_AXIS and node.axis is not None:
                    return f"{node.axis.target} · {len(node.axis.points)} point(s)"
                if node.kind is SemanticNodeKind.LOOP_BODY:
                    return str(node.data.get("point_count", "Executable child steps"))
                if node.kind is SemanticNodeKind.SET_ROI_VALUE:
                    return str(node.data.get("target", "Set ROI value"))
                return str(node.data.get("detail", node.kind.value.replace("_", " ").title()))
            if index.column() == 2:
                return self._value_text(ref)
            phase = _state_value(state, "phase") if state is not None else None
            if phase:
                return str(phase).upper()
            return {
                SemanticNodeKind.SWEEP_AXIS: "SWEEP",
                SemanticNodeKind.LOOP_BODY: "FLOW",
                SemanticNodeKind.SET_ROI_VALUE: "WAITING",
                SemanticNodeKind.FINALLY: "SAFE",
                SemanticNodeKind.GENERATED_SAFETY: "AUTO",
            }.get(node.kind, "READY")
        if role == int(Qt.ItemDataRole.ToolTipRole):
            target = node.axis.target if node.axis is not None else node.data.get("target", "")
            return f"{node.semantic_id}\n{target}" if target else node.semantic_id
        if role == int(Qt.ItemDataRole.ForegroundRole):
            phase = str(_state_value(state, "phase", "")) if state is not None else ""
            if phase == "applied":
                from PySide6.QtGui import QColor
                return QColor("#16a34a")
            if phase == "failed":
                from PySide6.QtGui import QColor
                return QColor("#dc2626")
        if role == int(MeasurementTreeRole.SEMANTIC_ID):
            return node.semantic_id
        if role == int(MeasurementTreeRole.NODE_KIND):
            return node.kind.value
        if role == int(MeasurementTreeRole.SOURCE_NODE_ID):
            return node.source_node_id
        if role == int(MeasurementTreeRole.AXIS_CONTEXT):
            return _state_value(state, "axis_context") if state is not None else None
        if role == int(MeasurementTreeRole.EDITABLE):
            return bool(node.editable and not self._read_only)
        if role == int(MeasurementTreeRole.DRAGGABLE):
            return bool(node.draggable and not self._read_only)
        if role == int(MeasurementTreeRole.EXECUTION_PHASE):
            return _state_value(state, "phase") if state is not None else None
        if role == int(MeasurementTreeRole.REQUESTED_VALUE):
            return _state_value(state, "requested_si") if state is not None else None
        if role == int(MeasurementTreeRole.APPLIED_VALUE):
            return _state_value(state, "applied_si") if state is not None else None
        if role == int(MeasurementTreeRole.READBACK_VALUE):
            return _state_value(state, "readback_si") if state is not None else None
        if role == int(Qt.ItemDataRole.FontRole) and index.column() == 0:
            from PySide6.QtGui import QFont
            font = QFont()
            if node.kind in {SemanticNodeKind.SWEEP_AXIS, SemanticNodeKind.SET_ROI_VALUE}:
                font.setWeight(QFont.Weight.DemiBold)
            return font
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        ref = index.internalPointer()
        if not isinstance(ref, _NodeRef):
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if ref.node.editable and not self._read_only:
            flags |= Qt.ItemFlag.ItemIsEditable
        if ref.node.draggable and not self._read_only:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        if ref.node.children and not self._read_only:
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == int(Qt.ItemDataRole.DisplayRole):
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return None

    def index_for_semantic_id(self, semantic_id: str) -> QModelIndex:
        ref = self._by_id.get(semantic_id)
        if ref is None:
            return QModelIndex()
        return self.createIndex(ref.row, 0, ref)

    def replace_tree(self, tree: SemanticMeasurementTree) -> None:
        if not isinstance(tree, SemanticMeasurementTree):
            raise TypeError("MeasurementTreeModel requires a SemanticMeasurementTree.")
        self.beginResetModel()
        self.tree = tree
        self._states.clear()
        self._reindex()
        self.endResetModel()

    def apply_state(self, state: object) -> bool:
        semantic_id = _state_value(state, "semantic_id")
        if not isinstance(semantic_id, str) or semantic_id not in self._by_id:
            return False
        self._states[semantic_id] = state
        index = self.index_for_semantic_id(semantic_id)
        if index.isValid():
            self.dataChanged.emit(index, self.index(index.row(), self.COLUMN_COUNT - 1, index.parent()))
        return True

    def state_for(self, semantic_id: str) -> object | None:
        return self._states.get(semantic_id)

    def value_for(self, semantic_id: str) -> str:
        ref = self._by_id.get(semantic_id)
        return self._value_text(ref) if ref is not None else "—"
