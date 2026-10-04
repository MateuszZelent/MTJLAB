"""Scientific contracts for the shared passive preview pipeline."""

from dataclasses import replace

import numpy as np
import pytest

from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from app.domain.spectrum_interference import SpectrumInterferenceCalibration
from app.spectrum.analysis import SpectrumAnalysisParameters, clean_spectrum_pipeline
from app.spectrum.interference_model import calibrated_interference_model
from app.spectrum.preview_processing import SpectrumPreviewProcessor


def fixture():
    axis = np.linspace(200e6, 1200e6, 1001)
    context = SpectrumAcquisitionContext(axis, "preview-test", settings_verified=True)
    baseline = np.full(len(axis), 1e-9)
    profile = BackgroundProfile("reference", context.context_id, baseline,
                                np.full(len(axis), 9e-24), np.full(len(axis), 1e-24),
                                9, 1., 10., "Low current operating point")
    return axis, context, profile


def dbm(watts):
    return tuple(10 * np.log10(watts) + 30)


def test_averages_power_before_subtraction_and_keeps_shared_reference_uncertainty():
    axis, context, profile = fixture()
    rows = tuple(dbm(np.full(len(axis), power)) for power in (0.4e-9, 1.2e-9, 1.1e-9))
    params = SpectrumAnalysisParameters(temporal_average_frames=3)
    result, stats = SpectrumPreviewProcessor().process(rows[-1], frequencies_hz=axis,
        power_rows=rows, timestamps_s=(20., 21., 22.), parameters=params, modes=("background",),
        background_profile=profile, background_context=context)
    np.testing.assert_allclose(result.values, -1e-10, atol=2e-24)
    assert stats.frame_count == 3 and stats.duration_s == 2
    assert stats.reference_mean_uncertainty_w == pytest.approx(1e-12)
    assert not stats.uncertainty_qualified
    assert result.unit == "W" and result.applied_modes == ("power_average", "background")
    assert "preview-processing-v1" in result.input_provenance
    np.testing.assert_array_equal(profile.mean_w, np.full(len(axis), 1e-9))


def test_averaging_is_linear_not_logarithmic_and_reference_math_is_after_mean():
    axis, _, _ = fixture()
    rows = (np.full(len(axis), -30.), np.full(len(axis), -60.))
    parameters = SpectrumAnalysisParameters(temporal_average_frames=2)
    result, _ = SpectrumPreviewProcessor().process(rows[-1], frequencies_hz=axis,
        parameters=parameters, power_rows=rows, timestamps_s=(1., 2.),
        reference_values_dbm=np.full(len(axis), -60.), reference_operation="ratio_linear")
    np.testing.assert_allclose(result.values, 500.5, rtol=1e-13)
    assert result.unit in {"ratio", "linear ratio"}


def test_gap_and_tail_bound_prevent_mixing_old_operating_points():
    axis, _, _ = fixture()
    rows = tuple(np.full(len(axis), value) for value in (-10., -20., -30., -40.))
    result, stats = SpectrumPreviewProcessor().process(rows[-1], frequencies_hz=axis,
        parameters=SpectrumAnalysisParameters(temporal_average_frames=4, temporal_max_gap_s=5.),
        power_rows=rows, timestamps_s=(1., 2., 30., 31.))
    np.testing.assert_allclose(result.values, 10*np.log10((1e-6+1e-7)/2)+30)
    assert stats.frame_count == 2 and stats.duration_s == 1
    with pytest.raises(ValueError, match="timestamps"):
        SpectrumPreviewProcessor().process(rows[-1], frequencies_hz=axis,
            parameters=SpectrumAnalysisParameters(temporal_average_frames=2),
            power_rows=rows, timestamps_s=(1., 1., 2., 3.))


@pytest.mark.parametrize("modes", [("narrow_reject",), ("denoise",), ("emi_reject",),
                                  ("narrow_reject", "emi_reject", "denoise")])
def test_protected_resonance_is_bit_exact_through_all_display_filters(modes):
    axis, _, _ = fixture()
    rng = np.random.default_rng(981)
    raw = -80 + rng.normal(0, .05, len(axis))
    raw += 40 * np.exp(-4*np.log(2)*((axis-500e6)/3e6)**2)
    region = (axis >= 490e6) & (axis <= 510e6)
    parameters = SpectrumAnalysisParameters(narrow_protected_regions_hz=((490e6, 510e6),))
    result = clean_spectrum_pipeline(raw, unit="dBm", modes=modes, frequencies_hz=axis,
                                    history=tuple(raw for _ in range(8)), parameters=parameters)
    np.testing.assert_array_equal(np.asarray(result.values)[region], raw[region])
    assert not any(region[i] for i in result.removed_peak_indices)
    assert not any(region[i] for i in result.stationary_interference_indices)


def calibration_fixture():
    axis, context, profile = fixture()
    protected = (axis >= 490e6) & (axis <= 510e6)
    basis = np.exp(-((axis-500e6)/50e6)**2)[:, None] * 1e-10
    calibration = SpectrumInterferenceCalibration("drift", context, profile.mean_w, basis,
        ~protected, protected, np.full(len(axis), 1e-12), ((-1., 1.),),
        ((profile.profile_id, profile.content_hash),), True, "Known synthetic support")
    signal = 1e-11 * np.exp(-4*np.log(2)*((axis-500e6)/3e6)**2)
    signal[~protected] = 0
    return axis, context, profile, calibration, signal


@pytest.mark.parametrize("explicit_protection", [False, True])
def test_reference_trained_model_removes_drift_without_erasing_overlapping_signal(explicit_protection):
    axis, context, profile, calibration, signal = calibration_fixture()
    watts = profile.mean_w + calibration.basis_w[:, 0]*.5 + signal
    result, stats = SpectrumPreviewProcessor().process(dbm(watts), frequencies_hz=axis,
        modes=("background", "narrow_reject", "denoise"), background_profile=profile,
        background_context=context, interference_calibration=calibration,
        parameters=SpectrumAnalysisParameters(narrow_protected_regions_hz=((490e6, 510e6),) if explicit_protection else ()))
    region = calibration.protected_mask
    np.testing.assert_allclose(np.asarray(result.values)[region], signal[region], atol=4e-24)
    assert stats.model_id == "drift" and not stats.uncertainty_qualified
    assert calibration.content_hash in result.input_provenance


@pytest.mark.parametrize("failure", ["qualification", "identity", "range", "protection"])
def test_invalid_drift_models_fail_explicitly_instead_of_using_raw_or_static_background(failure):
    axis, context, profile, calibration, signal = calibration_fixture()
    coefficient = 2. if failure == "range" else .5
    if failure == "qualification":
        calibration = replace(calibration, signal_control_regions_qualified=False)
    if failure == "identity":
        calibration = replace(calibration, source_profiles=(("wrong", "0"*64),))
    parameters = SpectrumAnalysisParameters(narrow_protected_regions_hz=((200e6, 250e6),) if failure == "protection" else ())
    with pytest.raises(ValueError):
        SpectrumPreviewProcessor().process(dbm(profile.mean_w + calibration.basis_w[:, 0]*coefficient + signal),
            frequencies_hz=axis, modes=("background",), parameters=parameters,
            background_profile=profile, background_context=context, interference_calibration=calibration)


def test_pipeline_rejects_a_model_with_another_baseline():
    axis, context, profile, calibration, _ = calibration_fixture()
    model = calibrated_interference_model(replace(calibration, baseline_w=profile.mean_w*2))
    with pytest.raises(ValueError, match="baseline"):
        clean_spectrum_pipeline(dbm(profile.mean_w), frequencies_hz=axis, unit="dBm", modes=("background",),
            background_profile=profile, background_context=context, background_model=model)


def test_native_conversion_cache_has_a_hard_memory_bound():
    processor = SpectrumPreviewProcessor()
    processor.CACHE_BYTES = 8 * 1001 * 6
    axis, _, _ = fixture()
    parameters = SpectrumAnalysisParameters(temporal_average_frames=2)
    for index in range(20):
        rows = tuple(np.frombuffer(np.full(len(axis), -80+index+j, dtype=float).tobytes()) for j in range(2))
        processor.process(rows[-1], frequencies_hz=axis, parameters=parameters,
                          power_rows=rows, timestamps_s=(float(index), float(index+1)))
        assert 0 < processor._cache_bytes <= processor.CACHE_BYTES


def test_mutable_public_inputs_cannot_reuse_stale_cached_power():
    processor = SpectrumPreviewProcessor()
    axis, _, _ = fixture()
    row = np.full(len(axis), -80.)
    parameters = SpectrumAnalysisParameters(temporal_average_frames=2)
    processor.process(row, frequencies_hz=axis, parameters=parameters, power_rows=(row, row))
    row[:] = -50.
    result, _ = processor.process(row, frequencies_hz=axis, parameters=parameters, power_rows=(row, row))
    np.testing.assert_allclose(result.values, -50., atol=1e-12)


def test_known_signal_transfer_measures_amplitude_width_area_and_exposes_unprotected_peak_loss():
    from tools.qualify_spectrum_preview import preview_transfer_report

    report = preview_transfer_report(repetitions=3)
    assert not report["laboratory_qualified"] and not report["confidence_interval_qualified"]
    for scenario in report["scenarios"].values():
        assert scenario["protected_signal_bias_gates_passed"], scenario
        methods = scenario["methods"]
        assert methods["average_minus_background"]["noise_rms_w"] < methods["single_minus_background"]["noise_rms_w"]*.6
    destructive = report["scenarios"]["narrow_gaussian"]["methods"]["unprotected_display_filters"]
    assert destructive["fit_failures"] or abs(destructive["amplitude_relative_error"]) > .5
