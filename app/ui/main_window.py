"""Compatibility facade for the application shell and extracted UI components.

New application code should import :class:`MainWindow` from
`app.ui.shell`; historical imports remain supported during migration.
"""

from app.devices.anritsu_ms2830a.ui import (
    AnritsuAdvancedSpectrumPanel,
    AnritsuNodeEditorDialog,
    AnritsuPageState,
    AnritsuSignalGeneratorNodeEditorDialog,
    AnritsuSpectrumConfigurationPanel,
)
from app.devices.keithley_2600.ui import (
    KeithleyConfigurationPanel,
    KeithleyNodeEditorDialog,
    KeithleyPage,
)
from app.devices.rigol_dg1000z.ui import RigolNodeEditorDialog
from app.ui.dashboard import DashboardPage, DeviceCard
from app.ui.execution import RunMonitorPage
from app.ui.recipes import DeviceParameterDialog, SweepGeneratorDialog
from app.ui.recipes.page import (
    AnritsuAcquisitionEditorDialog,
    CommentEditorDialog,
    FixedValueDialog,
    KeithleySweepBuilderDialog,
    RecipePage,
    RecipeTreeWidget,
    SweepLibraryButton,
)
from app.ui.results import ResultsPage
from app.ui.shell import MainWindow
from app.ui.widgets import LimitEditDialog, LimitField, SpectrumPlotWidget

__all__ = [
    "AnritsuAcquisitionEditorDialog",
    "AnritsuAdvancedSpectrumPanel",
    "AnritsuNodeEditorDialog",
    "AnritsuPageState",
    "AnritsuSignalGeneratorNodeEditorDialog",
    "AnritsuSpectrumConfigurationPanel",
    "CommentEditorDialog",
    "DashboardPage",
    "DeviceCard",
    "DeviceParameterDialog",
    "FixedValueDialog",
    "KeithleyConfigurationPanel",
    "KeithleyNodeEditorDialog",
    "KeithleyPage",
    "KeithleySweepBuilderDialog",
    "LimitEditDialog",
    "LimitField",
    "MainWindow",
    "RecipePage",
    "RecipeTreeWidget",
    "ResultsPage",
    "RigolNodeEditorDialog",
    "RunMonitorPage",
    "SpectrumPlotWidget",
    "SweepGeneratorDialog",
    "SweepLibraryButton",
]
