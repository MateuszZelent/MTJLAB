"""Shared series table for sweep editing and execution review."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QTreeWidgetItem
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    TreeWidget,
)

from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_VOLTAGE, format_quantity_auto
from app.recipes.measurement_series import MeasurementSeries
from app.ui.dialogs import StationDialog


def series_setpoint_text(series: MeasurementSeries) -> tuple[str, str, str]:
    voltage = format_quantity_auto(series.moke_voltage_v, DIMENSION_VOLTAGE)
    bias = format_quantity_auto(series.keithley_b_a, DIMENSION_CURRENT)
    start = format_quantity_auto(series.keithley_a_start_a, DIMENSION_CURRENT)
    stop = format_quantity_auto(series.keithley_a_stop_a, DIMENSION_CURRENT)
    return f"VOUT {series.moke_channel} · {voltage}", bias, f"{start} → {stop}"


class MeasurementSeriesDialog(StationDialog):
    def __init__(
        self, series: tuple[MeasurementSeries, ...], parent=None, *, read_only: bool = False,
    ) -> None:
        super().__init__(parent, resizable=True)
        self.setObjectName("measurementSeriesDialog")
        self.setWindowTitle("Measurement series")
        self.setMinimumSize(600, 360)
        self.resize(730, 500)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout()
        layout.addWidget(SubtitleLabel("Measurement series", surface))
        explanation = CaptionLabel(
            "The validated run uses the planned series shown below."
            if read_only else
            "Choose a starting series or mark individual series as already measured.", surface,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.start_combo = ComboBox(surface)
        self.start_combo.setAccessibleName("Start at series")
        self.start_combo.setPlaceholderText("Custom selection")
        if not read_only:
            start_row = QHBoxLayout()
            start_row.addWidget(CaptionLabel("Start at", surface))
            start_row.addWidget(self.start_combo, 1)
            layout.addLayout(start_row)
        else:
            self.start_combo.hide()
        self.table = TreeWidget(surface)
        self.table.setAccessibleName("Measurement series")
        self.table.setRootIsDecorated(False)
        self.table.setHeaderLabels([
            "Series", "MOKE Box", "Keithley B", "Keithley A", "Points",
            "Status" if read_only else "Already measured",
        ])
        header = self.table.header()
        for column in (0, 2, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self._rows: list[tuple[MeasurementSeries, QTreeWidgetItem]] = []
        for item in series:
            moke, bias, current_range = series_setpoint_text(item)
            row = QTreeWidgetItem([str(item.number), moke, bias, current_range, str(item.points), ""])
            if read_only:
                row.setText(5, "Already measured" if item.already_measured else "Planned")
            else:
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(5, Qt.CheckState.Checked if item.already_measured else Qt.CheckState.Unchecked)
                self.start_combo.addItem(f"Series {item.number} · {moke} · B {bias}", userData=item.number)
            row.setToolTip(0, item.id)
            self.table.addTopLevelItem(row)
            self._rows.append((item, row))
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.summary = CaptionLabel(surface)
        footer.addWidget(self.summary, 1)
        close = PushButton("Close" if read_only else "Cancel", surface)
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        if not read_only:
            apply = PrimaryPushButton("Apply selection", surface)
            apply.clicked.connect(self.accept)
            footer.addWidget(apply)
            self.table.itemChanged.connect(self._refresh_summary)
            self.start_combo.currentIndexChanged.connect(self._select_start)
        layout.addLayout(footer)
        self._read_only = read_only
        self._refresh_summary()

    @property
    def completed_series_ids(self) -> tuple[str, ...]:
        if self._read_only:
            return tuple(item.id for item, _ in self._rows if item.already_measured)
        return tuple(
            item.id for item, row in self._rows if row.checkState(5) == Qt.CheckState.Checked
        )

    def _select_start(self, index: int) -> None:
        if index < 0:
            return
        was_blocked = self.table.blockSignals(True)
        try:
            for position, (_, row) in enumerate(self._rows):
                row.setCheckState(5, Qt.CheckState.Checked if position < index else Qt.CheckState.Unchecked)
        finally:
            self.table.blockSignals(was_blocked)
        self._refresh_summary()

    def _refresh_summary(self, *_args) -> None:
        completed = self.completed_series_ids
        count = len(completed)
        self.summary.setText(f"{len(self._rows) - count} planned · {count} already measured")
        if not self._read_only:
            prefix = tuple(item.id for item, _ in self._rows[:count])
            was_blocked = self.start_combo.blockSignals(True)
            try:
                self.start_combo.setCurrentIndex(count if completed == prefix and count < len(self._rows) else -1)
            finally:
                self.start_combo.blockSignals(was_blocked)
