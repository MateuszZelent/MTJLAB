"""Known slopes/offsets and missing-data boundaries for field comparisons."""

from dataclasses import replace

import pytest

from app.devices.keithley_2600.characterization.field_analysis import analyze_field_series
from app.devices.keithley_2600.characterization.field_reader import load_field_series
from tests.test_keithley_field_worker import make_worker


def series_fixture(tmp_path):
    worker, _ = make_worker(tmp_path)
    worker.run()
    series = load_field_series(worker.directory)
    curves = []
    for curve in series.curves:
        if curve.dataset is not None:
            resistance = 1000 if curve.index == 0 else 2000
            points = tuple(replace(p, demanded_si=p.measured_current_a * 1.2,
                                   measured_voltage_v=resistance * p.measured_current_a + 0.01)
                           for p in curve.dataset.points)
            curve = replace(curve, dataset=replace(curve.dataset, points=points))
        curves.append(curve)
    return replace(series, curves=tuple(curves))


def test_measured_current_fit_has_intercept_and_explicit_reference(tmp_path):
    result = analyze_field_series(series_fixture(tmp_path), (1e-6, 3e-6), reference_index=0)
    assert result.fits[0].resistance_ohm == pytest.approx(1000)
    assert result.fits[0].voltage_offset_v == pytest.approx(.01)
    assert result.fits[2].resistance_ohm == pytest.approx(2000)
    assert result.fits[2].relative_resistance_percent == pytest.approx(100)
    assert result.fits[1].resistance_ohm is None
    assert result.fits[0].history_segment != result.fits[2].history_segment
    assert result.fits[0].point_indices == (0, 1, 2)
    assert result.fits[0].bias_coverage == "positive_only"
    assert result.fits[0].residuals_v == pytest.approx((0, 0, 0), abs=1e-12)


@pytest.mark.parametrize("area", [None, 0, .02])
def test_apparent_ra_requires_positive_known_area(tmp_path, area):
    series = series_fixture(tmp_path)
    curve = series.curves[0]
    config = replace(curve.dataset.config, metadata=replace(curve.dataset.config.metadata, junction_area_um2=area))
    curve = replace(curve, dataset=replace(curve.dataset, config=config))
    series = replace(series, curves=(curve, *series.curves[1:]))
    fit = analyze_field_series(series, (1e-6, 3e-6)).fits[0]
    if area:
        assert fit.apparent_ra_ohm_um2 == pytest.approx(20)
    else:
        assert fit.apparent_ra_ohm_um2 is None


def test_invalid_reference_never_substitutes_another_curve(tmp_path):
    result = analyze_field_series(series_fixture(tmp_path), (1e-6, 3e-6), reference_index=1)
    assert result.reference_error
    assert all(fit.relative_resistance_percent is None for fit in result.fits)
    assert result.fits[0].resistance_ohm is not None


@pytest.mark.parametrize("fault", ["compliance", "invalid", "branch", "truncated"])
def test_bad_fit_window_is_not_silently_repaired(tmp_path, fault):
    series = series_fixture(tmp_path)
    first = series.curves[0]
    points = list(first.dataset.points)
    if fault == "compliance":
        points[1] = replace(points[1], compliance_active=True)
    elif fault == "invalid":
        points[1] = replace(points[1], valid=False)
    elif fault == "branch":
        points[2] = replace(points[2], demanded_si=points[0].demanded_si)
    else:
        points.pop()
    first = replace(first, dataset=replace(first.dataset, points=tuple(points)))
    result = analyze_field_series(replace(series, curves=(first, *series.curves[1:])), (1e-6, 3e-6))
    assert result.fits[0].resistance_ohm is None
    assert result.fits[0].reason
    assert result.fits[2].resistance_ohm == pytest.approx(2000)


def test_no_extrapolation_to_unmeasured_window(tmp_path):
    result = analyze_field_series(series_fixture(tmp_path), (0, 4e-6))
    assert all(fit.resistance_ohm is None for fit in result.fits)
