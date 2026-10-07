"""SI prefix case must survive parsing, conversion and formatted round trips."""
import pytest

from app.domain.quantities import QuantityError, parse_quantity


@pytest.mark.parametrize("text, dimension, expected", [
    ("1 mV", "voltage", 1e-3),
    ("1 mv", "voltage", 1e-3),
    ("1 mA", "current", 1e-3),
    ("1 mHz", "frequency", 1e-3),
    ("1 MHz", "frequency", 1e6),
    ("1 Mhz", "frequency", 1e6),
    ("1 Mohm", "resistance", 1e6),
    ("1 mohm", "resistance", 1e-3),
    ("1 mΩ", "resistance", 1e-3),
    ("1 MΩ", "resistance", 1e6),
    ("-2,5e-3 µA", "current", -2.5e-9),
    ("2 μV", "voltage", 2e-6),
    ("-30 dBm", "dbm", -30),
    ("1 mW/Hz", "power_density", 1e-3),
    ("1 mW*Hz", "spectral_area", 1e-3),
])
def test_prefix_scale_and_round_trip(text, dimension, expected):
    value = parse_quantity(text, dimension)
    assert value.si_value == pytest.approx(expected, rel=1e-12, abs=0)
    unit = text.split()[-1]
    assert parse_quantity(value.format(unit), dimension).si_value == pytest.approx(expected, rel=1e-12, abs=0)


@pytest.mark.parametrize("unit", ["MV", "MA", "MW", "MS", "MT", "MW/Hz", "MV/s", "NS"])
def test_unsupported_prefix_is_rejected_not_scaled_as_milli(unit):
    with pytest.raises(QuantityError, match="Unknown unit"):
        parse_quantity("1 " + unit)
