"""Sweep tree panel and dialog for inspecting the THATEC experiment hierarchy."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSplitter,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    PlainTextEdit,
    PushButton,
    SegmentedWidget,
    SpinBox,
    TreeWidget,
)

from app.ui.widgets.fluent_code_viewer import FluentCodeViewer
from app.recipes.models import parse_recipe_text
from app.recipes.semantic_tree import (
    SemanticMeasurementTree,
    SemanticNodeKind,
    SemanticTreeNode,
    normalize_recipe_tree,
)
from app.storage import (
    RunDetail,
    StoredPoint,
    StoredReference,
    ThatecDevice,
    ThatecRecord,
    ThatecRow,
    ThatecRun,
    ThatecRunReader,
    ThatecTreeNode,
)
from app.ui.dialogs import StationDialog
from app.ui.measurement_tree import (
    MeasurementTreeModel,
    MeasurementTreeView,
    TreeInteractionMode,
)


class SweepTreePanel(QWidget):
    """Interactive tree viewer for the THATEC experiment hierarchy.

    Shows the measurement tree, devices, labbook and post-process records
    with an inspector pane and checkpoint navigation.  Can be embedded in
    the Results page or placed inside a dialog.
    """

    node_selected = Signal(object)       # public/private result record | None
    spectrum_requested = Signal(str, int)  # (row_id, checkpoint)
    stored_spectrum_requested = Signal(int, str)  # (point index, raw|processed)
    reference_spectrum_requested = Signal(int)  # (reference index)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._run: ThatecRun | None = None
        self._selected_path: Path | None = None
        self._selected_thatec_row: ThatecRow | None = None
        self._selected_stored_point: StoredPoint | None = None
        self._selected_stored_variant = "raw"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # --- Mode Switcher ---
        self.view_switch = SegmentedWidget(self)
        self.view_switch.addItem("tree", "Sweep structure")
        self.view_switch.addItem("data", "Recorded data & checkpoints")
        self.view_switch.setCurrentItem("tree")
        layout.addWidget(self.view_switch)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # --- Stacked Tree View ---
        self.tree_stack = QStackedWidget(self)

        # 1. Fluent Measurement Tree (identical to Sweeps & Execution)
        self.tree_model = MeasurementTreeModel(parent=self)
        self.measurement_tree = MeasurementTreeView(self)
        self.measurement_tree.setObjectName("resultsMeasurementTree")
        self.measurement_tree.setModel(self.tree_model)
        self.measurement_tree.set_interaction_mode(TreeInteractionMode.READ_ONLY)
        self.measurement_tree.setMinimumHeight(80)
        self.tree_stack.addWidget(self.measurement_tree)

        # 2. Storage Checkpoints & Datasets Tree
        self.tree = TreeWidget(self)
        self.tree.setHeaderLabels(["THATEC experiment", "Type"])
        self.tree.setMinimumHeight(80)
        self.tree_stack.addWidget(self.tree)

        splitter.addWidget(self.tree_stack)

        # --- Inspector + checkpoint ---
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        self.inspector = FluentCodeViewer(language="yaml", parent=self)
        self.inspector.setReadOnly(True)
        self.inspector.setMinimumHeight(80)
        bottom_layout.addWidget(self.inspector)

        checkpoint_bar = QHBoxLayout()
        checkpoint_label = CaptionLabel("THATEC checkpoint:", self)
        checkpoint_label.setObjectName("muted")
        checkpoint_bar.addWidget(checkpoint_label)
        self.thatec_checkpoint = SpinBox(self)
        self.thatec_checkpoint.setMinimum(0)
        checkpoint_bar.addWidget(self.thatec_checkpoint)
        self.show_spectrum_button = PushButton("Show spectrum", self)
        self.show_spectrum_button.setIcon(FluentIcon.VIEW)
        self.show_spectrum_button.setEnabled(False)
        self.show_spectrum_button.setToolTip(
            "Open the selected immutable result in the Spectrum viewer."
        )
        checkpoint_bar.addWidget(self.show_spectrum_button)
        checkpoint_bar.addStretch(1)
        bottom_layout.addLayout(checkpoint_bar)

        self.values_tree = TreeWidget(self)
        self.values_tree.setHeaderLabels(["Checkpoint", "Value", "Timestamp UTC"])
        self.values_tree.setUniformRowHeights(True)
        self.values_tree.setAlternatingRowColors(True)
        self.values_tree.setMaximumHeight(150)
        bottom_layout.addWidget(self.values_tree)

        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.setMinimumHeight(240)

        # --- Connections ---
        self.view_switch.currentItemChanged.connect(
            lambda route: self.tree_stack.setCurrentIndex(0 if route == "tree" else 1)
        )
        self.measurement_tree.semantic_selected.connect(self._on_semantic_selected)
        self.tree.currentItemChanged.connect(self._on_tree_selected)
        self.thatec_checkpoint.valueChanged.connect(
            lambda _value: self._render_selected_row()
        )
        self.show_spectrum_button.clicked.connect(self._show_selected_spectrum)

    def load(
        self,
        path: Path,
        run: ThatecRun,
        tree: tuple[ThatecTreeNode, ...],
        *,
        points: tuple[StoredPoint, ...] = (),
        references: tuple[StoredReference, ...] = (),
        detail: RunDetail | None = None,
    ) -> None:
        """Populate the panel from a loaded THATEC result."""
        self._run = run
        self._selected_path = path
        self._selected_thatec_row = None
        self._selected_stored_point = None
        self._selected_stored_variant = "raw"

        # 1. Build semantic measurement tree for the Sweeps-compatible view
        recipe_source = ""
        if detail is not None and detail.recipe_yaml:
            recipe_source = detail.recipe_yaml.strip()
        elif hasattr(run, "recipe_source") and run.recipe_source:
            recipe_source = run.recipe_source.strip()

        tree_built = False
        if recipe_source:
            try:
                recipe = parse_recipe_text(recipe_source, origin=str(path))
                snapshot = normalize_recipe_tree(recipe)
                self.tree_model.replace_tree(snapshot)
                self.measurement_tree.expandAll()
                tree_built = True
            except Exception:
                tree_built = False

        if not tree_built:
            snapshot = self._build_historical_semantic_tree(path, run, tree)
            self.tree_model.replace_tree(snapshot)
            self.measurement_tree.expandAll()

        # 2. Populate the checkpoints and dataset tree
        self._populate_tree(tree, points=points, references=references)

    def clear(self) -> None:
        """Reset the panel to its empty state."""
        self.tree.clear()
        self.tree_model.replace_tree(SemanticMeasurementTree((), {}, source_text=""))
        self.inspector.clear()
        self.values_tree.clear()
        self._run = None
        self._selected_path = None
        self._selected_thatec_row = None
        self._selected_stored_point = None
        self._selected_stored_variant = "raw"
        self.show_spectrum_button.setEnabled(False)

    def _build_historical_semantic_tree(
        self, path: Path, run: ThatecRun, tree: tuple[ThatecTreeNode, ...]
    ) -> SemanticMeasurementTree:
        by_id: dict[str, SemanticTreeNode] = {}

        def project_node(node: ThatecTreeNode) -> SemanticTreeNode:
            row = run.rows.get(node.id)
            detail = node.kind or (row.function if row is not None else "recorded")
            semantic_id = f"historical.row.{node.id}"
            projected = SemanticTreeNode(
                semantic_id=semantic_id,
                kind=SemanticNodeKind.ACTION,
                label=node.label,
                data={"detail": detail, "status": "RECORDED", "row_id": node.id},
                children=tuple(project_node(child) for child in node.children),
                editable=False,
                draggable=False,
            )
            by_id[semantic_id] = projected
            return projected

        children = [project_node(node) for node in tree]
        if run.devices:
            device_children: list[SemanticTreeNode] = []
            for device in run.devices:
                semantic_id = f"historical.device.{device.name}"
                projected = SemanticTreeNode(
                    semantic_id=semantic_id,
                    kind=SemanticNodeKind.DEVICE,
                    label=device.name,
                    data={"detail": "device settings", "status": "RECORDED"},
                    editable=False,
                    draggable=False,
                )
                by_id[semantic_id] = projected
                device_children.append(projected)
            devices = SemanticTreeNode(
                semantic_id="historical.devices",
                kind=SemanticNodeKind.SEQUENCE,
                label="Recorded device configuration",
                data={"detail": "THATEC /devices", "status": "RECORDED"},
                children=tuple(device_children),
                editable=False,
                draggable=False,
            )
            by_id[devices.semantic_id] = devices
            children.append(devices)
        root = SemanticTreeNode(
            semantic_id="historical.root",
            kind=SemanticNodeKind.SEQUENCE,
            label=f"Historical Sweep — {path.name}",
            data={"detail": "Reconstructed sweep hierarchy", "status": "RECORDED"},
            children=tuple(children),
            editable=False,
            draggable=False,
        )
        by_id[root.semantic_id] = root
        return SemanticMeasurementTree((root,), by_id, source_text="")

    def _on_semantic_selected(self, semantic_id: str) -> None:
        node = self.tree_model.tree.by_id.get(semantic_id)
        if node is None:
            return
        self.node_selected.emit(node)
        lines = [
            f"Operation: {node.label}",
            f"Kind: {node.kind.value}",
            f"Semantic ID: {node.semantic_id}",
        ]
        if node.data:
            lines.extend(("", "Parameters / Configuration:", _format_json(dict(node.data))))
        self.inspector.setPlainText("\n".join(lines))
        self.values_tree.clear()

        # Check if node corresponds to a THATEC row
        row_id = str(node.data.get("row_id") or "") if isinstance(node.data, dict) else ""
        if not row_id and node.semantic_id.startswith("historical.row."):
            row_id = node.semantic_id.removeprefix("historical.row.")

        if row_id and self._run and row_id in self._run.rows:
            row = self._run.rows[row_id]
            self._selected_thatec_row = row
            if row.shape and len(row.shape) >= 2:
                self.thatec_checkpoint.setMaximum(max(0, row.shape[0] - 1))
                self.show_spectrum_button.setEnabled(True)
            else:
                self.show_spectrum_button.setEnabled(False)
        else:
            self._selected_thatec_row = None
            self.show_spectrum_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Tree population
    # ------------------------------------------------------------------

    def _populate_tree(
        self,
        tree: tuple[ThatecTreeNode, ...],
        *,
        points: tuple[StoredPoint, ...] = (),
        references: tuple[StoredReference, ...] = (),
    ) -> None:
        self.tree.setUpdatesEnabled(False)
        try:
            self._populate_tree_content(tree, points=points, references=references)
        finally:
            self.tree.setUpdatesEnabled(True)

    def _populate_tree_content(
        self,
        tree: tuple[ThatecTreeNode, ...],
        *,
        points: tuple[StoredPoint, ...] = (),
        references: tuple[StoredReference, ...] = (),
    ) -> None:
        self.tree.clear()
        self.inspector.clear()
        self.values_tree.clear()
        if self._run is None:
            return
        measurements = QTreeWidgetItem(["Measurements", "THATEC tree"])
        self.tree.addTopLevelItem(measurements)
        for node in tree:
            self._add_tree_node(measurements, node)
        for title, records in (
            ("Devices", self._run.devices),
            ("Labbook", self._run.labbook),
            ("Post-process", self._run.post_process),
        ):
            section = QTreeWidgetItem([title, "THATEC"])
            self.tree.addTopLevelItem(section)
            for record in records:
                label = (
                    record.name
                    if isinstance(record, ThatecDevice)
                    else record.id
                )
                item = QTreeWidgetItem([label, "record"])
                item.setData(0, Qt.ItemDataRole.UserRole, record)
                section.addChild(item)

        # Private Lab Control groups and public THATEC rows describe the same
        # immutable run from different compatibility layers.  Keep both in a
        # dedicated Results branch so an operator can inspect the complete
        # checkpoint/reference history without losing the original Sweep tree.
        public_checkpoint_count = max(
            (row.shape[0] for row in self._run.rows.values() if len(row.shape) >= 2),
            default=0,
        )
        results = QTreeWidgetItem(
            [
                "Results",
                f"{len(points)} private; {public_checkpoint_count} public checkpoints; "
                f"{len(references)} references",
            ]
        )
        self.tree.addTopLevelItem(results)

        checkpoints = QTreeWidgetItem(
            [f"Checkpoints ({len(points)})", "private /points"]
        )
        results.addChild(checkpoints)
        point_items: list[QTreeWidgetItem] = []
        for point in points:
            point_item = QTreeWidgetItem(
                [
                    f"Checkpoint {point.index}",
                    f"{point.status}{' · spectrum' if point.has_spectrum else ''}",
                ]
            )
            point_item.setData(0, Qt.ItemDataRole.UserRole, point)
            point_items.append(point_item)
            setpoints = QTreeWidgetItem(["Setpoints", str(len(point.setpoints))])
            measurements = QTreeWidgetItem(
                ["Measurements", str(len(point.measurements))]
            )
            point_item.addChildren((setpoints, measurements))
            if point.setpoints:
                setpoints.addChildren(
                    [QTreeWidgetItem([str(key), str(value)]) for key, value in sorted(point.setpoints.items())]
                )
            if point.measurements:
                measurements.addChildren(
                    [QTreeWidgetItem([str(key), str(value)]) for key, value in sorted(point.measurements.items())]
                )
            spectrum_items: list[QTreeWidgetItem] = []
            if point.has_spectrum:
                raw_item = QTreeWidgetItem(["Raw spectrum", "private /spectra"])
                raw_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    (point, "raw"),
                )
                spectrum_items.append(raw_item)
                processed_item = QTreeWidgetItem(
                    ["Processed spectrum", "private /spectra"]
                )
                processed_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    (point, "processed"),
                )
                spectrum_items.append(processed_item)
            if spectrum_items:
                point_item.addChildren(spectrum_items)
        checkpoints.addChildren(point_items)

        if public_checkpoint_count:
            results.addChild(
                QTreeWidgetItem(
                    [
                        f"Public checkpoints ({public_checkpoint_count})",
                        "/measurement",
                    ]
                )
            )

        references_item = QTreeWidgetItem(
            [f"References ({len(references)})", "private /references"]
        )
        results.addChild(references_item)
        for reference in references:
            item = QTreeWidgetItem(
                [
                    f"Reference {reference.index}",
                    f"{reference.kind} · {reference.average_count} average(s)",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, reference)
            references_item.addChild(item)

        public_datasets = QTreeWidgetItem(
            [
                f"Public datasets ({sum(bool(row.shape) for row in self._run.rows.values())})",
                "/measurement",
            ]
        )
        results.addChild(public_datasets)
        for row in self._run.rows.values():
            if not row.shape:
                continue
            label = row.control_name or row.device_name or row.id
            item = QTreeWidgetItem([label, " x ".join(str(size) for size in row.shape)])
            item.setData(0, Qt.ItemDataRole.UserRole, row)
            item.setData(1, Qt.ItemDataRole.UserRole, row.id)
            public_datasets.addChild(item)
        self.tree.expandToDepth(1)

    def _add_tree_node(
        self, parent: QTreeWidgetItem, node: ThatecTreeNode
    ) -> None:
        item = QTreeWidgetItem([node.label, node.kind])
        item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            self._run.rows.get(node.id) if self._run else None,
        )
        item.setData(1, Qt.ItemDataRole.UserRole, node.id)
        parent.addChild(item)
        for child in node.children:
            self._add_tree_node(item, child)

    # ------------------------------------------------------------------
    # Selection handling
    # ------------------------------------------------------------------

    def _on_tree_selected(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        record = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        self.show_spectrum_button.setEnabled(False)
        self._selected_thatec_row = record if isinstance(record, ThatecRow) else None
        self._selected_stored_point = None
        self._selected_stored_variant = "raw"
        if isinstance(record, ThatecRow) and self._selected_path is not None:
            self._selected_thatec_row = record
            self.node_selected.emit(record)
            if not record.shape:
                self.inspector.setPlainText(
                    _format_json(
                        {
                            "definition": record.definition,
                            "metadata": record.metadata,
                            "recorded_data": (
                                "No measurement array for this THATEC "
                                "control/internal node."
                            ),
                        }
                    )
                )
                self.values_tree.clear()
                return
            self.thatec_checkpoint.setMaximum(
                max(0, record.shape[0] - 1 if record.shape else 0)
            )
            self.show_spectrum_button.setEnabled(len(record.shape) >= 2)
            self._render_selected_row()
        elif isinstance(record, StoredPoint):
            self._selected_thatec_row = None
            self.thatec_checkpoint.setRange(0, 0)
            self.values_tree.clear()
            self.inspector.setPlainText(
                _format_json(
                    {
                        "checkpoint": record.index,
                        "status": record.status,
                        "timestamp_utc": record.timestamp_utc,
                        "setpoints": record.setpoints,
                        "measurements": record.measurements,
                        "metadata": record.metadata,
                        "device_states": record.device_states,
                        "has_spectrum": record.has_spectrum,
                    }
                )
            )
            self._selected_stored_point = record
            self.show_spectrum_button.setEnabled(record.has_spectrum)
            self.node_selected.emit(record)
        elif isinstance(record, StoredReference):
            self._selected_thatec_row = None
            self.values_tree.clear()
            self.inspector.setPlainText(
                _format_json(
                    {
                        "reference_index": record.index,
                        "kind": record.kind,
                        "average_count": record.average_count,
                        "trace_name": record.trace_name,
                        "acquired_at_utc": record.acquired_at_utc,
                        "frequency_points": len(record.frequencies_hz),
                    }
                )
            )
            self.node_selected.emit(record)
            self.reference_spectrum_requested.emit(record.index)
        elif (
            isinstance(record, tuple)
            and len(record) == 2
            and isinstance(record[0], StoredPoint)
        ):
            point, variant = record
            self._selected_thatec_row = None
            self.values_tree.clear()
            self.inspector.setPlainText(
                _format_json(
                    {
                        "checkpoint": point.index,
                        "variant": variant,
                        "action": "Open in Spectrum viewer",
                    }
                )
            )
            self._selected_stored_point = point
            self._selected_stored_variant = str(variant)
            self.show_spectrum_button.setEnabled(point.has_spectrum)
            self.node_selected.emit(point)
            self.stored_spectrum_requested.emit(point.index, str(variant))
        elif isinstance(record, (ThatecDevice, ThatecRecord)):
            self.inspector.setPlainText(_format_json(dict(record.values)))
            self.node_selected.emit(record)
        else:
            self.node_selected.emit(None)

    def _render_selected_row(self) -> None:
        record = self._selected_thatec_row
        if not isinstance(record, ThatecRow) or self._selected_path is None:
            return
        checkpoint = self.thatec_checkpoint.value()
        self.inspector.setPlainText(
            _format_json(
                {
                    "definition": record.definition,
                    "metadata": record.metadata,
                    "shape": record.shape,
                    "timestamps": record.timestamp_count,
                    "checkpoint": checkpoint,
                }
            )
        )
        if len(record.shape) >= 2:
            self.values_tree.clear()
            self.spectrum_requested.emit(record.id, checkpoint)
        else:
            series, timestamps = ThatecRunReader.scalar_series(
                self._selected_path, record.id
            )
            self.values_tree.clear()
            for index, value in enumerate(series):
                timestamp = (
                    str(timestamps[index]) if index < len(timestamps) else ""
                )
                self.values_tree.addTopLevelItem(
                    QTreeWidgetItem(
                        [str(index), f"{float(value):.12g}", timestamp]
                    )
                )

    def _show_selected_spectrum(self) -> None:
        if self._selected_thatec_row is not None:
            self.spectrum_requested.emit(
                self._selected_thatec_row.id,
                self.thatec_checkpoint.value(),
            )
        elif self._selected_stored_point is not None:
            self.stored_spectrum_requested.emit(
                self._selected_stored_point.index,
                self._selected_stored_variant,
            )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def find_tree_item(self, row_id: str) -> QTreeWidgetItem | None:
        """Locate a tree item by its THATEC row identifier."""

        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(1, Qt.ItemDataRole.UserRole) == row_id:
                return item
            for index in range(item.childCount()):
                found = walk(item.child(index))
                if found is not None:
                    return found
            return None

        for index in range(self.tree.topLevelItemCount()):
            found = walk(self.tree.topLevelItem(index))
            if found is not None:
                return found
        return None


class SweepTreeDialog(StationDialog):
    """Modal dialog wrapping a :class:`SweepTreePanel`."""

    def __init__(
        self,
        path: Path,
        run: ThatecRun,
        tree: tuple[ThatecTreeNode, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Sweep Tree — {path.name}")
        self.resize(720, 600)

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=10)
        self.panel = SweepTreePanel()
        self.panel.load(path, run, tree)
        layout.addWidget(self.panel, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close = PushButton("Close", surface)
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)


def _format_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
