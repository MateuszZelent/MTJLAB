"""Metadata inspection panel with tabbed detail views."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.storage import RunDetail, ThatecRun
from app.storage.pythat_reader import PyThatRunData


class MetadataPanel(QWidget):
    """QTabWidget showing Metadata, Recipe, Settings, PyThat data and Device state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.metadata = QPlainTextEdit()
        self.recipe_snapshot = QPlainTextEdit()
        self.settings_snapshot = QPlainTextEdit()
        self.pythat_data = QPlainTextEdit()
        self.device_state = QPlainTextEdit()

        for widget in (
            self.metadata,
            self.recipe_snapshot,
            self.settings_snapshot,
            self.pythat_data,
            self.device_state,
        ):
            widget.setReadOnly(True)

        self.tabs.addTab(self.metadata, "Metadata")
        self.tabs.addTab(self.recipe_snapshot, "Recipe")
        self.tabs.addTab(self.settings_snapshot, "Settings")
        self.tabs.addTab(self.pythat_data, "PyThat data")
        self.tabs.addTab(self.device_state, "Device state")
        layout.addWidget(self.tabs)

    def show_detail(self, detail: RunDetail) -> None:
        """Populate all tabs from a private HDF5 run detail."""
        summary = detail.summary
        lines = [
            f"File: {summary.path}",
            f"State: {summary.status}",
            f"Created (UTC): {summary.created_at_utc or 'missing'}",
            f"Application version: {summary.application_version or 'missing'}",
            f"Plan hash: {summary.plan_sha256 or 'missing'}",
            f"Checkpoints: {summary.point_count}; stored spectra: {summary.spectrum_count}",
            "",
            "Instrument identities:",
        ]
        lines.extend(
            f"  {name}: {idn}" for name, idn in sorted(detail.device_idn.items())
        )
        lines.extend(
            ("", "Authenticated operator:", _format_json(detail.operator_context))
        )
        lines.extend(
            ("", "Capabilities (snapshot):", _format_json(detail.capabilities))
        )
        if detail.events:
            lines.extend(("", f"Recent events ({len(detail.events)}):"))
            lines.extend(
                f"  {event.timestamp_utc} [{event.severity}] {event.name}"
                for event in detail.events[-20:]
            )
        self.metadata.setPlainText("\n".join(lines))
        self.recipe_snapshot.setPlainText(detail.recipe_yaml)
        self.settings_snapshot.setPlainText(detail.settings_yaml)

    def show_thatec_summary(self, path: Path, run: ThatecRun) -> None:
        """Show minimal metadata for a public THATEC file without private groups."""
        self.metadata.setPlainText(
            f"Public THATEC file: {path}\nRows: {len(run.rows)}\n"
            f"Devices: {len(run.devices)}"
        )
        self.recipe_snapshot.clear()
        self.settings_snapshot.clear()

    def show_pythat(self, data: PyThatRunData | None) -> None:
        """Populate the PyThat tab."""
        if data is not None:
            self.pythat_data.setPlainText(
                "Dimensions:\n"
                + _format_json(data.dimensions)
                + "\n\nVariables:\n"
                + "\n".join(data.variables)
            )
        else:
            self.pythat_data.setPlainText(
                "Public THATEC tree loaded directly; "
                "no private application metadata required."
            )

    def show_device_state(self, device_states: dict) -> None:
        """Show device state JSON for a selected point."""
        self.device_state.setPlainText(_format_json(device_states))

    def clear(self) -> None:
        """Clear all tabs."""
        self.metadata.clear()
        self.recipe_snapshot.clear()
        self.settings_snapshot.clear()
        self.pythat_data.clear()
        self.device_state.clear()


def _format_json(value: object) -> str:
    """Format a Python object as indented JSON."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
