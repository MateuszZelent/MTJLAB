"""Spectrum browsing tab for the Results page."""

from __future__ import annotations

from collections.abc import Callable
from math import prod
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSplitter,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, ComboBox, PushButton, SpinBox, TreeWidget

from app.storage import (
    Hdf5RunReader,
    StoredPoint,
    StoredSpectrum,
    ThatecRun,
    ThatecRunReader,
    ThatecSpectrum,
)
from app.ui.results.data_classifier import find_spectrum_rows
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask
from app.ui.widgets import SpectrumPlotWidget


_BACKGROUND_SAMPLE_THRESHOLD = 100_000


class SpectrumResultsTab(QWidget):
    """Browse private Lab Control and public THATEC/PyThat spectra.

    Private files expose individual committed points in ``/points`` and
    ``/spectra``.  Public THATEC/PyThat files instead expose spectral rows in
    ``/measurement``.  Both are read-only and never contact an instrument.
    """

    status_changed = Signal(str)
    device_state_changed = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_path: Path | None = None
        self._run: ThatecRun | None = None
        self._stored_points: tuple[StoredPoint, ...] = ()
        self._public_spectrum: ThatecSpectrum | None = None
        self._read_pool = QThreadPool(self)
        self._read_pool.setMaxThreadCount(1)
        self._read_pool.setExpiryTimeout(15_000)
        self._read_request = 0
        self._active_read_request = 0
        self._read_tasks: dict[int, ResultReadTask] = {}
        self._read_context: dict[int, tuple[str, object]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Public THATEC/PyThat files can contain spectra without the private
        # Lab Control /points group. Browse published rows directly.
        public_selector = QVBoxLayout()
        public_selector.setContentsMargins(0, 0, 0, 0)
        public_selector.setSpacing(4)
        row_selector = QHBoxLayout()
        row_selector.addWidget(BodyLabel("Public spectrum row:", self))
        self.thatec_row_combo = ComboBox(self)
        self.thatec_row_combo.setMinimumWidth(280)
        self.thatec_row_combo.setAccessibleName("Public spectrum row")
        self.thatec_row_combo.setToolTip(
            "Choose a spectral row stored in the public THATEC/PyThat result."
        )
        row_selector.addWidget(self.thatec_row_combo, 1)
        row_selector.addWidget(BodyLabel("Checkpoint:", self))
        self.thatec_checkpoint = SpinBox(self)
        self.thatec_checkpoint.setRange(0, 0)
        self.thatec_checkpoint.setAccessibleName("Spectrum checkpoint")
        row_selector.addWidget(self.thatec_checkpoint)
        self.show_thatec_button = PushButton("Show spectrum", self)
        self.show_thatec_button.setEnabled(False)
        self.show_thatec_button.setToolTip(
            "Read the selected public spectrum without contacting an instrument."
        )
        row_selector.addWidget(self.show_thatec_button)
        public_selector.addLayout(row_selector)

        trace_selector = QHBoxLayout()
        trace_selector.addWidget(BodyLabel("Trace component:", self))
        self.thatec_trace_combo = ComboBox(self)
        self.thatec_trace_combo.setEnabled(False)
        self.thatec_trace_combo.setAccessibleName("Spectrum trace component")
        self.thatec_trace_combo.setToolTip(
            "Choose a channel from a multi-trace public spectrum."
        )
        trace_selector.addWidget(self.thatec_trace_combo, 1)
        public_selector.addLayout(trace_selector)
        layout.addLayout(public_selector)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.points = TreeWidget(self)
        self.points.setHeaderLabels(["Point", "State", "UTC time", "Data"])
        self.points.setMinimumHeight(120)
        self.points.setColumnWidth(0, 70)
        self.points.setColumnWidth(1, 80)
        self.points.setColumnWidth(2, 210)
        splitter.addWidget(self.points)

        navigation = QHBoxLayout()
        self.prev_button = PushButton("Previous", self)
        self.next_button = PushButton("Next", self)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        navigation.addWidget(self.prev_button)
        navigation.addWidget(self.next_button)
        navigation.addStretch(1)
        navigation_widget = QWidget()
        navigation_widget.setLayout(navigation)
        splitter.addWidget(navigation_widget)

        self.spectrum_plot = SpectrumPlotWidget(legend=False)
        self.spectrum_plot.set_title("Select a stored or public spectrum")
        self.spectrum_plot.setMinimumHeight(280)
        self.spectrum_state = ResultsStateCard(self)
        self.spectrum_view = QStackedWidget(self)
        self.spectrum_view.addWidget(self.spectrum_state)
        self.spectrum_view.addWidget(self.spectrum_plot)
        splitter.addWidget(self.spectrum_view)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        layout.addWidget(splitter, 1)

        self.spectrum_info = BodyLabel(
            "Spectra are read from HDF5 without contacting instruments."
        )
        self.spectrum_info.setObjectName("muted")
        self.spectrum_info.setWordWrap(True)
        layout.addWidget(self.spectrum_info)

        self.points.currentItemChanged.connect(self._on_point_selected)
        self.prev_button.clicked.connect(self._go_previous)
        self.next_button.clicked.connect(self._go_next)
        self.thatec_row_combo.currentIndexChanged.connect(self._thatec_row_changed)
        self.show_thatec_button.clicked.connect(self._load_selected_thatec_spectrum)
        self.thatec_trace_combo.currentIndexChanged.connect(self._render_thatec_trace)
        self.spectrum_plot.status_changed.connect(self._show_plot_status)
        self._show_spectrum_state(
            "Select a spectrum",
            "Choose a public spectrum row or a stored checkpoint to inspect its trace.",
        )

    def load(
        self,
        path: Path,
        run: ThatecRun,
        points: tuple[StoredPoint, ...],
    ) -> None:
        """Load private point data and public spectral rows from one file."""

        self._selected_path = path
        self._run = run
        self._stored_points = points
        self._populate_points(points)
        self._populate_thatec_rows()
        if self.thatec_row_combo.count():
            self._load_selected_thatec_spectrum()

    def show_thatec_spectrum(self, row_id: str, checkpoint: int) -> None:
        """Select a public spectrum requested by the tree or heatmap."""

        if self._selected_path is None or self._run is None:
            return
        row_index = self.thatec_row_combo.findData(row_id)
        if row_index < 0:
            self.spectrum_info.setText(
                f"THATEC row {row_id} does not contain a displayable spectrum."
            )
            return
        self.thatec_row_combo.setCurrentIndex(row_index)
        self.thatec_checkpoint.setValue(
            max(0, min(checkpoint, self.thatec_checkpoint.maximum()))
        )
        self._load_selected_thatec_spectrum()

    def clear(self) -> None:
        """Reset the tab to its empty state."""

        self._invalidate_pending_reads()
        self.points.clear()
        self._clear_spectrum()
        self._stored_points = ()
        self._selected_path = None
        self._run = None
        self._public_spectrum = None
        self.thatec_row_combo.blockSignals(True)
        self.thatec_row_combo.clear()
        self.thatec_row_combo.blockSignals(False)
        self.thatec_checkpoint.setRange(0, 0)
        self.thatec_trace_combo.blockSignals(True)
        self.thatec_trace_combo.clear()
        self.thatec_trace_combo.blockSignals(False)
        self.thatec_trace_combo.setEnabled(False)
        self.show_thatec_button.setEnabled(False)

    def _populate_points(self, points: tuple[StoredPoint, ...]) -> None:
        self.points.clear()
        self._clear_spectrum()
        for point in points:
            fields = {**point.setpoints, **point.measurements}
            suffix = " - spectrum" if point.has_spectrum else ""
            item = QTreeWidgetItem(
                [
                    str(point.index),
                    point.status,
                    point.timestamp_utc or "-",
                    f"{len(fields)} values{suffix}",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, point)
            item.setToolTip(3, self._point_tooltip(point))
            self.points.addTopLevelItem(item)

    def _populate_thatec_rows(self) -> None:
        self._public_spectrum = None
        self.thatec_row_combo.blockSignals(True)
        self.thatec_row_combo.clear()
        if self._run is not None:
            for row in find_spectrum_rows(self._run):
                label = row.control_name or row.device_name or row.id
                checkpoints = row.shape[0] if row.shape else 0
                sample_shape = " x ".join(str(size) for size in row.shape[1:])
                self.thatec_row_combo.addItem(
                    f"{label} ({checkpoints} checkpoints x {sample_shape})",
                    userData=row.id,
                )
        self.thatec_row_combo.blockSignals(False)
        self._thatec_row_changed()

    def _thatec_row_changed(self, *_args: object) -> None:
        self._invalidate_pending_reads()
        row_id = self.thatec_row_combo.currentData()
        row = self._run.rows.get(str(row_id)) if self._run is not None else None
        self._public_spectrum = None
        self.thatec_trace_combo.blockSignals(True)
        self.thatec_trace_combo.clear()
        self.thatec_trace_combo.blockSignals(False)
        self.thatec_trace_combo.setEnabled(False)
        if row is None or len(row.shape) < 2:
            self.thatec_checkpoint.setRange(0, 0)
            self.show_thatec_button.setEnabled(False)
            return
        self.thatec_checkpoint.setRange(0, max(0, row.shape[0] - 1))
        self.show_thatec_button.setEnabled(True)

    def _load_selected_thatec_spectrum(self) -> None:
        self._invalidate_pending_reads()
        row_id = self.thatec_row_combo.currentData()
        if self._selected_path is None or row_id is None or self._run is None:
            return
        checkpoint = self.thatec_checkpoint.value()
        row = self._run.rows.get(str(row_id))
        if row is None:
            return
        sample_count = prod(row.shape[1:]) if len(row.shape) >= 2 else 0
        self.show_thatec_button.setEnabled(False)
        self._show_spectrum_state(
            "Loading spectrum",
            f"Reading {sample_count:,} samples from checkpoint {checkpoint}...",
            loading=True,
        )
        if sample_count > _BACKGROUND_SAMPLE_THRESHOLD:
            self._start_read(
                "public",
                (str(row_id), checkpoint),
                ThatecRunReader.spectrum_slice,
                self._selected_path,
                str(row_id),
                checkpoint,
            )
            return
        try:
            spectrum = ThatecRunReader.spectrum_slice(
                self._selected_path, str(row_id), checkpoint
            )
        except Exception as exc:
            self._show_spectrum_error("Cannot read public THATEC/PyThat spectrum", exc)
            return
        self._accept_public_spectrum(spectrum)

    def _accept_public_spectrum(self, spectrum: ThatecSpectrum) -> None:
        self._public_spectrum = spectrum
        self.thatec_trace_combo.blockSignals(True)
        self.thatec_trace_combo.clear()
        for index, trace in enumerate(spectrum.traces):
            self.thatec_trace_combo.addItem(trace.name, userData=index)
        self.thatec_trace_combo.setCurrentIndex(0)
        self.thatec_trace_combo.blockSignals(False)
        self.thatec_trace_combo.setEnabled(bool(spectrum.traces))
        self.show_thatec_button.setEnabled(True)
        self._render_thatec_trace()

    def _render_thatec_trace(self, *_args: object) -> None:
        spectrum = self._public_spectrum
        trace_index = self.thatec_trace_combo.currentData()
        if spectrum is None or trace_index is None:
            return
        try:
            trace = spectrum.traces[int(trace_index)]
        except (IndexError, ValueError):
            return
        self._clear_spectrum()
        self.spectrum_plot.set_labels(
            x=spectrum.x_label,
            x_unit=spectrum.x_unit,
            y=spectrum.y_label,
            y_unit=spectrum.y_unit,
        )
        self.spectrum_plot.set_trace(
            trace.name,
            spectrum.x_values,
            trace.values,
            primary=True,
        )
        row = self._run.rows.get(spectrum.row_id) if self._run is not None else None
        label = row.control_name if row is not None and row.control_name else spectrum.row_id
        self.spectrum_plot.set_title(
            f"{label} - checkpoint {spectrum.checkpoint} - {trace.name}"
        )
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        self.spectrum_info.setText(
            "Public THATEC/PyThat spectrum: "
            f"{label}, checkpoint {spectrum.checkpoint}, {trace.name}, "
            f"{len(spectrum.x_values)} samples from {spectrum.source_shape}."
        )

    def _on_point_selected(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        self._update_nav_buttons()
        self._invalidate_pending_reads()
        self._clear_spectrum()
        if item is None or self._selected_path is None:
            self.device_state_changed.emit({})
            return
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(point, StoredPoint):
            return
        self.device_state_changed.emit(point.device_states)
        if not point.has_spectrum:
            self._show_spectrum_state(
                "No spectrum at this checkpoint",
                "This checkpoint contains scalar data only. Choose another checkpoint.",
            )
            return
        try:
            point_count = Hdf5RunReader.spectrum_point_count(
                self._selected_path, point.index
            )
        except Exception as exc:
            self._show_spectrum_error("Cannot inspect stored spectrum", exc)
            return
        if point_count > _BACKGROUND_SAMPLE_THRESHOLD:
            self._show_spectrum_state(
                "Loading stored spectrum",
                f"Reading and peak-preserving {point_count:,} source points...",
                loading=True,
            )
            self._start_read(
                "private",
                point,
                Hdf5RunReader.spectrum,
                self._selected_path,
                point.index,
                max_points=2_000,
            )
            return
        try:
            trace = Hdf5RunReader.spectrum(
                self._selected_path, point.index, max_points=2_000
            )
        except Exception as exc:
            self._show_spectrum_error("Cannot read stored spectrum", exc)
            return
        if trace is None:
            self._show_spectrum_state(
                "Stored spectrum missing",
                "The checkpoint advertises a spectrum, but no complete trace was found.",
            )
            return
        self._render_stored_spectrum(point, trace)

    def _render_stored_spectrum(
        self, point: StoredPoint, trace: StoredSpectrum
    ) -> None:
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Power", y_unit="dBm"
        )
        self.spectrum_plot.set_trace(
            "Stored spectrum",
            trace.frequencies_hz,
            trace.powers_dbm,
            primary=True,
        )
        self.spectrum_plot.set_title(
            f"Spectrum at point {point.index} ({trace.trace_name})"
        )
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        self.spectrum_info.setText(
            f"{trace.source_point_count} points in file; interactive peak-preserving "
            f"display; {trace.acquired_at_utc or 'missing time'}; "
            f"max {max(trace.powers_dbm):.4g} dBm"
        )

    def _go_previous(self) -> None:
        current = self.points.currentItem()
        if current is None:
            return
        index = self.points.indexOfTopLevelItem(current)
        if index > 0:
            self.points.setCurrentItem(self.points.topLevelItem(index - 1))

    def _go_next(self) -> None:
        current = self.points.currentItem()
        if current is None:
            return
        index = self.points.indexOfTopLevelItem(current)
        if index < self.points.topLevelItemCount() - 1:
            self.points.setCurrentItem(self.points.topLevelItem(index + 1))

    def _update_nav_buttons(self) -> None:
        current = self.points.currentItem()
        if current is None:
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        index = self.points.indexOfTopLevelItem(current)
        self.prev_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < self.points.topLevelItemCount() - 1)

    def _clear_spectrum(self) -> None:
        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Select a stored or public spectrum")
        self._show_spectrum_state(
            "Select a spectrum",
            "Spectra are read from HDF5 without contacting instruments.",
        )

    def _start_read(
        self,
        kind: str,
        context: object,
        operation: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        self._read_request += 1
        request_id = self._read_request
        self._active_read_request = request_id
        task = ResultReadTask(request_id, operation, *args, **kwargs)
        self._read_tasks[request_id] = task
        self._read_context[request_id] = (kind, context)
        task.signals.loaded.connect(self._read_loaded)
        task.signals.failed.connect(self._read_failed)
        task.signals.finished.connect(self._read_finished)
        self._read_pool.start(task)

    def _read_loaded(self, request_id: int, payload: object) -> None:
        if request_id != self._active_read_request:
            return
        kind, context = self._read_context.get(request_id, ("", None))
        if kind == "public" and isinstance(payload, ThatecSpectrum):
            self._accept_public_spectrum(payload)
        elif (
            kind == "private"
            and isinstance(context, StoredPoint)
            and isinstance(payload, StoredSpectrum)
        ):
            self._render_stored_spectrum(context, payload)
        elif kind == "private" and payload is None:
            self._show_spectrum_state(
                "Stored spectrum missing",
                "No complete spectrum is stored for this checkpoint.",
            )
        else:
            self._show_spectrum_state(
                "Unsupported spectrum result",
                "The reader returned data that cannot be plotted safely.",
            )

    def _read_failed(self, request_id: int, message: str) -> None:
        if request_id != self._active_read_request:
            return
        kind, _context = self._read_context.get(request_id, ("", None))
        title = (
            "Cannot read public THATEC/PyThat spectrum"
            if kind == "public"
            else "Cannot read stored spectrum"
        )
        self._show_spectrum_error(title, message)

    def _read_finished(self, request_id: int) -> None:
        self._read_tasks.pop(request_id, None)
        self._read_context.pop(request_id, None)

    def _invalidate_pending_reads(self) -> None:
        for task in self._read_tasks.values():
            task.cancel()
        self._read_pool.clear()
        self._read_tasks.clear()
        self._read_context.clear()
        self._read_request += 1
        self._active_read_request = self._read_request

    def _show_spectrum_state(
        self, title: str, description: str, *, loading: bool = False
    ) -> None:
        self.spectrum_state.show_state(
            title=title,
            description=description,
            accessible_name=title,
            loading=loading,
        )
        self.spectrum_view.setCurrentWidget(self.spectrum_state)
        self.spectrum_info.setText(description)

    def _show_spectrum_error(self, title: str, error: object) -> None:
        self._public_spectrum = None
        self.thatec_trace_combo.clear()
        self.thatec_trace_combo.setEnabled(False)
        self.show_thatec_button.setEnabled(
            self.thatec_row_combo.currentData() is not None
        )
        message = str(error)
        self._show_spectrum_state(title, message)
        self.status_changed.emit(f"{title}: {message}")

    def _show_plot_status(self, message: str) -> None:
        self.spectrum_info.setText(message)
        self.status_changed.emit(message)

    @staticmethod
    def _point_tooltip(point: StoredPoint) -> str:
        import json

        payload = {
            "setpoints": point.setpoints,
            "measurements": point.measurements,
            "metadata": point.metadata,
            "device_states": point.device_states,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
