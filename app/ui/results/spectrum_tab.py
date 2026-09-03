"""Spectrum browsing tab for the Results page."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from math import prod
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QSplitter,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, ComboBox, PushButton, SpinBox, TreeWidget

from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
)
from app.storage import (
    Hdf5RunReader,
    StoredPoint,
    StoredReference,
    StoredSpectrum,
    ThatecRun,
    ThatecRunReader,
    ThatecSpectrum,
)
from app.ui.results.data_classifier import find_scalar_rows, find_spectrum_rows
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask
from app.ui.widgets import SpectrumPlotWidget


_BACKGROUND_SAMPLE_THRESHOLD = 100_000


@dataclass(frozen=True, slots=True)
class PublicCheckpoint:
    """A lightweight checkpoint index for public-only THATEC files."""

    index: int
    timestamp_utc: str | None
    values: dict[str, float]
    spectrum_rows: tuple[str, ...]


_ALL_PARAMETERS = "__all_parameters__"
_ALL_VALUES = "__all_values__"
_ALL_PARAMETER_SETS = "__all_parameter_sets__"


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
        self._public_checkpoints: tuple[PublicCheckpoint, ...] = ()
        self._public_spectrum: ThatecSpectrum | None = None
        self._selected_private_point: StoredPoint | None = None
        self._selected_private_spectrum: StoredSpectrum | None = None
        self._selected_reference: StoredReference | None = None
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

        self.filter_selector = QHBoxLayout()
        filter_selector = self.filter_selector
        filter_selector.addWidget(BodyLabel("Parameter filter:", self))
        self.filter_parameter_combo = ComboBox(self)
        self.filter_parameter_combo.setMinimumWidth(220)
        self.filter_parameter_combo.setAccessibleName("Result parameter filter")
        self.filter_parameter_combo.setToolTip(
            "Limit the checkpoint browser to one exact setpoint or result value."
        )
        filter_selector.addWidget(self.filter_parameter_combo, 1)
        self.filter_value_combo = ComboBox(self)
        self.filter_value_combo.setMinimumWidth(180)
        self.filter_value_combo.setAccessibleName("Result parameter value filter")
        filter_selector.addWidget(self.filter_value_combo, 1)
        self.parameter_set_combo = ComboBox(self)
        self.parameter_set_combo.setMinimumWidth(240)
        self.parameter_set_combo.setAccessibleName("Result parameter set filter")
        self.parameter_set_combo.setToolTip(
            "Choose one exact combination of checkpoint parameters."
        )
        filter_selector.addWidget(self.parameter_set_combo, 1)
        self.clear_filter_button = PushButton("Clear filter", self)
        self.clear_filter_button.setEnabled(False)
        filter_selector.addWidget(self.clear_filter_button)
        self.filter_summary = BodyLabel("All checkpoints", self)
        self.filter_summary.setObjectName("muted")
        self.filter_summary.setWordWrap(True)
        filter_selector.addWidget(self.filter_summary)
        layout.addLayout(filter_selector)

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

        display_selector = QHBoxLayout()
        display_selector.addWidget(BodyLabel("Stored spectrum view:", self))
        self.spectrum_variant_combo = ComboBox(self)
        self.spectrum_variant_combo.setAccessibleName("Stored spectrum view")
        self.spectrum_variant_combo.setToolTip(
            "Choose raw, processed, or reference data for the selected checkpoint."
        )
        display_selector.addWidget(self.spectrum_variant_combo, 1)
        display_selector.addStretch(1)
        layout.addLayout(display_selector)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.points = TreeWidget(self)
        self.points.setHeaderLabels(["Point", "State", "UTC time", "Data"])
        self.points.setMinimumHeight(120)
        self.points.setColumnWidth(0, 70)
        self.points.setColumnWidth(1, 80)
        self.points.setColumnWidth(2, 210)
        splitter.addWidget(self.points)

        navigation = QHBoxLayout()
        self.prev_button = PushButton("← Previous spectrum", self)
        self.next_button = PushButton("Next spectrum →", self)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        navigation.addWidget(self.prev_button)
        navigation.addWidget(self.next_button)
        self.position_label = BodyLabel("No spectrum selected", self)
        self.position_label.setObjectName("muted")
        navigation.addWidget(self.position_label)
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
        self.filter_parameter_combo.currentIndexChanged.connect(
            self._parameter_key_changed
        )
        self.filter_value_combo.currentIndexChanged.connect(
            self._parameter_value_changed
        )
        self.parameter_set_combo.currentIndexChanged.connect(
            self._parameter_set_changed
        )
        self.clear_filter_button.clicked.connect(self.clear_parameter_filter)
        self.thatec_row_combo.currentIndexChanged.connect(self._thatec_row_changed)
        self.show_thatec_button.clicked.connect(self._load_selected_thatec_spectrum)
        self.thatec_trace_combo.currentIndexChanged.connect(self._render_thatec_trace)
        self.spectrum_variant_combo.currentIndexChanged.connect(
            self._render_selected_private_variant
        )
        self.spectrum_plot.status_changed.connect(self._show_plot_status)
        self._show_spectrum_state(
            "Select a spectrum",
            "Choose a public spectrum row or a stored checkpoint to inspect its trace.",
        )
        self._reset_variant_selector()
        self._filter_compact: bool | None = None

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        compact = event.size().width() < 980
        if compact == self._filter_compact:
            return
        self._filter_compact = compact
        self.filter_selector.setDirection(
            QBoxLayout.Direction.TopToBottom
            if compact
            else QBoxLayout.Direction.LeftToRight
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
        self._public_checkpoints = () if points else self._build_public_checkpoints()
        self._populate_points(points if points else self._public_checkpoints)
        self._populate_parameter_filters()
        self._populate_thatec_rows()
        if not points and self.points.topLevelItemCount():
            self.points.setCurrentItem(self.points.topLevelItem(0))
        elif self.thatec_row_combo.count():
            self._load_selected_thatec_spectrum()

    def show_stored_spectrum(self, index: int, variant: str = "raw") -> None:
        """Open a private checkpoint requested by the result tree."""

        def select_visible() -> bool:
            for item_index in range(self.points.topLevelItemCount()):
                item = self.points.topLevelItem(item_index)
                record = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(record, StoredPoint) and record.index == index:
                    self.points.setCurrentItem(item)
                    variant_index = self.spectrum_variant_combo.findData(variant)
                    if variant_index >= 0:
                        self.spectrum_variant_combo.setCurrentIndex(variant_index)
                    return True
            return False

        if select_visible():
            return
        # A tree action should remain reachable even when the Spectrum page is
        # filtered to another parameter set.  Clear that view-local filter and
        # then select the requested immutable checkpoint.
        if self._stored_points:
            self.clear_parameter_filter()
            select_visible()

    def show_reference(self, index: int) -> None:
        """Open one stored Anritsu reference from the Results tree."""

        if self._selected_path is None:
            return
        try:
            reference = Hdf5RunReader.reference(
                self._selected_path, index, max_points=2_000
            )
        except Exception as exc:
            self._show_spectrum_error("Cannot read stored reference", exc)
            return
        if reference is None:
            self._show_spectrum_state(
                "Reference spectrum unavailable",
                f"Reference {index} is not a complete stored spectrum.",
            )
            return
        self._public_spectrum = None
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = reference
        self._reset_variant_selector()
        self.device_state_changed.emit({})
        self.position_label.setText(f"Reference {reference.index}")
        self.spectrum_plot.clear()
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Power", y_unit="dBm"
        )
        self.spectrum_plot.set_trace(
            "Reference spectrum",
            reference.frequencies_hz,
            reference.powers_dbm,
            primary=True,
        )
        self.spectrum_plot.set_title(
            f"Reference {reference.index} ({reference.trace_name})"
        )
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        self.spectrum_info.setText(
            f"Reference {reference.index}; {len(reference.frequencies_hz)} points; "
            f"{reference.kind}, average count {reference.average_count}; "
            f"{reference.acquired_at_utc or 'missing time'}"
        )

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
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = None
        self._reset_variant_selector()
        self.device_state_changed.emit({})
        found_checkpoint = False
        for item_index in range(self.points.topLevelItemCount()):
            item = self.points.topLevelItem(item_index)
            record = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(record, PublicCheckpoint) and record.index == checkpoint:
                self.points.setCurrentItem(item)
                found_checkpoint = True
                break
        if not found_checkpoint and self._public_checkpoints:
            self.clear_parameter_filter()
            for item_index in range(self.points.topLevelItemCount()):
                item = self.points.topLevelItem(item_index)
                record = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(record, PublicCheckpoint) and record.index == checkpoint:
                    self.points.setCurrentItem(item)
                    break
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
        self._public_checkpoints = ()
        self._selected_path = None
        self._run = None
        self._public_spectrum = None
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = None
        self.thatec_row_combo.blockSignals(True)
        self.thatec_row_combo.clear()
        self.thatec_row_combo.blockSignals(False)
        self.thatec_checkpoint.setRange(0, 0)
        self.thatec_trace_combo.blockSignals(True)
        self.thatec_trace_combo.clear()
        self.thatec_trace_combo.blockSignals(False)
        self.thatec_trace_combo.setEnabled(False)
        self.show_thatec_button.setEnabled(False)
        self._reset_parameter_filters()
        self._reset_variant_selector()
        self.position_label.setText("No spectrum selected")

    def _populate_points(
        self,
        points: tuple[StoredPoint | PublicCheckpoint, ...],
    ) -> None:
        self.points.clear()
        self._clear_spectrum()
        items: list[QTreeWidgetItem] = []
        for point in points:
            if isinstance(point, StoredPoint):
                fields = {**point.setpoints, **point.measurements}
                suffix = " · spectrum" if point.has_spectrum else ""
                state = point.status
                timestamp = point.timestamp_utc or "-"
                label = str(point.index)
            else:
                fields = point.values
                suffix = " · public spectrum"
                state = "public"
                timestamp = point.timestamp_utc or "-"
                label = str(point.index)
            item = QTreeWidgetItem(
                [
                    label,
                    state,
                    timestamp,
                    f"{len(fields)} parameters{suffix}",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, point)
            item.setToolTip(3, self._point_tooltip(point))
            items.append(item)

        self.points.setUpdatesEnabled(False)
        try:
            self.points.addTopLevelItems(items)
        finally:
            self.points.setUpdatesEnabled(True)

        self._update_nav_buttons()
        if points:
            self.filter_summary.setText(f"Showing {len(points):,} checkpoint(s)")
        else:
            self.filter_summary.setText("No checkpoints")

    def _build_public_checkpoints(self) -> tuple[PublicCheckpoint, ...]:
        """Build a lazy-indexed checkpoint list for public-only THATEC files."""

        if self._run is None:
            return ()
        spectrum_rows = find_spectrum_rows(self._run)
        if not spectrum_rows:
            return ()
        checkpoint_count = max(row.shape[0] for row in spectrum_rows if row.shape)
        scalar_values: dict[str, tuple[object, object]] = {}
        for row in find_scalar_rows(self._run):
            label = row.control_name or row.device_name or row.id
            if label in scalar_values:
                label = f"{label} [{row.id}]"
            try:
                scalar_values[label] = ThatecRunReader.scalar_series(
                    self._selected_path, row.id
                ) if self._selected_path is not None else ((), ())
            except Exception:
                # A malformed scalar row must not prevent the spectrum rows
                # from being browseable; the row remains visible in the tree.
                continue

        checkpoints: list[PublicCheckpoint] = []
        row_ids = tuple(row.id for row in spectrum_rows)
        for index in range(checkpoint_count):
            values: dict[str, float] = {}
            timestamp_utc: str | None = None
            for label, (series, timestamps) in scalar_values.items():
                if index < len(series):
                    try:
                        value = float(series[index])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value):
                        values[label] = value
                if timestamp_utc is None and index < len(timestamps):
                    try:
                        timestamp = float(timestamps[index])
                        if math.isfinite(timestamp):
                            timestamp_utc = datetime.fromtimestamp(
                                timestamp, timezone.utc
                            ).isoformat()
                    except (TypeError, ValueError, OverflowError, OSError):
                        pass
            checkpoints.append(
                PublicCheckpoint(index, timestamp_utc, values, row_ids)
            )
        return tuple(checkpoints)

    def _populate_parameter_filters(self) -> None:
        records: tuple[StoredPoint | PublicCheckpoint, ...] = (
            self._stored_points or self._public_checkpoints
        )
        keys: set[str] = set()
        for record in records:
            keys.update(self._record_values(record))
        self.filter_parameter_combo.blockSignals(True)
        self.filter_parameter_combo.clear()
        self.filter_parameter_combo.addItem("All parameters", userData=_ALL_PARAMETERS)
        for key in sorted(keys):
            self.filter_parameter_combo.addItem(key, userData=key)
        self.filter_parameter_combo.blockSignals(False)
        self._populate_parameter_sets()
        self._parameter_key_changed()

    def _populate_parameter_values(self) -> None:
        key = self.filter_parameter_combo.currentData()
        records: tuple[StoredPoint | PublicCheckpoint, ...] = (
            self._stored_points or self._public_checkpoints
        )
        values: list[object] = []
        if key not in (None, _ALL_PARAMETERS):
            for record in records:
                value = self._record_values(record).get(str(key))
                if value is not None and not any(
                    self._values_equal(value, candidate) for candidate in values
                ):
                    values.append(value)
        self.filter_value_combo.blockSignals(True)
        self.filter_value_combo.clear()
        self.filter_value_combo.addItem("All values", userData=_ALL_VALUES)
        for value in sorted(values, key=self._format_value):
            self.filter_value_combo.addItem(
                self._format_parameter_value(str(key), value), userData=value
            )
        self.filter_value_combo.blockSignals(False)
        self.clear_filter_button.setEnabled(
            key not in (None, _ALL_PARAMETERS)
            and self.filter_value_combo.currentData() != _ALL_VALUES
        )

    def _populate_parameter_sets(self) -> None:
        records: tuple[StoredPoint | PublicCheckpoint, ...] = (
            self._stored_points or self._public_checkpoints
        )
        signatures: list[tuple[tuple[str, object], ...]] = []
        for record in records:
            signature = self._record_parameter_signature(record)
            if not any(
                self._signatures_equal(signature, candidate)
                for candidate in signatures
            ):
                signatures.append(signature)
        self.parameter_set_combo.blockSignals(True)
        self.parameter_set_combo.clear()
        self.parameter_set_combo.addItem(
            "All parameter sets", userData=_ALL_PARAMETER_SETS
        )
        for signature in sorted(signatures, key=self._format_signature):
            label = "; ".join(
                f"{key}={self._format_parameter_value(key, value)}"
                for key, value in signature
            ) or "No setpoints"
            self.parameter_set_combo.addItem(label, userData=signature)
        self.parameter_set_combo.blockSignals(False)

    def _parameter_value_changed(self, *_args: object) -> None:
        self._select_all_parameter_sets()
        self._parameter_filter_changed()

    def _parameter_set_changed(self, *_args: object) -> None:
        selected = self.parameter_set_combo.currentData()
        if selected not in (None, _ALL_PARAMETER_SETS):
            self.filter_parameter_combo.blockSignals(True)
            self.filter_parameter_combo.setCurrentIndex(0)
            self.filter_parameter_combo.blockSignals(False)
            self.filter_value_combo.blockSignals(True)
            self.filter_value_combo.clear()
            self.filter_value_combo.addItem("All values", userData=_ALL_VALUES)
            self.filter_value_combo.blockSignals(False)
        self._parameter_filter_changed()

    def _select_all_parameter_sets(self) -> None:
        if self.parameter_set_combo.currentData() == _ALL_PARAMETER_SETS:
            return
        self.parameter_set_combo.blockSignals(True)
        self.parameter_set_combo.setCurrentIndex(0)
        self.parameter_set_combo.blockSignals(False)

    def _parameter_key_changed(self, *_args: object) -> None:
        self._select_all_parameter_sets()
        self._populate_parameter_values()
        self._parameter_filter_changed()

    def _parameter_filter_changed(self, *_args: object) -> None:
        key = self.filter_parameter_combo.currentData()
        selected_value = self.filter_value_combo.currentData()
        selected_set = self.parameter_set_combo.currentData()
        records: tuple[StoredPoint | PublicCheckpoint, ...] = (
            self._stored_points or self._public_checkpoints
        )
        if selected_set not in (None, _ALL_PARAMETER_SETS):
            filtered = tuple(
                record
                for record in records
                if self._signatures_equal(
                    self._record_parameter_signature(record), selected_set
                )
            )
        elif key in (None, _ALL_PARAMETERS) or selected_value in (None, _ALL_VALUES):
            filtered = records
        else:
            filtered = tuple(
                record
                for record in records
                if self._values_equal(
                    self._record_values(record).get(str(key)), selected_value
                )
            )
        self.clear_filter_button.setEnabled(
            selected_set not in (None, _ALL_PARAMETER_SETS)
            or (
                key not in (None, _ALL_PARAMETERS)
                and selected_value not in (None, _ALL_VALUES)
            )
        )
        self._populate_points(filtered)
        if selected_set not in (None, _ALL_PARAMETER_SETS):
            self.filter_summary.setText(
                f"Showing {len(filtered):,} of {len(records):,} checkpoint(s)"
            )
        elif key in (None, _ALL_PARAMETERS) or selected_value in (None, _ALL_VALUES):
            self.filter_summary.setText(f"All {len(records):,} checkpoint(s)")
        else:
            self.filter_summary.setText(
                f"Showing {len(filtered):,} of {len(records):,} checkpoint(s)"
            )

    def clear_parameter_filter(self) -> None:
        self.parameter_set_combo.blockSignals(True)
        self.parameter_set_combo.setCurrentIndex(0)
        self.parameter_set_combo.blockSignals(False)
        self.filter_parameter_combo.blockSignals(True)
        self.filter_parameter_combo.setCurrentIndex(0)
        self.filter_parameter_combo.blockSignals(False)
        self._populate_parameter_values()
        self._parameter_filter_changed()

    def _reset_parameter_filters(self) -> None:
        self.filter_parameter_combo.blockSignals(True)
        self.filter_parameter_combo.clear()
        self.filter_parameter_combo.addItem("All parameters", userData=_ALL_PARAMETERS)
        self.filter_parameter_combo.blockSignals(False)
        self.filter_value_combo.blockSignals(True)
        self.filter_value_combo.clear()
        self.filter_value_combo.addItem("All values", userData=_ALL_VALUES)
        self.filter_value_combo.blockSignals(False)
        self.parameter_set_combo.blockSignals(True)
        self.parameter_set_combo.clear()
        self.parameter_set_combo.addItem(
            "All parameter sets", userData=_ALL_PARAMETER_SETS
        )
        self.parameter_set_combo.blockSignals(False)
        self.clear_filter_button.setEnabled(False)
        self.filter_summary.setText("All checkpoints")

    @staticmethod
    def _record_values(record: StoredPoint | PublicCheckpoint) -> dict[str, object]:
        if isinstance(record, StoredPoint):
            return {**record.setpoints, **record.measurements}
        return dict(record.values)

    @staticmethod
    def _record_parameter_signature(
        record: StoredPoint | PublicCheckpoint,
    ) -> tuple[tuple[str, object], ...]:
        values = record.setpoints if isinstance(record, StoredPoint) else record.values
        return tuple(sorted((str(key), value) for key, value in values.items()))

    @classmethod
    def _signatures_equal(
        cls,
        left: tuple[tuple[str, object], ...],
        right: object,
    ) -> bool:
        if not isinstance(right, tuple) or len(left) != len(right):
            return False
        return all(
            str(left_item[0]) == str(right_item[0])
            and cls._values_equal(left_item[1], right_item[1])
            for left_item, right_item in zip(left, right, strict=True)
        )

    @classmethod
    def _format_signature(cls, signature: tuple[tuple[str, object], ...]) -> str:
        return "; ".join(
            f"{key}={cls._format_parameter_value(key, value)}"
            for key, value in signature
        )

    @staticmethod
    def _format_parameter_value(key: str, value: object) -> str:
        """Format SI result values with a unit inferred from the stable key."""

        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return str(value)
        normalized = key.casefold()
        dimension = None
        if "dbm" in normalized:
            dimension = DIMENSION_DBM
        elif "db" in normalized:
            dimension = DIMENSION_DB
        elif normalized.endswith("_hz") or "frequency" in normalized or "(hz)" in normalized:
            dimension = DIMENSION_FREQUENCY
        elif normalized.endswith("_v") or "voltage" in normalized or "(v)" in normalized:
            dimension = DIMENSION_VOLTAGE
        elif normalized.endswith("_a") or "current" in normalized or "(a)" in normalized:
            dimension = DIMENSION_CURRENT
        elif normalized.endswith("_w") or "power" in normalized or "(w)" in normalized:
            dimension = DIMENSION_POWER
        elif normalized.endswith("_ohm") or "resistance" in normalized or "(ohm)" in normalized:
            dimension = DIMENSION_RESISTANCE
        elif normalized.endswith("_s") or "time" in normalized or "(s)" in normalized:
            dimension = DIMENSION_TIME
        elif normalized.endswith("_t") or "field" in normalized or "(t)" in normalized:
            dimension = DIMENSION_MAGNETIC_FIELD
        if dimension is None:
            return SpectrumResultsTab._format_value(value)
        try:
            return format_quantity_auto(float(value), dimension)
        except (TypeError, ValueError):
            return SpectrumResultsTab._format_value(value)

    @staticmethod
    def _values_equal(left: object, right: object) -> bool:
        if left is None or right is None:
            return left is right
        try:
            return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-15)
        except (TypeError, ValueError):
            return str(left) == str(right)

    @staticmethod
    def _format_value(value: object) -> str:
        if isinstance(value, float):
            return f"{value:.12g}"
        return str(value)

    def _populate_thatec_rows(self) -> None:
        self._public_spectrum = None
        self.thatec_row_combo.blockSignals(True)
        self.thatec_row_combo.clear()
        if self._run is not None:
            for row in find_spectrum_rows(self._run):
                label = row.control_name or row.device_name or row.id
                checkpoints = row.shape[0] if row.shape else 0
                sample_shape = " x ".join(str(size) for size in row.shape[1:])
                role = dict(row.definition).get("lab control role", "spectrum")
                variant = "processed" if role == "spectrum_processed" else "raw"
                self.thatec_row_combo.addItem(
                    f"{label} · {variant} ({checkpoints} checkpoints x {sample_shape})",
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
        self._selected_private_point = None
        self._selected_private_spectrum = None
        self._selected_reference = None
        self._reset_variant_selector()
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
        row_count = (
            row.shape[0]
            if row is not None and len(row.shape) >= 2
            else spectrum.checkpoint + 1
        )
        self.position_label.setText(
            f"Public checkpoint {spectrum.checkpoint + 1} / {row_count}"
        )
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
            self._selected_private_point = None
            self._selected_private_spectrum = None
            self._reset_variant_selector()
            self.device_state_changed.emit({})
            return
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(point, PublicCheckpoint):
            self._selected_private_point = None
            self._selected_private_spectrum = None
            self._reset_variant_selector()
            self.device_state_changed.emit({})
            if self.thatec_row_combo.count():
                self.thatec_checkpoint.setValue(
                    max(0, min(point.index, self.thatec_checkpoint.maximum()))
                )
                self._load_selected_thatec_spectrum()
            return
        if not isinstance(point, StoredPoint):
            return
        self._selected_private_point = point
        self._selected_private_spectrum = None
        self._selected_reference = None
        self._update_variant_selector(None)
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
        self._selected_private_point = point
        self._selected_private_spectrum = trace
        self._update_variant_selector(trace)
        self._render_selected_private_variant()

    def _render_selected_private_variant(self, *_args: object) -> None:
        point = self._selected_private_point
        trace = self._selected_private_spectrum
        if point is None or trace is None:
            return
        variant = self.spectrum_variant_combo.currentData() or "raw"
        reference: StoredReference | None = None
        if variant in {"reference", "raw_reference"}:
            reference_index = (
                trace.reference_index if trace.reference_index is not None else 0
            )
            if self._selected_reference is None or self._selected_reference.index != reference_index:
                try:
                    self._selected_reference = Hdf5RunReader.reference(
                        self._selected_path,
                        reference_index,
                        max_points=2_000,
                    ) if self._selected_path is not None else None
                except Exception as exc:
                    self._show_spectrum_error("Cannot read stored reference", exc)
                    return
            reference = self._selected_reference
            if reference is None:
                self._show_spectrum_state(
                    "Reference spectrum unavailable",
                    "This checkpoint does not have a complete stored reference.",
                )
                return

        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Select a stored or public spectrum")
        if variant == "processed":
            if trace.processed_values is None:
                self._show_spectrum_state(
                    "Processed spectrum unavailable",
                    "This checkpoint contains a raw trace but no processed values.",
                )
                return
            self.spectrum_plot.set_labels(
                x="Frequency", x_unit="Hz", y="Processed amplitude",
                y_unit=trace.processed_unit or "",
            )
            self.spectrum_plot.set_trace(
                "Processed spectrum",
                trace.frequencies_hz,
                trace.processed_values,
                primary=True,
            )
        elif variant == "reference":
            assert reference is not None
            self.spectrum_plot.set_labels(
                x="Frequency", x_unit="Hz", y="Power", y_unit="dBm"
            )
            self.spectrum_plot.set_trace(
                "Reference spectrum",
                reference.frequencies_hz,
                reference.powers_dbm,
                primary=True,
            )
        else:
            y_label = "Raw / processed" if variant == "raw_processed" else "Power"
            y_unit = "dBm / dB" if variant == "raw_processed" else "dBm"
            self.spectrum_plot.set_labels(
                x="Frequency", x_unit="Hz", y=y_label, y_unit=y_unit
            )
            self.spectrum_plot.set_trace(
                "Stored spectrum",
                trace.frequencies_hz,
                trace.powers_dbm,
                primary=True,
            )
            if variant == "raw_processed" and trace.processed_values is not None:
                self.spectrum_plot.set_trace(
                    "Processed spectrum",
                    trace.frequencies_hz,
                    trace.processed_values,
                    primary=False,
                )
            elif variant == "raw_reference" and reference is not None:
                self.spectrum_plot.set_trace(
                    "Reference spectrum",
                    reference.frequencies_hz,
                    reference.powers_dbm,
                    primary=False,
                )
        self.spectrum_plot.set_title(
            f"Spectrum at point {point.index} ({trace.trace_name})"
        )
        self.spectrum_plot.auto_range()
        self.spectrum_view.setCurrentWidget(self.spectrum_plot)
        processed_note = (
            f"; processed: {trace.processing_operation} ({trace.processed_unit})"
            if trace.processed_values is not None
            else ""
        )
        self.spectrum_info.setText(
            f"{trace.source_point_count} points in file (raw); view: {variant}; "
            f"{trace.acquired_at_utc or 'missing time'}; "
            f"raw peak {max(trace.powers_dbm):.4g} dBm{processed_note}"
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
            self.position_label.setText("No spectrum selected")
            return
        index = self.points.indexOfTopLevelItem(current)
        self.prev_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < self.points.topLevelItemCount() - 1)
        record = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(record, (StoredPoint, PublicCheckpoint)):
            self.position_label.setText(
                f"Spectrum {index + 1} / {self.points.topLevelItemCount()}"
            )

    def _update_variant_selector(self, trace: StoredSpectrum | None) -> None:
        options: list[tuple[str, str]] = [("Raw spectrum (dBm)", "raw")]
        if trace is not None and trace.processed_values is not None:
            options.extend(
                [
                    ("Processed spectrum", "processed"),
                    ("Raw + processed", "raw_processed"),
                ]
            )
        reference_index = (
            trace.reference_index if trace is not None else None
        )
        if self._selected_path is not None and reference_index is not None:
            try:
                reference = Hdf5RunReader.reference(
                    self._selected_path,
                    reference_index,
                    max_points=32,
                )
            except Exception:
                reference = None
            if reference is not None:
                options.extend(
                    [
                        ("Reference spectrum", "reference"),
                        ("Raw + reference", "raw_reference"),
                    ]
                )
        current = self.spectrum_variant_combo.currentData()
        self.spectrum_variant_combo.blockSignals(True)
        self.spectrum_variant_combo.clear()
        for label, value in options:
            self.spectrum_variant_combo.addItem(label, userData=value)
        selected = self.spectrum_variant_combo.findData(current)
        self.spectrum_variant_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.spectrum_variant_combo.setEnabled(trace is not None)
        self.spectrum_variant_combo.blockSignals(False)

    def _reset_variant_selector(self) -> None:
        self.spectrum_variant_combo.blockSignals(True)
        self.spectrum_variant_combo.clear()
        self.spectrum_variant_combo.addItem("Raw spectrum (dBm)", userData="raw")
        self.spectrum_variant_combo.setCurrentIndex(0)
        self.spectrum_variant_combo.setEnabled(False)
        self.spectrum_variant_combo.blockSignals(False)

    def _clear_spectrum(self) -> None:
        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Select a stored or public spectrum")
        self._reset_variant_selector()
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
    def _point_tooltip(point: StoredPoint | PublicCheckpoint) -> str:
        import json

        if isinstance(point, PublicCheckpoint):
            return json.dumps(
                {
                    "checkpoint": point.index,
                    "timestamp_utc": point.timestamp_utc,
                    "parameters": point.values,
                    "spectrum_rows": point.spectrum_rows,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        payload = {
            "setpoints": point.setpoints,
            "measurements": point.measurements,
            "metadata": point.metadata,
            "device_states": point.device_states,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
