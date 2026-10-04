import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timezone
from pathlib import Path
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import Theme

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.settings import SettingsRepository
from app.spectrum.analysis import SpectrumAnalysisParameters
from app.ui.design_system import apply_application_theme


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    return app


@pytest.mark.parametrize("theme,width", [(Theme.LIGHT, 720), (Theme.DARK, 580)])
def test_narrow_parameters_render_apply_protection_and_reject_invalid_units(application, theme, width):
    apply_application_theme(application, "dark" if theme == Theme.DARK else "light")
    parent = QWidget()
    dialog = SpectrumAnalysisSettingsDialog(parent)
    outcomes = []
    dialog.parameters_applied.connect(outcomes.append)
    try:
        dialog.resize(width, 740)
        dialog.show()
        application.processEvents()
        assert dialog.narrow_width.isVisible() and dialog.narrow_width.width() > 100
        assert dialog.apply_button.isVisible()
        dialog.narrow_width.setText("5 mV")
        QTest.mouseClick(dialog.apply_button, Qt.LeftButton)
        application.processEvents()
        assert dialog.validation_error.isVisible() and not outcomes and dialog.isVisible()
        dialog.narrow_width.setText("4 MHz")
        dialog.narrow_protect.setChecked(True)
        assert dialog.narrow_protected_start.isEnabled()
        dialog.narrow_protected_start.setText("510 MHz")
        dialog.narrow_protected_stop.setText("490 MHz")
        dialog.apply_button.click()
        assert not outcomes and "smaller" in dialog.validation_error.text()
        dialog.narrow_protected_stop.setText("530 MHz")
        dialog.narrow_threshold.setValue(7)
        params = dialog.get_parameters()
        assert params.narrow_max_width_hz == 4e6
        assert params.narrow_protected_regions_hz == ((510e6, 530e6),)
        assert params.narrow_threshold_sigma == 7
        output = Path("artifacts/spectrum-narrow-spikes")
        output.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(output / f"parameters-{theme.value}-{width}.png"))
        dialog.apply_button.click()
        assert outcomes == [params]
    finally:
        dialog.close()
        parent.deleteLater()
        apply_application_theme(application, "light")


def test_live_processed_db_source_filters_new_frames_and_preserves_raw(application):
    controller = MagicMock()
    controller.visa_address = "GPIB0::23::INSTR"
    controller.is_connected = False
    settings = SettingsRepository("app/resources/settings.template.yml").load().settings
    page = AnritsuPage(controller, settings, single_sweep_available=True)
    x = np.linspace(200e6, 1200e6, 2001)
    reference = SpectrumTrace(tuple(x), tuple(np.full(x.size, -80.)), datetime.now(timezone.utc), "REF")
    broad = 2 * np.exp(-4 * np.log(2) * ((x - 700e6) / 80e6)**2)
    spikes = np.exp(-4 * np.log(2) * ((x - 450e6) / 3e6)**2)
    try:
        page.resize(1450, 940)
        page.show()
        application.processEvents()
        page._reference_trace = reference
        page.reference_operation.setCurrentIndex(page.reference_operation.findData("difference_db"))
        page._analysis_parameters = SpectrumAnalysisParameters()
        page.cleanup_filters["narrow_reject"].setChecked(True)
        for amplitude in (5., -3.):
            raw = SpectrumTrace(tuple(x), tuple(-80 + broad + amplitude * spikes),
                                datetime.now(timezone.utc), "TRAC1")
            page._show_trace(raw, update_controls=False)
            page.analysis_source.setCurrentIndex(page.analysis_source.findData("processed"))
            page.show_analysis.setChecked(True)
            page._analyze_current_spectrum(force=True)
            deadline = time.monotonic() + 4
            while (page._cleanup_result is None or not page._cleanup_result.removed_peak_indices
                    or page._analysis_controller.busy) and time.monotonic() < deadline:
                application.processEvents()
                time.sleep(.005)
            cleanup = page._cleanup_result
            assert cleanup is not None and cleanup.unit == "dB", (
                page.analysis_status.text(), page._analysis_error, page._analysis_generation,
                page._invalidated_before_generation, page._analysis_source_key)
            assert 500 in cleanup.removed_peak_indices
            assert abs(cleanup.values[500] - broad[500]) < .2
            assert page._latest_trace is raw and page._reference_trace is reference
            assert raw.powers_dbm[500] == pytest.approx(-80 + broad[500] + amplitude)
            assert "bins replaced" in page.analysis_status.text()
            assert page.spectrum_plot._curves["Analysis"].isVisible()
        page.highlight_replacements.setChecked(True)
        page._sync_peak_markers()
        assert len(page.spectrum_plot.replacement_markers.points()) > 0
        assert page.highlight_peaks.text() == "Markers"
        selected = []
        page.spectrum_plot.peak_selected.connect(selected.append)
        page.spectrum_plot._peak_marker_clicked(None, [page.spectrum_plot.replacement_markers.points()[0]], None)
        assert not selected  # A replacement is not an unrelated detected peak.
        assert page.spectrum_plot.width() > 300 and page.spectrum_plot.height() > 100
        output = Path("artifacts/spectrum-narrow-spikes")
        output.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(output / "live-processed-width-filter.png"))
        controller.call.assert_not_called()
    finally:
        page.close()
        application.processEvents()
