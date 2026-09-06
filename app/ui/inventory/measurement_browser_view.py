"""Commercial laboratory-grade measurement browser split-view combining tree, plot, and analytics."""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import SimpleCardWidget

from app.inventory.analysis import calculate_mtj_metrics
from app.inventory.models import Sample, SampleRunRecord
from app.storage.hdf5_series_reader import Hdf5SeriesReader, MeasurementSeries
from app.ui.inventory.measurement_card import MeasurementAnalyticsCard
from app.ui.inventory.measurement_plot import MeasurementPlotWidget
from app.ui.inventory.measurement_tree import MeasurementTreeWidget


class MeasurementBrowserView(QWidget):
    """Splitter-based browser hosting the measurement tree, interactive plot, and physical analytics."""

    open_in_results_requested = Signal(str)  # run_path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sample: Sample | None = None
        self._runs: list[SampleRunRecord] = []
        self._series_cache: dict[tuple[str, str | None], MeasurementSeries] = {}
        self._current_channel: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)

        # Left Panel: Measurement Tree Widget inside a card container
        self.left_card = SimpleCardWidget(self.splitter)
        left_layout = QVBoxLayout(self.left_card)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(6)

        self.tree_widget = MeasurementTreeWidget(self.left_card)
        self.tree_widget.run_selected.connect(self._on_tree_run_selected)
        self.tree_widget.runs_checked_changed.connect(self._on_tree_runs_checked_changed)
        self.tree_widget.open_in_results_requested.connect(self.open_in_results_requested)
        left_layout.addWidget(self.tree_widget)

        self.splitter.addWidget(self.left_card)

        # Right Panel: Plot and Analytics Card inside a vertical splitter
        self.right_container = QWidget(self.splitter)
        right_layout = QVBoxLayout(self.right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.right_splitter = QSplitter(Qt.Orientation.Vertical, self.right_container)
        self.right_splitter.setChildrenCollapsible(False)
        self.right_splitter.setHandleWidth(5)

        # Top Right: Interactive Plot
        self.plot_card = SimpleCardWidget(self.right_splitter)
        plot_layout = QVBoxLayout(self.plot_card)
        plot_layout.setContentsMargins(8, 8, 8, 8)
        self.plot_widget = MeasurementPlotWidget(self.plot_card)
        self.plot_widget.y_channel_changed.connect(self._on_plot_channel_changed)
        plot_layout.addWidget(self.plot_widget)
        self.right_splitter.addWidget(self.plot_card)

        # Bottom Right: Physical Figures of Merit & Actions Card
        self.analytics_card = MeasurementAnalyticsCard(self.right_splitter)
        self.analytics_card.open_in_results_requested.connect(self.open_in_results_requested)
        self.right_splitter.addWidget(self.analytics_card)

        # Set vertical proportions: ~65% plot, ~35% analytics
        self.right_splitter.setSizes([380, 200])

        right_layout.addWidget(self.right_splitter, 1)
        self.splitter.addWidget(self.right_container)

        # Set horizontal proportions: ~38% tree, ~62% plot+analytics
        self.splitter.setSizes([380, 620])

        layout.addWidget(self.splitter, 1)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def set_runs(self, runs: Sequence[SampleRunRecord], sample: Sample | None = None) -> None:
        """Update the browser with runs and sample context."""
        self._sample = sample
        self._runs = list(runs)
        self._series_cache.clear()
        self._current_channel = None
        self.tree_widget.set_runs(runs, sample=sample)

    def filter_by_device(self, row: str, col: str) -> None:
        """Focus on a specific device / pillar in the tree."""
        self.tree_widget.filter_by_device(row, col)

    def clear(self) -> None:
        """Clear all views."""
        self._sample = None
        self._runs.clear()
        self._series_cache.clear()
        self._current_channel = None
        self.tree_widget.clear()
        self.plot_widget.clear()
        self.analytics_card.clear()

    # -------------------------------------------------------------------------
    # Internal Handlers
    # -------------------------------------------------------------------------

    def _get_series(self, run: SampleRunRecord, preferred_channel: str | None) -> MeasurementSeries | None:
        cache_key = (run.run_path, preferred_channel)
        if cache_key in self._series_cache:
            return self._series_cache[cache_key]

        series = Hdf5SeriesReader.read_series(run.run_path, preferred_y_channel=preferred_channel)
        if series is not None:
            self._series_cache[cache_key] = series
        return series

    def _get_dimension_label(self, run: SampleRunRecord) -> str:
        if self._sample:
            label = self._sample.cell_label(run.row, run.col)
            if label:
                return label
        return run.device_label

    def _on_tree_run_selected(self, run: SampleRunRecord) -> None:
        # If user has multiple items checked, multi-series overlay takes precedence on the plot
        checked = self.tree_widget.get_checked_runs()
        if len(checked) > 1:
            # Still update analytics card for the clicked item
            series = self._get_series(run, self._current_channel)
            metrics = (
                calculate_mtj_metrics(
                    series.x_data,
                    series.y_data,
                    x_name=series.x_name,
                    y_name=series.y_name,
                    dimension_label=self._get_dimension_label(run),
                )
                if series and series.point_count > 0
                else None
            )
            self.analytics_card.set_run_data(run, series, metrics)
            return

        # Single run inspection
        series = self._get_series(run, self._current_channel)
        if series is None:
            self.plot_widget.show_error(f"Cannot read HDF5 series:\n{run.run_path}")
            self.analytics_card.set_run_data(run, None, None)
            return

        metrics = calculate_mtj_metrics(
            series.x_data,
            series.y_data,
            x_name=series.x_name,
            y_name=series.y_name,
            dimension_label=self._get_dimension_label(run),
        )
        self.plot_widget.set_series(series)
        self.analytics_card.set_run_data(run, series, metrics)

    def _on_tree_runs_checked_changed(self, checked_runs: list[SampleRunRecord]) -> None:
        if len(checked_runs) == 0:
            # Revert to currently highlighted run
            selected = self.tree_widget.get_selected_run()
            if selected is not None:
                self._on_tree_run_selected(selected)
            else:
                self.plot_widget.clear()
                self.analytics_card.clear()
        elif len(checked_runs) == 1:
            self._on_tree_run_selected(checked_runs[0])
        else:
            # Multi-curve comparison mode
            series_list: list[MeasurementSeries] = []
            for r in checked_runs:
                s = self._get_series(r, self._current_channel)
                if s is not None and s.point_count > 0:
                    series_list.append(s)

            self.plot_widget.set_multi_series(series_list)

            # In multi-curve mode, update analytics card to reflect the latest checked run
            latest = checked_runs[-1]
            latest_series = self._get_series(latest, self._current_channel)
            metrics = (
                calculate_mtj_metrics(
                    latest_series.x_data,
                    latest_series.y_data,
                    x_name=latest_series.x_name,
                    y_name=latest_series.y_name,
                    dimension_label=self._get_dimension_label(latest),
                )
                if latest_series and latest_series.point_count > 0
                else None
            )
            self.analytics_card.set_run_data(latest, latest_series, metrics)

    def _on_plot_channel_changed(self, channel: str) -> None:
        self._current_channel = channel
        checked = self.tree_widget.get_checked_runs()
        if len(checked) > 1:
            self._on_tree_runs_checked_changed(checked)
        else:
            selected = self.tree_widget.get_selected_run()
            if selected is not None:
                self._on_tree_run_selected(selected)
