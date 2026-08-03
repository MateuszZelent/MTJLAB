"""Fluent floating quick controls for peripheral setpoints."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from decimal import Decimal
import math

from PySide6.QtCore import QObject, QSettings, Qt, Signal
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
    SubtitleLabel,
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
from app.ui.dialogs import StationDialog
from app.ui.widgets.quick_quantity_slider import QuickQuantitySlider
from app.ui.workers import DeviceController


class QuickControlCoordinator(QObject):
    state_changed = Signal(str, str, str)
    value_read = Signal(str, float)
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
            return
        self.publish_draft(target, text, source="quick_controls")
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
        self._inflight[device] = request
        self.state_changed.emit(request.target, "applying", "Applying...")
        self._controllers[device].call(
            "quick_setpoint", QuickControlCommand(request.target, request.text)
        )

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
        if operation != "quick_setpoint":
            return
        request = self._inflight[device]
        self._inflight[device] = None
        if request is not None:
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


class QuickControlRow(CardWidget):
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
        self._groups: dict[str, CardWidget] = {}
        self._selected: tuple[str, ...] = ()
        self.layout_root = QVBoxLayout(self)
        self.layout_root.setContentsMargins(18, 16, 18, 18)
        self.layout_root.setSpacing(12)
        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title_block.addWidget(SubtitleLabel("Quick Controls", self))
        subtitle = BodyLabel(
            "Live drafts shared with the active device card", self
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        title_block.addWidget(subtitle)
        header.addLayout(title_block, 1)
        header.addStretch(1)
        self.choose = PushButton("Choose...", self)
        self.choose.setToolTip("Choose which setpoints stay visible in this window.")
        header.addWidget(self.choose)
        self.layout_root.addLayout(header)
        self._build_output_controls()
        self.content_host = QWidget(self)
        self.content_host.setProperty("stationSurface", "surface")
        self.content_host.setMinimumWidth(0)
        self.content = QVBoxLayout(self.content_host)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(10)
        self.empty_state = CardWidget(self.content_host)
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
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.controls_scroll.setWidget(self.content_host)
        self.layout_root.addWidget(self.controls_scroll, 1)
        self.choose.clicked.connect(self.choose_controls)
        coordinator.state_changed.connect(self._state_changed)
        coordinator.draft_changed.connect(self._draft_changed)
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
                group_card = CardWidget(self.content_host)
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

    def closeEvent(self, event: QCloseEvent) -> None:
        QSettings("LabControl", "LabControl").setValue(
            "quick_controls/geometry", self.saveGeometry()
        )
        super().closeEvent(event)
