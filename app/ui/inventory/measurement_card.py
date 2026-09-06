"""Card presenting physical figures of merit, MTJ parameters and run actions."""

from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    SubtitleLabel,
)

from app.inventory.analysis import MtjFiguresOfMerit
from app.inventory.models import SampleRunRecord
from app.storage.hdf5_series_reader import MeasurementSeries


class MeasurementAnalyticsCard(SimpleCardWidget):
    """Present extracted MTJ physical parameters, run metadata, and export/view actions."""

    open_in_results_requested = Signal(str)  # run_path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_run: SampleRunRecord | None = None
        self._current_series: MeasurementSeries | None = None
        self._current_metrics: MtjFiguresOfMerit | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # Header Info: Run title, timestamp, badges
        header_box = QHBoxLayout()
        header_box.setSpacing(10)

        title_vbox = QVBoxLayout()
        title_vbox.setSpacing(2)
        self.run_title = SubtitleLabel("No Run Selected", self)
        self.run_title.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.run_coord_label = CaptionLabel("Select a sweep from the tree", self)
        title_vbox.addWidget(self.run_title)
        title_vbox.addWidget(self.run_coord_label)
        header_box.addLayout(title_vbox, 1)

        self.status_badge = CaptionLabel("—", self)
        self.status_badge.setStyleSheet(
            "background: palette(midlight); padding: 4px 8px; border-radius: 4px; font-weight: 600;"
        )
        self.elab_badge = CaptionLabel("eLab: —", self)
        self.elab_badge.setStyleSheet(
            "background: palette(midlight); padding: 4px 8px; border-radius: 4px; font-weight: 500;"
        )
        header_box.addWidget(self.status_badge)
        header_box.addWidget(self.elab_badge)

        layout.addLayout(header_box)

        # Physical Figures of Merit Grid
        self.metrics_container = QWidget(self)
        self.metrics_grid = QGridLayout(self.metrics_container)
        self.metrics_grid.setContentsMargins(0, 4, 0, 4)
        self.metrics_grid.setSpacing(8)

        # Create tile widgets for MTJ metrics
        self.tile_rp = self._create_metric_tile("Rp (Parallel)", "—")
        self.tile_rap = self._create_metric_tile("Rap (Antiparallel)", "—")
        self.tile_tmr = self._create_metric_tile("TMR Ratio", "—", highlight=True)
        self.tile_ra = self._create_metric_tile("RA Product", "—")
        self.tile_hc = self._create_metric_tile("Coercivity (Hc)", "—")
        self.tile_hoff = self._create_metric_tile("Offset (H_dipolar)", "—")

        self.metrics_grid.addWidget(self.tile_rp, 0, 0)
        self.metrics_grid.addWidget(self.tile_rap, 0, 1)
        self.metrics_grid.addWidget(self.tile_tmr, 0, 2)
        self.metrics_grid.addWidget(self.tile_ra, 1, 0)
        self.metrics_grid.addWidget(self.tile_hc, 1, 1)
        self.metrics_grid.addWidget(self.tile_hoff, 1, 2)

        layout.addWidget(self.metrics_container)

        # Action Buttons bar
        actions_bar = QHBoxLayout()
        actions_bar.setSpacing(8)

        self.open_results_btn = PrimaryPushButton("Open Full in Results", self, FluentIcon.DOCUMENT)
        self.open_results_btn.setToolTip("Open this HDF5 file in the full Results workspace with 2D heatmaps and PyThat")
        self.open_results_btn.clicked.connect(self._on_open_results_clicked)
        self.open_results_btn.setEnabled(False)

        self.export_csv_btn = PushButton("Export CSV", self, FluentIcon.SAVE)
        self.export_csv_btn.setToolTip("Export the current curve points to a comma-separated text file")
        self.export_csv_btn.clicked.connect(self._on_export_csv_clicked)
        self.export_csv_btn.setEnabled(False)

        self.open_elab_btn = PushButton("View in eLabFTW", self, FluentIcon.LINK)
        self.open_elab_btn.setToolTip("Open linked experiment record in eLabFTW web interface")
        self.open_elab_btn.clicked.connect(self._on_open_elab_clicked)
        self.open_elab_btn.setEnabled(False)

        actions_bar.addWidget(self.open_results_btn)
        actions_bar.addWidget(self.export_csv_btn)
        actions_bar.addWidget(self.open_elab_btn)
        actions_bar.addStretch(1)

        layout.addLayout(actions_bar)

    def _create_metric_tile(self, label: str, value: str, highlight: bool = False) -> QWidget:
        tile = SimpleCardWidget(self)
        tile.setStyleSheet(
            f"SimpleCardWidget {{ background: {'rgba(30, 102, 245, 0.08)' if highlight else 'palette(midlight)'}; "
            f"border: 1px solid {'palette(highlight)' if highlight else 'palette(mid)'}; "
            f"border-radius: 6px; padding: 6px; }}"
        )
        t_layout = QVBoxLayout(tile)
        t_layout.setContentsMargins(6, 4, 6, 4)
        t_layout.setSpacing(2)

        l_widget = CaptionLabel(label, tile)
        l_widget.setStyleSheet("color: palette(placeholderText); font-size: 11px;")
        v_widget = BodyLabel(value, tile)
        v_widget.setStyleSheet(f"font-weight: 700; font-size: 13px; {'color: palette(highlight);' if highlight else ''}")

        t_layout.addWidget(l_widget)
        t_layout.addWidget(v_widget)
        tile.value_label = v_widget  # type: ignore[attr-defined]
        return tile

    def clear(self) -> None:
        self._current_run = None
        self._current_series = None
        self._current_metrics = None

        self.run_title.setText("No Run Selected")
        self.run_coord_label.setText("Select a sweep from the tree")
        self.status_badge.setText("—")
        self.status_badge.setStyleSheet("background: palette(midlight); padding: 4px 8px; border-radius: 4px;")
        self.elab_badge.setText("eLab: —")
        self.elab_badge.setStyleSheet("background: palette(midlight); padding: 4px 8px; border-radius: 4px;")

        for tile in (self.tile_rp, self.tile_rap, self.tile_tmr, self.tile_ra, self.tile_hc, self.tile_hoff):
            tile.value_label.setText("—")  # type: ignore[attr-defined]

        self.open_results_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.open_elab_btn.setEnabled(False)

    def set_run_data(
        self,
        run: SampleRunRecord | None,
        series: MeasurementSeries | None,
        metrics: MtjFiguresOfMerit | None,
    ) -> None:
        """Alias for set_data."""
        self.set_data(run, series, metrics)

    def set_data(
        self,
        run: SampleRunRecord | None,
        series: MeasurementSeries | None,
        metrics: MtjFiguresOfMerit | None,
    ) -> None:
        self._current_run = run
        self._current_series = series
        self._current_metrics = metrics

        if run is None:
            self.clear()
            return

        name = run.recipe_name or Path(run.run_path).stem
        self.run_title.setText(f"{name} [{run.point_count} pts]")
        coord_txt = f"Coord: R{run.row}:C{run.col}"
        if run.device_label:
            coord_txt += f" · {run.device_label}"
        if run.created_at_utc:
            coord_txt += f" · {run.created_at_utc[:19].replace('T', ' ')} UTC"
        self.run_coord_label.setText(coord_txt)

        # Status badge
        st = run.status.lower()
        if st in ("completed", "good", "pass"):
            self.status_badge.setText(f"✔ {run.status.upper()}")
            self.status_badge.setStyleSheet(
                "background: rgba(34, 197, 94, 0.18); color: #15803d; padding: 4px 8px; border-radius: 4px; font-weight: 600;"
            )
        elif st in ("fault", "failed", "aborted", "burned", "shorted"):
            self.status_badge.setText(f"⚠ {run.status.upper()}")
            self.status_badge.setStyleSheet(
                "background: rgba(220, 38, 38, 0.18); color: #b91c1c; padding: 4px 8px; border-radius: 4px; font-weight: 600;"
            )
        else:
            self.status_badge.setText(run.status.upper())
            self.status_badge.setStyleSheet(
                "background: palette(midlight); padding: 4px 8px; border-radius: 4px; font-weight: 600;"
            )

        # eLab status
        if run.elab_status == "uploaded":
            exp_id_txt = f" #{run.elab_experiment_id}" if run.elab_experiment_id else ""
            self.elab_badge.setText(f"eLab: Uploaded{exp_id_txt}")
            self.elab_badge.setStyleSheet(
                "background: rgba(30, 102, 245, 0.15); color: #1e66f5; padding: 4px 8px; border-radius: 4px; font-weight: 600;"
            )
            self.open_elab_btn.setEnabled(bool(run.elab_url))
        else:
            self.elab_badge.setText("eLab: Not Uploaded")
            self.elab_badge.setStyleSheet(
                "background: palette(midlight); padding: 4px 8px; border-radius: 4px; color: palette(placeholderText);"
            )
            self.open_elab_btn.setEnabled(False)

        # Update metrics tiles
        if metrics is not None:
            self.tile_rp.value_label.setText(_fmt_val(metrics.r_p, "Ω"))  # type: ignore[attr-defined]
            self.tile_rap.value_label.setText(_fmt_val(metrics.r_ap, "Ω"))  # type: ignore[attr-defined]
            self.tile_tmr.value_label.setText(f"{metrics.tmr_percent:.1f} %" if metrics.tmr_percent is not None else "—")  # type: ignore[attr-defined]
            self.tile_ra.value_label.setText(f"{metrics.ra_product:.2f} Ω·µm²" if metrics.ra_product is not None else "—")  # type: ignore[attr-defined]
            self.tile_hc.value_label.setText(f"{metrics.h_coercive:.2f} Oe" if metrics.h_coercive is not None else "—")  # type: ignore[attr-defined]
            self.tile_hoff.value_label.setText(f"{metrics.h_offset:.2f} Oe" if metrics.h_offset is not None else "—")  # type: ignore[attr-defined]
        else:
            for tile in (self.tile_rp, self.tile_rap, self.tile_tmr, self.tile_ra, self.tile_hc, self.tile_hoff):
                tile.value_label.setText("—")  # type: ignore[attr-defined]

        p = Path(run.run_path)
        self.open_results_btn.setEnabled(p.is_file())
        self.export_csv_btn.setEnabled(series is not None and not series.is_empty)

    def _on_open_results_clicked(self) -> None:
        if self._current_run:
            self.open_in_results_requested.emit(self._current_run.run_path)

    def _on_open_elab_clicked(self) -> None:
        if self._current_run and self._current_run.elab_url:
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(self._current_run.elab_url))

    def _on_export_csv_clicked(self) -> None:
        if not self._current_series or self._current_series.is_empty or not self._current_run:
            return
        default_name = f"{Path(self._current_run.run_path).stem}_curve.csv"
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Export Curve CSV", default_name, "CSV Files (*.csv);;All Files (*)"
        )
        if not save_path:
            return

        try:
            with open(save_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                x_head = f"{self._current_series.x_label} ({self._current_series.x_unit})".strip()
                y_head = f"{self._current_series.y_label} ({self._current_series.y_unit})".strip()
                writer.writerow([x_head, y_head])
                for x, y in zip(self._current_series.x_values, self._current_series.y_values, strict=False):
                    writer.writerow([x, y])

            InfoBar.success(
                title="CSV Exported",
                content=f"Saved {len(self._current_series.x_values)} points to {Path(save_path).name}",
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
            )
        except Exception as exc:
            InfoBar.error(
                title="Export Failed",
                content=str(exc),
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=4000,
            )


def _fmt_val(val: float | None, unit: str) -> str:
    if val is None:
        return "—"
    if abs(val) >= 1e6:
        return f"{val / 1e6:.3f} M{unit}"
    if abs(val) >= 1e3:
        return f"{val / 1e3:.3f} k{unit}"
    if abs(val) < 1:
        return f"{val * 1e3:.2f} m{unit}"
    return f"{val:.2f} {unit}"
