"""Regression tests for PeakTableDialog, SpectrumWorkbench, and SpectrumAnalysisWorker."""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHeaderView, QWidget

from app.devices.anritsu_ms2830a.ui.analysis_worker import (
    SpectrogramAnalysisOutcome,
    SpectrogramAnalysisRequest,
    SpectrumAnalysisController,
    SpectrumAnalysisOutcome,
    SpectrumAnalysisRequest,
)
from app.devices.anritsu_ms2830a.ui.peak_analysis import PeakTableDialog, PeakTrackingWindow
from app.devices.anritsu_ms2830a.ui.spectrum_workbench import SpectrumWorkbench
from app.spectrum import SpectrumAnalysisParameters, SpectrumPeak
from app.spectrum import clean_spectrum_dbm as real_clean_spectrum_dbm


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


def _make_peaks(count: int, offset_hz: float = 0.0) -> tuple[SpectrumPeak, ...]:
    return tuple(
        SpectrumPeak(
            index=i * 10,
            frequency_hz=1e9 + i * 5e6 + offset_hz,
            amplitude_dbm=-20.0 - i * 0.5,
            noise_floor_dbm=-85.0,
            snr_db=65.0 - i * 0.5,
            prominence_db=40.0 - i * 0.5,
            fwhm_hz=250e3,
            left_half_power_hz=1e9 + i * 5e6 + offset_hz - 125e3,
            right_half_power_hz=1e9 + i * 5e6 + offset_hz + 125e3,
            fit_model="lorentzian",
            fit_center_hz=1e9 + i * 5e6 + offset_hz,
            fit_fwhm_hz=240e3,
            q_factor=4100.0 + i,
            fit_rmse_db=0.12,
        )
        for i in range(count)
    )


def test_peak_table_dialog_fast_overwrite_and_top_level_floating_flags(application):
    owner = QWidget()
    dialog = PeakTableDialog(owner)
    tracker = PeakTrackingWindow(owner)
    try:
        assert dialog.parentWidget() is None
        assert tracker.parentWidget() is None
        assert dialog.windowFlags() & Qt.WindowType.WindowMinMaxButtonsHint
        assert tracker.windowFlags() & Qt.WindowType.WindowMinMaxButtonsHint
        assert (
            dialog.table.horizontalHeader().sectionResizeMode(0)
            == QHeaderView.ResizeMode.Interactive
        )
        dialog.show()
        tracker.show()
        application.processEvents()
        if sys.platform == "win32" and application.platformName() == "windows":
            import win32con
            import win32gui

            for window in (dialog, tracker):
                hwnd = int(window.winId())
                assert win32gui.GetWindow(hwnd, win32con.GW_OWNER) == 0
                ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                assert bool(ex_style & win32con.WS_EX_APPWINDOW)
                assert not bool(ex_style & win32con.WS_EX_TOOLWINDOW)

        # First population creates items; subsequent overwrites reuse items cleanly.
        dialog.set_peaks(_make_peaks(20, 0.0), method="Raw")
        first_item = dialog.table.item(0, 1)
        durations_ms = []
        for step in range(1, 10):
            peaks = _make_peaks(20, step * 100.0)
            started = time.perf_counter()
            dialog.set_peaks(peaks, method="Raw")
            durations_ms.append((time.perf_counter() - started) * 1000.0)
        assert dialog.table.item(0, 1) is first_item
        assert sum(durations_ms) / len(durations_ms) < 25.0
    finally:
        dialog.close()
        tracker.close()
        owner.close()
        application.processEvents()


def test_spectrum_workbench_handles_nan_gaps_and_all_nan_without_raising(application):
    workbench = SpectrumWorkbench()
    try:
        freqs = np.array([1e6, 2e6, 3e6, 4e6, 5e6, 6e6, 7e6], dtype=float)
        # Spectrum with NaN gaps on both sides of a valid peak at 4 MHz.
        powers = np.array([np.nan, -40.0, -15.0, -10.0, np.nan, -40.0, np.nan], dtype=float)
        workbench.set_trace("Raw", freqs, powers, primary=True)
        workbench.auto_range()
        workbench.band_start.setText("1 MHz")
        workbench.band_stop.setText("7 MHz")
        assert workbench.apply_band()

        text = workbench.band_readout.text()
        assert "Peak: -10 dBm" in text
        assert "Minimum: -40 dBm" in text
        assert "−3 dB width:" in text
        assert "nan" not in text.lower()

        workbench.peak_search()
        assert workbench.marker.value() == pytest.approx(4e6)

        workbench.add_peak_marker()
        assert workbench.frequency_markers["M1"].value() == pytest.approx(4e6)

        # All-NaN trace should report n/a cleanly instead of raising QuantityError.
        all_nan = np.full_like(freqs, np.nan)
        workbench.set_trace("Raw", freqs, all_nan, primary=True)
        assert "Peak: n/a" in workbench.band_readout.text()
        assert "−3 dB width: n/a" in workbench.band_readout.text()

        statuses: list[str] = []
        workbench.status_changed.connect(statuses.append)
        workbench.peak_search()
        assert statuses and "no finite" in statuses[-1].lower()
    finally:
        workbench.tools.close()
        workbench.close()
        application.processEvents()


def test_spectrum_workbench_batches_updates_and_places_readouts_above_axis_ranges(application):
    workbench = SpectrumWorkbench()
    try:
        workbench.tools.resize(340, 700)
        workbench.tools.show()
        application.processEvents()
        assert workbench.marker_readout.y() < workbench.axis_ranges_toggle.y()
        assert workbench.band_readout.y() < workbench.axis_ranges_toggle.y()
        assert not workbench.axis_ranges_container.isVisible()

        calls = 0
        original = workbench.refresh_measurements

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        workbench.refresh_measurements = counted  # type: ignore[method-assign]
        with workbench.batch_updates():
            workbench.clear_trace("Missing1")
            workbench.clear_trace("Missing2")
            workbench.set_labels(y="Power", y_unit="dBm")
            workbench.set_trace("Raw", [1e6, 2e6, 3e6], [-80, -20, -80], primary=True)
            workbench.set_trace("Reference", [1e6, 2e6, 3e6], [-90, -90, -90])
        assert calls == 1

        calls = 0
        workbench.clear_trace("DoesNotExist")
        assert calls == 0
    finally:
        workbench.tools.close()
        workbench.close()
        application.processEvents()


def test_analysis_controller_retains_both_spectrum_and_spectrogram_pending_and_handles_empty(
    application,
):
    controller = SpectrumAnalysisController()
    outcomes: list[object] = []
    errors: list[tuple[int, str]] = []
    controller.result.connect(outcomes.append)
    controller.error.connect(lambda g, m: errors.append((g, m)))

    def slow_cleanup(*args, **kwargs):
        time.sleep(0.06)
        return real_clean_spectrum_dbm(*args, **kwargs)

    freqs = (1e6, 2e6, 3e6, 4e6, 5e6)
    row_a = np.array([-80.0, -79.0, -30.0, -79.0, -80.0], dtype=np.float32)
    row_b = np.array([-70.0, -69.0, -20.0, -69.0, -70.0], dtype=np.float32)
    spec_req_1 = SpectrumAnalysisRequest(1, freqs, tuple(row_a), "raw", (), False)
    spec_req_2 = SpectrumAnalysisRequest(2, freqs, tuple(row_b), "raw", (), False)
    # Filtered spectrogram where display_start_s excludes all rows -> graceful empty outcome.
    empty_gram_req = SpectrogramAnalysisRequest(
        generation=10,
        frequencies_hz=freqs,
        timestamps_s=(1.0, 2.0),
        rows=(row_a, row_b),
        source_unit="dBm",
        modes=(),
        parameters=SpectrumAnalysisParameters(),
        cache_key=("raw", ()),
        display_start_s=999.0,
        frame_ids=(1, 2),
    )
    # Two distinct frames sharing the exact same float timestamp -> distinct frame_ids avoid cache collision.
    same_stamp_gram_req = SpectrogramAnalysisRequest(
        generation=11,
        frequencies_hz=freqs,
        timestamps_s=(5.0, 5.0),
        rows=(row_a, row_b),
        source_unit="dBm",
        modes=("denoise",),
        parameters=SpectrumAnalysisParameters(),
        cache_key=("raw", ("denoise",)),
        frame_ids=(101, 102),
    )

    try:
        with patch(
            "app.devices.anritsu_ms2830a.ui.analysis_worker.clean_spectrum_dbm",
            side_effect=slow_cleanup,
        ):
            controller.submit(spec_req_1)
            controller.submit(spec_req_2)
            controller.submit(empty_gram_req)
            deadline = time.monotonic() + 5.0
            while len(outcomes) < 3 and not errors and time.monotonic() < deadline:
                application.processEvents()
                QTest.qWait(10)

        assert not errors
        assert len(outcomes) == 3
        spec_gens = [o.generation for o in outcomes if isinstance(o, SpectrumAnalysisOutcome)]
        gram_outcomes = [o for o in outcomes if isinstance(o, SpectrogramAnalysisOutcome)]
        assert spec_gens == [1, 2]
        assert len(gram_outcomes) == 1
        assert gram_outcomes[0].matrix.shape == (0, len(freqs))
        assert not gram_outcomes[0].matrix.flags.writeable

        outcomes.clear()
        controller.submit(same_stamp_gram_req)
        deadline = time.monotonic() + 5.0
        while not outcomes and not errors and time.monotonic() < deadline:
            application.processEvents()
            QTest.qWait(10)
        assert not errors
        assert len(outcomes) == 1
        gram = outcomes[0]
        assert isinstance(gram, SpectrogramAnalysisOutcome)
        assert gram.matrix.shape == (2, len(freqs))
        assert not gram.matrix.flags.writeable
        assert not np.allclose(gram.matrix[0], gram.matrix[1])
    finally:
        assert controller.close(timeout_ms=2000)
