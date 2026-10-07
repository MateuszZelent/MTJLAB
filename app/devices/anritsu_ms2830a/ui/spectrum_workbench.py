"""Local scientific inspection tools for the completed Live spectrum."""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from app.domain.quantities import (
    DIMENSION_FREQUENCY,
    format_quantity_auto,
    parse_quantity,
)
from app.ui.design_system import plot_theme, tokens_for
from app.ui.widgets import SpectrumPlotWidget
from .spectrum_controls import format_plot_amplitude, parse_plot_amplitude


class SpectrumWorkbench(SpectrumPlotWidget):
    """Display-only ranges, draggable markers and a sampled band inspector."""

    display_resumed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        self.fixed_ranges: dict[str, tuple[float, float]] = {}
        self.amplitude_unit = "dBm"
        self._trace_units: dict[str, str] = {}
        self.frequency_markers: dict[str, pg.InfiniteLine] = {}
        self._next_marker = 1
        self._hold_frequencies: np.ndarray | None = None
        self._refreshing = False
        self._restoring_ranges = False
        super().__init__(parent=parent, legend=True)
        self.toolbar_buttons[0].setToolTip("Fit unlocked axes to finite traces; preserve fixed X/Y limits.")
        # Keep every tool reachable beside the inspector at smaller widths.
        root = self.layout()
        toolbar = root.takeAt(0).layout()
        tool_grid = QGridLayout()
        tool_grid.setSpacing(4)
        for index, button in enumerate(self.toolbar_buttons):
            toolbar.removeWidget(button)
            tool_grid.addWidget(button, index // 4, index % 4)
        toolbar.removeWidget(self.readout)
        toolbar.deleteLater()
        root.insertLayout(0, tool_grid)
        root.insertWidget(1, self.readout)
        self.tools = ScrollArea(parent)
        self.tools.setObjectName("spectrumAnalysisTools")
        self.tools.setWidgetResizable(True)
        self.tools.setFrameShape(QFrame.Shape.NoFrame)
        self.tools.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tools.setMinimumWidth(300)
        content = QWidget()
        self.tools.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        layout.addWidget(StrongBodyLabel("View and analysis", content))
        note = BodyLabel("Ranges change this view only. Drag marker lines and band edges to inspect the trace.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.freeze = CheckBox("Freeze displayed spectrum", content)
        self.freeze.setToolTip("Pause this preview while acquisition continues. Unfreeze to show the latest completed frame.")
        self.freeze.toggled.connect(self._freeze_changed)
        layout.addWidget(self.freeze)
        self.axis_controls: dict[str, tuple[CheckBox, LineEdit, LineEdit]] = {}
        self.axis_feedback: dict[str, CaptionLabel] = {}
        for axis, title, defaults in (
            ("x", "Frequency range", ("1 MHz", "10 MHz")),
            ("y", "Amplitude range", ("-100 dBm", "0 dBm")),
        ):
            layout.addWidget(StrongBodyLabel(title, content))
            locked = CheckBox("Fixed range", content)
            locked.setToolTip("Keep these limits through new Live frames, Reset and mouse zoom.")
            lower, upper = LineEdit(content), LineEdit(content)
            lower.setText(defaults[0])
            upper.setText(defaults[1])
            lower.setAccessibleName(f"{title} minimum")
            upper.setAccessibleName(f"{title} maximum")
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(8)
            grid.addWidget(locked, 0, 0, 1, 2)
            grid.addWidget(BodyLabel("Minimum"), 1, 0)
            grid.addWidget(lower, 1, 1)
            grid.addWidget(BodyLabel("Maximum"), 2, 0)
            grid.addWidget(upper, 2, 1)
            apply = PushButton("Apply range", content)
            grid.addWidget(apply, 3, 0, 1, 2)
            message = CaptionLabel("", content)
            message.setWordWrap(True)
            message.hide()
            grid.addWidget(message, 4, 0, 1, 2)
            self.axis_feedback[axis] = message
            layout.addLayout(grid)
            self.axis_controls[axis] = locked, lower, upper
            apply.clicked.connect(lambda _checked=False, selected=axis: self.apply_axis_range(selected))
            lower.returnPressed.connect(lambda selected=axis: self.apply_axis_range(selected))
            upper.returnPressed.connect(lambda selected=axis: self.apply_axis_range(selected))
            locked.toggled.connect(lambda checked, selected=axis: self._lock_axis(selected, checked))
        self.fit_button = PushButton("Fit unlocked axes", content)
        self.fit_button.clicked.connect(self.auto_range)
        layout.addWidget(self.fit_button)
        layout.addWidget(StrongBodyLabel("Measurement trace", content))
        self.trace_selector = ComboBox(content)
        self.trace_selector.setAccessibleName("Trace used for marker and band measurements")
        self.trace_selector.currentIndexChanged.connect(self._measurement_trace_changed)
        layout.addWidget(self.trace_selector)
        layout.addWidget(StrongBodyLabel("Frequency markers", content))
        self.marker_frequency = LineEdit(content)
        self.marker_frequency.setPlaceholderText("Frequency, e.g. 2.45 GHz")
        self.marker_frequency.setAccessibleName("New marker frequency")
        layout.addWidget(self.marker_frequency)
        row = QHBoxLayout()
        self.add_marker_button = PushButton("Add line", content)
        self.add_peak_button = PushButton("At visible peak", content)
        row.addWidget(self.add_marker_button)
        row.addWidget(self.add_peak_button)
        layout.addLayout(row)
        self.add_marker_button.clicked.connect(self.add_marker_from_editor)
        self.marker_frequency.returnPressed.connect(self.add_marker_from_editor)
        self.add_peak_button.clicked.connect(self.add_peak_marker)
        self.marker_selector = ComboBox(content)
        self.marker_selector.setAccessibleName("Selected frequency marker")
        self.marker_selector.currentIndexChanged.connect(self._select_marker)
        layout.addWidget(self.marker_selector)
        row = QHBoxLayout()
        self.move_marker_button = PushButton("Move to frequency", content)
        self.remove_marker_button = PushButton("Remove line", content)
        row.addWidget(self.move_marker_button)
        row.addWidget(self.remove_marker_button)
        layout.addLayout(row)
        self.move_marker_button.clicked.connect(self.move_selected_marker)
        self.remove_marker_button.clicked.connect(self.remove_selected_marker)
        self.snap_markers = CheckBox("Snap lines to measured samples", content)
        self.snap_markers.setChecked(True)
        layout.addWidget(self.snap_markers)
        self.marker_readout = BodyLabel("No frequency markers.", content)
        self.marker_readout.setWordWrap(True)
        self.marker_readout.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.marker_readout)
        layout.addWidget(StrongBodyLabel("Band inspection", content))
        self.band_enabled = CheckBox("Show movable frequency band", content)
        layout.addWidget(self.band_enabled)
        band_grid = QGridLayout()
        self.band_start, self.band_stop = LineEdit(content), LineEdit(content)
        self.band_start.setPlaceholderText("Start, e.g. 1 MHz")
        self.band_stop.setPlaceholderText("Stop, e.g. 5 MHz")
        self.band_start.setAccessibleName("Inspection band start frequency")
        self.band_stop.setAccessibleName("Inspection band stop frequency")
        band_grid.addWidget(self.band_start, 0, 0)
        band_grid.addWidget(self.band_stop, 0, 1)
        self.apply_band_button = PushButton("Apply band", content)
        band_grid.addWidget(self.apply_band_button, 1, 0, 1, 2)
        layout.addLayout(band_grid)
        self.band_readout = BodyLabel("Enable the band to inspect measured samples.", content)
        self.band_readout.setWordWrap(True)
        self.band_readout.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.band_readout)
        self.feedback = BodyLabel("", content)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        layout.addStretch(1)
        palette = plot_theme(tokens_for(self._theme_name))
        self.band = pg.LinearRegionItem(values=(1e6, 5e6), pen=pg.mkPen(palette.reference))
        for line in self.band.lines:
            line.setBounds((0, None))
        self.plot.addItem(self.band, ignoreBounds=True)
        self.band.hide()
        self.band.sigRegionChanged.connect(self.refresh_measurements)
        self.band.sigRegionChangeFinished.connect(self._sync_band_editors)
        self.band_enabled.toggled.connect(self._toggle_band)
        self.apply_band_button.clicked.connect(self.apply_band)
        self.band_start.returnPressed.connect(self.apply_band)
        self.band_stop.returnPressed.connect(self.apply_band)
        self._sync_marker_actions()
        self.apply_theme(self._theme_name)
        self.plot.getViewBox().sigRangeChanged.connect(self._view_range_changed)

    @property
    def frozen(self) -> bool:
        return self.freeze.isChecked()

    def _freeze_changed(self, checked: bool) -> None:
        self.feedback.setText("Preview frozen; acquisition continues." if checked else "Showing the latest completed spectrum.")
        if not checked:
            self.display_resumed.emit()

    def _say_error(self, error: Exception) -> None:
        self.feedback.setText(str(error))

    @staticmethod
    def _frequency(text: str) -> float:
        value = parse_quantity(text, DIMENSION_FREQUENCY).si_value
        if not math.isfinite(value) or value < 0:
            raise ValueError("Frequency must be finite and non-negative, with an explicit unit.")
        return value

    def _amplitude(self, text: str) -> float:
        return parse_plot_amplitude(text, self.amplitude_unit)

    def _format_amplitude(self, value: float) -> str:
        return format_plot_amplitude(value, self.amplitude_unit)

    def set_trace_unit(self, name: str, unit: str) -> None:
        if self._trace_units.get(name, unit) != unit and name == self._hold_source:
            self.clear_holds()
        self._trace_units[name] = unit

    def _measurement_unit(self) -> str:
        return self._trace_units.get(self.trace_selector.currentText(), self.amplitude_unit)

    def _format_measurement(self, value: float) -> str:
        return f"{value:.9g} {self._measurement_unit()}"

    def apply_axis_range(self, axis: str) -> bool:
        locked, lower, upper = self.axis_controls[axis]
        try:
            # Viewport padding may extend below 0 Hz. This is a display bound,
            # not an instrument frequency or a sampled marker position.
            parse = (lambda text: parse_quantity(text, DIMENSION_FREQUENCY).si_value) if axis == "x" else self._amplitude
            limits = parse(lower.text()), parse(upper.text())
            if limits[0] >= limits[1]:
                raise ValueError("Minimum must be smaller than maximum; previous range preserved.")
        except (ValueError, TypeError) as error:
            self._say_error(error)
            self.axis_feedback[axis].setText(str(error))
            self.axis_feedback[axis].show()
            return False
        self.fixed_ranges[axis] = limits
        locked.blockSignals(True)
        locked.setChecked(True)
        locked.blockSignals(False)
        self._restore_fixed_ranges()
        self.axis_feedback[axis].hide()
        self.feedback.setText(f"Fixed {axis.upper()} range applied.")
        return True

    def _lock_axis(self, axis: str, checked: bool) -> None:
        if checked:
            view = self.plot.viewRange()[0 if axis == "x" else 1]
            self.fixed_ranges[axis] = tuple(view)
            self._sync_axis_editors(axis, tuple(view))
        else:
            self.fixed_ranges.pop(axis, None)
        self._restore_fixed_ranges()

    def _sync_axis_editors(self, axis: str, limits: tuple[float, float]) -> None:
        _, lower, upper = self.axis_controls[axis]
        formatter = (lambda value: format_quantity_auto(value, DIMENSION_FREQUENCY)) if axis == "x" else self._format_amplitude
        lower.setText(formatter(limits[0]))
        upper.setText(formatter(limits[1]))

    def _restore_fixed_ranges(self) -> None:
        if self._restoring_ranges:
            return
        self._restoring_ranges = True
        try:
            view = self.plot.getViewBox()
            for axis, limits in self.fixed_ranges.items():
                view.setRange(**{f"{axis}Range": limits}, padding=0)
            view.setMouseEnabled(x="x" not in self.fixed_ranges, y="y" not in self.fixed_ranges)
        finally:
            self._restoring_ranges = False

    def _view_range_changed(self, *_args) -> None:
        if self._restoring_ranges:
            return
        ranges = self.plot.viewRange()
        if any(tuple(ranges[0 if axis == "x" else 1]) != limits for axis, limits in self.fixed_ranges.items()):
            self._restore_fixed_ranges()

    def auto_range(self) -> None:
        previous = self.plot.viewRange()
        super().auto_range()
        self._restore_fixed_ranges()
        for axis, index in (("x", 0), ("y", 1)):
            if axis in self.fixed_ranges:
                continue
            current = self.plot.viewRange()[index]
            if current != previous[index]:
                self._sync_axis_editors(axis, tuple(current))

    def peak_search(self) -> None:
        """Search the selected measurement trace within the visible X range."""
        data = self._measurement_data()
        if data is None:
            self.status_changed.emit("Peak search unavailable: no measured trace.")
            return
        x, y = data
        lower, upper = self.plot.viewRange()[0]
        indices = np.flatnonzero((x >= lower) & (x <= upper))
        if not indices.size:
            self.status_changed.emit("Peak search unavailable: no measured samples in view.")
            return
        index = indices[int(np.argmax(y[indices]))]
        self.marker.setPos(float(x[index]))
        self.marker.show()
        self.status_changed.emit(
            f"Peak: {format_quantity_auto(float(x[index]), DIMENSION_FREQUENCY)}, "
            f"{self._format_measurement(float(y[index]))}"
        )

    def set_labels(self, **kwargs) -> None:
        unit = kwargs.get("y_unit", "dBm")
        if unit != self.amplitude_unit:
            # A ratio and absolute dBm are different physical dimensions.
            self.fixed_ranges.pop("y", None)
            self.amplitude_unit = unit
            if hasattr(self, "axis_controls"):
                self.axis_controls["y"][0].setChecked(False)
                self._sync_axis_editors("y", (-100, 0) if unit == "dBm" else (0, 1))
                self.feedback.setText("Amplitude unit changed; Y range unlocked. Set limits in the new unit.")
        super().set_labels(**kwargs)
        if hasattr(self, "band"):
            self.refresh_measurements()

    def set_trace(self, name: str, x: object, y: object, **kwargs) -> None:
        selected = self.trace_selector.currentText() if hasattr(self, "trace_selector") else ""
        kwargs["primary"] = name == (selected or name)
        super().set_trace(name, x, y, **kwargs)
        self._restore_fixed_ranges()
        if hasattr(self, "trace_selector"):
            self._sync_trace_choices()
            self.refresh_measurements()

    def _update_holds(self, x_values: np.ndarray, y_values: np.ndarray) -> None:
        if self._hold_frequencies is not None and not np.array_equal(self._hold_frequencies, x_values):
            # Holds compare the same measured frequency bins, never array indices
            # from a previous sweep range that happened to have the same length.
            self._max_hold = None
            self._min_hold = None
        self._hold_frequencies = x_values.copy()
        super()._update_holds(x_values, y_values)

    def clear_trace(self, name: str) -> None:
        super().clear_trace(name)
        if hasattr(self, "trace_selector"):
            self._sync_trace_choices()
            self.refresh_measurements()

    def _sync_trace_choices(self) -> None:
        names = [name for name in self._traces if name not in {"Max hold", "Min hold"}]
        current_names = [self.trace_selector.itemText(index) for index in range(self.trace_selector.count())]
        if names == current_names:
            return
        selected = self.trace_selector.currentText()
        self.trace_selector.blockSignals(True)
        self.trace_selector.clear()
        self.trace_selector.addItems(names)
        if selected in names:
            self.trace_selector.setCurrentText(selected)
        self.trace_selector.blockSignals(False)
        if self.trace_selector.currentText() != selected:
            self._measurement_trace_changed()

    def _measurement_data(self):
        return self._traces.get(self.trace_selector.currentText())

    def _measurement_trace_changed(self, *_args) -> None:
        self.clear_holds()
        self._hold_source = self.trace_selector.currentText() or None
        self._hold_frequencies = None
        self.refresh_measurements()

    def _sample(self, frequency_hz: float):
        data = self._measurement_data()
        if data is None or not data[0].size:
            return None
        x, y = data
        if frequency_hz < np.min(x) or frequency_hz > np.max(x):
            return None
        index = int(np.argmin(np.abs(x - frequency_hz)))
        return float(x[index]), float(y[index])

    def add_frequency_marker(self, frequency_hz: float) -> str:
        if not math.isfinite(frequency_hz) or frequency_hz < 0:
            raise ValueError("Marker frequency must be finite and non-negative.")
        name = f"M{self._next_marker}"
        self._next_marker += 1
        palette = plot_theme(tokens_for(self._theme_name))
        line = pg.InfiniteLine(pos=frequency_hz, angle=90, movable=True,
                               pen=pg.mkPen(palette.reference, width=2), label=name,
                               labelOpts={"position": 0.9 - 0.07 * ((self._next_marker - 2) % 4), "color": palette.axes})
        line.setBounds((0, None))
        self.frequency_markers[name] = line
        self.plot.addItem(line, ignoreBounds=True)
        line.sigPositionChanged.connect(self.refresh_measurements)
        line.sigPositionChangeFinished.connect(lambda: self._finish_marker_drag(name))
        self.marker_selector.addItem(name)
        self.marker_selector.setCurrentText(name)
        self._sync_marker_actions()
        self.refresh_measurements()
        return name

    def add_marker_from_editor(self) -> None:
        try:
            self.add_frequency_marker(self._frequency(self.marker_frequency.text()))
            self.feedback.setText("Marker added. Drag its line to move it.")
        except (ValueError, TypeError) as error:
            self._say_error(error)

    def add_peak_marker(self) -> None:
        data = self._measurement_data()
        if data is None:
            self.feedback.setText("No measured samples available.")
            return
        x, y = data
        lower, upper = self.plot.viewRange()[0]
        selected = np.flatnonzero((x >= lower) & (x <= upper))
        if not selected.size:
            self.feedback.setText("No measured samples in the visible frequency range.")
            return
        index = selected[int(np.argmax(y[selected]))]
        self.add_frequency_marker(float(x[index]))

    def _select_marker(self, *_args) -> None:
        line = self.frequency_markers.get(self.marker_selector.currentText())
        if line:
            self.marker_frequency.setText(format_quantity_auto(float(line.value()), DIMENSION_FREQUENCY))

    def move_selected_marker(self) -> None:
        line = self.frequency_markers.get(self.marker_selector.currentText())
        if line is None:
            return
        try:
            line.setPos(self._frequency(self.marker_frequency.text()))
        except (ValueError, TypeError) as error:
            self._say_error(error)

    def _finish_marker_drag(self, name: str) -> None:
        line = self.frequency_markers[name]
        sample = self._sample(float(line.value()))
        if self.snap_markers.isChecked() and sample:
            line.setPos(sample[0])
        self._select_marker()

    def remove_selected_marker(self) -> None:
        name = self.marker_selector.currentText()
        line = self.frequency_markers.pop(name, None)
        if line is not None:
            self.plot.removeItem(line)
            self.marker_selector.removeItem(self.marker_selector.currentIndex())
        self._sync_marker_actions()
        self.refresh_measurements()

    def _sync_marker_actions(self) -> None:
        available = bool(self.frequency_markers)
        self.move_marker_button.setEnabled(available)
        self.remove_marker_button.setEnabled(available)

    def _toggle_band(self, enabled: bool) -> None:
        self.band.setVisible(enabled)
        if enabled and not self.band_start.text():
            lower, upper = self.plot.viewRange()[0]
            span = upper - lower
            self.band.setRegion((max(0, lower + span * 0.25), max(0, lower + span * 0.75)))
        self._sync_band_editors()
        self.refresh_measurements()

    def _sync_band_editors(self, *_args) -> None:
        lower, upper = self.band.getRegion()
        self.band_start.setText(format_quantity_auto(lower, DIMENSION_FREQUENCY))
        self.band_stop.setText(format_quantity_auto(upper, DIMENSION_FREQUENCY))

    def apply_band(self) -> bool:
        try:
            lower, upper = self._frequency(self.band_start.text()), self._frequency(self.band_stop.text())
            if lower >= upper:
                raise ValueError("Band start must be smaller than stop.")
        except (ValueError, TypeError) as error:
            self._say_error(error)
            return False
        self.band.setRegion((lower, upper))
        self.band_enabled.setChecked(True)
        self.refresh_measurements()
        return True

    def refresh_measurements(self, *_args) -> None:
        if not hasattr(self, "band") or self._refreshing:
            return
        self._refreshing = True
        try:
            rows, samples = [], []
            for name, line in self.frequency_markers.items():
                frequency = float(line.value())
                sample = self._sample(frequency)
                text = f"{name}: {format_quantity_auto(frequency, DIMENSION_FREQUENCY)}"
                if sample:
                    text += f"\n  {self._format_measurement(sample[1])} at sample {format_quantity_auto(sample[0], DIMENSION_FREQUENCY)}"
                else:
                    text += "\n  Outside measured trace / no data"
                rows.append(text)
                samples.append((name, frequency, sample))
            if len(samples) >= 2:
                first = samples[0]
                for other in samples[1:]:
                    text = f"{other[0]} − {first[0]}: Δf {format_quantity_auto(other[1] - first[1], DIMENSION_FREQUENCY)}"
                    if first[2] and other[2]:
                        unit = "dB" if self._measurement_unit() in {"dBm", "dB"} else self._measurement_unit()
                        text += f", ΔA {other[2][1] - first[2][1]:.6g} {unit}"
                    rows.append(text)
            self.marker_readout.setText("\n".join(rows) or "No frequency markers.")
            data = self._measurement_data()
            if not self.band_enabled.isChecked():
                self.band_readout.setText("Enable the band to inspect measured samples.")
                return
            lower, upper = self.band.getRegion()
            span_text = f"Width: {format_quantity_auto(upper - lower, DIMENSION_FREQUENCY)}"
            if data is None:
                self.band_readout.setText(span_text + "\nNo measured samples.")
                return
            x, y = data
            selected = np.flatnonzero((x >= lower) & (x <= upper))
            if not selected.size:
                self.band_readout.setText(span_text + "\nNo measured samples in this band.")
                return
            peak = selected[int(np.argmax(y[selected]))]
            trough = float(np.min(y[selected]))
            self.band_readout.setText(
                f"{span_text}\nSamples: {selected.size}\n"
                f"Peak: {self._format_measurement(float(y[peak]))}\n"
                f"at {format_quantity_auto(float(x[peak]), DIMENSION_FREQUENCY)}\n"
                f"Minimum: {self._format_measurement(trough)}\n"
                f"{self._bandwidth_readout(x[selected], y[selected])}\n"
                "Extrema use measured bins; no power integration."
            )
        finally:
            self._refreshing = False

    def _bandwidth_readout(self, x: np.ndarray, y: np.ndarray) -> str:
        """Find the connected peak lobe's crossings inside the inspected band."""
        if self._measurement_unit() not in {"dBm", "dB"}:
            return "−3 dB width requires a logarithmic power trace."
        order = np.argsort(x)
        x, y = x[order], y[order]
        peak = int(np.argmax(y))
        threshold = float(y[peak]) - 3.0
        left, right = peak, peak
        while left > 0 and y[left] > threshold:
            left -= 1
        while right < y.size - 1 and y[right] > threshold:
            right += 1
        if y[left] > threshold or y[right] > threshold or left == peak or right == peak:
            return "−3 dB width: incomplete lobe inside this band."
        if np.any(np.diff(x) <= 0):
            return "−3 dB width unavailable: duplicate frequency bins."
        left_hz = float(x[left] + (threshold - y[left]) * (x[left + 1] - x[left]) / (y[left + 1] - y[left]))
        right_hz = float(x[right - 1] + (threshold - y[right - 1]) * (x[right] - x[right - 1]) / (y[right] - y[right - 1]))
        return f"−3 dB width: {format_quantity_auto(right_hz - left_hz, DIMENSION_FREQUENCY)} (interpolated crossings)"

    def apply_theme(self, theme: str) -> None:
        super().apply_theme(theme)
        palette = plot_theme(tokens_for(theme))
        for line in self.frequency_markers.values():
            line.setPen(pg.mkPen(palette.reference, width=2))
            line.label.setColor(palette.axes)
        if hasattr(self, "band"):
            color = QColor(palette.reference)
            color.setAlpha(35)
            self.band.setBrush(pg.mkBrush(color))
            for line in self.band.lines:
                line.setPen(pg.mkPen(palette.reference))
