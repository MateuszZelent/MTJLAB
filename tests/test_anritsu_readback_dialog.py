"""Regression tests for AnritsuReadbackDialog (UI-H1, UI-M3, BUG-M6, BUG-H4)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

from app.devices.anritsu_ms2830a.adapter import AnritsuFullConfigurationReadback
from app.devices.anritsu_ms2830a.ui.readback_dialog import (
    AnritsuReadbackDialog,
    _contrast_ratio,
)
from app.ui.design_system import apply_application_theme, tokens_for


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


def _sample_readback(*, vbw_auto: bool = False, vbw_hz: float | None = None) -> AnritsuFullConfigurationReadback:
    return AnritsuFullConfigurationReadback(
        start_hz=1e6,
        stop_hz=10e6,
        center_hz=5.5e6,
        span_hz=9e6,
        reference_level_dbm=-10.0,
        points=1001,
        rbw_auto=True,
        rbw_hz=1e3,
        vbw_auto=vbw_auto,
        vbw_mode="VID",
        vbw_hz=vbw_hz,
        sweep_time_auto=True,
        sweep_time_s=0.1,
        attenuation_auto=True,
        attenuation_db=10.0,
        detector="NORM",
        continuous_sweep=True,
        average_count=1,
    )


def test_readback_check_match_distinguishes_bool_from_numeric_and_handles_invalid_strings():
    assert not AnritsuReadbackDialog._check_match("rbw_auto", True, 1.0)
    assert not AnritsuReadbackDialog._check_match("rbw_auto", False, 0.0)
    assert not AnritsuReadbackDialog._check_match("rbw_auto", True, 1)
    assert not AnritsuReadbackDialog._check_match("start_hz", 1.0, True)
    assert not AnritsuReadbackDialog._check_match("start_hz", 1e6, "invalid")
    assert AnritsuReadbackDialog._check_match("rbw_auto", True, True)
    assert AnritsuReadbackDialog._check_match("start_hz", 1e6, 1e6)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_readback_dialog_wcag_contrast_vbw_off_layout_and_invalid_form_fallback(application, theme):
    apply_application_theme(application, theme)
    parent = QWidget()
    readback = _sample_readback(vbw_auto=False, vbw_hz=None)
    form_values = {
        "start_hz": "invalid",
        "stop_hz": 10e6,
        "center_hz": 5.5e6,
        "span_hz": 9e6,
        "reference_level_dbm": -10.0,
        "points": 1001,
        "rbw_auto": True,
        "rbw_hz": 1e3,
        "vbw_mode": "VID",
        "vbw_auto": False,
        "vbw_hz": None,
        "detector": "NORM",
        "average_count": 1,
    }
    dialog = AnritsuReadbackDialog(readback, form_values, parent)
    try:
        assert dialog._is_resizable
        assert not dialog.isSizeGripEnabled()
        assert dialog.table.columnWidth(3) <= 200

        # VBW mode with vbw_auto=False and vbw_hz=None must display "OFF", not "Manual".
        vbw_auto_row = next(
            row for row, definition in enumerate(dialog._param_definitions) if definition[0] == "vbw_auto"
        )
        assert dialog.table.item(vbw_auto_row, 1).text() == "OFF"

        # Invalid form value fallback renders cleanly in mismatch color with action enabled.
        assert dialog._status_items["start_hz"].text() == "Form: invalid"
        assert dialog._action_buttons["start_hz"].isEnabled()
        assert dialog._status_items["stop_hz"].text() == "MATCH"
        assert not dialog._action_buttons["stop_hz"].isEnabled()

        # Verify WCAG AA >= 4.5:1 contrast for both match and mismatch text in Light and Dark themes.
        surface = QColor(tokens_for(theme).dialog_surface)
        match_fg = dialog._status_items["stop_hz"].foreground().color()
        mismatch_fg = dialog._status_items["start_hz"].foreground().color()
        assert _contrast_ratio(match_fg, surface) >= 4.5
        assert _contrast_ratio(mismatch_fg, surface) >= 4.5
    finally:
        dialog.close()
        parent.close()
        apply_application_theme(application, "light")
        application.processEvents()
