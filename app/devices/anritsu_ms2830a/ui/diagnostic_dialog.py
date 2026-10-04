"""Shown REF stability diagnostics without inferred independence, TTL or CI."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QAbstractItemView, QFormLayout, QHBoxLayout, QTableWidgetItem, QWidget
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, TableWidget, TitleLabel

from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.spectrum.reference_diagnostics import ReferenceDiagnosticConfig
from app.ui.dialogs import StationDialog, StationFileDialog
from .correction_controller import SpectrumCorrectionController, SpectrumReferenceDiagnosticRequest


class SpectrumDiagnosticDialog(StationDialog):
    def __init__(self, parent=None):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Reference stability diagnostics")
        self.resize(850, 850)
        self._busy = self._closing = False
        self._report = None
        self._controller = SpectrumCorrectionController(self)
        self._controller.completed.connect(self._completed)
        self._controller.failed.connect(self._failed)
        self._controller.cancelled.connect(self._cancelled)
        layout = self.modal_content_layout(spacing=10)
        layout.addWidget(TitleLabel("Inspect recorded background stability", self))
        hint = BodyLabel("Analyze completed raw REF time-block mean powers. Source sweeps must reproduce "
                         "the recorded profile. Missing blocks are not interpolated.", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._editors = []
        self.reference = self._path_row(form, "Completed raw REF", False)
        self.output = self._path_row(form, "New diagnostic report", True)
        self.duration = LineEdit(content)
        self.duration.setText("1 s")
        form.addRow("Time-block duration", self.duration)
        self.bins = LineEdit(content)
        self.bins.setPlaceholderText("Optional zero-based indices, separated by commas or spaces")
        self.bins.setToolTip("At most 16 unique acquired bin indices. Empty selects uniform bins plus maximum REF power.")
        form.addRow("Diagnostic bin indices", self.bins)
        self._editors.extend((self.duration, self.bins))
        note = BodyLabel("At least 16 complete time blocks and regular effective cadence are required for Allan/ACF. "
                         "The final partial block is excluded. Pair counts are overlapping, not independent samples. "
                         "Diagnostics do not set a qualified TTL, sweep independence or confidence interval.", content)
        note.setWordWrap(True)
        form.addRow(note)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.status = BodyLabel("Ready. Choose a completed raw REF and a new JSON report.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.result_label = BodyLabel("No diagnostics yet.", self)
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        selectors = QHBoxLayout()
        self.bin_selector = ComboBox(self)
        self.bin_selector.setAccessibleName("Diagnostic frequency bin")
        self.metric_selector = ComboBox(self)
        self.metric_selector.setAccessibleName("Diagnostic metric")
        self.metric_selector.addItem("Allan variance", userData="allan")
        self.metric_selector.addItem("Autocorrelation", userData="acf")
        selectors.addWidget(self.bin_selector, 1)
        selectors.addWidget(self.metric_selector)
        layout.addLayout(selectors)
        self.table = TableWidget(self)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setMinimumHeight(130)
        layout.addWidget(self.table, 1)
        self.table_status = BodyLabel("Values are unavailable until analysis finishes.", self)
        self.table_status.setWordWrap(True)
        layout.addWidget(self.table_status)
        actions = QHBoxLayout()
        self.start = PrimaryPushButton("Analyze and save report", self)
        self.cancel = PushButton("Cancel analysis", self)
        self.close_button = PushButton("Close", self)
        self.cancel.setEnabled(False)
        self.bin_selector.setEnabled(False)
        self.metric_selector.setEnabled(False)
        for button in (self.start, self.cancel, self.close_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.start.clicked.connect(self._start)
        self.cancel.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.reject)
        self.bin_selector.currentIndexChanged.connect(self._render_table)
        self.metric_selector.currentIndexChanged.connect(self._render_table)

    def _path_row(self, form, label, output):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        editor = LineEdit(row)
        button = PushButton("Choose…", row)
        editor.setAccessibleName(label)

        def choose():
            if output:
                path, _ = StationFileDialog.getSaveFileName(self, label, "stability.json", "JSON (*.json)")
            else:
                path, _ = StationFileDialog.getOpenFileName(self, label, "", "HDF5 (*.h5)")
            if path:
                editor.setText(path)

        button.clicked.connect(choose)
        layout.addWidget(editor, 1)
        layout.addWidget(button)
        form.addRow(label, row)
        self._editors.extend((editor, button))
        return editor

    def _request(self):
        if not self.reference.text().strip() or not self.output.text().strip():
            raise ValueError("Choose completed REF and a new report.")
        config = ReferenceDiagnosticConfig(block_duration_s=parse_quantity(self.duration.text(), DIMENSION_TIME).si_value)
        bins = tuple(int(value) for value in self.bins.text().replace(",", " ").split()) if self.bins.text().strip() else None
        return SpectrumReferenceDiagnosticRequest(Path(self.reference.text().strip()), Path(self.output.text().strip()),
                                                  bin_indices=bins, config=config)

    def _set_busy(self, busy):
        self._busy = busy
        self.start.setEnabled(not busy and not self._closing)
        self.cancel.setEnabled(busy and not self._closing)
        for control in self._editors:
            control.setEnabled(not busy and not self._closing)
        self.bin_selector.setEnabled(not busy and not self._closing and self._report is not None)
        self.metric_selector.setEnabled(self.bin_selector.isEnabled())

    def _start(self):
        try:
            self._controller.diagnose_reference_archive(self._request())
        except (ValueError, BufferError) as exc:
            self.status.setText(f"Cannot start: {exc}")
            return
        self._report = None
        self._set_busy(True)
        self.status.setText("Reading REF, verifying its profile and analyzing time-block powers…")
        self.result_label.setText("Waiting for this diagnostic result.")
        self.table.setRowCount(0)
        self.table_status.setText("Analysis pending; no current Allan/ACF result.")
        self.progress.setRange(0, 0)
        self.progress.show()

    def _completed(self, operation, payload):
        if operation != "diagnose_reference" or self._closing:
            return
        path, report = payload
        self._report = report
        self._set_busy(False)
        self.progress.hide()
        self.status.setText(f"Report saved: {path}")
        issues = ", ".join(report["issues"]) or "none"
        self.result_label.setText(f"{report['total_sweeps']} raw sweeps; {len(report['block_counts'])} complete time blocks; "
            f"{report['discarded_tail_sweeps']} tail sweeps excluded.\n"
            f"Cadence {'regular' if report['cadence_valid'] else 'irregular'}; issues: {issues}. "
            "Raw/profile agreement verified. No independence, TTL or CI qualification inferred.")
        self.bin_selector.blockSignals(True)
        self.bin_selector.clear()
        for position, (index, frequency) in enumerate(zip(report["bin_indices"], report["frequencies_hz"], strict=True)):
            self.bin_selector.addItem(f"Bin {index} · {frequency:.6g} Hz", userData=position)
        self.bin_selector.blockSignals(False)
        self._render_table()

    def _render_table(self, *_args):
        self.table.setRowCount(0)
        report = self._report
        if report is None or self.bin_selector.currentData() is None:
            return
        position = self.bin_selector.currentData()
        allan = self.metric_selector.currentData() == "allan"
        headers = ["Tau (s)", "Allan variance (W²)", "Overlapping pairs"] if allan else ["Lag (s)", "Autocorrelation (1)"]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.resizeColumnsToContents()
        values = report["allan_variance_w2"] if allan else report["autocorrelation"]
        if values is None:
            self.table_status.setText("Unavailable: " + ", ".join(report["issues"]) + ". No gap interpolation or fabricated values.")
            return
        if not allan and not report["correlation_valid_bins"][position]:
            self.table_status.setText("Autocorrelation is undefined for a constant-power bin.")
            return
        times = report["allan_tau_s"] if allan else report["correlation_lag_s"]
        self.table.setRowCount(len(times))
        for row, value in enumerate(times):
            cells = [f"{value:.6g}", f"{values[row][position]:.6g}"]
            if allan:
                cells.append(str(report["allan_pair_counts"][row]))
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.table_status.setText("Statistics of time-block means; overlapping pairs do not establish independent sweeps or CI.")

    def _failed(self, operation, error):
        if operation == "diagnose_reference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText(f"Diagnostics failed: {error}")
            self.result_label.setText("No report published by this analysis. Source unchanged.")

    def _cancel(self):
        self._controller.cancel_processing()
        self.cancel.setEnabled(False)
        self.status.setText("Canceling diagnostics; waiting for the current checkpoint…")

    def _cancelled(self, operation):
        if operation == "diagnose_reference" and not self._closing:
            self._set_busy(False)
            self.progress.hide()
            self.status.setText("Diagnostics canceled. No report published; source unchanged.")
            self.result_label.setText("No diagnostics yet.")

    def reject(self):
        self._closing = True
        self._set_busy(self._busy)
        self.status.setText("Closing diagnostic worker…")
        if self._controller.close(wait_ms=0):
            super().reject()
        else:
            QTimer.singleShot(50, self, self.reject)

    def closeEvent(self, event):
        event.ignore()
        self.reject()

    def shutdown(self):
        self._closing = True
        # The owning window must stay alive while this worker drains. Retain
        # ownership so a subsequent close can safely inspect the controller.
        return self._controller.close(wait_ms=0)
