"""Fluent dialog and value contract for manual Anritsu spectrum archives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    TitleLabel,
)

from app.domain.manual_metadata import ManualMetadataValue
from app.storage import ManualSpectrumSaveMode
from app.ui.dialogs import StationDialog
from app.ui.dialogs import StationFileDialog as QFileDialog


@dataclass(frozen=True, slots=True)
class ManualSpectrumSaveOptions:
    destination: Path
    mode: ManualSpectrumSaveMode
    metadata_scope: str
    metadata_values: tuple[ManualMetadataValue, ...]
    trace_variant: str


class ManualSpectrumSaveDialog(StationDialog):
    """Choose the file policy, trace variant and confirmed device values."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        trace_choices: Sequence[tuple[str, str]],
        metadata_values: Sequence[ManualMetadataValue],
        default_destination: str | Path,
        default_mode: ManualSpectrumSaveMode = ManualSpectrumSaveMode.APPEND,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("manualSpectrumSaveDialog")
        self.setProperty("stationSurface", "raised")
        self.setWindowTitle("Save manual spectrum")
        self.setModal(True)
        self.setMinimumSize(680, 620)
        self.resize(760, 720)

        unique_metadata: dict[str, ManualMetadataValue] = {}
        for value in metadata_values:
            unique_metadata.setdefault(value.key, value)
        self._metadata_values = tuple(unique_metadata.values())
        self._metadata_checks: dict[str, CheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        heading = TitleLabel("Manual spectrum archive", self)
        layout.addWidget(heading)
        note = BodyLabel(
            "Save the completed trace currently visible in Anritsu. Device values below "
            "are the last confirmed readbacks already held by the station; this dialog "
            "does not send a new instrument query or change an output.",
            self,
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.destination = LineEdit(self)
        self.destination.setText(str(default_destination))
        self.destination.setPlaceholderText("Choose a base name, e.g. manual_spectrum.h5")
        self.destination.setClearButtonEnabled(True)
        browse = PushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        destination_row = QHBoxLayout()
        destination_row.setContentsMargins(0, 0, 0, 0)
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(browse)
        destination_host = QWidget(self)
        destination_host.setLayout(destination_row)
        form.addRow("HDF5 destination", destination_host)

        self.mode = ComboBox(self)
        self.mode.addItem(
            "Append subsequent spectra to this HDF5",
            userData=ManualSpectrumSaveMode.APPEND.value,
        )
        self.mode.addItem(
            "Create a new timestamped HDF5 for every save",
            userData=ManualSpectrumSaveMode.TIMESTAMPED.value,
        )
        mode_index = self.mode.findData(default_mode.value)
        self.mode.setCurrentIndex(max(0, mode_index))
        self.mode.setToolTip(
            "Append keeps one stable frequency grid. Timestamped mode adds the current "
            "UTC date and time to the selected base name."
        )
        form.addRow("File policy", self.mode)

        self.trace = ComboBox(self)
        for key, label in trace_choices:
            self.trace.addItem(label, userData=key)
        form.addRow("Spectrum variant", self.trace)

        self.metadata_scope = ComboBox(self)
        self.metadata_scope.addItem(
            "All available confirmed values",
            userData="all",
        )
        self.metadata_scope.addItem(
            "Only selected values",
            userData="selected",
        )
        self.metadata_scope.addItem("No device values", userData="none")
        self.metadata_scope.setCurrentIndex(0 if self._metadata_values else 2)
        self.metadata_scope.currentIndexChanged.connect(self._metadata_scope_changed)
        form.addRow("Device metadata", self.metadata_scope)
        layout.addLayout(form)

        values_title = StrongBodyLabel("Values attached to every saved spectrum", self)
        values_title.setObjectName("sectionTitle")
        layout.addWidget(values_title)
        values_hint = CaptionLabel(
            "Select individual readings such as Keithley B current, or use the policy above.",
            self,
        )
        values_hint.setObjectName("muted")
        layout.addWidget(values_hint)
        self.values_scroll = ScrollArea(self)
        self.values_scroll.setWidgetResizable(True)
        self.values_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        values_host = QWidget(self.values_scroll)
        values_layout = QVBoxLayout(values_host)
        values_layout.setContentsMargins(4, 4, 8, 4)
        values_layout.setSpacing(7)
        if self._metadata_values:
            for value in self._metadata_values:
                check = CheckBox(value.label, values_host)
                check.setChecked(True)
                check.setToolTip(f"{value.source} · {value.key}")
                value_label = CaptionLabel(
                    f"{value.display_value}  ·  {value.device}", values_host
                )
                value_label.setObjectName("muted")
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.addWidget(check)
                row.addStretch(1)
                row.addWidget(value_label)
                row_host = QWidget(values_host)
                row_host.setLayout(row)
                values_layout.addWidget(row_host)
                self._metadata_checks[value.key] = check
        else:
            empty = BodyLabel(
                "No confirmed device values are available yet. The spectrum can still be saved without metadata.",
                values_host,
            )
            empty.setWordWrap(True)
            empty.setObjectName("muted")
            values_layout.addWidget(empty)
        values_layout.addStretch(1)
        self.values_scroll.setWidget(values_host)
        self.values_scroll.setMinimumHeight(160)
        layout.addWidget(self.values_scroll, 1)

        self.error_label = CaptionLabel("", self)
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.save_button = PrimaryPushButton("Save spectrum", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.accept)
        self._metadata_scope_changed()

    def accept(self) -> None:
        destination = self.destination.text().strip()
        if not destination:
            self.error_label.setText("Choose an HDF5 destination before saving.")
            self.destination.setFocus()
            return
        if not self.trace.count():
            self.error_label.setText("No completed spectrum variant is available.")
            return
        super().accept()

    def options(self) -> ManualSpectrumSaveOptions:
        selected_keys = {
            key for key, checkbox in self._metadata_checks.items() if checkbox.isChecked()
        }
        return ManualSpectrumSaveOptions(
            destination=Path(self.destination.text().strip()).expanduser(),
            mode=ManualSpectrumSaveMode(self.mode.currentData()),
            metadata_scope=str(self.metadata_scope.currentData()),
            metadata_values=tuple(
                value for value in self._metadata_values if value.key in selected_keys
            ),
            trace_variant=str(self.trace.currentData()),
        )

    def _browse(self) -> None:
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Choose manual spectrum HDF5",
            self.destination.text().strip(),
            "HDF5 measurement (*.h5 *.hdf5)",
        )
        if selected:
            self.destination.setText(selected)

    def _metadata_scope_changed(self, *_args: object) -> None:
        scope = str(self.metadata_scope.currentData() or "none")
        for key, checkbox in self._metadata_checks.items():
            if scope == "all":
                checkbox.setChecked(True)
                checkbox.setEnabled(False)
            elif scope == "none":
                checkbox.setChecked(False)
                checkbox.setEnabled(False)
            else:
                checkbox.setEnabled(True)
