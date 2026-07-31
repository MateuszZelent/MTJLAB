"""Sweep tree panel and dialog for inspecting the THATEC experiment hierarchy."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSplitter,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, PlainTextEdit, PushButton, SpinBox, TreeWidget

from app.storage import (
    ThatecDevice,
    ThatecRecord,
    ThatecRow,
    ThatecRun,
    ThatecRunReader,
    ThatecTreeNode,
    StoredPoint,
    StoredReference,
)
from app.ui.dialogs import StationDialog


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

        splitter = QSplitter(Qt.Orientation.Vertical)

        # --- Tree ---
        self.tree = TreeWidget(self)
        self.tree.setHeaderLabels(["THATEC experiment", "Type"])
        self.tree.setMinimumHeight(160)
        splitter.addWidget(self.tree)

        # --- Inspector + checkpoint ---
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        self.inspector = PlainTextEdit(self)
        self.inspector.setReadOnly(True)
        self.inspector.setMinimumHeight(100)
        bottom_layout.addWidget(self.inspector)

        checkpoint_bar = QHBoxLayout()
        checkpoint_bar.addWidget(BodyLabel("THATEC checkpoint:", self))
        self.thatec_checkpoint = SpinBox(self)
        self.thatec_checkpoint.setMinimum(0)
        checkpoint_bar.addWidget(self.thatec_checkpoint)
        self.show_spectrum_button = PushButton("Show spectrum", self)
        self.show_spectrum_button.setEnabled(False)
        self.show_spectrum_button.setToolTip(
            "Open the selected immutable result in the Spectrum viewer."
        )
        checkpoint_bar.addWidget(self.show_spectrum_button)
        checkpoint_bar.addStretch(1)
        bottom_layout.addLayout(checkpoint_bar)

        self.values_tree = TreeWidget(self)
        self.values_tree.setHeaderLabels(["Checkpoint", "Value", "Timestamp UTC"])
        self.values_tree.setMaximumHeight(150)
        bottom_layout.addWidget(self.values_tree)

        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        # --- Connections ---
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
    ) -> None:
        """Populate the panel from a loaded THATEC result."""
        self._run = run
        self._selected_path = path
        self._selected_thatec_row = None
        self._selected_stored_point = None
        self._selected_stored_variant = "raw"
        self._populate_tree(tree, points=points, references=references)

    def clear(self) -> None:
        """Reset the panel to its empty state."""
        self.tree.clear()
        self.inspector.clear()
        self.values_tree.clear()
        self._run = None
        self._selected_path = None
        self._selected_thatec_row = None
        self._selected_stored_point = None
        self._selected_stored_variant = "raw"
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
        for point in points:
            point_item = QTreeWidgetItem(
                [
                    f"Checkpoint {point.index}",
                    f"{point.status}{' · spectrum' if point.has_spectrum else ''}",
                ]
            )
            point_item.setData(0, Qt.ItemDataRole.UserRole, point)
            checkpoints.addChild(point_item)
            setpoints = QTreeWidgetItem(["Setpoints", str(len(point.setpoints))])
            measurements = QTreeWidgetItem(
                ["Measurements", str(len(point.measurements))]
            )
            point_item.addChildren((setpoints, measurements))
            for key, value in sorted(point.setpoints.items()):
                setpoints.addChild(QTreeWidgetItem([str(key), str(value)]))
            for key, value in sorted(point.measurements.items()):
                measurements.addChild(QTreeWidgetItem([str(key), str(value)]))
            if point.has_spectrum:
                raw_item = QTreeWidgetItem(["Raw spectrum", "private /spectra"])
                raw_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    (point, "raw"),
                )
                point_item.addChild(raw_item)
                processed_item = QTreeWidgetItem(
                    ["Processed spectrum", "private /spectra"]
                )
                processed_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    (point, "processed"),
                )
                point_item.addChild(processed_item)

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

        layout = QVBoxLayout(self)
        self.panel = SweepTreePanel()
        self.panel.load(path, run, tree)
        layout.addWidget(self.panel, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close = PushButton("Close", self)
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)


def _format_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
