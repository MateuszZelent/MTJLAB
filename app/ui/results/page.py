"""Stored-run results page independent of device UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSplitter, QSpinBox, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from app.storage import Hdf5RunReader, RunDetail, StoredPoint, ThatecDevice, ThatecRecord, ThatecRow, ThatecRunReader, ThatecTreeNode, read_pythat_run_data
from app.ui.widgets import SpectrumPlotWidget


class ResultsPage(QWidget):
    """Browse immutable run files without opening an instrument session."""

    resume_requested = Signal(object)
    open_sweep_requested = Signal(object, object)

    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._output_dir = Path(output_dir)
        self._selected_path: Path | None = None
        self._thatec_run = None
        layout = QVBoxLayout(self)
        title = QLabel("Results")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.location = QLabel()
        self.location.setObjectName("muted")
        layout.addWidget(self.location)
        actions = QHBoxLayout()
        refresh = QPushButton("Refresh file list")
        open_file = QPushButton("Open HDF5 file…")
        self.open_sweep_button = QPushButton("Open reconstructed Sweep")
        self.open_sweep_button.setEnabled(False)
        self.open_sweep_button.setToolTip(
            "Build the executed THATEC measurement tree in the Sweeps workspace."
        )
        self.resume_button = QPushButton("Resume from safe checkpoint")
        self.resume_button.setEnabled(False)
        self.resume_button.setToolTip(
            "Available only for interrupted runs containing a confirmed safe boundary."
        )
        actions.addWidget(refresh)
        actions.addWidget(open_file)
        actions.addWidget(self.open_sweep_button)
        actions.addWidget(self.resume_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["File", "State", "Spectra", "Points"])
        self.runs.setMinimumWidth(240)
        self.runs.setColumnWidth(0, 220)
        splitter.addWidget(self.runs)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.experiment_tree = QTreeWidget()
        self.experiment_tree.setHeaderLabels(["THATEC experiment", "Type"])
        self.experiment_tree.setMinimumHeight(180)
        right_layout.addWidget(self.experiment_tree)
        self.inspector = QPlainTextEdit()
        self.inspector.setReadOnly(True)
        self.inspector.setMinimumHeight(130)
        right_layout.addWidget(self.inspector)
        checkpoint_bar = QHBoxLayout()
        checkpoint_bar.addWidget(QLabel("THATEC checkpoint:"))
        self.thatec_checkpoint = QSpinBox()
        self.thatec_checkpoint.setMinimum(0)
        checkpoint_bar.addWidget(self.thatec_checkpoint)
        checkpoint_bar.addStretch(1)
        right_layout.addLayout(checkpoint_bar)
        self.thatec_values = QTreeWidget()
        self.thatec_values.setHeaderLabels(["Checkpoint", "Value", "Timestamp UTC"])
        self.thatec_values.setMaximumHeight(150)
        right_layout.addWidget(self.thatec_values)
        self.details_tabs = QTabWidget()
        self.metadata = QPlainTextEdit()
        self.recipe_snapshot = QPlainTextEdit()
        self.settings_snapshot = QPlainTextEdit()
        self.pythat_data = QPlainTextEdit()
        self.device_state = QPlainTextEdit()
        for widget in (
            self.metadata, self.recipe_snapshot, self.settings_snapshot,
            self.pythat_data, self.device_state,
        ):
            widget.setReadOnly(True)
        self.details_tabs.addTab(self.metadata, "Metadata")
        self.details_tabs.addTab(self.recipe_snapshot, "Recipe")
        self.details_tabs.addTab(self.settings_snapshot, "Settings")
        self.details_tabs.addTab(self.pythat_data, "PyThat data")
        self.details_tabs.addTab(self.device_state, "Device state")
        right_layout.addWidget(self.details_tabs)

        self.points = QTreeWidget()
        self.points.setHeaderLabels(["Point", "State", "UTC time", "Data"])
        self.points.setMinimumHeight(150)
        self.points.setColumnWidth(0, 70)
        self.points.setColumnWidth(1, 80)
        self.points.setColumnWidth(2, 210)
        right_layout.addWidget(self.points)

        self.spectrum_plot = SpectrumPlotWidget(legend=False)
        self.spectrum_plot.set_title("Select a point containing a stored spectrum")
        self.spectrum_plot.setMinimumHeight(280)
        right_layout.addWidget(self.spectrum_plot, 1)
        self.spectrum_info = QLabel("Spectra are read from HDF5 without contacting instruments.")
        self.spectrum_info.setObjectName("muted")
        right_layout.addWidget(self.spectrum_info)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        refresh.clicked.connect(self.refresh)
        open_file.clicked.connect(self.browse_result_file)
        self.resume_button.clicked.connect(self._request_resume)
        self.open_sweep_button.clicked.connect(self._request_open_sweep)
        self.runs.currentItemChanged.connect(self._run_selected)
        self.points.currentItemChanged.connect(self._point_selected)
        self.experiment_tree.currentItemChanged.connect(self._tree_selected)
        self.thatec_checkpoint.valueChanged.connect(lambda _value: self._render_selected_thatec_row())
        self.refresh()

    def refresh(self) -> None:
        self.location.setText(f"Directory: {self._output_dir.resolve()}")
        self.resume_button.setEnabled(False)
        self.open_sweep_button.setEnabled(False)
        self.runs.clear()
        self.points.clear()
        self.experiment_tree.clear()
        self._clear_details()
        for summary in Hdf5RunReader.list_runs(self._output_dir):
            item = QTreeWidgetItem(
                [
                    summary.path.name,
                    summary.status,
                    str(summary.spectrum_count),
                    str(summary.point_count),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, str(summary.path))
            item.setToolTip(0, str(summary.path.resolve()))
            self.runs.addTopLevelItem(item)
        if self.runs.topLevelItemCount() == 0:
            self.metadata.setPlainText("No HDF5 files in the results directory.")
        elif self._selected_path is not None:
            for index in range(self.runs.topLevelItemCount()):
                candidate = self.runs.topLevelItem(index)
                if Path(str(candidate.data(0, Qt.ItemDataRole.UserRole))) == self._selected_path:
                    self.runs.setCurrentItem(candidate)
                    break

    def browse_result_file(self) -> None:
        """Select a THATEC-compatible result without copying it to measurements."""
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Open THATEC HDF5 result",
            str(self._output_dir),
            "HDF5 results (*.h5 *.hdf5);;All files (*)",
        )
        if selected:
            self.open_result_file(selected)

    def open_result_file(self, path: str | Path) -> None:
        """Add and open an arbitrary public THATEC result file in this session."""
        target = Path(path)
        try:
            run = ThatecRunReader.describe(target)
        except Exception as exc:
            self.metadata.setPlainText(f"Cannot read result:\n{exc}")
            return
        existing = next(
            (
                self.runs.topLevelItem(index)
                for index in range(self.runs.topLevelItemCount())
                if Path(str(self.runs.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole))) == target
            ),
            None,
        )
        if existing is None:
            point_count = max((row.shape[0] for row in run.rows.values() if row.shape), default=0)
            spectrum_count = sum(1 for row in run.rows.values() if len(row.shape) >= 2)
            existing = QTreeWidgetItem([target.name, "THATEC", str(spectrum_count), str(point_count)])
            existing.setData(0, Qt.ItemDataRole.UserRole, str(target))
            existing.setToolTip(0, str(target.resolve()))
            self.runs.addTopLevelItem(existing)
        self.runs.setCurrentItem(existing)

    def _run_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self.points.clear()
        self._clear_spectrum()
        if item is None:
            self.resume_button.setEnabled(False)
            self.open_sweep_button.setEnabled(False)
            return
        path = Path(str(item.data(0, Qt.ItemDataRole.UserRole)))
        self._selected_path = path
        try:
            self._thatec_run = ThatecRunReader.describe(path)
            tree = ThatecRunReader.tree(path)
        except Exception as exc:
            self.resume_button.setEnabled(False)
            self.metadata.setPlainText(f"Cannot read result:\n{exc}")
            self.recipe_snapshot.clear()
            self.settings_snapshot.clear()
            self.pythat_data.clear()
            self.device_state.clear()
            return
        self._populate_experiment_tree(tree)
        self.open_sweep_button.setEnabled(True)
        try:
            detail = Hdf5RunReader.detail(path)
            points = Hdf5RunReader.points(path)
            pythat_data = read_pythat_run_data(path)
        except Exception:
            detail = None
            points = ()
            pythat_data = None
        self.resume_button.setEnabled(bool(detail and detail.summary.status in {"aborted", "faulted", "incomplete"}))
        if detail is not None:
            self._show_detail(detail)
        else:
            self.metadata.setPlainText(
                f"Public THATEC file: {path}\nRows: {len(self._thatec_run.rows)}\n"
                f"Devices: {len(self._thatec_run.devices)}"
            )
            self.recipe_snapshot.clear()
            self.settings_snapshot.clear()
        if pythat_data is not None:
            self.pythat_data.setPlainText(
                "Dimensions:\n" + self._format_json(pythat_data.dimensions)
                + "\n\nVariables:\n" + "\n".join(pythat_data.variables)
            )
        else:
            self.pythat_data.setPlainText("Public THATEC tree loaded directly; no private application metadata required.")
        for point in points:
            fields = {**point.setpoints, **point.measurements}
            suffix = " • spectrum" if point.has_spectrum else ""
            point_item = QTreeWidgetItem(
                [
                    str(point.index),
                    point.status,
                    point.timestamp_utc or "—",
                    f"{len(fields)} values{suffix}",
                ]
            )
            point_item.setData(0, Qt.ItemDataRole.UserRole, point)
            point_item.setToolTip(3, self._point_tooltip(point))
            self.points.addTopLevelItem(point_item)

    def _request_open_sweep(self) -> None:
        """Open the public THATEC execution tree in the dedicated Sweep workspace."""
        if self._thatec_run is None or self._selected_path is None:
            return
        try:
            tree = ThatecRunReader.tree(self._selected_path)
        except Exception as exc:
            self.metadata.setPlainText(f"Cannot reconstruct THATEC Sweep:\n{exc}")
            return
        self.open_sweep_requested.emit(self._thatec_run, tree)

    def _populate_experiment_tree(self, tree: tuple[ThatecTreeNode, ...]) -> None:
        self.experiment_tree.clear()
        measurements = QTreeWidgetItem(["Measurements", "THATEC tree"])
        self.experiment_tree.addTopLevelItem(measurements)
        for node in tree:
            self._add_thatec_tree_node(measurements, node)
        for title, records in (
            ("Devices", self._thatec_run.devices),
            ("Labbook", self._thatec_run.labbook),
            ("Post-process", self._thatec_run.post_process),
        ):
            section = QTreeWidgetItem([title, "THATEC"])
            self.experiment_tree.addTopLevelItem(section)
            for record in records:
                item = QTreeWidgetItem([record.name if isinstance(record, ThatecDevice) else record.id, "record"])
                item.setData(0, Qt.ItemDataRole.UserRole, record)
                section.addChild(item)
        self.experiment_tree.expandToDepth(1)

    def _add_thatec_tree_node(self, parent: QTreeWidgetItem, node: ThatecTreeNode) -> None:
        item = QTreeWidgetItem([node.label, node.kind])
        item.setData(0, Qt.ItemDataRole.UserRole, self._thatec_run.rows.get(node.id))
        item.setData(1, Qt.ItemDataRole.UserRole, node.id)
        parent.addChild(item)
        for child in node.children:
            self._add_thatec_tree_node(item, child)

    def _find_tree_item(self, row_id: str) -> QTreeWidgetItem | None:
        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(1, Qt.ItemDataRole.UserRole) == row_id:
                return item
            for index in range(item.childCount()):
                found = walk(item.child(index))
                if found is not None:
                    return found
            return None
        for index in range(self.experiment_tree.topLevelItemCount()):
            found = walk(self.experiment_tree.topLevelItem(index))
            if found is not None:
                return found
        return None

    def _tree_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        record = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(record, ThatecRow) and self._selected_path is not None:
            self._selected_thatec_row = record
            if not record.shape:
                self.inspector.setPlainText(
                    self._format_json(
                        {
                            "definition": record.definition,
                            "metadata": record.metadata,
                            "recorded_data": "No measurement array for this THATEC control/internal node.",
                        }
                    )
                )
                self.thatec_values.clear()
                self._clear_spectrum()
                return
            self.thatec_checkpoint.setMaximum(max(0, record.shape[0] - 1 if record.shape else 0))
            self._render_selected_thatec_row()
        elif isinstance(record, (ThatecDevice, ThatecRecord)):
            self.inspector.setPlainText(self._format_json(dict(record.values)))

    def _render_selected_thatec_row(self) -> None:
        record = getattr(self, "_selected_thatec_row", None)
        if not isinstance(record, ThatecRow) or self._selected_path is None:
            return
        checkpoint = self.thatec_checkpoint.value()
        self.inspector.setPlainText(self._format_json({"definition": record.definition, "metadata": record.metadata, "shape": record.shape, "timestamps": record.timestamp_count, "checkpoint": checkpoint}))
        values = ThatecRunReader.row_slice(self._selected_path, record.id, checkpoint).values
        if len(record.shape) == 2:
            self.thatec_values.clear()
            data = ThatecRunReader.row_slice(self._selected_path, record.id, checkpoint)
            offset, multiplier = (data.scale[0], data.scale[1]) if len(data.scale) >= 2 else (0.0, 1.0)
            x_values = tuple(offset + multiplier * index for index in range(len(data.values)))
            self.spectrum_plot.set_trace("Selected THATEC spectrum", x_values, tuple(float(value) for value in data.values), primary=True)
            self.spectrum_plot.set_title(f"{record.control_name or record.id} — checkpoint {checkpoint}")
            self.spectrum_plot.auto_range()
        else:
            series, timestamps = ThatecRunReader.scalar_series(self._selected_path, record.id)
            self.thatec_values.clear()
            for index, value in enumerate(series):
                timestamp = str(timestamps[index]) if index < len(timestamps) else ""
                self.thatec_values.addTopLevelItem(
                    QTreeWidgetItem([str(index), f"{float(value):.12g}", timestamp])
                )
            x_values = tuple(float(value) for value in (timestamps if len(timestamps) else range(len(series))))
            self.spectrum_plot.set_trace("Selected THATEC scalar", x_values, tuple(float(value) for value in series), primary=True)
            self.spectrum_plot.set_title(f"{record.control_name or record.id} — {len(series)} checkpoints")
            self.spectrum_plot.auto_range()
            self.inspector.append("\n\nSelected value:\n" + self._format_json([float(value) for value in values]))

    def _request_resume(self) -> None:
        if self._selected_path is not None and self.resume_button.isEnabled():
            self.resume_requested.emit(self._selected_path)

    def _show_detail(self, detail: RunDetail) -> None:
        summary = detail.summary
        lines = [
            f"File: {summary.path}",
            f"State: {summary.status}",
            f"Created (UTC): {summary.created_at_utc or 'missing'}",
            f"Application version: {summary.application_version or 'missing'}",
            f"Plan hash: {summary.plan_sha256 or 'missing'}",
            f"Checkpoints: {summary.point_count}; stored spectra: {summary.spectrum_count}",
            "",
            "Instrument identities:",
        ]
        lines.extend(f"  {name}: {idn}" for name, idn in sorted(detail.device_idn.items()))
        lines.extend(("", "Authenticated operator:", self._format_json(detail.operator_context)))
        lines.extend(("", "Capabilities (snapshot):", self._format_json(detail.capabilities)))
        if detail.events:
            lines.extend(("", f"Recent events ({len(detail.events)}):"))
            lines.extend(
                f"  {event.timestamp_utc} [{event.severity}] {event.name}"
                for event in detail.events[-20:]
            )
        self.metadata.setPlainText("\n".join(lines))
        self.recipe_snapshot.setPlainText(detail.recipe_yaml)
        self.settings_snapshot.setPlainText(detail.settings_yaml)

    @staticmethod
    def _format_json(value: object) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

    def _point_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self._clear_spectrum()
        if item is None or self._selected_path is None:
            self.device_state.clear()
            return
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(point, StoredPoint):
            return
        self.device_state.setPlainText(self._format_json(point.device_states))
        if not point.has_spectrum:
            self.spectrum_info.setText("This checkpoint contains no spectrum.")
            return
        try:
            trace = Hdf5RunReader.spectrum(self._selected_path, point.index, max_points=2_000)
        except Exception as exc:
            self.spectrum_info.setText(f"Cannot read spectrum: {exc}")
            return
        if trace is None:
            self.spectrum_info.setText("No spectrum for the selected checkpoint.")
            return
        self.spectrum_plot.set_trace(
            "Stored spectrum",
            trace.frequencies_hz,
            trace.powers_dbm,
            primary=True,
        )
        self.spectrum_plot.set_title(f"Spectrum at point {point.index} ({trace.trace_name})")
        self.spectrum_plot.auto_range()
        self.spectrum_info.setText(
            f"{trace.source_point_count} points in file; interactive peak-preserving display • "
            f"{trace.acquired_at_utc or 'missing time'} • max {max(trace.powers_dbm):.4g} dBm"
        )

    @staticmethod
    def _point_tooltip(point: StoredPoint) -> str:
        payload = {
            "setpoints": point.setpoints,
            "measurements": point.measurements,
            "metadata": point.metadata,
            "device_states": point.device_states,
        }
        return ResultsPage._format_json(payload)

    def _clear_details(self) -> None:
        self.metadata.clear()
        self.recipe_snapshot.clear()
        self.settings_snapshot.clear()
        self.pythat_data.clear()
        self.device_state.clear()
        self._clear_spectrum()

    def _clear_spectrum(self) -> None:
        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Select a point containing a stored spectrum")
        self.spectrum_info.setText("Spectra are read from HDF5 without contacting instruments.")
