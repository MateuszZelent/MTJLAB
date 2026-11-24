"""Front-panel capture must remain a bounded, read-only device operation."""

import struct

import pytest

from app.devices.rigol_dg1000z.adapter import RigolAdapter
from app.devices.rigol_dg1000z.module import _dispatch
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError
from tests.helpers import simulation_settings


def bmp_bytes():
    pixels = bytes(320 * 240 * 3)
    header = struct.pack('<2sIHHI', b'BM', 54 + len(pixels), 0, 0, 54)
    info = struct.pack('<IiiHHIIiiII', 40, 320, 240, 1, 24, 0, len(pixels), 0, 0, 0, 0)
    return header + info + pixels


class ScreenSession(RigolSimulator):
    data = bmp_bytes()
    failure = None

    def query_binary_values(self, command, datatype='f', is_big_endian=False, container=list):
        assert command == ':HCOP:SDUM:DATA?'
        assert datatype == 'B' and container is bytes
        assert self.timeout <= 1000
        self.commands.append(command)
        if self.failure:
            raise self.failure
        return self.data


def adapter_with_screen():
    session = ScreenSession()
    adapter = RigolAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    session.commands.clear()
    return adapter, session


@pytest.mark.parametrize('on', [False, True])
def test_capture_reads_only_and_preserves_outputs_and_confirmation(on):
    adapter, session = adapter_with_screen()
    session.output[1] = on
    state = adapter.state
    timeout = session.timeout
    assert _dispatch(adapter, 'capture_screen', None) == bmp_bytes()
    assert session.commands == [':HCOP:SDUM:DATA?']
    assert session.output == {1: on, 2: False}
    assert session.timeout == timeout
    assert adapter.state == state


@pytest.mark.parametrize('data', [b'', b'not an image', bmp_bytes()[:-10], b'BM' + bytes(70)],
                         ids=['empty', 'text', 'truncated', 'bad-header'])
def test_rejects_invalid_screen(data):
    adapter, session = adapter_with_screen()
    session.data = data
    with pytest.raises(DeviceError, match='screen|BMP'):
        adapter.capture_screen()
    assert session.commands == [':HCOP:SDUM:DATA?']


def test_timeout_is_not_retried_and_timeout_is_restored():
    adapter, session = adapter_with_screen()
    timeout = session.timeout
    session.failure = TimeoutError('screen timeout')
    with pytest.raises(DeviceError, match='screen'):
        adapter.capture_screen()
    assert session.commands == [':HCOP:SDUM:DATA?']
    assert session.timeout == timeout
    assert not any(session.output.values())


def test_disconnected_capture_does_not_open_or_enable_hardware():
    adapter = RigolAdapter(simulation_settings())
    with pytest.raises(ConnectionError):
        adapter.capture_screen()


def test_simulation_explains_that_no_real_front_panel_is_available():
    session = RigolSimulator()
    adapter = RigolAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    with pytest.raises(DeviceError, match='simulat'):
        adapter.capture_screen()
