"""Heatmap visualization tab for spectral sweep results."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter, SVGExporter
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, ComboBox, PushButton
from app.ui.dialogs import StationFileDialog as QFileDialog

from app.storage import ThatecRow, ThatecRun, ThatecRunReader
from app.ui.design_system import plot_theme, tokens_for
from app.ui.results.data_classifier import find_spectrum_rows


class HeatmapPlotWidget(QWidget):
    """Interactive 2-D heatmap built on ``pyqtgraph.ImageItem``.

    Axes:
        X — frequency (or spectrum sample index), derived from THATEC scale.
        Y — checkpoint index (or swept parameter value when available).
        Color — measured amplitude (typically dBm).
    """

    checkpoint_clicked = Signal(int)  # emitted when user clicks a row

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: np.ndarray | None = None  # shape (checkpoints, freq_points)
        self._x_values: np.ndarray | None = None
        self._y_values: np.ndarray | None = None
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

        self.image_item = pg.ImageItem()
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
    ) -> None:
        """Set the 2-D data matrix and update the plot.

        Parameters
        ----------
        data : ndarray, shape (rows, cols)
            The 2-D heatmap matrix (e.g. checkpoints × frequency bins).
        x_values, y_values : optional axis value arrays
            Physical values for the horizontal and vertical axes.
        """
        self._data = np.asarray(data, dtype=float)
        rows, cols = self._data.shape

        if x_values is not None:
            self._x_values = np.asarray(x_values, dtype=float)
        else:
            self._x_values = np.arange(cols, dtype=float)

        if y_values is not None:
            self._y_values = np.asarray(y_values, dtype=float)
        else:
            self._y_values = np.arange(rows, dtype=float)

        # Transform to correctly position the image
        x_min, x_max = float(self._x_values[0]), float(self._x_values[-1])
        y_min, y_max = float(self._y_values[0]), float(self._y_values[-1])
        dx = (x_max - x_min) / max(cols - 1, 1)
        dy = (y_max - y_min) / max(rows - 1, 1)

        self.image_item.setImage(self._data.T)  # ImageItem expects (cols, rows)
        self.image_item.setRect(x_min - dx / 2, y_min - dy / 2, (x_max - x_min) + dx, (y_max - y_min) + dy)

        self.plot.setLabel("bottom", x_label, units=x_unit)
        self.plot.setLabel("left", y_label, units=y_unit if y_unit else None)
        self.color_bar.setLabel(z_label)

        self._apply_colormap(self.colormap_combo.currentText())
        finite = self._data[np.isfinite(self._data)]
        if finite.size > 0:
            self.color_bar.setLevels((float(finite.min()), float(finite.max())))
        self.auto_range()

    def clear(self) -> None:
        self.image_item.clear()
        self._data = None
        self._x_values = None
        self._y_values = None
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
        if "PNG" in selected or suffix == ".png":
            ImageExporter(self.plot.plotItem).export(path)
        elif "SVG" in selected or suffix == ".svg":
            SVGExporter(self.plot.plotItem).export(path)
        else:
            self._export_csv(Path(path))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_colormap(self, name: str) -> None:
        try:
            cmap = pg.colormap.get(name)
        except Exception:
            cmap = pg.colormap.get("viridis")
        self.image_item.setLookupTable(cmap.getLookupTable(nPts=256))

    def _mouse_moved(self, event: tuple[object, ...]) -> None:
        position = event[0]
        if not self.plot.sceneBoundingRect().contains(position):
            return
        point = self.plot.plotItem.vb.mapSceneToView(position)
        x, y = float(point.x()), float(point.y())
        self.crosshair_x.setPos(x)
        self.crosshair_y.setPos(y)
        z = self._interpolate_z(x, y)
        z_text = f"{z:.4g}" if z is not None else "—"
        self.readout.setText(f"X: {x:.6g}   Y: {y:.4g}   Z: {z_text}")

    def _mouse_clicked(self, event: Any) -> None:
        if event.button() != 1 or self._data is None or self._y_values is None:
            return
        position = event.scenePos()
        if not self.plot.sceneBoundingRect().contains(position):
            return
        point = self.plot.plotItem.vb.mapSceneToView(position)
        y = float(point.y())
        # Find nearest checkpoint index
        distances = np.abs(self._y_values - y)
        nearest = int(np.argmin(distances))
        self.checkpoint_clicked.emit(nearest)

    def _interpolate_z(self, x: float, y: float) -> float | None:
        if self._data is None or self._x_values is None or self._y_values is None:
            return None
        if (
            x < self._x_values[0]
            or x > self._x_values[-1]
            or y < self._y_values[0]
            or y > self._y_values[-1]
        ):
            return None
        col = int(np.searchsorted(self._x_values, x, side="right") - 1)
        row = int(np.searchsorted(self._y_values, y, side="right") - 1)
        col = max(0, min(col, self._data.shape[1] - 1))
        row = max(0, min(row, self._data.shape[0] - 1))
        return float(self._data[row, col])

    def _export_csv(self, path: Path) -> None:
        if self._data is None or self._x_values is None or self._y_values is None:
            return
        with path.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            header = ["checkpoint/frequency"] + [
                f"{float(x):.9g}" for x in self._x_values
            ]
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
        self._spectrum_rows: list[ThatecRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Row selector ---
        selector = QHBoxLayout()
        selector.addWidget(BodyLabel("Spectrum row:"))
        self.row_combo = ComboBox(self)
        self.row_combo.setMinimumWidth(300)
        self.row_combo.setToolTip("Select a THATEC row with 2-D spectral data")
        selector.addWidget(self.row_combo, 1)

        self.load_button = PushButton(parent=self)
        self.load_button.setText("Load heatmap")
        self.load_button.setToolTip("Read all checkpoints and render the heatmap")
        self.load_button.setObjectName("plotToolButton")
        selector.addWidget(self.load_button)
        selector.addStretch(1)
        layout.addLayout(selector)

        # --- Heatmap ---
        self.heatmap = HeatmapPlotWidget()
        layout.addWidget(self.heatmap, 1)

        self.info_label = BodyLabel("Select a spectrum row and click 'Load heatmap'.")
        self.info_label.setObjectName("muted")
        layout.addWidget(self.info_label)

        # --- Connections ---
        self.load_button.clicked.connect(self._load_selected_row)
        self.heatmap.checkpoint_clicked.connect(self._on_checkpoint_clicked)

    def apply_theme(self, theme: str) -> None:
        """Retheme the visible pyqtgraph scene with the application tokens."""

        self.heatmap.apply_theme(theme)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, path: Path, run: ThatecRun) -> None:
        """Prepare the tab with available spectrum rows from a THATEC result."""
        self._selected_path = path
        self._run = run
        self._spectrum_rows = find_spectrum_rows(run)

        self.row_combo.clear()
        self.heatmap.clear()
        for row in self._spectrum_rows:
            label = row.control_name or row.device_name or row.id
            detail = f"{label}  ({row.id}, {row.shape[0]}×{row.shape[1]})"
            self.row_combo.addItem(detail, userData=row.id)

        if self._spectrum_rows:
            self.info_label.setText(
                f"{len(self._spectrum_rows)} spectral row(s) available. "
                "Select one and click 'Load heatmap'."
            )
        else:
            self.info_label.setText("No 2-D spectral rows found in this result.")

    def load_heatmap_for_row(self, row_id: str) -> None:
        """Read all checkpoints for a given 2-D row and render the heatmap."""
        if self._selected_path is None or self._run is None:
            return
        row = self._run.rows.get(row_id)
        if row is None or len(row.shape) < 2:
            self.info_label.setText(f"Row {row_id} is not a 2-D spectral row.")
            return

        checkpoints = row.shape[0]
        freq_points = row.shape[1]
        matrix = np.full((checkpoints, freq_points), np.nan, dtype=float)
        x_values: np.ndarray | None = None

        self.info_label.setText(
            f"Loading {checkpoints} checkpoints for {row_id}…"
        )

        for checkpoint in range(checkpoints):
            try:
                data = ThatecRunReader.row_slice(
                    self._selected_path, row_id, checkpoint
                )
                values = np.asarray(data.values, dtype=float)
                if values.size == freq_points:
                    matrix[checkpoint, :] = values
                if x_values is None and len(data.scale) >= 2:
                    offset, multiplier = data.scale[0], data.scale[1]
                    x_values = np.array(
                        [offset + multiplier * i for i in range(freq_points)],
                        dtype=float,
                    )
            except Exception:
                continue  # Leave row as NaN

        if x_values is None:
            x_values = np.arange(freq_points, dtype=float)

        y_values = np.arange(checkpoints, dtype=float)
        label = row.control_name or row.device_name or row_id

        self.heatmap.set_data(
            matrix,
            x_values=x_values,
            y_values=y_values,
            x_label="Frequency",
            x_unit="Hz",
            y_label="Checkpoint",
            z_label="Amplitude (dBm)",
        )
        self.heatmap.plot.setTitle(
            f"{label} — {checkpoints} checkpoints × {freq_points} frequency bins"
        )
        finite = matrix[np.isfinite(matrix)]
        z_range = f"{float(finite.min()):.4g} … {float(finite.max()):.4g}" if finite.size else "—"
        self.info_label.setText(
            f"Heatmap loaded: {checkpoints}×{freq_points}, range {z_range} dBm. "
            "Click on the heatmap to select a checkpoint."
        )
        self.status_changed.emit(f"Heatmap for {label} loaded")

    def clear(self) -> None:
        """Reset the tab."""
        self.row_combo.clear()
        self.heatmap.clear()
        self._spectrum_rows = []
        self._selected_path = None
        self._run = None
        self.info_label.setText("Select a spectrum row and click 'Load heatmap'.")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_selected_row(self) -> None:
        row_id = self.row_combo.currentData()
        if row_id is not None:
            self.load_heatmap_for_row(row_id)

    def _on_checkpoint_clicked(self, checkpoint: int) -> None:
        row_id = self.row_combo.currentData()
        if row_id is not None:
            self.checkpoint_clicked.emit(row_id, checkpoint)

