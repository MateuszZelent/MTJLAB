"""Fluent manual voltage control and coupled Lake Shore calibration pages."""

from __future__ import annotations

import statistics
import math
from collections.abc import Callable
from dataclasses import asdict
from threading import Event

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QSizePolicy, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    Slider,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
    isDarkTheme,
)

from app.devices.moke_box.calibration import CalibrationContext, CalibrationRequest, MokeCalibration
from app.devices.moke_box.calibration_runner import (
    CalibrationRunResult,
    MokeCalibrationRunner,
    WorkflowCancellation,
)
from app.devices.moke_box.ui.voltage_history import MokeVoltageHistory
from app.devices.moke_box.ui.configuration_panel import MokeVoltageConfigurationPanel
from app.domain.errors import ConfigurationError
from app.domain.quick_controls import quantity_step_si, render_quantity_si_like, step_quantity_text
from app.domain.quantities import (
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.safety.moke_box import MokeControlProfile, MokeVoltagePlan, MokeVoltageResult, MokeRampProgress, MokeLiveTargets, control_profile_from_settings, additional_control_profiles_from_settings
from app.settings.models import StationSettings
from app.storage.moke_calibration_store import MokeCalibrationRepository
from app.ui.design_system import plot_theme, tokens_for
from app.ui.common import line_edit
from app.ui.workers import DeviceController
from app.ui.widgets.quick_quantity_slider import QuantitySliderMapping


class MokeFieldWorker(QObject):
    progress = Signal(object)
    voltage_progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, kind, request, leases, directory, cancel, live_targets=None):
        super().__init__()
        self.kind, self.request = kind, request
        self.leases, self.directory, self.cancel = leases, directory, cancel
        self.live_targets = live_targets

    @Slot()
    def run(self):
        moke = self.leases["moke_box"]
        interrupts = tuple(lease.interruption_event for lease in self.leases.values())
        cancel = WorkflowCancellation((self.cancel, *interrupts))
        try:
            if self.kind == "calibration":
                result = MokeCalibrationRunner(
                    moke, self.leases["lakeshore_gaussmeter"], self.directory,
                    cancel=self.cancel, interruption_events=interrupts,
                    progress=self.progress.emit, voltage_progress=self.voltage_progress.emit,
                ).run(self.request, armed=True)
            elif self.kind == "zero":
                profile = moke.get_control_profile(self.request)
                with moke.io_timeout(profile.ramp_timeout_s + 5):
                    result = moke.stop_vout(self.request, progress=self.voltage_progress.emit)
            else:
                plan = self.request
                if cancel.is_set():
                    raise ConfigurationError("MOKE manual operation was cancelled before arming.")
                moke.configure_voltage_plan(plan)
                moke.arm_voltage_plan(plan)
                profile = moke.get_control_profile(plan.channel)
                with moke.io_timeout(profile.ramp_timeout_s + 5):
                    result = moke.ramp_vout(plan.channel, plan.targets_v[0], cancel=cancel,
                                           progress=self.voltage_progress.emit, live_targets=self.live_targets)
            self.succeeded.emit(result)
        except Exception as exc:  # noqa: BLE001 - report failure after attempting qualified cleanup
            if self.kind == "calibration":
                # Runner owns cleanup after mutation. Failed preflight must leave DAC untouched.
                self.failed.emit(str(exc))
            else:
                try:
                    moke.emergency_off()
                except Exception:  # noqa: BLE001 - report the primary workflow failure
                    self.failed.emit(f"{exc}; emergency shutdown also failed")
                else:
                    self.failed.emit(str(exc))
        finally:
            if self.live_targets is not None:
                self.live_targets.close()
            for lease in reversed(tuple(self.leases.values())):
                try:
                    lease.release()
                except Exception as exc:  # noqa: BLE001 - report release failure and retain controller reservation
                    self.failed.emit(f"Instrument reservation could not be released: {exc}")
            self.finished.emit()


class MokeFieldWorkflow(QObject):
    """Own one worker at a time; keep every hardware operation off the GUI."""

    status = Signal(str)
    calibration_changed = Signal(object)
    profile_changed = Signal(object)
    busy_changed = Signal(bool)
    vout_overview_requested = Signal()
    voltage_confirmed = Signal(int, float)
    quick_draft_changed = Signal(str, str)
    quick_step_changed = Signal(str, str)
    quick_failed = Signal(str)
    quick_bounds_changed = Signal()
    floating_requested = Signal()

    def _manual_profile(self, channel=None):
        if self._profile is None:
            return None
        channel = self._selected_channel() if channel is None else channel
        if channel == self._profile.channel:
            return self._profile
        for profile in additional_control_profiles_from_settings(self._settings, simulation=self._simulation):
            if profile.channel == channel:
                return profile
        return None

    def quick_control_bounds(self, channel=None):
        """Expose the qualified channel and operator envelope without I/O."""
        profile = self._manual_profile(channel)
        if profile is None or not self._connected:
            return None
        if profile.channel == self._selected_channel():
            lo, hi = self.configuration_panel.minimum_text, self.configuration_panel.maximum_text
        else:
            lo, hi, _, _ = self._channel_drafts.get(profile.channel, self._default_channel_draft(profile))
        minimum = max(profile.minimum_v, self._voltage(lo))
        maximum = min(profile.maximum_v, self._voltage(hi))
        if minimum >= maximum:
            return None
        return profile.channel, f"{minimum:.12g} V", f"{maximum:.12g} V"

    def quick_control_draft(self):
        return f"moke_box.vout{self._selected_channel()}.voltage", self.target.text()

    def quick_control_drafts(self):
        drafts = {f"moke_box.vout{channel}.voltage": values[3]
                  for channel, values in self._channel_drafts.items()}
        target, text = self.quick_control_draft()
        drafts[target] = text
        return drafts

    def voltage_step_text(self, channel):
        return self._channel_steps.get(channel, "1 mV")

    def set_voltage_step(self, channel, text):
        if channel not in range(8):
            raise ValueError("MOKE channel must be 0 through 7.")
        quantity_step_si("0 V", DIMENSION_VOLTAGE, step_text=text)
        changed = self.voltage_step_text(channel) != text
        self._channel_steps[channel] = text
        if channel == self._selected_channel():
            self.configuration_panel.voltage_step.set_step_text(text)
            self.target.setProperty("precisionStep", text)
            self._update_voltage_slider_step()
        if changed:
            self.quick_step_changed.emit(f"moke_box.vout{channel}.voltage", text)

    @property
    def confirmed_voltages(self):
        return dict(self._last_voltages)

    def refresh_quick_values(self):
        if self._connected and not self.busy and not self._external_controlled:
            self._controller.call("read_vouts")

    def set_quick_control_draft(self, target: str, text: str):
        if target != f"moke_box.vout{self._selected_channel()}.voltage":
            return
        if self._external_controlled or (self.busy and self._running_kind != "voltage"):
            return
        parse_quantity(text, DIMENSION_VOLTAGE)
        previous = self.target.blockSignals(True)
        self._edited_voltage_channels.add(self._selected_channel())
        self.target.setText(text)
        self.target.blockSignals(previous)
        # Live uses the same replaceable target and the same transport owner.
        self._target_changed(text) if self.live_control_switch.isChecked() else self._target_changed()

    def can_update_live_target(self, target: str) -> bool:
        return (self.busy and self._running_kind == "voltage" and self._connected
                and not self._external_controlled and self.live_control_switch.isChecked()
                and target == f"moke_box.vout{self._selected_channel()}.voltage")

    def request_quick_voltage(self, target: str, text: str):
        """Use the same authorized, reserved worker as manual Apply voltage."""
        if self.can_update_live_target(target):
            voltage = parse_quantity(text, DIMENSION_VOLTAGE).si_value
            self._manual_voltage_plan((voltage,))
            self.set_quick_control_draft(target, text)
            self._submit_live_target()
            return
        if self.busy or self._external_controlled:
            raise ConfigurationError("MOKE Box is busy or reserved by a recipe.")
        channels = [channel for channel in range(8) if target == f"moke_box.vout{channel}.voltage"]
        if not self._connected or not channels or self._manual_profile(channels[0]) is None:
            raise ConfigurationError("MOKE output channel has no approved binding.")
        parse_quantity(text, DIMENSION_VOLTAGE)
        self.channel_selector.setCurrentIndex(self.channel_selector.findData(channels[0]))
        voltage = parse_quantity(text, DIMENSION_VOLTAGE).si_value
        self._manual_voltage_plan((voltage,))  # Validate before changing the shared draft.
        self.set_quick_control_draft(target, text)
        self._start_manual()

    def request_quick_zero(self):
        if not self._connected or self._profile is None or self._external_controlled:
            raise ConfigurationError("MOKE zero ramp is unavailable; check connection and reservation.")
        self._zero()

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget):
        super().__init__(parent)
        self._controller = controller
        self._reference: DeviceController | None = None
        self._settings = settings
        self._profile: MokeControlProfile | None = None
        self._channel_drafts = {}
        self._channel_steps = {}
        self._initialized_voltage_channels = set()
        self._edited_voltage_channels = set()
        self._slider_readback_extents = {}
        self._draft_channel = settings.moke_box.voltage_control.channel
        self._reference_identity = None
        self._simulation = False
        self._thread: QThread | None = None
        self._worker = None
        self._running_kind = None
        self._cancel = Event()
        self._manual_plan = None
        self._manual_envelope = None
        self._slider_mapping = None
        self._syncing_slider = False
        self._last_voltages = {}
        self._calibration_request = None
        self._last_result: CalibrationRunResult | None = None
        self._review_model = None
        self._active_model = None
        self._authorize: Callable[[str, object], None] | None = None
        self._pause_live: Callable[[], None] | None = None
        self._connected = False
        self._reference_connected = False
        self._external_controlled = False
        self._input_controls = []
        self._live_pending = False
        self._live_targets = None
        self._last_requested_v = None
        self._quick_bounds_signature = None
        self._history_visible = True
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(400)
        self._live_timer.timeout.connect(self._submit_live_target)
        self.control_page = self._build_control(parent)
        self.calibration_page = self._build_calibration(parent)
        self._controller.result.connect(self._device_result)
        self._controller.state_changed.connect(self._state_changed)
        self._controller.error.connect(self._device_error)
        self.set_settings(settings)
        self._refresh_controls()

    @property
    def busy(self) -> bool:
        return self._thread is not None

    @property
    def has_pending_live_target(self) -> bool:
        return self._live_pending and self.live_control_switch.isChecked()

    def bind_reference(self, reference: DeviceController, *, simulation: bool,
                       authorize: Callable[[str, object], None] | None,
                       pause_live: Callable[[], None]) -> None:
        self._reference = reference
        self._simulation = simulation
        self._authorize = authorize
        self._pause_live = pause_live
        reference.result.connect(self._reference_result)
        reference.state_changed.connect(self._reference_state)

    def _scroll_page(self, parent):
        area = ScrollArea(parent)
        area.setWidgetResizable(True)
        body = QWidget(area)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 6, 2, 6)
        layout.setSpacing(12)
        area.setWidget(body)
        return area, body, layout

    def _card(self, parent, heading):
        card = CardWidget(parent)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        title = StrongBodyLabel(heading, card)
        layout.addWidget(title)
        return card, layout

    def _entry(self, form, label, text):
        entry = LineEdit()
        entry.setText(text)
        entry.setAccessibleName(label)
        if form is not None:
            form.addRow(BodyLabel(label), entry)
        self._input_controls.append(entry)
        entry.textChanged.connect(self._revoke_calibration)
        return entry

    def _build_control(self, parent):
        page = QWidget(parent)
        page.setObjectName("mokeVoltageControlPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal, page)
        self.workspace_splitter.setObjectName("mokeWorkspace")
        self.workspace_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.workspace_splitter, 1)
        page.installEventFilter(self)
        source_scroll, source_tab, content = self._scroll_page(page)
        source_pane = QWidget(page)
        source_layout = QVBoxLayout(source_pane)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(6)
        source_scroll.setObjectName("mokeControlPanel")
        source_scroll.setMinimumWidth(430)
        source_tab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content.setContentsMargins(8, 6, 8, 6)
        content.setSpacing(6)
        self.live_control_switch = SwitchButton(source_tab)
        self.live_control_switch.setObjectName("mokeLiveControlSwitch")
        self.live_control_switch.setOnText("Live control")
        self.live_control_switch.setOffText("Live control")
        self.live_control_switch.setChecked(False)
        self.live_control_switch.setToolTip(
            "Live ON: valid voltage changes are applied automatically after 400 ms. "
            "Live OFF: use Apply voltage. Enabling Live approves the selected range without sending a voltage.")
        self.live_control_switch.checkedChanged.connect(self._live_control_toggled)
        control_header = QHBoxLayout()
        control_header.addWidget(self.live_control_switch)
        control_header.addStretch()
        self.open_floating_button = PushButton("Open floating controls", source_pane)
        self.open_floating_button.setAccessibleName("Open MOKE voltage controls in a floating window")
        self.open_floating_button.clicked.connect(self.floating_requested)
        control_header.addWidget(self.open_floating_button)
        source_layout.addLayout(control_header)
        source_layout.addWidget(source_scroll, 1)
        action_footer = CardWidget(source_pane)
        source_layout.addWidget(action_footer)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        self.read_configuration_button = PushButton("Read all VOUT", source_tab)
        self.read_configuration_button.setToolTip("Open the VOUT 0–7 table and read all DAC registers. This does not change an output or measure Kepco current.")
        self.read_voltage_button = PushButton("Read selected VOUT", source_tab)
        self.read_voltage_button.setToolTip("Refresh the selected channel's confirmed DAC value and history. The protocol returns all eight registers. This does not measure current or magnetic field.")
        for button in (self.read_configuration_button, self.read_voltage_button):
            buttons.addWidget(button)
        content.addLayout(buttons)
        self.configuration_panel = MokeVoltageConfigurationPanel(source_tab)
        content.addWidget(self.configuration_panel)
        self.channel_selector = self.configuration_panel.channel
        self.channel_selector.setAccessibleName("MOKE output channel")
        self.channel_selector.currentIndexChanged.connect(self._channel_changed)
        self._input_controls.append(self.channel_selector)
        self.profile_label = self.configuration_panel.profile_summary
        self.field_readout = self.configuration_panel.calculated_field
        self.target = self.configuration_panel.level
        self.configuration_panel.voltage_step.step_changed.connect(
            lambda text: self.set_voltage_step(self._selected_channel(), text))
        self._input_controls.append(self.target)
        self.target.textChanged.connect(self._target_changed)
        self.target.returnPressed.connect(self._submit_live_target)
        self.configuration_panel.range_changed.connect(self._operator_limits_changed)
        self.configuration_panel.validation_failed.connect(self._failed)
        self.voltage_slider = Slider(Qt.Orientation.Horizontal, source_tab)
        self.voltage_slider.setAccessibleName("Programming voltage slider")
        self.voltage_slider.setToolTip("Live ON: automatically apply voltage. Live OFF: select a voltage, then click Apply voltage.")
        self.voltage_slider.valueChanged.connect(self._slider_changed)
        self.voltage_slider.sliderReleased.connect(self._submit_live_target)
        self.voltage_slider.installEventFilter(self)
        self._input_controls.append(self.voltage_slider)
        self.configuration_panel.form.insertRow(2, "Voltage slider", self.voltage_slider)
        self.initial_readback_note = CaptionLabel("", source_tab)
        self.initial_readback_note.setWordWrap(True)
        self.initial_readback_note.hide()
        self.configuration_panel.form.insertRow(3, self.initial_readback_note)
        self.manual_settling = line_edit("2 s")
        self.manual_settling.setAccessibleName("Voltage settling time")
        self.manual_settling.setToolTip("Wait after the final DAC readback before allowing the next target. Must be at least the station minimum; elapsed time does not confirm field stability.")
        self.manual_settling.textChanged.connect(self._revoke_arm)
        self._input_controls.append(self.manual_settling)
        self.configuration_panel.form.insertRow(4, "Settling time", self.manual_settling)
        workflow = CardWidget(source_tab)
        workflow.setObjectName("mokeOutputWorkflow")
        workflow_layout = QVBoxLayout(workflow)
        workflow_layout.setContentsMargins(7, 5, 7, 5)
        self.manual_status = BodyLabel("Output control is disabled until qualified.", workflow)
        self.manual_status.setWordWrap(True)
        workflow_layout.addWidget(self.manual_status)
        note = CaptionLabel("Programming voltage controls Kepco current. DAC zero does not confirm power-off or zero field.", workflow)
        note.setWordWrap(True)
        self.output_note = note
        workflow_layout.addWidget(note)
        actions = QHBoxLayout(action_footer)
        actions.setContentsMargins(8, 5, 8, 5)
        self.set_button = PrimaryPushButton("Apply voltage", action_footer)
        self.zero_button = PushButton("Turn off field", action_footer)
        actions.addWidget(self.set_button)
        actions.addWidget(self.zero_button)
        self.voltage_readout = StrongBodyLabel("Confirmed DAC: — V", workflow)
        workflow_layout.addWidget(self.voltage_readout)
        self.ramp_status = CaptionLabel("", workflow)
        self.ramp_status.setWordWrap(True)
        self.ramp_status.hide()
        workflow_layout.addWidget(self.ramp_status)
        self.ramp_progress_bar = ProgressBar(workflow)
        self.ramp_progress_bar.setRange(0, 1000)
        self.ramp_progress_bar.setValue(0)
        self.ramp_progress_bar.hide()
        workflow_layout.addWidget(self.ramp_progress_bar)
        content.addWidget(workflow)
        self.set_button.clicked.connect(self._start_manual)
        self.zero_button.clicked.connect(self._zero)
        self.read_configuration_button.clicked.connect(self._read_vout_overview)
        self.read_voltage_button.clicked.connect(lambda: self._controller.call("read_vouts"))
        content.addStretch(1)
        self.workspace_splitter.addWidget(source_pane)
        field_scroll, field_column, field_layout = self._scroll_page(page)
        field_scroll.setObjectName("mokeFieldPanel")
        field_column.setObjectName("mokeFieldColumn")
        field_column.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        field_layout.setContentsMargins(0, 0, 0, 0)
        field_layout.setSpacing(6)
        self.field_card, field_content = self._card(field_column, "Magnetic field · B(U)")
        self.field_heading = field_content.itemAt(0).widget()
        self.field_card.setObjectName("mokeCalibratedFieldCard")
        field_content.addWidget(self.field_readout)
        self.field_readout.show()
        self.field_basis = CaptionLabel("Voltage draft: — V", self.field_card)
        self.field_basis.setWordWrap(True)
        field_content.addWidget(self.field_basis)
        field_note = CaptionLabel(
            "B↑ and B↓ are predictions from the activated calibration branches. "
            "They assume the recorded full-range conditioning, rather than verified current field history. "
            "This is not a live Lake Shore measurement. Review and activate a saved calibration to show predictions.",
            self.field_card)
        field_note.setWordWrap(True)
        field_content.addWidget(field_note)
        self.field_note = field_note
        self.compact_field_note = CaptionLabel("Predictions from saved calibration; not a live Hall measurement.", self.field_card)
        self.compact_field_note.setWordWrap(True)
        self.compact_field_note.hide()
        field_content.addWidget(self.compact_field_note)
        field_layout.addWidget(self.field_card)
        history_card, history_layout = self._card(field_column, "Voltage / field · live history")
        self.history_card = history_card
        history_layout.setContentsMargins(7, 6, 7, 6)
        history_layout.setSpacing(4)
        self.voltage_history = MokeVoltageHistory(history_card, window_s=30.0)
        history_layout.addWidget(self.voltage_history, 1)
        history_actions = QHBoxLayout()
        self.clear_history_button = PushButton("Clear history", history_card)
        self.clear_history_button.clicked.connect(self.voltage_history.clear)
        history_actions.addWidget(self.clear_history_button)
        history_layout.addLayout(history_actions)
        field_layout.addWidget(history_card, 1)
        self.workspace_splitter.addWidget(field_scroll)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 7)
        self.workspace_splitter.setSizes([450, 910])
        self._source_content = content
        self._field_layout = field_layout
        self._field_scroll = field_scroll
        self._history_visible = True
        self._sync_slider()
        return page

    def _read_vout_overview(self):
        self.vout_overview_requested.emit()
        self._controller.call("read_vouts")

    def eventFilter(self, watched, event):
        if watched is getattr(self, "voltage_slider", None) and event.type() == QEvent.Type.KeyPress:
            direction = {Qt.Key.Key_Up: 1, Qt.Key.Key_Right: 1, Qt.Key.Key_Down: -1, Qt.Key.Key_Left: -1}.get(event.key())
            if direction is not None and watched.isEnabled():
                try:
                    text, voltage_v = step_quantity_text(self.target.text(), DIMENSION_VOLTAGE, direction,
                                                         step_text=self.target.property("precisionStep"))
                    bounded_v = min(max(voltage_v, self._voltage(self.configuration_panel.minimum_text)),
                                    self._voltage(self.configuration_panel.maximum_text))
                    if bounded_v != voltage_v:
                        text = render_quantity_si_like(text, DIMENSION_VOLTAGE, bounded_v)
                    self.target.setText(text)
                except ValueError:
                    pass
                event.accept()
                return True
        if watched is getattr(self, "control_page", None) and event.type() == QEvent.Type.Resize:
            self._update_workspace_orientation(watched.width())
        return super().eventFilter(watched, event)

    def _update_workspace_orientation(self, width, *, reset_sizes=False):
        orientation = Qt.Orientation.Vertical if self._history_visible and width < 640 else Qt.Orientation.Horizontal
        if hasattr(self, "compact_field_note"):
            expanded_note = self._history_visible and width >= 800
            self.field_note.setVisible(expanded_note)
            self.compact_field_note.setVisible(not expanded_note)
        if orientation != self.workspace_splitter.orientation() or reset_sizes:
            self.workspace_splitter.setOrientation(orientation)
            # A narrow stacked panel needs most of the height for its controls.
            # Reusing the wide layout's proportions hides the voltage editor.
            sizes = [650, 350] if orientation == Qt.Orientation.Vertical else [450, 910]
            self.workspace_splitter.setSizes(sizes)

    def set_history_visible(self, visible: bool):
        """Rearrange the same controls; plot toggling never communicates with hardware."""
        if visible == self._history_visible:
            return
        self._history_visible = visible
        if visible:
            self._source_content.removeWidget(self.field_card)
            self._field_layout.insertWidget(0, self.field_card)
        else:
            self._field_layout.removeWidget(self.field_card)
            self._source_content.insertWidget(3, self.field_card)
        self.field_note.setVisible(visible)
        self.field_heading.setVisible(visible)
        self.compact_field_note.setVisible(not visible)
        self.history_card.setVisible(visible)
        self._field_scroll.setVisible(visible)
        self.field_card.show()
        self._update_workspace_orientation(self.control_page.width(), reset_sizes=True)

    def _selected_channel(self):
        return self.channel_selector.currentData()

    def _default_channel_draft(self, profile):
        lo, hi = max(-.5, profile.minimum_v), min(.5, profile.maximum_v)
        if lo >= hi:
            lo, hi = profile.minimum_v, profile.maximum_v
        return f"{lo:g} V", f"{hi:g} V", f"{profile.minimum_settling_s:g} s", "0 mV"

    def _channel_defaults(self, channel):
        profile = self._manual_profile(channel)
        if profile is not None:
            return self._default_channel_draft(profile)
        setting = (self._settings.moke_box.voltage_control if channel == self._settings.moke_box.voltage_control.channel
                   else self._settings.moke_box.channel_profiles.get(str(channel)))
        if setting is None:
            return "-0.5 V", "0.5 V", "2 s", "0 mV"
        lo = max(-.5, self._voltage(setting.minimum))
        hi = min(.5, self._voltage(setting.maximum))
        if lo >= hi:
            lo, hi = self._voltage(setting.minimum), self._voltage(setting.maximum)
        return f"{lo:g} V", f"{hi:g} V", setting.minimum_settling_time, "0 mV"

    def _adopt_initial_voltages(self, values):
        """Initialize each untouched channel once, without authorizing or writing it."""
        selected_changed = False
        for channel, voltage_v in values.items():
            if channel in self._initialized_voltage_channels or not math.isfinite(voltage_v):
                continue
            self._initialized_voltage_channels.add(channel)
            if channel in self._edited_voltage_channels:
                continue
            if channel == self._selected_channel():
                lo, hi, settling, draft = (self.configuration_panel.minimum_text,
                    self.configuration_panel.maximum_text, self.manual_settling.text(), self.target.text())
            else:
                lo, hi, settling, draft = self._channel_drafts.get(channel, self._channel_defaults(channel))
            try:
                text = render_quantity_si_like(draft, DIMENSION_VOLTAGE, voltage_v, preferred_unit="mV")
                # Do not round an acquired value to a coarse operator edit precision.
                if self._voltage(text) != voltage_v:
                    text = f"{voltage_v:.17g} V"
            except ValueError:
                text = f"{voltage_v:.17g} V"
            self._channel_drafts[channel] = (lo, hi, settling, text)
            self._slider_readback_extents[channel] = voltage_v
            if channel == self._selected_channel():
                previous = self.target.blockSignals(True)
                self.target.setText(text)
                self.target.blockSignals(previous)
                selected_changed = True
            self.quick_draft_changed.emit(f"moke_box.vout{channel}.voltage", text)
        if selected_changed:
            self._sync_slider()

    def _channel_changed(self, *_args):
        self._disable_live()
        if hasattr(self, "manual_settling"):
            self._channel_drafts[self._draft_channel] = (
                self.configuration_panel.minimum_text, self.configuration_panel.maximum_text,
                self.manual_settling.text(), self.target.text())
            self._draft_channel = self._selected_channel()
            self.set_voltage_step(self._selected_channel(), self.voltage_step_text(self._selected_channel()))
            profile = self._manual_profile()
            self.configuration_panel.profile = profile
            lo, hi, settling, target = self._channel_drafts.get(
                self._selected_channel(), self._channel_defaults(self._selected_channel()))
            for editor, text in ((self.manual_settling, settling), (self.target, target)):
                previous = editor.blockSignals(True)
                editor.setText(text)
                editor.blockSignals(previous)
            self.configuration_panel.set_operator_limits(lo, hi)
        self._revoke_arm()
        if hasattr(self, "voltage_history"):
            self.voltage_history.clear()
            self.voltage_readout.setText("Confirmed DAC: — V")
            voltage = self._last_voltages.get(self._selected_channel())
            if voltage is not None:
                self._record_voltage(voltage)
            self._preview_target()
        self._refresh_profile_summary()

    def _refresh_profile_summary(self):
        profile = self._manual_profile()
        if profile is None:
            self.profile_label.setText("Read-only output: no approved binding.")
            return
        purpose = "Unconnected DAC test output" if profile.kepco_mode == "dac_test" else "Kepco coil output"
        self.output_note.setText(
            "Unconnected DAC test output. Its voltage does not represent the coil field."
            if profile.kepco_mode == "dac_test" else
            "Programming voltage controls Kepco current. DAC zero does not confirm power-off or zero field.")
        self.profile_label.setText(
            f"VOUT{profile.channel} - {purpose}\n"
            f"Station {profile.minimum_v:g} to {profile.maximum_v:g} V; ramp <= {profile.maximum_slew_v_s:g} V/s\n"
            f"{profile.binding_id}" + (" (SIMULATION)" if profile.simulation else ""))

    def _sync_slider(self, *_args):
        try:
            minimum = self._voltage(self.configuration_panel.minimum_text)
            maximum = self._voltage(self.configuration_panel.maximum_text)
            existing_v = self._slider_readback_extents.get(self._selected_channel())
            outside = existing_v is not None and not minimum <= existing_v <= maximum
            self.initial_readback_note.setVisible(outside)
            if outside:
                self.initial_readback_note.setText(
                    f"Initial DAC {existing_v:+.6g} V is outside the working range. "
                    "The slider displays this value; new targets stay within the working limits.")
                minimum, maximum = min(minimum, existing_v), max(maximum, existing_v)
            self._slider_mapping = QuantitySliderMapping(minimum, maximum, (maximum - minimum) / 10000)
            self._syncing_slider = True
            try:
                self.voltage_slider.setRange(0, self._slider_mapping.maximum_position)
            finally:
                self._syncing_slider = False
            self._target_changed()
        except (ValueError, RuntimeError):
            self._slider_mapping = None
            self.voltage_slider.setEnabled(False)

    def _slider_changed(self, position):
        if self._slider_mapping is not None and not self._syncing_slider:
            voltage = self._slider_mapping.value_for_position(position)
            voltage = min(max(voltage, self._voltage(self.configuration_panel.minimum_text)),
                          self._voltage(self.configuration_panel.maximum_text))
            self.target.setText(render_quantity_si_like(self.target.text(), DIMENSION_VOLTAGE, voltage, preferred_unit="mV"))

    def _target_changed(self, *_args):
        if _args:
            self._edited_voltage_channels.add(self._selected_channel())
        try:
            parse_quantity(self.target.text(), DIMENSION_VOLTAGE)
        except ValueError:
            pass
        else:
            self.quick_draft_changed.emit(
                f"moke_box.vout{self._selected_channel()}.voltage", self.target.text())
        try:
            voltage = self._voltage(self.target)
            if self._slider_mapping is not None:
                self._update_voltage_slider_step()
                self._syncing_slider = True
                try:
                    self.voltage_slider.setValue(self._slider_mapping.position_for_value(voltage))
                finally:
                    self._syncing_slider = False
            self._manual_plan = self._manual_voltage_plan((voltage,))
        except (ValueError, RuntimeError):
            self._manual_plan = None
        self._preview_target()
        try:
            requested_v = self._voltage(self.target)
        except ValueError:
            requested_v = None
        if requested_v != self._last_requested_v:
            self._last_requested_v = requested_v
            self.voltage_history.append_requested(requested_v)
        self._refresh_controls()
        if _args and self.live_control_switch.isChecked():
            self._live_pending = self._manual_plan is not None
            if self._live_targets is not None:
                self._live_targets.discard()  # Never execute an obsolete debounced draft.
            if self._live_pending and not self._live_timer.isActive():
                self._live_timer.start()
            elif not self._live_pending:
                self._live_timer.stop()

    def _update_voltage_slider_step(self):
        if self._slider_mapping is not None:
            step_v = quantity_step_si("0 V", DIMENSION_VOLTAGE,
                                      step_text=self.target.property("precisionStep"))
            positions = max(1, round(step_v / self._slider_mapping.step_si))
            self.voltage_slider.setSingleStep(positions)
            self.voltage_slider.setPageStep(positions * 10)

    def _disable_live(self):
        self._live_timer.stop()
        self._live_pending = False
        if self._live_targets is not None:
            self._live_targets.discard()
        self.live_control_switch.blockSignals(True)
        self.live_control_switch.setChecked(False)
        self.live_control_switch.blockSignals(False)

    def _live_control_toggled(self, enabled):
        self._live_timer.stop()
        self._live_pending = False
        if self._live_targets is not None:
            self._live_targets.discard()
        if enabled:
            if self.busy or self._external_controlled or not self._connected:
                self._disable_live()
                return
            self._prepare_manual()
            if self._manual_envelope is None:
                self._disable_live()
                return
            self.manual_status.setText("Live ON. Valid voltage changes are applied automatically after 400 ms.")
        else:
            self.manual_status.setText("Live OFF. Change voltage, then click Apply voltage.")
        self._refresh_controls()

    def _submit_live_target(self):
        self._live_timer.stop()
        if not self.live_control_switch.isChecked() or not self._live_pending:
            return
        if self.busy:
            if self._live_targets is not None and self._manual_plan is not None:
                try:
                    self._approve("ramp_vout", self._manual_plan)
                    accepted = self._live_targets.publish(self._manual_plan)
                    self.manual_status.setText(
                        "Live target updated; waiting for confirmed DAC in the background." if accepted else
                        "Latest Live target queued; current DAC confirmation is finishing.")
                except (ValueError, RuntimeError) as exc:
                    self._failed(str(exc))
                    self._cancel.set()
            return  # Keep the latest draft until completion proves it was consumed.
        self._live_pending = False
        if self._connected and not self._external_controlled and self._manual_envelope is not None:
            self._start_manual()

    def _preview_target(self):
        if not hasattr(self, "field_readout"):
            return
        try:
            voltage = self._voltage(self.target)
            if self._manual_profile() is not None:
                voltage = self._manual_voltage_plan((voltage,)).applied_voltage(voltage)
            self.show_field_preview(voltage)
        except (ValueError, RuntimeError):
            self.field_basis.setText("Voltage draft: invalid or outside the permitted range")
            self.field_readout.setText("Calculated field: enter a valid voltage")

    def _record_voltage(self, voltage):
        self._last_voltages[self._selected_channel()] = voltage
        self.voltage_confirmed.emit(self._selected_channel(), voltage)
        self.voltage_readout.setText(f"Confirmed DAC: {voltage:+.6f} V")
        model = self._active_model if self._profile and self._selected_channel() == self._profile.channel else None
        self.voltage_history.append(voltage, model)

    def show_execution_voltage(self, channel, voltage_v, calibration_document=None):
        """Render runner-confirmed voltage without querying a leased device."""
        if type(channel) is not int or channel not in range(8):
            return
        if self._selected_channel() != channel:
            self.channel_selector.setCurrentIndex(channel)
        self._last_voltages[channel] = voltage_v
        self.voltage_confirmed.emit(channel, voltage_v)
        self.voltage_readout.setText(f"Confirmed DAC: {voltage_v:+.6f} V")
        self.field_basis.setText(f"Runner-confirmed DAC: {voltage_v:+.6g} V")
        model = MokeCalibration.from_document(calibration_document) if isinstance(calibration_document, dict) else None
        self.voltage_history.append(voltage_v, model)
        if model is None:
            self.field_readout.setText("Calculated field: no calibration in this run")
            return
        try:
            up, down = model.ascending.estimate(voltage_v), model.descending.estimate(voltage_v)
            self.field_readout.setText(f"B↑ {up * 1000:+.6g} mT\nB↓ {down * 1000:+.6g} mT")
        except ConfigurationError as exc:
            self.field_readout.setText(f"Calculated field unavailable: {exc}")

    def _build_calibration(self, parent):
        page = QWidget(parent)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, body, layout = self._scroll_page(page)
        outer.addWidget(scroll, 1)
        page.setObjectName("mokeFieldCalibrationPage")
        card, content = self._card(body, "Field calibration · Lake Shore reference")
        card.setObjectName("mokeFieldCalibrationCard")
        self.reference_label = BodyLabel("Connect Lake Shore 475 in DC mode.", card)
        self.reference_label.setWordWrap(True)
        content.addWidget(self.reference_label)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.cal_minimum = self._entry(form, "Calibration minimum", "-0.5 V")
        self.cal_maximum = self._entry(form, "Calibration maximum", "0.5 V")
        self.grid_points = SpinBox(card)
        self.grid_points.setRange(3, 1001)
        self.grid_points.setValue(21)
        form.addRow(BodyLabel("Points per branch"), self.grid_points)
        self.samples = SpinBox(card)
        self.samples.setRange(2, 1000)
        self.samples.setValue(3)
        form.addRow(BodyLabel("Samples per point"), self.samples)
        self.repetitions = SpinBox(card)
        self.repetitions.setRange(1, 100)
        self.repetitions.setValue(2)
        form.addRow(BodyLabel("Repeat cycles"), self.repetitions)
        for entry in (self.grid_points, self.samples, self.repetitions):
            self._input_controls.append(entry)
            entry.valueChanged.connect(self._revoke_calibration)
        self.settling = self._entry(form, "Settling time", "2 s")
        self.sample_interval = self._entry(form, "Sample interval", "100 ms")
        self.point_timeout = self._entry(form, "Point deadline", "30 s")
        self.stability = self._entry(form, "Maximum field stddev", "100 uT")
        self.probe = self._entry(form, "Probe identity", "")
        self.orientation = self._entry(form, "Probe orientation", "")
        self.geometry = self._entry(form, "Gap / geometry", "")
        self.uncertainty = self._entry(form, "Reference uncertainty", "")
        content.addLayout(form)
        self.acquire_hall = CheckBox("Also record Hall-1 voltage and raw samples", card)
        self.acquire_hall.setChecked(True)
        self.acquire_hall.toggled.connect(self._revoke_calibration)
        self._input_controls.append(self.acquire_hall)
        content.addWidget(self.acquire_hall)
        hint = CaptionLabel(
            "One full-range conditioning cycle runs before recorded ascending and descending branches. "
            "The complete trajectory is included in the arm permission.", card)
        hint.setWordWrap(True)
        content.addWidget(hint)
        footer = CardWidget(page)
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(14, 10, 14, 10)
        actions = QHBoxLayout()
        self.arm_calibration_button = PushButton("Arm calibration", footer)
        self.start_calibration_button = PrimaryPushButton("Start calibration", footer)
        self.stop_button = PushButton("Stop", footer)
        for button in (self.arm_calibration_button, self.start_calibration_button, self.stop_button):
            actions.addWidget(button)
        footer_layout.addLayout(actions)
        self.arm_calibration_button.clicked.connect(self._arm_calibration)
        self.start_calibration_button.clicked.connect(self._start_calibration)
        self.stop_button.clicked.connect(self.stop)
        self.progress_bar = ProgressBar(footer)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        footer_layout.addWidget(self.progress_bar)
        self.calibration_status = BodyLabel("No calibration run yet.", footer)
        self.calibration_status.setWordWrap(True)
        footer_layout.addWidget(self.calibration_status)
        outer.addWidget(footer)
        layout.addWidget(card)
        review, review_layout = self._card(body, "Calibration review and activation")
        saved_row = QHBoxLayout()
        self.saved_models = ComboBox(review)
        self.saved_models.setAccessibleName("Saved field calibration")
        self.load_model_button = PushButton("Load for review", review)
        self.load_model_button.clicked.connect(self._load_model)
        saved_row.addWidget(self.saved_models, 1)
        saved_row.addWidget(self.load_model_button)
        review_layout.addLayout(saved_row)
        self.review_summary = BodyLabel("Completed models are saved without activation.", review)
        self.review_summary.setWordWrap(True)
        review_layout.addWidget(self.review_summary)
        self.reviewed = CheckBox("I reviewed the branches, repeatability and reference conditions", review)
        self.reviewed.toggled.connect(self._refresh_controls)
        review_layout.addWidget(self.reviewed)
        self.activate_button = PrimaryPushButton("Activate reviewed calibration", review)
        self.activate_button.clicked.connect(self._activate)
        review_layout.addWidget(self.activate_button)
        self.plot = pg.PlotWidget(review)
        self.plot.setMinimumHeight(230)
        self.plot.setLabel("bottom", "Programming voltage", units="V")
        self.plot.setLabel("left", "Reference field", units="T")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.addLegend()
        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(palette.background)
        for name in ("left", "bottom"):
            self.plot.getAxis(name).setPen(pg.mkPen(palette.axes))
            self.plot.getAxis(name).setTextPen(pg.mkPen(palette.axes))
        self.up_curve = self.plot.plot(name="Ascending", pen=pg.mkPen(palette.measurement, width=2))
        self.down_curve = self.plot.plot(name="Descending", pen=pg.mkPen(palette.axes, width=2))
        review_layout.addWidget(self.plot)
        layout.addWidget(review)
        return page

    def set_settings(self, settings: StationSettings):
        self._disable_live()
        if self.busy:
            self.stop()
        self._settings = settings
        self._channel_drafts.clear()
        self._initialized_voltage_channels.clear()
        self._slider_readback_extents.clear()
        self._draft_channel = settings.moke_box.voltage_control.channel
        self.manual_settling.setText(settings.moke_box.voltage_control.minimum_settling_time)
        self._manual_envelope = None
        self._manual_plan = self._calibration_request = None
        self._active_model = None
        self._review_model = None
        self.reviewed.setChecked(False)
        self._profile = None
        if self._connected:
            self._controller.call("get_control_profile")
            self._controller.call("read_vouts")
        self._refresh_controls()

    def _revoke_arm(self, *_args):
        self._disable_live()
        self._manual_envelope = None
        self._manual_plan = self._calibration_request = None
        if hasattr(self, "manual_status"):
            self.manual_status.setText(self._control_disabled_reason())
            self._target_changed()

    def _control_disabled_reason(self):
        if not self._connected:
            return "Connect MOKE Box to read DAC values. Voltage control requires a qualified station profile."
        if self._profile is None:
            return "Connected for readout only. Live, Apply voltage and DAC zero are locked: no approved voltage-control profile. Qualify the Kepco connection and limits in the station profile before enabling control."
        if self._manual_profile() is None:
            return "This output has no approved binding and remains read-only."
        if self._selected_channel() != self._profile.channel:
            return f"VOUT{self._selected_channel()}: unconnected DAC test output. Voltage limits and ramps apply; field calibration belongs to VOUT{self._profile.channel}."
        return "Apply voltage sets the draft. Live automatically applies valid edits within the working range."

    def _revoke_calibration(self, *_args):
        self._calibration_request = None
        self._refresh_controls()

    def _voltage(self, widget):
        return parse_quantity(widget if isinstance(widget, str) else widget.text(), DIMENSION_VOLTAGE).si_value

    def _manual_voltage_plan(self, targets):
        profile = self._manual_profile()
        if profile is None:
            raise ConfigurationError("This output has no approved binding.")
        settling_s = parse_quantity(self.manual_settling.text(), DIMENSION_TIME).si_value
        if settling_s < profile.minimum_settling_s:
            raise ConfigurationError("Settling time must be at least the qualified station minimum.")
        return self._make_plan(self.configuration_panel.minimum_text, self.configuration_panel.maximum_text, targets, settling_s=settling_s, profile=profile)

    def _operator_limits_changed(self, *_args):
        self._revoke_arm()
        self._sync_slider()

    def _make_plan(self, minimum, maximum, targets, *, settling_s=0.0, profile=None):
        if self._profile is None:
            raise ConfigurationError("Connect a qualified output profile first.")
        profile = self._profile if profile is None else profile
        plan = MokeVoltagePlan(profile.fingerprint, profile.channel,
                               self._voltage(minimum), self._voltage(maximum), tuple(targets), settling_s)
        plan.validate(profile)
        return plan

    def _approve(self, kind, request):
        if self._authorize is None and not self._simulation:
            raise ConfigurationError("This output workflow is not bound to the station authorization service.")
        if self._authorize is not None:
            self._authorize(kind, asdict(request))

    def _prepare_manual(self):
        try:
            if not self._connected or self._manual_profile() is None:
                raise ConfigurationError("This channel is read-only. Select the qualified Kepco output channel.")
            plan = self._manual_voltage_plan((self._voltage(self.target),))
            self._approve("arm_voltage_plan", plan)
            self._manual_envelope = (plan.profile_fingerprint, plan.channel, plan.minimum_v, plan.maximum_v, plan.settling_s)
            self._manual_plan = plan
            self.manual_status.setText("Control enabled. Click Apply voltage, or enable Live control for automatic changes.")
            self._refresh_controls()
            return plan
        except (ConfigurationError, ValueError, RuntimeError) as exc:
            self._failed(str(exc))

    def _start_manual(self):
        if self.busy or self._external_controlled:
            return
        self._live_timer.stop()
        self._live_pending = False
        plan = self._prepare_manual()
        if plan is not None:
            self._start_job("voltage", plan)

    def _make_calibration_request(self):
        if self._profile is not None and self._selected_channel() != self._profile.channel:
            raise ConfigurationError(f"Select coil VOUT{self._profile.channel} for field calibration.")
        if self._reference_identity is None or self._profile is None:
            raise ConfigurationError("Connect both MOKE-Box and Lake Shore first.")
        lo, hi = self._voltage(self.cal_minimum), self._voltage(self.cal_maximum)
        count = self.grid_points.value()
        grid = tuple(lo + (hi - lo) * index / (count - 1) for index in range(count))
        plan = self._make_plan(self.cal_minimum, self.cal_maximum, grid)
        context = CalibrationContext(
            self._profile.fingerprint, self._profile.binding_id, self._profile.channel,
            self._profile.simulation, self._reference_identity.idn, self.probe.text(),
            self.orientation.text(), self.geometry.text(),
            parse_quantity(self.uncertainty.text(), DIMENSION_MAGNETIC_FIELD).si_value,
        )
        return CalibrationRequest(
            plan, context, samples_per_point=self.samples.value(),
            settling_s=parse_quantity(self.settling.text(), DIMENSION_TIME).si_value,
            sample_interval_s=parse_quantity(self.sample_interval.text(), DIMENSION_TIME).si_value,
            point_timeout_s=parse_quantity(self.point_timeout.text(), DIMENSION_TIME).si_value,
            repetitions=self.repetitions.value(), acquire_hall=self.acquire_hall.isChecked(),
            maximum_stddev_t=parse_quantity(self.stability.text(), DIMENSION_MAGNETIC_FIELD).si_value,
        )

    def _arm_calibration(self):
        self._disable_live()
        try:
            request = self._make_calibration_request()
            request.trajectory.validate(self._profile)
            self._approve("arm_field_calibration", request)
            self._manual_envelope = None
            self._manual_plan = None
            self._calibration_request = request
            count = len(request.trajectory.targets_v)
            self.calibration_status.setText(f"Calibration armed · {count} voltage targets including conditioning.")
            self._refresh_controls()
        except (ConfigurationError, ValueError, RuntimeError) as exc:
            self._failed(str(exc))

    def _start_calibration(self):
        if self._calibration_request is not None:
            self._start_job("calibration", self._calibration_request)

    def _zero(self):
        self._disable_live()
        self._edited_voltage_channels.add(self._selected_channel())
        self._manual_envelope = None
        try:
            zero_text = render_quantity_si_like(self.target.text(), DIMENSION_VOLTAGE, 0, preferred_unit="mV")
        except ValueError:
            zero_text = "0 V"  # Invalid drafts must never prevent shutdown.
        previous = self.target.blockSignals(True)
        self.target.setText(zero_text)
        self.target.blockSignals(previous)
        self._target_changed()  # Display the zero request; actual voltage still comes only from readback.
        if self.busy:
            self.stop()
        elif self._profile is not None:
            self._start_job("zero", self._selected_channel())

    def _start_job(self, kind, request):
        if self.busy or self._external_controlled:
            return
        leases = {}
        try:
            if kind != "zero":
                self._approve("start_field_calibration" if kind == "calibration" else "ramp_vout", request)
            if self._pause_live is not None:
                self._pause_live()
            controllers = {"moke_box": self._controller}
            if kind == "calibration":
                if self._reference is None:
                    raise ConfigurationError("Lake Shore controller is unavailable.")
                controllers["lakeshore_gaussmeter"] = self._reference
            for key in sorted(controllers):
                leases[key] = controllers[key].acquire_run_lease()
        except (ConfigurationError, ValueError, RuntimeError) as exc:
            for lease in reversed(tuple(leases.values())):
                lease.release()
            self._failed(str(exc))
            return
        self._cancel = Event()
        self._manual_plan = self._calibration_request = None
        if kind == "calibration":
            self._manual_envelope = None
            self.reviewed.setChecked(False)
            self._last_result = None
            self._review_model = None
            self.progress_bar.setValue(0)
            self.progress_bar.setMaximum(2 * len(request.plan.targets_v) * request.repetitions)
        self._thread = QThread(self)
        self._running_kind = kind
        self._live_targets = MokeLiveTargets(request) if kind == "voltage" and self.live_control_switch.isChecked() else None
        self._worker = MokeFieldWorker(kind, request, leases, self._settings.moke_box.calibration_directory,
                                       self._cancel, self._live_targets)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress)
        self._worker.voltage_progress.connect(self._voltage_progress)
        self._worker.succeeded.connect(self._succeeded)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._job_finished)
        self.manual_status.setText("Returning DAC to zero…" if kind == "zero" else (
            f"Ramping voltage, then waiting {max(request.settling_s, self._manual_profile().minimum_settling_s):g} s for settling…"
            if kind == "voltage" else "Applying armed voltage trajectory…"))
        self.calibration_status.setText("Calibration running…" if kind == "calibration" else self.calibration_status.text())
        self.ramp_status.setText("Waiting for DAC readback...")
        self.ramp_status.show()
        self.ramp_progress_bar.setValue(0)
        self.ramp_progress_bar.show()
        self._refresh_controls()
        self.busy_changed.emit(True)
        self._thread.start()

    def stop(self):
        self._disable_live()
        self._manual_envelope = None
        self._cancel.set()
        if self.busy:
            self.calibration_status.setText("Stopping · waiting for DAC-zero confirmation and durable close…")
        elif self._profile is not None:
            self._zero()

    def prepare_application_shutdown(self) -> bool:
        self._disable_live()
        if self.busy:
            self.stop()
            return False
        return True

    def _job_finished(self):
        thread = self._thread
        kind = self._running_kind
        self.ramp_progress_bar.hide()
        self._worker = None
        self._running_kind = None
        self._thread = None
        self._live_targets = None
        self.busy_changed.emit(False)
        if thread is not None:
            thread.deleteLater()
        if kind == "calibration":
            try:
                self._list_saved_models()
            except (ValueError, RuntimeError, OSError) as exc:
                self._failed(str(exc))
        self._controller.call("get_control_profile")
        self._target_changed()
        if self._live_pending and self.live_control_switch.isChecked():
            self._live_timer.start()

    def _voltage_progress(self, sample):
        if not isinstance(sample, MokeRampProgress) or not self.busy:
            return
        self._last_voltages[sample.channel] = sample.actual_v
        if sample.channel != self._selected_channel():
            self.voltage_confirmed.emit(sample.channel, sample.actual_v)
            return
        self._record_voltage(sample.actual_v)
        percent = round(100 * sample.fraction)
        phase = {"zeroing": "Turning off", "ramping": "Changing voltage", "settling": "Settling"}[sample.phase]
        self.ramp_status.setText(
            f"{phase} VOUT{sample.channel}: {sample.actual_v:+.6f} V -> {sample.target_v:+.6g} V "
            f"({percent}%, {sample.elapsed_s:.1f} s)")
        self.ramp_progress_bar.setValue(round(1000 * sample.fraction))
        self.ramp_progress_bar.show()
        self.ramp_status.show()

    def _progress(self, point):
        self._record_voltage(point.actual_v)
        self.progress_bar.setValue(point.index + 1)
        self.calibration_status.setText(
            f"{point.direction} · cycle {point.cycle + 1} · {point.actual_v:+.6g} V → "
            f"{statistics.fmean(point.reference_samples_t) * 1000:+.6g} mT")

    def _succeeded(self, result):
        if isinstance(result, CalibrationRunResult):
            self._last_result = result
            model = result.model
            self._review_model = model
            self.up_curve.setData(model.ascending.voltage_v, model.ascending.field_t)
            self.down_curve.setData(model.descending.voltage_v, model.descending.field_t)
            stddev = max((*model.ascending.stddev_t, *model.descending.stddev_t))
            self.review_summary.setText(
                f"Saved {len(result.points)} points · maximum repeat stddev {stddev * 1000:.6g} mT · "
                f"reference uncertainty {model.context.reference_uncertainty_t * 1000:.6g} mT.\n"
                f"Raw data: {result.path}\nModel: {model.calibration_id}")
            self.calibration_status.setText("Calibration saved. DAC zero confirmed; Kepco power/current state remains unknown.")
            self._record_voltage(0.0)
            self.ramp_status.setText("Calibration completed; DAC zero confirmed. Actual field and Kepco power-off are not verified.")
        elif isinstance(result, MokeVoltageResult):
            if self._running_kind == "voltage" and self.live_control_switch.isChecked():
                try:
                    self._live_pending = self._manual_plan is not None and self._voltage(self.target) != result.requested_v
                except ValueError:
                    self._live_pending = False
                if not self._live_pending:
                    self._live_timer.stop()
            self._record_voltage(result.actual_v)
            self.ramp_status.setText(
                "DAC zero confirmed. Actual field and Kepco power-off are not verified."
                if result.safe_target_confirmed and self._manual_profile().kepco_mode == "current" else
                "Output at zero: DAC readback confirmed." if result.safe_target_confirmed else
                "Target voltage confirmed; settling completed.")
            self.manual_status.setText(
                "DAC zero confirmed. Kepco power/current state remains unknown." if result.safe_target_confirmed else
                f"Requested {result.requested_v:+.6g} V · applied {result.applied_v:+.6g} V · readback confirmed.")
            self._preview_target()
        self.status.emit("MOKE field workflow completed")

    def _activate(self):
        if self._review_model is None or self._profile is None:
            return
        try:
            self._active_model = MokeCalibrationRepository(self._settings.moke_box.calibration_directory).activate(
                self._review_model.calibration_id, profile_fingerprint=self._profile.fingerprint,
                simulation=self._profile.simulation, reviewed=self.reviewed.isChecked())
            self.calibration_changed.emit(self._active_model)
            self.calibration_status.setText("Reviewed calibration activated for this output profile.")
            self._disable_live()
            self._manual_envelope = None
            self._manual_plan = None
            self.voltage_history.clear()
            self._target_changed()
        except (ConfigurationError, ValueError, OSError) as exc:
            self._failed(str(exc))

    def _list_saved_models(self):
        selected = self.saved_models.currentData()
        self.saved_models.clear()
        if self._profile is None:
            return
        repository = MokeCalibrationRepository(self._settings.moke_box.calibration_directory)
        rejected = []
        for identity in repository.list_ids():
            try:
                model = repository.load(identity)
            except ConfigurationError as exc:
                rejected.append(f"{identity[:10]}: {exc}")
                continue
            if model.context.profile_fingerprint == self._profile.fingerprint and model.context.simulation == self._profile.simulation:
                self.saved_models.addItem(f"{model.created_utc[:19]} · {identity[:10]}", userData=identity)
        index = self.saved_models.findData(selected)
        if index >= 0:
            self.saved_models.setCurrentIndex(index)
        if rejected:
            message = f"Rejected {len(rejected)} saved calibration(s). {rejected[0]}"
            self.calibration_status.setText(message)
            self.status.emit(message)

    def _load_model(self):
        identity = self.saved_models.currentData()
        if not identity or self.busy:
            return
        self._review_model = None
        self.reviewed.setChecked(False)
        self._refresh_controls()
        try:
            model = MokeCalibrationRepository(self._settings.moke_box.calibration_directory).load(identity)
            if self._profile is None or model.context.profile_fingerprint != self._profile.fingerprint:
                raise ConfigurationError("Calibration does not match the connected output profile.")
            self._review_model = model
            self.reviewed.setChecked(False)
            self.up_curve.setData(model.ascending.voltage_v, model.ascending.field_t)
            self.down_curve.setData(model.descending.voltage_v, model.descending.field_t)
            self.review_summary.setText(
                f"Saved calibration: {model.created_utc}\nProbe: {model.context.probe_id}\n"
                f"Raw run: {model.raw_run_id}\nModel: {model.calibration_id}")
            self._refresh_controls()
        except (ValueError, RuntimeError, OSError) as exc:
            self._failed(str(exc))

    def show_field_preview(self, voltage_v):
        self.field_basis.setText(f"Voltage draft (DAC quantized): {voltage_v:+.6g} V")
        if self._profile is not None and self._selected_channel() != self._profile.channel:
            self.field_readout.setText(f"Field calibration belongs to coil VOUT{self._profile.channel}; no field prediction for this output.")
            return
        if self._active_model is None or self._profile is None:
            self.field_readout.setText("Predicted field: no active calibration")
            return
        try:
            up = self._active_model.ascending.estimate(voltage_v)
            down = self._active_model.descending.estimate(voltage_v)
            self.field_readout.setText(f"B↑ {up * 1000:+.6g} mT\nB↓ {down * 1000:+.6g} mT")
        except (ConfigurationError, ValueError) as exc:
            self.field_readout.setText(f"Predicted field unavailable: {exc}")

    def _failed(self, message):
        if self.busy:
            self.ramp_status.setText(f"Operation failed or interrupted: {message}\nDisplayed voltage is the last confirmed readback.")
        self.quick_failed.emit(message)
        self._disable_live()
        self._manual_envelope = None
        self._manual_plan = self._calibration_request = None
        self.manual_status.setText(message)
        self.calibration_status.setText(message)
        self.status.emit(f"MOKE field workflow: {message}")
        self._target_changed()

    def _device_result(self, operation, result):
        if operation == "connect":
            self._connected = True
            self._initialized_voltage_channels.clear()
            self._controller.call("get_control_profile")
            self._controller.call("read_vouts")
        elif operation == "get_control_profile":
            previous_profile = self._profile
            previously_qualified = self._profile is not None
            self._profile = result if isinstance(result, MokeControlProfile) else None
            self.configuration_panel.profile = self._manual_profile()
            if self._profile is not None:
                try:
                    expected = control_profile_from_settings(self._settings, simulation=self._simulation)
                    if expected.fingerprint != self._profile.fingerprint:
                        raise ConfigurationError("Reconnect after changing the MOKE output profile.")
                except (ValueError, RuntimeError) as exc:
                    self._profile = None
                    self._failed(str(exc))
            if self._profile is not None:
                profile = self._profile
                if profile == previous_profile:
                    # The adapter re-checks the connected binding after a job.
                    # An unchanged profile needs no form/catalog reconstruction.
                    self._target_changed()
                    return
                if not previously_qualified:
                    previous = self.channel_selector.blockSignals(True)
                    self.channel_selector.setCurrentIndex(profile.channel)
                    self.channel_selector.blockSignals(previous)
                    self._channel_changed()
                self.profile_label.setText(
                    f"{'SIMULATION · ' if profile.simulation else ''}VOUT {profile.channel} · "
                    f"station {profile.minimum_v:g}…{profile.maximum_v:g} V · {profile.binding_id}\n"
                    f"Ramp ≤ {profile.maximum_slew_v_s:g} V/s · step ≥ {profile.step_interval_s:g} s · settling ≥ {profile.minimum_settling_s:g} s"
                    + (" · simulated hardware waits are skipped" if profile.simulation else ""))
                if not previously_qualified:
                    self.manual_status.setText(self._control_disabled_reason())
                self._active_model = None
                try:
                    self._active_model = MokeCalibrationRepository(self._settings.moke_box.calibration_directory).active(
                        profile_fingerprint=profile.fingerprint, simulation=profile.simulation)
                    self._list_saved_models()
                    self.show_field_preview(self._voltage(self.target))
                except (ConfigurationError, ValueError) as exc:
                    self._failed(str(exc))
            else:
                self._revoke_arm()
                self.profile_label.setText("Read-only · no approved voltage-control profile.")
            self._refresh_profile_summary()
            self.profile_changed.emit(self._profile)
        elif operation == "read_vouts" and isinstance(result, dict):
            self._last_voltages.update(result)
            self._adopt_initial_voltages(result)
            for channel, voltage_v in result.items():
                if channel != self._selected_channel():
                    self.voltage_confirmed.emit(channel, voltage_v)
            voltage = result.get(self._selected_channel())
            if voltage is not None:
                self._record_voltage(voltage)
                self._preview_target()
        self._target_changed()

    def _device_error(self, operation, _message):
        if operation in {"connect", "read_vouts", "get_control_profile"}:
            self._connected = False
            self._profile = None
            self._revoke_arm()

    def _state_changed(self, state):
        if state in {"disconnected", "fault"}:
            self._connected = False
            self._profile = None
            self._revoke_arm()
        self._refresh_controls()

    def _reference_result(self, operation, result):
        if operation == "connect":
            self._reference_connected = True
            self._reference_identity = result
            self.reference_label.setText(result.idn)
        self._refresh_controls()

    def _reference_state(self, state):
        if state in {"disconnected", "fault", "unknown"}:
            self._reference_connected = False
            self._reference_identity = None
            self._calibration_request = None
        self._refresh_controls()

    def set_execution_controlled(self, controlled: bool):
        self._external_controlled = controlled
        self._revoke_arm()
        self._refresh_controls()

    def _refresh_controls(self, *_args):
        # Bounds do not change with a draft or DAC telemetry. Rebuilding every
        # QuickControls slider here interrupts gestures and floods layout work.
        bounds_signature = tuple(self.quick_control_bounds(channel) for channel in range(8))
        if bounds_signature != self._quick_bounds_signature:
            self._quick_bounds_signature = bounds_signature
            self.quick_bounds_changed.emit()
        if not hasattr(self, "activate_button"):
            return
        ready = self._connected and self._profile is not None and not self.busy and not self._external_controlled
        manual_ready = ready and self._manual_profile() is not None and self._selected_channel() in self._initialized_voltage_channels
        self.live_control_switch.setEnabled(manual_ready or (self.busy and self.live_control_switch.isChecked()))
        self.set_button.setEnabled(manual_ready and self._manual_plan is not None)
        self.read_voltage_button.setEnabled(self._connected and not self.busy and not self._external_controlled)
        self.read_configuration_button.setEnabled(self.read_voltage_button.isEnabled())
        profile = self._manual_profile()
        self.zero_button.setText("Turn off field" if profile is not None and profile.kepco_mode == "current" else "Turn off output")
        self.zero_button.setEnabled(self._connected and profile is not None and not self._external_controlled
                                   and self._running_kind != "zero")
        reason = self._control_disabled_reason()
        for button in (self.set_button, self.zero_button, self.live_control_switch):
            if not button.isEnabled():
                button.setToolTip("Operation in progress or reserved by a recipe." if self.busy or self._external_controlled else reason)
            else:
                button.setToolTip("Keep editing while DAC confirmation runs in the background. Only the latest valid target is applied." if button is self.live_control_switch else
                                  f"Ramp selected VOUT{self._selected_channel()} to zero; this does not confirm Kepco power-off." if button is self.zero_button else
                                  "Send the draft target through the approved voltage ramp.")
        calibration_ready = ready and self._reference_connected and self._selected_channel() == self._profile.channel and self._profile.channel in self._initialized_voltage_channels
        self.arm_calibration_button.setEnabled(calibration_ready)
        self.start_calibration_button.setEnabled(calibration_ready and self._calibration_request is not None)
        self.stop_button.setEnabled(self.busy)
        self.activate_button.setEnabled(ready and self._review_model is not None and self.reviewed.isChecked())
        self.load_model_button.setEnabled(ready and self.saved_models.count() > 0)
        self.saved_models.setEnabled(ready)
        live_draft = self.busy and self._running_kind == "voltage"
        editable = (not self.busy or live_draft) and not self._external_controlled and self._manual_profile() is not None
        # Set each input directly to its final state. Disabling a pressed
        # slider/active editor and then re-enabling it interrupts the gesture.
        for control in self._input_controls:
            if control is self.target or control is self.voltage_slider:
                continue
            control.setEnabled(not self.busy and not self._external_controlled)
        self.configuration_panel.level_field.setEnabled(editable)
        self.target.setEnabled(editable)
        self.configuration_panel.level_field.edit_button.setEnabled(not self.busy and editable)
        self.configuration_panel.level_field.range_pill.setEnabled(not self.busy and editable)
        self.voltage_slider.setEnabled(editable and self._slider_mapping is not None)
