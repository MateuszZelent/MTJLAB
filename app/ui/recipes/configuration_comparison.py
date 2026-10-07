"""Offline, unit-aware review of the fields a recipe will program."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtGui import QColor
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidgetItem, QWidget, QVBoxLayout, QSizePolicy
from qfluentwidgets import TableWidget, isDarkTheme, StrongBodyLabel, PushButton, CaptionLabel

from app.domain.quantities import parse_quantity
from app.domain.errors import ConfigurationError
from app.ui.design_system.tokens import tokens_for


def equivalent_setting(left: object, right: object) -> bool:
    """Compare physical values in SI, without confusing dimensions or prefixes."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    try:
        a, b = parse_quantity(str(left)), parse_quantity(str(right))
        return a.dimension == b.dimension and math.isclose(
            a.si_value, b.si_value, rel_tol=1e-9, abs_tol=0.0
        )
    except (ValueError, ConfigurationError):
        try:
            return math.isclose(float(str(left)), float(str(right)), rel_tol=1e-9)
        except ValueError:
            return str(left) == str(right)


@dataclass(frozen=True)
class ConfigurationComparisonRow:
    key: str
    label: str
    current: object | None
    planned: object
    action: str


class ConfigurationComparison(TableWidget):
    """All programmed fields, including automatic and coupled settings."""

    summary_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("configurationComparison")
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["Parameter", "Current device-page setting", "Sweep value", "Effect"])
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(28)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setMinimumHeight(180)
        self.setMaximumHeight(250)
        self.rows: tuple[ConfigurationComparisonRow, ...] = ()
        self.counts: dict[str, int] = {}
        self.summary_text = "No configuration changes"

    def set_rows(self, rows: list[ConfigurationComparisonRow]) -> None:
        self.rows = tuple(rows)
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        self.setRowCount(len(rows))
        counts = dict(changed=0, same=0, preserved=0, unknown=0, sweep=0, blocked=0)
        for index, row in enumerate(rows):
            if row.key == "sense_mode" and row.planned != "2wire":
                status, color = "PROHIBITED: only 2wire", tokens.danger
            elif row.action == "Preserve":
                status, color = "Not programmed", tokens.text_muted
            elif row.action == "Sweep":
                status, color = "Changes over ROI", tokens.caution
            elif row.action == "Derived":
                status, color = "Depends on preceding recipe settings", tokens.caution
            elif row.current is None:
                status, color = "Current unknown", tokens.text_muted
            elif row.action == "Require" and not equivalent_setting(row.current, row.planned):
                status, color = "Requirement not met", tokens.danger
            elif equivalent_setting(row.current, row.planned):
                status, color = "Same", tokens.success
            else:
                status, color = "Changes", tokens.caution
            if row.key == "sense_mode" and row.planned != "2wire":
                counts["blocked"] += 1
            elif row.action == "Preserve":
                counts["preserved"] += 1
            elif row.action == "Require":
                if row.current is None:
                    counts["unknown"] += 1
                elif not equivalent_setting(row.current, row.planned):
                    counts["blocked"] += 1
            elif row.action == "Sweep":
                counts["sweep"] += 1
            elif row.action == "Derived":
                counts["unknown"] += 1
            elif row.action == "Set":
                counts["unknown" if row.current is None else
                       "same" if equivalent_setting(row.current, row.planned) else "changed"] += 1
            values = (row.label, "Unknown" if row.current is None else str(row.current),
                      str(row.planned), f"{row.action} · {status}")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setForeground(QColor(color))
                self.setItem(index, column, item)
        self.counts = counts
        noun = "parameter" if counts["changed"] == 1 else "parameters"
        parts = [f"{counts['changed']} {noun} changed", f"{counts['same']} same"]
        for key, label in (("sweep", "varied by ROI"), ("preserved", "preserved"),
                           ("unknown", "unknown"), ("blocked", "blocked / mismatch")):
            if counts[key]:
                parts.append(f"{counts[key]} {label}")
        self.summary_text = " · ".join(parts)
        self.summary_changed.emit(self.summary_text)


class ConfigurationReview(QWidget):
    """Compact persistent summary with an optional complete mutation table."""

    def __init__(self, parent=None, *, baseline_label="device-page settings at opening",
                 current_label="Current device-page setting"):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.summary = StrongBodyLabel(self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        note = CaptionLabel(f"Compared with {baseline_label}. Unknown values are not counted as unchanged.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.toggle = PushButton("Show parameter comparison", self)
        self.toggle.setCheckable(True)
        layout.addWidget(self.toggle)
        self.table = ConfigurationComparison(self)
        self.table.horizontalHeaderItem(1).setText(current_label)
        self.table.setMinimumHeight(120)
        self.table.setMaximumHeight(160)
        self.table.hide()
        layout.addWidget(self.table)
        self.table.summary_changed.connect(self.summary.setText)
        self.table.summary_changed.connect(self._fit_table)
        self.toggle.toggled.connect(self._toggle)

    def _toggle(self, checked):
        self.table.setVisible(checked)
        self.toggle.setText("Hide parameter comparison" if checked else "Show parameter comparison")

    def _fit_table(self, _summary):
        self.table.setFixedHeight(min(160, 38 + 28 * self.table.rowCount()))

    def bind(self, host, rows):
        """Observe form edits only; the callback must never write to hardware."""
        def refresh(*args):
            self.table.set_rows(rows())
        for widget in host.findChildren(QWidget):
            if self.isAncestorOf(widget):
                continue
            for name in ("textChanged", "currentIndexChanged", "valueChanged", "toggled"):
                signal = getattr(widget, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(refresh)
                    break
        refresh()


def current_parameter_setting(parent, target):
    """Find the owning recipe page's read-only draft provider, never hardware."""
    while parent is not None:
        provider = getattr(parent, "parameter_snapshot_provider", None)
        if callable(provider):
            try:
                return provider().get(target)
            except Exception:
                return None
        parent = parent.parentWidget()
    return None

