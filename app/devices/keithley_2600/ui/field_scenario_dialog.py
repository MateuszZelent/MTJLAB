"""Fluent measurement-tree confirmation, with no instrument access."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QTreeWidgetItem
from qfluentwidgets import BodyLabel, PrimaryPushButton, PushButton, SubtitleLabel, TreeWidget

from app.ui.dialogs import StationDialog


class FieldScenarioDialog(StationDialog):
    def __init__(self, scenario, parent=None):
        super().__init__(parent, resizable=True)
        self.scenario = scenario
        self.setWindowTitle("Review field-line characterization")
        self.setModal(True)
        self.resize(960, 740)
        self.setMinimumSize(640, 480)
        layout = self.modal_content_layout(spacing=12)
        surface = self.modal_shell.surface
        layout.addWidget(SubtitleLabel("Review the measurement sequence", surface))
        summary = BodyLabel(
            f"Sample: {scenario.config.sweep.metadata.sample_id} · "
            f"{len(scenario.config.currents_a)} field targets · "
            f"up to {len(scenario.sample_setpoints_si)} A points per curve\n"
            "Expand each target to inspect the steps. No output is enabled by this preview.", surface)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        self.tree = TreeWidget(surface)
        self.tree.setHeaderHidden(True)
        self.tree.setWordWrap(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tree.setAccessibleName("Field-line measurement sequence")

        def add(parent_item, step):
            item = QTreeWidgetItem([step.title])
            parent_item.addChild(item)
            if step.detail:
                detail = QTreeWidgetItem([step.detail])
                item.addChild(detail)
            for child in step.children:
                add(item, child)
            return item

        root = self.tree.invisibleRootItem()
        for step in scenario.steps:
            add(root, step)
        self.tree.expandToDepth(0)
        layout.addWidget(self.tree, 1)
        actions = QHBoxLayout()
        expand = PushButton("Expand all", surface)
        expand.clicked.connect(self.tree.expandAll)
        collapse = PushButton("Collapse details", surface)
        collapse.clicked.connect(lambda: (self.tree.collapseAll(), self.tree.expandToDepth(0)))
        actions.addWidget(expand)
        actions.addWidget(collapse)
        actions.addStretch()
        self.cancel_button = PushButton("Cancel", surface)
        self.start_button = PrimaryPushButton("Confirm and start series", surface)
        self.start_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.start_button.clicked.connect(self.accept)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.start_button)
        layout.addLayout(actions)
        self.cancel_button.setFocus()
