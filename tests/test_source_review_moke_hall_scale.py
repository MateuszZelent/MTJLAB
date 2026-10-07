"""Synthetic Hall ADC and reference magnet must use the same field scale."""
from unittest.mock import Mock

import pytest

from app.devices.moke_box.models import MokeHallVoltageReading, hall_field_from_voltage
from app.devices.moke_box.protocol import MokeAd7734Frame, request_samples, set_vout
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.simulation import SimulationContext


@pytest.mark.parametrize("voltage", [-5.0, -.1, 0, .1, 5.0])
def test_hall_tracks_shared_magnet_after_binary_dac_write(voltage):
    context = SimulationContext(seed=13)
    transport = SimulatedMokeBoxTransport(context, field_channel=2)
    transport._random = Mock(gauss=Mock(return_value=0.0))
    transport.connect("SIM::MOKE", 1)
    transport.send(set_vout(2, voltage))
    transport.send(request_samples(1))
    frame = MokeAd7734Frame.decode(transport.recv_exact(4))
    reading = MokeHallVoltageReading.from_ad7734_codes((frame.code_u24,))
    assert hall_field_from_voltage(reading.voltage_v) == pytest.approx(context.magnet.field_t(), abs=10 / 0x7FFFFF)
    assert context.metadata(("moke_box",))["moke_hall_model_version"] == "2"


@pytest.mark.parametrize("field,code", [(-12, 0), (12, 0xFFFFFF)])
def test_simulated_adc_saturates_at_both_exact_endpoints(field, code):
    context = SimulationContext(seed=13, magnet=Mock(field_t=Mock(return_value=field)))
    transport = SimulatedMokeBoxTransport(context)
    transport._random = Mock(gauss=Mock(return_value=0.0))
    transport.connect("SIM::MOKE", 1)
    transport.send(request_samples(1))
    assert MokeAd7734Frame.decode(transport.recv_exact(4)).code_u24 == code
