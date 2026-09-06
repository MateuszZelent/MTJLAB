"""Shared Fluent-compatible popup infrastructure."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox as QtMessageBox,
    QDialog,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qframelesswindow import FramelessDialog
from qfluentwidgets import (
    BodyLabel,
    FluentTitleBar,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    SubtitleLabel,
)


def _blend_modal_colors(first: QColor, second: QColor, weight: float) -> QColor:
    amount = max(0.0, min(1.0, weight))
    return QColor.fromRgb(
        round(first.red() * (1.0 - amount) + second.red() * amount),
        round(first.green() * (1.0 - amount) + second.green() * amount),
        round(first.blue() * (1.0 - amount) + second.blue() * amount),
    )


class _StationModalBackdrop(QWidget):
    """Shared quiet elevation layer behind station modal surfaces."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        palette = self.palette()
        window = palette.color(QPalette.ColorRole.Window)
        base = palette.color(QPalette.ColorRole.Base)
        accent = palette.color(QPalette.ColorRole.Highlight)
        gradient = QLinearGradient(0, 0, 0, max(1, self.height()))
        gradient.setColorAt(0.0, _blend_modal_colors(window.lighter(106), accent, 0.08))
        gradient.setColorAt(0.52, window)
        gradient.setColorAt(1.0, _blend_modal_colors(base.darker(104), accent, 0.04))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(gradient)
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 16, 16)


class StationCardWidget(SimpleCardWidget):
    """Calm station card with a stable surface while the pointer moves over it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationHover", "disabled")


def _add_station_modal_shadow(widget: QWidget, *, blur: float = 32, y: float = 6) -> None:
    """Retained for API compatibility; hardware DWM provides native frameless shadow."""
    del widget, blur, y


class StationModalShell(QWidget):
    """Reusable raised Fluent surface for station dialogs and floating tools."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        outer_margins: tuple[int, int, int, int] = (10, 10, 10, 10),
        backdrop_margins: tuple[int, int, int, int] = (10, 10, 10, 10),
        surface_margins: tuple[int, int, int, int] = (16, 14, 16, 14),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("stationModalShell")
        self.setProperty("stationSurface", "raised")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(*outer_margins)
        self.outer_layout.setSpacing(0)

        self.backdrop = _StationModalBackdrop(self)
        self.backdrop.setObjectName("stationModalBackdrop")
        self.backdrop.setProperty("stationSurface", "raised")
        self.backdrop.setProperty("stationHover", "disabled")
        self.backdrop.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.backdrop.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.backdrop_layout = QVBoxLayout(self.backdrop)
        self.backdrop_layout.setContentsMargins(*backdrop_margins)
        self.backdrop_layout.setSpacing(0)
        self.outer_layout.addWidget(self.backdrop)

        self.surface = StationCardWidget(self.backdrop)
        self.surface.setObjectName("stationModalSurface")
        self.surface.setProperty("stationSurface", "card")
        self.surface.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.surface_layout = QVBoxLayout(self.surface)
        self.surface_layout.setContentsMargins(*surface_margins)
        self.surface_layout.setSpacing(8)
        # A named alias makes the intended insertion point explicit for new
        # dialogs without forcing existing subclasses to change their layout.
        self.content_layout = self.surface_layout
        self.backdrop_layout.addWidget(self.surface)
        _add_station_modal_shadow(self.surface)


class StationDialog(FramelessDialog):
    """Theme-aware host for every station-owned popup or floating window."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        resizable: bool = False,
        modal_shell_outer_margins: tuple[int, int, int, int] | None = None,
        modal_shell_backdrop_margins: tuple[int, int, int, int] | None = None,
        modal_shell_surface_margins: tuple[int, int, int, int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_resizable = False
        self.setTitleBar(FluentTitleBar(self))
        self.titleBar.minBtn.hide()
        self.titleBar.maxBtn.hide()
        self.titleBar.setDoubleClickEnabled(False)
        self.titleBar.closeBtn.clicked.disconnect()
        self.titleBar.closeBtn.clicked.connect(self.close)
        self.setProperty("stationSurface", "page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        modal_shell_kwargs: dict[str, tuple[int, int, int, int]] = {}
        if modal_shell_outer_margins is not None:
            modal_shell_kwargs["outer_margins"] = modal_shell_outer_margins
        if modal_shell_backdrop_margins is not None:
            modal_shell_kwargs["backdrop_margins"] = modal_shell_backdrop_margins
        if modal_shell_surface_margins is not None:
            modal_shell_kwargs["surface_margins"] = modal_shell_surface_margins
        self.modal_shell = StationModalShell(self, **modal_shell_kwargs)
        self._modal_shell_is_content = False
        self._position_modal_shell()
        if resizable:
            self.set_resizable(True)

    def set_resizable(self, resizable: bool = True) -> None:
        """Enable or disable window controls, double-click maximization, and resize borders."""

        self._is_resizable = resizable
        if resizable:
            self.titleBar.minBtn.show()
            self.titleBar.maxBtn.show()
            self.titleBar.setDoubleClickEnabled(True)
            self.setResizeEnabled(True)
            self._enable_platform_window_controls()
        else:
            self.titleBar.minBtn.hide()
            self.titleBar.maxBtn.hide()
            self.titleBar.setDoubleClickEnabled(False)
            self.setResizeEnabled(False)

    def _enable_platform_window_controls(self) -> None:
        import sys

        if sys.platform == "win32":
            try:
                import win32con
                import win32gui

                hwnd = int(self.winId())
                if hwnd:
                    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                    win32gui.SetWindowLong(
                        hwnd,
                        win32con.GWL_STYLE,
                        style
                        | win32con.WS_MAXIMIZEBOX
                        | win32con.WS_MINIMIZEBOX
                        | win32con.WS_THICKFRAME,
                    )
            except Exception:
                pass

    def use_modal_shell_content(self) -> StationModalShell:
        """Raise the shared shell so a migrated dialog can own its content."""

        self._modal_shell_is_content = True
        self.modal_shell.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._position_modal_shell()
        return self.modal_shell

    def modal_content_layout(self, *, spacing: int | None = None) -> QVBoxLayout:
        """Return the supported content layout for station-owned modals."""

        layout = self.use_modal_shell_content().surface_layout
        if spacing is not None:
            layout.setSpacing(spacing)
        return layout

    def _update_modal_shell_geometry(self) -> None:
        inset = 8
        title_bar_height = self.titleBar.height() if hasattr(self, "titleBar") else 0
        top_inset = max(inset, title_bar_height + 6)
        self.modal_shell.setGeometry(
            self.rect().adjusted(inset, top_inset, -inset, -inset)
        )

    def _update_modal_shell_stacking(self) -> None:
        if self._modal_shell_is_content:
            self.modal_shell.raise_()
        else:
            self.modal_shell.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )
            self.modal_shell.lower()

    def _position_modal_shell(self) -> None:
        self._update_modal_shell_geometry()
        self._update_modal_shell_stacking()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if getattr(self, "_is_resizable", False):
            self._enable_platform_window_controls()
        self._position_modal_shell()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_modal_shell_geometry()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        }:
            from qfluentwidgets import isDarkTheme
            from app.ui.widgets.spectrum_plot import SpectrumPlotWidget

            theme = "dark" if isDarkTheme() else "light"
            for plot in self.findChildren(SpectrumPlotWidget):
                plot.apply_theme(theme)
            self.update()
            for child in self.findChildren(QWidget):
                child.update()

    def nativeEvent(self, event_type, message) -> tuple[bool, int]:  # noqa: N802 - Qt override
        import sys
        if sys.platform == "win32":
            try:
                import win32con
                from qframelesswindow.windows import MSG, LPNCCALCSIZE_PARAMS, cast
                msg = MSG.from_address(message.__int__())
                if msg.message == win32con.WM_NCCALCSIZE:
                    handled, result = super().nativeEvent(event_type, message)
                    if msg.wParam and handled:
                        params = cast(msg.lParam, LPNCCALCSIZE_PARAMS).contents
                        new_w = params.rgrc[0].right - params.rgrc[0].left
                        new_h = params.rgrc[0].bottom - params.rgrc[0].top
                        old_w = params.rgrc[1].right - params.rgrc[1].left
                        old_h = params.rgrc[1].bottom - params.rgrc[1].top
                        if new_w == old_w and new_h == old_h:
                            return True, 0
                    return handled, result
            except Exception:
                pass
        return super().nativeEvent(event_type, message)


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
        surface = self.use_modal_shell_content().surface

        self.title_label = SubtitleLabel(title, surface)
        self.title_label.setWordWrap(True)
        self.content_label = BodyLabel(text, surface)
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        self.primary_button = PrimaryPushButton(
            StationMessageBox._button_label(primary), surface
        )
        self.primary_button.setMinimumWidth(120)
        self.primary_button.clicked.connect(lambda: self._finish(primary, True))
        self.secondary_button: PushButton | None = None
        if secondary is not None:
            self.secondary_button = PushButton(
                StationMessageBox._button_label(secondary), surface
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

        layout = self.modal_content_layout(spacing=12)
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
        surface = self.use_modal_shell_content().surface

        layout = self.modal_content_layout(spacing=12)
        heading = SubtitleLabel(title, surface)
        body = BodyLabel(text, surface)
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
        close = PushButton("Close", surface)
        self.go_to_settings_button = PrimaryPushButton(action_label, surface)
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
        *,
        additional_safety_devices: tuple[str, ...] = (),
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

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=12)
        heading = SubtitleLabel("Device readiness", surface)
        guidance = BodyLabel(
            "Connect and verify every device used by this sweep before starting. "
            "Connecting never enables an output.",
            surface,
        )
        guidance.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(guidance)
        self.safety_guidance = BodyLabel(parent=surface)
        self.safety_guidance.setObjectName("muted")
        self.safety_guidance.setWordWrap(True)
        if additional_safety_devices:
            names = ", ".join(
                display_names.get(device, device) for device in additional_safety_devices
            )
            self.safety_guidance.setText(
                "Normal measurement also prepares safe shutdown for: "
                f"{names}. These devices are informational here and are not "
                "additional sweep prerequisites."
            )
            layout.addWidget(self.safety_guidance)
        else:
            self.safety_guidance.hide()

        for device in self._required_devices:
            row = SimpleCardWidget(surface)
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
        self.cancel_button = PushButton("Cancel", surface)
        self.connect_missing_button = PushButton("Connect missing devices", surface)
        self.start_button = PrimaryPushButton("Start sweep", surface)
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

    @property
    def connectable_devices(self) -> tuple[str, ...]:
        return tuple(
            device
            for device in self.missing_devices
            if self._states[device][0] not in self._UNSAFE_STATES
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
        self.connect_missing_button.setEnabled(bool(self.connectable_devices))
        self.start_button.setEnabled(not missing)

    def _request_missing_connections(self) -> None:
        self.connect_missing_requested.emit(self.connectable_devices)

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
