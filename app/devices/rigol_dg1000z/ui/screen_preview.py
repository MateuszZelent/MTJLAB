"""Read-only front-panel image, using the existing instrument owner thread."""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, PushButton, SwitchButton


class _ScreenCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.setMinimumSize(160, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Rigol front-panel screen")
        layout = QVBoxLayout(self)
        self.placeholder = BodyLabel("No screen captured", self)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.placeholder)

    def sizeHint(self):
        return QSize(320, 240)

    def set_image(self, image):
        self.image = image
        self.placeholder.setVisible(image.isNull())
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.image.isNull():
            return
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                        size.width(), size.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self.image)


class RigolScreenPreview(CardWidget):
    """Single-flight polling; display requests never configure the instrument."""

    POLL_INTERVAL_MS = 1000
    CONTROL_QUIET_MS = 120
    TIMEOUT_RETRY_MS = 2000

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("rigolScreenPreview")
        self._controller = controller
        self._connected = False
        self._execution = False
        self._reserved = False
        self._live_control = False
        self._control_busy = False
        self._refresh_due = False
        self._pending = False
        self._generation = 0
        self._pending_generation = None
        self._captured_at = ""
        self._error_text = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._request_live_frame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        toolbar = QHBoxLayout()
        self.live_switch = SwitchButton(self)
        self.live_switch.setOnText("Live")
        self.live_switch.setOffText("Live")
        self.live_switch.setAccessibleName("Live Rigol screen preview")
        self.live_switch.setToolTip("Refresh the instrument screen about once per second when controls are idle.")
        self.refresh_button = PushButton("Refresh", self)
        self.refresh_button.setAccessibleName("Refresh Rigol screen")
        toolbar.addWidget(self.live_switch)
        toolbar.addStretch(1)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.image_view = _ScreenCanvas(self)
        layout.addWidget(self.image_view, 1)
        self.timestamp_label = CaptionLabel("Actual instrument screen · read-only", self)
        self.timestamp_label.setObjectName("muted")
        self.timestamp_label.setWordWrap(True)
        layout.addWidget(self.timestamp_label)
        self.status_label = CaptionLabel(self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.refresh_button.clicked.connect(self.request_frame)
        self.live_switch.checkedChanged.connect(self._live_changed)
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._state_changed)
        controller.reservation_changed.connect(self.set_reserved)
        self._sync_controls()

    @property
    def has_frame(self):
        return not self.image_view.image.isNull()

    def _available(self):
        return self._connected and not self._execution and not self._reserved

    def _sync_controls(self):
        self.live_switch.setEnabled(self._available())
        self.refresh_button.setEnabled(self._available() and not self._pending and not self._control_busy)
        if not self._connected:
            text = "Connect Rigol to view its screen."
        elif self._execution:
            text = "Paused while the sweep controls Rigol."
        elif self._reserved:
            text = "Paused while Rigol is reserved by another operation."
        elif self._control_busy:
            text = "Waiting for Rigol control commands to finish."
        elif self._error_text:
            text = self._error_text
        elif self._pending:
            text = "Reading instrument screen…"
        elif self.live_switch.isChecked():
            text = "Live · refreshes after changes and about once per second while idle"
        elif self._live_control:
            text = "Refreshes after applied live changes."
        elif self.has_frame:
            text = "Paused · last captured screen"
        else:
            text = "Select Refresh or turn on Live."
        self.status_label.setText(text)

    def _automatic_capture_due(self):
        return self.live_switch.isChecked() or (self._live_control and self._refresh_due)

    def _schedule(self, delay=None):
        self._timer.stop()
        if (self.isVisible() and self._available() and not self._pending
                and not self._control_busy and self._automatic_capture_due()):
            if delay is None:
                delay = self.CONTROL_QUIET_MS if self._refresh_due else 0
            self._timer.start(delay)

    def set_live_control_enabled(self, enabled):
        self._live_control = enabled
        self._sync_controls()
        self._schedule()

    def set_control_activity(self, busy, *, applied=False):
        changed = busy != self._control_busy
        self._control_busy = busy
        if applied and (self.live_switch.isChecked() or self._live_control):
            self._refresh_due = True
        if changed or applied:
            self._sync_controls()
            self._schedule(None if self._refresh_due else self.POLL_INTERVAL_MS)

    def _request_live_frame(self):
        if self._automatic_capture_due():
            self.request_frame()

    def _live_changed(self, checked):
        if checked:
            self._error_text = ""
        self._sync_controls()
        self._schedule()

    def _state_changed(self, state):
        connected = str(state).lower() in {"connected", "verified", "output_off", "output_on"}
        if connected == self._connected:
            return
        self._connected = connected
        self._generation += 1
        self._refresh_due = False
        self._error_text = ""
        self._captured_at = ""
        self.image_view.set_image(QImage())
        self.timestamp_label.setText("Actual instrument screen · read-only")
        self._sync_controls()
        self._schedule()

    def set_execution_controlled(self, controlled):
        self._set_pause("_execution", controlled)

    def set_reserved(self, reserved):
        self._set_pause("_reserved", reserved)

    def _set_pause(self, name, value):
        if getattr(self, name) == bool(value):
            return
        setattr(self, name, bool(value))
        self._generation += 1
        self._sync_controls()
        self._schedule()

    def request_frame(self):
        if not self.isVisible() or not self._available() or self._pending or self._control_busy:
            return
        self._timer.stop()
        self._refresh_due = False
        self._pending = True
        self._pending_generation = self._generation
        self._error_text = ""
        self._sync_controls()
        # Set pending first: GUI admission guards can fail synchronously.
        self._controller.call("capture_screen")

    def _result(self, operation, result):
        if operation != "capture_screen" or not self._pending:
            return
        self._pending = False
        if self._pending_generation != self._generation:
            self._sync_controls()
            self._schedule()
            return
        image = QImage.fromData(result, "BMP") if isinstance(result, bytes) else QImage()
        if image.isNull() or image.size() != QSize(320, 240):
            self._capture_failed("Could not display the Rigol screen image.")
            return
        self.image_view.set_image(image)
        self._captured_at = datetime.now(UTC).astimezone().strftime("%H:%M:%S")
        self.timestamp_label.setText(f"Captured {self._captured_at} · 320 × 240 · read-only")
        self._sync_controls()
        self._schedule(None if self._refresh_due else self.POLL_INTERVAL_MS)

    def _error(self, operation, error):
        if operation != "capture_screen" or not self._pending:
            return
        self._pending = False
        if self._pending_generation != self._generation:
            self._sync_controls()
            self._schedule()
            return
        message = error.strip() or "Could not read the Rigol screen."
        if self.live_switch.isChecked() and ("timeout" in message.lower() or "VI_ERROR_TMO" in message):
            self._error_text = f"{message} · Retrying when Rigol is idle."
            self._sync_controls()
            self._schedule(self.TIMEOUT_RETRY_MS)
        else:
            self._capture_failed(message)

    def _capture_failed(self, message):
        self._timer.stop()
        self._error_text = message
        self.live_switch.setChecked(False)
        self._sync_controls()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)
