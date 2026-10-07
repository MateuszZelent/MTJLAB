"""Scientific summaries use series dimensions, never guessed Oe/ohm labels."""

import math

import pytest

from app.inventory.analysis import calculate_mtj_metrics
from app.inventory.models import SampleRunRecord
from app.ui.inventory.measurement_card import MeasurementAnalyticsCard
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


X = (-3, -2, -1, 0, 1, 2, 3, 2, 1, 0, -1, -2, -3)
R = (1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 1, 1)


@pytest.mark.parametrize("unit", ["T", "mT", "Oe", "A/m"])
def test_loop_preserves_field_unit_and_normalizes_resistance(unit):
    metrics = calculate_mtj_metrics(X, R, x_unit=unit, y_unit="kohm", dimension_label="200 nm")
    assert metrics.curve_type == "mr_loop"
    assert metrics.r_p == 1000
    assert metrics.r_ap == 2000
    assert metrics.tmr_percent == 100
    assert metrics.ra_product == pytest.approx(1000 * math.pi * 0.1**2)
    assert metrics.h_coercive == pytest.approx(1.5)
    assert dict(metrics.summary_items())["Coercivity (Hc)"] == f"1.5 {unit}"


@pytest.mark.parametrize("x_unit,y_unit", [("Hz", "dBm"), ("A", "V"), ("", ""), ("T", "V")])
def test_long_series_and_misleading_names_do_not_create_mr_metrics(x_unit, y_unit):
    metrics = calculate_mtj_metrics(X, R, x_name="magnetic field", y_name="resistance", x_unit=x_unit, y_unit=y_unit)
    assert metrics.curve_type == "scalar_series"
    assert metrics.r_p is None
    assert metrics.r_min is None
    assert metrics.tmr_percent is None
    assert metrics.h_coercive is None
    assert not metrics.summary_items()


def test_iv_prefixes_produce_ohms_not_current_extrema_labeled_ohms():
    metrics = calculate_mtj_metrics([-1, 0, 1, 2], [-1, 0, 1, 2], x_unit="mV", y_unit="uA")
    assert metrics.curve_type == "iv_curve"
    assert metrics.r_p == pytest.approx(1000)
    assert metrics.r_min is None and metrics.r_max is None


def test_resistance_versus_time_has_no_invented_field_or_parallel_state():
    metrics = calculate_mtj_metrics(X, R, x_unit="s", y_unit="kohm")
    assert metrics.r_min == 1000 and metrics.r_max == 2000
    assert metrics.r_p is None and metrics.h_coercive is None


def test_missing_samples_do_not_create_switching_crossings():
    resistance = list(R)
    resistance[5] = resistance[11] = math.nan
    metrics = calculate_mtj_metrics(X, resistance, x_unit="T", y_unit="ohm")
    assert metrics.r_p == 1
    assert metrics.h_coercive is None and metrics.h_offset is None


def test_card_uses_same_units_as_summary(shell_qt_application):
    metrics = calculate_mtj_metrics(X, R, x_unit="mT", y_unit="kohm")
    run = SampleRunRecord("sample", "1", "1", "test", "absent.h5", "", "", "completed", 13, 0, "loop")
    card = MeasurementAnalyticsCard()
    try:
        card.resize(1000, 400)
        card.set_data(run, None, metrics)
        card.show()
        shell_qt_application.processEvents()
        assert card.tile_hc.value_label.text() == "1.5 mT"
        assert card.tile_hoff.value_label.text() == "0 mT"
        assert card.tile_hc.isVisible()
    finally:
        card.close()
        card.deleteLater()
