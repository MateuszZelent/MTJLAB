"""Temporary bridge for legacy device-specific recipe editors.

The generic recipe workspace imports this module rather than individual device
UI packages.  It keeps the remaining migration surface explicit and gives new
modules one integration point while existing node editors are moved behind
their respective ``RecipeExtension`` implementations.
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
