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
    upload_to_elab: bool = False


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
        default_trace_variant: str = "raw",
        default_metadata_scope: str = "all",
        default_metadata_keys: Sequence[str] = (),
        default_upload_to_elab: bool = False,
        elab_upload_available: bool = False,
        elab_upload_hint: str = "",
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

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=12)
        heading = TitleLabel("Manual spectrum archive", surface)
        layout.addWidget(heading)
        note = BodyLabel(
            "Save the completed trace currently visible in Anritsu. Device values below "
            "are the last confirmed readbacks already held by the station; this dialog "
            "does not send a new instrument query or change an output.",
            surface,
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.destination = LineEdit(surface)
        self.destination.setText(str(default_destination))
        self.destination.setPlaceholderText("Choose a base name, e.g. manual_spectrum.h5")
        self.destination.setClearButtonEnabled(True)
        browse = PushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        destination_row = QHBoxLayout()
        destination_row.setContentsMargins(0, 0, 0, 0)
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(browse)
        destination_host = QWidget(surface)
        destination_host.setLayout(destination_row)
        form.addRow("HDF5 destination", destination_host)

        self.mode = ComboBox(surface)
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

        self.upload_to_elab = CheckBox("Upload this saved result to eLab", surface)
        self.upload_to_elab.setChecked(
            bool(default_upload_to_elab and elab_upload_available)
        )
        self.upload_to_elab.setEnabled(elab_upload_available)
        self.upload_to_elab.setToolTip(
            "Upload only after the local HDF5 save succeeds. eLab uploads use a closed "
            "timestamped file so an append archive cannot change after upload."
        )
        self.elab_upload_hint = CaptionLabel(
            elab_upload_hint
            or (
                "Configure a template in the eLabFTW tab first."
                if not elab_upload_available
                else "A closed timestamped HDF5 will be used for this eLab upload."
            ),
            surface,
        )
        self.elab_upload_hint.setObjectName("muted")
        self.elab_upload_hint.setWordWrap(True)
        elab_row = QVBoxLayout()
        elab_row.setContentsMargins(0, 0, 0, 0)
        elab_row.setSpacing(2)
        elab_row.addWidget(self.upload_to_elab)
        elab_row.addWidget(self.elab_upload_hint)
        elab_host = QWidget(surface)
        elab_host.setLayout(elab_row)
        form.addRow("eLabFTW", elab_host)
        self.upload_to_elab.toggled.connect(self._elab_upload_toggled)
        self.mode.currentIndexChanged.connect(self._elab_mode_changed)
        self._elab_upload_toggled(self.upload_to_elab.isChecked())

        self.trace = ComboBox(surface)
        for key, label in trace_choices:
            self.trace.addItem(label, userData=key)
        trace_index = self.trace.findData(default_trace_variant)
        self.trace.setCurrentIndex(max(0, trace_index))
        form.addRow("Spectrum variant", self.trace)

        self.metadata_scope = ComboBox(surface)
        self.metadata_scope.addItem(
            "All available confirmed values",
            userData="all",
        )
        self.metadata_scope.addItem(
            "Only selected values",
            userData="selected",
        )
        self.metadata_scope.addItem("No device values", userData="none")
        default_scope = default_metadata_scope if self._metadata_values else "none"
        scope_index = self.metadata_scope.findData(default_scope)
        self.metadata_scope.setCurrentIndex(max(0, scope_index))
        self.metadata_scope.currentIndexChanged.connect(self._metadata_scope_changed)
        form.addRow("Device metadata", self.metadata_scope)
        layout.addLayout(form)

        values_title = StrongBodyLabel(
            "Values attached to every saved spectrum", surface
        )
        values_title.setObjectName("sectionTitle")
        layout.addWidget(values_title)
        values_hint = CaptionLabel(
            "Select individual readings such as Keithley B current, or use the policy above.",
            surface,
        )
        values_hint.setObjectName("muted")
        layout.addWidget(values_hint)
        self.values_scroll = ScrollArea(surface)
        self.values_scroll.setWidgetResizable(True)
        self.values_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        selected_metadata_keys = set(default_metadata_keys)
        values_host = QWidget(self.values_scroll)
        values_layout = QVBoxLayout(values_host)
        values_layout.setContentsMargins(4, 4, 8, 4)
        values_layout.setSpacing(7)
        if self._metadata_values:
            for value in self._metadata_values:
                check = CheckBox(value.label, values_host)
                check.setChecked(
                    default_scope == "all"
                    or (
                        default_scope == "selected"
                        and value.key in selected_metadata_keys
                    )
                )
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

        self.error_label = CaptionLabel("", surface)
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", surface)
        self.save_button = PrimaryPushButton("Save spectrum", surface)
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
            upload_to_elab=self.upload_to_elab.isChecked(),
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

    def _elab_upload_toggled(self, checked: bool) -> None:
        append_index = self.mode.findData(ManualSpectrumSaveMode.APPEND.value)
        if append_index >= 0:
            self.mode.setItemEnabled(append_index, not checked)
        if checked and self.mode.currentData() == ManualSpectrumSaveMode.APPEND.value:
            timestamped_index = self.mode.findData(ManualSpectrumSaveMode.TIMESTAMPED.value)
            if timestamped_index >= 0:
                self.mode.setCurrentIndex(timestamped_index)
        if checked:
            self.elab_upload_hint.setText(
                "eLab upload is enabled. The local result will be timestamped and closed "
                "before the background upload starts."
            )

    def _elab_mode_changed(self, _index: int) -> None:
        if self.upload_to_elab.isChecked() and self.mode.currentData() == ManualSpectrumSaveMode.APPEND.value:
            timestamped_index = self.mode.findData(ManualSpectrumSaveMode.TIMESTAMPED.value)
            if timestamped_index >= 0:
                self.mode.setCurrentIndex(timestamped_index)
