"""Frequency versus spectrum number for the Execute workspace."""

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QRectF, QThreadPool
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, TransparentPushButton, isDarkTheme

from app.storage.hdf5_reader import Hdf5RunReader
from app.ui.design_system import plot_theme, tokens_for
from app.ui.results.workers import ResultReadTask
from app.ui.widgets.plot_ownership import create_plot_widget, own_plot_item_menus


class ExecutionSpectrogram(QWidget):
    """Incrementally read committed spectra without blocking the GUI thread."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._archive: Path | None = None
        self._requested_points = 0
        self._read_points = 0
        self._batch_end = 0
        self._request_id = 0
        self._reader: ResultReadTask | None = None
        self._rows: list[tuple[float, float, np.ndarray]] = []
        self._frequency_extent: tuple[float, float] | None = None
        self._active = False
        self.image: pg.ImageItem | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.status = CaptionLabel("Waiting for stored spectra", self)
        self.status.setWordWrap(True)
        self.fit_axes = TransparentPushButton("Auto fit", self)
        self.fit_axes.setAccessibleName("Auto fit spectrogram axes")
        self.fit_axes.setToolTip("Resume automatic fitting to frequency and acquisition number.")
        self.fit_axes.setEnabled(False)
        self.fit_axes.clicked.connect(self._fit_axes)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.fit_axes)
        toolbar.addWidget(self.status, 1)
        layout.addLayout(toolbar)

    def _create_plot(self) -> None:
        self.plot = create_plot_widget(self)
        self.plot.setObjectName("executionSpectrogramPlot")
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Spectrum number")
        self.image = pg.ImageItem(axisOrder="row-major", autoDownsample=True)
        self.plot.addItem(self.image)
        self.color_bar = pg.ColorBarItem(values=(-120.0, 0.0), colorMap=pg.colormap.get("viridis"), interactive=False)
        self.color_bar.setImageItem(self.image, insert_in=self.plot.getPlotItem())
        self.color_bar.axis.setLabel("Stored power", units="dBm")
        own_plot_item_menus(self.color_bar, self.plot)
        self.layout().addWidget(self.plot, 1)
        self._apply_theme()

    def set_archive(self, path: Path | None, stored_points: int = 0) -> None:
        if path is not None and self._archive is None:
            stored_points = max(stored_points, self._requested_points)
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        self._request_id += 1
        self._archive = path
        self._requested_points = 0
        self._read_points = 0
        self._rows.clear()
        self._frequency_extent = None
        if self.image is not None:
            self.image.clear()
            self._fit_axes()
        self.fit_axes.setEnabled(False)
        self.status.setText("Waiting for stored spectra")
        self.status.setToolTip("")
        self.set_stored_points(stored_points)

    def set_stored_points(self, count: int) -> None:
        self._requested_points = max(self._requested_points, count)
        self._request_rows()

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            if self.image is None:
                self._create_plot()
            self._render()
            self._request_rows()

    def _request_rows(self) -> None:
        if (not self._active or self._archive is None or self._reader is not None
                or self._read_points >= self._requested_points):
            return
        self._batch_end = min(self._read_points + 128, self._requested_points)
        self._request_id += 1
        self._reader = ResultReadTask(
            self._request_id, Hdf5RunReader.spectrum_preview_rows,
            self._archive, self._read_points, self._batch_end, cooperative_cancel=True,
        )
        self._reader.signals.loaded.connect(self._rows_loaded)
        self._reader.signals.failed.connect(self._read_failed)
        if not self._rows:
            self.status.setText("Loading stored spectra…")
        QThreadPool.globalInstance().start(self._reader)

    def _rows_loaded(self, request_id: int, rows: list) -> None:
        if request_id != self._request_id:
            return
        self._reader = None
        self._read_points = self._batch_end
        self._rows.extend(rows)
        for start_hz, stop_hz, _values in rows:
            left, right = self._frequency_extent or (start_hz, stop_hz)
            self._frequency_extent = min(left, start_hz), max(right, stop_hz)
        if self._active:
            self._render()
        self._request_rows()

    def _read_failed(self, request_id: int, message: str) -> None:
        if request_id == self._request_id:
            self._reader = None
            self.status.setText("Could not read stored spectra")
            self.status.setToolTip(message)

    def _fit_axes(self) -> None:
        view = self.plot.getViewBox()
        view.enableAutoRange()
        view.updateAutoRange()

    def _render(self) -> None:
        count = len(self._rows)
        if not count:
            self.status.setText("Waiting for stored spectra")
            return
        start_hz, stop_hz = self._frequency_extent
        indexes = np.linspace(0, count - 1, min(count, 2048), dtype=int)
        rows = [self._rows[index] for index in indexes]
        columns = max(len(values) for _left, _right, values in rows)
        grid = np.linspace(start_hz, stop_hz, columns)
        matrix = np.stack([
            values if left == start_hz and right == stop_hz and len(values) == columns
            else np.interp(grid, np.linspace(left, right, len(values)), values, left=np.nan, right=np.nan)
            for left, right, values in rows
        ]).astype(np.float32, copy=False)
        low, high = float(np.nanmin(matrix)), float(np.nanmax(matrix))
        levels = (low, high if high > low else low + 1.0)
        self.image.setImage(matrix, autoLevels=False, levels=levels)
        bin_hz = (stop_hz - start_hz) / (columns - 1)
        self.image.setRect(QRectF(start_hz - bin_hz / 2, 0.5, stop_hz - start_hz + bin_hz, count))
        self.color_bar.setLevels(levels)
        self.fit_axes.setEnabled(True)
        self.status.setToolTip("")
        summary = f"Stored spectra: {count}"
        if len(rows) < count:
            summary += f" · {len(rows)} displayed"
        self.status.setText(summary)

    def event(self, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange} and hasattr(self, "color_bar"):
            self._apply_theme()
        return super().event(event)

    def _apply_theme(self) -> None:
        theme = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(theme.background)
        for axis in (self.plot.getAxis("left"), self.plot.getAxis("bottom"), self.color_bar.axis):
            axis.setPen(pg.mkPen(theme.axes))
            axis.setTextPen(pg.mkPen(theme.axes))
