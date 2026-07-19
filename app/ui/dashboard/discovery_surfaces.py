"""Fluent card surfaces for TCP/IP discovery and saved station connections."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, FluentIcon, IconWidget, StrongBodyLabel

from app.ui.design_system.tokens import SPACING


class DiscoveryEmptyState(CardWidget):
    """A calm, directional empty state shared by discovery result surfaces."""

    def __init__(
        self,
        *,
        icon: FluentIcon,
        title: str,
        description: str,
        accessible_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "raised")
        self.setAccessibleName(accessible_name)
        self.setMinimumHeight(176)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING["xl"], SPACING["xl"], SPACING["xl"], SPACING["xl"])
        layout.setSpacing(SPACING["sm"])
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_widget = IconWidget(icon, self)
        icon_widget.setFixedSize(30, 30)
        layout.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignHCenter)
        title_label = StrongBodyLabel(title, self)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        description_label = BodyLabel(description, self)
        description_label.setWordWrap(True)
        description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description_label.setMaximumWidth(560)
        layout.addWidget(description_label, 0, Qt.AlignmentFlag.AlignHCenter)


class TcpEndpointRow(CardWidget):
    """Focusable Fluent card representing one host observed by a TCP scan."""

    selected = Signal(str)

    def __init__(
        self,
        *,
        host: str,
        endpoint: str,
        state: str,
        verification: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self.endpoint = endpoint
        self.state = state
        self.verification = verification
        self.setProperty("stationSurface", "card")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"TCP discovery result {endpoint}")
        self.setToolTip("Select this endpoint to run the read-only MOKE identification.")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING["lg"], SPACING["md"], SPACING["lg"], SPACING["md"])
        layout.setSpacing(SPACING["xs"])
        header = QHBoxLayout()
        self.endpoint_label = StrongBodyLabel(endpoint, self)
        self.endpoint_label.setWordWrap(True)
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.endpoint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.endpoint_label, 1)
        self.state_label = CaptionLabel(parent=self)
        self.state_label.setProperty("stationState", state)
        self.state_label.setAccessibleName(f"TCP status {state} for {endpoint}")
        header.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self.verification_label = BodyLabel(parent=self)
        self.verification_label.setWordWrap(True)
        self.verification_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.verification_label)
        self.update_result(endpoint=endpoint, state=state, verification=verification)

    def update_result(self, *, endpoint: str, state: str, verification: str) -> None:
        self.endpoint = endpoint
        self.state = state
        self.verification = verification
        self.endpoint_label.setText(endpoint)
        self.state_label.setText(_tcp_state_label(state))
        self.state_label.setProperty("stationState", state)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)
        self.verification_label.setText(verification or "No verification result")
        self.setAccessibleName(f"TCP discovery result {endpoint}: {_tcp_state_label(state)}")

    def set_selected(self, selected: bool) -> None:
        self.setProperty("stationSelected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        if selected:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def click(self) -> None:
        """Select this result (also useful for keyboard and focused tests)."""

        self.selected.emit(self.host)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.click()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class TcpDiscoveryResultsView(QWidget):
    """Scrollable, selection-aware Fluent cards replacing the TCP result table."""

    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows_by_host: dict[str, TcpEndpointRow] = {}
        self._selected_host: str | None = None
        layout = QStackedLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setProperty("stationSurface", "surface")
        self.content = QWidget(self.scroll_area)
        self.content.setProperty("stationSurface", "surface")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(SPACING["sm"])
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty_state = DiscoveryEmptyState(
            icon=FluentIcon.GLOBE,
            title="No TCP/IP endpoints yet",
            description="Enter one private address or a bounded range, then scan the configured port. Open endpoints can be identified before assignment.",
            accessible_name="No TCP endpoints discovered",
            parent=self,
        )
        self.scroll_area.setWidget(self.content)
        layout.addWidget(self.empty_state)
        layout.addWidget(self.scroll_area)
        layout.setCurrentWidget(self.empty_state)

    @property
    def row_count(self) -> int:
        return len(self._rows_by_host)

    @property
    def selected_host(self) -> str | None:
        return self._selected_host

    @property
    def selected_endpoint(self) -> str | None:
        row = self._selected_row
        return row.endpoint if row else None

    @property
    def selected_state(self) -> str | None:
        row = self._selected_row
        return row.state if row else None

    @property
    def selected_verification(self) -> str | None:
        row = self._selected_row
        return row.verification if row else None

    @property
    def _selected_row(self) -> TcpEndpointRow | None:
        return self._rows_by_host.get(self._selected_host or "")

    def clear(self) -> None:
        for row in self._rows_by_host.values():
            self.content_layout.removeWidget(row)
            row.deleteLater()
        self._rows_by_host.clear()
        self._selected_host = None
        self.layout().setCurrentWidget(self.empty_state)
        self.selection_changed.emit()

    def upsert_endpoint(
        self,
        *,
        host: str,
        endpoint: str,
        state: str,
        verification: str,
    ) -> None:
        row = self._rows_by_host.get(host)
        if row is None:
            row = TcpEndpointRow(
                host=host,
                endpoint=endpoint,
                state=state,
                verification=verification,
                parent=self.content,
            )
            row.selected.connect(self.select_host)
            self._rows_by_host[host] = row
            self.content_layout.addWidget(row)
        else:
            row.update_result(endpoint=endpoint, state=state, verification=verification)
        self.layout().setCurrentWidget(self.scroll_area)
        row.set_selected(host == self._selected_host)

    def row_for_host(self, host: str) -> TcpEndpointRow | None:
        return self._rows_by_host.get(host)

    def select_host(self, host: str) -> None:
        if host not in self._rows_by_host:
            return
        changed = host != self._selected_host
        self._selected_host = host
        for candidate_host, row in self._rows_by_host.items():
            row.set_selected(candidate_host == host)
        if changed:
            self.selection_changed.emit()


class SavedInstrumentCard(CardWidget):
    """Read-only summary of one saved station connection."""

    def __init__(self, values: tuple[str, str, str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        instrument, resource, backend, status = values
        self.setProperty("stationSurface", "card")
        self.setAccessibleName(f"Saved instrument {instrument}")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING["lg"], SPACING["md"], SPACING["lg"], SPACING["md"])
        layout.setSpacing(SPACING["xs"])
        header = QHBoxLayout()
        title = StrongBodyLabel(instrument, self)
        title.setWordWrap(True)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(title, 1)
        state = CaptionLabel(status, self)
        state.setProperty("stationState", status.lower().replace(" ", "_"))
        state.setWordWrap(True)
        header.addWidget(state, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        resource_label = BodyLabel(resource, self)
        resource_label.setWordWrap(True)
        resource_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        resource_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        resource_label.setToolTip(resource)
        layout.addWidget(resource_label)
        backend_label = CaptionLabel(f"Backend: {backend}", self)
        backend_label.setWordWrap(True)
        layout.addWidget(backend_label)


class SavedInstrumentsView(QWidget):
    """Responsive Fluent card list for the saved station inventory."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cards: list[SavedInstrumentCard] = []
        layout = QStackedLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setProperty("stationSurface", "surface")
        self.content = QWidget(self.scroll_area)
        self.content.setProperty("stationSurface", "surface")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(SPACING["sm"])
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty_state = DiscoveryEmptyState(
            icon=FluentIcon.SAVE,
            title="No saved instruments",
            description="Verified VISA and TCP/IP assignments will appear here. Connection and output controls remain on each instrument page.",
            accessible_name="No saved instrument resources",
            parent=self,
        )
        self.scroll_area.setWidget(self.content)
        layout.addWidget(self.empty_state)
        layout.addWidget(self.scroll_area)
        layout.setCurrentWidget(self.empty_state)

    @property
    def count(self) -> int:
        return len(self.cards)

    def set_instruments(self, values: Iterable[tuple[str, str, str, str]]) -> None:
        for card in self.cards:
            self.content_layout.removeWidget(card)
            card.deleteLater()
        self.cards.clear()
        values = tuple(values)
        self.layout().setCurrentWidget(self.scroll_area if values else self.empty_state)
        for value in values:
            card = SavedInstrumentCard(value, self.content)
            self.cards.append(card)
            self.content_layout.addWidget(card)


def _tcp_state_label(state: str) -> str:
    return {
        "scanning": "Scanning…",
        "closed": "Closed",
        "open": "TCP port open",
        "cancelled": "Cancelled",
        "entered": "Entered manually",
    }.get(state, state.replace("_", " ").title())
