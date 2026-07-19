"""Metadata inspection panel with tabbed detail views."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ComboBox, Pivot

from app.storage import RunDetail, ThatecRun
from app.storage.pythat_reader import PyThatRunData


class _FluentMetadataSections(QWidget):
    """Compact Fluent navigation for the immutable run-detail documents."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.navigation = Pivot(self)
        self.navigation.setItemFontSize(14)
        self.compact_navigation = ComboBox(self)
        self.compact_navigation.setAccessibleName("Result detail section")
        self.compact_navigation.hide()
        self.compact_navigation.currentIndexChanged.connect(self.setCurrentIndex)
        self.stack = QStackedWidget(self)
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        layout.addWidget(self.navigation)
        layout.addWidget(self.compact_navigation)
        layout.addWidget(self.stack, 1)
        self._routes: list[str] = []
        self._labels: list[str] = []

    def addTab(self, page: QWidget, label: str) -> int:
        index = self.stack.addWidget(page)
        route = f"metadata-section-{index}"
        self._routes.append(route)
        self._labels.append(label)
        self.compact_navigation.addItem(label, userData=index)
        self.navigation.addItem(
            route,
            label,
            onClick=lambda _checked=False, index=index: self.setCurrentIndex(index),
        )
        if index == 0:
            self.setCurrentIndex(index)
        return index

    def setCurrentIndex(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.navigation.setCurrentItem(self._routes[index])
        if self.compact_navigation.currentIndex() != index:
            self.compact_navigation.blockSignals(True)
            self.compact_navigation.setCurrentIndex(index)
            self.compact_navigation.blockSignals(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        compact = event.size().width() < 720
        self.navigation.setVisible(not compact)
        self.compact_navigation.setVisible(compact)

    def tabText(self, index: int) -> str:
        return self._labels[index]


class MetadataPanel(QWidget):
    """Fluent detail navigation for metadata, snapshots and device state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = _FluentMetadataSections(self)
        self.section_navigation = self.tabs.navigation
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
