"""Sample inventory UI module."""

from app.ui.inventory.attachment_card import AttachmentCard
from app.ui.inventory.attachment_viewer import ImageViewerDialog, open_attachment
from app.ui.inventory.matrix_widget import SampleMatrixWidget
from app.ui.inventory.measurement_browser_view import MeasurementBrowserView
from app.ui.inventory.measurement_card import MeasurementAnalyticsCard
from app.ui.inventory.measurement_plot import MeasurementPlotWidget
from app.ui.inventory.measurement_tree import MeasurementTreeWidget
from app.ui.inventory.page import SampleInventoryPage
from app.ui.inventory.programming_dialog import RenumberRowsDialog, SampleProgrammingDialog

__all__ = [
    "AttachmentCard",
    "ImageViewerDialog",
    "MeasurementAnalyticsCard",
    "MeasurementBrowserView",
    "MeasurementPlotWidget",
    "MeasurementTreeWidget",
    "RenumberRowsDialog",
    "SampleInventoryPage",
    "SampleMatrixWidget",
    "SampleProgrammingDialog",
    "open_attachment",
]
