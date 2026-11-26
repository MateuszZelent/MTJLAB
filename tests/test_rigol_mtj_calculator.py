"""Unit tests for the Rigol DG1032Z -> MTJ load calculator physics core."""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from app.devices.rigol_dg1000z.mtj_calculator import (
    AmbiguousBranchError,
    CalculatorRangeError,
    ConstantResistanceModel,
    ReferenceCurvePass,
    ReferencePoint,
    WaveformRequest,
    compute_resistance_stats,
    compute_rigol_voltage_limit_for_mtj,
    curve_transfer_series,
    export_conversion_csv,
    parse_rigol_load,
    solve_dc_point,
    solve_waveform,
    split_curve_passes,
)
from tests.helpers import simulation_settings


def _keithley_a_sample_pass() -> ReferenceCurvePass:
    """Build a small realistic MTJ pass matching artifacts/sweep-r19c7-02/keithley-a.csv."""
    points = (
        ReferencePoint(0, -5.10559e-3, -554.04e-3, 108.51635168511376, True),
        ReferencePoint(1, -2.04669e-3, -230.014e-3, 112.38340930966585, True),
        ReferencePoint(2, -0.00529289e-3, -0.557363e-3, 105.30409662774022, True),
        ReferencePoint(3, 2.03327e-3, 241.25e-3, 118.65123667786372, True),
        ReferencePoint(4, 5.09481e-3, 589.21e-3, 115.64906247730534, True),
    )
    return ReferenceCurvePass(
        pass_index=1,
        label="Pass 1",
        points=points,
        resistance_stats=compute_resistance_stats(points),
    )


def test_parse_rigol_load_conventions() -> None:
    highz = parse_rigol_load("HIGHZ")
    assert highz.is_high_z is True
    assert highz.open_circuit_scale == pytest.approx(1.0)

    load50 = parse_rigol_load("50 ohm")
    assert load50.is_high_z is False
    assert load50.load_ohm == pytest.approx(50.0)
    assert load50.open_circuit_scale == pytest.approx(2.0)

    load100 = parse_rigol_load(100.0)
    assert load100.load_ohm == pytest.approx(100.0)
    assert load100.open_circuit_scale == pytest.approx(1.5)

    with pytest.raises(ValueError):
        parse_rigol_load("0 ohm")
    with pytest.raises(ValueError):
        parse_rigol_load("-50 ohm")
    with pytest.raises(ValueError):
        parse_rigol_load("50 mA")


def test_constant_resistance_highz_and_load50_physics() -> None:
    # R = 114 ohm with 108..120 ohm band
    model = ConstantResistanceModel(114.0, 108.0, 120.0)

    # HighZ: V_oc = V_set = 100 mV -> V_MTJ = 100 * 114 / 164 = 69.512195 mV
    res_hz = solve_dc_point(model, input_key="setting", target_si=0.1, output_load="HIGHZ")
    assert res_hz.point.open_circuit_v == pytest.approx(0.1)
    assert res_hz.point.mtj_voltage_v == pytest.approx(0.1 * 114.0 / 164.0)
    assert res_hz.point.mtj_current_a == pytest.approx(0.1 / 164.0)
    assert res_hz.point.voltage_transfer_ratio == pytest.approx(114.0 / 164.0)
    assert res_hz.point.reflection_gamma == pytest.approx((114.0 - 50.0) / 164.0)
    assert res_hz.band is not None
    assert res_hz.band.mtj_voltage_min_v == pytest.approx(0.1 * 108.0 / 158.0)
    assert res_hz.band.mtj_voltage_max_v == pytest.approx(0.1 * 120.0 / 170.0)
    assert res_hz.band.mtj_current_min_a == pytest.approx(0.1 / 170.0)
    assert res_hz.band.mtj_current_max_a == pytest.approx(0.1 / 158.0)

    # LOAD 50 ohm: V_oc = 2 * V_set = 200 mV -> V_MTJ = 100 * (1 + Gamma) = 139.02439 mV > V_set!
    res_50 = solve_dc_point(model, input_key="setting", target_si=0.1, output_load=50.0)
    assert res_50.point.open_circuit_v == pytest.approx(0.2)
    assert res_50.point.mtj_voltage_v == pytest.approx(0.2 * 114.0 / 164.0)
    assert res_50.point.voltage_transfer_ratio == pytest.approx(2.0 * 114.0 / 164.0)
    assert res_50.point.mtj_voltage_v > 0.1

    # Round-trip from target MTJ voltage and target MTJ current
    from_v = solve_dc_point(
        model, input_key="voltage", target_si=res_50.point.mtj_voltage_v, output_load=50.0
    )
    assert from_v.point.rigol_setpoint_v == pytest.approx(0.1)
    from_i = solve_dc_point(
        model, input_key="current", target_si=res_hz.point.mtj_current_a, output_load="HIGHZ"
    )
    assert from_i.point.rigol_setpoint_v == pytest.approx(0.1)


def test_measured_curve_exact_points_and_bidirectional_roundtrip() -> None:
    curve_pass = _keithley_a_sample_pass()
    # Endpoint: I = 5.09481 mA, V = 589.21 mV -> V_oc = 589.21 + 50*5.09481 = 843.9505 mV
    expected_oc = 589.21e-3 + 50.0 * 5.09481e-3

    hz = solve_dc_point(curve_pass, input_key="current", target_si=5.09481e-3, output_load="HIGHZ")
    assert hz.point.rigol_setpoint_v == pytest.approx(expected_oc, rel=1e-9)
    assert hz.point.mtj_voltage_v == pytest.approx(589.21e-3, rel=1e-9)

    l50 = solve_dc_point(curve_pass, input_key="voltage", target_si=589.21e-3, output_load=50.0)
    assert l50.point.rigol_setpoint_v == pytest.approx(expected_oc / 2.0, rel=1e-9)
    assert l50.point.mtj_current_a == pytest.approx(5.09481e-3, rel=1e-9)

    # Interpolated interior point round-trip
    mid = solve_dc_point(curve_pass, input_key="setting", target_si=0.4, output_load="HIGHZ")
    back = solve_dc_point(
        curve_pass, input_key="voltage", target_si=mid.point.mtj_voltage_v, output_load="HIGHZ"
    )
    assert back.point.rigol_setpoint_v == pytest.approx(0.4, rel=1e-9)
    assert back.point.mtj_current_a == pytest.approx(mid.point.mtj_current_a, rel=1e-9)

    # Near-zero resistance artifact (105.3 ohm at -5.3 uA) must NOT distort R_min stats
    assert curve_pass.resistance_stats is not None
    assert curve_pass.resistance_stats.min_ohm == pytest.approx(108.51635168511376, rel=1e-6)
    assert curve_pass.resistance_stats.max_ohm == pytest.approx(118.65123667786372, rel=1e-6)


def test_measured_curve_rejects_extrapolation_fail_closed() -> None:
    curve_pass = _keithley_a_sample_pass()
    with pytest.raises(CalculatorRangeError) as exc_info:
        solve_dc_point(curve_pass, input_key="setting", target_si=2.0, output_load="HIGHZ")
    assert exc_info.value.max_value_si < 1.0


def test_non_monotonic_switching_curve_flags_ambiguous_branches() -> None:
    # Abrupt resistance drop causes V_oc to fold back over a narrow voltage window
    pts = (
        ReferencePoint(0, 0.0, 0.0),
        ReferencePoint(1, 2e-3, 400e-3),  # V_oc = 500 mV (AP high-R state)
        ReferencePoint(2, 3e-3, 300e-3),  # V_oc = 450 mV (switched to P low-R state)
        ReferencePoint(3, 5e-3, 500e-3),  # V_oc = 750 mV
    )
    curve_pass = ReferenceCurvePass(1, "Pass 1", pts, compute_resistance_stats(pts))
    with pytest.raises(AmbiguousBranchError) as exc_info:
        solve_dc_point(curve_pass, input_key="setting", target_si=0.475, output_load="HIGHZ")
    assert len(exc_info.value.candidates) >= 2


def test_split_curve_passes_separates_hysteretic_forward_and_reverse_sweeps() -> None:
    # Forward -2..+2 mA then reverse +2..-2 mA
    currents = (-2e-3, -1e-3, 0.0, 1e-3, 2e-3, 1e-3, 0.0, -1e-3, -2e-3)
    pts = tuple(ReferencePoint(i, c, c * 115.0) for i, c in enumerate(currents))
    passes = split_curve_passes(pts)
    assert len(passes) == 2
    assert len(passes[0].points) == 5
    assert len(passes[1].points) == 5
    assert passes[0].points[-1].current_a == pytest.approx(2e-3)
    assert passes[1].points[0].current_a == pytest.approx(2e-3)
    assert passes[1].points[-1].current_a == pytest.approx(-2e-3)


def test_waveform_sine_and_square_and_pulse_matches_analytical_formulas() -> None:
    model = ConstantResistanceModel(150.0, 100.0, 200.0)
    # SIN: Vpp = 200 mV, Offset = 100 mV -> High = 200 mV, Low = 0 mV, HighZ
    req_sin = WaveformRequest(
        waveform="SIN",
        high_v=0.2,
        low_v=0.0,
        output_load="HIGHZ",
        frequency_hz=1_000.0,
    )
    res_sin = solve_waveform(model, req_sin)
    # R = 150, R_s = 50 -> I_DC = 0.1/200 = 0.5 mA, I_ac_pk = 0.1/200 = 0.5 mA
    expected_i_rms = math.sqrt(0.0005**2 + (0.0005**2) / 2.0)
    assert res_sin.nominal.mtj_voltage_min_v == pytest.approx(0.0, abs=1e-9)
    assert res_sin.nominal.mtj_voltage_max_v == pytest.approx(0.15, rel=1e-6)
    assert res_sin.nominal.mtj_current_rms_a == pytest.approx(expected_i_rms, rel=1e-3)
    assert res_sin.nominal.mtj_power_avg_w == pytest.approx(expected_i_rms**2 * 150.0, rel=1e-3)
    assert res_sin.resistance_range is not None
    assert res_sin.resistance_range.mtj_current_max_a == pytest.approx(0.2 / 150.0, rel=1e-6)

    # SQU with 25% duty cycle: High = 200 mV, Low = 0 mV, LOAD 50 ohm -> V_oc = 400 mV during 25%
    req_squ = WaveformRequest(
        waveform="SQU",
        high_v=0.2,
        low_v=0.0,
        output_load=50.0,
        frequency_hz=10_000.0,
        duty_percent=25.0,
    )
    res_squ = solve_waveform(model, req_squ)
    i_high = 0.4 / 200.0  # 2 mA
    assert res_squ.nominal.mtj_current_max_a == pytest.approx(i_high, rel=1e-6)
    assert res_squ.nominal.mtj_current_mean_a == pytest.approx(0.25 * i_high, rel=2e-2)
    assert res_squ.nominal.mtj_current_rms_a == pytest.approx(math.sqrt(0.25) * i_high, rel=2e-2)

    # PULS with 100 us width at 1 kHz (10% duty)
    req_puls = WaveformRequest(
        waveform="PULS",
        high_v=0.2,
        low_v=0.0,
        output_load="HIGHZ",
        frequency_hz=1_000.0,
        pulse_width_s=100e-6,
    )
    res_puls = solve_waveform(model, req_puls)
    assert res_puls.nominal.mtj_current_mean_a == pytest.approx(0.1 * (0.2 / 200.0), rel=2e-2)


def test_waveform_on_nonlinear_curve_and_hardware_advisory_flags() -> None:
    curve_pass = _keithley_a_sample_pass()
    settings = simulation_settings()

    req = WaveformRequest(
        waveform="SIN",
        high_v=0.5,
        low_v=-0.5,
        output_load="HIGHZ",
        frequency_hz=1_000.0,
    )
    res = solve_waveform(curve_pass, req, settings=settings, channel=1)
    assert res.nominal.mtj_voltage_min_v < 0.0
    assert res.nominal.mtj_voltage_max_v > 0.0
    assert res.nominal.mtj_current_rms_a > 0.0
    assert res.exceeds_hardware_oc_limit is False

    # Exceeding measured curve range fails closed
    req_oob = WaveformRequest(
        waveform="SIN", high_v=2.0, low_v=-2.0, output_load="HIGHZ", frequency_hz=1_000.0
    )
    with pytest.raises(CalculatorRangeError):
        solve_waveform(curve_pass, req_oob)

    # Exceeding DG1032Z 10 V open-circuit ceiling sets hardware advisory flag on resistor model
    res_over = solve_dc_point(
        ConstantResistanceModel(115.0),
        input_key="setting",
        target_si=6.0,  # 6 V in LOAD 50 ohm => 12 V open-circuit > 10 V ceiling
        output_load=50.0,
        settings=settings,
        channel=1,
    )
    assert res_over.point.exceeds_hardware_oc_limit is True
    assert res_over.advisory_safety_message is not None


def test_transfer_series_and_csv_export(tmp_path: Path) -> None:
    model = ConstantResistanceModel(114.0, 108.0, 120.0)
    series = curve_transfer_series(model, "HIGHZ", setting_range_v=(-0.5, 0.5), sample_count=21)
    assert len(series.rigol_setpoint_v) == 21
    assert series.band_low_voltage_v is not None
    assert series.band_high_voltage_v is not None

    out_csv = tmp_path / "conversion.csv"
    exported = export_conversion_csv(out_csv, model, "HIGHZ", setting_range_v=(-0.5, 0.5))
    assert exported == out_csv
    lines = out_csv.read_text(encoding="utf-8").splitlines()
    assert any("50" in line and "advisory" in line.lower() for line in lines if line.startswith("#"))
    data_lines = [line for line in lines if not line.startswith("#")]
    rows = list(csv.DictReader(data_lines))
    assert len(rows) > 10
    assert {"rigol_setpoint_v", "open_circuit_v", "mtj_voltage_v", "mtj_current_a"}.issubset(
        rows[0].keys()
    )


def test_narrow_pulse_100ns_at_1khz_exact_quadrature_and_inverted_polarity() -> None:
    model = ConstantResistanceModel(150.0)
    # 100 ns FWHM pulse at 1 kHz (duty = 1e-4) with 10 ns 10-90% edges -> exact analytical integral!
    req = WaveformRequest(
        waveform="PULS",
        high_v=0.2,
        low_v=0.0,
        output_load="HIGHZ",
        frequency_hz=1_000.0,
        pulse_width_s=100e-9,
        pulse_leading_s=10e-9,
        pulse_trailing_s=10e-9,
    )
    res = solve_waveform(model, req)
    i_high = 0.2 / 200.0  # 1 mA
    # Area of symmetric trapezoid of FWHM 100 ns equals 100 ns * height exactly:
    assert res.nominal.mtj_current_mean_a == pytest.approx(1e-4 * i_high, rel=1e-4)
    assert res.nominal.mtj_current_peak_a == pytest.approx(i_high, rel=1e-9)
    assert res.nominal.mtj_current_rms_a == pytest.approx(math.sqrt(1e-4) * i_high, rel=5e-2)

    # Inverted 25% square wave reflects around offset -> 75% at high_v
    req_inv = WaveformRequest(
        waveform="SQU",
        high_v=0.2,
        low_v=0.0,
        output_load="HIGHZ",
        frequency_hz=1_000.0,
        duty_percent=25.0,
        inverted=True,
    )
    res_inv = solve_waveform(model, req_inv)
    assert res_inv.nominal.mtj_current_mean_a == pytest.approx(0.75 * i_high, rel=1e-6)
    assert any("INV" in w for w in res_inv.warnings)


def test_compute_rigol_voltage_limit_for_mtj_constant_resistance_conservative_at_rmax() -> None:
    model = ConstantResistanceModel(114.0, 108.0, 120.0)

    # ±670 mV on MTJ with R in [108, 120] ohm:
    # Conservative bound MUST solve at R_max = 120 ohm so V_MTJ never exceeds 670 mV.
    # V_oc = 0.67 * (120 + 50) / 120 = 0.9491666... V -> floored downward to 0.9491 V
    lim_hz = compute_rigol_voltage_limit_for_mtj(
        model, mtj_min_v=-0.67, mtj_max_v=0.67, output_load="HIGHZ"
    )
    assert lim_hz.governing_resistance_ohm == pytest.approx(120.0)
    assert lim_hz.clamped_to_measured_range is False
    assert lim_hz.clamped_to_hardware_limit is False
    assert lim_hz.rigol_max_v == pytest.approx(0.67 * 170.0 / 120.0, rel=1e-9)
    assert lim_hz.rigol_min_v == pytest.approx(-0.67 * 170.0 / 120.0, rel=1e-9)
    assert lim_hz.rigol_symmetric_limit_v == pytest.approx(0.9491, abs=1e-9)
    # Verify worst-case V_MTJ across the entire [108, 120] ohm band stays <= 670 mV:
    for r_ohm in (108.0, 114.0, 120.0):
        v_mtj = lim_hz.rigol_symmetric_limit_v * r_ohm / (r_ohm + 50.0)
        assert v_mtj <= 0.67 + 1e-12
    # Worst-case current occurs at R_min = 108 ohm:
    assert lim_hz.estimated_current_max_a == pytest.approx(0.9491 / 158.0, rel=1e-6)

    # LOAD 50 ohm halves displayed Rigol voltage: 0.9491666 / 2 = 0.4745833 -> floored to 0.4745 V
    lim_50 = compute_rigol_voltage_limit_for_mtj(
        model, mtj_min_v=-0.67, mtj_max_v=0.67, output_load=50.0
    )
    assert lim_50.rigol_symmetric_limit_v == pytest.approx(0.4745, abs=1e-9)
    assert lim_50.open_circuit_limit_v == pytest.approx(0.9490, abs=1e-9)

    # Hardware ceiling clamping: 9 V MTJ on 114 ohm would require > 10 V V_oc -> clamped to 10 V / k
    lim_hw = compute_rigol_voltage_limit_for_mtj(
        model, mtj_min_v=-9.0, mtj_max_v=9.0, output_load=50.0
    )
    assert lim_hw.clamped_to_hardware_limit is True
    assert lim_hw.rigol_symmetric_limit_v == pytest.approx(5.0, abs=1e-9)
    assert lim_hw.open_circuit_limit_v == pytest.approx(10.0, abs=1e-9)


def test_compute_rigol_voltage_limit_for_mtj_measured_curve_in_range_and_no_extrapolation() -> None:
    curve_pass = _keithley_a_sample_pass()

    # In-range request: ±500 mV lies strictly inside [-554.04 mV, +589.21 mV]
    in_range = compute_rigol_voltage_limit_for_mtj(
        curve_pass, mtj_min_v=-0.50, mtj_max_v=0.50, output_load="HIGHZ"
    )
    assert in_range.clamped_to_measured_range is False
    assert in_range.effective_mtj_min_v == pytest.approx(-0.50)
    assert in_range.effective_mtj_max_v == pytest.approx(0.50)
    assert in_range.rigol_min_v < -0.65
    assert in_range.rigol_max_v > 0.65
    assert in_range.rigol_symmetric_limit_v <= min(abs(in_range.rigol_min_v), abs(in_range.rigol_max_v)) + 1e-12

    # Out-of-range request: ±670 mV exceeds measured curve -> clamps to verified range, NO extrapolation!
    clamped = compute_rigol_voltage_limit_for_mtj(
        curve_pass, mtj_min_v=-0.67, mtj_max_v=0.67, output_load="HIGHZ"
    )
    assert clamped.clamped_to_measured_range is True
    assert clamped.effective_mtj_min_v == pytest.approx(-0.55404, rel=1e-5)
    assert clamped.effective_mtj_max_v == pytest.approx(0.58921, rel=1e-5)
    expected_min_set = -0.55404 - 50.0 * 5.10559e-3  # -0.8093195 V
    expected_max_set = 0.58921 + 50.0 * 5.09481e-3  # +0.8439505 V
    assert clamped.rigol_min_v == pytest.approx(expected_min_set, rel=1e-6)
    assert clamped.rigol_max_v == pytest.approx(expected_max_set, rel=1e-6)
    # Conservative symmetric limit uses the tighter of |rigol_min_v| and |rigol_max_v|, floored downward
    assert clamped.rigol_symmetric_limit_v == pytest.approx(0.8093, abs=1e-9)
    assert any("extrapolation" in w.lower() for w in clamped.warnings)

    # Reject invalid bounds (min >= max, non-finite, or not bracketing 0 V)
    with pytest.raises(ValueError):
        compute_rigol_voltage_limit_for_mtj(curve_pass, mtj_min_v=0.5, mtj_max_v=-0.5, output_load="HIGHZ")
    with pytest.raises(ValueError):
        compute_rigol_voltage_limit_for_mtj(curve_pass, mtj_min_v=0.1, mtj_max_v=0.5, output_load="HIGHZ")
    with pytest.raises(ValueError):
        compute_rigol_voltage_limit_for_mtj(curve_pass, mtj_min_v=math.nan, mtj_max_v=0.5, output_load="HIGHZ")
