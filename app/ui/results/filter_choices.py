"""Model-backed selectors for potentially large checkpoint filter catalogues."""

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtWidgets import QComboBox, QProxyStyle, QStyle
from qfluentwidgets import ListView


class FilterChoiceModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = ()
        self.header = ("", None)
        self.formatter = str

    def set_options(self, header, records, formatter):
        self.beginResetModel()
        self.header = header
        self.records = records
        self.formatter = formatter
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records) + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < self.rowCount():
            return None
        row = index.row()
        if role == Qt.ItemDataRole.UserRole:
            return self.header[1] if row == 0 else self.records[row - 1]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self.header[0] if row == 0 else self.formatter(self.records[row - 1])
        return None


class FilterComboBox(QComboBox):
    """Qt's virtual item view avoids a QAction/widget for every filter value.

    This is a page-local functional control with a Fluent list view; it does
    not introduce another application shell or emulate the Fluent ComboBox API.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.options = FilterChoiceModel(self)
        self.setModel(self.options)
        self._popup_style = _BoundedComboStyle()
        self._popup_style.setParent(self)
        self.setStyle(self._popup_style)
        view = ListView(self)
        view.setUniformItemSizes(True)
        self.setView(view)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(10)
        self.setMaxVisibleItems(15)
        self.setMinimumHeight(32)

    def set_options(self, header, records=(), formatter=str):
        blocked = self.blockSignals(True)
        try:
            self.options.set_options(header, records, formatter)
            self.setCurrentIndex(0)
        finally:
            self.blockSignals(blocked)


class _BoundedComboStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            # Menu-style popups measure every item and ignore maxVisibleItems.
            return 0
        return super().styleHint(hint, option, widget, returnData)
