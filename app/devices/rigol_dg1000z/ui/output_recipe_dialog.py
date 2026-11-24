"""Offline editor for the Rigol recipe's output path and SYNC settings."""

from __future__ import annotations

import math

from PySide6.QtWidgets import QFormLayout, QFrame, QHBoxLayout, QLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
)

from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.recipes import RecipeNode
from app.ui.design_system.tokens import SPACING
from app.ui.recipes.configuration_comparison import ConfigurationComparisonRow, ConfigurationReview
from app.ui.recipes.fluent_dialog import FluentRecipeDialog

_LABELS = {
    "channel": "Channel",
    "output_load": "Output load (Ω or HIGHZ)",
    "polarity": "Output polarity",
    "mode": "Output mode",
    "gate_polarity": "Gate polarity",
    "sync_enabled": "SYNC output",
    "sync_polarity": "SYNC polarity",
    "sync_delay": "SYNC delay",
}
_CHOICES = {
    "channel": (("Channel 1", 1), ("Channel 2", 2)),
    "polarity": (("Normal", "NORM"), ("Inverted", "INV")),
    "mode": (("Continuous", "NORM"), ("Gated", "GAT")),
    "gate_polarity": (("Normal", "NORM"), ("Inverted", "INV")),
    "sync_polarity": (("Normal", "NORM"), ("Inverted", "INV")),
}
_REVIEW_KEYS = {"polarity": "output_polarity", "mode": "output_mode"}


def _reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith("${") and value.endswith("}")


class RigolOutputNodeEditorDialog(FluentRecipeDialog):
    """Edit only authored fields; opening or saving never communicates with hardware."""

    def __init__(self, node: RecipeNode, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if node.type != "configure_rigol_output":
            raise ConfigurationError("The Rigol output editor requires an output configuration step.")
        self._node = node
        self._editors: dict[str, tuple[QWidget, str]] = {}
        self.setObjectName("rigolOutputRecipeDialog")
        self.setWindowTitle("Rigol DG1032Z — output and SYNC")
        self.setMinimumSize(680, 620)
        self.resize(920, 800)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=SPACING["md"])
        layout.addWidget(SubtitleLabel("Output and SYNC", surface))
        description = BodyLabel(
            "Set the output connection and synchronization signal for this channel. "
            "The waveform and DC level are defined in the initial waveform step.", surface,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        body = QWidget(surface)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(SPACING["lg"])
        body_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        for title, keys in (
            ("Output connection", ("channel", "output_load", "polarity", "mode", "gate_polarity")),
            ("Synchronization", ("sync_enabled", "sync_polarity", "sync_delay")),
        ):
            authored = [key for key in keys if key in node.data]
            if not authored:
                continue
            card = CardWidget(body)
            form = QFormLayout(card)
            form.setContentsMargins(*(SPACING["lg"],) * 4)
            form.setSpacing(SPACING["md"])
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.addRow(StrongBodyLabel(title, card))
            for key in authored:
                editor, kind = self._create_editor(key, node.data[key], card)
                editor.setAccessibleName(_LABELS[key])
                self._editors[key] = (editor, kind)
                form.addRow(_LABELS[key], editor)
            body_layout.addWidget(card)

        self.review = ConfigurationReview(body, baseline_label="the saved output settings",
                                          current_label="Saved output setting")
        body_layout.addWidget(self.review)
        body_layout.addStretch(1)
        self.controls_scroll = ScrollArea(surface)
        self.controls_scroll.setObjectName("rigolOutputSettingsScroll")
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setWidget(body)
        layout.addWidget(self.controls_scroll, 1)

        self.validation_message = BodyLabel(surface)
        self.validation_message.setWordWrap(True)
        self.validation_message.hide()
        layout.addWidget(self.validation_message)
        note = CaptionLabel(
            "Saved to the recipe only. This step leaves OUTPUT OFF and requires "
            "an earlier waveform configuration for the same channel.", surface,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", surface)
        self.apply_button = PrimaryPushButton("Save output settings", surface)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self.accept)
        self._initial_values = {key: self._editor_value(key) for key in self._editors}
        self.review.bind(body, self._comparison_rows)

    @staticmethod
    def _create_editor(key: str, value: object, parent: QWidget) -> tuple[QWidget, str]:
        choices = _CHOICES.get(key)
        if key == "sync_enabled" and not isinstance(value, bool):
            choices = (("Disabled", False), ("Enabled", True))
        if choices is not None and not _reference(value):
            editor = ComboBox(parent)
            for label, choice in choices:
                editor.addItem(label, userData=choice)
            index = next((i for i, (_, choice) in enumerate(choices)
                          if type(choice) is type(value) and choice == value), -1)
            if index < 0:
                editor.addItem(f"{value} — invalid", userData=value)
                index = editor.count() - 1
            editor.setCurrentIndex(index)
            return editor, "choice"
        if key == "sync_enabled" and isinstance(value, bool):
            checkbox = CheckBox("Enabled", parent)
            checkbox.setChecked(value)
            return checkbox, "bool"
        line = LineEdit(parent)
        line.setText(str(value))
        if key == "sync_delay":
            line.setPlaceholderText("For example 0 s or 1 ms")
        return line, "str"

    def _editor_value(self, key: str) -> object:
        editor, kind = self._editors[key]
        if kind == "choice":
            return editor.currentData()
        if kind == "bool":
            return editor.isChecked()
        text = editor.text().strip()
        if key == "sync_enabled" and text.lower() in {"true", "false"}:
            return text.lower() == "true"
        return text

    def node_fields(self) -> dict[str, object]:
        fields = dict(self._node.data)
        for key in self._editors:
            value = self._editor_value(key)
            initial = self._initial_values[key]
            if type(value) is not type(initial) or value != initial:
                fields[key] = value
        return fields

    def _comparison_rows(self) -> list[ConfigurationComparisonRow]:
        fields = self.node_fields()
        rows = [ConfigurationComparisonRow(
            _REVIEW_KEYS.get(key, key), _LABELS[key], self._node.data[key], fields[key], "Set",
        ) for key in self._editors]
        rows.append(ConfigurationComparisonRow("output_enabled", "OUTPUT during configuration",
                                               None, False, "Set"))
        return rows

    def accept(self) -> None:
        try:
            fields = self.node_fields()
            for key, choices in _CHOICES.items():
                value = fields.get(key)
                if key in fields and not _reference(value) and not any(
                    type(value) is type(choice) and value == choice for _, choice in choices
                ):
                    raise ConfigurationError(f"Select a valid {_LABELS[key].lower()}.")
            delay = fields.get("sync_delay")
            if delay is not None and not _reference(delay):
                seconds = parse_quantity(str(delay), DIMENSION_TIME).si_value
                if not 0 <= seconds <= 10:
                    raise ConfigurationError("SYNC delay must be between 0 s and 10 s.")
            if "sync_enabled" in fields and not _reference(fields["sync_enabled"]) and type(fields["sync_enabled"]) is not bool:
                raise ConfigurationError("SYNC output must be enabled or disabled.")
            load = fields.get("output_load")
            if load is not None and not _reference(load) and str(load).strip().upper() not in {"HIGHZ", "INF", "INFINITY"}:
                ohms = float(load)
                if not math.isfinite(ohms) or not 1 <= ohms <= 10_000:
                    raise ConfigurationError("Output load must be HIGHZ or a resistance from 1 to 10000 Ω.")
        except (ValueError, ConfigurationError) as exc:
            self.validation_message.setText(f"Cannot save: {exc}")
            self.validation_message.show()
            return
        super().accept()
