"""Fluent cards for the results of a conservative VISA discovery scan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QResizeEvent
from PySide6.QtWidgets import QBoxLayout, QScrollArea, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    IconWidget,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
)

from app.devices.discovery import DiscoveredInstrument
from app.ui.design_system.tokens import SPACING


AssignmentTargets = Mapping[str, str]
DEFAULT_ASSIGNMENT_TARGETS: AssignmentTargets = {
    "rigol": "Rigol DG1032Z",
    "keithley": "Keithley 2600",
    "anritsu": "Anritsu MS2830A",
    "lakeshore_gaussmeter": "Lake Shore 475",
}


@dataclass(frozen=True, slots=True)
class VisaResultState:
    """One discovery result enriched with its station-assignment state."""

    result: DiscoveredInstrument
    status: Literal["recognized", "unknown", "unavailable", "assigned"]
    configured_device: str | None

    @classmethod
    def from_result(
        cls,
        result: DiscoveredInstrument,
        *,
        configured_device: str | None,
    ) -> "VisaResultState":
        if configured_device is not None:
            status = "assigned"
        elif result.idn is None:
            status = "unavailable"
        elif result.device is None:
            status = "unknown"
        else:
            status = "recognized"
        return cls(result=result, status=status, configured_device=configured_device)


class VisaResultRow(CardWidget):
    """A single VISA resource with a deliberate, locally scoped assignment action."""

    assignment_requested = Signal(str, str, str)

    def __init__(
        self,
        state: VisaResultState,
        *,
        assignment_targets: AssignmentTargets,
        assignment_allowed: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self._assignment_allowed = assignment_allowed
        self.setAccessibleName(f"VISA scan result for {state.result.resource}")
        self.setMinimumHeight(142)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING["lg"], SPACING["md"], SPACING["lg"], SPACING["md"])
        layout.setSpacing(SPACING["sm"])

        self.top_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        top_row = self.top_row
        top_row.setSpacing(SPACING["sm"])
        identity = BodyLabel(self._identity_text, self)
        identity.setWordWrap(True)
        identity.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        identity.setAccessibleName(f"Instrument identity for {state.result.resource}")
        top_row.addWidget(identity, 1)
        self.status = CaptionLabel(self._status_text, self)
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status.setAccessibleName(f"VISA status {self._status_text} for {state.result.resource}")
        top_row.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top_row)

        self.resource_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        resource_row = self.resource_row
        resource_row.setSpacing(SPACING["sm"])
        self.resource = BodyLabel(state.result.resource, self)
        self.resource.setWordWrap(True)
        self.resource.setMinimumWidth(0)
        self.resource.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.resource.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.resource.setToolTip("VISA resource — select text to copy")
        self.resource.setAccessibleName(f"VISA resource {state.result.resource}")
        resource_row.addWidget(self.resource, 1)
        self.copy_button = PushButton("Copy", self)
        self.copy_button.setAccessibleName(f"Copy VISA resource {state.result.resource}")
        self.copy_button.setToolTip("Copy this VISA resource to the clipboard")
        self.copy_button.clicked.connect(self._copy_resource)
        resource_row.addWidget(self.copy_button)
        layout.addLayout(resource_row)

        details = CaptionLabel(
            f"Backend: {state.result.backend}  •  {state.result.idn or state.result.error or 'No response'}",
            self,
        )
        details.setWordWrap(True)
        details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details.setAccessibleName(f"VISA details for {state.result.resource}")
        layout.addWidget(details)

        self.action_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        action_row = self.action_row
        action_row.setSpacing(SPACING["sm"])
        self.assignment = ComboBox(self)
        self.assignment.setMinimumWidth(0)
        self.assignment.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.assignment.setAccessibleName(f"Assign VISA resource {state.result.resource} to an instrument")
        self.assignment.setPlaceholderText("Choose station instrument")
        self.assignment.addItem("Choose station instrument", userData=None)
        for key, label in assignment_targets.items():
            self.assignment.addItem(label, userData=key)
        if state.result.device and self.assignment.findData(state.result.device) >= 0:
            self.assignment.setCurrentIndex(self.assignment.findData(state.result.device))
        else:
            self.assignment.setCurrentIndex(0)
        action_row.addWidget(self.assignment, 1)
        self.assign_button = PrimaryPushButton("Assign", self)
        self.assign_button.setAccessibleName(f"Assign VISA resource {state.result.resource}")
        self.assign_button.setToolTip("Save this VISA resource as the selected instrument connection")
        action_row.addWidget(self.assign_button)
        layout.addLayout(action_row)

        self.assignment.currentIndexChanged.connect(self._update_assignment_controls)
        self.assign_button.clicked.connect(self._request_assignment)
        self.set_assignment_allowed(assignment_allowed)

    def set_compact_layout(self, compact: bool) -> None:
        """Stack controls when a station window becomes narrow."""

        direction = (
            QBoxLayout.Direction.TopToBottom
            if compact
            else QBoxLayout.Direction.LeftToRight
        )
        self.resource_row.setDirection(direction)
        self.action_row.setDirection(direction)
        self.top_row.setDirection(direction)

    @property
    def _identity_text(self) -> str:
        return self.state.result.idn or "Instrument did not respond to identification"

    @property
    def _status_text(self) -> str:
        return {
            "recognized": "Recognized",
            "unknown": "Unknown instrument",
            "unavailable": "Unavailable",
            "assigned": f"Assigned to {self.state.configured_device}",
        }[self.state.status]

    @property
    def _can_assign(self) -> bool:
        return (
            self._assignment_allowed
            and self.state.status not in {"unavailable", "assigned"}
            and self.assignment.currentData() is not None
        )

    def set_assignment_allowed(self, allowed: bool) -> None:
        self._assignment_allowed = allowed
        editable = allowed and self.state.status not in {"unavailable", "assigned"}
        self.assignment.setEnabled(editable)
        self.assign_button.setEnabled(self._can_assign)
        if not allowed:
            message = "An engineer or service role is required to change VISA assignments."
            self.assignment.setToolTip(message)
            self.assign_button.setToolTip(message)
        elif self.state.status == "assigned":
            self.assignment.setToolTip("This VISA resource is already assigned.")
            self.assign_button.setToolTip("This VISA resource is already assigned.")
        elif self.state.status == "unavailable":
            self.assignment.setToolTip("An unresponsive VISA resource cannot be assigned.")
            self.assign_button.setToolTip("An unresponsive VISA resource cannot be assigned.")
        else:
            self.assignment.setToolTip("")
            self.assign_button.setToolTip("Save this VISA resource as the selected instrument connection")

    def _update_assignment_controls(self) -> None:
        self.assign_button.setEnabled(self._can_assign)

    def _request_assignment(self) -> None:
        device = self.assignment.currentData()
        if isinstance(device, str) and self._can_assign:
            self.assignment_requested.emit(device, self.state.result.resource, self.state.result.backend)

    def _copy_resource(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.state.result.resource)


class VisaResultsView(QWidget):
    """Scrollable collection of Fluent VISA result cards."""

    assignment_requested = Signal(object)
    _COMPACT_BREAKPOINT = 420

    def __init__(
        self,
        *,
        assignment_targets: AssignmentTargets | None = None,
        assignment_allowed: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.rows: list[VisaResultRow] = []
        self._assignment_allowed = assignment_allowed
        self._assignment_targets = dict(assignment_targets or DEFAULT_ASSIGNMENT_TARGETS)

        layout = QStackedLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.empty_state = CardWidget(self)
        self.empty_state.setProperty("stationSurface", "raised")
        self.empty_state.setAccessibleName("No VISA scan results")
        self.empty_state.setMinimumHeight(196)
        self.empty_state.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(SPACING["xl"], SPACING["xl"], SPACING["xl"], SPACING["xl"])
        empty_layout.setSpacing(SPACING["sm"])
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_icon = IconWidget(FluentIcon.SEARCH, self.empty_state)
        self.empty_icon.setFixedSize(32, 32)
        empty_layout.addWidget(self.empty_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty_title = StrongBodyLabel("Ready to discover VISA instruments", self.empty_state)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        self.empty_description = BodyLabel(
            "Start a scan to enumerate VISA resources. Only *IDN? is sent, with a short timeout; instrument outputs are not changed.",
            self.empty_state,
        )
        self.empty_description.setWordWrap(True)
        self.empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_description.setMaximumWidth(560)
        empty_layout.addWidget(self.empty_description, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.empty_state)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setProperty("stationSurface", "surface")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.content = QWidget(self.scroll_area)
        self.scroll_area.viewport().setProperty("stationSurface", "surface")
        self.content.setProperty("stationSurface", "surface")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, SPACING["sm"], 0, 0)
        self.content_layout.setSpacing(SPACING["md"])
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(self.content)
        layout.addWidget(self.scroll_area)
        layout.setCurrentWidget(self.empty_state)

    def set_results(self, states: tuple[VisaResultState, ...]) -> None:
        for row in self.rows:
            self.content_layout.removeWidget(row)
            row.deleteLater()
        self.rows.clear()
        self.layout().setCurrentWidget(self.scroll_area if states else self.empty_state)
        for state in states:
            row = VisaResultRow(
                state,
                assignment_targets=self._assignment_targets,
                assignment_allowed=self._assignment_allowed,
                parent=self.content,
            )
            row.assignment_requested.connect(
                lambda device, resource, backend, source=row: self._emit_assignment(
                    source, device, resource, backend
                )
            )
            self.rows.append(row)
            self.content_layout.addWidget(row)
        self._update_compact_rows()

    def set_assignment_allowed(self, allowed: bool) -> None:
        self._assignment_allowed = allowed
        for row in self.rows:
            row.set_assignment_allowed(allowed)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_compact_rows()

    def _update_compact_rows(self) -> None:
        compact = self.width() < self._COMPACT_BREAKPOINT
        for row in self.rows:
            row.set_compact_layout(compact)

    def _emit_assignment(
        self,
        row: VisaResultRow,
        device: str,
        resource: str,
        backend: str,
    ) -> None:
        self.assignment_requested.emit({device: (resource, backend, row.state.result.idn)})
