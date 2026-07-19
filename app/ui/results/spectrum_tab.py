"""Spectrum browsing tab for the Results page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.storage import (
    Hdf5RunReader,
    StoredPoint,
    ThatecRow,
    ThatecRun,
    ThatecRunReader,
)
from app.ui.widgets import SpectrumPlotWidget


class SpectrumResultsTab(QWidget):
    """Browse individual spectra stored in an HDF5 result file.

    Supports both the private ``Hdf5RunReader.spectrum`` API (Lab Control
    HDF5 files with ``/spectra``) and the public THATEC 2-D row format
    (``ThatecRunReader.row_slice``).
    """

    status_changed = Signal(str)
    device_state_changed = Signal(dict)  # emitted with point.device_states on selection

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_path: Path | None = None
        self._run: ThatecRun | None = None
        self._stored_points: tuple[StoredPoint, ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # --- Points list ---
        self.points = QTreeWidget()
        self.points.setHeaderLabels(["Point", "State", "UTC time", "Data"])
        self.points.setMinimumHeight(120)
        self.points.setColumnWidth(0, 70)
        self.points.setColumnWidth(1, 80)
        self.points.setColumnWidth(2, 210)
        splitter.addWidget(self.points)

        # --- Navigation ---
        nav = QHBoxLayout()
        self.prev_button = QPushButton("← Previous")
        self.next_button = QPushButton("Next →")
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        nav.addWidget(self.prev_button)
        nav.addWidget(self.next_button)
        nav.addStretch(1)
        nav_widget = QWidget()
        nav_widget.setLayout(nav)
        splitter.addWidget(nav_widget)

        # --- Spectrum plot ---
        self.spectrum_plot = SpectrumPlotWidget(legend=False)
        self.spectrum_plot.set_title("Select a point containing a stored spectrum")
        self.spectrum_plot.setMinimumHeight(280)
        splitter.addWidget(self.spectrum_plot)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)

        layout.addWidget(splitter, 1)

        self.spectrum_info = QLabel(
            "Spectra are read from HDF5 without contacting instruments."
        )
        self.spectrum_info.setObjectName("muted")
        layout.addWidget(self.spectrum_info)

        # --- Connections ---
        self.points.currentItemChanged.connect(self._on_point_selected)
        self.prev_button.clicked.connect(self._go_previous)
        self.next_button.clicked.connect(self._go_next)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(
        self,
        path: Path,
        run: ThatecRun,
        points: tuple[StoredPoint, ...],
    ) -> None:
        """Load stored points and prepare the spectrum browser."""
        self._selected_path = path
        self._run = run
        self._stored_points = points
        self._populate_points(points)

    def show_thatec_spectrum(self, row_id: str, checkpoint: int) -> None:
        """Display a THATEC 2-D row slice as a spectrum plot.

        Called from the sweep tree panel when a spectral node is selected.
        """
        if self._selected_path is None or self._run is None:
            return
        row = self._run.rows.get(row_id)
        if row is None or len(row.shape) < 2:
            return
        try:
            data = ThatecRunReader.row_slice(self._selected_path, row_id, checkpoint)
        except Exception as exc:
            self.spectrum_info.setText(f"Cannot read THATEC spectrum: {exc}")
            return
        offset, multiplier = (
            (data.scale[0], data.scale[1])
            if len(data.scale) >= 2
            else (0.0, 1.0)
        )
        x_values = tuple(
            offset + multiplier * index for index in range(len(data.values))
        )
        self.spectrum_plot.set_trace(
            "Selected THATEC spectrum",
            x_values,
            tuple(float(v) for v in data.values),
            primary=True,
        )
        label = row.control_name or row_id
        self.spectrum_plot.set_title(f"{label} — checkpoint {checkpoint}")
        self.spectrum_plot.auto_range()
        self.spectrum_info.setText(
            f"THATEC spectrum: {label}, checkpoint {checkpoint}, "
            f"{len(data.values)} points"
        )

    def clear(self) -> None:
        """Reset the tab to its empty state."""
        self.points.clear()
        self._clear_spectrum()
        self._stored_points = ()
        self._selected_path = None
        self._run = None

    # ------------------------------------------------------------------
    # Points list
    # ------------------------------------------------------------------

    def _populate_points(self, points: tuple[StoredPoint, ...]) -> None:
        self.points.clear()
        self._clear_spectrum()
        for point in points:
            fields = {**point.setpoints, **point.measurements}
            suffix = " • spectrum" if point.has_spectrum else ""
            item = QTreeWidgetItem(
                [
                    str(point.index),
                    point.status,
                    point.timestamp_utc or "—",
                    f"{len(fields)} values{suffix}",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, point)
            item.setToolTip(3, self._point_tooltip(point))
            self.points.addTopLevelItem(item)

    # ------------------------------------------------------------------
    # Spectrum display
    # ------------------------------------------------------------------

    def _on_point_selected(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        self._update_nav_buttons()
        self._clear_spectrum()
        if item is None or self._selected_path is None:
            self.device_state_changed.emit({})
            return
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(point, StoredPoint):
            return
        self.device_state_changed.emit(point.device_states)
        if not point.has_spectrum:
            self.spectrum_info.setText("This checkpoint contains no spectrum.")
            return
        try:
            trace = Hdf5RunReader.spectrum(
                self._selected_path, point.index, max_points=2_000
            )
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
        self.spectrum_plot.set_title(
            f"Spectrum at point {point.index} ({trace.trace_name})"
        )
        self.spectrum_plot.auto_range()
        self.spectrum_info.setText(
            f"{trace.source_point_count} points in file; "
            f"interactive peak-preserving display • "
            f"{trace.acquired_at_utc or 'missing time'} • "
            f"max {max(trace.powers_dbm):.4g} dBm"
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

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
        self.next_button.setEnabled(
            index < self.points.topLevelItemCount() - 1
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_spectrum(self) -> None:
        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Select a point containing a stored spectrum")
        self.spectrum_info.setText(
            "Spectra are read from HDF5 without contacting instruments."
        )

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
