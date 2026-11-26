"""Qt UI owned by the Rigol DG1000Z module."""

from typing import Any

from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot, RigolPage
from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog

__all__ = [
    "RigolConfigurationSnapshot",
    "RigolLoadCalculatorPage",
    "RigolNodeEditorDialog",
    "RigolPage",
]


def __getattr__(name: str) -> Any:
    """Lazy-load RigolLoadCalculatorPage to avoid circular imports with Results widgets."""
    if name == "RigolLoadCalculatorPage":
        from app.devices.rigol_dg1000z.ui.calculator_page import RigolLoadCalculatorPage

        return RigolLoadCalculatorPage
    raise AttributeError(name)
