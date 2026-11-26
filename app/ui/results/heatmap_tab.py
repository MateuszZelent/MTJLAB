"""Heatmap visualization tab for spectral sweep results."""

from __future__ import annotations

import csv
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from html import escape
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLayout,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FlowLayout,
    FluentIcon,
    PushButton,
    ScrollArea,
    TogglePushButton,
)

from app.domain.quantities import (
    DIMENSION_FREQUENCY,
    DIMENSION_MAGNETIC_FIELD,
    format_quantity_auto,
)
from app.recipes.parameter_registry import parameter_descriptor
from app.spectrum.spintronic_fits import (
    extract_heatmap_ridge,
    fit_kittel_dispersion,
    kittel_frequency_hz,
    magnetic_field_scale_to_tesla,
)
from app.storage import StoredPoint, ThatecRow, ThatecRun, ThatecRunReader
from app.storage.filesystem_paths import native_path
from app.storage.heatmap_analysis_store import validate_cell_edges, write_heatmap_analysis
from app.ui.design_system import plot_theme, tokens_for
from app.ui.design_system.paper_plot_style import PAPER_STYLE
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.results.analysis_export import (
    ensure_derived_destination,
    heatmap_export_path,
    measurement_parameter_metadata,
    processing_manifest,
    write_analysis_manifest,
)
from app.ui.results.batch_processing import (
    HeatmapBatch,
    heatmap_title,
    measurement_parameters,
    plan_heatmaps,
)
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.filter_choices import FilterComboBox
from app.ui.results.heatmap_cache import HeatmapSourceCache
from app.ui.results.heatmap_coordinates import (
    SERIES_DIMENSION_ID,
    VISIT_DIMENSION_ID,
    HeatmapCoordinates,
    HeatmapDimension,
    HeatmapRange,
    HeatmapRequest,
    _selection_range,
    build_heatmap_coordinates,
    linked_heatmap_ranges,
    read_heatmap_matrix,
)
from app.ui.results.heatmap_ranges import HeatmapRangeDialog
from app.ui.results.map_controls import MapProcessingControls
from app.ui.results.map_processing import MapProcessing, analyse_map
from app.ui.results.processing import ResultSpectrumProcessor, recorded_instrument_average
from app.ui.results.processing_controls import ResultProcessingControls
from app.ui.results.processing_overlay import ResultProcessingOverlay
from app.ui.results.series_heatmap_views import (
    SeriesHeatmapView,
    read_series_heatmap_views,
    save_series_heatmap_view,
)
from app.ui.results.state_card import ResultsStateCard
from app.ui.results.workers import ResultReadTask
from app.ui.widgets.plot_ownership import (
    create_plot_widget,
    own_plot_item_menus,
    own_signal_proxy,
    set_plot_export_action,
)
from app.ui.widgets.plot_style import apply_scientific_style


def _read_coordinate_payload(path, run, row, points, *, cancelled=None, spectrum_reader=None):
    coordinates = build_heatmap_coordinates(path, run, row, points)
    frequencies = ()
    if row.shape[0]:
        # Read a valid grid once per spectral row, outside Qt. Missing first
        # checkpoints need not prevent inspecting a later recorded spectrum.
        for checkpoint in range(row.shape[0]):
            if cancelled is not None and cancelled():
                raise InterruptedError("Heatmap coordinate read cancelled")
            try:
                read = spectrum_reader or ThatecRunReader.spectrum_slice
                frequencies = tuple(read(path, row.id, checkpoint).x_values)
                break
            except Exception:
                continue
    return coordinates, frequencies


def _archive_identity(path):
    stat = native_path(path).stat()
    return path, stat.st_mtime_ns, stat.st_size


def _read_heatmap_archive(path, run, rows, points, *, expected_identity=None, cancelled=None, progress=None):
    identity = _archive_identity(path)
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("The archive changed. Reopen the result before reading it into memory.")
    spectra = ThatecRunReader.read_spectral_rows(path, rows, cancelled=cancelled, progress=progress)
    cache = HeatmapSourceCache(spectra)
    coordinates = {
        row.id: _read_coordinate_payload(path, run, row, points, cancelled=cancelled,
                                         spectrum_reader=cache.spectrum_slice)
        for row in rows
    }
    if identity != _archive_identity(path):
        raise ValueError("The archive changed while reading. Reopen it and try again.")
    return identity, cache, coordinates


def _axis_export_label(label: str, unit: str) -> str:
    return f"{label} ({unit})" if unit else label


def _absolute_map_view(matrix, colour_range):
    values = np.abs(matrix)
    if colour_range == "robust":
        levels = tuple(map(float, np.nanquantile(values, (0.01, 0.99))))
    else:
        levels = float(np.nanmin(values)), float(np.nanmax(values))
    return values, levels


def _heatmap_display(payload, row, processing, state, show_absolute):
    z_label = payload.z_label
    if state.view not in {"component", "removed"}:
        if payload.z_unit == "dB":
            z_label = "Relative power"
        elif payload.z_unit == "ratio":
            z_label = "Power ratio"
        elif payload.z_unit == "W" and (state.component.endswith("power")
                                        or processing.math_operation == "subtract_power_signed"):
            z_label = "Power difference"
    signed = payload.z_unit == "dB" or (payload.z_unit == "W" and (
        processing.math_operation == "subtract_power_signed"
        or dict(row.definition).get("lab control role") == "spectrum_processed"
        or state.view == "removed"
        or state.view == "result" and state.component.endswith("power")
    ))
    absolute = signed and show_absolute
    if absolute:
        matrix, levels = _absolute_map_view(payload.matrix, state.colour_range)
        return matrix, levels, f"Abs · {z_label}", signed, absolute
    return payload.matrix, payload.levels, z_label, signed, absolute


def _write_heatmap_companions(path, values, x, y, checkpoints, metadata, x_edges, y_edges):
    write_heatmap_analysis(Path(path).with_suffix(".h5"), values, x, y, checkpoints, metadata,
                           x_edges=x_edges, y_edges=y_edges)
    write_analysis_manifest(path, metadata)
    return str(path)


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
    processing_notes: tuple[str, ...] = ()
    baselines: tuple = ()
    line_input_dbm: np.ndarray | None = None
    line_input_is_raw: bool = True
    corrected_frequencies_hz: tuple[float, ...] = ()
    instrument_averages: dict[int, dict] = field(default_factory=dict)
    request: HeatmapRequest | None = None


def _process_map_payload(payload, state, frequency_axis, *, cancelled=None):
    # Input remains reachable even when a proposed filter would reject the map.
    if state.view == "input":
        state = replace(state, component="none", mask_lines=False, image_filter="none", view="result")
    if frequency_axis is None:
        if state.component != "none" or state.mask_lines or state.image_filter != "none" or state.view in {"component", "removed"}:
            raise ValueError("Choose Frequency as X or Y before using common-component, line, or 2D image filters.")
        finite = payload.matrix[np.isfinite(payload.matrix)]
        levels = payload.levels
        if state.colour_range == "robust":
            levels = tuple(map(float, np.quantile(finite, (0.01, 0.99))))
        elif state.colour_range == "symmetric":
            bound = max(abs(levels[0]), abs(levels[1]))
            levels = (-bound, bound)
        return replace(payload, levels=levels)
    if state.mask_lines and not payload.line_input_is_raw:
        raise ValueError("Choose a Raw spectrum row before classifying stationary lines; use post-processing to subtract recorded background/reference.")
    result = analyse_map(payload.matrix, unit=payload.z_unit,
        frequencies_hz=payload.x_values if frequency_axis == 1 else payload.y_values,
        coordinate_values=payload.y_values if frequency_axis == 1 else payload.x_values,
        frequency_axis=frequency_axis, state=state, line_input_dbm=payload.line_input_dbm, cancelled=cancelled)
    return replace(payload, matrix=result.values, z_unit=result.unit,
                   z_label=("Stripe correction" if state.view == "component" and state.component == "repair_stripe_bands_db" else
                            "Common component" if state.view == "component" else
                            "Removed by 2D filter" if state.view == "removed" else "Power / contrast"),
                   levels=result.levels, processing_notes=payload.processing_notes + result.notes,
                   corrected_frequencies_hz=result.corrected_frequencies_hz)


def _read_heatmap_payload(
    path: Path,
    row: ThatecRow,
    coordinates: HeatmapCoordinates,
    request: HeatmapRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
    processing=None,
    points=(),
    source_cache=None,
    checkpoint_indices=None,
) -> _HeatmapPayload:
    """Read the exact requested physical-coordinate plane."""

    processed_row = dict(row.definition).get("lab control role") == "spectrum_processed"
    if processing and processing.active and processed_row:
        raise ValueError("Choose the recorded spectrum row for post-processing. This row already contains derived values.")
    processor = ResultSpectrumProcessor(path, processing, points, cancelled=cancelled, reader=source_cache) if processing and processing.active else None
    matrix = read_heatmap_matrix(path, row, coordinates, request, cancelled=cancelled, processor=processor,
                                capture_input=processor is not None,
                                spectrum_reader=source_cache.spectrum_slice if source_cache else None,
                                checkpoint_indices=checkpoint_indices)
    instrument_averages = {}
    if points:
        checkpoints = set(np.unique(matrix.cell_checkpoints))
        for point in points:
            if point.index in checkpoints:
                average = recorded_instrument_average(point)
                if average is not None:
                    instrument_averages[point.index] = average
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
        processing_notes=tuple(processor.notes) if processor else (),
        baselines=processor.baselines if processor else (),
        line_input_dbm=matrix.input_values if matrix.input_unit == "dBm" else None,
        line_input_is_raw=not processed_row,
        instrument_averages=instrument_averages,
        request=request,
    )


class HeatmapPlotWidget(QWidget):
    """Interactive 2-D heatmap built on ``pyqtgraph.PColorMeshItem``.

    Axes:
        X — frequency (or spectrum sample index), derived from THATEC scale.
        Y — checkpoint index (or swept parameter value when available).
        Color — measured amplitude (typically dBm).
    """

    checkpoint_clicked = Signal(int)  # emitted for the source checkpoint of a cell
    hovered_cell = Signal(int, int)  # emitted as (row_index, col_index) on hover
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
        self._z_label = "Amplitude (dBm)"
        self._z_unit = ""
        self._cell_checkpoint_indices: np.ndarray | None = None
        self._last_readout_cell: tuple[int, int] | None = None
        self._theme_name = "dark"
        self.export_metadata = {}
        self._export_context: tuple[HeatmapCoordinates, dict[int, StoredPoint]] | None = None
        self._axis_metadata = {"x": {"label": "X", "unit": ""}, "y": {"label": "Y", "unit": ""}}
        self._export_pool = QThreadPool(self)
        self._export_pool.setMaxThreadCount(1)
        self._export_task = None

        layout = QVBoxLayout(self)
        self._main_layout = layout
        self._compact_stretch = True
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # --- Toolbar ---
        toolbar_host = QWidget(self)
        toolbar = FlowLayout(toolbar_host, needAni=False)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)

        palette_label = CaptionLabel("Palette:", self)
        palette_label.setObjectName("muted")
        toolbar.addWidget(palette_label)
        self.colormap_combo = ComboBox(self)
        self.colormap_combo.addItems(
            ["viridis", "inferno", "plasma", "magma", "cividis", "turbo", "hot", "CET-D1"]
        )
        self.colormap_combo.setToolTip("Select color palette")
        toolbar.addWidget(self.colormap_combo)

        auto_range_btn = PushButton(parent=self)
        auto_range_btn.setText("Auto range")
        auto_range_btn.setToolTip("Reset zoom and show all data")
        auto_range_btn.setObjectName("plotToolButton")
        auto_range_btn.clicked.connect(self.auto_range)
        toolbar.addWidget(auto_range_btn)

        export_btn = PushButton(parent=self)
        export_btn.setText("Export")
        export_btn.setToolTip(f"{PAPER_STYLE.description}. Export PNG, SVG or CSV with a reloadable HDF5 snapshot of the visible cells.")
        export_btn.setObjectName("plotToolButton")
        export_btn.clicked.connect(self.export)
        toolbar.addWidget(export_btn)
        self.batch_button = PushButton("Postprocess all measurements…", toolbar_host)
        self.batch_button.setEnabled(False)
        self.batch_button.setToolTip("Apply this heatmap's processing and axes to every recorded set in the sweep.")
        toolbar.addWidget(self.batch_button)

        self.range_button = PushButton("Axis ranges…", self)
        self.range_button.clicked.connect(self.configure_ranges)
        toolbar.addWidget(self.range_button)
        self.show_absolute = CheckBox("Abs", toolbar_host)
        self.show_absolute.setAccessibleName("Show absolute heatmap difference")
        self.show_absolute.setToolTip("Show the magnitude of signed dB or W differences.")
        self.show_absolute.setEnabled(False)
        toolbar.addWidget(self.show_absolute)
        self.export_button = export_btn
        self.readout = CaptionLabel("X: —   Y: —   Z: —", self)
        self.readout.setObjectName("plotReadout")
        toolbar.addWidget(self.readout)
        layout.addWidget(toolbar_host)

        # --- Plot ---
        self.plot = create_plot_widget()
        set_plot_export_action(self.plot, self.export)
        self.plot.setBackground(None)
        self.plot.showGrid(x=False, y=False)
        self.plot.getViewBox().setDefaultPadding(0)
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Checkpoint")
        self.plot.setMenuEnabled(True)
        self.plot.setMouseEnabled(x=True, y=True)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.plot.setMinimumHeight(150)
        self.setMinimumHeight(190)
        self.plot.sizeHint = lambda: QSize(600, 220)

        self.pcolor_item = pg.PColorMeshItem()
        self.raster_item = pg.ImageItem()
        self.raster_item.setVisible(False)
        self.image_item = self.pcolor_item
        self.plot.addItem(self.pcolor_item)
        self.plot.addItem(self.raster_item)
        self._dispersion_casing = pg.PlotDataItem(pen=pg.mkPen("#202020", width=4))
        self._dispersion_curve = pg.PlotDataItem(
            pen=pg.mkPen("#ffffff", width=2, style=Qt.PenStyle.DashLine)
        )
        for item, z_order in ((self._dispersion_casing, 5), (self._dispersion_curve, 6)):
            item.setZValue(z_order)
            item.hide()
            self.plot.addItem(item, ignoreBounds=True)
        self._cached_mesh_fingerprint: tuple[bytes, bytes] | None = None
        self._cached_mesh_vertices: tuple[np.ndarray, np.ndarray] | None = None

        # Color bar
        self.color_bar = pg.ColorBarItem(
            interactive=True,
            orientation="right",
            label=None,
        )
        self.color_bar.setImageItem(self.image_item, insert_in=self.plot.getPlotItem())
        own_plot_item_menus(self.color_bar, self.plot)

        # Keep the canvas about 30% shorter while retaining its readable minimum.
        layout.addWidget(self.plot, 7)
        layout.addStretch(3)

        # --- Crosshair ---
        self.crosshair_x = pg.InfiniteLine(
            angle=90, movable=False
        )
        self.crosshair_y = pg.InfiniteLine(
            angle=0, movable=False
        )
        self.plot.addItem(self.crosshair_x, ignoreBounds=True)
        self.plot.addItem(self.crosshair_y, ignoreBounds=True)
        self.crosshair_x.hide()
        self.crosshair_y.hide()
        self.plot.viewport().installEventFilter(self)

        self._mouse_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved
        )
        own_signal_proxy(self._mouse_proxy, self)
        self.plot.scene().sigMouseClicked.connect(self._mouse_clicked)
        self.colormap_combo.currentTextChanged.connect(self._apply_colormap)
        self.apply_theme(self._theme_name)

    def sizeHint(self) -> QSize:
        return QSize(600, 260)

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
        z_unit: str = "",
        levels: tuple[float, float] | None = None,
        cell_checkpoint_indices: np.ndarray | None = None,
        x_cell_edges: np.ndarray | None = None,
        y_cell_edges: np.ndarray | None = None,
    ) -> None:
        """Set the 2-D data matrix and update the plot.

        Parameters
        ----------
        data : ndarray, shape (rows, cols)
            The 2-D heatmap matrix (e.g. checkpoints x frequency bins).
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
            if x_cell_edges is not None:
                x_cell_edges = np.asarray(x_cell_edges)[::-1]
        if y_delta.size and np.all(y_delta < 0):
            y_axis = y_axis[::-1]
            matrix = matrix[::-1, :]
            checkpoint_indices = checkpoint_indices[::-1, :]
            if y_cell_edges is not None:
                y_cell_edges = np.asarray(y_cell_edges)[::-1]
        x_edges = self._cell_edges(x_axis) if x_cell_edges is None else validate_cell_edges(x_axis, x_cell_edges)
        y_edges = self._cell_edges(y_axis) if y_cell_edges is None else validate_cell_edges(y_axis, y_cell_edges)
        same_coordinates = (self._data is not None
                            and np.array_equal(self._x_values, x_axis)
                            and np.array_equal(self._y_values, y_axis))
        self._data = matrix
        self._x_values = x_axis
        self._y_values = y_axis
        self._x_edges = x_edges
        self._y_edges = y_edges
        self._x_export_label = _axis_export_label(x_label, x_unit)
        self._y_export_label = _axis_export_label(y_label, y_unit)
        self._cell_checkpoint_indices = checkpoint_indices
        self._last_readout_cell = None

        # Fast path: ImageItem for uniform sweeps, PColorMeshItem for nonuniform (GUI-03)
        use_raster = self._is_uniform_axis(x_edges) and self._is_uniform_axis(y_edges)
        if use_raster:
            self.pcolor_item.setVisible(False)
            self.pcolor_item.setData()
            self.raster_item.setVisible(True)
            self.raster_item.setImage(self._data.T, autoLevels=False)
            rect = QRectF(
                float(self._x_edges[0]),
                float(self._y_edges[0]),
                float(self._x_edges[-1] - self._x_edges[0]),
                float(self._y_edges[-1] - self._y_edges[0]),
            )
            self.raster_item.setRect(rect)
            self.image_item = self.raster_item
        else:
            self.raster_item.setVisible(False)
            self.raster_item.clear()
            self.pcolor_item.setVisible(True)
            # Endpoints/count alone cannot identify a nonuniform grid.
            fingerprint = (self._x_edges.tobytes(), self._y_edges.tobytes())
            if (
                self._cached_mesh_fingerprint == fingerprint
                and self._cached_mesh_vertices is not None
            ):
                x_vertices, y_vertices = self._cached_mesh_vertices
            else:
                x_vertices, y_vertices = np.meshgrid(self._x_edges, self._y_edges)
                self._cached_mesh_fingerprint = fingerprint
                self._cached_mesh_vertices = (x_vertices, y_vertices)
            self.pcolor_item.setData(
                x_vertices,
                y_vertices,
                self._data,
                autoLevels=False,
            )
            self.image_item = self.pcolor_item

        self.color_bar.setImageItem(self.image_item)

        self.plot.setLabel("bottom", x_label, units=x_unit)
        self.plot.setLabel("left", y_label, units=y_unit if y_unit else None)
        self._z_label, self._z_unit = z_label, z_unit
        self.color_bar.setLabel("right", text=z_label, units=z_unit)
        apply_scientific_style(self.plot.plotItem, self._theme_name, color_bar=self.color_bar)
        self._axis_metadata = {"x": {"label": x_label, "unit": x_unit},
                               "y": {"label": y_label, "unit": y_unit}}

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
            padding = max(abs(low) * 1e-12, abs(np.spacing(low))) if low else 1e-12
            low, high = low - padding, high + padding
        # ColorBarItem uses rounding as both the drag step and minimum span.
        # Its default of 1 would turn sub-nanowatt residuals into a 1 W range.
        self.color_bar.rounding = max(
            (high - low) / 1000, float(np.spacing(max(abs(low), abs(high))))
        )
        self.color_bar.setLevels((low, high))
        self.image_item.setLevels((low, high))
        if not same_coordinates:
            self.auto_range()

    def clear(self) -> None:
        self._hide_crosshair()
        self.clear_dispersion_overlay()
        self.show_absolute.setEnabled(False)
        self.export_metadata = {}
        self._export_context = None
        self.pcolor_item.setData()
        self.raster_item.clear()
        self._cached_mesh_fingerprint = None
        self._cached_mesh_vertices = None
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

    def set_compact_stretch(self, enabled: bool) -> None:
        """Toggle the 30% bottom spacer (kept on standalone widget; off when 1D slice is docked)."""
        enabled = bool(enabled)
        if enabled == self._compact_stretch:
            return
        self._compact_stretch = enabled
        plot_index = self._main_layout.indexOf(self.plot)
        self._main_layout.setStretch(plot_index, 7 if enabled else 1)
        self._main_layout.setStretch(plot_index + 1, 3 if enabled else 0)

    def set_dispersion_overlay(self, x_values, y_values) -> None:
        """Overlay a clipped high-contrast dashed Kittel/ridge curve inside the 2D ViewBox."""
        x = np.asarray(x_values, dtype=float)
        y = np.asarray(y_values, dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        x, y = x[mask], y[mask]
        if x.size < 2:
            self.clear_dispersion_overlay()
            return
        self._dispersion_casing.setData(x, y)
        self._dispersion_curve.setData(x, y)
        self._dispersion_casing.setVisible(True)
        self._dispersion_curve.setVisible(True)

    def clear_dispersion_overlay(self) -> None:
        for item in (self._dispersion_casing, self._dispersion_curve):
            item.setData([], [])
            item.hide()

    def auto_range(self) -> None:
        if self._x_edges is not None and self._y_edges is not None:
            view = self.plot.getViewBox()
            view.disableAutoRange()
            view.setRange(xRange=(float(self._x_edges[0]), float(self._x_edges[-1])),
                          yRange=(float(self._y_edges[0]), float(self._y_edges[-1])), padding=0)

    def apply_theme(self, theme: str) -> None:
        self._theme_name = theme
        palette = plot_theme(tokens_for(theme))
        self.plot.setBackground(palette.background)
        apply_scientific_style(self.plot.plotItem, theme, color_bar=self.color_bar)
        self.crosshair_x.setPen(pg.mkPen(palette.grid, width=1))
        self.crosshair_y.setPen(pg.mkPen(palette.grid, width=1))

    def export(self) -> None:
        if self._data is None:
            self.status_changed.emit("Load a recorded heatmap before exporting.")
            return
        if self._export_task is not None:
            return
        metadata = json.loads(json.dumps(self.export_metadata))
        title = self.plot.plotItem.titleLabel.item.toPlainText()
        z_label, z_unit = self._z_label, self._z_unit
        view_state = self.plot.getViewBox().state
        inverted = [bool(view_state["xInverted"]), bool(view_state["yInverted"])]
        metadata.update(axes=self._axis_metadata, view_ranges=self.plot.viewRange(),
                        palette=self.colormap_combo.currentText(), colour_levels=list(self.color_bar.levels()),
                        inverted_axes=inverted)
        try:
            x_bounds, y_bounds = metadata["view_ranges"]
            # Include partially visible cells and preserve their original edges.
            x_mask = (self._x_edges[:-1] < x_bounds[1]) & (self._x_edges[1:] > x_bounds[0])
            y_mask = (self._y_edges[:-1] < y_bounds[1]) & (self._y_edges[1:] > y_bounds[0])
            if not x_mask.any() or not y_mask.any():
                raise ValueError("The displayed range contains no recorded heatmap cells.")
            x_values, y_values = self._x_values[x_mask], self._y_values[y_mask]
            values = self._data[np.ix_(y_mask, x_mask)]
            checkpoints = self._cell_checkpoint_indices[np.ix_(y_mask, x_mask)]
            x_indices, y_indices = np.flatnonzero(x_mask), np.flatnonzero(y_mask)
            x_edges = self._x_edges[x_indices[0]:x_indices[-1] + 2].copy()
            y_edges = self._y_edges[y_indices[0]:y_indices[-1] + 2].copy()
            self._set_export_parameters(metadata, checkpoints, x_values, y_values)
            source = native_path(metadata["source_file"])
            if source.is_file():
                identity = source.stat()
                metadata["source_identity"] = {"size_bytes": identity.st_size, "mtime_ns": identity.st_mtime_ns}
            default = heatmap_export_path(metadata)
            native_path(default.parent).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.status_changed.emit(f"Cannot prepare heatmap export directory: {exc}")
            return
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export heatmap",
            str(default),
            "PNG (*.png);;SVG (*.svg);;CSV (*.csv)",
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".png" if "PNG" in selected else ".svg" if "SVG" in selected else ".csv"
        suffix = Path(path).suffix.lower()
        try:
            ensure_derived_destination(path, metadata["source_file"])
            if suffix not in {".png", ".svg", ".csv"}:
                raise ValueError("Choose a PNG, SVG or CSV filename.")
            if native_path(Path(path).with_suffix(".h5")).exists():
                raise ValueError("The companion HDF5 already exists. Choose a new export name.")
            if suffix in {".png", ".svg"}:
                from app.ui.widgets.figure_export import export_heatmap_figure

                axes = metadata["axes"]
                metadata["figure_presentation"] = export_heatmap_figure(Path(path),
                    values=values, x_edges=x_edges, y_edges=y_edges,
                    view_ranges=metadata["view_ranges"],
                    x_label=axes["x"]["label"], x_unit=axes["x"]["unit"],
                    y_label=axes["y"]["label"], y_unit=axes["y"]["unit"],
                    z_label=z_label, z_unit=z_unit,
                    levels=metadata["colour_levels"], colormap=metadata["palette"],
                    title=title, inverted_axes=inverted)
            else:
                self._export_csv(Path(path), values, x_values, y_values)
        except Exception as exc:
            self.status_changed.emit(f"Heatmap export failed: {exc}")
            return
        self.export_button.setEnabled(False)
        task = ResultReadTask(1, _write_heatmap_companions, path, values,
                              x_values, y_values, checkpoints, metadata, x_edges, y_edges)
        self._export_task = task
        task.signals.loaded.connect(self._export_loaded)
        task.signals.failed.connect(self._export_failed)
        task.signals.finished.connect(self._export_finished)
        self.status_changed.emit("Saving selected heatmap data and metadata…")
        self._export_pool.start(task)

    def _set_export_parameters(self, metadata, checkpoints, x_values, y_values) -> None:
        if self._export_context is not None:
            coordinates, points = self._export_context
            indices = np.unique(checkpoints[checkpoints >= 0]).tolist()
            metadata["parameters"] = measurement_parameter_metadata(
                measurement_parameters(coordinates, indices, points)
            )
        records = metadata.get("parameters")
        parameters = {
            record["id"]: record for record in records
            if isinstance(record, dict) and isinstance(record.get("id"), str)
        } if isinstance(records, list) else {}
        selection = metadata.get("coordinate_selection") or {}
        for axis, values in (("x", x_values), ("y", y_values)):
            label, unit = metadata["axes"][axis]["label"], metadata["axes"][axis]["unit"]
            key = selection.get(f"{axis}_id")
            if key is None and label.casefold() == "frequency":
                key = "frequency"
            if key == "frequency" or (self._export_context is None and key in parameters):
                record = parameters.setdefault(key, {"id": key, "label": label, "unit": unit})
                record.update(minimum=float(values.min()), maximum=float(values.max()))
        if parameters:
            metadata["parameters"] = list(parameters.values())

    def _export_finished(self, _request_id):
        self._export_task = None
        self.export_button.setEnabled(True)

    def _export_loaded(self, _request_id, destination):
        self.status_changed.emit(f"Heatmap and HDF5 saved: {destination}")

    def _export_failed(self, _request_id, message):
        self.status_changed.emit(f"Image saved, but companion export failed: {message}")

    def configure_ranges(self):
        if self._data is None:
            self.status_changed.emit("Load a heatmap before setting axis ranges.")
            return
        try:
            dialog = HeatmapRangeDialog(self, self._axis_metadata, self.plot.viewRange())
        except ValueError as exc:
            self.status_changed.emit(f"Cannot edit these axis units: {exc}")
            return
        if dialog.exec():
            self.plot.setRange(xRange=dialog.ranges[0], yRange=dialog.ranges[1], padding=0)
        dialog.deleteLater()

    def closeEvent(self, event):
        if not self._export_pool.waitForDone(5000):
            event.ignore()
            return
        super().closeEvent(event)

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
    def _is_uniform_axis(axis: np.ndarray) -> bool:
        """Return True if coordinates are uniformly spaced within floating tolerance."""
        if axis.size <= 2:
            return True
        deltas = np.diff(axis)
        mean_delta = float(np.mean(deltas))
        if abs(mean_delta) < 1e-15:
            return False
        return bool(np.allclose(deltas, mean_delta, rtol=1e-3, atol=1e-9))

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
        if not self.plot.getViewBox().sceneBoundingRect().contains(position):
            self._hide_crosshair()
            return
        point = self.plot.plotItem.vb.mapSceneToView(position)
        x, y = float(point.x()), float(point.y())
        cell = self._cell_indices(x, y)
        if cell is None:
            self._hide_crosshair()
            self.readout.setText("X: -   Y: -   Z: -")
            return
        self.crosshair_x.setPos(x)
        self.crosshair_y.setPos(y)
        self.crosshair_x.show()
        self.crosshair_y.show()
        if cell == self._last_readout_cell:
            return
        self._last_readout_cell = cell
        display_x = float(self._x_values[cell[1]])
        display_y = float(self._y_values[cell[0]])
        z = float(self._data[cell[0], cell[1]])
        z_text = f"{z:.4g}" if np.isfinite(z) else "-"
        self.readout.setText(
            f"X: {display_x:.6g}   Y: {display_y:.4g}   Z: {z_text}"
        )
        self.hovered_cell.emit(cell[0], cell[1])

    def _hide_crosshair(self):
        self.crosshair_x.hide()
        self.crosshair_y.hide()
        self._last_readout_cell = None

    def eventFilter(self, obj, event):
        if obj is self.plot.viewport() and event.type() == QEvent.Type.Leave:
            self._hide_crosshair()
        return super().eventFilter(obj, event)

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

    def _export_csv(self, path: Path, values=None, x_values=None, y_values=None) -> None:
        if self._data is None or self._x_values is None or self._y_values is None:
            return
        ensure_derived_destination(path, self.export_metadata.get("source_file"))
        values = self._data if values is None else values
        x_values = self._x_values if x_values is None else x_values
        y_values = self._y_values if y_values is None else y_values
        # QFileDialog already captured the user's explicit overwrite decision.
        with native_path(path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            header = [self._y_export_label] + [
                f"{float(x):.9g}" for x in x_values
            ]
            header[0] = f"{header[0]} / {self._x_export_label}"
            writer.writerow(header)
            for row_idx in range(values.shape[0]):
                row_values = [f"{float(y_values[row_idx]):.9g}"] + [
                    f"{float(v):.9g}" for v in values[row_idx]
                ]
                writer.writerow(row_values)


class HeatmapResultsTab(QWidget):
    """Tab wrapping :class:`HeatmapPlotWidget` with THATEC row selection."""

    checkpoint_clicked = Signal(str, int)  # (row_id, checkpoint)
    status_changed = Signal(str)
    batch_requested = Signal(object, object, object, object)
    powerpoint_requested = Signal(object, object, object, object)

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
        self._read_kinds = {}
        self.processing_overlay = ResultProcessingOverlay(self)
        self._coordinate_cache = {}
        self._frequency_values = ()
        self._processing_dirty = False
        self._source_payload = None
        self._source_row = None
        self._displayed_payload = None
        self._displayed_analysis = None
        self._source_cache = None
        self._source_cache_key = None
        self._source_caches = OrderedDict()
        self._archive_cache = None
        self._archive_cache_key = None
        self._selected_archive_identity = None
        self._reset_series_views()
        self._refresh_plane = QTimer(self)
        self._refresh_plane.setSingleShot(True)
        self._refresh_plane.setInterval(120)
        self._refresh_plane.timeout.connect(self._load_selected_row)
        self._automatic_palette_key = None
        self._explicit_axis_ranges: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._inspector_visible = True
        self._slice_visible = True
        self._vcut_visible = False
        self._kittel_enabled = False
        self._last_slice_cell: tuple[int, int] | None = None
        self._theme_name = "dark"

        # --- Data-First Horizontal Split (Left: 2D Heatmap + Live 1D Cuts | Right: Scrollable Inspector) ---
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.workspace_splitter.setObjectName("resultsTabSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)

        # ==================== LEFT STAGE: TELEMETRY + 2D MAP + LINKED 1D SLICES ====================
        main_stage = QWidget(self.workspace_splitter)
        stage_layout = QVBoxLayout(main_stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(4)

        # Dedicated single-line Telemetry & Tools Strip ABOVE plot (zero floating bubbles!)
        telemetry_bar = QFrame(main_stage)
        telemetry_bar.setObjectName("resultsTelemetryBar")
        telemetry_layout = QHBoxLayout(telemetry_bar)
        telemetry_layout.setContentsMargins(8, 4, 8, 4)
        telemetry_layout.setSpacing(6)

        self.slice_toggle_button = TogglePushButton("1D Slice", telemetry_bar)
        self.slice_toggle_button.setChecked(True)
        self.slice_toggle_button.setToolTip("Show live horizontal 1D spectrum slice P(f) below the heatmap")
        self.slice_toggle_button.toggled.connect(self.set_slice_visible)
        telemetry_layout.addWidget(self.slice_toggle_button)

        self.vcut_toggle_button = TogglePushButton("P(H) Cut", telemetry_bar)
        self.vcut_toggle_button.setChecked(False)
        self.vcut_toggle_button.setToolTip("Show optional vertical cut P(sweep) beside the heatmap")
        self.vcut_toggle_button.toggled.connect(self.set_vcut_visible)
        telemetry_layout.addWidget(self.vcut_toggle_button)

        self.kittel_button = TogglePushButton("Kittel / Ridge", telemetry_bar)
        self.kittel_button.setChecked(False)
        self.kittel_button.setToolTip("Track the dominant FMR peak ridge and fit Kittel dispersion f₀(H) when a field axis is active")
        self.kittel_button.toggled.connect(self._on_kittel_toggled)
        telemetry_layout.addWidget(self.kittel_button)

        self.kittel_readout_label = CaptionLabel("", telemetry_bar)
        self.kittel_readout_label.setObjectName("resultsTelemetryMuted")
        telemetry_layout.addWidget(self.kittel_readout_label, 1)

        self.inspector_toggle_button = TogglePushButton("Inspector", telemetry_bar)
        self.inspector_toggle_button.setChecked(True)
        self.inspector_toggle_button.setToolTip("Show or hide the right heatmap controls & processing inspector")
        self.inspector_toggle_button.toggled.connect(self.set_inspector_visible)
        telemetry_layout.addWidget(self.inspector_toggle_button)
        stage_layout.addWidget(telemetry_bar)

        # Vertical splitter: Top = 2D Map (+ optional vertical cut) | Bottom = Live 1D Slice P(f)
        self.plot_splitter = QSplitter(Qt.Orientation.Vertical, main_stage)
        self.plot_splitter.setObjectName("resultsTabSplitter")
        self.plot_splitter.setChildrenCollapsible(False)

        top_plot_row = QWidget(self.plot_splitter)
        top_plot_layout = QHBoxLayout(top_plot_row)
        top_plot_layout.setContentsMargins(0, 0, 0, 0)
        top_plot_layout.setSpacing(4)

        # --- Heatmap ---
        self.heatmap = HeatmapPlotWidget(top_plot_row)
        self.heatmap.set_compact_stretch(False)
        self.heatmap.batch_button.clicked.connect(self._open_batch)
        self.heatmap.show_absolute.toggled.connect(self._absolute_view_changed)
        self.heatmap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.heatmap_state = ResultsStateCard(top_plot_row)
        self.heatmap_view = QStackedWidget(top_plot_row)
        self.heatmap_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.heatmap_view.addWidget(self.heatmap_state)
        self.heatmap_view.addWidget(self.heatmap)
        top_plot_layout.addWidget(self.heatmap_view, 4)

        # Optional Vertical Cut P(H) dock (off by default so 2D map gets full width!)
        self.vcut_panel = QFrame(top_plot_row)
        self.vcut_panel.setObjectName("resultsInspectorDrawer")
        self.vcut_panel.setMinimumWidth(145)
        self.vcut_panel.setMaximumWidth(220)
        vcut_layout = QVBoxLayout(self.vcut_panel)
        vcut_layout.setContentsMargins(4, 2, 4, 2)
        vcut_layout.setSpacing(2)
        self.vcut_header = CaptionLabel("Cut P(Y)", self.vcut_panel)
        self.vcut_header.setObjectName("resultsTelemetryMuted")
        vcut_layout.addWidget(self.vcut_header)
        self.vcut_plot = create_plot_widget(self.vcut_panel)
        self.vcut_plot.showGrid(x=True, y=True, alpha=0.18)
        self.vcut_plot.setMouseEnabled(x=False, y=False)
        self.vcut_curve = self.vcut_plot.plot([], [])
        self.vcut_marker = pg.InfiniteLine(angle=0, movable=False)
        self.vcut_plot.addItem(self.vcut_marker, ignoreBounds=True)
        self.vcut_marker.hide()
        vcut_layout.addWidget(self.vcut_plot, 1)
        self.vcut_panel.hide()
        top_plot_layout.addWidget(self.vcut_panel, 1)
        self.plot_splitter.addWidget(top_plot_row)

        # Bottom Dock: Live Linked 1D Horizontal Slice P(f)
        self.slice_panel = QFrame(self.plot_splitter)
        self.slice_panel.setObjectName("resultsScrubberBar")
        slice_layout = QVBoxLayout(self.slice_panel)
        slice_layout.setContentsMargins(6, 3, 6, 3)
        slice_layout.setSpacing(2)
        slice_hdr = QHBoxLayout()
        slice_hdr.setContentsMargins(0, 0, 0, 0)
        slice_title = CaptionLabel("1D Spectrum Slice", self.slice_panel)
        slice_title.setObjectName("resultsTelemetryPrimary")
        self.slice_meta_label = CaptionLabel("Hover 2D map to inspect slice · click to open in Spectrum", self.slice_panel)
        self.slice_meta_label.setObjectName("resultsTelemetryMuted")
        slice_hdr.addWidget(slice_title)
        slice_hdr.addStretch(1)
        slice_hdr.addWidget(self.slice_meta_label)
        slice_layout.addLayout(slice_hdr)
        self.slice_plot = create_plot_widget(self.slice_panel)
        self.slice_plot.setMinimumHeight(96)
        self.slice_plot.showGrid(x=True, y=True, alpha=0.18)
        self.slice_plot.setMouseEnabled(x=True, y=False)
        self.slice_curve = self.slice_plot.plot([], [])
        self.slice_marker = pg.InfiniteLine(angle=90, movable=False)
        self.slice_plot.addItem(self.slice_marker, ignoreBounds=True)
        self.slice_marker.hide()
        slice_layout.addWidget(self.slice_plot, 1)
        self.plot_splitter.addWidget(self.slice_panel)
        self.plot_splitter.setStretchFactor(0, 3)
        self.plot_splitter.setStretchFactor(1, 1)
        self.plot_splitter.setSizes([380, 140])
        stage_layout.addWidget(self.plot_splitter, 1)

        self.info_label = BodyLabel("Select a spectrum row and click 'Load heatmap'.", main_stage)
        self.info_label.setObjectName("muted")
        self.info_label.setWordWrap(True)
        stage_layout.addWidget(self.info_label)
        self.workspace_splitter.addWidget(main_stage)

        # ==================== RIGHT DRAWER: SCROLLABLE HEATMAP CONTROLS & DSP INSPECTOR ====================
        self.inspector_drawer = ScrollArea(self.workspace_splitter)
        self.inspector_drawer.setObjectName("resultsInspectorScroll")
        self.inspector_drawer.setWidgetResizable(True)
        self.inspector_drawer.setFrameShape(ScrollArea.Shape.NoFrame)
        self.inspector_drawer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector_drawer.setMinimumWidth(295)

        inspector_host = QWidget(self.inspector_drawer)
        inspector_host.setObjectName("resultsInspectorDrawer")
        inspector_host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        inspector_layout = QVBoxLayout(inspector_host)
        inspector_layout.setContentsMargins(8, 6, 8, 8)
        inspector_layout.setSpacing(6)

        # --- Row selector & filters in a CardWidget (all controls directly parented to controls_card) ---
        self.controls_card = CardWidget(inspector_host)
        controls_layout = QVBoxLayout(self.controls_card)
        controls_layout.setContentsMargins(10, 8, 10, 8)
        controls_layout.setSpacing(6)

        self.selector_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        selector = self.selector_layout
        selector.setSpacing(6)

        form_grid = QGridLayout()
        form_grid.setContentsMargins(0, 0, 0, 0)
        form_grid.setHorizontalSpacing(6)
        form_grid.setVerticalSpacing(5)
        form_grid.setColumnStretch(1, 1)

        lbl_data = CaptionLabel("Data:", self.controls_card)
        lbl_data.setObjectName("muted")
        self.variant_combo = ComboBox(self.controls_card)
        self.variant_combo.setToolTip(
            "Choose raw power or the stored processed spectrum (raw minus reference)."
        )
        form_grid.addWidget(lbl_data, 0, 0)
        form_grid.addWidget(self.variant_combo, 0, 1)

        lbl_x = CaptionLabel("X axis:", self.controls_card)
        lbl_x.setObjectName("muted")
        self.x_axis_combo = ComboBox(self.controls_card)
        self.x_axis_combo.setMinimumWidth(115)
        form_grid.addWidget(lbl_x, 1, 0)
        form_grid.addWidget(self.x_axis_combo, 1, 1)

        lbl_y = CaptionLabel("Y axis:", self.controls_card)
        lbl_y.setObjectName("muted")
        self.y_axis_combo = ComboBox(self.controls_card)
        self.y_axis_combo.setMinimumWidth(115)
        form_grid.addWidget(lbl_y, 2, 0)
        form_grid.addWidget(self.y_axis_combo, 2, 1)

        lbl_row = CaptionLabel("Spectrum row:", self.controls_card)
        lbl_row.setObjectName("muted")
        self.row_combo = ComboBox(self.controls_card)
        self.row_combo.setMinimumWidth(130)
        self.row_combo.setToolTip("Select a THATEC row with 2-D spectral data")
        form_grid.addWidget(lbl_row, 3, 0)
        form_grid.addWidget(self.row_combo, 3, 1)

        actions_col = QVBoxLayout()
        actions_col.setContentsMargins(0, 0, 0, 0)
        actions_col.setSpacing(5)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(6)
        self.load_button = PushButton(parent=self.controls_card)
        self.load_button.setText("Load heatmap")
        self.load_button.setIcon(FluentIcon.TILES)
        self.load_button.setToolTip("Build the heatmap for the selected axes and filters")
        self.load_button.setObjectName("plotToolButton")
        self.load_button.setEnabled(False)
        action_row.addWidget(self.load_button, 1)

        self.read_file_button = PushButton("Read file", self.controls_card)
        self.read_file_button.setToolTip(
            "Read all heatmap spectra into memory so axes and filters can change without reading the spectra again."
        )
        self.read_file_button.setEnabled(False)
        action_row.addWidget(self.read_file_button)
        actions_col.addLayout(action_row)

        self.powerpoint_button = PushButton("Export sweep to PowerPoint…", self.controls_card)
        self.powerpoint_button.setToolTip(
            "Create one PowerPoint slide per recorded heatmap group using the selected axes and processing."
        )
        self.powerpoint_button.setEnabled(False)
        actions_col.addWidget(self.powerpoint_button)

        selector.addLayout(form_grid)
        selector.addLayout(actions_col)
        controls_layout.addLayout(selector)

        self.series_host = QWidget(self.controls_card)
        series_layout = QVBoxLayout(self.series_host)
        series_layout.setContentsMargins(0, 0, 0, 0)
        series_selector = QHBoxLayout()
        series_selector.addWidget(CaptionLabel("Measurement series", self.series_host))
        self.series_combo = ComboBox(self.series_host)
        self.series_combo.setAccessibleName("Measurement series")
        self.series_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.series_combo.setMinimumWidth(160)
        self.series_combo.currentIndexChanged.connect(self._series_changed)
        series_selector.addWidget(self.series_combo, 1)
        series_layout.addLayout(series_selector)
        series_actions = QHBoxLayout()
        self.save_series_view_button = PushButton("Save series view", self.series_host)
        self.save_series_view_button.setToolTip("Save this series' heatmap for later review and PowerPoint export.")
        self.save_series_view_button.setEnabled(False)
        self.save_series_view_button.clicked.connect(self._save_series_view)
        series_actions.addWidget(self.save_series_view_button)
        self.series_view_status = CaptionLabel("", self.series_host)
        self.series_view_status.setWordWrap(True)
        series_actions.addWidget(self.series_view_status, 1)
        series_layout.addLayout(series_actions)
        controls_layout.addWidget(self.series_host)
        self.series_host.hide()

        self.filter_host = QWidget(self.controls_card)
        self.filter_layout = QVBoxLayout(self.filter_host)
        self.filter_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_layout.setSpacing(4)
        self._range_combos: dict[str, tuple[ComboBox, ComboBox]] = {}
        self._filter_combos: dict[str, ComboBox] = {}
        controls_layout.addWidget(self.filter_host)
        inspector_layout.addWidget(self.controls_card)

        self.processing_controls = ResultProcessingControls(inspector_host)
        inspector_layout.addWidget(self.processing_controls)
        self.processing_controls.changed.connect(self._processing_changed)
        self.map_controls = MapProcessingControls(inspector_host)
        inspector_layout.addWidget(self.map_controls)
        self.map_controls.changed.connect(self._map_changed)
        inspector_layout.addStretch(1)

        self.inspector_drawer.setWidget(inspector_host)
        self.workspace_splitter.addWidget(self.inspector_drawer)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([640, 320])
        layout.addWidget(self.workspace_splitter, 1)
        self.setMinimumHeight(220)

        # --- Connections ---
        self.load_button.clicked.connect(self._load_selected_row)
        self.read_file_button.clicked.connect(self._read_file)
        self.powerpoint_button.clicked.connect(lambda: self._open_batch(power_point=True))
        self.variant_combo.currentIndexChanged.connect(self._variant_changed)
        self.x_axis_combo.currentIndexChanged.connect(self._axis_changed)
        self.y_axis_combo.currentIndexChanged.connect(self._axis_changed)
        self.row_combo.currentIndexChanged.connect(self._row_changed)
        self.heatmap.checkpoint_clicked.connect(self._on_checkpoint_clicked)
        self.heatmap.hovered_cell.connect(self._update_cross_sections)
        self.heatmap.status_changed.connect(self._show_plot_status)
        self.heatmap_state.action_requested.connect(self._load_selected_row)
        self._show_heatmap_state(
            "Select a heatmap",
            "Choose a two-dimensional public spectrum row to compare checkpoints.",
        )
        self._selector_compact: bool | None = True
        self.apply_theme(self._theme_name)

    def sizeHint(self) -> QSize:
        return QSize(600, 300)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_selector_layout(event.size().width())

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_selector_layout(self.width())
        QTimer.singleShot(0, self, lambda: self._sync_selector_layout(self.width()))
        if self._processing_dirty and self._coordinates is not None:
            self._processing_dirty = False
            self._load_selected_row()

    def _sync_selector_layout(self, width: int) -> None:
        card_w = max(220, self.controls_card.width() - 20)
        compact = card_w < 820
        if compact != self._selector_compact:
            self._selector_compact = compact
            self.selector_layout.setDirection(
                QBoxLayout.Direction.TopToBottom
                if compact
                else QBoxLayout.Direction.LeftToRight
            )

    def set_inspector_visible(self, visible: bool) -> None:
        self._inspector_visible = bool(visible)
        self.inspector_drawer.setVisible(self._inspector_visible)
        if self.inspector_toggle_button.isChecked() != self._inspector_visible:
            blocked = self.inspector_toggle_button.blockSignals(True)
            self.inspector_toggle_button.setChecked(self._inspector_visible)
            self.inspector_toggle_button.blockSignals(blocked)

    def toggle_inspector(self) -> None:
        self.set_inspector_visible(not self._inspector_visible)

    def set_slice_visible(self, visible: bool) -> None:
        self._slice_visible = bool(visible)
        self.slice_panel.setVisible(self._slice_visible)
        if self._slice_visible and self._last_slice_cell is not None:
            self._update_cross_sections(*self._last_slice_cell)

    def set_vcut_visible(self, visible: bool) -> None:
        self._vcut_visible = bool(visible)
        self.vcut_panel.setVisible(self._vcut_visible)
        if self._vcut_visible and self._last_slice_cell is not None:
            self._update_cross_sections(*self._last_slice_cell)

    def toggle_kittel_overlay(self) -> None:
        self.kittel_button.setChecked(not self.kittel_button.isChecked())

    def _on_kittel_toggled(self, checked: bool) -> None:
        self._kittel_enabled = bool(checked)
        self._update_dispersion_overlay()

    def _update_cross_sections(self, row_idx: int, col_idx: int) -> None:
        """Render auto-scaled live 1D horizontal slice and optional vertical cut."""
        data = self.heatmap._data
        x_vals = self.heatmap._x_values
        y_vals = self.heatmap._y_values
        if data is None or x_vals is None or y_vals is None:
            return
        row_idx = max(0, min(row_idx, data.shape[0] - 1))
        col_idx = max(0, min(col_idx, data.shape[1] - 1))
        self._last_slice_cell = (row_idx, col_idx)

        x_meta = self.heatmap._axis_metadata["x"]
        y_meta = self.heatmap._axis_metadata["y"]
        z_unit = self.heatmap._z_unit or ""
        x_cur = float(x_vals[col_idx])
        y_cur = float(y_vals[row_idx])
        z_cur = float(data[row_idx, col_idx])
        x_str = (
            format_quantity_auto(x_cur, DIMENSION_FREQUENCY)
            if x_meta.get("unit") == "Hz"
            else f"{x_cur:.5g} {x_meta.get('unit', '')}".strip()
        )
        y_str = f"{y_cur:.5g} {y_meta.get('unit', '')}".strip()
        z_str = f"{z_cur:.4g} {z_unit}".strip() if np.isfinite(z_cur) else "—"

        if self._slice_visible:
            row_slice = data[row_idx, :]
            finite = np.isfinite(x_vals) & np.isfinite(row_slice)
            self.slice_curve.setData(x_vals[finite], row_slice[finite])
            slice_axis_key = (x_meta["label"], x_meta.get("unit") or "", z_unit or "Z", self._theme_name)
            if getattr(self, "_slice_axis_key", None) != slice_axis_key:
                self._slice_axis_key = slice_axis_key
                self.slice_plot.setLabel("bottom", x_meta["label"], units=x_meta.get("unit") or None)
                self.slice_plot.setLabel("left", z_unit or "Z")
                apply_scientific_style(self.slice_plot.plotItem, self._theme_name)
            self.slice_marker.setPos(x_cur)
            self.slice_marker.show()
            if np.any(finite):
                lo, hi = float(np.min(row_slice[finite])), float(np.max(row_slice[finite]))
                pad = max(abs(hi - lo) * 0.12, 1e-6)
                self.slice_plot.setYRange(lo - pad, hi + pad, padding=0)
                self.slice_plot.setXRange(float(x_vals[0]), float(x_vals[-1]), padding=0)
            self.slice_meta_label.setText(f"{y_meta['label']} = {y_str} · {x_str} ({z_str})")

        if self._vcut_visible:
            col_slice = data[:, col_idx]
            finite = np.isfinite(y_vals) & np.isfinite(col_slice)
            self.vcut_curve.setData(col_slice[finite], y_vals[finite])
            self.vcut_plot.setLabel("bottom", z_unit or "Z")
            self.vcut_marker.setPos(y_cur)
            self.vcut_marker.show()
            if np.any(finite):
                lo, hi = float(np.min(col_slice[finite])), float(np.max(col_slice[finite]))
                pad = max(abs(hi - lo) * 0.12, 1e-6)
                self.vcut_plot.setXRange(lo - pad, hi + pad, padding=0)
                self.vcut_plot.setYRange(float(y_vals[0]), float(y_vals[-1]), padding=0)
            self.vcut_header.setText(f"Cut @ {x_str}")

    def _update_dispersion_overlay(self) -> None:
        """Track resonance ridge on 2D map and fit Kittel dispersion when field units exist."""
        if not self._kittel_enabled or self.heatmap._data is None:
            self.heatmap.clear_dispersion_overlay()
            self.kittel_readout_label.setText("")
            return
        data = self.heatmap._data
        x_vals = self.heatmap._x_values
        y_vals = self.heatmap._y_values
        x_meta = self.heatmap._axis_metadata["x"]
        y_meta = self.heatmap._axis_metadata["y"]
        if x_meta.get("unit") == "Hz":
            freq_axis = 1
            sweep_unit = y_meta.get("unit", "")
        elif y_meta.get("unit") == "Hz":
            freq_axis = 0
            sweep_unit = x_meta.get("unit", "")
        else:
            self.heatmap.clear_dispersion_overlay()
            self.kittel_readout_label.setText("Kittel / Ridge requires a Frequency (Hz) axis")
            return

        sweeps, freqs_hz = extract_heatmap_ridge(
            data,
            x_vals,
            y_vals,
            frequency_axis=freq_axis,
            exclude_bands_hz=self.map_controls.state.stripe_bands_hz,
        )
        if sweeps.size < 2:
            self.heatmap.clear_dispersion_overlay()
            self.kittel_readout_label.setText("No prominent resonance ridge detected")
            return

        scale_t = magnetic_field_scale_to_tesla(sweep_unit)
        if scale_t is not None and sweeps.size >= 4:
            try:
                fit = fit_kittel_dispersion(sweeps * scale_t, freqs_hz)
                dense_sweep = np.linspace(float(np.min(sweeps)), float(np.max(sweeps)), 120)
                dense_f = kittel_frequency_hz(
                    dense_sweep * scale_t,
                    m_eff_t=fit.m_eff_t,
                    h_k_t=fit.h_k_t,
                    gamma_ghz_per_t=fit.gamma_ghz_per_t,
                )
                if freq_axis == 1:
                    self.heatmap.set_dispersion_overlay(dense_f, dense_sweep)
                else:
                    self.heatmap.set_dispersion_overlay(dense_sweep, dense_f)
                hk_txt = format_quantity_auto(fit.h_k_t, DIMENSION_MAGNETIC_FIELD)
                self.kittel_readout_label.setText(
                    f"Kittel: μ₀M_eff = {fit.m_eff_t:.3g} T · H_k = {hk_txt} · R² = {fit.r_squared:.3f}"
                )
                return
            except (ValueError, RuntimeError):
                # Fall through to tracked ridge display when field axis does not follow Kittel dispersion.
                self.kittel_readout_label.setText("")

        if freq_axis == 1:
            self.heatmap.set_dispersion_overlay(freqs_hz, sweeps)
        else:
            self.heatmap.set_dispersion_overlay(sweeps, freqs_hz)
        f_lo = format_quantity_auto(float(np.min(freqs_hz)), DIMENSION_FREQUENCY)
        f_hi = format_quantity_auto(float(np.max(freqs_hz)), DIMENSION_FREQUENCY)
        self.kittel_readout_label.setText(
            f"Tracked ridge: {sweeps.size} pts · {f_lo} – {f_hi}"
        )

    def apply_theme(self, theme: str) -> None:
        """Retheme the visible pyqtgraph scenes (2D map + 1D slices) with application tokens."""
        self._theme_name = theme
        tokens = tokens_for(theme)
        palette = plot_theme(tokens)
        teal = "#94e2d5" if theme == "dark" else "#0e858c"
        self.heatmap.apply_theme(theme)
        for plot_widget, curve, marker, color in (
            (self.slice_plot, self.slice_curve, self.slice_marker, tokens.accent),
            (self.vcut_plot, self.vcut_curve, self.vcut_marker, teal),
        ):
            plot_widget.setBackground(palette.background)
            apply_scientific_style(plot_widget.plotItem, theme)
            curve.setPen(pg.mkPen(color, width=1.6))
            marker.setPen(
                pg.mkPen(
                    teal if color == tokens.accent else tokens.accent,
                    width=1.1,
                    style=Qt.PenStyle.DashLine,
                )
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(
        self, path: Path, run: ThatecRun, points: tuple[StoredPoint, ...] = (), *, references=()
    ) -> None:
        """Prepare the tab with available spectrum rows from a THATEC result."""
        self._invalidate_pending_read()
        self.powerpoint_button.setEnabled(False)
        self.controls_card.setEnabled(True)
        self.processing_controls.setEnabled(True)
        self.map_controls.setEnabled(True)
        self.controls_card.show()
        self.processing_controls.show()
        self.map_controls.show()
        self._coordinate_cache.clear()
        self._source_cache = None
        self._source_cache_key = None
        self._source_caches.clear()
        self._clear_archive_cache()
        self._coordinates = None
        self._coordinate_row_id = None
        self._processing_dirty = False
        self._reset_series_filter()
        self._source_payload = None
        self._source_row = None
        self._displayed_payload = None
        self._displayed_analysis = None
        self.map_controls.set_state(MapProcessing())
        self.processing_controls.set_references(references)
        self._selected_path = path
        try:
            self._selected_archive_identity = _archive_identity(path)
        except OSError:
            self._selected_archive_identity = None
        self._reset_series_views()
        if self._selected_archive_identity is not None:
            try:
                self._series_views = read_series_heatmap_views(self._selected_archive_identity)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._series_view_error = str(exc)
        self._run = run
        self._points = points
        self._spectrum_rows = find_heatmap_rows(run)
        self.read_file_button.setEnabled(bool(self._spectrum_rows))
        self.heatmap.clear()
        self._spectrum_rows_by_variant = self._split_spectrum_rows_by_variant(
            self._spectrum_rows
        )
        self.variant_combo.blockSignals(True)
        self.variant_combo.clear()
        for variant, label in (
            ("raw", "Raw (dBm)"),
            ("processed", "Stored processed spectrum"),
        ):
            if self._spectrum_rows_by_variant.get(variant):
                self.variant_combo.addItem(label, userData=variant)
        self.variant_combo.blockSignals(False)
        self._populate_rows_for_selected_variant()
        self._configure_coordinates()

        if self._spectrum_rows:
            self.load_button.setEnabled(self._coordinates is not None)
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

    def load_analysis(self, path, analysis):
        self.clear()
        self.controls_card.setEnabled(False)
        self.processing_controls.setEnabled(False)
        self.map_controls.setEnabled(False)
        self.controls_card.hide()
        self.processing_controls.hide()
        self.map_controls.hide()
        metadata = analysis.metadata
        self._displayed_analysis = analysis
        self._render_analysis(analysis)
        self.heatmap.colormap_combo.setCurrentText(metadata.get("palette", "viridis"))
        ranges = metadata.get("view_ranges")
        inverted = metadata.get("inverted_axes", (False, False))
        self.heatmap.plot.getViewBox().invertX(bool(inverted[0]))
        self.heatmap.plot.getViewBox().invertY(bool(inverted[1]))
        if ranges:
            self.heatmap.plot.setRange(xRange=ranges[0], yRange=ranges[1], padding=0)
        self.info_label.setText(f"Loaded derived heatmap: {path.name}. Open the source sweep to change processing; axis ranges and export remain available.")
        self.heatmap_view.setCurrentWidget(self.heatmap)

    def _render_analysis(self, analysis):
        metadata = analysis.metadata
        unit = metadata["value_unit"]
        axes = metadata["axes"]
        already_absolute = metadata.get("display_transform") == "absolute_difference"
        spectrum_settings = metadata.get("spectrum_processing", {})
        map_settings = metadata.get("map_processing", {})
        signed = unit == "dB" or (unit == "W" and (
            metadata.get("signed_difference") is True
            or (isinstance(spectrum_settings, dict)
                and spectrum_settings.get("operation") in {"subtract_power_signed", "subtract_reference_signed"})
            or (isinstance(map_settings, dict) and (
                map_settings.get("view") == "removed"
                or map_settings.get("view", "result") == "result"
                and map_settings.get("component") in {"median_power", "quantile_power", "reference_power"}
            ))
        ))
        self.heatmap.show_absolute.setEnabled(signed and not already_absolute)
        absolute = self.heatmap.show_absolute.isEnabled() and self.heatmap.show_absolute.isChecked()
        values = analysis.values
        levels = tuple(metadata["colour_levels"]) if "colour_levels" in metadata else None
        if absolute:
            values, levels = _absolute_map_view(values, "full")
        label = "Abs · Derived values" if absolute or already_absolute else "Derived values"
        self.heatmap.set_data(values, analysis.x_values, analysis.y_values,
            x_label=axes["x"]["label"], x_unit=axes["x"].get("unit", ""),
            y_label=axes["y"]["label"], y_unit=axes["y"].get("unit", ""),
            z_label=label, z_unit=unit, levels=levels,
            cell_checkpoint_indices=analysis.checkpoints, x_cell_edges=analysis.x_edges, y_cell_edges=analysis.y_edges)
        self.heatmap.export_metadata = dict(metadata)
        if absolute:
            self.heatmap.export_metadata["display_transform"] = "absolute_difference"
        self.heatmap.plot.setTitle(escape(metadata.get("title") or "Exported heatmap"))
        if self.heatmap._data is not None and self.heatmap._data.size:
            self._update_cross_sections(
                self.heatmap._data.shape[0] // 2,
                self.heatmap._data.shape[1] // 2,
            )
            self._update_dispersion_overlay()

    def load_heatmap_for_row(self, row_id: str) -> None:
        """Read all checkpoints for a given 2-D row and render the heatmap."""
        self._invalidate_pending_read()
        self._source_payload = None
        if self._selected_path is None or self._run is None:
            return
        row = self._run.rows.get(row_id)
        if row is None or len(row.shape) != 2:
            self._show_heatmap_error(
                f"Row {row_id} is not a two-dimensional single-trace spectrum."
            )
            return
        if self._coordinates is None or self._coordinate_row_id != row_id:
            self._processing_dirty = True
            self._configure_coordinates()
            return

        if self.processing_controls.state.active and dict(row.definition).get("lab control role") == "spectrum_processed":
            self._show_heatmap_error("Choose a raw spectrum row before applying post-processing.")
            return
        self.load_button.setEnabled(False)
        self._start_read(self._selected_path, row)

    def _open_batch(self, *, power_point=False):
        row_id = self.row_combo.currentData()
        if self._coordinates is None or row_id != self._coordinate_row_id or self._selected_archive_identity is None:
            return
        row = self._run.rows[row_id]
        request = self._request()
        # The visible plane may have a narrower, automatic range because
        # one series has fewer points. Whole-sweep export applies only
        # ranges the operator explicitly set on the plotted axes.
        filters = {key: value for key, value in request.filters.items()
                   if key == "frequency" or key in self._explicit_axis_ranges}
        series_views = {series: view for (saved_row, x_id, y_id, series), view in self._series_views.items()
                        if (saved_row, x_id, y_id) == (row.id, request.x_id, request.y_id)}
        items = plan_heatmaps(self._coordinates, replace(request, filters=filters), self._points,
                             series_views=series_views)
        if not items:
            self._show_plot_status("No recorded measurement matches the selected axes and ranges.")
            return
        processing, map_state, palette_name, absolute = (
            self._shared_series_settings if series_views and self._shared_series_settings is not None
            else self._current_series_settings()
        )
        def palette_colours(name):
            return tuple(map(tuple, pg.colormap.get(name).getLookupTable(nPts=256, mode="float")))

        try:
            colours = palette_colours(palette_name)
            series_palettes = {name: palette_colours(name) for name in {view.palette for view in series_views.values()}}
        except (OSError, ValueError, KeyError) as exc:
            self._show_plot_status(f"Cannot export a saved heatmap palette: {exc}")
            return
        preview = manual_levels = None
        if self.heatmap.batch_button.isEnabled():
            preview = self.heatmap.plot.grab()
            shown_levels = tuple(map(float, self.heatmap.color_bar.levels()))
            if not series_views and shown_levels != self._auto_colour_levels:
                manual_levels = shown_levels
        cached_row = self._archive_cache.preloaded_row(row.id) if self._archive_cache is not None else None
        state = HeatmapBatch(row, self._coordinates, self._points, processing, map_state, absolute,
                             palette_name, colours, manual_levels, cached_row, series_palettes)
        signal = self.powerpoint_requested if power_point else self.batch_requested
        signal.emit(self._selected_archive_identity, items, state, preview)

    def _series_view_key(self):
        return (self.row_combo.currentData(), self.x_axis_combo.currentData(),
                self.y_axis_combo.currentData(), self.series_combo.currentData())

    def _reset_series_views(self):
        self._series_views = {}
        self._series_view_error = ""
        self._pending_series_view = None
        self._shared_series_settings = None
        self._series_entry_settings = None

    def _update_series_view_status(self):
        key = self._series_view_key()
        if self._series_view_error:
            self.series_view_status.setText("Saved views unavailable")
            self.series_view_status.setToolTip(self._series_view_error)
        else:
            self.series_view_status.setText(
                "Saved view" if key in self._series_views else "No saved view" if key[-1] else "Select one series"
            )
            self.series_view_status.setToolTip("Views are saved per series, data row and axes.")
        self.save_series_view_button.setEnabled(
            bool(key[-1] and self._selected_archive_identity and self.heatmap.batch_button.isEnabled())
        )

    def _save_series_view(self):
        if not self.save_series_view_button.isEnabled() or self._displayed_payload is None:
            return
        row, payload = self._displayed_payload
        request = self._request()
        if row.id != self.row_combo.currentData() or payload.request != request:
            self._show_plot_status("Load the selected series before saving its view.")
            return
        try:
            plot = self.heatmap.plot
            axes = plot.getViewBox().state
            view = SeriesHeatmapView(
                row.id, request, self.heatmap.colormap_combo.currentText(),
                tuple(map(float, self.heatmap.color_bar.levels())),
                tuple(tuple(map(float, limits)) for limits in plot.viewRange()),
                (bool(axes["xInverted"]), bool(axes["yInverted"])),
                self.heatmap.show_absolute.isEnabled() and self.heatmap.show_absolute.isChecked(),
                self.processing_controls.state, self.map_controls.state,
            )
            self._series_views = save_series_heatmap_view(self._selected_archive_identity, view)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._show_plot_status(f"Cannot save series view: {exc}")
            return
        if self._shared_series_settings is None:
            self._shared_series_settings = self._series_entry_settings
        self._series_view_error = ""
        self._update_series_view_status()
        self._show_plot_status(f"Saved heatmap view for Series {view.key[-1]}.")

    def _prepare_series_view(self):
        view = self._series_views.get(self._series_view_key())
        self._pending_series_view = view
        self._update_series_view_status()
        if view is None:
            if self._shared_series_settings is None:
                self._series_entry_settings = self._current_series_settings()
                return False
            settings, self._shared_series_settings = self._shared_series_settings, None
            self._series_entry_settings = settings
        else:
            if self._shared_series_settings is None:
                self._shared_series_settings = self._current_series_settings()
            settings = view.processing, view.map_state, view.palette, view.absolute
            self._rebuild_filters({key: _selection_range(value) for key, value in view.request.filters.items()})
        processing, map_state, palette, absolute = settings
        self.processing_controls.set_state(processing)
        self.variant_combo.setEnabled(not processing.active)
        self.map_controls.set_state(map_state)
        self.heatmap.colormap_combo.setCurrentText(palette)
        checkbox = self.heatmap.show_absolute
        blocked = checkbox.blockSignals(True)
        checkbox.setChecked(absolute)
        checkbox.blockSignals(blocked)
        return view is not None

    def _current_series_settings(self):
        return (self.processing_controls.state, self.map_controls.state,
                self.heatmap.colormap_combo.currentText(), self.heatmap.show_absolute.isChecked())

    def _render_payload(self, row: ThatecRow, payload: _HeatmapPayload) -> None:
        self._displayed_payload = (row, payload)
        label = ("Post-processed spectrum" if self.processing_controls.state.active
                 else row.control_name or row.device_name or row.id)
        state = self.map_controls.state
        if state.active:
            label = ("Common component" if state.view == "component" else
                     "Input spectrum" if state.view == "input" else
                     "Removed by 2D filter" if state.view == "removed" else
                     "Differential power" if state.component.endswith("power") else
                     "Relative contrast" if state.component == "median_db" else "Spectrum")
            label += f" ({payload.z_unit})" if payload.z_unit else ""
        palette_key = (state.component, state.image_filter, state.view, payload.z_unit)
        if palette_key != self._automatic_palette_key:
            if ((state.view == "result" and state.component != "none" or state.view == "removed")
                    and payload.levels[0] < 0 < payload.levels[1]):
                self.heatmap.colormap_combo.setCurrentText("CET-D1")
            self._automatic_palette_key = palette_key
        matrix, levels, z_label, signed, absolute = _heatmap_display(
            payload, row, self.processing_controls.state, state, self.heatmap.show_absolute.isChecked())
        self.heatmap.show_absolute.setEnabled(signed)
        self.heatmap.set_data(
            matrix,
            x_values=payload.x_values,
            y_values=payload.y_values,
            x_label=payload.x_label,
            x_unit=payload.x_unit,
            y_label=payload.y_label,
            y_unit=payload.y_unit,
            z_label=z_label, z_unit=payload.z_unit,
            levels=levels,
            cell_checkpoint_indices=payload.cell_checkpoints,
        )
        self._auto_colour_levels = tuple(map(float, self.heatmap.color_bar.levels()))
        checkpoints = np.unique(payload.cell_checkpoints[payload.cell_checkpoints >= 0]).tolist()
        request = payload.request or self._request()
        coordinates = self._active_coordinates()
        points_by_index = {p.index: p for p in self._points}
        parameters = measurement_parameters(coordinates, checkpoints, points_by_index)
        self.heatmap._export_context = (coordinates, points_by_index)
        title = heatmap_title(coordinates, request, parameters)
        self.heatmap.plot.setTitle(escape(title))
        self.heatmap.export_metadata = {
            "source_file": str(self._selected_path), "spectrum_row": row.id,
            "title": title,
            "spectrum_processing": processing_manifest(self.processing_controls.state),
            "recorded_baselines": [processing_manifest(r) for r in payload.baselines],
            "map_processing": processing_manifest(self.map_controls.state),
            "line_classification_source": "Recorded dBm, same selected coordinate plane" if state.mask_lines else None,
            "coordinate_selection": processing_manifest(request),
            "axes": {"x": {"label": payload.x_label, "unit": payload.x_unit},
                     "y": {"label": payload.y_label, "unit": payload.y_unit}},
            "value_unit": payload.z_unit, "notes": list(payload.processing_notes),
            "corrected_frequencies_hz": list(payload.corrected_frequencies_hz),
            "display_transform": "absolute_difference" if absolute else "none",
            "signed_difference": signed,
        }
        if payload.instrument_averages:
            self.heatmap.export_metadata["instrument_averages_by_checkpoint"] = payload.instrument_averages
        view = self._pending_series_view
        self._pending_series_view = None
        if view is not None and view.key == self._series_view_key() and view.request == request:
            self.heatmap.colormap_combo.setCurrentText(view.palette)
            self.heatmap.color_bar.setLevels(view.colour_levels)
            self.heatmap.plot.getViewBox().invertX(view.inverted_axes[0])
            self.heatmap.plot.getViewBox().invertY(view.inverted_axes[1])
            self.heatmap.plot.setRange(xRange=view.view_ranges[0], yRange=view.view_ranges[1], padding=0)
        unit = f" {payload.z_unit}" if payload.z_unit else ""
        missing = (
            f" {payload.missing_checkpoints} unreadable checkpoint(s) are shown as gaps."
            if payload.missing_checkpoints
            else ""
        )
        message = (
            f"Heatmap loaded: {payload.matrix.shape[0]} x {payload.matrix.shape[1]}, "
            f"range {levels[0]:.4g} to {levels[1]:.4g}{unit}."
            f"{missing} Click to inspect the source checkpoint spectrum."
        )
        checkpoint_count = len(checkpoints)
        message += f" {checkpoint_count} recorded checkpoint(s) selected."
        if payload.instrument_averages:
            counts = {average["sweep_count"] for average in payload.instrument_averages.values()}
            sweeps = f"{min(counts)}–{max(counts)} sweeps" if len(counts) > 1 else f"{min(counts)} sweeps each"
            message += f" {len(payload.instrument_averages)} Anritsu averages ({sweeps})."
        if checkpoint_count == 1:
            message += " Only one checkpoint is selected; this map does not show a sweep."
        if payload.processing_notes:
            message += " " + "; ".join(payload.processing_notes)
        self.info_label.setText(message)
        self.heatmap_view.setCurrentWidget(self.heatmap)
        if self.heatmap._data is not None and self.heatmap._data.size:
            self._update_cross_sections(
                self.heatmap._data.shape[0] // 2,
                self.heatmap._data.shape[1] // 2,
            )
            self._update_dispersion_overlay()
        self.load_button.setEnabled(True)
        self.heatmap.batch_button.setEnabled(self._coordinates is not None and self._selected_archive_identity is not None)
        self._update_series_view_status()
        self.status_changed.emit(f"Heatmap for {label} loaded")

    def _absolute_view_changed(self) -> None:
        if self._displayed_analysis is not None:
            self._render_analysis(self._displayed_analysis)
        elif self._displayed_payload is not None and self.heatmap_view.currentWidget() is self.heatmap:
            self._render_payload(*self._displayed_payload)

    def clear(self) -> None:
        """Reset the tab."""
        self._invalidate_pending_read()
        self.powerpoint_button.setEnabled(False)
        for combo in (self.row_combo, self.variant_combo, self.x_axis_combo, self.y_axis_combo):
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(blocked)
        self.heatmap.clear()
        self.heatmap.clear_dispersion_overlay()
        self.slice_curve.setData([], [])
        self.slice_marker.hide()
        self.vcut_curve.setData([], [])
        self.vcut_marker.hide()
        self._last_slice_cell = None
        self.slice_meta_label.setText(
            "Hover 2D map to inspect slice · click to open in Spectrum"
        )
        self.kittel_readout_label.setText("")
        self._spectrum_rows = []
        self._spectrum_rows_by_variant = {}
        self._selected_path = None
        self._selected_archive_identity = None
        self._reset_series_views()
        self._source_cache = None
        self._source_cache_key = None
        self._source_caches.clear()
        self._clear_archive_cache()
        self._run = None
        self._points = ()
        self.processing_controls.set_references(())
        self.map_controls.set_state(MapProcessing())
        self._coordinates = None
        self._coordinate_row_id = None
        self._coordinate_cache.clear()
        self._frequency_values = ()
        self._processing_dirty = False
        self._source_payload = None
        self._source_row = None
        self._displayed_payload = None
        self._displayed_analysis = None
        self._range_combos = {}
        self._filter_combos = {}
        self._reset_series_filter()
        self.filter_host.hide()
        self.load_button.setEnabled(False)
        self._show_heatmap_state(
            "Select a result",
            "Choose an HDF5 result containing public spectral rows.",
        )

    def closeEvent(self, event):
        self._invalidate_pending_read()
        if not self._read_pool.waitForDone(5000) or not self.heatmap._export_pool.waitForDone(5000):
            event.ignore()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _processing_changed(self, _state=None) -> None:
        self._source_payload = None
        if self.processing_controls.state.active:
            index = self.variant_combo.findData("raw")
            if index >= 0 and self.variant_combo.currentIndex() != index:
                self.variant_combo.setCurrentIndex(index)
        self.variant_combo.setEnabled(not self.processing_controls.state.active)
        self._processing_dirty = True
        if self.isVisible() and self._coordinates is not None:
            self._processing_dirty = False
            self._load_selected_row()

    def _load_selected_row(self) -> None:
        row_id = self.row_combo.currentData()
        if row_id is not None:
            self.load_heatmap_for_row(str(row_id))

    def _clear_archive_cache(self) -> None:
        self._archive_cache = None
        self._archive_cache_key = None
        self._source_cache = None
        self.read_file_button.setText("Read file")
        self.read_file_button.setEnabled(False)

    def _read_file(self) -> None:
        if self._selected_path is None or self._run is None or not self._spectrum_rows:
            return
        self._invalidate_pending_read()
        self._source_cache = None
        self._source_caches.clear()
        self.read_file_button.setEnabled(False)
        self.load_button.setEnabled(False)
        request_id = self._active_read_request
        self._show_heatmap_state(
            "Reading file", "Loading the recorded spectra into memory…", loading=True,
            progress_total=sum(row.shape[0] for row in self._spectrum_rows) or None,
        )
        task = ResultReadTask(request_id, _read_heatmap_archive, self._selected_path,
                              self._run, tuple(self._spectrum_rows), self._points,
                              expected_identity=self._selected_archive_identity,
                              cooperative_cancel=True, report_progress=True)
        self._read_tasks[request_id] = task
        self._read_kinds[request_id] = "archive"
        task.signals.loaded.connect(self._archive_loaded)
        task.signals.progress.connect(self._archive_progress)
        task.signals.failed.connect(self._read_failed)
        task.signals.finished.connect(self._read_finished)
        self._read_pool.start(task)

    def _archive_progress(self, request_id: int, completed: int, total: int) -> None:
        if request_id == self._active_read_request and self._read_kinds.get(request_id) == "archive":
            self.heatmap_state.set_read_progress(completed, total)

    def _archive_loaded(self, request_id, payload) -> None:
        if request_id != self._active_read_request:
            return
        self._archive_cache_key, self._archive_cache, coordinates = payload
        self._coordinate_cache.update(coordinates)
        self.read_file_button.setText("File in memory")
        self._load_selected_row()

    def _row_changed(self, *_args: object) -> None:
        self.heatmap.batch_button.setEnabled(False)
        self._invalidate_pending_read()
        self._source_payload = None
        self.heatmap.clear()
        row_id = self.row_combo.currentData()
        if row_id is None:
            self.powerpoint_button.setEnabled(False)
            return
        if self._coordinates is None or row_id != self._coordinate_row_id:
            self._configure_coordinates()
        if self._archive_cache is not None:
            self._refresh_plane.start()
            return
        self._show_heatmap_state(
            "Heatmap ready to load",
            "Load the selected spectral row to compare all recorded checkpoints.",
            action_text="Load selected heatmap",
        )

    def _configure_coordinates(self) -> None:
        self._coordinates = None
        self.powerpoint_button.setEnabled(False)
        self.series_host.hide()
        self.filter_host.hide()
        if self._selected_path is None or self._run is None:
            return
        row_id = self.row_combo.currentData()
        row = self._run.rows.get(str(row_id)) if row_id is not None else None
        if row is None:
            return
        if row.id in self._coordinate_cache:
            self._accept_coordinates(row, self._coordinate_cache[row.id])
            return
        self.load_button.setEnabled(False)
        self._read_request += 1
        request_id = self._read_request
        self._active_read_request = request_id
        task = ResultReadTask(request_id, _read_coordinate_payload,
                              self._selected_path, self._run, row, self._points,
                              cooperative_cancel=True)
        self._read_tasks[request_id] = task
        self._read_kinds[request_id] = "coordinates"
        task.signals.loaded.connect(lambda rid, payload, row=row: self._read_loaded(rid, row, payload))
        task.signals.failed.connect(self._read_failed)
        task.signals.finished.connect(self._read_finished)
        self._read_pool.start(task)

    def _accept_coordinates(self, row, payload):
        coordinates, frequencies = payload
        self._coordinate_cache[row.id] = payload
        self._coordinates = coordinates
        self._frequency_values = frequencies
        self._coordinate_row_id = row.id
        self._populate_series_filter(coordinates)
        axis_dimensions = coordinates.axis_dimensions
        for combo in (self.x_axis_combo, self.y_axis_combo):
            combo.blockSignals(True)
            combo.clear()
            for dimension in axis_dimensions:
                suffix = f" ({dimension.unit})" if dimension.unit else ""
                combo.addItem(f"{dimension.label}{suffix}", userData=dimension.id)
        self.x_axis_combo.setCurrentIndex(0)
        # Prefer the innermost authored sweep. For sequential or partial runs
        # without a Cartesian recipe mapping, favor the most sampled axis.
        if self.series_combo.count() and any(axis.id == "keithley.A.current" for axis in axis_dimensions):
            y_id = "keithley.A.current"
        elif coordinates.sweep_dimension_ids:
            y_id = coordinates.sweep_dimension_ids[-1]
        else:
            y_id = max(reversed(axis_dimensions[1:]), key=lambda axis: len(set(axis.values))).id
        self.y_axis_combo.setCurrentIndex(self.y_axis_combo.findData(y_id))
        for combo in (self.x_axis_combo, self.y_axis_combo):
            combo.blockSignals(False)
        self._rebuild_filters()
        self.load_button.setEnabled(True)
        self.powerpoint_button.setEnabled(self._selected_archive_identity is not None)
        if self._prepare_series_view():
            self._processing_dirty = True
        if self._processing_dirty and self.isVisible():
            self._processing_dirty = False
            self._load_selected_row()

    def _active_coordinates(self) -> HeatmapCoordinates:
        if self._coordinates is None:
            raise ValueError("Heatmap coordinates are not available for this row.")
        return self._coordinates

    def _request(self) -> HeatmapRequest:
        filters: dict[str, object] = {
            dimension.id: dimension.values[0]
            for dimension in self._active_coordinates().dimensions
            if dimension.is_constant and dimension.id != SERIES_DIMENSION_ID
        }
        filters.update({
            dimension_id: (
                float(minimum.currentData()),
                float(maximum.currentData()),
            )
            for dimension_id, (minimum, maximum) in self._range_combos.items()
            if minimum.currentData() is not None and maximum.currentData() is not None
        })
        filters.update(
            {
                dimension_id: float(combo.currentData())
                for dimension_id, combo in self._filter_combos.items()
                if combo.currentData() is not None
            }
        )
        if (series := self.series_combo.currentData()) is not None:
            filters[SERIES_DIMENSION_ID] = series
        return HeatmapRequest(
            str(self.x_axis_combo.currentData()),
            str(self.y_axis_combo.currentData()),
            filters,
        )

    def _axis_changed(self, *_args: object) -> None:
        if self.x_axis_combo.currentData() == self.y_axis_combo.currentData():
            alternate = 0 if self.x_axis_combo.currentIndex() else 1
            self.y_axis_combo.blockSignals(True)
            self.y_axis_combo.setCurrentIndex(alternate)
            self.y_axis_combo.blockSignals(False)
        self._explicit_axis_ranges.clear()
        self._rebuild_filters()
        self._row_changed()
        if self._prepare_series_view():
            self._refresh_plane.start()

    def _reset_series_filter(self) -> None:
        blocked = self.series_combo.blockSignals(True)
        self.series_combo.clear()
        self.series_combo.blockSignals(blocked)
        self.series_host.hide()
        self._explicit_axis_ranges.clear()

    def _populate_series_filter(self, coordinates: HeatmapCoordinates) -> None:
        previous = self.series_combo.currentData()
        had_choices = self.series_combo.count() > 0
        self._reset_series_filter()
        series = next((d for d in coordinates.dimensions if d.id == SERIES_DIMENSION_ID), None)
        if series is None:
            return
        blocked = self.series_combo.blockSignals(True)
        self.series_combo.addItem("All recorded series", userData=None)
        fixed = [d for d in coordinates.dimensions if d.id == "keithley.B.current"
                 or (d.id.startswith("moke_box.vout") and d.id.endswith(".voltage"))]
        numbers = tuple(map(int, series.values))
        records = {number: [set() for _ in fixed] for number in sorted(set(numbers))}
        for index, number in enumerate(numbers):
            values = records[number]
            for dimension, observed in zip(fixed, values, strict=True):
                observed.add(dimension.values[index])
        for number in records:
            text = f"Series {number}" if number else "Without a recorded series"
            if number:
                for dimension, values in zip(fixed, records[number], strict=True):
                    if len(values) == 1:
                        value = self._format_coordinate_value(dimension, next(iter(values)))
                        label = "Keithley B" if dimension.id == "keithley.B.current" else dimension.label.split(" programming")[0]
                        text += f" · {label} {value}"
            self.series_combo.addItem(text, userData=number)
        first = min(number for number in records if number)
        wanted = previous if had_choices else first
        index = self.series_combo.findData(wanted)
        self.series_combo.setCurrentIndex(index if index >= 0 else self.series_combo.findData(first))
        self.series_combo.blockSignals(blocked)
        self.series_host.show()

    def _series_changed(self, *_args: object) -> None:
        if self._coordinates is None:
            return
        was_displayed = self.heatmap_view.currentWidget() is self.heatmap and self.heatmap._data is not None
        self._explicit_axis_ranges.clear()
        self._rebuild_filters()
        self._row_changed()
        self.heatmap.plot.getViewBox().invertX(False)
        self.heatmap.plot.getViewBox().invertY(False)
        if self._prepare_series_view() or was_displayed:
            self._refresh_plane.start()

    def _linked_ranges(self, preferred=None):
        return linked_heatmap_ranges(
            self._active_coordinates(), str(self.x_axis_combo.currentData()),
            str(self.y_axis_combo.currentData()), series=self.series_combo.currentData(),
            preferred=preferred,
        )

    def _rebuild_filters(self, preferred=None) -> None:
        while self.filter_layout.count():
            item = self.filter_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        self._range_combos = {}
        self._filter_combos = {}
        if self._coordinates is None:
            self.filter_host.hide()
            return
        axes = {self.x_axis_combo.currentData(), self.y_axis_combo.currentData()}
        ranges = self._linked_ranges(preferred)
        physical_dimensions = [
            self._coordinates.dimension(dimension_id) for dimension_id in ranges
        ]
        section = None
        # In a spectral map the fixed operating point comes before its
        # dependent plotted current range.
        if "frequency" not in axes:
            physical_dimensions.sort(key=lambda dimension: dimension.id not in axes)
        for dimension in physical_dimensions:
            heading = "Axis ranges" if dimension.id in axes else "Slice filters"
            if section != heading:
                section = heading
                title = CaptionLabel(heading, self.filter_host)
                title.setObjectName("muted")
                self.filter_layout.addWidget(title)
            values, selected = ranges[dimension.id]
            if dimension.is_constant:
                value = self._format_coordinate_value(dimension, values[0])
                label = CaptionLabel(f"{dimension.label}: {value}", self.filter_host)
                label.setObjectName("heatmapFixedCoordinate")
                label.setWordWrap(True)
                self.filter_layout.addWidget(label)
            elif dimension.id == VISIT_DIMENSION_ID and dimension.id not in axes:
                self._add_visit_filter(values, selected.minimum)
            else:
                self._add_range_row(dimension, values, selected_range=selected)
        if "frequency" not in axes:
            if section != "Slice filters":
                title = CaptionLabel("Slice filters", self.filter_host)
                title.setObjectName("muted")
                self.filter_layout.addWidget(title)
            row = QWidget(self.filter_host)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            freq_lbl = CaptionLabel("Frequency:", row)
            freq_lbl.setObjectName("muted")
            row_layout.addWidget(freq_lbl)
            combo = FilterComboBox(row)
            combo.setMinimumWidth(150)
            combo.setAccessibleName("Heatmap frequency slice")
            frequency = self._coordinates.dimension("frequency")
            combo.set_options(("Choose a recorded frequency", None), self._frequency_values,
                              lambda value: self._format_coordinate_value(frequency, float(value)))
            if self._frequency_values:
                bounds = preferred.get("frequency") if preferred else None
                combo.setCurrentIndex(max(1, combo.findData(bounds.minimum)) if bounds else 1)
            combo.currentIndexChanged.connect(self._frequency_changed)
            row_layout.addWidget(combo)
            row_layout.addStretch(1)
            self.filter_layout.addWidget(row)
            self._filter_combos["frequency"] = combo
        self.filter_layout.addStretch(1)
        self.filter_host.setVisible(bool(physical_dimensions or self._filter_combos))

    def _add_visit_filter(self, values: list[float], selected: float) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)
        label = CaptionLabel("Visit:", self.filter_host)
        label.setObjectName("muted")
        row.addWidget(label)
        combo = ComboBox(self.filter_host)
        combo.setAccessibleName("Visit at the same physical settings")
        combo.setToolTip("Select which recorded visit to show at repeated sweep settings.")
        for value in values:
            combo.addItem(f"{value:g}", userData=value)
        combo.setCurrentIndex(combo.findData(selected))
        combo.setEnabled(len(values) > 1)
        combo.currentIndexChanged.connect(self._refresh_linked_ranges)
        row.addWidget(combo, 1)
        self.filter_layout.addLayout(row)
        self._filter_combos[VISIT_DIMENSION_ID] = combo

    def _add_range_row(
        self,
        dimension: HeatmapDimension,
        values: list[float],
        *,
        selected_range: HeatmapRange,
    ) -> None:
        """Add two compact recorded-value selectors for one physical coordinate."""

        if not values:
            return
        row = QWidget(self.filter_host)
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 2)
        outer.setSpacing(2)
        label = CaptionLabel(f"{dimension.label}:", row)
        label.setWordWrap(True)
        outer.addWidget(label)
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        from_lbl = CaptionLabel("from", row)
        from_lbl.setObjectName("muted")
        row_layout.addWidget(from_lbl)
        minimum = ComboBox(row)
        minimum.setMinimumWidth(84)
        minimum.setAccessibleName(f"{dimension.label} minimum")
        minimum.setObjectName("heatmapRangeMinimum")
        row_layout.addWidget(minimum, 1)
        to_lbl = CaptionLabel("to", row)
        to_lbl.setObjectName("muted")
        row_layout.addWidget(to_lbl)
        maximum = ComboBox(row)
        maximum.setMinimumWidth(84)
        maximum.setAccessibleName(f"{dimension.label} maximum")
        maximum.setObjectName("heatmapRangeMaximum")
        row_layout.addWidget(maximum, 1)
        outer.addLayout(row_layout)
        for value in values:
            text = self._format_coordinate_value(dimension, value)
            minimum.addItem(text, userData=value)
            maximum.addItem(text, userData=value)
        minimum.setCurrentIndex(minimum.findData(selected_range.minimum))
        maximum.setCurrentIndex(maximum.findData(selected_range.maximum))
        minimum.setEnabled(len(values) > 1)
        maximum.setEnabled(len(values) > 1)
        dimension_id = str(dimension.id)
        minimum.currentIndexChanged.connect(
            lambda _index, dimension_id=dimension_id: self._range_changed(
                dimension_id, "minimum"
            )
        )
        maximum.currentIndexChanged.connect(
            lambda _index, dimension_id=dimension_id: self._range_changed(
                dimension_id, "maximum"
            )
        )
        self.filter_layout.addWidget(row)
        self._range_combos[dimension_id] = (minimum, maximum)

    def _range_changed(self, dimension_id: str, edge: str) -> None:
        controls = self._range_combos.get(dimension_id)
        if controls is None:
            return
        minimum, maximum = controls
        lower, upper = minimum.currentData(), maximum.currentData()
        if lower is None or upper is None:
            return
        if lower > upper:
            target = maximum if edge == "minimum" else minimum
            replacement = minimum.currentIndex() if edge == "minimum" else maximum.currentIndex()
            target.blockSignals(True)
            target.setCurrentIndex(replacement)
            target.blockSignals(False)
        if dimension_id in {self.x_axis_combo.currentData(), self.y_axis_combo.currentData()}:
            if minimum.currentIndex() == 0 and maximum.currentIndex() == maximum.count() - 1:
                self._explicit_axis_ranges.discard(dimension_id)
            else:
                self._explicit_axis_ranges.add(dimension_id)
        self._refresh_linked_ranges()

    def _refresh_linked_ranges(self, *_args: object) -> None:
        axes = {self.x_axis_combo.currentData(), self.y_axis_combo.currentData()}
        preferred = {}
        for key, (lower, upper) in self._range_combos.items():
            if key not in axes or key in self._explicit_axis_ranges:
                preferred[key] = HeatmapRange(lower.currentData(), upper.currentData())
        visit = self._filter_combos.get(VISIT_DIMENSION_ID)
        if visit is not None and visit.currentData() is not None:
            preferred[VISIT_DIMENSION_ID] = HeatmapRange(visit.currentData(), visit.currentData())
        for key, (values, bounds) in self._linked_ranges(preferred).items():
            dimension = self._active_coordinates().dimension(key)
            pair = self._range_combos.get(key)
            if pair is not None:
                for combo, selected in zip(pair, (bounds.minimum, bounds.maximum), strict=True):
                    self._update_range_options(combo, dimension, values, selected)
            elif key == VISIT_DIMENSION_ID and visit is not None:
                self._update_range_options(visit, dimension, values, bounds.minimum)
        self._row_changed()

    def _update_range_options(self, combo, dimension, values, selected) -> None:
        blocked = combo.blockSignals(True)
        try:
            if tuple(combo.itemData(index) for index in range(combo.count())) != tuple(values):
                combo.clear()
                for value in values:
                    combo.addItem(self._format_coordinate_value(dimension, value), userData=value)
            combo.setCurrentIndex(combo.findData(selected))
            combo.setEnabled(len(values) > 1)
        finally:
            combo.blockSignals(blocked)

    def _frequency_changed(self, *_args: object) -> None:
        self._row_changed()

    @staticmethod
    def _clear_layout(layout: QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout() is not None:
                HeatmapResultsTab._clear_layout(item.layout())

    @staticmethod
    def _format_coordinate_value(dimension: HeatmapDimension, value: float) -> str:
        """Format known SI coordinates with the registered engineering unit."""

        quantity_dimension = DIMENSION_FREQUENCY if dimension.is_frequency else None
        if quantity_dimension is None:
            try:
                quantity_dimension = parameter_descriptor(dimension.id).dimension
            except KeyError:
                quantity_dimension = None
        if quantity_dimension is not None:
            try:
                return format_quantity_auto(value, quantity_dimension)
            except ValueError:
                pass
        suffix = f" {dimension.unit}" if dimension.unit else ""
        return f"{value:.9g}{suffix}"

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
        self._row_changed()

    def _start_read(self, path: Path, row: ThatecRow) -> None:
        self.heatmap.batch_button.setEnabled(False)
        # Reuse full raw grids and baselines for the two most recent planes.
        # File changes receive a fresh cache; never show stale archive values.
        try:
            identity = _archive_identity(path)
        except OSError as exc:
            self.load_button.setEnabled(True)
            self._show_heatmap_error(f"Cannot read the result archive: {exc}")
            return
        request = self._request()
        if self._archive_cache is not None and identity != self._archive_cache_key:
            self._clear_archive_cache()
            self.read_file_button.setEnabled(True)
            self._show_heatmap_error("The archive changed. Reopen the result to refresh its data.")
            return
        cache_key = (*identity, row.id,
                     request.x_id, request.y_id, tuple(sorted(request.filters.items())))
        reused = self._archive_cache is not None or cache_key in self._source_caches
        self._source_cache = self._archive_cache or self._source_caches.pop(cache_key, None) or HeatmapSourceCache()
        if self._archive_cache is None:
            self._source_caches[cache_key] = self._source_cache
        self._source_cache_key = cache_key
        # Raw and the recorded processed row can coexist; do not accumulate
        # every visited plane or file in memory.
        while len(self._source_caches) > 2:
            self._source_caches.popitem(last=False)
        if not reused:
            self._show_heatmap_state(
                "Loading heatmap",
                f"Reading {row.shape[0]:,} checkpoints and {row.shape[0] * row.shape[1]:,} spectral samples...",
                loading=True,
            )
        else:
            self.info_label.setText("Recalculating from cached spectra… original data unchanged.")
        self._read_request += 1
        request_id = self._read_request
        self._active_read_request = request_id
        task = ResultReadTask(
            request_id,
            _read_heatmap_payload,
            path,
            row,
            self._active_coordinates(),
            request,
            cooperative_cancel=True,
            processing=self.processing_controls.state, points=self._points,
            source_cache=self._source_cache,
        )
        self._read_tasks[request_id] = task
        self._read_kinds[request_id] = "matrix"
        task.signals.loaded.connect(
            lambda loaded_id, payload, row=row: self._read_loaded(
                loaded_id, row, payload
            )
        )
        task.signals.failed.connect(self._read_failed)
        task.signals.finished.connect(self._read_finished)
        filters = [box.text() for box in self.processing_controls.filters.values() if box.isChecked()]
        self.processing_overlay.begin(request_id, "Recalculating heatmap…",
            "Processing each recorded spectrum" + (": " + " · ".join(filters) if filters else "."))
        self._read_pool.start(task)

    def _read_loaded(
        self, request_id: int, row: ThatecRow, payload: object
    ) -> None:
        if request_id != self._active_read_request:
            return
        if self._read_kinds.get(request_id) == "coordinates":
            self._accept_coordinates(row, payload)
            return
        if not isinstance(payload, _HeatmapPayload):
            self._show_heatmap_error(
                "The HDF5 reader returned an unsupported heatmap payload."
            )
            return
        if self._read_kinds.get(request_id) == "map":
            self._render_payload(row, payload)
        else:
            self._source_payload = payload
            self._source_row = row
            self._set_map_coordinates(payload)
            self._map_changed()

    def _set_map_coordinates(self, payload):
        frequency_on_x = self.x_axis_combo.currentData() == "frequency"
        axis_id = self.y_axis_combo.currentData() if frequency_on_x else self.x_axis_combo.currentData()
        if self._coordinates is None or axis_id is None:
            return
        dimension = self._coordinates.dimension(str(axis_id))
        values = payload.y_values if frequency_on_x else payload.x_values
        self.map_controls.set_coordinates(values, lambda value: self._format_coordinate_value(dimension, value))

    def _map_changed(self, _state=None):
        # Retain one input matrix. Map-only edits never re-read HDF5, and all
        # numeric processing stays in the existing serial result worker.
        if self._source_payload is None or self._source_row is None:
            return
        self.heatmap.batch_button.setEnabled(False)
        self._invalidate_pending_read()
        row, source = self._source_row, self._source_payload
        if not self.map_controls.state.active:
            self._render_payload(row, source)
            return
        frequency_axis = (1 if self.x_axis_combo.currentData() == "frequency" else
                          0 if self.y_axis_combo.currentData() == "frequency" else None)
        self._read_request += 1
        request_id = self._read_request
        self._active_read_request = request_id
        task = ResultReadTask(request_id, _process_map_payload, source,
                              self.map_controls.state, frequency_axis, cooperative_cancel=True)
        self._read_tasks[request_id] = task
        self._read_kinds[request_id] = "map"
        task.signals.loaded.connect(lambda rid, payload, row=row: self._read_loaded(rid, row, payload))
        task.signals.failed.connect(self._read_failed)
        task.signals.finished.connect(self._read_finished)
        self.load_button.setEnabled(False)
        self.info_label.setText("Analysing the selected map slice… original data unchanged.")
        self.processing_overlay.begin(request_id, "Recalculating heatmap…",
            "Applying filters to the selected two-dimensional map.")
        self._read_pool.start(task)

    def _read_failed(self, request_id: int, message: str) -> None:
        if request_id == self._active_read_request:
            if self._read_kinds.get(request_id) == "archive":
                self._show_heatmap_state("Cannot read file into memory", message, action_text="Load selected heatmap")
                self.status_changed.emit(message)
            else:
                self._show_heatmap_error(message)

    def _read_finished(self, request_id: int) -> None:
        self._read_tasks.pop(request_id, None)
        self._read_kinds.pop(request_id, None)
        self.processing_overlay.finish(request_id)
        if request_id == self._active_read_request:
            self.load_button.setEnabled(self.row_combo.currentData() is not None)
            self.read_file_button.setEnabled(bool(self._spectrum_rows) and self._archive_cache is None)

    def _invalidate_pending_read(self) -> None:
        self._refresh_plane.stop()
        self.save_series_view_button.setEnabled(False)
        if "archive" in self._read_kinds.values() and self._archive_cache is None:
            self.read_file_button.setEnabled(bool(self._spectrum_rows))
        self.processing_overlay.clear()
        for task in self._read_tasks.values():
            task.cancel()
        self._read_pool.clear()
        self._read_tasks.clear()
        self._read_kinds.clear()
        self._read_request += 1
        self._active_read_request = self._read_request

    def _show_heatmap_state(
        self,
        title: str,
        description: str,
        *,
        loading: bool = False,
        action_text: str = "",
        progress_total: int | None = None,
    ) -> None:
        self.heatmap.batch_button.setEnabled(False)
        self.save_series_view_button.setEnabled(False)
        self.heatmap_state.show_state(
            title=title,
            description=description,
            accessible_name=title,
            loading=loading,
            action_text=action_text,
            progress_total=progress_total,
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
