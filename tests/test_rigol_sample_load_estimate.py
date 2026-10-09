"""Analytical checks of the advisory resistive sample model (no instrument I/O)."""

import pytest

from app.devices.rigol_dg1000z.load_estimate import estimate_sample_load


def calculate(**changes):
    args = {"high_v": .082, "low_v": .082, "output_load": "HIGHZ", "waveform": "DC",
            "resistance_ohm": 175., "minimum_ohm": 150., "maximum_ohm": 200.}
    args.update(changes)
    return estimate_sample_load(**args)


def test_dc_distinguishes_nominal_and_different_worst_case_resistances():
    value = calculate()
    assert value.nominal.current_min_a == pytest.approx(.082 / 225)
    assert value.nominal.current_max_a == value.nominal.current_min_a
    assert value.nominal.voltage_min_v == pytest.approx(.082 * 175 / 225)
    assert value.resistance_range.peak_current_a == pytest.approx(.000410)
    assert value.resistance_range.peak_voltage_v == pytest.approx(.0656)
    assert value.resistance_range.current_min_a == pytest.approx(.000328)


@pytest.mark.parametrize("load,factor", [("HIGHZ", 1), ("50", 2), ("100", 1.5)])
@pytest.mark.parametrize("waveform", ["SIN", "SQU", "RAMP", "PULS"])
def test_ac_offset_and_negative_peak_with_load_convention(load, factor, waveform):
    # 600 mVpp at -100 mV offset: negative peak dominates.
    value = calculate(high_v=.2, low_v=-.4, output_load=load, waveform=waveform)
    assert value.nominal.current_min_a == pytest.approx(-.4 * factor / 225)
    assert value.nominal.current_max_a == pytest.approx(.2 * factor / 225)
    assert value.resistance_range.peak_current_a == pytest.approx(.002 * factor)
    assert value.resistance_range.voltage_min_v == pytest.approx(-.32 * factor)
    assert value.resistance_range.voltage_max_v == pytest.approx(.16 * factor)
    assert value.resistance_range.peak_voltage_v == pytest.approx(.32 * factor)


def test_negative_dc_and_zero_voltage():
    value = calculate(high_v=-.6, low_v=-.6)
    assert value.resistance_range.current_min_a == pytest.approx(-.003)
    assert value.resistance_range.current_max_a == pytest.approx(-.0024)
    assert value.resistance_range.voltage_min_v == pytest.approx(-.48)
    zero = calculate(high_v=0., low_v=0.)
    assert zero.resistance_range.peak_current_a == zero.resistance_range.peak_voltage_v == 0


@pytest.mark.parametrize("changes", [
    {"minimum_ohm": 0}, {"resistance_ohm": -1}, {"maximum_ohm": float("inf")},
    {"resistance_ohm": float("nan")}, {"minimum_ohm": 190}, {"maximum_ohm": 160},
    {"high_v": float("nan")}, {"low_v": .2}, {"waveform": "NOIS"},
    {"waveform": "USER"}, {"high_v": .3},
])
def test_invalid_and_unsupported_inputs_never_produce_estimate(changes):
    with pytest.raises(ValueError):
        calculate(**changes)
