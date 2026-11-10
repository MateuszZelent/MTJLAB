"""Read-only, aggregate comparison of device tabs with a sweep's settings."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTreeWidgetItem,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    PushButton,
    SearchLineEdit,
    StrongBodyLabel,
    SubtitleLabel,
    TreeWidget,
    isDarkTheme,
    qconfig,
)

from app.recipes.parameter_overview import SweepParameterEntry, entry_comparison
from app.ui.design_system.tokens import SPACING, tokens_for
from app.ui.recipes.fluent_dialog import FluentRecipeDialog

_DEVICE_NAMES = {
    "keithley": "Keithley 2600",
    "rigol": "Rigol DG1000Z",
    "anritsu": "Anritsu MS2830A",
    "anritsu_sg": "Anritsu signal generator",
    "moke_box": "MOKE Box",
    "lakeshore": "Lake Shore 475",
}
_ENTRY_ROLE = Qt.ItemDataRole.UserRole


def _device_name(device: str) -> str:
    return _DEVICE_NAMES.get(device, device)


def _value_text(value: object) -> str:
    # The model owns quantity normalization and range descriptions. Preserve
    # explicit units and precision here; never infer units from a parameter key.
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    return str(value)


class SweepParameterOverviewDialog(FluentRecipeDialog):
    """All participating settings, including preserved and disabled entries."""

    source_requested = Signal(str)

    def __init__(
        self,
        recipe_name: str,
        entries: tuple[SweepParameterEntry, ...],
        parent=None,
    ):
        super().__init__(parent)
        self.entries = tuple(entries)
        self.setObjectName("sweepParameterOverviewDialog")
        self.setWindowTitle("Sweep settings comparison")
        self.setModal(True)
        self.setMinimumSize(720, 520)
        available = self.screen().availableGeometry()
        self.resize(min(1280, available.width() - 48), min(800, available.height() - 48))
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=SPACING["md"])

        title = SubtitleLabel("Sweep settings comparison", surface)
        title.setWordWrap(True)
        layout.addWidget(title)
        self.recipe_label = BodyLabel(recipe_name or "Untitled sweep", surface)
        self.recipe_label.setTextFormat(Qt.TextFormat.PlainText)
        self.recipe_label.setWordWrap(True)
        self.recipe_label.setToolTip(recipe_name)
        layout.addWidget(self.recipe_label)
        explanation = CaptionLabel(
            "Current device tab values compared with this sweep. "
            "Swept ranges count as different. Disabled blocks remain listed.", surface,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.summary_label = StrongBodyLabel(surface)
        self.summary_label.setObjectName("sweepParameterSummary")
        self.summary_label.setWordWrap(True)
        counts = Counter(entry_comparison(entry) for entry in self.entries)
        swept = sum(entry.action.casefold() == "sweep" for entry in self.entries)
        disabled = sum(not entry.enabled for entry in self.entries)
        self.summary_label.setText(
            f"{counts['same']} same  ·  {counts['different']} different  ·  "
            f"{counts['unavailable']} unavailable  ·  {swept} swept {'axis' if swept == 1 else 'axes'}"
            + (f"  ·  {disabled} disabled" if disabled else "")
        )
        layout.addWidget(self.summary_label)

        filters = QHBoxLayout()
        filters.setSpacing(SPACING["md"])
        self.search = SearchLineEdit(surface)
        self.search.setObjectName("sweepParameterSearch")
        self.search.setPlaceholderText("Search settings, values or source blocks")
        self.search.setAccessibleName("Search sweep settings")
        self.search.setToolTip("Search device, channel, parameter, value, action or source. Ctrl+F")
        self.search.setMinimumWidth(180)
        self.device_filter = ComboBox(surface)
        self.device_filter.setObjectName("sweepParameterDeviceFilter")
        self.device_filter.setAccessibleName("Filter by device")
        self.device_filter.setMinimumWidth(170)
        self.device_filter.addItem("All devices", userData=None)
        for device in dict.fromkeys(entry.device for entry in self.entries):
            self.device_filter.addItem(_device_name(device), userData=device)
        self.only_differences = CheckBox("Only differences", surface)
        self.only_differences.setObjectName("sweepParameterOnlyDifferences")
        self.only_differences.setToolTip("Show settings that differ from the device tab, including swept ranges.")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.device_filter)
        filters.addWidget(self.only_differences)
        layout.addLayout(filters)

        self.count_label = CaptionLabel(surface)
        self.count_label.setObjectName("sweepParameterVisibleCount")
        layout.addWidget(self.count_label)
        self.stack = QStackedWidget(surface)
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.tree = TreeWidget(self.stack)
        self.tree.setObjectName("sweepParameterTree")
        self.tree.setAccessibleName("Sweep settings grouped by device and channel")
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels([
            "Parameter", "Device tab value", "Sweep value", "Comparison", "Action", "Source block",
        ])
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setUniformRowHeights(False)
        self.tree.setWordWrap(True)
        self.tree.setIndentation(SPACING["lg"])
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setStretchLastSection(True)
        for column, width in enumerate((200, 165, 260, 120, 110, 190)):
            self.tree.setColumnWidth(column, width)
        self.stack.addWidget(self.tree)
        self.empty_label = BodyLabel(self.stack)
        self.empty_label.setObjectName("sweepParameterEmptyState")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty_label)
        layout.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        footer.setSpacing(SPACING["md"])
        hint = CaptionLabel("Select a setting, then press Enter to open its source block.", surface)
        hint.setWordWrap(True)
        footer.addWidget(hint, 1)
        self.close_button = PushButton("Close", surface)
        self.close_button.setObjectName("sweepParameterClose")
        self.close_button.setMinimumWidth(100)
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

        self._rows: list[tuple[QTreeWidgetItem, SweepParameterEntry, str]] = []
        groups: dict[tuple[str, str], QTreeWidgetItem] = {}
        for entry in self.entries:
            key = (entry.device, str(entry.channel))
            group = groups.get(key)
            if group is None:
                channel = str(entry.channel) if entry.channel is not None else ""
                group_name = _device_name(entry.device)
                if channel:
                    if entry.device == "moke_box" and not channel.upper().startswith("VOUT"):
                        channel = f"VOUT {channel}"
                    group_name += (f" · {channel}" if channel.upper().startswith("VOUT")
                                   else f" · Channel {channel}")
                group = QTreeWidgetItem(self.tree, [group_name])
                group.setFirstColumnSpanned(True)
                font = self.tree.font()
                font.setBold(True)
                group.setFont(0, font)
                group.setSizeHint(0, QSize(0, 36))
                group.setToolTip(0, group_name)
                groups[key] = group
            status = entry_comparison(entry)
            values = [
                entry.label,
                _value_text(entry.card_value),
                _value_text(entry.sweep_value),
                status.upper(),
                entry.action.capitalize() + (" (disabled)" if not entry.enabled else ""),
                entry.source_label or entry.source_id or "Device tab / preserved",
            ]
            item = QTreeWidgetItem(group, values)
            item.setData(0, _ENTRY_ROLE, entry)
            item.setSizeHint(0, QSize(0, 40))
            for column, value in enumerate(values):
                tooltip = value
                if entry.detail:
                    tooltip += f"\n\n{entry.detail}"
                if entry.source_id:
                    tooltip += f"\nSource: {entry.source_label} ({entry.source_id})\nEnter: open source block"
                item.setToolTip(column, tooltip)
            searchable = " ".join([
                entry.device, _device_name(entry.device),
                str(entry.channel) if entry.channel is not None else "",
                entry.parameter, *values, entry.source_id, entry.detail,
            ]).casefold()
            self._rows.append((item, entry, searchable))
        self.tree.expandAll()
        self._refresh_colors()
        qconfig.themeChanged.connect(self._refresh_colors)
        self.search.textChanged.connect(self._apply_filters)
        self.device_filter.currentIndexChanged.connect(self._apply_filters)
        self.only_differences.toggled.connect(self._apply_filters)
        self.tree.itemActivated.connect(self._request_source)
        self._search_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self._search_shortcut.activated.connect(self._focus_search)
        for control in (self.search, self.device_filter, self.only_differences):
            control.setEnabled(bool(self.entries))
        self.setTabOrder(self.search, self.device_filter)
        self.setTabOrder(self.device_filter, self.only_differences)
        self.setTabOrder(self.only_differences, self.tree)
        self.setTabOrder(self.tree, self.close_button)
        self._apply_filters()
        self.search.setFocus()

    def _refresh_colors(self, *_args) -> None:
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        colors = {"same": tokens.success, "different": tokens.caution,
                  "unavailable": tokens.text_muted}
        for item, entry, _searchable in self._rows:
            for column in range(self.tree.columnCount()):
                color = (colors[entry_comparison(entry)] if column in (2, 3)
                         else tokens.text_primary)
                if column == 4 and not entry.enabled:
                    color = tokens.text_muted
                item.setForeground(column, QColor(color))
        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setForeground(0, QColor(tokens.text_primary))

    def _apply_filters(self, *_args) -> None:
        words = self.search.text().casefold().split()
        device = self.device_filter.currentData()
        different_only = self.only_differences.isChecked()
        shown = 0
        for item, entry, searchable in self._rows:
            visible = (
                (device is None or entry.device == device)
                and (not different_only or entry_comparison(entry) == "different")
                # Single-letter channels must not match unrelated text such as
                # "mA" or "enabled" when searching for "Keithley A/B".
                and all(word in searchable.split() if len(word) == 1 else word in searchable
                        for word in words)
            )
            item.setHidden(not visible)
            shown += visible
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            group.setHidden(all(group.child(i).isHidden() for i in range(group.childCount())))
            group.setExpanded(True)
        self.count_label.setText(f"{shown} / {len(self.entries)} settings shown")
        self.empty_label.setText(
            "No device settings in this sweep.\nAdd a device block to include its settings here."
            if not self.entries else
            "No settings match these filters.\nClear the search or change the device and difference filters."
        )
        self.stack.setCurrentWidget(self.tree if shown else self.empty_label)

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _request_source(self, item: QTreeWidgetItem, _column: int) -> None:
        entry = item.data(0, _ENTRY_ROLE)
        if entry is not None and entry.source_id:
            self.source_requested.emit(entry.source_id)
            # The owning page selects the block after exec() returns.
            self.accept()
