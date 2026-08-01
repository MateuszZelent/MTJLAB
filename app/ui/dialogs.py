"""Shared Fluent-compatible popup infrastructure."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox as QtMessageBox, QWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    SubtitleLabel,
)


class StationDialog(QDialog):
    """Theme-aware host for every station-owned popup or floating window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        }:
            self.update()
            for child in self.findChildren(QWidget):
                child.update()


class StationAlertDialog(StationDialog):
    """Reliable Fluent alert that keeps its text above the modal surface."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        primary: QtMessageBox.StandardButton,
        secondary: QtMessageBox.StandardButton | None,
        default_button: QtMessageBox.StandardButton,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setProperty("stationSurface", "raised")
        parent_width = parent.width() if parent is not None else 720
        preferred_width = max(360, min(640, parent_width - 48))
        self.setMinimumWidth(preferred_width)
        self.setMaximumWidth(preferred_width)

        self.title_label = SubtitleLabel(title, self)
        self.title_label.setWordWrap(True)
        self.content_label = BodyLabel(text, self)
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        self.primary_button = PrimaryPushButton(
            StationMessageBox._button_label(primary), self
        )
        self.primary_button.setMinimumWidth(120)
        self.primary_button.clicked.connect(lambda: self._finish(primary, True))
        self.secondary_button: PushButton | None = None
        if secondary is not None:
            self.secondary_button = PushButton(
                StationMessageBox._button_label(secondary), self
            )
            self.secondary_button.setMinimumWidth(120)
            self.secondary_button.clicked.connect(
                lambda: self._finish(secondary, False)
            )

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        if self.secondary_button is not None:
            buttons.addWidget(self.secondary_button)
        buttons.addWidget(self.primary_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addWidget(self.content_label)
        layout.addSpacing(8)
        layout.addLayout(buttons)

        self.selected_button = QtMessageBox.StandardButton.NoButton
        if default_button == secondary and self.secondary_button is not None:
            self.secondary_button.setFocus()
        else:
            self.primary_button.setFocus()

    def _finish(self, button: QtMessageBox.StandardButton, accepted: bool) -> None:
        self.selected_button = button
        if accepted:
            self.accept()
        else:
            self.reject()


class StationSettingsGuidanceDialog(StationDialog):
    """An alert with a direct, non-destructive route to a configuration fix."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        action_label: str = "Go to settings",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setProperty("stationSurface", "raised")
        self.setMinimumWidth(max(360, min(640, (parent.width() if parent else 720) - 48)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        heading = SubtitleLabel(title, self)
        body = BodyLabel(text, self)
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = PushButton("Close", self)
        self.go_to_settings_button = PrimaryPushButton(action_label, self)
        close.clicked.connect(self.reject)
        self.go_to_settings_button.clicked.connect(self.accept)
        buttons.addWidget(close)
        buttons.addWidget(self.go_to_settings_button)
        layout.addLayout(buttons)
        self.go_to_settings_button.setFocus()


class SweepDeviceReadinessDialog(StationDialog):
    """Fluent preflight gate for the devices required by one sweep plan."""

    connect_missing_requested = Signal(tuple)
    start_requested = Signal()

    _READY_STATES = frozenset({"connected", "verified", "output_off"})
    _UNSAFE_STATES = frozenset({"output_on", "compliance", "fault", "unknown"})

    def __init__(
        self,
        required_devices: tuple[str, ...],
        display_names: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sweep device readiness")
        self.setModal(True)
        self.setProperty("stationSurface", "raised")
        self._required_devices = tuple(required_devices)
        self._states: dict[str, tuple[str, bool, str | None]] = {
            device: ("disconnected", False, None) for device in self._required_devices
        }
        self.rows: dict[str, SimpleCardWidget] = {}
        self._status_labels: dict[str, BodyLabel] = {}
        parent_width = parent.width() if parent is not None else 720
        self.setMinimumWidth(max(420, min(680, parent_width - 48)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        heading = SubtitleLabel("Device readiness", self)
        guidance = BodyLabel(
            "Connect and verify every device used by this sweep before starting. "
            "Connecting never enables an output.",
            self,
        )
        guidance.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(guidance)

        for device in self._required_devices:
            row = SimpleCardWidget(self)
            row.setProperty("stationSurface", "card")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(14, 10, 14, 10)
            name = BodyLabel(display_names.get(device, device), row)
            name.setAccessibleName(f"Required sweep device {device}")
            status = BodyLabel("Not connected", row)
            status.setAccessibleName(f"Sweep readiness status for {device}")
            status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(name, 1)
            row_layout.addWidget(status)
            layout.addWidget(row)
            self.rows[device] = row
            self._status_labels[device] = status

        layout.addSpacing(4)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.connect_missing_button = PushButton("Connect missing devices", self)
        self.start_button = PrimaryPushButton("Start sweep", self)
        self.cancel_button.setAccessibleName("Cancel sweep readiness")
        self.connect_missing_button.setAccessibleName("Connect missing sweep devices")
        self.start_button.setAccessibleName("Start verified sweep")
        self.cancel_button.clicked.connect(self.reject)
        self.connect_missing_button.clicked.connect(self._request_missing_connections)
        self.start_button.clicked.connect(self._request_start)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.connect_missing_button)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        self._refresh()

    def update_device(
        self,
        device: str,
        state: str,
        identity_verified: bool,
        error: str | None = None,
    ) -> None:
        """Render the latest confirmed device evidence for one required row."""

        if device not in self._states:
            return
        self._states[device] = (str(state), bool(identity_verified), error)
        self._refresh()

    @property
    def missing_devices(self) -> tuple[str, ...]:
        return tuple(
            device for device in self._required_devices if not self._device_is_ready(device)
        )

    def _device_is_ready(self, device: str) -> bool:
        state, identity_verified, _error = self._states[device]
        return identity_verified and state in self._READY_STATES

    def _refresh(self) -> None:
        for device in self._required_devices:
            state, identity_verified, error = self._states[device]
            label = self._status_labels[device]
            if error:
                text = "Connection failed"
                semantic_state = "fault"
            elif state in self._UNSAFE_STATES:
                text = f"Unsafe state: {state.replace('_', ' ')}"
                semantic_state = state
            elif self._device_is_ready(device):
                text = "Ready — identity verified"
                semantic_state = "verified"
            elif identity_verified:
                text = f"Waiting for safe state: {state.replace('_', ' ')}"
                semantic_state = state
            else:
                text = "Not connected"
                semantic_state = "disconnected"
            label.setText(text)
            label.setProperty("stationState", semantic_state)
            label.update()
        missing = self.missing_devices
        self.connect_missing_button.setEnabled(bool(missing))
        self.start_button.setEnabled(not missing)

    def _request_missing_connections(self) -> None:
        self.connect_missing_requested.emit(self.missing_devices)

    def _request_start(self) -> None:
        if not self.missing_devices:
            self.start_requested.emit()


class StationMessageBox:
    """Fluent modal prompts with the return contract of ``QMessageBox``.

    Existing callers use standard-button values to protect destructive or
    hardware-adjacent actions.  Keeping that contract at this single boundary
    lets the pages use a Fluent presentation without changing their decisions.
    """

    StandardButton = QtMessageBox.StandardButton

    @classmethod
    def information(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = StandardButton.Ok,
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton, offer_settings=True)

    @classmethod
    def warning(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = StandardButton.Ok,
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton, offer_settings=True)

    @classmethod
    def critical(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = StandardButton.Ok,
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton, offer_settings=True)

    @classmethod
    def question(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = (
            StandardButton.Yes | StandardButton.No
        ),
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton)

    @classmethod
    def _show(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton,
        default_button: QtMessageBox.StandardButton,
        *,
        offer_settings: bool = False,
    ) -> QtMessageBox.StandardButton:
        if offer_settings:
            from app.ui.settings_guidance import settings_issue_for_error

            issue = settings_issue_for_error(text)
            owner = parent
            while owner is not None and not callable(
                getattr(owner, "_open_settings_issue", None)
            ):
                owner = owner.parentWidget()
            if issue is not None and owner is not None:
                dialog = StationSettingsGuidanceDialog(parent, title, text)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    owner._open_settings_issue(issue)  # type: ignore[attr-defined]
                return cls.StandardButton.Ok
        available = tuple(
            button
            for button in (
                cls.StandardButton.Yes,
                cls.StandardButton.Ok,
                cls.StandardButton.Save,
                cls.StandardButton.No,
                cls.StandardButton.Cancel,
            )
            if buttons & button
        )
        if not available:
            available = (cls.StandardButton.Ok,)
        primary = next(
            (
                button
                for button in available
                if button not in {cls.StandardButton.No, cls.StandardButton.Cancel}
            ),
            available[0],
        )
        secondary = next((button for button in available if button != primary), None)

        dialog = StationAlertDialog(
            parent,
            title,
            text,
            primary,
            secondary,
            default_button,
        )
        dialog.exec()
        return dialog.selected_button

    @classmethod
    def _button_label(cls, button: QtMessageBox.StandardButton) -> str:
        return {
            cls.StandardButton.Ok: "OK",
            cls.StandardButton.Yes: "Yes",
            cls.StandardButton.No: "No",
            cls.StandardButton.Save: "Save",
            cls.StandardButton.Cancel: "Cancel",
        }.get(button, "OK")

    @classmethod
    def settings_guidance(cls, parent: QWidget | None, title: str, text: str) -> bool:
        """Show an actionable safety/configuration alert and return its choice."""

        dialog = StationSettingsGuidanceDialog(parent, title, text)
        return dialog.exec() == QDialog.DialogCode.Accepted

    @classmethod
    def action_guidance(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        action_label: str,
    ) -> bool:
        dialog = StationSettingsGuidanceDialog(
            parent, title, text, action_label=action_label
        )
        return dialog.exec() == QDialog.DialogCode.Accepted


class StationFileDialog(QFileDialog):
    """Force Qt-rendered file pickers so station light/dark tokens are respected."""

    @staticmethod
    def getOpenFileName(
        parent: QWidget | None = None,
        caption: str = "",
        directory: str = "",
        filter: str = "",
        selectedFilter: str = "",
        options: QFileDialog.Option = QFileDialog.Option(0),
    ) -> tuple[str, str]:
        return QFileDialog.getOpenFileName(
            parent,
            caption,
            directory,
            filter,
            selectedFilter,
            options | QFileDialog.Option.DontUseNativeDialog,
        )

    @staticmethod
    def getSaveFileName(
        parent: QWidget | None = None,
        caption: str = "",
        directory: str = "",
        filter: str = "",
        selectedFilter: str = "",
        options: QFileDialog.Option = QFileDialog.Option(0),
    ) -> tuple[str, str]:
        return QFileDialog.getSaveFileName(
            parent,
            caption,
            directory,
            filter,
            selectedFilter,
            options | QFileDialog.Option.DontUseNativeDialog,
        )

    @staticmethod
    def getExistingDirectory(
        parent: QWidget | None = None,
        caption: str = "",
        directory: str = "",
        options: QFileDialog.Option = QFileDialog.Option.ShowDirsOnly,
    ) -> str:
        return QFileDialog.getExistingDirectory(
            parent,
            caption,
            directory,
            options | QFileDialog.Option.DontUseNativeDialog,
        )
