"""Reference arithmetic must preserve dimensions without silent saturation."""

import math

import pytest

from app.spectrum.processing import apply_reference_operation


OPERATIONS = (
    "difference_db", "ratio_linear", "multiply_linear", "add_power",
    "subtract_power", "subtract_power_signed",
)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("signal,reference", [([[1.0, 2.0]], [1.0, 2.0]), ([1.0, 2.0], [[1.0], [2.0]])])
def test_rejects_broadcastable_non_spectra(operation, signal, reference):
    with pytest.raises(ValueError, match="one-dimensional"):
        apply_reference_operation(signal, reference, operation)


@pytest.mark.parametrize("operation,signal,reference,expected,unit", [
    ("ratio_linear", [4000, -4000], [4000, -4000], [1, 1], "ratio"),
    ("multiply_linear", [4000, -4000], [-4000, 4000], [1, 1], "mW²"),
    ("add_power", [4000, -4000], [4000, -4000],
     [4000 + 10 * math.log10(2), -4000 + 10 * math.log10(2)], "dBm"),
    ("subtract_power", [-3100, -3100], [-3110, -3110],
     [-3100 + 10 * math.log10(0.9)] * 2, "dBm"),
    ("subtract_power_signed", [0, -10], [-10, 0], [0.0009, -0.0009], "W"),
    ("subtract_power_signed", [4000, -4000], [4000, -4000], [0, 0], "W"),
])
def test_representable_results_survive_extreme_intermediate_powers(operation, signal, reference, expected, unit):
    values, actual_unit = apply_reference_operation(signal, reference, operation)
    assert actual_unit == unit
    assert values == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("operation,signal,reference", [
    ("difference_db", [1e308] * 2, [-1e308] * 2),
    ("ratio_linear", [4000] * 2, [0] * 2),
    ("ratio_linear", [-4000] * 2, [0] * 2),
    ("multiply_linear", [2000] * 2, [2000] * 2),
    ("multiply_linear", [-2000] * 2, [-2000] * 2),
    ("subtract_power_signed", [4000] * 2, [0] * 2),
    ("subtract_power_signed", [-4000] * 2, [-4010] * 2),
])
def test_unrepresentable_output_is_an_explicit_error(operation, signal, reference):
    with pytest.raises(ValueError, match="numeric range"):
        apply_reference_operation(signal, reference, operation)


def test_nearly_equal_powers_keep_small_signed_residual():
    delta_db = 1e-12
    values, unit = apply_reference_operation([delta_db, 0], [0, delta_db], "subtract_power_signed")
    expected = 1e-3 * math.expm1(delta_db * math.log(10) / 10)
    assert unit == "W"
    assert values == pytest.approx([expected, -expected], rel=1e-12, abs=0)
