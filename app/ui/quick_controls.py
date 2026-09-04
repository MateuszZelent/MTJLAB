"""Fluent floating quick controls for peripheral setpoints."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from decimal import Decimal
import math

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSettings,
    QSize,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPalette,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    FluentWidget,
    InfoBadge,
    InfoLevel,
    LineEdit,
    PrimaryToolButton,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    ToolButton,
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
from app.safety.quick_controls import QuickControlSafetyBound, quick_control_safety_bounds
from app.settings.models import StationSettings
from app.ui.dialogs import StationCardWidget, StationDialog, StationModalShell
from app.ui.design_system.tokens import tokens_for
from app.ui.widgets.quick_quantity_slider import QuickQuantitySlider
from app.ui.workers import DeviceController


QUICK_OUTPUT_DESCRIPTORS: tuple[tuple[str, str], ...] = (
    ("output.rigol.1", "Rigol DG1022Z · channel 1 output"),
    ("output.rigol.2", "Rigol DG1022Z · channel 2 output"),
    ("output.keithley.A", "Keithley 2600 · channel A output"),
    ("output.keithley.B", "Keithley 2600 · channel B output"),
    ("output.keithley.group", "Keithley 2600 · A+B group action"),
)
QUICK_OUTPUT_TARGETS = tuple(target for target, _label in QUICK_OUTPUT_DESCRIPTORS)
QUICK_OUTPUT_LABELS = dict(QUICK_OUTPUT_DESCRIPTORS)


QuickHardwareRequestBuilder = Callable[
    [QuickSetpoint, str], tuple[str, object]
]


def _output_target(device: str, channel: str) -> str:
    return f"output.{device}.{channel}"


class _QuickOutputBody(QWidget):
    """Responsive output matrix: two columns when wide, one when narrow."""

    def __init__(self, on_resize: Callable[[int], None], parent: QWidget) -> None:
        super().__init__(parent)
        self._on_resize = on_resize

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._on_resize(self.width())


class _QuickResizeHandle(QWidget):
    """Small transparent edge target used when native frameless hit-testing is absent."""

    def __init__(
        self,
        name: str,
        edges: frozenset[str],
        cursor: Qt.CursorShape,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.edges = edges
        self.setObjectName(f"quickControlsResizeHandle_{name}")
        self.setCursor(cursor)
        self.setToolTip("Drag the edge to resize Quick Controls")
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._press_global: QPoint | None = None
        self._start_geometry: QRect | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._start_geometry = QRect(self.window().geometry())
            self.window().activateWindow()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._press_global is not None:
            self._resize_from_delta(event.globalPosition().toPoint() - self._press_global)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = None
            self._start_geometry = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _resize_from_delta(self, delta: QPoint) -> None:
        window = self.window()
        start = QRect(self._start_geometry or window.geometry())
        rect = QRect(start)
        minimum_width = window.minimumWidth()
        minimum_height = window.minimumHeight()
        maximum_width = window.maximumWidth()
        maximum_height = window.maximumHeight()
        if "left" in self.edges:
            new_left = min(start.left() + delta.x(), start.right() - minimum_width + 1)
            if maximum_width < 16_777_215:
                new_left = max(new_left, start.right() - maximum_width + 1)
            rect.setLeft(new_left)
        if "right" in self.edges:
            new_right = max(start.right() + delta.x(), start.left() + minimum_width - 1)
            if maximum_width < 16_777_215:
                new_right = min(new_right, start.left() + maximum_width - 1)
            rect.setRight(new_right)
        if "top" in self.edges:
            new_top = min(start.top() + delta.y(), start.bottom() - minimum_height + 1)
            if maximum_height < 16_777_215:
                new_top = max(new_top, start.bottom() - maximum_height + 1)
            rect.setTop(new_top)
        if "bottom" in self.edges:
            new_bottom = max(start.bottom() + delta.y(), start.top() + minimum_height - 1)
            if maximum_height < 16_777_215:
                new_bottom = min(new_bottom, start.top() + maximum_height - 1)
            rect.setBottom(new_bottom)
        window.setGeometry(rect)


def _add_quick_shadow(widget: QWidget, *, blur: float = 28, y: float = 5) -> None:
    """Retained for API compatibility; hardware DWM provides native frameless shadow."""
    del widget, blur, y


class QuickControlCoordinator(QObject):
    state_changed = Signal(str, str, str)
    value_read = Signal(str, float)
    configuration_verified = Signal(str, object)
    bounds_changed = Signal()
    draft_changed = Signal(str, str, str)
    confirmed_changed = Signal(str, float)

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
        self._bound_objects: dict[str, QuickControlSafetyBound] = {}
        self._draft_texts: dict[str, str] = {
            descriptor.target: descriptor.default_text
            for descriptor in QUICK_CONTROL_DESCRIPTORS
        }
        self._confirmed_values: dict[str, float] = {}
        self._dirty_drafts: set[str] = set()
        self._adopt_readback_targets: dict[str, set[str]] = {
            "rigol": set(),
            "keithley": set(),
        }
        self._device_states: dict[str, str] = {
            device: str(getattr(controllers[device], "state_value", "disconnected"))
            .strip()
            .lower()
            for device in ("rigol", "keithley")
        }
        self._hardware_request_builders: dict[str, QuickHardwareRequestBuilder] = {}
        if settings is not None:
            self.set_settings(settings)
        self._sequence = 0
        self._inflight: dict[str, QuickSetpoint | None] = {
            "rigol": None,
            "keithley": None,
        }
        self._inflight_operations: dict[str, str | None] = {
            "rigol": None,
            "keithley": None,
        }
        self._inflight_payloads: dict[str, object | None] = {
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

    def set_hardware_request_builder(
        self, device: str, builder: QuickHardwareRequestBuilder | None
    ) -> None:
        """Register the device-specific OFF/ON dispatch policy.

        The coordinator owns the shared draft and queue, while a device page
        supplies the complete, already validated configuration needed for a
        non-energising ``output_off`` transaction.  When no builder is
        registered the legacy quick-setpoint payload remains the default.
        """

        if device not in self._controllers:
            raise KeyError(f"Unknown quick-control device {device!r}.")
        if builder is None:
            self._hardware_request_builders.pop(device, None)
        else:
            self._hardware_request_builders[device] = builder

    def submit(self, target: str, text: str) -> None:
        descriptor = QUICK_CONTROLS_BY_TARGET[target]
        value_si = parse_quantity(text, descriptor.dimension).si_value
        _bounded, limited, detail = self.bound_value(target, value_si)
        if limited:
            self.state_changed.emit(target, "rejected", detail)
            return
        self.publish_draft(target, text, source="quick_controls")
        device = descriptor.device_module
        if not self._device_can_apply(device):
            self.state_changed.emit(
                target,
                "draft",
                f"Device {device} is not connected with a confirmed output state; "
                "the value remains a local draft.",
            )
            return
        self._sequence += 1
        request = QuickSetpoint(target, text, value_si, self._sequence)
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
        self._bound_objects = resolved
        for targets in self._adopt_readback_targets.values():
            targets.clear()
        self.bounds_changed.emit()

    def draft_text(self, target: str) -> str | None:
        return self._draft_texts.get(target)

    def publish_draft(
        self, target: str, text: str, *, source: str = "device_card"
    ) -> bool:
        """Publish a valid UI draft without communicating with hardware."""

        descriptor = QUICK_CONTROLS_BY_TARGET.get(target)
        if descriptor is None:
            raise KeyError(f"Unknown quick-control target {target!r}.")
        parse_quantity(text, descriptor.dimension)
        if self._draft_texts.get(target) == text and target in self._dirty_drafts:
            return True
        self._draft_texts[target] = text
        self._dirty_drafts.add(target)
        self.draft_changed.emit(target, text, source)
        return True

    def publish_draft_snapshot(
        self,
        values: Mapping[str, str | float],
        *,
        source: str = "device_card",
        mark_dirty: bool = True,
    ) -> None:
        """Publish a page snapshot using explicit-unit text at the boundary."""

        for target, value in values.items():
            descriptor = QUICK_CONTROLS_BY_TARGET[target]
            text = (
                value
                if isinstance(value, str)
                else format_quantity_auto(float(value), descriptor.dimension)
            )
            if mark_dirty:
                self.publish_draft(target, text, source=source)
            else:
                parse_quantity(text, descriptor.dimension)
                self._draft_texts[target] = text
                self._dirty_drafts.discard(target)
                self.draft_changed.emit(target, text, source)

    def confirmed_snapshot(
        self, target: str, value_si: float, *, adopt_draft: bool = False
    ) -> None:
        """Store readback and optionally make it the shared visible draft."""

        descriptor = QUICK_CONTROLS_BY_TARGET[target]
        if not math.isfinite(value_si):
            raise ValueError("Quick-control readback must be finite.")
        value_si = float(value_si)
        self._confirmed_values[target] = value_si
        if adopt_draft or target not in self._dirty_drafts:
            existing = self._draft_texts.get(target)
            if existing:
                try:
                    text = render_quantity_si_like(
                        existing, descriptor.dimension, value_si
                    )
                except Exception:
                    text = format_quantity_auto(value_si, descriptor.dimension)
            else:
                text = format_quantity_auto(value_si, descriptor.dimension)
            self._draft_texts[target] = text
            self._dirty_drafts.discard(target)
            self.draft_changed.emit(target, text, "readback")
        self.confirmed_changed.emit(target, value_si)

    def bound_texts(self, target: str) -> tuple[str, str] | None:
        return self._bound_texts.get(target)

    def bound(self, target: str) -> QuickControlSafetyBound | None:
        return self._bound_objects.get(target)

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
            if self._device_can_apply(device):
                self._controllers[device].call("quick_readback")

    def cancel_all(self, reason: str = "Cancelled") -> None:
        for targets in self._adopt_readback_targets.values():
            targets.clear()
        for device, pending in self._pending.items():
            for target in tuple(pending):
                self.state_changed.emit(target, "unknown", reason)
            pending.clear()

    def _send_next(self, device: str) -> None:
        if self._inflight[device] is not None or not self._pending[device]:
            return
        _target, request = self._pending[device].popitem(last=False)
        if not self._device_can_apply(device):
            self.state_changed.emit(
                request.target,
                "draft",
                f"Device {device} is not connected with a confirmed output state; "
                "the value remains a local draft.",
            )
            return
        state = self._device_states[device]
        builder = self._hardware_request_builders.get(device)
        try:
            operation, payload = (
                builder(request, state)
                if builder is not None
                else (
                    "quick_setpoint",
                    QuickControlCommand(request.target, request.text),
                )
            )
        except Exception as exc:
            self.state_changed.emit(request.target, "rejected", str(exc))
            self._send_next(device)
            return
        self._inflight[device] = request
        self._inflight_operations[device] = operation
        self._inflight_payloads[device] = payload
        self.state_changed.emit(
            request.target,
            "applying",
            (
                "Applying configuration with OUTPUT OFF (dry run)..."
                if operation == "quick_configure"
                else "Applying live setpoint..."
            ),
        )
        self._controllers[device].call(operation, payload)

    def _result(self, device: str, operation: str, result: object) -> None:
        if operation == "quick_readback" and isinstance(result, dict):
            for target, value in result.items():
                if str(target) in QUICK_CONTROLS_BY_TARGET:
                    target = str(target)
                    adopt_draft = target in self._adopt_readback_targets[device]
                    self.confirmed_snapshot(
                        target, float(value), adopt_draft=adopt_draft
                    )
                    if adopt_draft:
                        self._adopt_readback_targets[device].discard(target)
                    self.value_read.emit(target, float(value))
                    self.state_changed.emit(
                        target, "ready", "Verified device configuration"
                    )
            return
        if operation != self._inflight_operations[device]:
            return
        request = self._inflight[device]
        self._inflight[device] = None
        self._inflight_operations[device] = None
        payload = self._inflight_payloads[device]
        self._inflight_payloads[device] = None
        if request is not None:
            if operation == "quick_configure":
                self.configuration_verified.emit(request.target, payload)
            self.confirmed_snapshot(
                request.target, float(result), adopt_draft=True
            )
            descriptor = QUICK_CONTROLS_BY_TARGET[request.target]
            if descriptor.device_module == "rigol" and request.target.rsplit(".", 1)[-1] in {
                "high_level",
                "low_level",
                "amplitude",
                "offset",
            }:
                atomic_group = descriptor.atomic_group
                self._adopt_readback_targets[device].update(
                    descriptor.target
                    for descriptor in QUICK_CONTROL_DESCRIPTORS
                    if descriptor.atomic_group == atomic_group
                )
            self.value_read.emit(request.target, float(result))
            self.state_changed.emit(
                request.target,
                "applied",
                f"Applied {request.text} · readback {float(result):.12g} SI",
            )
        self._send_next(device)
        # A successful Rigol voltage update can change all four coupled views;
        # reconcile the full adapter snapshot after the queued setpoint.
        if request is not None and request.target.rsplit(".", 1)[-1] in {
            "high_level",
            "low_level",
            "amplitude",
            "offset",
        }:
            self._controllers[device].call("quick_readback")

    def _error(self, device: str, operation: str, error: str) -> None:
        if operation != self._inflight_operations[device]:
            return
        request = self._inflight[device]
        self._inflight[device] = None
        self._inflight_operations[device] = None
        self._inflight_payloads[device] = None
        if request is not None:
            self.state_changed.emit(request.target, "rejected", error)
        if self._device_can_apply(device):
            self._controllers[device].call("quick_readback")
        self._send_next(device)

    def _device_state(self, device: str, state: str) -> None:
        normalized = str(state).strip().lower()
        self._device_states[device] = normalized
        if normalized in {"disconnected", "fault", "unknown", "compliance"}:
            pending = self._pending[device]
            for target in tuple(pending):
                self.state_changed.emit(target, "draft", f"Device {state}")
            pending.clear()

    def _device_can_apply(self, device: str) -> bool:
        """Allow hardware traffic only for an authoritative OFF or ON state."""

        return self._device_states.get(device) in {"output_off", "output_on"}


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


class QuickControlRow(StationCardWidget):
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
        self.setProperty("stationSurface", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
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
        self.slider = QuickQuantitySlider(
            target=descriptor.target,
            descriptor=descriptor,
            editor=self.value,
            show_title=False,
            parent=self,
        )
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.increase)
        layout.addLayout(controls)
        # Keep the old aggregate label as a non-visual readout for callers
        # that inspected it; the visible Fluent slider renders MIN and MAX
        # independently so they remain readable at narrow widths.
        self.limits = CaptionLabel("Safety limits unavailable", self)
        self.limits.setObjectName("quickControlLimitsCompat")
        self.limits.setVisible(False)
        layout.addWidget(self.limits)
        self.refresh_limits()
        self.status = CaptionLabel("Ready · draft stays synchronized with the device card", self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.decrease.clicked.connect(lambda: self.step(-1, Decimal(1)))
        self.increase.clicked.connect(lambda: self.step(1, Decimal(1)))
        self.value.step_requested.connect(self.step)
        self.slider.draft_value_changed.connect(self._draft_changed)
        self.slider.commit_requested.connect(self._commit_requested)

    def refresh_limits(self) -> None:
        bound = self._coordinator.bound(self.descriptor.target)
        if bound is None:
            self.slider.clear_bounds()
            self.limits.setText("Safety limits unavailable")
            return
        self.slider.set_bounds(bound)
        self.limits.setText(
            f"MIN  {bound.minimum_text}    MAX  {bound.maximum_text}"
        )

    def set_value_text(self, text: str) -> None:
        self.slider.set_value_text(text)

    def set_value_si(self, value_si: float) -> None:
        self.slider.set_value_si(value_si)

    def _draft_changed(self, target: str, text: str) -> None:
        self._coordinator.publish_draft(target, text, source="quick_controls")

    def _commit_requested(self, _target: str, _text: str) -> None:
        self.submit()

    def step(self, direction: int, multiplier: object) -> None:
        try:
            previous_si = self.slider.value_si()
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
        self.slider.set_value_text(text)
        if limited:
            self.set_state("limit", detail)
            if previous_si == bounded_si:
                return
        self.submit()

    def submit(self) -> None:
        try:
            text = self.value.text()
            parse_quantity(text, self.descriptor.dimension)
        except ValueError as exc:
            self.set_state("rejected", str(exc))
            return
        self.submit_requested.emit(self.descriptor.target, text)

    def set_state(self, state: str, detail: str) -> None:
        self.setProperty("quickState", state)
        self.status.setText(f"{state.title()} · {detail}")
        self.style().unpolish(self)
        self.style().polish(self)


class QuickControlPicker(StationDialog):
    def __init__(
        self,
        selected: tuple[str, ...],
        selected_outputs: tuple[str, ...] | QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        # Keep the old two-argument construction usable for callers outside the
        # window while allowing the picker to own output visibility as well.
        if isinstance(selected_outputs, QWidget) and parent is None:
            parent = selected_outputs
            selected_outputs = None
        super().__init__(parent)
        self.setWindowTitle("Choose quick controls")
        self.setMinimumWidth(500)
        self.resize(520, 640)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_shell.surface_layout
        layout.setSpacing(12)

        heading = SubtitleLabel("Choose quick controls", surface)
        layout.addWidget(heading)
        intro = CaptionLabel(
            "Keep the values and hardware actions you use most often within reach.",
            surface,
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        picker_scroll = ScrollArea(surface)
        picker_scroll.setObjectName("quickPickerScroll")
        picker_scroll.setFrameShape(QFrame.Shape.NoFrame)
        picker_scroll.setWidgetResizable(True)
        picker_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        picker_content = QWidget(surface)
        picker_content.setProperty("stationSurface", "surface")
        picker_layout = QVBoxLayout(picker_content)
        picker_layout.setContentsMargins(0, 0, 8, 0)
        picker_layout.setSpacing(10)

        setpoint_card = StationCardWidget(picker_content)
        setpoint_card.setObjectName("quickPickerSetpoints")
        setpoint_layout = QVBoxLayout(setpoint_card)
        setpoint_layout.setContentsMargins(14, 12, 14, 12)
        setpoint_layout.setSpacing(6)
        setpoint_title = StrongBodyLabel("Setpoints", setpoint_card)
        setpoint_layout.addWidget(setpoint_title)
        setpoint_hint = CaptionLabel(
            "Sliders and typed values stay synchronized with the active device card.",
            setpoint_card,
        )
        setpoint_hint.setObjectName("muted")
        setpoint_hint.setWordWrap(True)
        setpoint_layout.addWidget(setpoint_hint)
        self.checkboxes: dict[str, CheckBox] = {}
        for descriptor in QUICK_CONTROL_DESCRIPTORS:
            checkbox = CheckBox(descriptor.label, setpoint_card)
            checkbox.setChecked(descriptor.target in selected)
            setpoint_layout.addWidget(checkbox)
            self.checkboxes[descriptor.target] = checkbox
        picker_layout.addWidget(setpoint_card)

        output_card = StationCardWidget(picker_content)
        output_card.setObjectName("quickPickerOutputs")
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(14, 12, 14, 12)
        output_layout.setSpacing(6)
        output_title = StrongBodyLabel("Hardware outputs", output_card)
        output_layout.addWidget(output_title)
        output_hint = CaptionLabel(
            "Show or hide independent channel actions and the Keithley A+B group action.",
            output_card,
        )
        output_hint.setObjectName("muted")
        output_hint.setWordWrap(True)
        output_layout.addWidget(output_hint)
        chosen_outputs = (
            QUICK_OUTPUT_TARGETS
            if selected_outputs is None
            else tuple(str(target) for target in selected_outputs)
        )
        self.output_checkboxes: dict[str, CheckBox] = {}
        for target, label in QUICK_OUTPUT_DESCRIPTORS:
            checkbox = CheckBox(label, output_card)
            checkbox.setChecked(target in chosen_outputs)
            output_layout.addWidget(checkbox)
            self.output_checkboxes[target] = checkbox
        picker_layout.addWidget(output_card)
        picker_layout.addStretch(1)
        picker_scroll.setWidget(picker_content)
        layout.addWidget(picker_scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", surface)
        apply = PrimaryPushButton("Show selected", surface)
        cancel.clicked.connect(self.reject)
        apply.clicked.connect(self.accept)
        footer.addWidget(cancel)
        footer.addWidget(apply)
        layout.addLayout(footer)

    def selected_targets(self) -> tuple[str, ...]:
        return tuple(
            target for target, checkbox in self.checkboxes.items() if checkbox.isChecked()
        )

    def selected_output_targets(self) -> tuple[str, ...]:
        return tuple(
            target
            for target, checkbox in self.output_checkboxes.items()
            if checkbox.isChecked()
        )


class QuickOutputRow(StationCardWidget):
    """One physical output channel with an observed state badge."""

    def __init__(
        self,
        device: str,
        channel: str,
        label: str,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.channel = channel
        self.setObjectName("quickOutputRow")
        self.setProperty("stationSurface", "raised")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(5)

        title_block = QVBoxLayout()
        title_block.setContentsMargins(0, 0, 0, 0)
        title_block.setSpacing(0)
        title = StrongBodyLabel(f"CH {channel}", self)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setMinimumWidth(0)
        title.setToolTip(f"{label} physical output channel {channel}")
        title_block.addWidget(title)
        layout.addLayout(title_block, 1)

        self.state = InfoBadge(self, InfoLevel.WARNING)
        self.state.setText("UNKNOWN")
        self.state.setObjectName("quickOutputState")
        self.state.setProperty("quickOutputState", "unknown")
        self.state.setFixedWidth(68)
        self.state.setFixedHeight(22)
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.state)

        self.on_button = PrimaryToolButton(self)
        self.on_button.setObjectName("outputOnButton")
        self.on_button.setProperty("quickOutputAction", "on")
        self.on_button.setIcon(FluentIcon.POWER_BUTTON)
        self.on_button.setIconSize(QSize(16, 16))
        self.on_button.setFixedSize(32, 32)
        self.on_button.setAccessibleName(f"{label} channel {channel} output on")
        self.on_button.setToolTip(
            f"Enable {label} channel {channel} through safety validation and readback."
        )
        self.off_button = ToolButton(self)
        self.off_button.setObjectName("outputOffButton")
        self.off_button.setProperty("quickOutputAction", "off")
        self.off_button.setIcon(FluentIcon.CLOSE)
        self.off_button.setIconSize(QSize(16, 16))
        self.off_button.setFixedSize(32, 32)
        self.off_button.setAccessibleName(f"{label} channel {channel} output off")
        self.off_button.setToolTip(
            f"Disable {label} channel {channel} and verify the hardware state."
        )
        layout.addWidget(self.on_button)
        layout.addWidget(self.off_button)

    def set_state(self, state: str) -> None:
        normalized = state.lower()
        if normalized not in {"on", "off", "unknown"}:
            normalized = "unknown"
        self.state.setText(normalized.upper())
        self.state.setProperty("quickOutputState", normalized)
        self.state.setLevel(
            {
                "on": InfoLevel.ERROR,
                "off": InfoLevel.SUCCESS,
                "unknown": InfoLevel.WARNING,
            }[normalized]
        )
        self.state.update()


class QuickControlsWindow(FluentWidget):
    output_requested = Signal(str, str, bool)
    output_group_requested = Signal(str, bool)

    def __init__(self, coordinator: QuickControlCoordinator, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setObjectName("quickControlsWindow")
        self.setProperty("stationSurface", "raised")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Quick peripheral controls")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        # Re-apply the frameless native hooks after changing window flags. The
        # QFluent base installs them before this utility adds its top-level flags.
        self.updateFrameless()
        self._repair_title_bar_window_actions()
        self.setResizeEnabled(True)
        self.setCustomBackgroundColor(
            tokens_for("light").surface_raised,
            tokens_for("dark").surface_raised,
        )
        self.resize(550, 720)
        self._base_minimum_height = 460
        self._height_fit_pending = False
        self._height_fit_timer = QTimer(self)
        self._height_fit_timer.setSingleShot(True)
        self._height_fit_timer.timeout.connect(self._fit_height_to_content)
        self._screen_signal_connected = False
        self.setMinimumSize(420, self._base_minimum_height)
        self._resize_handle_width = 8
        self._resize_handles: dict[str, _QuickResizeHandle] = {}
        for name, edges, cursor in (
            ("left", frozenset({"left"}), Qt.CursorShape.SizeHorCursor),
            ("right", frozenset({"right"}), Qt.CursorShape.SizeHorCursor),
            ("top", frozenset({"top"}), Qt.CursorShape.SizeVerCursor),
            ("bottom", frozenset({"bottom"}), Qt.CursorShape.SizeVerCursor),
            (
                "top_left",
                frozenset({"top", "left"}),
                Qt.CursorShape.SizeFDiagCursor,
            ),
            (
                "bottom_right",
                frozenset({"bottom", "right"}),
                Qt.CursorShape.SizeFDiagCursor,
            ),
            (
                "top_right",
                frozenset({"top", "right"}),
                Qt.CursorShape.SizeBDiagCursor,
            ),
            (
                "bottom_left",
                frozenset({"bottom", "left"}),
                Qt.CursorShape.SizeBDiagCursor,
            ),
        ):
            self._resize_handles[name] = _QuickResizeHandle(
                name, edges, cursor, self
            )
        self._coordinator = coordinator
        self._rows: dict[str, QuickControlRow] = {}
        self._groups: dict[str, CardWidget] = {}
        self._output_rows: dict[tuple[str, str], QuickOutputRow] = {}
        self._output_group_buttons: dict[str, PrimaryToolButton] = {}
        self._output_group_off_buttons: dict[str, ToolButton] = {}
        self._output_group_containers: dict[str, QWidget] = {}
        self._output_device_titles: dict[str, QWidget] = {}
        self._output_card: CardWidget | None = None
        self._output_body: _QuickOutputBody | None = None
        self._output_body_layout: QGridLayout | None = None
        self._output_layout_mode = ""
        self._selected_outputs: tuple[str, ...] = QUICK_OUTPUT_TARGETS
        self._selected: tuple[str, ...] = ()
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(0, 0, 0, 0)
        self.layout_root.setSpacing(0)
        self.modal_shell = StationModalShell(
            self,
            outer_margins=(10, 38, 10, 10),
            backdrop_margins=(10, 10, 10, 10),
            surface_margins=(12, 12, 12, 12),
        )
        self.layout_root.addWidget(self.modal_shell, 1)
        self.backdrop = self.modal_shell.backdrop
        self.surface = self.modal_shell.surface
        self.surface_layout = self.modal_shell.surface_layout

        header = QHBoxLayout()
        header.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title_block.addWidget(SubtitleLabel("Quick Controls", self.surface))
        subtitle = BodyLabel(
            "Live drafts shared with the active device card", self.surface
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        title_block.addWidget(subtitle)
        header.addLayout(title_block, 1)
        self.live_badge = InfoBadge("LIVE", self.surface, InfoLevel.SUCCESS)
        self.live_badge.setFixedHeight(24)
        header.addWidget(self.live_badge, 0, Qt.AlignmentFlag.AlignTop)
        self.choose = PushButton("Choose...", self.surface)
        self.choose.setIcon(FluentIcon.SETTING)
        self.choose.setToolTip("Choose which setpoints stay visible in this window.")
        header.addWidget(self.choose)
        self.surface_layout.addLayout(header)
        self._build_output_controls()

        self.controls_content = QWidget(self.surface)
        self.controls_content.setObjectName("quickControlsContent")
        self.controls_content.setProperty("stationSurface", "surface")
        self.controls_content.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        controls_content_layout = QVBoxLayout(self.controls_content)
        controls_content_layout.setContentsMargins(0, 0, 8, 0)
        controls_content_layout.setSpacing(8)
        if self._output_card is not None:
            controls_content_layout.addWidget(self._output_card)

        self.content_host = QWidget(self.controls_content)
        self.content_host.setProperty("stationSurface", "surface")
        self.content_host.setMinimumWidth(0)
        self.content_host.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.content = QVBoxLayout(self.content_host)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(10)
        self.empty_state = StationCardWidget(self.content_host)
        self.empty_state.setProperty("stationSurface", "card")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(18, 18, 18, 18)
        empty_layout.setSpacing(6)
        empty_title = StrongBodyLabel("No quick controls selected", self.empty_state)
        empty_layout.addWidget(empty_title)
        empty_text = CaptionLabel(
            "Choose a frequency, voltage or current setpoint to keep it one drag away.",
            self.empty_state,
        )
        empty_text.setObjectName("muted")
        empty_text.setWordWrap(True)
        empty_layout.addWidget(empty_text)
        self.controls_scroll = ScrollArea(self)
        self.controls_scroll.setObjectName("quickControlsScroll")
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        controls_content_layout.addWidget(self.content_host)
        controls_content_layout.addStretch(1)
        self.controls_scroll.setWidget(self.controls_content)
        self.surface_layout.addWidget(self.controls_scroll, 1)
        self.set_output_targets(self._selected_outputs, persist=False)
        self.choose.clicked.connect(self.choose_controls)
        coordinator.state_changed.connect(self._state_changed)
        coordinator.draft_changed.connect(self._draft_changed)
        coordinator.value_read.connect(self._value_read)
        coordinator.bounds_changed.connect(self._refresh_limits)

    def _repair_title_bar_window_actions(self) -> None:
        """Bind title-bar actions after this widget becomes a top-level window.

        ``FluentWidget`` builds its title bar while the widget still has its
        ``MainWindow`` parent. qframelesswindow therefore resolves
        ``self.window()`` to the parent and binds the close/minimize buttons to
        the station shell. Quick Controls becomes a top-level window just
        below, so those connections must target this window instead.
        """
        self.titleBar.minBtn.clicked.disconnect()
        self.titleBar.minBtn.clicked.connect(self.showMinimized)
        self.titleBar.closeBtn.clicked.disconnect()
        self.titleBar.closeBtn.clicked.connect(self.close)
        self.titleBar.closeBtn.show()
        self.titleBar.closeBtn.raise_()

    def _screen_available_height(self) -> int | None:
        screen = self.screen()
        if screen is None and self.windowHandle() is not None:
            screen = self.windowHandle().screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        return int(screen.availableGeometry().height())

    def _schedule_height_fit(self) -> None:
        if self._height_fit_timer.isActive():
            return
        self._height_fit_pending = True
        self._height_fit_timer.start(0)

    def _fit_height_to_content(self) -> None:
        self._height_fit_pending = False
        if not self.isVisible():
            return

        content_layout = self.controls_content.layout()
        if content_layout is not None:
            content_layout.activate()
        self.controls_content.updateGeometry()
        viewport_height = self.controls_scroll.viewport().height()
        if viewport_height <= 0:
            return

        content_height = self.controls_content.sizeHint().height()
        if content_layout is not None:
            content_height = max(content_height, content_layout.sizeHint().height())
        chrome_height = max(0, self.height() - viewport_height)
        natural_height = content_height + chrome_height

        available_height = self._screen_available_height()
        height_cap = None
        if available_height is not None and available_height > 0:
            height_cap = max(1, math.floor(available_height * 0.70))
            self.setMinimumHeight(min(self._base_minimum_height, height_cap))
        else:
            self.setMinimumHeight(self._base_minimum_height)

        minimum_height = self.minimumHeight()
        desired_height = max(minimum_height, natural_height)
        if height_cap is not None:
            desired_height = min(desired_height, height_cap)
        if desired_height != self.height():
            self.resize(self.width(), desired_height)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._screen_signal_connected and self.windowHandle() is not None:
            self.windowHandle().screenChanged.connect(
                lambda _screen: self._schedule_height_fit()
            )
            self._screen_signal_connected = True
        self._position_resize_handles()
        self.titleBar.raise_()
        self.titleBar.closeBtn.raise_()
        self._schedule_height_fit()

    def _build_output_controls(self) -> None:
        """Expose deliberate output requests without bypassing device pages."""

        card = StationCardWidget(self.surface)
        card.setObjectName("quickControlsOutputCard")
        card.setProperty("stationSurface", "card")
        self._output_card = card
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        card.setMinimumWidth(0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(1)
        title = SubtitleLabel("Outputs", card)
        title.setObjectName("sectionTitle")
        title_block.addWidget(title)
        note = CaptionLabel(
            "A/B output · the device card remains the safety and readback authority.",
            card,
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        title_block.addWidget(note)
        heading.addLayout(title_block, 1)
        heading_badge = InfoBadge("HARDWARE", card, InfoLevel.WARNING)
        heading_badge.setFixedHeight(22)
        heading.addWidget(heading_badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)
        output_body = _QuickOutputBody(self._relayout_output_body, card)
        output_body.setObjectName("quickOutputBody")
        output_body_layout = QGridLayout(output_body)
        output_body_layout.setContentsMargins(0, 0, 0, 0)
        output_body_layout.setHorizontalSpacing(6)
        output_body_layout.setVerticalSpacing(5)
        output_body_layout.setColumnStretch(0, 1)
        output_body_layout.setColumnStretch(1, 1)
        self._output_body = output_body
        self._output_body_layout = output_body_layout
        for device, label, channels in (
            ("rigol", "Rigol DG1022Z", ("1", "2")),
            ("keithley", "Keithley 2600", ("A", "B")),
        ):
            device_title = StrongBodyLabel(label, card)
            device_title.setObjectName("quickOutputDeviceTitle")
            self._output_device_titles[device] = device_title
            for channel in channels:
                output_row = QuickOutputRow(device, channel, label, card)
                self._output_rows[(device, channel)] = output_row
                output_row.on_button.clicked.connect(
                    lambda _checked=False, name=device, ch=channel: self.output_requested.emit(
                        name, ch, True
                    )
                )
                output_row.off_button.clicked.connect(
                    lambda _checked=False, name=device, ch=channel: self.output_requested.emit(
                        name, ch, False
                    )
                )
            if device == "keithley":
                group_container = QWidget(card)
                group_container.setObjectName("quickOutputGroupActions")
                group_actions = QHBoxLayout(group_container)
                group_actions.setContentsMargins(2, 1, 2, 0)
                group_actions.setSpacing(5)
                group_label = CaptionLabel("A+B", group_container)
                group_label.setToolTip("Apply one safety-validated action to both Keithley channels.")
                group_actions.addWidget(group_label, 1)
                group_on = PrimaryToolButton(group_container)
                group_on.setObjectName("outputOnButton")
                group_on.setProperty("quickOutputAction", "on")
                group_on.setIcon(FluentIcon.POWER_BUTTON)
                group_on.setIconSize(QSize(16, 16))
                group_on.setFixedSize(32, 32)
                group_on.setAccessibleName("Keithley channels A and B output on")
                group_on.setToolTip(
                    "Validate and enable Keithley A and B in order, with readback."
                )
                group_off = ToolButton(group_container)
                group_off.setObjectName("outputOffButton")
                group_off.setProperty("quickOutputAction", "off")
                group_off.setIcon(FluentIcon.CLOSE)
                group_off.setIconSize(QSize(16, 16))
                group_off.setFixedSize(32, 32)
                group_off.setAccessibleName("Keithley channels A and B output off")
                group_off.setToolTip(
                    "Disable both Keithley channels and verify each readback."
                )
                self._output_group_buttons[device] = group_on
                self._output_group_off_buttons[device] = group_off
                group_on.clicked.connect(
                    lambda _checked=False, name=device: self.output_group_requested.emit(
                        name, True
                    )
                )
                group_off.clicked.connect(
                    lambda _checked=False, name=device: self.output_group_requested.emit(
                        name, False
                    )
                )
                group_actions.addWidget(group_on)
                group_actions.addWidget(group_off)
                self._output_group_containers[device] = group_container
        layout.addWidget(output_body)
        self._relayout_output_body(0)
        _add_quick_shadow(card, blur=22, y=3)

    def _relayout_output_body(self, width: int) -> None:
        layout = self._output_body_layout
        if layout is None:
            return
        mode = "wide" if width >= 380 else "narrow"
        if mode == self._output_layout_mode:
            return
        self._output_layout_mode = mode
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self._output_body)

        rigol_title = self._output_device_titles["rigol"]
        keithley_title = self._output_device_titles["keithley"]
        rigol_rows = [
            self._output_rows[("rigol", channel)] for channel in ("1", "2")
        ]
        keithley_rows = [
            self._output_rows[("keithley", channel)] for channel in ("A", "B")
        ]
        group = self._output_group_containers["keithley"]
        if mode == "wide":
            layout.addWidget(rigol_title, 0, 0)
            layout.addWidget(keithley_title, 0, 1)
            for row, widget in enumerate(rigol_rows, start=1):
                layout.addWidget(widget, row, 0)
            for row, widget in enumerate(keithley_rows, start=1):
                layout.addWidget(widget, row, 1)
            layout.addWidget(group, 3, 1)
            return

        row = 0
        layout.addWidget(rigol_title, row, 0, 1, 2)
        row += 1
        for widget in rigol_rows:
            layout.addWidget(widget, row, 0, 1, 2)
            row += 1
        layout.addWidget(keithley_title, row, 0, 1, 2)
        row += 1
        for widget in keithley_rows:
            layout.addWidget(widget, row, 0, 1, 2)
            row += 1
        layout.addWidget(group, row, 0, 1, 2)

    def set_output_state(self, device: str, channel: str, state: str) -> None:
        row = self._output_rows.get((device, channel))
        if row is not None:
            row.set_state(state)

    def output_targets(self) -> tuple[str, ...]:
        return self._selected_outputs

    def set_output_targets(
        self, targets: tuple[str, ...], *, persist: bool = True
    ) -> None:
        selected = set(str(target) for target in targets)
        self._selected_outputs = tuple(
            target for target in QUICK_OUTPUT_TARGETS if target in selected
        )
        for (device, channel), row in self._output_rows.items():
            row.setVisible(_output_target(device, channel) in selected)
        for device, title in self._output_device_titles.items():
            channels_visible = any(
                _output_target(device, channel) in selected
                for current_device, channel in self._output_rows
                if current_device == device
            )
            group_visible = (
                device == "keithley" and "output.keithley.group" in selected
            )
            title.setVisible(channels_visible or group_visible)
        for device, container in self._output_group_containers.items():
            container.setVisible(f"output.{device}.group" in selected)
        if self._output_card is not None:
            self._output_card.setVisible(bool(self._selected_outputs))
        if persist:
            QSettings("LabControl", "LabControl").setValue(
                "quick_controls/outputs", list(self._selected_outputs)
            )
        self._schedule_height_fit()

    def restore_workspace(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        selected = settings.value("quick_controls/targets", (), list)
        valid = tuple(str(item) for item in selected if str(item) in QUICK_CONTROLS_BY_TARGET)
        self.set_targets(valid)
        saved_outputs = settings.value("quick_controls/outputs", None)
        if saved_outputs is None:
            output_targets = QUICK_OUTPUT_TARGETS
        elif isinstance(saved_outputs, (list, tuple)):
            output_targets = tuple(str(item) for item in saved_outputs)
        else:
            output_targets = (str(saved_outputs),)
        self.set_output_targets(output_targets)
        geometry = settings.value("quick_controls/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self._schedule_height_fit()

    def set_targets(self, targets: tuple[str, ...]) -> None:
        previous_values = {
            target: row.value.text() for target, row in self._rows.items()
        }
        self._selected = tuple(dict.fromkeys(targets))
        self._rows.clear()
        while self.content.count():
            item = self.content.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if widget is self.empty_state:
                widget.hide()
            else:
                widget.deleteLater()
        self._groups.clear()

        if not self._selected:
            self.content.addWidget(self.empty_state)
            self.empty_state.show()
        else:
            self.empty_state.hide()
            groups: dict[str, tuple[str, str, list[str]]] = {}
            group_metadata = {
                "rigol": (
                    "Rigol generator",
                    "Frequency and voltage drafts for both output channels.",
                ),
                "keithley": (
                    "Keithley source",
                    "Channel source levels with the same safety envelope as the card.",
                ),
            }
            for target in self._selected:
                descriptor = QUICK_CONTROLS_BY_TARGET[target]
                title, hint = group_metadata.get(
                    descriptor.device_module,
                    (descriptor.device_module.title(), "Live device-card drafts."),
                )
                if descriptor.device_module not in groups:
                    groups[descriptor.device_module] = (
                        title,
                        hint,
                        [],
                    )
                groups[descriptor.device_module][2].append(target)

            for device, (title, hint, group_targets) in groups.items():
                group_card = StationCardWidget(self.content_host)
                group_card.setObjectName(f"quickControlsGroup_{device}")
                group_card.setProperty("stationSurface", "card")
                group_layout = QVBoxLayout(group_card)
                group_layout.setContentsMargins(10, 10, 10, 10)
                group_layout.setSpacing(8)
                group_title = StrongBodyLabel(title, group_card)
                group_layout.addWidget(group_title)
                group_hint = CaptionLabel(hint, group_card)
                group_hint.setObjectName("muted")
                group_hint.setWordWrap(True)
                group_layout.addWidget(group_hint)
                for target in group_targets:
                    descriptor = QUICK_CONTROLS_BY_TARGET[target]
                    row = QuickControlRow(descriptor, self._coordinator, group_card)
                    current_text = self._coordinator.draft_text(target)
                    if current_text is None:
                        current_text = previous_values.get(target, descriptor.default_text)
                    row.set_value_text(current_text)
                    row.submit_requested.connect(self._coordinator.submit)
                    row.move_requested.connect(self._move_control)
                    group_layout.addWidget(row)
                    self._rows[target] = row
                self.content.addWidget(group_card)
                self._groups[device] = group_card
            self.content.addStretch(1)
        self.controls_scroll.verticalScrollBar().setValue(0)
        QSettings("LabControl", "LabControl").setValue(
            "quick_controls/targets", list(self._selected)
        )
        self._schedule_height_fit()

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
        picker = QuickControlPicker(self._selected, self._selected_outputs, self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.set_targets(picker.selected_targets())
            self.set_output_targets(picker.selected_output_targets())

    def _state_changed(self, target: str, state: str, detail: str) -> None:
        row = self._rows.get(target)
        if row is not None:
            row.set_state(state, detail)

    def _draft_changed(self, target: str, text: str, _source: str) -> None:
        row = self._rows.get(target)
        if row is not None and row.value.text() != text:
            row.set_value_text(text)

    def _value_read(self, target: str, value_si: float) -> None:
        row = self._rows.get(target)
        if row is None:
            return
        row.set_value_si(value_si)

    def _refresh_limits(self) -> None:
        for row in self._rows.values():
            row.refresh_limits()

    def _position_resize_handles(self, *, raise_handles: bool = False) -> None:
        if not hasattr(self, "_resize_handles") or not self._resize_handles:
            return
        width = self.width()
        height = self.height()
        edge = self._resize_handle_width
        corner = edge + 4
        resizable = not self.isMaximized() and not self.isFullScreen()
        geometries = {
            "left": QRect(0, corner, edge, max(0, height - 2 * corner)),
            "right": QRect(
                max(0, width - edge), corner, edge, max(0, height - 2 * corner)
            ),
            "top": QRect(corner, 0, max(0, width - 2 * corner), edge),
            "bottom": QRect(
                corner, max(0, height - edge), max(0, width - 2 * corner), edge
            ),
            "top_left": QRect(0, 0, corner, corner),
            "top_right": QRect(max(0, width - corner), 0, corner, corner),
            "bottom_left": QRect(0, max(0, height - corner), corner, corner),
            "bottom_right": QRect(
                max(0, width - corner), max(0, height - corner), corner, corner
            ),
        }
        for name, handle in self._resize_handles.items():
            geom = geometries[name]
            if handle.geometry() != geom:
                handle.setGeometry(geom)
            if handle.isVisible() != resizable:
                handle.setVisible(resizable)
            if raise_handles:
                handle.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._position_resize_handles(raise_handles=False)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._position_resize_handles(raise_handles=True)

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

    def closeEvent(self, event: QCloseEvent) -> None:
        self._height_fit_timer.stop()
        self._height_fit_pending = False
        QSettings("LabControl", "LabControl").setValue(
            "quick_controls/geometry", self.saveGeometry()
        )
        super().closeEvent(event)
