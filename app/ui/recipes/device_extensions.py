"""UI extension boundary for device-specific recipe editors.

``RecipePage`` owns the recipe workflow, while the concrete device packages
own their editor widgets and configuration snapshots.  This module is the
single UI-facing composition point between those two layers; it keeps the
recipe page independent from concrete device package paths.
"""

from app.devices.anritsu_ms2830a.ui.recipe_extension import (
    AnritsuAdvancedSpectrumPanel,
    AnritsuConfigurationSnapshot,
    AnritsuNodeEditorDialog,
    AnritsuPage,
    AnritsuPageState,
    AnritsuSignalGeneratorNodeEditorDialog,
    AnritsuSpectrumConfigurationPanel,
    SignalGeneratorSnapshot,
)
from app.devices.keithley_2600.ui.recipe_extension import (
    KeithleyConfigurationPanel,
    KeithleyConfigurationSnapshot,
    KeithleyNodeEditorDialog,
    KeithleyPage,
    _keithley_roi_definition,
)
from app.devices.rigol_dg1000z.ui.recipe_extension import (
    RigolConfigurationSnapshot,
    RigolNodeEditorDialog,
    RigolPage,
)

__all__ = [
    "AnritsuAdvancedSpectrumPanel",
    "AnritsuConfigurationSnapshot",
    "AnritsuNodeEditorDialog",
    "AnritsuPage",
    "AnritsuPageState",
    "AnritsuSignalGeneratorNodeEditorDialog",
    "AnritsuSpectrumConfigurationPanel",
    "KeithleyConfigurationPanel",
    "KeithleyConfigurationSnapshot",
    "KeithleyNodeEditorDialog",
    "KeithleyPage",
    "RigolConfigurationSnapshot",
    "RigolNodeEditorDialog",
    "RigolPage",
    "SignalGeneratorSnapshot",
    "_keithley_roi_definition",
]
