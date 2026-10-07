"""Fluent navigation rows whose motion does not compete with their layout."""

from PySide6.QtCore import Property, QRect
from qfluentwidgets import NavigationTreeWidget


class StationNavigationTreeWidget(NavigationTreeWidget):
    """Animate row size while leaving row position owned by QVBoxLayout.

    Fluent's geometry animation captures a position that becomes stale when
    another branch expands. Even a leaf click starts that animation. Its next
    frame can therefore move the leaf back over its previous neighbour.
    Keep Fluent's expansion signals and ancestor sizing, but never animate pos.
    """

    def __init__(self, icon, text, isSelectable, parent=None):
        super().__init__(icon, text, isSelectable, parent)
        self.expandAni.setPropertyName(b"expansionGeometry")
        self.expandAni.finished.connect(self._settle_expansion)

    def _set_expansion_geometry(self, rectangle):
        self.setFixedSize(rectangle.size())

    expansionGeometry = Property(QRect, NavigationTreeWidget.geometry, _set_expansion_geometry)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Reveal children by clipping; do not squeeze fixed-height rows into
        # the intermediate height (Qt otherwise makes their spacing negative).
        self.vBoxLayout.setGeometry(QRect(
            0, 0, self.width(), max(self.height(), self.vBoxLayout.sizeHint().height())
        ))

    def setExpanded(self, isExpanded, ani=False):
        # Instant transitions (compact rail, resize, restoration) must cancel
        # pending frames, including a request for the already-selected state.
        if not ani:
            self.expandAni.stop()
        super().setExpanded(isExpanded, ani)
        if not ani:
            self._settle_expansion()

    def _settle_expansion(self):
        # A nested branch may have changed the target height during this run.
        self.setFixedSize(self.sizeHint())
        ancestor = self.treeParent
        while ancestor is not None:
            ancestor.setFixedSize(ancestor.sizeHint())
            ancestor = ancestor.treeParent

    def clone(self):
        # Compact-mode flyouts must use the same animation ownership contract.
        root = type(self)(self.icon(), self.text(), self.isSelectable, self.parent())
        root.setSelected(self.isSelected)
        root.setFixedSize(self.size())
        root.setTextColor(self.lightTextColor, self.darkTextColor)
        root.setIndicatorColor(self.itemWidget.lightIndicatorColor, self.itemWidget.darkIndicatorColor)
        root.nodeDepth = self.nodeDepth
        root.clicked.connect(self.clicked)
        self.selectedChanged.connect(root.setSelected)
        for child in self.treeChildren:
            root.addChild(child.clone())
        return root
