"""Explicit physical ranges for the displayed heatmap, independent of data filters."""

import math

from PySide6.QtWidgets import QFormLayout, QHBoxLayout
from qfluentwidgets import CaptionLabel, LineEdit, PrimaryPushButton, PushButton

from app.domain.quantities import format_quantity_auto, parse_quantity
from app.ui.dialogs import StationDialog


def axis_value_text(value, unit):
    if not unit:
        return f"{value:.12g}"
    quantity = parse_quantity(f"{value:.16g} {unit}")
    return format_quantity_auto(quantity.si_value, quantity.dimension)


def parse_axis_value(text, unit):
    if unit:
        dimension = parse_quantity(f"1 {unit}").dimension
        return parse_quantity(text, dimension).as_unit(unit)
    value = float(text.replace(",", "."))
    if not math.isfinite(value):
        raise ValueError("Axis limits must be finite.")
    return value


class HeatmapRangeDialog(StationDialog):
    def __init__(self, parent, axes, ranges):
        super().__init__(parent)
        self.setWindowTitle("Heatmap axis ranges")
        self.set_resizable(True)
        self.resize(660, 420)
        self.axes = axes
        self.ranges = ranges
        root = self.modal_content_layout(spacing=12)
        note = CaptionLabel("Set the displayed X and Y ranges. Include units, for example 1 GHz or 2 mA. Measurement data and coordinate filters stay unchanged.", self)
        note.setWordWrap(True)
        root.addWidget(note)
        form = QFormLayout()
        self.fields = {}
        for index, axis in enumerate(("x", "y")):
            unit = axes[axis].get("unit", "")
            label = axes[axis]["label"]
            for edge, value in zip(("minimum", "maximum"), ranges[index], strict=True):
                edit = LineEdit(self)
                edit.setAccessibleName(f"{axis.upper()} {label} {edge}")
                edit.setText(axis_value_text(value, unit))
                form.addRow(f"{axis.upper()} · {label} · {edge}", edit)
                self.fields[axis, edge] = edit
        root.addLayout(form)
        self.error = CaptionLabel("", self)
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        root.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        apply = PrimaryPushButton("Apply ranges", self)
        apply.clicked.connect(self._save)
        footer.addWidget(cancel)
        footer.addWidget(apply)
        root.addLayout(footer)

    def _save(self):
        try:
            ranges = []
            for axis in ("x", "y"):
                bounds = tuple(parse_axis_value(self.fields[axis, edge].text(), self.axes[axis].get("unit", ""))
                               for edge in ("minimum", "maximum"))
                if bounds[0] >= bounds[1]:
                    raise ValueError(f"{axis.upper()} minimum must be smaller than maximum.")
                ranges.append(bounds)
            self.ranges = ranges
        except (TypeError, ValueError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()
