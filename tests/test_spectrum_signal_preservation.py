"""Scientific transfer checks, independent of visual smoothing."""

import numpy as np
import pytest

pytest.importorskip("scipy", reason="Optional qualification dependencies are required")

from app.spectrum.resonance_metrics import fit_linear_resonance, resonance_area, resonance_values
from tools.qualify_spectrum_signal_preservation import signal_preservation_report


@pytest.mark.parametrize("shape", ["gaussian", "lorentzian"])
@pytest.mark.parametrize("amplitude", [1e-10, -1e-10])
def test_signed_linear_fit_recovers_si_parameters_on_ghz_axis(shape, amplitude):
    frequencies = np.linspace(7e9, 7.001e9, 1001)
    center, width = 7.0005123e9, 16000
    values = resonance_values(frequencies, amplitude, center, width, shape=shape) + 2e-12
    original = values.copy()
    fit = fit_linear_resonance(frequencies, values, initial_center_hz=center - 1000,
                               initial_fwhm_hz=width * 1.1, shape=shape)
    assert fit.amplitude_w == pytest.approx(amplitude, rel=1e-7)
    assert fit.center_hz == pytest.approx(center, abs=1e-3)
    assert fit.fwhm_hz == pytest.approx(width, rel=1e-7)
    assert fit.baseline_w == pytest.approx(2e-12, rel=1e-7)
    expected_area = resonance_area(amplitude, center, width, frequencies[0], frequencies[-1], shape=shape)
    assert fit.finite_window_area_w_hz == pytest.approx(expected_area, rel=1e-7)
    np.testing.assert_array_equal(values, original)


def test_zero_trace_and_nonphysical_arrays_do_not_produce_resonance_parameters():
    frequencies = np.linspace(1e6, 2e6, 101)
    for values in (np.zeros(101), np.full(101, np.nan), np.ones(101, dtype=complex)):
        with pytest.raises(ValueError):
            fit_linear_resonance(frequencies, values, initial_center_hz=1.5e6, initial_fwhm_hz=1e4)


def test_gamma_power_injection_preserves_sign_shape_and_exposes_coherent_limit():
    report = signal_preservation_report(repetitions=4)
    assert report["coverage_95"] is None and report["false_detection_rate"] is None
    assert not report["laboratory_qualified"]
    assert report["samples_per_fwhm"] >= 10
    for name in ("gaussian_positive", "lorentzian_positive", "gaussian_negative", "lorentzian_negative",
                 "overlap_model", "overlap_calibrated_range"):
        scenario = report["scenarios"][name]
        assert scenario["bias_gates_passed"], scenario["methods"]["selected_correction"]
        assert scenario["rejected_signal_frames"] == 0
    overlap = report["scenarios"]["overlap_model"]["methods"]
    assert overlap["selected_correction"]["spectrum_rmse_w"]["mean"] < overlap["static_reference"]["spectrum_rmse_w"]["mean"]
    coherent = report["scenarios"]["coherent_interference"]
    assert not coherent["bias_gates_passed"] and not coherent["additive_power_model_applicable"]
    assert coherent["methods"]["selected_correction"]["amplitude_relative_error"]["mean"] > 1


def test_generator_seed_reproduces_metrics_and_rejects_unbounded_inputs():
    first = signal_preservation_report(repetitions=1, scenarios=("gaussian_negative",))
    assert first == signal_preservation_report(repetitions=1, scenarios=("gaussian_negative",))
    with pytest.raises(ValueError):
        signal_preservation_report(repetitions=0)
    with pytest.raises(ValueError):
        signal_preservation_report(points=1000001)


def test_inadequate_calibration_range_fails_gates_and_counts_every_rejected_frame():
    report = signal_preservation_report(repetitions=2, scenarios=("overlap_insufficient_range",))
    scenario = report["scenarios"]["overlap_insufficient_range"]
    assert scenario["rejected_signal_frames"] == 32
    assert not scenario["bias_gates_passed"]
    selected = scenario["methods"]["selected_correction"]
    assert selected["successful_repetitions"] == 0 and selected["failed_repetitions"] == 2
    assert selected["amplitude_relative_error"] is None and selected["spectrum_rmse_w"] is None


def test_broad_and_weak_resonances_have_explicit_accuracy_scope():
    report = signal_preservation_report(repetitions=3, scenarios=("broad_gaussian", "weak_gaussian"))
    broad, weak = (report["scenarios"][name] for name in ("broad_gaussian", "weak_gaussian"))
    assert broad["true_fwhm_hz"] == 160000
    assert broad["samples_per_fwhm"] == 160
    assert broad["high_snr_bias_gates_applicable"] and broad["bias_gates_passed"]
    assert not weak["high_snr_bias_gates_applicable"] and not weak["bias_gates_passed"]
    assert "low_SNR" in weak["interpretation"]
    assert weak["true_amplitude_w"] == 1e-13
    for scenario in (broad, weak):
        method = scenario["methods"]["selected_correction"]
        assert method["successful_repetitions"] + method["failed_repetitions"] == 3


def test_resonance_present_in_reference_cannot_be_claimed_as_recovered():
    report = signal_preservation_report(repetitions=3,
        scenarios=("reference_contaminated", "shifted_reference_resonance"))
    for scenario in report["scenarios"].values():
        assert "difference_of_states" in scenario["interpretation"]
        assert not scenario["high_snr_bias_gates_applicable"]
        assert not scenario["bias_gates_passed"]
        assert scenario["additive_power_model_applicable"]
        method = scenario["methods"]["selected_correction"]
        assert method["successful_repetitions"] + method["failed_repetitions"] == 3
    same = report["scenarios"]["reference_contaminated"]["methods"]["selected_correction"]
    # If a numerical fit is available, its amplitude is noise rather than the
    # canceled resonance. Fit failures are also legitimate and remain counted.
    if same["successful_repetitions"]:
        assert abs(same["amplitude_relative_error"]["mean"] + 1) < .01
    assert report["coverage_95"] is None and not report["laboratory_qualified"]
