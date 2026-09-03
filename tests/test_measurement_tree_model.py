from __future__ import annotations

import os
from dataclasses import replace

from PySide6.QtCore import QMimeData, QPointF, QSize, Qt
from PySide6.QtGui import QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.recipes.semantic_tree import (
    AxisPointContext,
    SemanticMeasurementTree,
    SemanticNodeKind,
    SemanticTreeNode,
    SweepAxisBinding,
)


def semantic_tree() -> SemanticMeasurementTree:
    binding = SweepAxisBinding(
        axis_id="axis-current",
        source_node_id="source-current",
        owner_node_id="device-keithley",
        device_module="keithley",
        endpoint="B",
        parameter_id="source.level",
        target="keithley.B.current",
        dimension="current",
        stages=(),
        points=(),
    )
    operation = SemanticTreeNode(
        "axis-current.set-roi-value",
        SemanticNodeKind.SET_ROI_VALUE,
        "source-current",
        "Set ROI value · Keithley B · source current",
        {"target": "keithley.B.current", "dimension": "current"},
        editable=False,
        draggable=False,
    )
    loop = SemanticTreeNode(
        "axis-current.loop",
        SemanticNodeKind.LOOP_BODY,
        "source-current",
        "For each source-current point",
        {"point_count": 2},
        children=(operation,),
    )
    axis = SemanticTreeNode(
        "axis-current",
        SemanticNodeKind.SWEEP_AXIS,
        "source-current",
        "Sweep axis · Source current",
        {"target": "keithley.B.current"},
        axis=binding,
        children=(loop,),
    )
    root = SemanticTreeNode(
        "sequence",
        SemanticNodeKind.SEQUENCE,
        "sequence",
        "Measurement sequence",
        children=(axis,),
    )
    return SemanticMeasurementTree(
        roots=(root,),
        by_id={node.semantic_id: node for node in (root, axis, loop, operation)},
        parent_by_id={axis.semantic_id: root.semantic_id, loop.semantic_id: axis.semantic_id, operation.semantic_id: loop.semantic_id},
        children_by_id={root.semantic_id: (axis.semantic_id,), axis.semantic_id: (loop.semantic_id,), loop.semantic_id: (operation.semantic_id,), operation.semantic_id: ()},
    )


def operation_state(semantic_id: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        semantic_id=semantic_id,
        phase="applied",
        requested_si=0.005,
        applied_si=0.005,
        readback_si=0.005,
        verification="readback",
        action_index=2,
        total_actions=8,
        axis_context=AxisPointContext(
            "axis-current", 1, 2, 0, 0.005, {"keithley.B.current": 0.005}, ("source-current",)
        ),
    )


def test_model_exposes_semantic_hierarchy_without_widget_items() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeRole
    from app.ui.design_system.tokens import tokens_for
    from qfluentwidgets import isDarkTheme

    model = MeasurementTreeModel(semantic_tree())
    axis = model.index_for_semantic_id("axis-current")
    operation = model.index_for_semantic_id("axis-current.set-roi-value")
    assert axis.isValid()
    assert operation.parent() == model.index_for_semantic_id("axis-current.loop")
    assert model.data(axis, Qt.ItemDataRole.UserRole + 1) == "axis-current"
    assert model.data(operation, Qt.ItemDataRole.DisplayRole).startswith("Set ROI value")
    assert not model.data(operation, Qt.ItemDataRole.DecorationRole).isNull()
    tokens = tokens_for("dark" if isDarkTheme() else "light")
    assert model.data(axis, MeasurementTreeRole.ACCENT_COLOR) == tokens.accent
    assert model.data(operation, MeasurementTreeRole.ACCENT_COLOR) == tokens.accent
    assert model.headerData(0, Qt.Orientation.Horizontal) == "Operation"
    assert model.headerData(1, Qt.Orientation.Horizontal) == "Configured / active value"


def test_runtime_update_emits_data_changed_only_for_affected_row() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel

    model = MeasurementTreeModel(semantic_tree())
    from PySide6.QtTest import QSignalSpy

    signal = QSignalSpy(model.dataChanged)
    model.apply_state(operation_state("axis-current.set-roi-value"))
    assert signal.count() == 2
    assert signal.at(0)[0].row() == model.index_for_semantic_id("axis-current.set-roi-value").row()
    assert signal.at(0)[1].row() == signal.at(0)[0].row()
    changed_ids = {
        model.data(event[0], Qt.ItemDataRole.UserRole + 1)
        for event in (signal.at(index) for index in range(signal.count()))
    }
    assert changed_ids == {
        "axis-current.set-roi-value",
        "axis-current",
    }


def test_axis_value_and_progress_are_kept_in_separate_columns() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel

    model = MeasurementTreeModel(semantic_tree())
    model.apply_state(operation_state("axis-current.set-roi-value"))
    axis = model.index_for_semantic_id("axis-current")

    assert model.data(axis.siblingAtColumn(1)) == "5 mA"
    assert model.data(axis.siblingAtColumn(2)) == "2/2 · ROI 1"
    assert model.data(axis.siblingAtColumn(3)) == "APPLIED"


def test_model_replace_tree_is_explicit_and_flags_generated_row_read_only() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeRole

    model = MeasurementTreeModel(semantic_tree())
    index = model.index_for_semantic_id("axis-current.set-roi-value")
    flags = model.flags(index)
    assert not flags & Qt.ItemFlag.ItemIsEditable
    assert not flags & Qt.ItemFlag.ItemIsDragEnabled
    assert not flags & Qt.ItemFlag.ItemIsDropEnabled
    assert model.data(index, MeasurementTreeRole.EDITABLE) is False


def test_empty_sequence_advertises_an_insertion_target() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeRole

    original = semantic_tree()
    empty_root = replace(original.roots[0], children=())
    tree = replace(
        original,
        roots=(empty_root,),
        by_id={empty_root.semantic_id: empty_root},
        parent_by_id={},
        children_by_id={empty_root.semantic_id: ()},
    )
    model = MeasurementTreeModel(tree)
    root = model.index_for_semantic_id("sequence")

    assert model.data(root, MeasurementTreeRole.DROP_TARGET) is True
    assert model.flags(root) & Qt.ItemFlag.ItemIsDropEnabled


def test_unknown_runtime_state_emits_one_diagnostic_without_prefix_attachment() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel
    from PySide6.QtTest import QSignalSpy

    model = MeasurementTreeModel(semantic_tree())
    signal = QSignalSpy(model.unknown_semantic_state)
    unknown = operation_state("technical.generated.action")

    assert model.apply_state(unknown) is False
    assert model.apply_state(unknown) is False
    assert signal.count() == 1
    assert signal.at(0)[0] == "technical.generated.action"
    assert not model.index_for_semantic_id("technical.generated.action").isValid()


def test_measurement_tree_is_fluent_model_view() -> None:
    import qfluentwidgets
    from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeView

    app = QApplication.instance() or QApplication([])
    view = MeasurementTreeView()
    assert isinstance(view, qfluentwidgets.TreeView)
    view.setModel(MeasurementTreeModel(semantic_tree()))
    view.resize(900, 600)
    view.show()
    QApplication.processEvents()
    assert view.viewport().geometry().width() > 0
    assert view.indentation() == 24
    assert view.iconSize() == QSize(18, 18)
    assert view.sizeHintForRow(0) >= 34
    view.close()
    del app


def test_fluent_tree_expands_the_complete_measurement_hierarchy_by_default() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeView

    app = QApplication.instance() or QApplication([])
    view = MeasurementTreeView()
    model = MeasurementTreeModel(semantic_tree())
    view.setModel(model)
    view.resize(900, 600)
    view.show()
    QApplication.processEvents()

    root = model.index_for_semantic_id("sequence")
    axis = model.index_for_semantic_id("axis-current")
    loop = model.index_for_semantic_id("axis-current.loop")
    assert view.isExpanded(root)
    assert view.isExpanded(axis)
    assert view.isExpanded(loop)
    view.close()
    del app


def test_read_only_tree_follow_reveals_the_active_nested_operation() -> None:
    from app.ui.measurement_tree import (
        MeasurementTreeModel,
        MeasurementTreeView,
        TreeInteractionMode,
    )

    app = QApplication.instance() or QApplication([])
    view = MeasurementTreeView()
    view.set_interaction_mode(TreeInteractionMode.READ_ONLY)
    model = MeasurementTreeModel(semantic_tree())
    view.setModel(model)
    view.resize(420, 120)
    view.show()
    QApplication.processEvents()

    view.follow_semantic_id("axis-current.set-roi-value", force=True)
    QApplication.processEvents()

    active = model.index_for_semantic_id("axis-current.set-roi-value")
    active_rect = view.visualRect(active)
    assert active_rect.isValid()
    assert view.viewport().rect().intersects(active_rect)
    view.close()
    del app


def test_fluent_tree_drop_emits_a_model_owned_move_request() -> None:
    from unittest.mock import Mock

    from app.ui.measurement_tree import (
        MeasurementTreeModel,
        MeasurementTreeView,
        TreeDropPlacement,
    )

    app = QApplication.instance() or QApplication([])
    view = MeasurementTreeView()
    model = MeasurementTreeModel(semantic_tree())
    view.setModel(model)
    view.resize(900, 600)
    view.show()
    QApplication.processEvents()
    requests = []

    def accept(request) -> None:
        requests.append(request)
        request.accepted = True

    view.move_requested.connect(accept)
    target = model.index_for_semantic_id("axis-current.loop")
    event = Mock()
    event.source.return_value = view
    event.position.return_value = QPointF(view.visualRect(target).center())
    event.mimeData().hasFormat.return_value = False
    view._dragged_semantic_id = "axis-current"
    view.dropEvent(event)

    assert len(requests) == 1
    assert requests[0].source_semantic_id == "axis-current"
    assert requests[0].destination_semantic_id == "axis-current.loop"
    assert requests[0].placement is TreeDropPlacement.INSIDE
    event.accept.assert_called_once()
    view.close()
    del app


def test_real_qt_library_drag_events_show_and_commit_drop_feedback() -> None:
    """Exercise the actual Qt drag-event path, including the drop cue state."""

    from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeView

    app = QApplication.instance() or QApplication([])
    view = MeasurementTreeView()
    model = MeasurementTreeModel(semantic_tree())
    view.setModel(model)
    view.resize(900, 600)
    view.show()
    app.processEvents()

    root = model.index_for_semantic_id("sequence")
    position = view.visualRect(root).center()
    mime = QMimeData()
    mime.setData(view._library_mime_type, b"flow:wait")

    drag_move = QDragMoveEvent(
        position,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.dragMoveEvent(drag_move)
    assert drag_move.isAccepted()
    assert view._drop_target is not None
    assert view._drop_target[0] == "sequence"

    requests = []
    view.library_drop_requested.connect(
        lambda request: (requests.append(request), setattr(request, "accepted", True))
    )
    drop = QDropEvent(
        position,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.dropEvent(drop)

    assert len(requests) == 1
    assert requests[0].destination_semantic_id == "sequence"
    assert drop.isAccepted()
    view.close()
    del app
