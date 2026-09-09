"""Tests for Keithley characterization models and scientific parameter extraction."""

from __future__ import annotations

import math
import numpy as np
import pytest

from app.devices.keithley_2600.characterization.analyzer import KeithleyCharacterizationAnalyzer
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    CharacterizationPoint,
    CharacterizationSweepConfig,
    SampleMetadata,
)


@pytest.mark.parametrize("current,voltage", [([1e-6], [.001]), ([1e-6, 2e-6], [.001, .002]),
    ([1e-6] * 3, [.001] * 3), ([1e-6, 2e-6, 3e-6], [.001] * 3)])
def test_insufficient_or_degenerate_linearity_is_not_perfect(current, voltage):
    result = KeithleyCharacterizationAnalyzer._calculate_linearity(
        np.array(current), np.array(voltage), np.zeros(len(current), dtype=bool), 1000)
    assert math.isnan(result)


def test_differentials_keep_order_and_split_at_turning_point():
    current = np.array([1, 2, 3, 2, 1], dtype=float) * 1e-6
    voltage = current * 1000
    conductance, resistance = KeithleyCharacterizationAnalyzer._compute_differential_curves(current, voltage)
    assert len(resistance) == 7
    assert math.isnan(resistance[3][0])
    assert [v for v, r in resistance[:3]] == pytest.approx([.001, .002, .003])
    assert [v for v, r in resistance[4:]] == pytest.approx([.003, .002, .001])
    assert all(r == pytest.approx(1000) for v, r in resistance if math.isfinite(v))
    assert all(g == pytest.approx(.001) for v, g in conductance if math.isfinite(v))


def test_differentials_do_not_bridge_invalid_or_duplicate_coordinates():
    current = np.array([1, 2, np.nan, 4, 5]) * 1e-6
    assert KeithleyCharacterizationAnalyzer._compute_differential_curves(current, current * 1000) == ([], [])
    current = np.array([1, 2, 2, 3]) * 1e-6
    assert KeithleyCharacterizationAnalyzer._compute_differential_curves(current, current * 1000) == ([], [])


@pytest.mark.parametrize("defect", ["gap", "turn", "duplicate", "nonfinite"])
def test_bdr_rejects_disconnected_or_multibranch_data(defect):
    voltage = np.linspace(-.1, .1, 21)
    current = (voltage + voltage ** 3) / 1000
    mask = np.zeros(len(voltage), dtype=bool)
    if defect == "gap":
        mask[10] = True
    elif defect == "turn":
        voltage[11:] = voltage[11:][::-1]
        current[11:] = current[11:][::-1]
    elif defect == "duplicate":
        voltage[10] = voltage[9]
    else:
        current[10] = np.nan
    result = KeithleyCharacterizationAnalyzer._fit_bdr_tunnel_model(current, voltage, mask)
    assert result == ((None, None, None), None)


def _build_ohmic_clamped_dataset(
    r_sample: float = 450.0,
    v_comp: float = 0.670,
    i_min: float = -0.010,
    i_max: float = 0.010,
    points_count: int = 101,
    junction_area_um2: float | None = 2.0,
) -> CharacterizationDataset:
    """Simulate a sample with ohmic resistance clamped by compliance voltage."""
    i_demanded = np.linspace(i_min, i_max, points_count)
    points: list[CharacterizationPoint] = []

    for idx, demanded in enumerate(i_demanded):
        unclamped_v = demanded * r_sample
        comp_active = abs(unclamped_v) >= v_comp
        if comp_active:
            v_meas = math.copysign(v_comp, unclamped_v)
            i_meas = v_meas / r_sample
        else:
            v_meas = unclamped_v
            i_meas = demanded

        true_r = (v_meas / i_meas) if abs(i_meas) > 1e-15 else r_sample
        app_r = (v_meas / demanded) if abs(demanded) > 1e-15 else r_sample
        power = abs(v_meas * i_meas)

        points.append(
            CharacterizationPoint(
                index=idx,
                demanded_si=float(demanded),
                measured_voltage_v=float(v_meas),
                measured_current_a=float(i_meas),
                true_resistance_ohm=float(true_r),
                apparent_resistance_ohm=float(app_r),
                power_w=float(power),
                compliance_active=bool(comp_active),
                timestamp_epoch=float(idx * 0.05),
            )
        )

    config = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=i_min,
        stop_level_si=i_max,
        points_count=points_count,
        compliance_si=v_comp,
        metadata=SampleMetadata(
            sample_id="MTJ-Test-01",
            junction_area_um2=junction_area_um2,
            operator="Physicist",
        ),
    )
    checksum = CharacterizationDataset.calculate_checksum(points)
    return CharacterizationDataset(
        config=config,
        points=tuple(points),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256=checksum,
    )


def test_ohmic_clamped_dataset_analysis():
    """Verify standard MTJLAB scenario: 450 Ohm sample clamped at 670 mV."""
    dataset = _build_ohmic_clamped_dataset()
    params = KeithleyCharacterizationAnalyzer.analyze(dataset)

    # 1. Zero-bias resistance R0 should be ~450 Ohm
    assert math.isclose(params.zero_bias_resistance_ohm, 450.0, rel_tol=0.01)
    assert math.isclose(params.zero_bias_conductance_s, 1.0 / 450.0, rel_tol=0.01)

    # 2. RA product: 450 Ohm * 2.0 um^2 = 900 Ohm*um^2
    assert params.ra_product_ohm_um2 is not None
    assert math.isclose(params.ra_product_ohm_um2, 900.0, rel_tol=0.01)

    # 3. Compliance detection: 1.5 mA into 450 Ohm is 675 mV > 670 mV
    assert params.compliance_detected is True
    assert params.clamped_points_fraction > 0.5  # Most points are > 1.5 mA
    assert params.compliance_onset_point is not None
    onset_i, onset_v = params.compliance_onset_point
    assert abs(onset_v) >= 0.669
    assert abs(onset_i) <= (0.670 / 450.0) + 1e-5

    # 4. Linearity R^2 before compliance should be very high (> 0.999)
    assert params.linearity_r2 > 0.999

    # 5. Maximum power: V_comp * (V_comp / R) = 0.670 * (0.670 / 450) ~ 0.998 mW
    expected_pmax = (0.670 ** 2) / 450.0
    assert math.isclose(params.max_power_dissipated_w, expected_pmax, rel_tol=0.02)


def test_bdr_tunnel_barrier_extraction():
    """Verify Brinkman-Dynes-Rowell tunnel barrier height and asymmetry extraction."""
    # Create non-linear tunnel junction data
    # G(V) = G0 * (1 - a1*V + a2*V^2)
    # Using G0 = 1/500, a2 = 0.5, a1 = -0.05
    g0 = 1.0 / 500.0
    c0 = g0
    c1 = -0.05 * g0
    c2 = 0.5 * g0

    v_vals = np.linspace(-0.5, 0.5, 51)
    # Integral of G(V) to get I(V): I = c0*V + (c1/2)*V^2 + (c2/3)*V^3
    i_vals = c0 * v_vals + 0.5 * c1 * (v_vals ** 2) + (1.0 / 3.0) * c2 * (v_vals ** 3)

    points: list[CharacterizationPoint] = []
    for idx, (v, i) in enumerate(zip(v_vals, i_vals)):
        points.append(
            CharacterizationPoint(
                index=idx,
                demanded_si=float(v),
                measured_voltage_v=float(v),
                measured_current_a=float(i),
                true_resistance_ohm=float(v / i) if abs(i) > 1e-12 else 500.0,
                apparent_resistance_ohm=float(v / i) if abs(i) > 1e-12 else 500.0,
                power_w=float(abs(v * i)),
                compliance_active=False,
                timestamp_epoch=float(idx * 0.05),
            )
        )

    config = CharacterizationSweepConfig(
        channel="A",
        mode="voltage",
        start_level_si=-0.5,
        stop_level_si=0.5,
        points_count=len(v_vals),
        compliance_si=0.050,
        metadata=SampleMetadata(
            sample_id="MTJ-Tunnel-01",
            nominal_barrier_thickness_nm=1.0,
            junction_area_um2=1.0,
        ),
    )
    dataset = CharacterizationDataset(
        config=config,
        points=tuple(points),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256="abc",
    )

    params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    assert params.tunnel_barrier_height_ev is not None
    assert 1.0 < params.tunnel_barrier_height_ev < 10.0
    assert params.tunnel_barrier_asymmetry_ev is not None
    assert params.bdr_coefficients is not None


def test_edge_cases_empty_and_single_point():
    """Verify resilience against empty datasets and single-point measurements."""
    config = CharacterizationSweepConfig()
    empty_dataset = CharacterizationDataset(
        config=config,
        points=(),
        started_at_iso="",
        completed_at_iso="",
    )
    params = KeithleyCharacterizationAnalyzer.analyze(empty_dataset)
    assert math.isnan(params.zero_bias_resistance_ohm)
    assert params.compliance_detected is False
    assert params.clamped_points_fraction == 0.0

    single_point = CharacterizationPoint(
        index=0,
        demanded_si=0.001,
        measured_voltage_v=0.45,
        measured_current_a=0.001,
        true_resistance_ohm=450.0,
        apparent_resistance_ohm=450.0,
        power_w=0.00045,
        compliance_active=False,
        timestamp_epoch=0.0,
    )
    dataset_1 = CharacterizationDataset(
        config=config,
        points=(single_point,),
        started_at_iso="",
        completed_at_iso="",
    )
    params_1 = KeithleyCharacterizationAnalyzer.analyze(dataset_1)
    # One finite-bias ratio cannot determine a zero-bias slope and offset.
    assert math.isnan(params_1.zero_bias_resistance_ohm)
    assert math.isnan(params_1.zero_bias_conductance_s)


def test_all_points_in_compliance():
    """Verify handling when all points hit compliance (e.g. open circuit)."""
    points = [
        CharacterizationPoint(
            index=i,
            demanded_si=float(i * 1e-3),
            measured_voltage_v=0.670,
            measured_current_a=1e-7,
            true_resistance_ohm=6.7e6,
            apparent_resistance_ohm=670.0,
            power_w=6.7e-8,
            compliance_active=True,
            timestamp_epoch=float(i),
        )
        for i in range(1, 10)
    ]
    config = CharacterizationSweepConfig(compliance_si=0.670)
    dataset = CharacterizationDataset(
        config=config,
        points=tuple(points),
        started_at_iso="",
        completed_at_iso="",
    )
    params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    assert params.compliance_detected is True
    assert math.isclose(params.clamped_points_fraction, 1.0)


def test_voltage_mode_dataset_analysis():
    """Verify analysis in voltage sweep mode with current compliance clamping."""
    v_dem = np.linspace(-0.5, 0.5, 101)
    r_sample = 100.0
    i_comp = 0.002  # 2 mA limit

    points: list[CharacterizationPoint] = []
    for idx, v in enumerate(v_dem):
        unclamped_i = v / r_sample
        comp_active = abs(unclamped_i) >= i_comp
        if comp_active:
            i_meas = math.copysign(i_comp, unclamped_i)
            v_meas = i_meas * r_sample
        else:
            i_meas = unclamped_i
            v_meas = v

        true_r = (v_meas / i_meas) if abs(i_meas) > 1e-12 else r_sample
        app_r = (v / i_meas) if abs(i_meas) > 1e-12 else r_sample

        points.append(
            CharacterizationPoint(
                index=idx,
                demanded_si=float(v),
                measured_voltage_v=float(v_meas),
                measured_current_a=float(i_meas),
                true_resistance_ohm=float(true_r),
                apparent_resistance_ohm=float(app_r),
                power_w=float(abs(v_meas * i_meas)),
                compliance_active=bool(comp_active),
                timestamp_epoch=float(idx * 0.05),
            )
        )

    config = CharacterizationSweepConfig(
        channel="A",
        mode="voltage",
        start_level_si=-0.5,
        stop_level_si=0.5,
        points_count=101,
        compliance_si=i_comp,
        metadata=SampleMetadata(sample_id="Sample-VMode"),
    )
    dataset = CharacterizationDataset(
        config=config,
        points=tuple(points),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256="abc123",
    )

    params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    assert math.isclose(params.zero_bias_resistance_ohm, 100.0, rel_tol=0.02)
    assert params.compliance_detected is True
    assert params.compliance_onset_point is not None
    assert math.isclose(params.max_current_a, 0.002, rel_tol=0.01)
    assert params.clamped_points_fraction > 0.5


def test_reversed_sweep_direction_and_rectification_ratio():
    """Verify analyzer correctly handles reversed (high to low) sweep directions."""
    # Asymmetric IV: diode-like behaviour with higher current at positive bias
    v_dem = np.linspace(0.5, -0.5, 51)  # Descending order
    points: list[CharacterizationPoint] = []
    for idx, v in enumerate(v_dem):
        # Asymmetric conduction: 2x current for positive bias
        factor = 2.0 if v > 0 else 1.0
        i = (v / 500.0) * factor
        points.append(
            CharacterizationPoint(
                index=idx,
                demanded_si=float(v),
                measured_voltage_v=float(v),
                measured_current_a=float(i),
                true_resistance_ohm=float(v / i) if abs(i) > 1e-12 else 500.0,
                apparent_resistance_ohm=float(v / i) if abs(i) > 1e-12 else 500.0,
                power_w=float(abs(v * i)),
                compliance_active=False,
                timestamp_epoch=float(idx * 0.05),
            )
        )

    config = CharacterizationSweepConfig(
        channel="A",
        mode="voltage",
        start_level_si=0.5,
        stop_level_si=-0.5,
        points_count=len(v_dem),
        compliance_si=0.010,
        metadata=SampleMetadata(sample_id="Asymm-01"),
    )
    dataset = CharacterizationDataset(
        config=config,
        points=tuple(points),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256="test",
    )
    params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    # Rectification ratio should be ~2.0 even when sweep order is reversed
    assert params.rectification_ratio is not None
    assert math.isclose(params.rectification_ratio, 2.0, rel_tol=0.05)


def test_zero_bias_fit_requires_three_distinct_currents():
    analyzer = KeithleyCharacterizationAnalyzer
    for currents in ([1e-6, 2e-6], [1e-6, 1e-6, 1e-6], [1e-6, 2e-6, 2e-6]):
        current = np.asarray(currents)
        voltage = 1000 * current + .001
        result = analyzer._extract_zero_bias_resistance(
            current, voltage, np.zeros(len(current), dtype=bool)
        )
        assert math.isnan(result)
    current = np.asarray([1e-6, 2e-6, 3e-6])
    result = analyzer._extract_zero_bias_resistance(
        current, 1000 * current + .001, np.zeros(3, dtype=bool)
    )
    assert math.isclose(result, 1000, rel_tol=1e-12)
