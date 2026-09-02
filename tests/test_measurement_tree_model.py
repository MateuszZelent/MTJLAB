from __future__ import annotations

import os

import pytest
from PySide6.QtCore import Qt
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
    from app.ui.measurement_tree import MeasurementTreeModel

    model = MeasurementTreeModel(semantic_tree())
    axis = model.index_for_semantic_id("axis-current")
    operation = model.index_for_semantic_id("axis-current.set-roi-value")
    assert axis.isValid()
    assert operation.parent() == model.index_for_semantic_id("axis-current.loop")
    assert model.data(axis, Qt.ItemDataRole.UserRole + 1) == "axis-current"
    assert model.data(operation, Qt.ItemDataRole.DisplayRole).startswith("Set ROI value")


def test_runtime_update_emits_data_changed_only_for_affected_row() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel

    model = MeasurementTreeModel(semantic_tree())
    from PySide6.QtTest import QSignalSpy

    signal = QSignalSpy(model.dataChanged)
    model.apply_state(operation_state("axis-current.set-roi-value"))
    assert signal.count() == 1
    assert signal.at(0)[0].row() == model.index_for_semantic_id("axis-current.set-roi-value").row()
    assert signal.at(0)[1].row() == signal.at(0)[0].row()


def test_model_replace_tree_is_explicit_and_flags_generated_row_read_only() -> None:
    from app.ui.measurement_tree import MeasurementTreeModel, MeasurementTreeRole

    model = MeasurementTreeModel(semantic_tree())
    index = model.index_for_semantic_id("axis-current.set-roi-value")
    flags = model.flags(index)
    assert not flags & Qt.ItemFlag.ItemIsEditable
    assert not flags & Qt.ItemFlag.ItemIsDragEnabled
    assert not flags & Qt.ItemFlag.ItemIsDropEnabled
    assert model.data(index, MeasurementTreeRole.EDITABLE) is False


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
    view.close()
    del app
