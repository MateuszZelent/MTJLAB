"""Fluent floating quick controls for peripheral setpoints."""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal

from PySide6.QtCore import QObject, QSettings, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QDialog, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from app.domain.quick_controls import (
    QuickControlCommand,
    QuickSetpoint,
    render_quantity_si_like,
    step_quantity_text,
)
from app.domain.quantities import format_quantity_auto, parse_quantity
from app.recipes.parameter_registry import (
    QUICK_CONTROLS_BY_TARGET,
    QUICK_CONTROL_DESCRIPTORS,
    QuickControlDescriptor,
)
from app.safety.quick_controls import quick_control_safety_bounds
from app.settings.models import StationSettings
from app.ui.dialogs import StationDialog
from app.ui.workers import DeviceController


class QuickControlCoordinator(QObject):
    state_changed = Signal(str, str, str)
    value_read = Signal(str, float)
    bounds_changed = Signal()

    def __init__(
        self,
        controllers: dict[str, DeviceController],
        parent: QObject,
        *,
        settings: StationSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self._controllers = controllers
        self._bounds: dict[str, tuple[float, float]] = {}
        self._bound_texts: dict[str, tuple[str, str]] = {}
        if settings is not None:
            self.set_settings(settings)
        self._sequence = 0
        self._inflight: dict[str, QuickSetpoint | None] = {
            "rigol": None,
            "keithley": None,
        }
        self._pending: dict[str, OrderedDict[str, QuickSetpoint]] = {
            "rigol": OrderedDict(),
            "keithley": OrderedDict(),
        }
        for device in self._inflight:
            controller = controllers[device]
            controller.result.connect(
                lambda operation, result, name=device: self._result(
                    name, operation, result
                )
            )
            controller.error.connect(
                lambda operation, error, name=device: self._error(
                    name, operation, error
                )
            )
            controller.state_changed.connect(
                lambda state, name=device: self._device_state(name, state)
            )

    def submit(self, target: str, text: str) -> None:
        descriptor = QUICK_CONTROLS_BY_TARGET[target]
        value_si = parse_quantity(text, descriptor.dimension).si_value
        _bounded, limited, detail = self.bound_value(target, value_si)
        if limited:
            self.state_changed.emit(target, "rejected", detail)
            self._controllers[descriptor.device_module].call("quick_readback")
            return
        self._sequence += 1
        request = QuickSetpoint(target, text, value_si, self._sequence)
        device = descriptor.device_module
        self._pending[device][target] = request
        self.state_changed.emit(target, "pending", "Waiting for device readback")
        self._send_next(device)

    def set_settings(self, settings: StationSettings) -> None:
        """Refresh UI preflight bounds from the active station configuration."""

        resolved = quick_control_safety_bounds(settings)
        self._bounds = {
            target: (bound.minimum_si, bound.maximum_si)
            for target, bound in resolved.items()
        }
        self._bound_texts = {
            target: (bound.minimum_text, bound.maximum_text)
            for target, bound in resolved.items()
        }
        self.bounds_changed.emit()

    def bound_texts(self, target: str) -> tuple[str, str] | None:
        return self._bound_texts.get(target)

    def bound_value(self, target: str, value_si: float) -> tuple[float, bool, str]:
        """Clamp arrow stepping while typed out-of-range input remains rejected."""

        try:
            lower, upper = self._bounds[target]
        except KeyError:
            return value_si, False, ""
        if value_si < lower:
            return lower, True, f"Minimum safety limit reached ({lower:.12g} SI)"
        if value_si > upper:
            return upper, True, f"Maximum safety limit reached ({upper:.12g} SI)"
        return value_si, False, ""

    def refresh(self) -> None:
        for device in self._inflight:
            self._controllers[device].call("quick_readback")

    def cancel_all(self, reason: str = "Cancelled") -> None:
        for device, pending in self._pending.items():
            for target in tuple(pending):
                self.state_changed.emit(target, "unknown", reason)
            pending.clear()

    def _send_next(self, device: str) -> None:
        if self._inflight[device] is not None or not self._pending[device]:
            return
        _target, request = self._pending[device].popitem(last=False)
        self._inflight[device] = request
        self.state_changed.emit(request.target, "applying", "Applying...")
        self._controllers[device].call(
            "quick_setpoint", QuickControlCommand(request.target, request.text)
        )

    def _result(self, device: str, operation: str, result: object) -> None:
        if operation == "quick_readback" and isinstance(result, dict):
            for target, value in result.items():
                if str(target) in QUICK_CONTROLS_BY_TARGET:
                    self.value_read.emit(str(target), float(value))
                    self.state_changed.emit(
                        str(target), "ready", "Verified device configuration"
                    )
            return
        if operation != "quick_setpoint":
            return
        request = self._inflight[device]
        self._inflight[device] = None
        if request is not None:
            self.value_read.emit(request.target, float(result))
            self.state_changed.emit(
                request.target,
                "applied",
                f"Applied {request.text} · readback {float(result):.12g} SI",
            )
        self._send_next(device)

    def _error(self, device: str, operation: str, error: str) -> None:
        if operation != "quick_setpoint":
            return
        request = self._inflight[device]
        self._inflight[device] = None
        if request is not None:
            self.state_changed.emit(request.target, "rejected", error)
        self._controllers[device].call("quick_readback")
        self._send_next(device)

    def _device_state(self, device: str, state: str) -> None:
        if state in {"disconnected", "fault", "unknown"}:
            pending = self._pending[device]
            for target in tuple(pending):
                self.state_changed.emit(target, "unknown", f"Device {state}")
            pending.clear()


class QuantityStepEdit(LineEdit):
    step_requested = Signal(int, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Quick controls already provide a precision-aware stepper that also
        # clamps against the live safety envelope.  Keep that guarded path in
        # charge instead of allowing the generic text-field stepper to bypass
        # it before this widget receives the key event.
        self.setProperty("precisionArrowStepping", False)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down}:
            multiplier = Decimal(1)
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                multiplier = Decimal(10)
            elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                multiplier = Decimal("0.1")
            self.step_requested.emit(
                1 if event.key() == Qt.Key.Key_Up else -1, multiplier
            )
            event.accept()
            return
        super().keyPressEvent(event)


class QuickControlRow(QWidget):
    submit_requested = Signal(str, str)
    move_requested = Signal(str, int)

    def __init__(
        self,
        descriptor: QuickControlDescriptor,
        coordinator: QuickControlCoordinator,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.descriptor = descriptor
        self._coordinator = coordinator
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        header = QHBoxLayout()
        title = StrongBodyLabel(descriptor.label, self)
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(title, 1)
        move_up = PushButton("↑", self)
        move_down = PushButton("↓", self)
        for button in (move_up, move_down):
            button.setFixedSize(30, 26)
        move_up.setToolTip("Move this control up")
        move_down.setToolTip("Move this control down")
        move_up.clicked.connect(lambda: self.move_requested.emit(descriptor.target, -1))
        move_down.clicked.connect(lambda: self.move_requested.emit(descriptor.target, 1))
        header.addWidget(move_up)
        header.addWidget(move_down)
        layout.addLayout(header)
        controls = QHBoxLayout()
        self.decrease = PushButton("−", self)
        self.value = QuantityStepEdit(self)
        self.value.setText(descriptor.default_text)
        self.increase = PushButton("+", self)
        self.decrease.setFixedWidth(38)
        self.increase.setFixedWidth(38)
        for button in (self.decrease, self.increase):
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(350)
            button.setAutoRepeatInterval(60)
        controls.addWidget(self.decrease)
        controls.addWidget(self.value, 1)
        controls.addWidget(self.increase)
        layout.addLayout(controls)
        self.limits = CaptionLabel("Safety limits unavailable", self)
        self.limits.setObjectName("quickControlLimits")
        layout.addWidget(self.limits)
        self.refresh_limits()
        self.status = CaptionLabel("Ready · output state is never changed", self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.decrease.clicked.connect(lambda: self.step(-1, Decimal(1)))
        self.increase.clicked.connect(lambda: self.step(1, Decimal(1)))
        self.value.step_requested.connect(self.step)
        self._typing_timer = QTimer(self)
        self._typing_timer.setSingleShot(True)
        self._typing_timer.setInterval(150)
        self._typing_timer.timeout.connect(self.submit)
        self.value.textEdited.connect(lambda _text: self._typing_timer.start())
        self.value.editingFinished.connect(self.submit)

    def refresh_limits(self) -> None:
        bounds = self._coordinator.bound_texts(self.descriptor.target)
        if bounds is None:
            self.limits.setText("Safety limits unavailable")
            return
        self.limits.setText(f"MIN  {bounds[0]}    MAX  {bounds[1]}")

    def step(self, direction: int, multiplier: object) -> None:
        try:
            previous_si = parse_quantity(
                self.value.text(), self.descriptor.dimension
            ).si_value
            text, value_si = step_quantity_text(
                self.value.text(),
                self.descriptor.dimension,
                direction,
                multiplier=Decimal(multiplier),
            )
            bounded_si, limited, detail = self._coordinator.bound_value(
                self.descriptor.target, value_si
            )
            if limited:
                text = render_quantity_si_like(
                    self.value.text(), self.descriptor.dimension, bounded_si
                )
        except ValueError as exc:
            self.set_state("rejected", str(exc))
            return
        self.value.setText(text)
        if limited:
            self.set_state("limit", detail)
            if previous_si == bounded_si:
                return
        self.submit()

    def submit(self) -> None:
        self._typing_timer.stop()
        try:
            parse_quantity(self.value.text(), self.descriptor.dimension)
        except ValueError as exc:
            self.set_state("rejected", str(exc))
            return
        self.submit_requested.emit(self.descriptor.target, self.value.text())

    def set_state(self, state: str, detail: str) -> None:
        self.setProperty("quickState", state)
        self.status.setText(f"{state.title()} · {detail}")
        self.style().unpolish(self)
        self.style().polish(self)


class QuickControlPicker(StationDialog):
    def __init__(self, selected: tuple[str, ...], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose quick controls")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        intro = BodyLabel(
            "Select Rigol and Keithley setpoints to keep visible beside the spectrum."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.checkboxes: dict[str, CheckBox] = {}
        for descriptor in QUICK_CONTROL_DESCRIPTORS:
            checkbox = CheckBox(descriptor.label, self)
            checkbox.setChecked(descriptor.target in selected)
            layout.addWidget(checkbox)
            self.checkboxes[descriptor.target] = checkbox
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", self)
        apply = PrimaryPushButton("Show selected", self)
        cancel.clicked.connect(self.reject)
        apply.clicked.connect(self.accept)
        footer.addWidget(cancel)
        footer.addWidget(apply)
        layout.addLayout(footer)

    def selected_targets(self) -> tuple[str, ...]:
        return tuple(
            target for target, checkbox in self.checkboxes.items() if checkbox.isChecked()
        )


class QuickControlsWindow(StationDialog):
    output_requested = Signal(str, str, bool)

    def __init__(self, coordinator: QuickControlCoordinator, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Quick peripheral controls")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(430, 600)
        self.setMinimumSize(360, 280)
        self._coordinator = coordinator
        self._rows: dict[str, QuickControlRow] = {}
        self._selected: tuple[str, ...] = ()
        self.layout_root = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Peripheral setpoints", self))
        header.addStretch(1)
        self.choose = PushButton("Choose...", self)
        header.addWidget(self.choose)
        self.layout_root.addLayout(header)
        self._build_output_controls()
        self.content_host = QWidget(self)
        self.content_host.setMinimumWidth(0)
        self.content = QVBoxLayout(self.content_host)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(6)
        self.controls_scroll = ScrollArea(self)
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.controls_scroll.setWidget(self.content_host)
        self.layout_root.addWidget(self.controls_scroll, 1)
        self.choose.clicked.connect(self.choose_controls)
        coordinator.state_changed.connect(self._state_changed)
        coordinator.value_read.connect(self._value_read)
        coordinator.bounds_changed.connect(self._refresh_limits)

    def _build_output_controls(self) -> None:
        """Expose deliberate output requests without bypassing device pages."""

        card = CardWidget(self)
        card.setObjectName("quickControlsOutputCard")
        card.setProperty("stationSurface", "card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        title = StrongBodyLabel("Output control", card)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        note = CaptionLabel(
            "ON uses the same safety validation and hardware readback as the "
            "full device page. OFF requests a confirmed safe state.",
            card,
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        for device, label, channels in (
            ("rigol", "Rigol DG1022Z", ("1", "2")),
            ("keithley", "Keithley 2600", ("A", "B")),
        ):
            row = QHBoxLayout()
            row.addWidget(BodyLabel(label, card))
            channel = ComboBox(card)
            channel.addItems(channels)
            channel.setAccessibleName(f"{label} output channel")
            channel.setToolTip("Choose the physical channel for this output request.")
            row.addWidget(channel)
            on = PrimaryPushButton("OUTPUT ON", card)
            on.setObjectName("outputOnButton")
            on.setToolTip(
                "Request OUTPUT ON through the complete safety and readback workflow."
            )
            on.setAccessibleName(f"{label} selected channel output on")
            off = PushButton("OUTPUT OFF", card)
            off.setObjectName("outputOffButton")
            off.setToolTip(
                "Request OUTPUT OFF and verify the selected channel's readback."
            )
            off.setAccessibleName(f"{label} selected channel output off")
            on.clicked.connect(
                lambda _checked=False, name=device, selector=channel: self.output_requested.emit(
                    name, selector.currentText(), True
                )
            )
            off.clicked.connect(
                lambda _checked=False, name=device, selector=channel: self.output_requested.emit(
                    name, selector.currentText(), False
                )
            )
            row.addWidget(on)
            row.addWidget(off)
            row.addStretch(1)
            layout.addLayout(row)
        self.layout_root.addWidget(card)

    def restore_workspace(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        selected = settings.value("quick_controls/targets", (), list)
        valid = tuple(str(item) for item in selected if str(item) in QUICK_CONTROLS_BY_TARGET)
        self.set_targets(valid)
        geometry = settings.value("quick_controls/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def set_targets(self, targets: tuple[str, ...]) -> None:
        previous_values = {
            target: row.value.text() for target, row in self._rows.items()
        }
        self._selected = tuple(dict.fromkeys(targets))
        for row in self._rows.values():
            self.content.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for target in self._selected:
            descriptor = QUICK_CONTROLS_BY_TARGET[target]
            row = QuickControlRow(descriptor, self._coordinator, self)
            if target in previous_values:
                row.value.setText(previous_values[target])
            row.submit_requested.connect(self._coordinator.submit)
            row.move_requested.connect(self._move_control)
            self.content.addWidget(row)
            self._rows[target] = row
        QSettings("LabControl", "LabControl").setValue(
            "quick_controls/targets", list(self._selected)
        )

    def _move_control(self, target: str, direction: int) -> None:
        items = list(self._selected)
        if target not in items:
            return
        current = items.index(target)
        destination = max(0, min(len(items) - 1, current + direction))
        if destination == current:
            return
        items[current], items[destination] = items[destination], items[current]
        self.set_targets(tuple(items))

    def choose_controls(self) -> None:
        picker = QuickControlPicker(self._selected, self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.set_targets(picker.selected_targets())

    def _state_changed(self, target: str, state: str, detail: str) -> None:
        row = self._rows.get(target)
        if row is not None:
            row.set_state(state, detail)

    def _value_read(self, target: str, value_si: float) -> None:
        row = self._rows.get(target)
        if row is None:
            return
        descriptor = QUICK_CONTROLS_BY_TARGET[target]
        row.value.setText(format_quantity_auto(value_si, descriptor.dimension))

    def _refresh_limits(self) -> None:
        for row in self._rows.values():
            row.refresh_limits()

    def closeEvent(self, event: QCloseEvent) -> None:
        QSettings("LabControl", "LabControl").setValue(
            "quick_controls/geometry", self.saveGeometry()
        )
        super().closeEvent(event)
