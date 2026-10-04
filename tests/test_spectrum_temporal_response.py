"""Closed-form step transfer, signed noisy recovery and cadence-aware delays."""

import json
import subprocess
import sys

import numpy as np

from app.domain.spectrum_correction import TemporalAverageMode
from tools.qualify_spectrum_temporal_response import expected_step_response, first_crossing_delay, temporal_response_report


def test_closed_form_responses_have_distinct_temporal_meaning():
    times = np.arange(6, dtype=float)
    np.testing.assert_allclose(expected_step_response(times, 2, TemporalAverageMode.BLOCK,
        tau_s=1, window_frames=3), [0, 0, 1 / 3, 1 / 2, 3 / 5, 2 / 3])
    np.testing.assert_allclose(expected_step_response(times, 2, TemporalAverageMode.WINDOW,
        tau_s=1, window_frames=3), [0, 0, 1 / 3, 2 / 3, 1, 1])
    ema = expected_step_response(times, 2, TemporalAverageMode.EMA_PREVIEW, tau_s=1, window_frames=3)
    np.testing.assert_allclose(ema, [0, 0, 1 - np.exp(-1), 1 - np.exp(-2), 1 - np.exp(-3), 1 - np.exp(-4)])
    assert first_crossing_delay(times, ema, 2, level=.5) == 1
    assert first_crossing_delay(times, np.zeros(6), 2) is None


def test_actual_core_noiseless_response_matches_closed_form_for_both_signs_and_jitter():
    report = temporal_response_report(repetitions=1, noise=False)
    for scenario in report["scenarios"].values():
        assert scenario["rejected_frames"] == 0
        for method in scenario["summary"].values():
            assert method["successful_trials"] == 1 and method["failed_trials"] == 0
            assert method["maximum_fraction_error"] < 1e-10
        for curve in scenario["example"]["curves"].values():
            np.testing.assert_allclose(curve["observed_fraction"], curve["expected_fraction"], atol=1e-10)
    assert report["scenarios"]["negative_regular"]["step_amplitude_w"] < 0
    assert not report["laboratory_qualified"]
    json.dumps(report, allow_nan=False)


def test_noise_is_reproducible_and_response_error_remains_visible():
    report = temporal_response_report(repetitions=2)
    assert report == temporal_response_report(repetitions=2)
    for scenario in report["scenarios"].values():
        for method in scenario["summary"].values():
            assert 0 < method["mean_rms_fraction_error"] < .001
            assert method["failed_trials"] == 0
    assert report["scenarios"]["positive_irregular"]["irregular_cadence"]


def test_rejected_frame_is_not_silently_removed_from_temporal_trials(monkeypatch):
    from app.spectrum.realtime_processor import RealtimeSpectrumProcessor

    original = RealtimeSpectrumProcessor.ingest

    def reject_one(self, envelope, values):
        return False if envelope.frame_id == 50 else original(self, envelope, values)

    monkeypatch.setattr(RealtimeSpectrumProcessor, "ingest", reject_one)
    report = temporal_response_report(repetitions=1)
    for scenario in report["scenarios"].values():
        assert scenario["rejected_frames"] == 3
        for method in scenario["summary"].values():
            assert method["successful_trials"] == 0 and method["failed_trials"] == 1
            assert method["mean_rms_fraction_error"] is None


def test_cli_publishes_new_report_and_refuses_overwrite(tmp_path):
    output = tmp_path / "response.json"
    command = [sys.executable, "-m", "tools.qualify_spectrum_temporal_response", "--output", str(output),
               "--repetitions", "1", "--no-noise"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    before = output.read_bytes()
    assert json.loads(before)["noise_enabled"] is False
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert output.read_bytes() == before
