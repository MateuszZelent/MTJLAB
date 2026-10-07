"""Read-only action-index projection of the accepted execution plan."""

from dataclasses import dataclass
from bisect import bisect_right
from html import escape
import math
from pprint import pformat

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import BodyLabel, CardWidget, CheckBox, PlainTextEdit, PushButton, SpinBox, StrongBodyLabel, isDarkTheme

from app.domain.quantities import format_quantity_auto
from app.recipes.parameter_registry import PARAMETERS_BY_TARGET
from app.ui.design_system import tokens_for, plot_theme
from app.ui.widgets.plot_ownership import coalesce_plot_refresh, create_plot_widget


@dataclass
class PlannedSeries:
    target: str
    steps: list[int]
    values: list[float]


def action_values(action):
    """Explicit mutations, including configuration and authored shutdowns."""
    p = action.payload
    kind = action.kind
    channel = p.get("channel")
    values = {}
    if kind == "configure_keithley":
        request = p["request"]
        channel = request.channel
        values[f"keithley.{channel}.output"] = 0.0
        if request.mode in {"current", "voltage"} and (request.changed_fields is None or "level_si" in request.changed_fields):
            values[f"keithley.{channel}.{request.mode}"] = request.level_si
    elif kind in {"configure_rigol", "configure_rigol_output"}:
        config = p["config"]
        values[f"rigol.{config.channel}.output"] = 0.0
        if kind == "configure_rigol":
            for key, field in (("frequency", "frequency_hz"), ("high_level", "high_level_v"), ("low_level", "low_level_v")):
                values[f"rigol.{config.channel}.{key}"] = getattr(config, field)
    elif kind == "configure_anritsu":
        config = p["config"]
        for key, field in (("start_frequency", "start_hz"), ("stop_frequency", "stop_hz"), ("reference_level", "reference_level_dbm"), ("points", "points")):
            if config.changed_fields is None or field in config.changed_fields:
                values[f"anritsu.spectrum.{key}"] = getattr(config, field)
    elif kind == "update_keithley_level":
        values[f"keithley.{channel}.{p['mode']}"] = p["level_si"]
    elif kind == "update_moke_voltage":
        values[f"moke_box.vout{channel}.voltage"] = p["voltage_v"]
    elif kind == "update_rigol_frequency":
        values[f"rigol.{channel}.frequency"] = p["frequency_hz"]
    elif kind == "update_rigol_levels":
        values.update({f"rigol.{channel}.{key}": p[key + "_v"] for key in ("high_level", "low_level")})
    elif kind in {"set_keithley_output", "set_rigol_output"}:
        device = "keithley" if "keithley" in kind else "rigol"
        values[f"{device}.{channel}.output"] = float(p["enabled"])
    return values


def planned_series(actions):
    """Compress held values; absent initial values remain unknown, never zero."""
    series = {}
    prior_context = {}
    held = {}
    modes = {}
    moke_safe = {}
    active_moke = None
    for action in actions:
        profile = action.payload.get("profile")
        if action.kind == "configure_moke_box" and profile is not None:
            moke_safe[f"moke_box.vout{profile.channel}.voltage"] = profile.safe_v
    for step, action in enumerate(actions, 1):
        context = dict(action.setpoints_si)
        values = {k: v for k, v in context.items() if prior_context.get(k) != v}
        prior_context = context
        values.update(action_values(action))
        request = action.payload.get("request")
        if action.kind == "configure_moke_box":
            active_moke = f"moke_box.vout{action.payload['profile'].channel}.voltage"
        if action.kind == "configure_keithley" and request is not None:
            modes[request.channel] = request.mode
        if action.kind == "ramp_keithley_to_zero":
            channel = action.payload.get("channel")
            mode = modes.get(channel)
            if mode in {"current", "voltage"}:
                values[f"keithley.{channel}.{mode}"] = 0.0
        if action.kind == "stop_moke_voltage":
            if active_moke in moke_safe:
                values[active_moke] = moke_safe[active_moke]
            elif len(moke_safe) == 1:
                values.update(moke_safe)
        for target, value in values.items():
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            if target not in PARAMETERS_BY_TARGET and not target.endswith(".output"):
                continue
            if held.get(target) == value:
                continue
            held[target] = value
            row = series.setdefault(target, PlannedSeries(target, [], []))
            row.steps.append(step)
            row.values.append(float(value))
    return tuple(series.values())


def format_planned(target, value):
    if target.endswith(".output"):
        return "ON" if value else "OFF"
    descriptor = PARAMETERS_BY_TARGET[target]
    return format_quantity_auto(value, descriptor.dimension)


class ExecutionPlanTimeline(CardWidget):
    """Static curves plus one moving cursor; no curve rebuild per event."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.actions = ()
        self.series = ()
        self.current_step = 0
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("Execution plan · action timeline", self))
        header.addStretch()
        self.follow = CheckBox("Follow current action", self)
        self.follow.setChecked(True)
        header.addWidget(self.follow)
        self.step = SpinBox(self)
        self.step.setRange(1, 1)
        self.step.setAccessibleName("Inspect planned action")
        header.addWidget(self.step)
        layout.addLayout(header)
        note = BodyLabel("Planned setpoints, not measured traces. Each coloured lane has its own range. "
                         "X = action number, not time; vertical steps do not imply a physical ramp.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.plot = create_plot_widget(self)
        coalesce_plot_refresh(self.plot)
        self.plot.setMinimumHeight(260)
        self.plot.setMaximumHeight(420)
        self.plot.setLabel("bottom", "Action number")
        self.plot.setMouseEnabled(x=True, y=False)
        layout.addWidget(self.plot)
        self.legend = BodyLabel("", self)
        self.legend.setWordWrap(True)
        layout.addWidget(self.legend)
        self.details = BodyLabel("No accepted plan yet.", self)
        self.details.setWordWrap(True)
        # A changing setpoint must not resize the whole scrollable execution
        # page on every frame. Full details remain available on hover.
        self.details.setFixedHeight(48)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.settings_button = PushButton("Action settings (SI)", self)
        self.settings_button.setCheckable(True)
        layout.addWidget(self.settings_button)
        self.settings = PlainTextEdit(self)
        self.settings.setReadOnly(True)
        self.settings.setFixedHeight(130)
        self.settings.hide()
        layout.addWidget(self.settings)
        self.settings_button.toggled.connect(self._show_settings)
        self.shutdown = BodyLabel("", self)
        self.shutdown.setWordWrap(True)
        layout.addWidget(self.shutdown)
        self.step.valueChanged.connect(self.inspect_step)
        self.follow.toggled.connect(lambda enabled: self.set_current_step(self.current_step) if enabled else None)
        self.hide()

    def set_plan(self, actions, *, shutdown_actions=(), execution_mode="measurement"):
        self.actions = tuple(actions)
        self.setVisible(bool(self.actions))
        self.series = planned_series(self.actions)
        # Ranges are known from the complete plan. Recomputing all existing
        # curves' bounds after every addItem makes construction quadratic.
        self.plot.disableAutoRange()
        self.plot.clear()
        theme = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(theme.background)
        ticks = [(0, "Actions")]
        # Distinct parameter colours; plots retain SI-derived engineering labels.
        colours = ("#1677ff", "#d46b08", "#389e0d", "#9254de", "#c41d7f", "#08979c")
        legends = []
        for lane, row in enumerate(self.series, 1):
            lo, hi = min(row.values), max(row.values)
            label = PARAMETERS_BY_TARGET[row.target].ui_label if row.target in PARAMETERS_BY_TARGET else row.target
            ticks.append((lane, f"{label} [{format_planned(row.target, lo)} … {format_planned(row.target, hi)}]"))
            xs, ys = [], []
            for i, (step, value) in enumerate(zip(row.steps, row.values)):
                y = lane + (0.65 * (value - lo) / (hi - lo) - 0.325 if hi != lo else 0)
                end = row.steps[i + 1] if i + 1 < len(row.steps) else len(self.actions) + 1
                xs.extend((step, end))
                ys.extend((y, y))
            self.plot.plot(xs, ys, pen=pg.mkPen(colours[(lane - 1) % len(colours)], width=2))
            legends.append(f'<span style="color:{colours[(lane - 1) % len(colours)]}">● {escape(row.target)}</span>')
        self.legend.setText(" &nbsp; ".join(legends))
        self.plot.getAxis("left").setTicks([ticks])
        self.plot.setYRange(-0.5, max(1, len(self.series)) + 0.6, padding=0)
        self.plot.setXRange(1, max(2, len(self.actions) + 1), padding=0)
        def marker_style(action):
            if action.kind.startswith("set_") and "output" in action.kind:
                return ("t1", "#389e0d") if action.payload.get("enabled") else ("t", "#cf1322")
            if action.kind.startswith("configure_"):
                return "s", "#9254de"
            if action.kind.startswith(("acquire_", "measure_")):
                return "o", "#08979c"
            if action.kind == "wait":
                return "o", "#8c8c8c"
            return "d", "#1677ff"
        marker_groups = {}
        for step, action in enumerate(self.actions, 1):
            marker_groups.setdefault(marker_style(action), []).append(step)
        self.legend.setText(self.legend.text() + '<br>Actions: ▲ ON · ▼ OFF · ■ Configuration · ● Measurement / wait · ◆ Other')
        # One shared symbol/brush per category. Passing a brush per action
        # constructs thousands of QBrush objects and atlas entries on the GUI.
        for (symbol, colour), steps in marker_groups.items():
            markers = pg.ScatterPlotItem(
                x=steps, y=[0] * len(steps), size=6, pen=None,
                brush=colour, symbol=symbol, hoverable=True, data=steps,
                tip=lambda x, y, data: self.action_description(int(data)),
            )
            markers.sigClicked.connect(self._clicked)
            self.plot.addItem(markers)
        self.cursor = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen(colours[0], width=2))
        self.plot.addItem(self.cursor)
        self.step.setRange(1, max(1, len(self.actions)))
        self.step.setValue(1)
        self.shutdown.setText("Guaranteed shutdown (after plan or on stop/fault): " + (", ".join(shutdown_actions) or "see run safety policy"))
        if execution_mode == "dry_run":
            self.shutdown.setText("DRY RUN: outputs forced OFF; curves show authored intent. " + self.shutdown.text())
        self.current_step = 0
        self.inspect_step(1)

    def action_description(self, step):
        if not self.actions:
            return "No accepted plan yet."
        action = self.actions[step - 1]
        values = []
        for row in self.series:
            index = bisect_right(row.steps, step) - 1
            if index >= 0:
                values.append(f"{row.target}: {format_planned(row.target, row.values[index])}")
        return f"Action {step}/{len(self.actions)} · {action.kind} · {action.node_id}" + (" · FINALLY" if action.is_finally else "") + "\n" + " · ".join(values)

    def inspect_step(self, step):
        description = self.action_description(step)
        if self.details.text() != description:
            self.details.setText(description)
            self.details.setToolTip(description)
        if self.settings_button.isChecked() and self.actions:
            self.settings.setPlainText(pformat(self.actions[step - 1].payload, sort_dicts=False))

    def _show_settings(self, visible):
        self.settings.setVisible(visible)
        self.inspect_step(self.step.value())

    def _clicked(self, _item, points, _event):
        if points:
            self.follow.setChecked(False)
            self.step.setValue(int(points[0].data()))

    def set_current_step(self, step):
        if not self.actions or not hasattr(self, "cursor"):
            return
        next_step = max(0, min(int(step), len(self.actions)))
        if self.current_step == next_step and (
            not self.follow.isChecked() or self.step.value() == next_step
        ):
            return
        self.current_step = next_step
        self.cursor.setValue(self.current_step)
        if self.follow.isChecked() and self.current_step:
            self.step.setValue(self.current_step)
