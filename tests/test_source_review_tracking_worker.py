"""Peak tracking computation is asynchronous and session-qualified."""
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QThread

from app.devices.anritsu_ms2830a.ui import analysis_worker
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_tracking_detector_runs_in_worker_even_with_auto_peaks_off(shell_qt_application, monkeypatch):
    peak = SimpleNamespace(frequency_hz=3.)
    threads, results = [], []

    def detect(*args, **kwargs):
        threads.append(QThread.currentThread() == shell_qt_application.thread())
        assert kwargs["fit"] is False
        return (peak,)

    monkeypatch.setattr(analysis_worker, "detect_spectrum_peaks", detect)
    controller = analysis_worker.SpectrumAnalysisController()
    controller.result.connect(results.append)
    try:
        controller.submit(analysis_worker.SpectrumAnalysisRequest(
            1, (1., 2., 3., 4., 5.), (-80., -70., -30., -70., -80.),
            "raw", (), False, tracking_context=(4, 3., 1.)))
        wait_until(shell_qt_application, lambda: bool(results))
        assert threads == [False]
        assert results[0].peaks is None
        assert results[0].tracked_peak is peak
        assert results[0].tracking_context == (4, 3., 1.)
    finally:
        assert controller.close(timeout_ms=2000)


def test_tracking_ignores_old_session_and_uses_result_frame_identity():
    tracking = Mock()
    context = (2, 3., 1.)
    page = SimpleNamespace(_tracked_peak_target_hz=3., _tracked_peak_gate_hz=1.,
        _peak_tracking_window=tracking, _applied_analysis_generation=10,
        _tracked_peak_generation=8, _tracked_peak_revision=5,
        _tracking_context=lambda: context, _tracking_started_monotonic=1.,
        _peak_measurement_method=lambda: "Raw", _display_revision=999)
    result = SimpleNamespace(tracking_context=(1, 3., 1.), frame_id=6,
                             tracked_peak=SimpleNamespace(frequency_hz=3.1))
    AnritsuPage._update_peak_tracking(page, 2., result)
    tracking.append.assert_not_called()
    result.tracking_context = context
    AnritsuPage._update_peak_tracking(page, 2., result)
    tracking.append.assert_called_once()
    assert page._tracked_peak_revision == 6
    assert page._tracked_peak_target_hz == 3.1
