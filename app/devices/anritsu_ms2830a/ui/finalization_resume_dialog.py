"""Fluent review of a stopped batch and explicit replacement output paths."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QFormLayout, QHBoxLayout, QHeaderView, QTableWidgetItem, QWidget
from qfluentwidgets import BodyLabel, CheckBox, LineEdit, PrimaryPushButton, ProgressBar, PushButton, TableWidget, TitleLabel

from app.domain.spectrum_finalization import SpectrumFinalizationResumeRequest, SpectrumResumeInspectionRequest
from app.ui.dialogs import StationDialog, StationFileDialog
from app.ui.widgets.fluent_ownership import own_fluent_helpers
from .correction_controller import SpectrumCorrectionController


class SpectrumFinalizationResumeDialog(StationDialog):
    selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent, resizable=True)
        self.setWindowTitle("Resume a stopped spectrum batch")
        self.resize(980, 750)
        self._busy = self._closing = False
        self._inspection = self._inspection_input = self._pending_request = None
        self._replacements = {}
        self._controller = SpectrumCorrectionController(self)
        self._controller.completed.connect(self._completed)
        self._controller.failed.connect(self._failed)
        self._controller.cancelled.connect(lambda operation: self._failed(operation, "Inspection canceled."))
        layout = self.modal_content_layout(spacing=10)
        layout.addWidget(TitleLabel("Resume a stopped spectrum batch", self))
        hint = BodyLabel("Completed artifacts are verified and retained. Each existing unfinished output needs "
                         "a new path. The previous journal and source archives remain unchanged.", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._editors = []
        self.previous = self._path_row(form, "Previous batch journal", False)
        self.previous.textChanged.connect(self._invalidate)
        layout.addLayout(form)
        self.recover_tail = CheckBox("Recover a torn last journal record (preserve the original bytes)", self)
        self.recover_tail.toggled.connect(self._invalidate)
        layout.addWidget(self.recover_tail)
        self.inspect = PushButton("Verify completed blocks and show remaining outputs", self)
        self.inspect.clicked.connect(self._inspect)
        layout.addWidget(self.inspect)
        self.table = TableWidget(self)
        self.table.setAccessibleName("Batch resume block review")
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Block", "State", "Original output", "Replacement"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(130)
        self.table.itemSelectionChanged.connect(self._update_ready)
        layout.addWidget(self.table, 1)
        self.adopt = CheckBox("Retain the verified completed file missing from the journal", self)
        self.adopt.setEnabled(False)
        self.adopt.toggled.connect(self._update_ready)
        layout.addWidget(self.adopt)
        replacements = QHBoxLayout()
        self.replace_output = PushButton("Choose new output for selected block…", self)
        self.clear_output = PushButton("Clear replacement", self)
        self.replace_output.clicked.connect(self._replace_output)
        self.clear_output.clicked.connect(self._clear_output)
        replacements.addWidget(self.replace_output)
        replacements.addWidget(self.clear_output)
        layout.addLayout(replacements)
        footer = QFormLayout()
        footer.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.journal = self._path_row(footer, "New resume journal", True)
        self.journal.textChanged.connect(self._journal_changed)
        layout.addLayout(footer)
        self.stopped = CheckBox("I confirm the previous offline processing job has ended", self)
        self.stopped.toggled.connect(self._update_ready)
        layout.addWidget(self.stopped)
        self.status = BodyLabel("Choose a previous journal and verify it before resuming.", self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.hide()
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.start = PrimaryPushButton("Resume remaining blocks", self)
        self.close_button = PushButton("Cancel", self)
        self.start.clicked.connect(self._submit)
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.start)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._update_ready()
        own_fluent_helpers(self)

    def _path_row(self, form, label, output):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        edit, browse = LineEdit(row), PushButton("Browse…", row)
        edit.setAccessibleName(label)
        browse.setAccessibleName("Browse " + label)
        layout.addWidget(edit, 1)
        layout.addWidget(browse)
        form.addRow(label, row)
        self._editors.extend((edit, browse))

        def choose():
            method = StationFileDialog.getSaveFileName if output else StationFileDialog.getOpenFileName
            path, _filter = method(self, label, edit.text(), "JSONL (*.jsonl)")
            if path:
                edit.setText(path)

        browse.clicked.connect(choose)
        return edit

    def _invalidate(self, *_args):
        self._inspection = None
        self._replacements.clear()
        self.table.setRowCount(0)
        self.adopt.setChecked(False)
        self.stopped.setChecked(False)
        self.status.setText("Journal selection changed. Verify it again before resume.")
        self._update_ready()

    def _update_ready(self, *_args):
        if not hasattr(self, "start"):
            return
        ready = not self._busy and not self._closing
        row = self.table.currentRow()
        selected = self._inspection is not None and 0 <= row < len(self._inspection.blocks)
        unfinished = selected and not self._inspection.blocks[row].completed
        candidate = (self._inspection.blocks[self._inspection.completed_blocks] if self._inspection is not None
                     and self._inspection.completed_blocks < len(self._inspection.blocks) else None)
        self.adopt.setText(f"Retain verified block {candidate.index} output missing from the journal" if candidate is not None
                           and candidate.adoptable else "Retain the verified completed file missing from the journal")
        self.adopt.setToolTip(str(candidate.destination) if candidate is not None and candidate.adoptable
                             else "Only the first unfinished file can be retained after complete verification.")
        self.adopt.setEnabled(ready and candidate is not None and candidate.adoptable
                              and candidate.index not in self._replacements)
        self.replace_output.setEnabled(ready and unfinished)
        self.clear_output.setEnabled(ready and unfinished and row in self._replacements)
        missing = self._inspection is not None and any(block.output_exists and not block.completed
            and block.index not in self._replacements and not (block.adoptable and self.adopt.isChecked())
            for block in self._inspection.blocks)
        self.start.setEnabled(ready and self._inspection is not None and self._inspection.status != "completed"
                              and not missing and self.stopped.isChecked() and bool(self.journal.text().strip()))

    def _journal_changed(self, *_args):
        self._update_ready()
        if self._inspection is not None and hasattr(self, "status"):
            self.status.setText(f"{self._inspection.completed_blocks}/{len(self._inspection.blocks)} journaled blocks verified. "
                + ("This batch is already completed." if self._inspection.status == "completed"
                   else "Resume journal changed. Review replacements and confirm the previous processing job has ended."))

    def _set_busy(self, busy):
        self._busy = busy
        for control in (*self._editors, self.recover_tail, self.inspect, self.stopped):
            control.setEnabled(not busy and not self._closing)
        self._update_ready()

    def _inspect(self):
        try:
            if not self.previous.text().strip():
                raise ValueError("Choose a previous batch journal.")
            request = SpectrumResumeInspectionRequest(Path(self.previous.text().strip()), self.recover_tail.isChecked())
            self._inspection_input = (request.previous_journal.resolve(), request.recover_torn_tail)
            self._inspection = None
            self._replacements.clear()
            self.adopt.setChecked(False)
            self.table.setRowCount(0)
            self._set_busy(True)
            self.progress.setRange(0, 0)
            self.progress.show()
            self.status.setText("Verifying completed artifacts and their source identities…")
            self._controller.inspect_batch_resume(request)
        except (ValueError, BufferError) as exc:
            self._failed("inspect_batch_resume", str(exc))

    def _completed(self, operation, result):
        if operation != "inspect_batch_resume" or self._closing:
            return
        current = (Path(self.previous.text().strip()).resolve(), self.recover_tail.isChecked())
        if current != self._inspection_input or current[0] != result.previous_journal:
            self._failed(operation, "Selection changed during inspection. Verify it again.")
            return
        self._inspection = result
        self.table.setRowCount(len(result.blocks))
        for block in result.blocks:
            state = "Verified complete" if block.completed else "Verified closed, not journaled" if block.adoptable else (
                "Existing output: choose new path" if block.output_exists else "Pending")
            for column, text in enumerate((str(block.index), state, block.destination.name, "")):
                item = QTableWidgetItem(text)
                item.setToolTip(f"SIGNAL: {block.signal_path}\nOriginal output: {block.destination}")
                if block.adoption_error:
                    item.setToolTip(item.toolTip() + "\nCannot retain this file: " + block.adoption_error)
                self.table.setItem(block.index, column, item)
        self.progress.hide()
        self._set_busy(False)
        self.status.setText(f"{result.completed_blocks}/{len(result.blocks)} journaled blocks verified. "
            + ("This batch is already completed." if result.status == "completed" else
               "Choose replacements for existing unfinished outputs, a new journal, and confirm processing has ended.")
            + (" A torn final record will be omitted from the new journal; the original is retained."
               if result.torn_tail_recovered else ""))

    def _failed(self, operation, error):
        if operation == "inspect_batch_resume" and not self._closing:
            self._inspection = None
            self.progress.hide()
            self._set_busy(False)
            self.status.setText("Resume inspection failed: " + error)

    def _replace_output(self):
        row = self.table.currentRow()
        if self._inspection is None or not 0 <= row < len(self._inspection.blocks) or self._inspection.blocks[row].completed:
            return
        path, _filter = StationFileDialog.getSaveFileName(self, "New unfinished block output", "", "HDF5 (*.h5)")
        if path:
            if row == self._inspection.completed_blocks:
                self.adopt.setChecked(False)
            self._replacements[row] = Path(path).resolve()
            self.table.item(row, 3).setText(Path(path).name)
            self.table.item(row, 3).setToolTip(str(self._replacements[row]))
            self.status.setText(f"Replacement chosen for block {row}. Paths are checked again before processing.")
            self._update_ready()

    def _clear_output(self):
        row = self.table.currentRow()
        self._replacements.pop(row, None)
        if row >= 0:
            self.table.item(row, 3).setText("")
            self.table.item(row, 3).setToolTip("")
            self.status.setText(f"Replacement cleared for block {row}. Existing unfinished files require a new path.")
        self._update_ready()

    def _submit(self):
        try:
            if self._busy or self._closing:
                return
            if self._inspection is None or self._inspection.status == "completed":
                raise ValueError("Verify a stopped incomplete batch first.")
            if not self.journal.text().strip():
                raise ValueError("Choose a new resume journal.")
            if self.adopt.isChecked() and (self._inspection.completed_blocks >= len(self._inspection.blocks)
                    or not self._inspection.blocks[self._inspection.completed_blocks].adoptable):
                raise ValueError("Only a verified closed unfinished output can be retained.")
            targets = [self._replacements.get(block.index, block.destination) for block in self._inspection.blocks]
            journal = Path(self.journal.text().strip()).resolve()
            forbidden = set(self._inspection.source_paths) | {self._inspection.previous_journal}
            adopt_index = self._inspection.completed_blocks if self.adopt.isChecked() else None
            if (journal.exists() or len(set(targets + [journal])) != len(targets) + 1
                    or any(path in forbidden for path in targets + [journal])
                    or any(path.exists() for block, path in zip(self._inspection.blocks, targets)
                           if not block.completed and block.index != adopt_index)):
                raise ValueError("Choose distinct new unfinished outputs and a new journal separate from every source.")
            self._pending_request = SpectrumFinalizationResumeRequest(self._inspection.previous_journal, journal,
                self.stopped.isChecked(), tuple(sorted(self._replacements.items())), self.recover_tail.isChecked(),
                expected_parent_hash=self._inspection.parent_hash, adopt_closed_output=self.adopt.isChecked(),
                expected_closed_output_hash=None if adopt_index is None
                    else self._inspection.blocks[adopt_index].closed_output_hash)
            self._closing = True
            self._set_busy(False)
            self.status.setText("Preparing verified selections for resume…")
            self._finish_selection()
        except (ValueError, TypeError) as exc:
            self.status.setText("Resume selection requires correction: " + str(exc))

    def _finish_selection(self):
        if not self._controller.close(wait_ms=0):
            QTimer.singleShot(50, self, self._finish_selection)
            return
        if self._pending_request is not None:
            self.selected.emit(self._pending_request)
            super().accept()
        else:
            super().reject()

    def reject(self):
        self._pending_request = None
        self._closing = True
        self._set_busy(self._busy)
        self._finish_selection()

    def closeEvent(self, event):
        event.ignore()
        self.reject()

    def shutdown(self):
        self._closing = True
        self._pending_request = None
        return self._controller.close(wait_ms=0)
