"""Tests for Anritsu MS2830A fast binary trace acquisition, bandwidth controls, and readback reconciliation."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from qfluentwidgets import TableWidget

from app.devices.anritsu_ms2830a import (
    AnritsuAdapter,
    AnritsuFullConfigurationReadback,
    SpectrumConfig,
    SpectrumTrace,
)
from app.devices.anritsu_ms2830a.ui.page import AnritsuSpectrumConfigurationPanel
from app.devices.anritsu_ms2830a.ui.readback_dialog import AnritsuReadbackDialog
from app.devices.anritsu_ms2830a.module import MODULE as ANRITSU_MODULE
from app.devices.simulators import SimulatedVisaFactory
from app.safety.anritsu import normalize_anritsu_detector
from app.settings import SettingsRepository


def _simulation_settings():
    repo = SettingsRepository(".config/settings.yml")
    return repo.load().settings


class AnritsuFastAcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.settings = _simulation_settings()
        self.adapter = AnritsuAdapter(
            self.settings,
            session_factory=SimulatedVisaFactory("anritsu"),
        )
        self.adapter.connect()

    def tearDown(self) -> None:
        self.adapter.disconnect()

    def test_read_full_configuration(self) -> None:
        readback = self.adapter.read_full_configuration()
        self.assertIsInstance(readback, AnritsuFullConfigurationReadback)
        self.assertGreater(readback.start_hz, 0)
        self.assertGreater(readback.stop_hz, readback.start_hz)
        self.assertEqual(readback.points, 1001)
        self.assertTrue(readback.rbw_auto)
        self.assertTrue(readback.vbw_auto)
        self.assertIn(readback.vbw_mode, {"VID", "POW"})
        self.assertEqual(readback.instrument_mode, "SPECT")
        self.assertTrue(readback.continuous_sweep)
        self.assertGreater(readback.average_count, 0)

    def test_dispatch_read_full_configuration(self) -> None:
        result = ANRITSU_MODULE.dispatch(self.adapter, "read_full_configuration", None)
        self.assertIsInstance(result, AnritsuFullConfigurationReadback)

    def test_acquire_fresh_trace(self) -> None:
        trace = self.adapter.acquire_fresh_trace("TRAC1", timeout_s=2.0)
        self.assertIsInstance(trace, SpectrumTrace)
        self.assertEqual(len(trace.powers_dbm), 1001)
        self.assertEqual(len(trace.frequencies_hz), 1001)

    def test_dispatch_acquire_fresh_trace(self) -> None:
        trace = ANRITSU_MODULE.dispatch(self.adapter, "acquire_fresh_trace", "TRAC1")
        self.assertIsInstance(trace, SpectrumTrace)
        self.assertEqual(len(trace.powers_dbm), 1001)

    def test_spectrum_config_with_bandwidth_filters(self) -> None:
        panel = AnritsuSpectrumConfigurationPanel(self.settings)
        panel.rbw_mode.setCurrentIndex(panel.rbw_mode.findData("manual"))
        panel.rbw.setText("1 MHz")
        panel.vbw_auto.setCurrentIndex(panel.vbw_auto.findData("manual"))
        panel.vbw_mode.setCurrentIndex(panel.vbw_mode.findData("VID"))
        panel.vbw.setText("100 kHz")

        config = panel.spectrum_config("TRAC1")
        self.assertIsInstance(config, SpectrumConfig)
        self.assertFalse(config.rbw_auto)
        self.assertAlmostEqual(config.rbw_hz, 1e6)
        self.assertFalse(config.vbw_auto)
        self.assertEqual(config.vbw_mode, "VID")
        self.assertAlmostEqual(config.vbw_hz, 1e5)

    def test_readback_dialog_matches_and_mismatches(self) -> None:
        readback = AnritsuFullConfigurationReadback(
            start_hz=1e6,
            stop_hz=10e6,
            center_hz=5.5e6,
            span_hz=9e6,
            reference_level_dbm=0.0,
            points=1001,
            rbw_auto=True,
            rbw_hz=1e3,
            vbw_auto=True,
            vbw_mode="VID",
            vbw_hz=1e3,
            sweep_time_auto=True,
            sweep_time_s=0.1,
            attenuation_auto=True,
            attenuation_db=10.0,
            detector="NORM",
            continuous_sweep=True,
            average_count=10,
            instrument_mode="SPECT",
        )
        form_values = {
            "start_hz": 1e6,        # Match
            "stop_hz": 5e6,         # Mismatch (hw: 10 MHz, form: 5 MHz)
            "center_hz": 3e6,
            "span_hz": 4e6,
            "reference_level_dbm": 0.0, # Match
            "points": 1001,         # Match
            "rbw_auto": True,       # Match
            "rbw_hz": None,
            "vbw_auto": False,      # Mismatch (hw: True, form: False)
            "vbw_mode": "VID",      # Match
            "vbw_hz": 500.0,
            "average_count": 50,    # Mismatch (hw: 10, form: 50)
        }
        dialog = AnritsuReadbackDialog(readback, form_values, parent=None)
        self.assertIsInstance(dialog.table, TableWidget)
        self.assertEqual(dialog.table.columnCount(), 4)
        self.assertEqual(dialog.table.rowCount(), len(dialog._param_definitions))

        # Check Start frequency status (should be MATCH)
        start_status = dialog._status_items["start_hz"].text()
        self.assertEqual(start_status, "MATCH")

        # Check Stop frequency status (should show form mismatch)
        stop_status = dialog._status_items["stop_hz"].text()
        self.assertIn("Form: 5 MHz", stop_status)

        # Test individual assignment
        assigned_params = []
        dialog.assign_requested.connect(lambda k, v: assigned_params.append((k, v)))
        btn = dialog._action_buttons["stop_hz"]
        btn.click()
        self.assertEqual(len(assigned_params), 1)
        self.assertEqual(assigned_params[0][0], "stop_hz")
        self.assertEqual(assigned_params[0][1], 10e6)
        self.assertEqual(dialog._status_items["stop_hz"].text(), "MATCH")

        # Test use all compatible values
        all_assigned = []
        dialog.assign_all_requested.connect(lambda rb: all_assigned.append(rb))
        dialog.use_all_button.click()
        self.assertEqual(len(all_assigned), 1)
        self.assertEqual(all_assigned[0], readback)

        dialog.close()

    def test_fetch_current_trace_fast(self) -> None:
        trace = self.adapter.fetch_current_trace_fast("TRAC1")
        self.assertIsInstance(trace, SpectrumTrace)
        self.assertEqual(len(trace.powers_dbm), 1001)
        self.assertEqual(len(trace.frequencies_hz), 1001)

    def test_dispatch_fetch_current_trace_fast(self) -> None:
        trace = ANRITSU_MODULE.dispatch(self.adapter, "fetch_current_trace_fast", "TRAC1")
        self.assertIsInstance(trace, SpectrumTrace)
        self.assertEqual(len(trace.powers_dbm), 1001)

    def test_averaging_stats_display(self) -> None:
        from datetime import datetime, timezone
        from PySide6.QtCore import QObject, Signal
        from app.devices.anritsu_ms2830a.ui.page import AnritsuPage

        class DummyController(QObject):
            result = Signal(str, object)
            error = Signal(str, str)
            state_changed = Signal(str)

            def __init__(self) -> None:
                super().__init__()
                self.calls: list[tuple[str, object]] = []

            def call(self, operation: str, payload: object = None) -> None:
                self.calls.append((operation, payload))

        controller = DummyController()
        page = AnritsuPage(controller, self.settings, single_sweep_available=True)
        try:
            # Initial state
            self.assertEqual(page.average_stats_label.text(), "No averaging performed yet")

            # Set average count to 5 and start
            page.average_count.setValue(5)
            page.start_averaging()
            self.assertTrue(page._averaging_active)
            self.assertEqual(page.average_stats_label.text(), "Averaging spectrum: 0 / 5 spectra…")

            # Feed frames
            trace = SpectrumTrace(
                frequencies_hz=(1e6, 2e6),
                powers_dbm=(-50.0, -40.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            # Frame 1
            controller.result.emit("fetch_current_trace_fast", trace)
            self.assertIn("Progress: 1 / 5 spectra", page.average_stats_label.text())
            self.assertIn("/s", page.average_stats_label.text())

            # Frames 2, 3, 4
            for i in range(2, 5):
                controller.result.emit("fetch_current_trace_fast", trace)
                self.assertIn(f"Progress: {i} / 5 spectra", page.average_stats_label.text())

            # Frame 5 (completion)
            controller.result.emit("fetch_current_trace_fast", trace)
            self.assertFalse(page._averaging_active)
            self.assertIn("Completed: 5 spectra taken in ", page.average_stats_label.text())
            self.assertIn("/s", page.average_stats_label.text())
            self.assertIn("Averaged spectrum completed: 5 spectra taken in ", page.info.text())
        finally:
            page.close()

    def test_averaging_cancellation_stats(self) -> None:
        from datetime import datetime, timezone
        from PySide6.QtCore import QObject, Signal
        from app.devices.anritsu_ms2830a.ui.page import AnritsuPage

        class DummyController(QObject):
            result = Signal(str, object)
            error = Signal(str, str)
            state_changed = Signal(str)

            def call(self, operation: str, payload: object = None) -> None:
                pass

        controller = DummyController()
        page = AnritsuPage(controller, self.settings, single_sweep_available=True)
        try:
            page.average_count.setValue(10)
            page.start_averaging()
            trace = SpectrumTrace(
                frequencies_hz=(1e6, 2e6),
                powers_dbm=(-50.0, -40.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            controller.result.emit("fetch_current_trace_fast", trace)
            controller.result.emit("fetch_current_trace_fast", trace)
            page.cancel_averaging()
            self.assertFalse(page._averaging_active)
            self.assertIn("Averaging cancelled at 2 / 10 spectra", page.average_stats_label.text())
        finally:
            page.close()

    def test_detector_nrm_norm_reconciliation(self) -> None:
        # Unit normalization checks
        self.assertEqual(normalize_anritsu_detector("NRM"), "NORM")
        self.assertEqual(normalize_anritsu_detector("NORMAL"), "NORM")
        self.assertEqual(normalize_anritsu_detector("NORM"), "NORM")
        self.assertEqual(normalize_anritsu_detector("POS"), "POS")
        self.assertEqual(normalize_anritsu_detector("positive"), "POS")
        self.assertEqual(normalize_anritsu_detector("SAMPLE"), "SAMP")

        # Readback dialog comparison with NRM (hardware) vs NORM (form)
        readback = AnritsuFullConfigurationReadback(
            start_hz=1e6,
            stop_hz=10e6,
            center_hz=5.5e6,
            span_hz=9e6,
            reference_level_dbm=0.0,
            points=1001,
            rbw_auto=True,
            rbw_hz=1e3,
            vbw_auto=True,
            vbw_mode="VID",
            vbw_hz=1e3,
            sweep_time_auto=True,
            sweep_time_s=0.1,
            attenuation_auto=True,
            attenuation_db=10.0,
            detector="NRM",
            continuous_sweep=True,
            average_count=10,
            instrument_mode="SPECT",
        )
        form_values = {
            "start_hz": 1e6,
            "stop_hz": 10e6,
            "reference_level_dbm": 0.0,
            "points": 1001,
            "rbw_auto": True,
            "vbw_auto": True,
            "vbw_mode": "VID",
            "detector": "NORM",
            "average_count": 10,
        }
        dialog = AnritsuReadbackDialog(readback, form_values, parent=None)
        try:
            # Detector row text should be displayed as normalized "NORM"
            row_idx = None
            for r, row_def in enumerate(dialog._param_definitions):
                if row_def[0] == "detector":
                    row_idx = r
                    break
            self.assertIsNotNone(row_idx)
            hw_item = dialog.table.item(row_idx, 1)
            self.assertEqual(hw_item.text(), "NORM")

            # Status should be MATCH
            detector_status = dialog._status_items["detector"].text()
            self.assertEqual(detector_status, "MATCH")

            # Now verify when form differs (e.g. POS vs NRM)
            form_diff = dict(form_values)
            form_diff["detector"] = "POS"
            dialog_diff = AnritsuReadbackDialog(readback, form_diff, parent=None)
            try:
                diff_status = dialog_diff._status_items["detector"].text()
                self.assertEqual(diff_status, "Form: POS")
            finally:
                dialog_diff.close()
        finally:
            dialog.close()

        # Adapter readback normalization when hardware returns "NRM"
        self.adapter._session.detector = "NRM"
        cfg = self.adapter.read_full_configuration()
        self.assertEqual(cfg.detector, "NORM")

        adv = self.adapter.read_advanced_spectrum_configuration()
        self.assertEqual(adv.detector, "NORM")

    def test_live_fast_mode_and_refresh_interval(self) -> None:
        from datetime import datetime, timezone
        from PySide6.QtCore import QObject, Signal
        from app.devices.anritsu_ms2830a.ui.page import AnritsuPage

        class DummyController(QObject):
            result = Signal(str, object)
            error = Signal(str, str)
            state_changed = Signal(str)

            def __init__(self) -> None:
                super().__init__()
                self.calls: list[tuple[str, object]] = []

            def call(self, operation: str, payload: object = None) -> None:
                self.calls.append((operation, payload))

        # Check adapter fetch_current_trace uses fast binary transfer
        trace = self.adapter.fetch_current_trace("TRAC1")
        self.assertIsInstance(trace, SpectrumTrace)
        self.assertEqual(len(trace.powers_dbm), 1001)

        controller = DummyController()
        page = AnritsuPage(controller, self.settings, single_sweep_available=True)
        try:
            # Default refresh interval
            self.assertEqual(page.refresh.value(), 50)

            # Test dynamic refresh interval change
            page.refresh.setValue(100)
            self.assertEqual(page.refresh.value(), 100)

            # Simulate starting Live
            page.toggle_live()
            self.assertTrue(any(call[0] == "start_live" for call in controller.calls))

            # Simulate start_live result from controller
            from app.devices.anritsu_ms2830a import AnritsuConfigurationSnapshot
            snapshot = AnritsuConfigurationSnapshot(
                start_hz=1e6, stop_hz=10e6, reference_level_dbm=0.0, points=1001
            )
            controller.result.emit("start_live", snapshot)

            self.assertTrue(page._timer.isActive())
            self.assertEqual(page._timer.interval(), 100)
            self.assertEqual(page.live.text(), "Stop Live")

            # Simulate frames arriving
            sample_trace = SpectrumTrace(
                frequencies_hz=(1e6, 2e6),
                powers_dbm=(-50.0, -40.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            controller.result.emit("fetch_current_trace_fast", sample_trace)
            self.assertEqual(page._live_frame_count, 1)
            self.assertIn("FRAME 1", page.live_indicator.text())

            # Simulate stop Live
            page.toggle_live()
            self.assertTrue(any(call[0] == "stop_live" for call in controller.calls))
            controller.result.emit("stop_live", None)
            self.assertFalse(page._timer.isActive())
            self.assertEqual(page.live.text(), "Start Live")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
