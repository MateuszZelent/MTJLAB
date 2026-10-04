"""Fluent selection of explicit profiles and one continuous archived SIGNAL block."""

from pathlib import Path
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QFormLayout, QHBoxLayout, QHeaderView, QTableWidgetItem, QWidget
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PrimaryPushButton, ProgressBar, PushButton, ScrollArea, TableWidget, TitleLabel

from app.domain.spectrum_finalization import SpectrumFinalizationBatchRequest
from app.ui.dialogs import StationDialog, StationFileDialog
from app.ui.widgets.fluent_ownership import own_fluent_helpers
from .correction_controller import (
    SpectrumCorrectionController, SpectrumFinalizationRequest, SpectrumFinalizationSourcesRequest,
)


class SpectrumFinalizationSelectionDialog(StationDialog):
    selected = Signal(object)

    def __init__(self, parent=None, *, batch_mode=False, initial_source_path=None):
        super().__init__(parent, resizable=True)
        self._batch_mode = batch_mode
        self._initial_source_path = initial_source_path
        self._queued_blocks = []
        self.setWindowTitle("Choose reference history and SIGNAL block")
        self.resize(880, 740)
        self._busy = self._closing = False
        self._loaded_sources = None
        self._pending_request = None
        self._controller = SpectrumCorrectionController(self)
        self._controller.completed.connect(self._completed)
        self._controller.failed.connect(self._failed)
        self._controller.cancelled.connect(self._cancelled)
        layout = self.modal_content_layout(spacing=10)
        layout.addWidget(TitleLabel("Finalize selected SIGNAL blocks" if batch_mode
                                   else "Finalize a selected SIGNAL block", self))
        hint = BodyLabel(("Choose completed REF profiles before and after each continuous SIGNAL block. " if batch_mode
                          else "Choose completed REF profiles before and after one continuous SIGNAL block. ") +
            "Both REF profiles may be in the same history file. Original archives remain unchanged.", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = ScrollArea(self)
        self.form_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        form = QFormLayout(content)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._editors = []
        self.paths, self.profiles = [], []
        for label in ("SIGNAL archive", "REF before archive", "REF after archive"):
            edit = self._path_row(form, label, False)
            edit.textChanged.connect(self._invalidate)
            self.paths.append(edit)
            selector = ComboBox(content)
            selector.setAccessibleName(label + " profile")
            selector.currentIndexChanged.connect(self._selection_changed)
            selector.setEnabled(False)
            form.addRow(label + " profile", selector)
            self.profiles.append(selector)
        self.inspect = PushButton("Load available profiles", content)
        form.addRow(self.inspect)
        self.indices = LineEdit(content)
        self.indices.setMaxLength(32768)
        self.indices.setPlaceholderText("Empty: entire archive. Points: 10 11 12, or range 10:13 (end excluded)")
        self.indices.setAccessibleName("SIGNAL checkpoint indices")
        form.addRow("SIGNAL points", self.indices)
        self._editors.append(self.indices)
        self.load_interleaved = PushButton("Load alternating SIGNAL blocks", content)
        self.load_interleaved.setEnabled(False)
        self.load_interleaved.setToolTip("Use one closed alternating archive for all three sources.")
        self.load_interleaved.clicked.connect(self._inspect_interleaved)
        form.addRow(self.load_interleaved)
        self.interleaved_blocks = ComboBox(content)
        self.interleaved_blocks.setAccessibleName("Recorded alternating SIGNAL block")
        self.interleaved_blocks.setEnabled(False)
        self.interleaved_blocks.currentIndexChanged.connect(self._interleaved_selection_changed)
        form.addRow("Alternating SIGNAL block", self.interleaved_blocks)
        self.output = self._path_row(form, "New final artifact", True)
        note = BodyLabel(("Each queued block must contain one continuous segment. " if batch_mode
                          else "Only one continuous segment is finalized. ") + "Profile dates, settings and selected raw "
            "sweeps are checked during finalization. No qualified confidence interval is inferred.", content)
        note.setWordWrap(True)
        form.addRow(note)
        if batch_mode:
            self.setWindowTitle("Choose SIGNAL blocks for batch finalization")
            self.add_block = PushButton("Add selected block to batch", content)
            self.add_block.setEnabled(False)
            self.add_block.clicked.connect(self._add_block)
            form.addRow(self.add_block)
            self.block_table = TableWidget(content)
            self.block_table.setAccessibleName("Queued SIGNAL blocks")
            self.block_table.setColumnCount(3)
            self.block_table.setHorizontalHeaderLabels(["SIGNAL points", "REF profiles", "Output"])
            self.block_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.block_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.block_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.block_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.block_table.setMinimumHeight(150)
            form.addRow(self.block_table)
            self.remove_block = PushButton("Remove selected block", content)
            self.remove_block.setEnabled(False)
            self.block_table.itemSelectionChanged.connect(self._queue_selection_changed)
            self.remove_block.clicked.connect(self._remove_block)
            form.addRow(self.remove_block)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        if batch_mode:
            journal_form = QFormLayout()
            self.journal = self._path_row(journal_form, "New batch journal", True, "JSONL (*.jsonl)")
            self.journal.textChanged.connect(self._selection_changed)
            layout.addLayout(journal_form)
        self.status = BodyLabel("Choose three sources, then load their profiles.", self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.hide()
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.start = PrimaryPushButton("Finalize selected block", self)
        if batch_mode:
            self.start.setText("Finalize queued blocks")
        self.close_button = PushButton("Cancel", self)
        self.start.setEnabled(False)
        actions.addWidget(self.start)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.inspect.clicked.connect(self._inspect)
        self.start.clicked.connect(self._submit)
        self.close_button.clicked.connect(self.reject)
        own_fluent_helpers(self)
        if initial_source_path is not None:
            for editor in self.paths:
                editor.setText(str(initial_source_path))
            QTimer.singleShot(0, self, self._inspect)

    def _path_row(self, form, label, output, file_filter="HDF5 (*.h5)"):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        edit = LineEdit(row)
        edit.setAccessibleName(label)
        button = PushButton("Browse…", row)
        button.setAccessibleName("Browse " + label)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        form.addRow(label, row)
        self._editors.extend((edit, button))

        def browse():
            method = StationFileDialog.getSaveFileName if output else StationFileDialog.getOpenFileName
            path, _filter = method(self, label, edit.text(), file_filter)
            if path:
                edit.setText(path)

        button.clicked.connect(browse)
        return edit

    def _invalidate(self, *_args):
        self._loaded_sources = None
        for selector in self.profiles:
            selector.clear()
            selector.setEnabled(False)
        self.interleaved_blocks.clear()
        self.interleaved_blocks.setEnabled(False)
        self._selection_changed()
        self.status.setText(f"Next block sources changed. Load profiles to add another block. "
                            f"{len(self._queued_blocks)} queued blocks are retained." if self._batch_mode
                            else "Sources changed. Load profiles again before finalization.")

    def _selection_changed(self, *_args):
        if hasattr(self, "start"):
            ready = not self._busy and not self._closing
            selected = self._loaded_sources is not None and all(selector.currentData() is not None for selector in self.profiles)
            self.load_interleaved.setEnabled(ready and self._loaded_sources is not None
                                            and len(set(self._loaded_sources)) == 1)
            if self._batch_mode:
                self.add_block.setEnabled(ready and selected and len(self._queued_blocks) < 256)
                self.start.setEnabled(ready and bool(self._queued_blocks) and bool(self.journal.text().strip()))
                self._queue_selection_changed()
            else:
                self.start.setEnabled(ready and selected)

    def _queue_selection_changed(self):
        self.remove_block.setEnabled(not self._busy and not self._closing and self.block_table.currentRow() >= 0)

    def _remove_block(self):
        index = self.block_table.currentRow()
        if 0 <= index < len(self._queued_blocks) and not self._busy and not self._closing:
            self._queued_blocks.pop(index)
            self.block_table.removeRow(index)
            self.status.setText(f"{len(self._queued_blocks)} blocks queued. Source archives remain unchanged.")
            self._selection_changed()

    def _add_block(self):
        try:
            if self._busy or self._closing or len(self._queued_blocks) >= 256:
                raise ValueError("Wait for inspection or remove a block; the batch limit is 256.")
            request = self._selected_block()
            target = request.destination.resolve()
            sources = {path.resolve() for block in (*self._queued_blocks, request)
                       for path in (block.signal_path, block.before_path, block.after_path)}
            outputs = {block.destination.resolve() for block in self._queued_blocks}
            if target in outputs or target.exists() or target in sources or outputs & sources:
                raise ValueError("Choose a distinct new output separate from all queued source archives.")
            self._queued_blocks.append(request)
            row = self.block_table.rowCount()
            self.block_table.insertRow(row)
            points = request.point_indices
            caption = "Entire archive" if points is None else (
                f"{len(points)} {'point' if len(points) == 1 else 'points'}: " + (", ".join(map(str, points)) if len(points) <= 8
                                             else f"{points[0]} … {points[-1]}")
            )
            for column, text in enumerate((caption, f"{request.before_profile_id} / {request.after_profile_id}", target.name)):
                item = QTableWidgetItem(text)
                item.setToolTip(f"SIGNAL: {request.signal_path}\nREF before: {request.before_path}\n"
                    f"REF after: {request.after_path}\nOutput: {target}")
                self.block_table.setItem(row, column, item)
            self.status.setText(f"{len(self._queued_blocks)} blocks queued. Choose another block or finalize the batch.")
            self._selection_changed()
        except (ValueError, TypeError) as exc:
            self.status.setText("Selection requires correction: " + str(exc))

    def _set_busy(self, busy):
        self._busy = busy
        for editor in self._editors:
            editor.setEnabled(not busy and not self._closing)
        self.inspect.setEnabled(not busy and not self._closing)
        for selector in self.profiles:
            selector.setEnabled(not busy and not self._closing and self._loaded_sources is not None)
        self.interleaved_blocks.setEnabled(not busy and not self._closing and self.interleaved_blocks.count() > 0)
        self._selection_changed()

    def _inspect(self):
        try:
            if not all(edit.text().strip() for edit in self.paths):
                raise ValueError("Choose SIGNAL, REF before and REF after archives.")
            request = SpectrumFinalizationSourcesRequest(*(Path(edit.text().strip()) for edit in self.paths))
            self._set_busy(True)
            self.progress.setRange(0, 0)
            self.progress.show()
            self.status.setText("Reading and verifying available profiles…")
            self._controller.inspect_finalization_sources(request)
        except (ValueError, BufferError) as exc:
            self._failed("inspect_finalization", str(exc))

    def _completed(self, operation, result):
        if operation == "inspect_interleaved_blocks" and not self._closing:
            path, blocks = result
            if self._loaded_sources != (path, path, path):
                self._failed(operation, "Source paths changed during interleaved inspection.")
                return
            self.interleaved_blocks.clear()
            self.interleaved_blocks.addItem("Choose a recorded SIGNAL block…", userData=None)
            for block in blocks:
                suffix = "bracketed" if block.after_profile_id is not None else "missing REF after"
                self.interleaved_blocks.addItem(
                    f"Points {block.start_point}–{block.stop_point} · {suffix}", userData=block)
            self.progress.hide()
            self._set_busy(False)
            self.status.setText("Choose a SIGNAL block; its recorded REF neighbours will be selected explicitly.")
            return
        if operation != "inspect_finalization" or self._closing:
            return
        if tuple(Path(edit.text().strip()).resolve() for edit in self.paths) != tuple(
                source for source, _profiles, _points in result):
            self._failed(operation, "Sources changed during inspection. Load their profiles again.")
            return
        self._loaded_sources = tuple(source for source, _profiles, _points in result)
        for selector, (_path, choices, _points) in zip(self.profiles, result, strict=True):
            selector.clear()
            selector.addItem("Choose a profile…", userData=None)
            for identity, state, count, _start, end, _context in choices:
                try:
                    at = datetime.fromtimestamp(end, timezone.utc).isoformat(timespec="milliseconds")
                except (ValueError, OverflowError, OSError):
                    at = f"{end} s UTC"
                selector.addItem(f"{identity} · {state.replace(chr(10), ' ')[:80]} · {count} sweeps · {at}",
                                 userData=identity)
            selector.setCurrentIndex(1 if len(choices) == 1 else 0)
        self.progress.hide()
        self._set_busy(False)
        self.status.setText(f"Profiles verified. SIGNAL contains {result[0][2]} checkpoints. "
                            "Choose a profile in each source and the SIGNAL points to finalize.")
        if self._initial_source_path is not None:
            self._inspect_interleaved()

    def _inspect_interleaved(self):
        if self._busy or self._closing or self._loaded_sources is None or len(set(self._loaded_sources)) != 1:
            return
        try:
            self._set_busy(True)
            self.progress.setRange(0, 0)
            self.progress.show()
            self.status.setText("Reading SIGNAL blocks and their committed REF boundaries…")
            self._controller.inspect_interleaved_blocks(self._loaded_sources[0])
        except (ValueError, BufferError) as exc:
            self._failed("inspect_interleaved_blocks", str(exc))

    def _interleaved_selection_changed(self, *_args):
        block = self.interleaved_blocks.currentData()
        if block is None:
            return
        self.indices.setText(f"{block.start_point}:{block.stop_point + 1}")
        for selector, identity in zip(self.profiles,
                (block.before_profile_id, block.before_profile_id, block.after_profile_id), strict=True):
            selector.setCurrentIndex(selector.findData(identity) if identity is not None else 0)
        self.status.setText("Recorded REF neighbours selected. Choose a new output for this block." if block.after_profile_id
                            else "This SIGNAL block has no completed REF after it; bracketed finalization is unavailable.")

    def _failed(self, operation, error):
        if operation in {"inspect_finalization", "inspect_interleaved_blocks"} and not self._closing:
            self._loaded_sources = None
            self.interleaved_blocks.clear()
            self.interleaved_blocks.setEnabled(False)
            self.progress.hide()
            self._set_busy(False)
            self.status.setText("Profile inspection failed: " + error)

    def _cancelled(self, operation):
        self._failed(operation, "Inspection canceled; source archives unchanged.")

    def _selected_block(self):
        if self._loaded_sources is None or not all(selector.currentData() is not None for selector in self.profiles):
            raise ValueError("Load sources and explicitly choose every profile.")
        if not self.output.text().strip():
            raise ValueError("Choose a new output artifact.")
        indices = None
        if self.indices.text().strip():
            text = self.indices.text().strip()
            if ":" in text:
                bounds = text.split(":")
                if len(bounds) != 2 or any(not value.isdecimal() or len(value) > 19 for value in bounds):
                    raise ValueError("Use start:end for a checkpoint range; the end is excluded.")
                start, stop = map(int, bounds)
                if not 0 <= start < stop or stop - start > 100_000:
                    raise ValueError("Choose an increasing checkpoint range containing at most 100000 sweeps.")
                indices = tuple(range(start, stop))
                parts = []
            else:
                parts = text.replace(",", " ").split()
            if indices is None and (not parts or not all(part.isdecimal() for part in parts)):
                raise ValueError("Use zero-based nonnegative checkpoint numbers, separated by commas or spaces.")
            if indices is None:
                indices = tuple(int(part) for part in parts)
            if any(first >= second for first, second in zip(indices, indices[1:])):
                raise ValueError("Checkpoint numbers must be unique and strictly ordered from lowest to highest.")
        signal, before, after = self._loaded_sources
        return SpectrumFinalizationRequest(signal, before, after,
            Path(self.output.text().strip()), point_indices=indices,
            signal_profile_id=self.profiles[0].currentData(),
            before_profile_id=self.profiles[1].currentData(),
            after_profile_id=self.profiles[2].currentData())

    def _submit(self):
        try:
            if self._busy or self._closing:
                return
            if self._batch_mode:
                if not self.journal.text().strip():
                    raise ValueError("Choose a new batch journal.")
                journal = Path(self.journal.text().strip()).resolve()
                paths = {path.resolve() for block in self._queued_blocks for path in
                    (block.signal_path, block.before_path, block.after_path, block.destination)}
                if journal.exists() or journal in paths:
                    raise ValueError("Choose a new journal separate from every source and output.")
                self._pending_request = SpectrumFinalizationBatchRequest(tuple(self._queued_blocks), journal)
            else:
                self._pending_request = self._selected_block()
            self._closing = True
            self._set_busy(False)
            self.status.setText("Preparing selected block for finalization…")
            self._finish_selection()
        except (ValueError, TypeError) as exc:
            self.status.setText("Selection requires correction: " + str(exc))

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
        self.status.setText("Closing profile inspection…")
        self._finish_selection()

    def closeEvent(self, event):
        event.ignore()
        self.reject()

    def shutdown(self):
        self._closing = True
        self._pending_request = None
        return self._controller.close(wait_ms=0)
