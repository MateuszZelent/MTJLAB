"""Heatmap visualization tab for spectral sweep results."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter, SVGExporter
from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, ComboBox, PushButton
from app.ui.dialogs import StationFileDialog as QFileDialog

from app.storage import StoredPoint, ThatecRow, ThatecRun, ThatecRunReader
from app.ui.design_system import plot_theme, tokens_for
from app.ui.results.heatmap_coordinates import (
    HeatmapCoordinates,
    HeatmapRequest,
    build_heatmap_coordinates,
    read_heatmap_matrix,
)
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask


_BACKGROUND_MATRIX_THRESHOLD = 100_000


def _axis_export_label(label: str, unit: str) -> str:
    return f"{label} ({unit})" if unit else label


@dataclass(frozen=True, slots=True)
class _HeatmapPayload:
    matrix: np.ndarray
    x_values: np.ndarray
    y_values: np.ndarray
    x_label: str
    x_unit: str
    y_label: str
    y_unit: str
    z_label: str
    z_unit: str
    levels: tuple[float, float]
    missing_checkpoints: int
    cell_checkpoints: np.ndarray


def _read_heatmap_payload(
    path: Path,
    row: ThatecRow,
    coordinates: HeatmapCoordinates,
    request: HeatmapRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> _HeatmapPayload:
    """Read the exact requested physical-coordinate plane."""

    matrix = read_heatmap_matrix(path, row, coordinates, request, cancelled=cancelled)
    return _HeatmapPayload(
        matrix=matrix.values,
        x_values=matrix.x_values,
        y_values=matrix.y_values,
        x_label=matrix.x_label,
        x_unit=matrix.x_unit,
        y_label=matrix.y_label,
        y_unit=matrix.y_unit,
        z_label=matrix.z_label,
        z_unit=matrix.z_unit,
        levels=(float(np.nanmin(matrix.values)), float(np.nanmax(matrix.values))),
        missing_checkpoints=matrix.missing_checkpoints,
        cell_checkpoints=matrix.cell_checkpoints,
    )


class HeatmapPlotWidget(QWidget):
    """Interactive 2-D heatmap built on ``pyqtgraph.PColorMeshItem``.

    Axes:
        X — frequency (or spectrum sample index), derived from THATEC scale.
        Y — checkpoint index (or swept parameter value when available).
        Color — measured amplitude (typically dBm).
    """

    checkpoint_clicked = Signal(int)  # emitted for the source checkpoint of a cell
    status_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: np.ndarray | None = None  # shape (Y coordinates, X coordinates)
        self._x_values: np.ndarray | None = None
        self._y_values: np.ndarray | None = None
        self._x_edges: np.ndarray | None = None
        self._y_edges: np.ndarray | None = None
        self._x_export_label = "X"
        self._y_export_label = "Y"
        self._cell_checkpoint_indices: np.ndarray | None = None
        self._last_readout_cell: tuple[int, int] | None = None
        self._theme_name = "dark"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # --- Toolbar ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self.colormap_combo = ComboBox(self)
        self.colormap_combo.addItems(
            ["viridis", "inferno", "plasma", "magma", "cividis", "turbo", "hot"]
        )
        self.colormap_combo.setToolTip("Select color palette")
        toolbar.addWidget(BodyLabel("Palette:"))
        toolbar.addWidget(self.colormap_combo)

        auto_range_btn = PushButton(parent=self)
        auto_range_btn.setText("Auto range")
        auto_range_btn.setToolTip("Reset zoom and show all data")
        auto_range_btn.setObjectName("plotToolButton")
        auto_range_btn.clicked.connect(self.auto_range)
        toolbar.addWidget(auto_range_btn)

        export_btn = PushButton(parent=self)
        export_btn.setText("Export")
        export_btn.setToolTip("Export heatmap to PNG, SVG or CSV")
        export_btn.setObjectName("plotToolButton")
        export_btn.clicked.connect(self.export)
        toolbar.addWidget(export_btn)

        toolbar.addStretch(1)
        self.readout = BodyLabel("X: —   Y: —   Z: —")
        self.readout.setObjectName("plotReadout")
        toolbar.addWidget(self.readout)
        layout.addLayout(toolbar)

        # --- Plot ---
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Checkpoint")
        self.plot.setMenuEnabled(True)
        self.plot.setMouseEnabled(x=True, y=True)

        self.image_item = pg.PColorMeshItem()
        self.plot.addItem(self.image_item)

        # Color bar
        self.color_bar = pg.ColorBarItem(
            interactive=True,
            orientation="right",
            label="Amplitude (dBm)",
        )
        self.color_bar.setImageItem(self.image_item, insert_in=self.plot.getPlotItem())

        layout.addWidget(self.plot, 1)

        # --- Crosshair ---
        self.crosshair_x = pg.InfiniteLine(
            angle=90, movable=False
        )
        self.crosshair_y = pg.InfiniteLine(
            angle=0, movable=False
        )
        self.plot.addItem(self.crosshair_x, ignoreBounds=True)
        self.plot.addItem(self.crosshair_y, ignoreBounds=True)

        self._mouse_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved
        )
        self.plot.scene().sigMouseClicked.connect(self._mouse_clicked)
        self.colormap_combo.currentTextChanged.connect(self._apply_colormap)
        self.apply_theme(self._theme_name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_data(
        self,
        data: np.ndarray,
        x_values: np.ndarray | None = None,
        y_values: np.ndarray | None = None,
        *,
        x_label: str = "Frequency",
        x_unit: str = "Hz",
        y_label: str = "Checkpoint",
        y_unit: str = "",
        z_label: str = "Amplitude (dBm)",
        levels: tuple[float, float] | None = None,
        cell_checkpoint_indices: np.ndarray | None = None,
    ) -> None:
        """Set the 2-D data matrix and update the plot.

        Parameters
        ----------
        data : ndarray, shape (rows, cols)
            The 2-D heatmap matrix (e.g. checkpoints × frequency bins).
        x_values, y_values : optional axis value arrays
            Physical values for the horizontal and vertical axes.
        """
        matrix = np.asarray(data, dtype=float)
        if matrix.ndim != 2 or 0 in matrix.shape:
            raise ValueError("Heatmap data must be a non-empty two-dimensional matrix.")
        rows, cols = matrix.shape
        x_axis = (
            np.asarray(x_values, dtype=float)
            if x_values is not None
            else np.arange(cols, dtype=float)
        )
        y_axis = (
            np.asarray(y_values, dtype=float)
            if y_values is not None
            else np.arange(rows, dtype=float)
        )
        if x_axis.ndim != 1 or x_axis.size != cols:
            raise ValueError("Heatmap X coordinates must match the matrix columns.")
        if y_axis.ndim != 1 or y_axis.size != rows:
            raise ValueError("Heatmap Y coordinates must match the matrix rows.")
        if not np.all(np.isfinite(x_axis)) or not np.all(np.isfinite(y_axis)):
            raise ValueError("Heatmap coordinate axes must contain only finite values.")

        x_delta = np.diff(x_axis)
        y_delta = np.diff(y_axis)
        if x_delta.size and not (np.all(x_delta > 0) or np.all(x_delta < 0)):
            raise ValueError("Heatmap X coordinates must be strictly monotonic.")
        if y_delta.size and not (np.all(y_delta > 0) or np.all(y_delta < 0)):
            raise ValueError("Heatmap Y coordinates must be strictly monotonic.")
        checkpoint_indices = (
            np.asarray(cell_checkpoint_indices, dtype=int)
            if cell_checkpoint_indices is not None
            else np.broadcast_to(np.arange(rows, dtype=int)[:, None], matrix.shape).copy()
        )
        if checkpoint_indices.shape != matrix.shape:
            raise ValueError("Heatmap checkpoint mapping must match the data matrix.")
        if x_delta.size and np.all(x_delta < 0):
            x_axis = x_axis[::-1]
            matrix = matrix[:, ::-1]
            checkpoint_indices = checkpoint_indices[:, ::-1]
        if y_delta.size and np.all(y_delta < 0):
            y_axis = y_axis[::-1]
            matrix = matrix[::-1, :]
            checkpoint_indices = checkpoint_indices[::-1, :]
        x_edges = self._cell_edges(x_axis)
        y_edges = self._cell_edges(y_axis)
        self._data = matrix
        self._x_values = x_axis
        self._y_values = y_axis
        self._x_edges = x_edges
        self._y_edges = y_edges
        self._x_export_label = _axis_export_label(x_label, x_unit)
        self._y_export_label = _axis_export_label(y_label, y_unit)
        self._cell_checkpoint_indices = checkpoint_indices
        self._last_readout_cell = None

        # PColorMeshItem accepts the physical cell vertices directly.  An
        # ImageItem can only apply one affine scale, which silently distorts
        # logarithmic and otherwise nonuniform sweep axes.
        x_vertices, y_vertices = np.meshgrid(self._x_edges, self._y_edges)
        self.image_item.setData(
            x_vertices,
            y_vertices,
            self._data,
            autoLevels=False,
        )

        self.plot.setLabel("bottom", x_label, units=x_unit)
        self.plot.setLabel("left", y_label, units=y_unit if y_unit else None)
        self.color_bar.setLabel("right", text=z_label)

        self._apply_colormap(self.colormap_combo.currentText())
        if levels is None:
            finite = self._data[np.isfinite(self._data)]
            if finite.size == 0:
                raise ValueError("Heatmap contains no finite values.")
            levels = (float(finite.min()), float(finite.max()))
        low, high = levels
        if not np.isfinite(low) or not np.isfinite(high) or low > high:
            raise ValueError("Heatmap colour levels must be finite and ordered.")
        if low == high:
            padding = max(abs(low) * 1e-12, 1e-12)
            low, high = low - padding, high + padding
        self.color_bar.setLevels((low, high))
        self.image_item.setLevels((low, high))
        self.auto_range()

    def clear(self) -> None:
        self.image_item.setData()
        self._data = None
        self._x_values = None
        self._y_values = None
        self._x_edges = None
        self._y_edges = None
        self._cell_checkpoint_indices = None
        self._last_readout_cell = None
        self._x_export_label = "X"
        self._y_export_label = "Y"
        self.readout.setText("X: —   Y: —   Z: —")

    def auto_range(self) -> None:
        self.plot.enableAutoRange()
        self.plot.autoRange()

    def apply_theme(self, theme: str) -> None:
        self._theme_name = theme
        palette = plot_theme(tokens_for(theme))
        self.plot.setBackground(palette.background)
        for axis in ("left", "bottom"):
            item = self.plot.getAxis(axis)
            item.setPen(pg.mkPen(palette.axes))
            item.setTextPen(pg.mkPen(palette.axes))
        self.crosshair_x.setPen(pg.mkPen(palette.grid, width=1))
        self.crosshair_y.setPen(pg.mkPen(palette.grid, width=1))

    def export(self) -> None:
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export heatmap",
            "heatmap.png",
            "PNG (*.png);;SVG (*.svg);;CSV (*.csv)",
        )
        if not path:
            return
        suffix = Path(path).suffix.lower()
        try:
            if "PNG" in selected or suffix == ".png":
                ImageExporter(self.plot.plotItem).export(path)
            elif "SVG" in selected or suffix == ".svg":
                SVGExporter(self.plot.plotItem).export(path)
            else:
                self._export_csv(Path(path))
        except Exception as exc:
            self.status_changed.emit(f"Heatmap export failed: {exc}")
            return
        self.status_changed.emit(f"Heatmap exported to {path}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_colormap(self, name: str) -> None:
        try:
            cmap = pg.colormap.get(name)
        except Exception:
            cmap = pg.colormap.get("viridis")
        self.color_bar.setColorMap(cmap)

    @staticmethod
    def _cell_edges(values: np.ndarray) -> np.ndarray:
        """Return physical boundaries around coordinate values treated as centers."""

        if values.size == 1:
            half_width = max(abs(float(values[0])) * 1e-9, 1.0)
            return np.asarray(
                (values[0] - half_width, values[0] + half_width),
                dtype=float,
            )
        midpoints = (values[:-1] + values[1:]) / 2.0
        first = values[0] - (midpoints[0] - values[0])
        last = values[-1] + (values[-1] - midpoints[-1])
        return np.concatenate((np.asarray((first,), dtype=float), midpoints, np.asarray((last,), dtype=float)))

    def _mouse_moved(self, event: tuple[object, ...]) -> None:
        position = event[0]
        if not self.plot.sceneBoundingRect().contains(position):
            return
        point = self.plot.plotItem.vb.mapSceneToView(position)
        x, y = float(point.x()), float(point.y())
        self.crosshair_x.setPos(x)
        self.crosshair_y.setPos(y)
        cell = self._cell_indices(x, y)
        if cell == self._last_readout_cell:
            return
        self._last_readout_cell = cell
        if cell is None:
            self.readout.setText("X: -   Y: -   Z: -")
            return
        display_x = float(self._x_values[cell[1]])
        display_y = float(self._y_values[cell[0]])
        z = float(self._data[cell[0], cell[1]])
        z_text = f"{z:.4g}" if np.isfinite(z) else "-"
        self.readout.setText(
            f"X: {display_x:.6g}   Y: {display_y:.4g}   Z: {z_text}"
        )

    def _mouse_clicked(self, event: Any) -> None:
        if (
            event.button() != 1
            or self._data is None
            or self._y_values is None
            or self._cell_checkpoint_indices is None
        ):
            return
        position = event.scenePos()
        if not self.plot.sceneBoundingRect().contains(position):
            return
        point = self.plot.plotItem.vb.mapSceneToView(position)
        cell = self._cell_indices(float(point.x()), float(point.y()))
        if cell is None:
            return
        checkpoint = int(self._cell_checkpoint_indices[cell])
        if checkpoint >= 0:
            self.checkpoint_clicked.emit(checkpoint)

    def _interpolate_z(self, x: float, y: float) -> float | None:
        cell = self._cell_indices(x, y)
        return None if cell is None else float(self._data[cell[0], cell[1]])

    def _cell_indices(self, x: float, y: float) -> tuple[int, int] | None:
        if (
            self._data is None
            or self._x_values is None
            or self._y_values is None
            or self._x_edges is None
            or self._y_edges is None
        ):
            return None
        if (
            x < self._x_edges[0]
            or x > self._x_edges[-1]
            or y < self._y_edges[0]
            or y > self._y_edges[-1]
        ):
            return None
        col = int(np.searchsorted(self._x_edges, x, side="right") - 1)
        row = int(np.searchsorted(self._y_edges, y, side="right") - 1)
        col = max(0, min(col, self._data.shape[1] - 1))
        row = max(0, min(row, self._data.shape[0] - 1))
        return row, col

    def _export_csv(self, path: Path) -> None:
        if self._data is None or self._x_values is None or self._y_values is None:
            return
        # QFileDialog already captured the user's explicit overwrite decision.
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            header = [self._y_export_label] + [
                f"{float(x):.9g}" for x in self._x_values
            ]
            header[0] = f"{header[0]} / {self._x_export_label}"
            writer.writerow(header)
            for row_idx in range(self._data.shape[0]):
                row_values = [f"{float(self._y_values[row_idx]):.9g}"] + [
                    f"{float(v):.9g}" for v in self._data[row_idx]
                ]
                writer.writerow(row_values)


class HeatmapResultsTab(QWidget):
    """Tab wrapping :class:`HeatmapPlotWidget` with THATEC row selection."""

    checkpoint_clicked = Signal(str, int)  # (row_id, checkpoint)
    status_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_path: Path | None = None
        self._run: ThatecRun | None = None
        self._points: tuple[StoredPoint, ...] = ()
        self._coordinates: HeatmapCoordinates | None = None
        self._coordinate_row_id: str | None = None
        self._spectrum_rows: list[ThatecRow] = []
        self._spectrum_rows_by_variant: dict[str, list[ThatecRow]] = {}
        self._read_pool = QThreadPool(self)
        self._read_pool.setMaxThreadCount(1)
        self._read_pool.setExpiryTimeout(15_000)
        self._read_request = 0
        self._active_read_request = 0
        self._read_tasks: dict[int, ResultReadTask] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Row selector ---
        selector = QHBoxLayout()
        selector.addWidget(BodyLabel("Data:"))
        self.variant_combo = ComboBox(self)
        self.variant_combo.setToolTip(
            "Choose raw power or the stored processed spectrum (raw minus reference)."
        )
        selector.addWidget(self.variant_combo)
        selector.addWidget(BodyLabel("X axis:"))
        self.x_axis_combo = ComboBox(self)
        self.x_axis_combo.setMinimumWidth(170)
        selector.addWidget(self.x_axis_combo)
        selector.addWidget(BodyLabel("Y axis:"))
        self.y_axis_combo = ComboBox(self)
        self.y_axis_combo.setMinimumWidth(170)
        selector.addWidget(self.y_axis_combo)
        selector.addWidget(BodyLabel("Spectrum row:"))
        self.row_combo = ComboBox(self)
        self.row_combo.setMinimumWidth(300)
        self.row_combo.setToolTip("Select a THATEC row with 2-D spectral data")
        selector.addWidget(self.row_combo, 1)

        self.load_button = PushButton(parent=self)
        self.load_button.setText("Load heatmap")
        self.load_button.setToolTip("Read all checkpoints and render the heatmap")
        self.load_button.setObjectName("plotToolButton")
        self.load_button.setEnabled(False)
        selector.addWidget(self.load_button)
        selector.addStretch(1)
        layout.addLayout(selector)

        self.filter_host = QWidget(self)
        self.filter_layout = QHBoxLayout(self.filter_host)
        self.filter_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_layout.setSpacing(6)
        self._filter_combos: dict[str, ComboBox] = {}
        layout.addWidget(self.filter_host)

        # --- Heatmap ---
        self.heatmap = HeatmapPlotWidget()
        self.heatmap_state = ResultsStateCard(self)
        self.heatmap_view = QStackedWidget(self)
        self.heatmap_view.addWidget(self.heatmap_state)
        self.heatmap_view.addWidget(self.heatmap)
        layout.addWidget(self.heatmap_view, 1)

        self.info_label = BodyLabel("Select a spectrum row and click 'Load heatmap'.")
        self.info_label.setObjectName("muted")
        layout.addWidget(self.info_label)

        # --- Connections ---
        self.load_button.clicked.connect(self._load_selected_row)
        self.variant_combo.currentIndexChanged.connect(self._variant_changed)
        self.x_axis_combo.currentIndexChanged.connect(self._axis_changed)
        self.y_axis_combo.currentIndexChanged.connect(self._axis_changed)
        self.row_combo.currentIndexChanged.connect(self._row_changed)
        self.heatmap.checkpoint_clicked.connect(self._on_checkpoint_clicked)
        self.heatmap.status_changed.connect(self._show_plot_status)
        self.heatmap_state.action_requested.connect(self._load_selected_row)
        self._show_heatmap_state(
            "Select a heatmap",
            "Choose a two-dimensional public spectrum row to compare checkpoints.",
        )

    def apply_theme(self, theme: str) -> None:
        """Retheme the visible pyqtgraph scene with the application tokens."""

        self.heatmap.apply_theme(theme)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(
        self, path: Path, run: ThatecRun, points: tuple[StoredPoint, ...] = ()
    ) -> None:
        """Prepare the tab with available spectrum rows from a THATEC result."""
        self._invalidate_pending_read()
        self._selected_path = path
        self._run = run
        self._points = points
        self._spectrum_rows = find_heatmap_rows(run)
        self.heatmap.clear()
        self._spectrum_rows_by_variant = self._split_spectrum_rows_by_variant(
            self._spectrum_rows
        )
        self.variant_combo.blockSignals(True)
        self.variant_combo.clear()
        for variant, label in (
            ("raw", "Raw (dBm)"),
            ("processed", "Processed — Raw − Reference (dB)"),
        ):
            if self._spectrum_rows_by_variant.get(variant):
                self.variant_combo.addItem(label, userData=variant)
        self.variant_combo.blockSignals(False)
        self._populate_rows_for_selected_variant()
        self._configure_coordinates()

        if self._spectrum_rows:
            self.load_button.setEnabled(True)
            self._show_heatmap_state(
                "Heatmap available",
                f"{len(self._spectrum_rows)} spectral row(s) available. "
                "Load the selected row to compare all checkpoints.",
                action_text="Load selected heatmap",
            )
        else:
            self.load_button.setEnabled(False)
            self._show_heatmap_state(
                "No heatmap-compatible rows",
                "This result has no single-trace checkpoint-by-spectrum matrix. "
                "Multi-trace data remains available in the Spectrum view.",
            )

    def load_heatmap_for_row(self, row_id: str) -> None:
        """Read all checkpoints for a given 2-D row and render the heatmap."""
        self._invalidate_pending_read()
        if self._selected_path is None or self._run is None:
            return
        row = self._run.rows.get(row_id)
        if row is None or len(row.shape) != 2:
            self._show_heatmap_error(
                f"Row {row_id} is not a two-dimensional single-trace spectrum."
            )
            return

        checkpoints = row.shape[0]
        freq_points = row.shape[1]
        self.load_button.setEnabled(False)
        self._show_heatmap_state(
            "Loading heatmap",
            f"Reading {checkpoints:,} checkpoints and "
            f"{checkpoints * freq_points:,} spectral samples...",
            loading=True,
        )
        if checkpoints > 4 or checkpoints * freq_points > _BACKGROUND_MATRIX_THRESHOLD:
            self._start_read(self._selected_path, row)
            return
        try:
            payload = _read_heatmap_payload(
                self._selected_path, row, self._active_coordinates(), self._request()
            )
        except Exception as exc:
            self._show_heatmap_error(str(exc))
            return
        self._render_payload(row, payload)

    def _render_payload(self, row: ThatecRow, payload: _HeatmapPayload) -> None:
        label = row.control_name or row.device_name or row.id
        z_axis_label = payload.z_label
        if payload.z_unit:
            z_axis_label = f"{z_axis_label} ({payload.z_unit})"
        self.heatmap.set_data(
            payload.matrix,
            x_values=payload.x_values,
            y_values=payload.y_values,
            x_label=payload.x_label,
            x_unit=payload.x_unit,
            y_label=payload.y_label,
            y_unit=payload.y_unit,
            z_label=z_axis_label,
            levels=payload.levels,
            cell_checkpoint_indices=payload.cell_checkpoints,
        )
        self.heatmap.plot.setTitle(
            f"{label} - {payload.y_label} × {payload.x_label}"
        )
        unit = f" {payload.z_unit}" if payload.z_unit else ""
        missing = (
            f" {payload.missing_checkpoints} unreadable checkpoint(s) are shown as gaps."
            if payload.missing_checkpoints
            else ""
        )
        message = (
            f"Heatmap loaded: {payload.matrix.shape[0]} x {payload.matrix.shape[1]}, "
            f"range {payload.levels[0]:.4g} to {payload.levels[1]:.4g}{unit}."
            f"{missing} Click the heatmap to open one checkpoint spectrum."
        )
        self.info_label.setText(message)
        self.heatmap_view.setCurrentWidget(self.heatmap)
        self.load_button.setEnabled(True)
        self.status_changed.emit(f"Heatmap for {label} loaded")

    def clear(self) -> None:
        """Reset the tab."""
        self._invalidate_pending_read()
        self.row_combo.clear()
        self.variant_combo.clear()
        self.x_axis_combo.clear()
        self.y_axis_combo.clear()
        self.heatmap.clear()
        self._spectrum_rows = []
        self._spectrum_rows_by_variant = {}
        self._selected_path = None
        self._run = None
        self._points = ()
        self._coordinates = None
        self._coordinate_row_id = None
        self.load_button.setEnabled(False)
        self._show_heatmap_state(
            "Select a result",
            "Choose an HDF5 result containing public spectral rows.",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_selected_row(self) -> None:
        row_id = self.row_combo.currentData()
        if row_id is not None:
            self.load_heatmap_for_row(str(row_id))

    def _row_changed(self, *_args: object) -> None:
        self._invalidate_pending_read()
        self.heatmap.clear()
        if self.row_combo.currentData() is None:
            return
        if str(self.row_combo.currentData()) != self._coordinate_row_id:
            self._configure_coordinates()
        self._show_heatmap_state(
            "Heatmap ready to load",
            "Load the selected spectral row to compare all recorded checkpoints.",
            action_text="Load selected heatmap",
        )

    def _configure_coordinates(self) -> None:
        self._coordinates = None
        if self._selected_path is None or self._run is None:
            return
        row_id = self.row_combo.currentData()
        row = self._run.rows.get(str(row_id)) if row_id is not None else None
        if row is None:
            return
        try:
            coordinates = build_heatmap_coordinates(
                self._selected_path, self._run, row, self._points
            )
        except Exception as exc:
            self._show_heatmap_error(str(exc))
            return
        self._coordinates = coordinates
        self._coordinate_row_id = row.id
        for combo in (self.x_axis_combo, self.y_axis_combo):
            combo.blockSignals(True)
            combo.clear()
            for dimension in coordinates.dimensions:
                suffix = f" ({dimension.unit})" if dimension.unit else ""
                combo.addItem(f"{dimension.label}{suffix}", userData=dimension.id)
            combo.blockSignals(False)
        self.x_axis_combo.setCurrentIndex(0)
        # Recipe sweep order is outer-to-inner.  With Frequency on X, use the
        # innermost physical sweep as the useful default Y coordinate.
        self.y_axis_combo.setCurrentIndex(
            len(coordinates.dimensions) - 1 if len(coordinates.dimensions) > 1 else 0
        )
        self._rebuild_filters()

    def _active_coordinates(self) -> HeatmapCoordinates:
        if self._coordinates is None:
            raise ValueError("Heatmap coordinates are not available for this row.")
        return self._coordinates

    def _request(self) -> HeatmapRequest:
        return HeatmapRequest(
            str(self.x_axis_combo.currentData()),
            str(self.y_axis_combo.currentData()),
            {dimension_id: float(combo.currentData()) for dimension_id, combo in self._filter_combos.items()},
        )

    def _axis_changed(self, *_args: object) -> None:
        if self.x_axis_combo.currentData() == self.y_axis_combo.currentData():
            alternate = 0 if self.x_axis_combo.currentIndex() else 1
            self.y_axis_combo.blockSignals(True)
            self.y_axis_combo.setCurrentIndex(alternate)
            self.y_axis_combo.blockSignals(False)
        self._rebuild_filters()
        self._row_changed()

    def _rebuild_filters(self) -> None:
        while self.filter_layout.count():
            item = self.filter_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._filter_combos = {}
        if self._coordinates is None:
            self.filter_host.hide()
            return
        axes = {self.x_axis_combo.currentData(), self.y_axis_combo.currentData()}
        needs_filter = any(
            dimension.id not in axes for dimension in self._coordinates.dimensions
        )
        if needs_filter:
            self.filter_layout.addWidget(BodyLabel("Filters:"))
        for dimension in self._coordinates.dimensions:
            if dimension.id in axes or dimension.is_frequency:
                continue
            self.filter_layout.addWidget(BodyLabel(f"{dimension.label}:"))
            combo = ComboBox(self.filter_host)
            for value in sorted(set(dimension.values)):
                suffix = f" {dimension.unit}" if dimension.unit else ""
                combo.addItem(f"{value:.9g}{suffix}", userData=value)
            combo.currentIndexChanged.connect(self._row_changed)
            self.filter_layout.addWidget(combo)
            self._filter_combos[dimension.id] = combo
        if "frequency" not in axes:
            self.filter_layout.addWidget(BodyLabel("Frequency:"))
            combo = ComboBox(self.filter_host)
            try:
                spectrum = ThatecRunReader.spectrum_slice(
                    self._selected_path, str(self.row_combo.currentData()), 0
                )
                for value in spectrum.x_values:
                    combo.addItem(f"{value:.9g} {spectrum.x_unit}", userData=value)
            except Exception:
                combo.addItem("No readable frequency grid", userData=float("nan"))
            combo.currentIndexChanged.connect(self._row_changed)
            self.filter_layout.addWidget(combo)
            self._filter_combos["frequency"] = combo
        self.filter_layout.addStretch(1)
        self.filter_host.setVisible(bool(self._filter_combos))

    @staticmethod
    def _split_spectrum_rows_by_variant(
        rows: list[ThatecRow],
    ) -> dict[str, list[ThatecRow]]:
        """Classify public spectrum rows using their persisted thaTEC role."""

        variants = {"raw": [], "processed": []}
        for row in rows:
            role = dict(row.definition).get("lab control role", "")
            variant = "processed" if role == "spectrum_processed" else "raw"
            variants[variant].append(row)
        return variants

    def _populate_rows_for_selected_variant(self) -> None:
        variant = str(self.variant_combo.currentData() or "raw")
        rows = self._spectrum_rows_by_variant.get(variant, [])
        self.row_combo.blockSignals(True)
        self.row_combo.clear()
        for row in rows:
            label = row.control_name or row.device_name or row.id
            detail = f"{label}  ({row.id}, {row.shape[0]}×{row.shape[1]})"
            self.row_combo.addItem(detail, userData=row.id)
        self.row_combo.blockSignals(False)

    def _variant_changed(self, *_args: object) -> None:
        self._invalidate_pending_read()
        self.heatmap.clear()
        self._populate_rows_for_selected_variant()
        self._configure_coordinates()
        self._row_changed()

    def _start_read(self, path: Path, row: ThatecRow) -> None:
        self._read_request += 1
        request_id = self._read_request
        self._active_read_request = request_id
        task = ResultReadTask(
            request_id,
            _read_heatmap_payload,
            path,
            row,
            self._active_coordinates(),
            self._request(),
            cooperative_cancel=True,
        )
        self._read_tasks[request_id] = task
        task.signals.loaded.connect(
            lambda loaded_id, payload, row=row: self._read_loaded(
                loaded_id, row, payload
            )
        )
        task.signals.failed.connect(self._read_failed)
        task.signals.finished.connect(self._read_finished)
        self._read_pool.start(task)

    def _read_loaded(
        self, request_id: int, row: ThatecRow, payload: object
    ) -> None:
        if request_id != self._active_read_request:
            return
        if not isinstance(payload, _HeatmapPayload):
            self._show_heatmap_error(
                "The HDF5 reader returned an unsupported heatmap payload."
            )
            return
        self._render_payload(row, payload)

    def _read_failed(self, request_id: int, message: str) -> None:
        if request_id == self._active_read_request:
            self._show_heatmap_error(message)

    def _read_finished(self, request_id: int) -> None:
        self._read_tasks.pop(request_id, None)
        if request_id == self._active_read_request:
            self.load_button.setEnabled(self.row_combo.currentData() is not None)

    def _invalidate_pending_read(self) -> None:
        for task in self._read_tasks.values():
            task.cancel()
        self._read_pool.clear()
        self._read_tasks.clear()
        self._read_request += 1
        self._active_read_request = self._read_request

    def _show_heatmap_state(
        self,
        title: str,
        description: str,
        *,
        loading: bool = False,
        action_text: str = "",
    ) -> None:
        self.heatmap_state.show_state(
            title=title,
            description=description,
            accessible_name=title,
            loading=loading,
            action_text=action_text,
        )
        self.heatmap_view.setCurrentWidget(self.heatmap_state)
        self.info_label.setText(description)

    def _show_heatmap_error(self, message: str) -> None:
        self.load_button.setEnabled(self.row_combo.currentData() is not None)
        self._show_heatmap_state(
            "Cannot build heatmap",
            message,
            action_text=(
                "Try again" if self.row_combo.currentData() is not None else ""
            ),
        )
        self.status_changed.emit(f"Cannot build heatmap: {message}")

    def _show_plot_status(self, message: str) -> None:
        self.info_label.setText(message)
        self.status_changed.emit(message)

    def _on_checkpoint_clicked(self, checkpoint: int) -> None:
        row_id = self.row_combo.currentData()
        if row_id is not None:
            self.checkpoint_clicked.emit(row_id, checkpoint)
