"""Qt model for the immutable semantic measurement tree.

The model is deliberately the only mutable presentation surface. Recipe and
execution code replace a semantic snapshot at a document boundary or update a
single operation state while a run is active.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum
from typing import Any, Mapping

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from qfluentwidgets import FluentIcon, isDarkTheme

from app.domain.quantities import format_quantity_auto
from app.recipes.semantic_tree import SemanticMeasurementTree, SemanticNodeKind, SemanticTreeNode
from app.ui.design_system.tokens import tokens_for


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
    ACCENT_COLOR = int(Qt.ItemDataRole.UserRole) + 11
    DROP_TARGET = int(Qt.ItemDataRole.UserRole) + 12


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
    unknown_semantic_state = Signal(str)

    COLUMN_COUNT = 4
    HEADERS = ("Operation", "Configured / active value", "Progress", "State")

    def __init__(
        self,
        tree: SemanticMeasurementTree | None = None,
        parent=None,
        *,
        states: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.tree = tree or SemanticMeasurementTree((), {}, source_text="")
        self._roots: tuple[_NodeRef, ...] = ()
        self._by_id: dict[str, _NodeRef] = {}
        self._states: dict[str, object] = dict(states or {})
        self._reported_unknown_ids: set[str] = set()
        self._read_only = False
        self._icons: dict[tuple[str, str], object] = {}
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
        if ref.node.kind is SemanticNodeKind.SWEEP_AXIS:
            # An outer axis must keep its own value while an inner axis runs.
            # Looking for an arbitrary descendant would format the inner
            # setpoint using the outer dimension and mislead the operator.
            return self._state_for(f"{ref.node.semantic_id}.set-roi-value")
        candidates: list[object] = []
        for child in ref.node.children:
            child_ref = self._by_id.get(child.semantic_id)
            if child_ref is not None:
                state = self._descendant_state(child_ref)
                if state is not None:
                    candidates.append(state)
        return max(
            candidates,
            key=lambda candidate: int(_state_value(candidate, "action_index", -1)),
            default=None,
        )

    @staticmethod
    def _tokens():
        return tokens_for("dark" if isDarkTheme() else "light")

    def _accent_color(self, node: SemanticTreeNode) -> str:
        tokens = self._tokens()
        label = node.label.lower()
        if node.kind is SemanticNodeKind.ACTION:
            if "wait" in label:
                return tokens.caution
            if any(word in label for word in ("acquire", "measure", "spectrum")):
                return tokens.success
            if "output" in label:
                return tokens.caution
        return {
            SemanticNodeKind.DEVICE: tokens.accent,
            SemanticNodeKind.SWEEP_AXIS: tokens.accent,
            SemanticNodeKind.LOOP_BODY: tokens.accent,
            SemanticNodeKind.SET_ROI_VALUE: tokens.accent,
            SemanticNodeKind.FINALLY: tokens.caution,
            SemanticNodeKind.GENERATED_SAFETY: tokens.success,
        }.get(node.kind, tokens.neutral)

    @staticmethod
    def _icon_name(node: SemanticTreeNode) -> str:
        if node.kind is SemanticNodeKind.ACTION:
            label = node.label.lower()
            if "wait" in label:
                return "wait"
            if any(word in label for word in ("acquire", "measure", "spectrum")):
                return "acquire"
            if "output" in label:
                return "output"
            if "comment" in label:
                return "comment"
            return "action"
        return node.kind.value

    def _icon(self, node: SemanticTreeNode) -> object:
        theme = "dark" if isDarkTheme() else "light"
        name = self._icon_name(node)
        cache_key = (theme, name)
        icon = self._icons.get(cache_key)
        if icon is not None:
            return icon
        fluent_icon = {
            "sequence": FluentIcon.FOLDER,
            "device": FluentIcon.IOT,
            "sweep_axis": FluentIcon.SYNC,
            "loop_body": FluentIcon.RETURN,
            "set_roi_value": FluentIcon.UPDATE,
            "finally": FluentIcon.ACCEPT,
            "generated_safety": FluentIcon.POWER_BUTTON,
            "wait": FluentIcon.STOP_WATCH,
            "acquire": FluentIcon.PROJECTOR,
            "output": FluentIcon.POWER_BUTTON,
            "comment": FluentIcon.QUICK_NOTE,
            "action": FluentIcon.PLAY,
        }[name]
        icon = fluent_icon.icon()
        self._icons[cache_key] = icon
        return icon

    def _value_text(self, ref: _NodeRef) -> str:
        state = self._descendant_state(ref)
        if (
            ref.node.kind is SemanticNodeKind.ACTION
            and isinstance(ref.node.data.get("duration"), str)
        ):
            return str(ref.node.data["duration"])
        if state is None:
            return "—"
        applied = _state_value(state, "applied_si")
        requested = _state_value(state, "requested_si")
        readback = _state_value(state, "readback_si")
        axis = ref.node.axis
        dimension = (
            axis.dimension
            if axis is not None
            else ref.node.data.get("dimension")
            if isinstance(ref.node.data.get("dimension"), str)
            else None
        )
        if applied is not None and dimension:
            applied_text = format_quantity_auto(float(applied), dimension)
            text = applied_text
            if requested is not None:
                requested_text = format_quantity_auto(float(requested), dimension)
                text = (
                    applied_text
                    if requested_text == applied_text
                    else f"requested {requested_text} · applied {applied_text}"
                )
            if readback is not None and readback != applied:
                text += f" · readback {format_quantity_auto(float(readback), dimension)}"
            return text
        if requested is not None and dimension:
            return f"requested {format_quantity_auto(float(requested), dimension)}"
        context = _state_value(state, "axis_context")
        if context is not None:
            value = _state_value(context, "value_si")
            if value is not None and dimension:
                rendered = format_quantity_auto(float(value), dimension)
                point_index = _state_value(context, "point_index")
                point_count = _state_value(context, "point_count")
                if isinstance(point_index, int) and isinstance(point_count, int) and point_count > 0:
                    rendered += f" · {point_index + 1}/{point_count}"
                return rendered
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
                value = self._value_text(ref)
                if value != "—":
                    return value
                if node.kind is SemanticNodeKind.SWEEP_AXIS and node.axis is not None:
                    return f"{node.axis.target} · {len(node.axis.points)} point(s)"
                if node.kind is SemanticNodeKind.LOOP_BODY:
                    return str(node.data.get("point_count", "Executable child steps"))
                if node.kind is SemanticNodeKind.SET_ROI_VALUE:
                    return str(node.data.get("target", "Set ROI value"))
                return str(node.data.get("detail", node.kind.value.replace("_", " ").title()))
            if index.column() == 2:
                context = _state_value(state, "axis_context") if state is not None else None
                point_index = _state_value(context, "point_index") if context is not None else None
                point_count = _state_value(context, "point_count") if context is not None else None
                stage_index = _state_value(context, "stage_index") if context is not None else None
                if isinstance(point_index, int) and isinstance(point_count, int):
                    stage = f" · ROI {stage_index + 1}" if isinstance(stage_index, int) else ""
                    return f"{point_index + 1}/{point_count}{stage}"
                return "—"
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
        if role == int(Qt.ItemDataRole.DecorationRole) and index.column() == 0:
            return self._icon(node)
        if role == int(Qt.ItemDataRole.ForegroundRole):
            phase = str(_state_value(state, "phase", "")) if state is not None else ""
            if phase == "applied":
                return QBrush(QColor(self._tokens().success))
            if phase == "failed":
                return QBrush(QColor(self._tokens().danger))
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
        if role == int(MeasurementTreeRole.ACCENT_COLOR):
            return self._accent_color(node)
        if role == int(MeasurementTreeRole.DROP_TARGET):
            # Recipe containers may be empty and still need to advertise a
            # valid insertion surface.  Generated rows and the Finally
            # boundary remain intentionally non-droppable.
            return node.kind in {
                SemanticNodeKind.SEQUENCE,
                SemanticNodeKind.DEVICE,
                SemanticNodeKind.SWEEP_AXIS,
                SemanticNodeKind.LOOP_BODY,
            }
        if role == int(Qt.ItemDataRole.FontRole) and index.column() == 0:
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
        # ``editable`` is a semantic capability used by the recipe page to
        # enable modal editors.  It is deliberately not exposed as
        # ItemIsEditable: labels are derived presentation text and the model
        # has no inline ``setData`` transaction.  Advertising the Qt editing
        # flag would let a platform style open a text editor for a name and
        # silently bypass the ROI/device/WAIT validation dialogs.
        if ref.node.draggable and not self._read_only:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        if (
            not self._read_only
            and ref.node.kind
            in {
                SemanticNodeKind.SEQUENCE,
                SemanticNodeKind.DEVICE,
                SemanticNodeKind.SWEEP_AXIS,
                SemanticNodeKind.LOOP_BODY,
            }
        ):
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
        self._reported_unknown_ids.clear()
        self._reindex()
        self.endResetModel()

    def apply_state(self, state: object) -> bool:
        """Apply one runtime state and notify the affected rows."""

        return bool(self.apply_states((state,)))

    def apply_states(self, states: Iterable[object]) -> int:
        """Apply a presentation batch with one notification per affected row.

        A sweep can deliver several semantic confirmations between GUI turns.
        Updating the backing map first and emitting the row notifications only
        once prevents Qt from repainting the same tree for every confirmation.
        The runner event stream remains lossless; this is only a view-level
        batching boundary.
        """

        changed: dict[str, _NodeRef] = {}
        for state in states:
            semantic_id = _state_value(state, "semantic_id")
            if not isinstance(semantic_id, str):
                continue
            ref = self._by_id.get(semantic_id)
            if ref is None:
                if semantic_id not in self._reported_unknown_ids:
                    self._reported_unknown_ids.add(semantic_id)
                    self.unknown_semantic_state.emit(semantic_id)
                continue
            self._states[semantic_id] = state
            changed[semantic_id] = ref
            if ref.node.kind is SemanticNodeKind.SET_ROI_VALUE:
                ancestor = ref.parent
                while ancestor is not None:
                    if ancestor.node.kind is SemanticNodeKind.SWEEP_AXIS:
                        changed[ancestor.node.semantic_id] = ancestor
                        break
                    ancestor = ancestor.parent

        for ref in changed.values():
            index = self.index_for_semantic_id(ref.node.semantic_id)
            if not index.isValid():
                continue
            self.dataChanged.emit(
                index,
                self.index(index.row(), self.COLUMN_COUNT - 1, index.parent()),
            )
        return len(changed)

    def state_for(self, semantic_id: str) -> object | None:
        return self._states.get(semantic_id)

    def value_for(self, semantic_id: str) -> str:
        ref = self._by_id.get(semantic_id)
        return self._value_text(ref) if ref is not None else "—"
