from __future__ import annotations

import datetime
import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QGridLayout, QWidget
from qfluentwidgets import isDarkTheme

from app.devices.anritsu_ms2830a.ui.page import (
    _AnritsuSpectrogramWindow,
    _AnritsuSpectrumWindow,
    AnritsuPage,
)
from app.devices.keithley_2600.ui.page import _KeithleyFloatingPanelWindow
from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.settings import SettingsRepository
from app.spectrum.analysis import SpectrumAnalysisParameters, SpectrumCleanupResult
from app.ui.dialogs import StationDialog
from app.ui.widgets import SpectrumPlotWidget
from tests.helpers import SETTINGS_TEMPLATE


class SpectrumPlotAndFloatingFixesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_station_dialog_resizable_mode(self) -> None:
        dialog = StationDialog(resizable=True)
        try:
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog._is_resizable)
            self.assertFalse(dialog.titleBar.minBtn.isHidden())
            self.assertFalse(dialog.titleBar.maxBtn.isHidden())
            self.assertTrue(dialog.titleBar._isDoubleClickEnabled)
            self.assertTrue(dialog._isResizeEnabled)
        finally:
            dialog.close()

    def test_anritsu_floating_spectrum_window_is_resizable(self) -> None:
        parent = QWidget()
        window = _AnritsuSpectrumWindow(parent)
        try:
            window.show()
            self.application.processEvents()
            self.assertTrue(window._is_resizable)
            self.assertFalse(window.titleBar.minBtn.isHidden())
            self.assertFalse(window.titleBar.maxBtn.isHidden())
            self.assertTrue(window.titleBar._isDoubleClickEnabled)
            self.assertTrue(window._isResizeEnabled)
        finally:
            window.close()
            parent.deleteLater()

    def test_anritsu_floating_spectrogram_window_is_resizable(self) -> None:
        parent = QWidget()
        window = _AnritsuSpectrogramWindow(parent)
        try:
            window.show()
            self.application.processEvents()
            self.assertTrue(window._is_resizable)
            self.assertFalse(window.titleBar.minBtn.isHidden())
            self.assertFalse(window.titleBar.maxBtn.isHidden())
            self.assertTrue(window.titleBar._isDoubleClickEnabled)
            self.assertTrue(window._isResizeEnabled)
        finally:
            window.close()
            parent.deleteLater()

    def test_keithley_floating_panel_window_is_resizable(self) -> None:
        parent = QWidget()
        panel = QWidget()
        window = _KeithleyFloatingPanelWindow("Plot", panel, parent)
        try:
            window.show()
            self.application.processEvents()
            self.assertTrue(window._is_resizable)
            self.assertFalse(window.titleBar.minBtn.isHidden())
            self.assertFalse(window.titleBar.maxBtn.isHidden())
            self.assertTrue(window.titleBar._isDoubleClickEnabled)
            self.assertTrue(window._isResizeEnabled)
        finally:
            window.close()
            parent.deleteLater()

    def test_spectrum_plot_widget_initializes_with_active_theme(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            expected_theme = "dark" if isDarkTheme() else "light"
            self.assertEqual(plot._theme_name, expected_theme)
        finally:
            plot.close()

    def test_floating_spectrum_window_initializes_with_active_theme(self) -> None:
        parent = QWidget()
        window = _AnritsuSpectrumWindow(parent)
        try:
            expected_theme = "dark" if isDarkTheme() else "light"
            self.assertEqual(window.spectrum._theme_name, expected_theme)
        finally:
            window.close()
            parent.deleteLater()

    def test_station_dialog_propagates_theme_change_to_child_plots(self) -> None:
        dialog = StationDialog()
        try:
            layout = dialog.modal_content_layout()
            plot = SpectrumPlotWidget(parent=dialog)
            layout.addWidget(plot)
            plot.apply_theme("light" if isDarkTheme() else "dark")
            # Trigger changeEvent with StyleChange
            event = QEvent(QEvent.Type.StyleChange)
            dialog.changeEvent(event)
            expected_theme = "dark" if isDarkTheme() else "light"
            self.assertEqual(plot._theme_name, expected_theme)
        finally:
            dialog.close()

    def test_spectrum_plot_preserves_legend_visibility_toggle(self) -> None:
        plot = SpectrumPlotWidget(legend=True)
        try:
            x = [1.0, 2.0, 3.0]
            y = [-10.0, -20.0, -30.0]
            plot.set_trace("Processed", x, y)
            curve = plot._curves["Processed"]
            self.assertTrue(curve.isVisible())

            # Simulate user toggling trace off in the legend (eye click)
            plot.set_trace_visibility("Processed", False)
            self.assertFalse(curve.isVisible())

            # Now incoming new data arrives: set_trace should preserve False!
            plot.set_trace("Processed", x, [-15.0, -25.0, -35.0])
            self.assertFalse(curve.isVisible())

            # Simulate user toggling trace back on
            plot.set_trace_visibility("Processed", True)
            self.assertTrue(curve.isVisible())

            # Next update stays visible
            plot.set_trace("Processed", x, [-12.0, -22.0, -32.0])
            self.assertTrue(curve.isVisible())
        finally:
            plot.close()

    def test_anritsu_page_has_show_analysis_checkbox(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            self.assertTrue(hasattr(page, "show_analysis"))
            self.assertTrue(page.show_analysis.isChecked())
        finally:
            page.close()

    def test_anritsu_can_display_only_analysis_with_processed_hidden(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            freqs = (1.0e9, 2.0e9, 3.0e9)
            raw = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=(-20.0, -15.0, -25.0),
                trace_name="TRAC1",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            ref = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=(-30.0, -30.0, -30.0),
                trace_name="REF",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            page._reference_trace = ref
            idx = page.reference_operation.findData("difference_db")
            page.reference_operation.setCurrentIndex(idx)
            page._show_trace(raw, update_controls=False)

            # Set cleanup mode to denoise and analysis source to processed
            idx_cleanup = page.cleanup_mode.findData("denoise")
            self.assertGreaterEqual(idx_cleanup, 0)
            page.cleanup_mode.setCurrentIndex(idx_cleanup)

            idx_source = page.analysis_source.findData("processed")
            self.assertGreaterEqual(idx_source, 0)
            page.analysis_source.setCurrentIndex(idx_source)

            # Set a simulated cleanup result on "processed"
            cleanup = SpectrumCleanupResult(
                values_dbm=(10.0, 15.0, 5.0),
                unit="dB",
                method="denoise",
                noise_sigma_db=0.5,
                stationary_interference_indices=(),
            )
            page._cleanup_result = cleanup

            # Now hide Processed, Raw, Averaged, Reference — keep ONLY Analysis!
            page.show_raw.setChecked(False)
            page.show_average.setChecked(False)
            page.show_reference.setChecked(False)
            page.show_processed.setChecked(False)
            page.show_analysis.setChecked(True)
            page._refresh_spectrum_display()

            # Verify: Processed is not displayed, but Analysis IS displayed!
            traces = [t.key for t in page._display_state.traces]
            self.assertNotIn("processed", traces)
            self.assertNotIn("raw", traces)
            self.assertTrue(any(k.startswith("analysis:") for k in traces), f"Traces: {traces}")

            # Plot has only Analysis curve active
            self.assertTrue(page.spectrum_plot._curves["Analysis"].isVisible())
            self.assertFalse(page.spectrum_plot._curves["Processed"].isVisible())
        finally:
            page.close()

    def test_anritsu_does_not_wipe_cleanup_result_on_new_trace(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            freqs = (1.0e9, 2.0e9, 3.0e9)
            cleanup = SpectrumCleanupResult(
                values_dbm=(-20.0, -15.0, -25.0),
                unit="dBm",
                method="wavelet",
                noise_sigma_db=0.5,
                stationary_interference_indices=(),
            )
            page._cleanup_result = cleanup

            trace2 = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=(-21.0, -14.0, -26.0),
                trace_name="TRAC1",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            # New frame arrives:
            page._show_trace(trace2, update_controls=False)

            # Cleanup result MUST NOT be None!
            self.assertIsNotNone(page._cleanup_result)
            self.assertEqual(page._cleanup_result.method, "wavelet")
        finally:
            page.close()

    def test_display_controls_changed_does_not_invalidate_cleanup_result(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            cleanup = SpectrumCleanupResult(
                values_dbm=(-20.0, -15.0, -25.0),
                unit="dBm",
                method="wavelet",
                noise_sigma_db=0.5,
                stationary_interference_indices=(),
            )
            page._cleanup_result = cleanup
            # Toggle show_raw
            page.show_raw.setChecked(not page.show_raw.isChecked())
            self.assertIsNotNone(page._cleanup_result)
        finally:
            page.close()

    def test_spectrum_analysis_settings_dialog_fields_and_apply(self) -> None:
        parent = QWidget()
        initial = SpectrumAnalysisParameters(
            denoise_window=9,
            emi_threshold_db=10.0,
            emi_max_std_db=0.75,
            emi_min_frames=5,
            peak_min_snr_db=6.0,
            peak_min_prominence_db=3.0,
            peak_max_count=20,
            peak_fit_models=True,
        )
        dialog = SpectrumAnalysisSettingsDialog(parent, current_parameters=initial)
        try:
            self.assertEqual(dialog.denoise_window.value(), 9)
            self.assertAlmostEqual(dialog.emi_threshold.value(), 10.0)
            self.assertAlmostEqual(dialog.emi_max_std.value(), 0.75)
            self.assertEqual(dialog.emi_min_frames.value(), 5)
            self.assertAlmostEqual(dialog.peak_snr.value(), 6.0)
            self.assertAlmostEqual(dialog.peak_prominence.value(), 3.0)
            self.assertEqual(dialog.peak_max_count.value(), 20)
            self.assertTrue(dialog.peak_fit_models.isChecked())

            # Change values
            dialog.denoise_window.setValue(15)
            dialog.emi_threshold.setValue(14.0)
            dialog.emi_max_std.setValue(1.2)
            dialog.emi_min_frames.setValue(8)
            dialog.peak_snr.setValue(12.0)
            dialog.peak_prominence.setValue(5.0)
            dialog.peak_max_count.setValue(50)
            dialog.peak_fit_models.setChecked(False)

            captured_params: list[SpectrumAnalysisParameters] = []
            dialog.parameters_applied.connect(captured_params.append)

            # Click Apply
            dialog.apply_button.click()
            self.assertEqual(len(captured_params), 1)
            p = captured_params[0]
            self.assertEqual(p.denoise_window, 15)
            self.assertAlmostEqual(p.emi_threshold_db, 14.0)
            self.assertAlmostEqual(p.emi_max_std_db, 1.2)
            self.assertEqual(p.emi_min_frames, 8)
            self.assertAlmostEqual(p.peak_min_snr_db, 12.0)
            self.assertAlmostEqual(p.peak_min_prominence_db, 5.0)
            self.assertEqual(p.peak_max_count, 50)
            self.assertFalse(p.peak_fit_models)

            # Click Reset to defaults
            dialog.reset_button.click()
            self.assertEqual(dialog.denoise_window.value(), 9)
            self.assertAlmostEqual(dialog.emi_threshold.value(), 10.0)
            self.assertAlmostEqual(dialog.emi_max_std.value(), 0.75)
            self.assertEqual(dialog.emi_min_frames.value(), 5)
            self.assertAlmostEqual(dialog.peak_snr.value(), 6.0)
            self.assertAlmostEqual(dialog.peak_prominence.value(), 3.0)
            self.assertEqual(dialog.peak_max_count.value(), 20)
            self.assertTrue(dialog.peak_fit_models.isChecked())
        finally:
            dialog.close()
            parent.deleteLater()

    def test_anritsu_page_configure_analysis_opens_settings_dialog(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            self.assertTrue(hasattr(page, "configure_analysis"))
            self.assertEqual(page.configure_analysis.text(), "Parameters…")
            self.assertIsNone(page._analysis_settings_dialog)

            # Open settings dialog
            page.configure_analysis.click()
            self.assertIsNotNone(page._analysis_settings_dialog)
            dialog = page._analysis_settings_dialog
            self.assertTrue(dialog.isVisible())

            # Apply new parameters through dialog
            new_params = SpectrumAnalysisParameters(denoise_window=13)
            dialog.parameters_applied.emit(new_params)
            self.assertEqual(page._analysis_parameters.denoise_window, 13)

            # Close dialog
            dialog.close()
            self.assertIsNone(page._analysis_settings_dialog)
        finally:
            page.close()

    def test_smooth_switching_between_raw_and_processed(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            freqs = (1.0e9, 2.0e9, 3.0e9)
            raw = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=(-20.0, -15.0, -25.0),
                trace_name="TRAC1",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            ref = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=(-30.0, -30.0, -30.0),
                trace_name="REF",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            page._reference_trace = ref
            idx = page.reference_operation.findData("difference_db")
            page.reference_operation.setCurrentIndex(idx)
            page._show_trace(raw, update_controls=False)

            # Selecting reference operation difference_db automatically activates Processed
            self.assertTrue(page.show_processed.isChecked())
            self.assertFalse(page.show_raw.isChecked())
            self.assertTrue(page.spectrum_plot._curves["Processed"].isVisible())
            raw_curve = page.spectrum_plot._curves.get("Raw")
            self.assertTrue(raw_curve is None or not raw_curve.isVisible())

            # User clicks Raw
            page.show_raw.setChecked(True)
            self.application.processEvents()

            # Raw must now be checked and Processed automatically unchecked
            self.assertTrue(page.show_raw.isChecked())
            self.assertFalse(page.show_processed.isChecked())
            self.assertTrue(page.spectrum_plot._curves["Raw"].isVisible())
            self.assertFalse(page.spectrum_plot._curves["Processed"].isVisible())

            # User clicks Processed
            page.show_processed.setChecked(True)
            self.application.processEvents()

            # Processed must now be checked and Raw automatically unchecked
            self.assertTrue(page.show_processed.isChecked())
            self.assertFalse(page.show_raw.isChecked())
            self.assertTrue(page.spectrum_plot._curves["Processed"].isVisible())
            self.assertFalse(page.spectrum_plot._curves["Raw"].isVisible())
        finally:
            page.close()

    def test_analysis_completed_applied_across_live_frames(self) -> None:
        from app.devices.anritsu_ms2830a.ui.analysis_worker import SpectrumAnalysisOutcome

        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            freqs = (1.0e9, 2.0e9, 3.0e9)
            raw = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=(-20.0, -15.0, -25.0),
                trace_name="TRAC1",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            idx_cleanup = page.cleanup_mode.findData("denoise")
            page.cleanup_mode.setCurrentIndex(idx_cleanup)
            page._show_trace(raw, update_controls=False)

            gen = page._analysis_generation

            # Simulate 3 more live frames arriving while analysis is in progress
            for _ in range(3):
                page._show_trace(raw, update_controls=False)

            # Analysis outcome for earlier generation finishes
            cleanup = SpectrumCleanupResult(
                values_dbm=(-19.5, -15.2, -24.8),
                unit="dBm",
                method="denoise",
                noise_sigma_db=0.4,
                stationary_interference_indices=(),
            )
            outcome = SpectrumAnalysisOutcome(
                generation=gen,
                frame_id=1,
                source_key="raw",
                source_unit="dBm",
                cleanup=cleanup,
                peaks=(),
            )
            page._analysis_completed(outcome)
            self.application.processEvents()

            # The outcome must NOT be discarded; it must be applied!
            self.assertIsNotNone(page._cleanup_result)
            self.assertEqual(page._cleanup_result.method, "denoise")
            self.assertTrue(page.spectrum_plot._curves["Analysis"].isVisible())
        finally:
            page.close()

    def test_spectrum_plot_legend_synchronization(self) -> None:
        plot = SpectrumPlotWidget(legend=True)
        try:
            x = np.array([1e6, 2e6, 3e6])
            y1 = np.array([-70.0, -60.0, -80.0])
            y2 = np.array([10.0, 20.0, 5.0])

            plot.set_trace("Raw", x, y1)
            legend = plot.plot.getPlotItem().legend
            self.assertIsNotNone(legend)
            legend_names = [label.text for _, label in legend.items]
            self.assertEqual(legend_names, ["Raw"])

            plot.set_trace("Processed", x, y2)
            legend_names = [label.text for _, label in legend.items]
            self.assertEqual(legend_names, ["Raw", "Processed"])

            # Hide Raw -> legend must contain only Processed
            plot.set_trace_visibility("Raw", False)
            legend_names = [label.text for _, label in legend.items]
            self.assertEqual(legend_names, ["Processed"])

            # Clear Processed -> legend must be empty
            plot.clear_trace("Processed")
            legend_names = [label.text for _, label in legend.items]
            self.assertEqual(legend_names, [])

            # Re-show Raw -> legend must contain Raw again
            plot.set_trace_visibility("Raw", True)
            legend_names = [label.text for _, label in legend.items]
            self.assertEqual(legend_names, ["Raw"])
        finally:
            plot.close()

    def test_anritsu_trace_checkbox_toggling_and_auto_range(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            freqs = tuple(float(f) for f in np.linspace(1e9, 2e9, 101))
            powers = tuple(float(p) for p in np.random.uniform(-80.0, -70.0, 101))
            raw = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=powers,
                trace_name="TRAC1",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            ref = SpectrumTrace(
                frequencies_hz=freqs,
                powers_dbm=tuple(-30.0 for _ in freqs),
                trace_name="REF",
                acquired_at_utc=datetime.datetime.now(datetime.timezone.utc),
            )
            page._reference_trace = ref
            idx = page.reference_operation.findData("difference_db")
            page.reference_operation.setCurrentIndex(idx)
            page._show_trace(raw, update_controls=False)

            # Initially after difference_db: Processed is checked, Raw is unchecked
            self.assertTrue(page.show_processed.isChecked())
            self.assertFalse(page.show_raw.isChecked())
            legend = page.spectrum_plot.plot.getPlotItem().legend
            self.assertIsNotNone(legend)
            legend_names = [label.text for _, label in legend.items]
            self.assertIn("Processed", legend_names)
            self.assertNotIn("Raw", legend_names)

            # Check Raw -> Processed should be unchecked, Raw checked
            page.show_raw.setChecked(True)
            self.assertTrue(page.show_raw.isChecked())
            self.assertFalse(page.show_processed.isChecked())
            legend_names = [label.text for _, label in legend.items]
            self.assertIn("Raw", legend_names)
            self.assertNotIn("Processed", legend_names)
            # Unit must be dBm, Y label Amplitude, Y range around [-80, -70]
            self.assertEqual(page._active_spectrum_unit, "dBm")
            y_range = page.spectrum_plot.plot.getViewBox().viewRange()[1]
            self.assertLess(y_range[0], -70.0)
            self.assertGreater(y_range[1], -80.0)

            # Switch back to Processed -> Raw unchecked, Processed checked
            page.show_processed.setChecked(True)
            self.assertTrue(page.show_processed.isChecked())
            self.assertFalse(page.show_raw.isChecked())
            legend_names = [label.text for _, label in legend.items]
            self.assertIn("Processed", legend_names)
            self.assertNotIn("Raw", legend_names)
            self.assertEqual(page._active_spectrum_unit, "dB")

            # Unchecking all traces (including Analysis) must fallback to Raw so plot is never blank
            page.show_analysis.setChecked(False)
            page.show_processed.setChecked(False)
            self.assertTrue(page.show_raw.isChecked())
            self.assertFalse(page.show_processed.isChecked())
        finally:
            page.close()

    def test_anritsu_signal_analysis_card_no_overlapping_widgets(self) -> None:
        controller = MagicMock()
        controller.visa_address = "TCPIP::192.168.1.10::INSTR"
        controller.is_connected = False
        settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
        page = AnritsuPage(controller, settings, single_sweep_available=True)
        try:
            layout = page.signal_analysis_card.layout()
            self.assertIsInstance(layout, QGridLayout)
            occupied_cells: dict[tuple[int, int], QWidget] = {}
            for i in range(layout.count()):
                item = layout.itemAt(i)
                widget = item.widget()
                if widget is not None:
                    row, col, row_span, col_span = layout.getItemPosition(i)
                    for r in range(row, row + row_span):
                        for c in range(col, col + col_span):
                            cell = (r, c)
                            self.assertNotIn(
                                cell,
                                occupied_cells,
                                f"Grid collision at cell {cell}: {widget} overlaps with {occupied_cells.get(cell)}"
                            )
                            occupied_cells[cell] = widget
        finally:
            page.close()


