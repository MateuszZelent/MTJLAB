"""Regression tests for recipe dialogs, spectrum controls, manual save, and analysis settings."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import (
    SpectrumAnalysisSettingsDialog,
)
from app.devices.anritsu_ms2830a.ui.manual_save import ManualSpectrumSaveDialog
from app.devices.anritsu_ms2830a.ui.recipe_dialog import (
    AnritsuNodeEditorDialog,
    AnritsuSignalGeneratorNodeEditorDialog,
)
from app.devices.anritsu_ms2830a.ui.spectrum_controls import (
    PlotRangeController,
    PlotScaleDialog,
    parse_plot_amplitude,
)
from app.ui.widgets import SpectrumPlotWidget
from tests.helpers import simulation_settings


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


def test_parse_plot_amplitude_normalizes_decimal_commas_and_rejects_nonfinite():
    assert parse_plot_amplitude(" 1,5 ratio ", "ratio") == pytest.approx(1.5)
    assert parse_plot_amplitude(" 2,25 linear ratio ", "linear ratio") == pytest.approx(2.25)
    assert parse_plot_amplitude(" 2,5e-8 mW² ", "mW²") == pytest.approx(2.5e-8)
    with pytest.raises(ValueError, match="finite"):
        parse_plot_amplitude("inf ratio", "ratio")
    with pytest.raises(ValueError, match="finite"):
        parse_plot_amplitude("nan mW²", "mW²")


def test_sg_recipe_dialog_catches_quantity_error_and_attenuation_uses_dimension_db(application):
    sg_dialog = AnritsuSignalGeneratorNodeEditorDialog(frequency="not-a-frequency", power="-20 dBm")
    node_dialog = AnritsuNodeEditorDialog(simulation_settings())
    try:
        with patch("app.devices.anritsu_ms2830a.ui.recipe_dialog.QMessageBox.warning") as warning:
            sg_dialog.accept()
            warning.assert_called_once()
        assert sg_dialog.result() == 0

        node_dialog._load_parameter_value("advanced.attenuation", "12,0 dB")
        assert node_dialog.advanced_panel.attenuation.value() == 12
        node_dialog._load_parameter_value("advanced.attenuation", "inf dB")
        assert node_dialog.advanced_panel.attenuation.value() == 12
        node_dialog._load_parameter_value("advanced.attenuation", "nan dB")
        assert node_dialog.advanced_panel.attenuation.value() == 12
    finally:
        sg_dialog.close()
        node_dialog.close()
        application.processEvents()


def test_plot_scale_dialog_disconnects_safely_even_if_finished_fires_twice(application):
    plot = SpectrumPlotWidget()
    controller = PlotRangeController(plot.plot.getViewBox(), plot)
    dialog = PlotScaleDialog(controller, plot)
    try:
        dialog.finished.emit(0)
        dialog.finished.emit(0)
    finally:
        dialog.close()
        plot.close()
        application.processEvents()


def test_manual_save_dialog_resets_elab_upload_hint_when_unchecked(application):
    dialog = ManualSpectrumSaveDialog(
        None,
        trace_choices=(("raw", "Raw"),),
        metadata_values=(),
        default_destination="manual.h5",
        elab_upload_available=True,
        elab_upload_hint="Custom template ready.",
    )
    try:
        assert dialog.elab_upload_hint.text() == "Custom template ready."
        dialog.upload_to_elab.setChecked(True)
        assert "eLab upload is enabled" in dialog.elab_upload_hint.text()
        dialog.upload_to_elab.setChecked(False)
        assert dialog.elab_upload_hint.text() == "Custom template ready."
    finally:
        dialog.close()
        application.processEvents()


def test_analysis_settings_dialog_uses_segmented_tabs_and_uniform_widths(application):
    parent = QWidget()
    dialog = SpectrumAnalysisSettingsDialog(parent)
    try:
        dialog.show()
        application.processEvents()
        assert dialog.section_tabs is not None
        assert dialog.narrow_width.isVisible()
        assert not dialog.peak_card.isVisible()
        assert dialog.narrow_width.width() == dialog.additional_bands.width() == 180

        dialog.focus_peaks()
        application.processEvents()
        assert dialog.peak_card.isVisible()
        assert dialog.peak_min_width.isVisibleTo(dialog)
    finally:
        dialog.close()
        parent.close()
        application.processEvents()
