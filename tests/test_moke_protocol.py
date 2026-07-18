from __future__ import annotations

import unittest

from app.devices.moke_box.protocol import (
    MokeFrame, MokeTarget, decode_voltage, encode_voltage, readback_vout, request_samples,
    set_hall_gains, set_kerr_gain, set_vout,
)
from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig, hall_field_from_voltage
from app.domain.errors import DeviceError


class MokeProtocolTests(unittest.TestCase):
    def test_confirmed_vout_vectors(self) -> None:
        self.assertEqual(encode_voltage(-10), (0x00, 0x00))
        self.assertEqual(encode_voltage(-5), (0x40, 0x00))
        self.assertEqual(encode_voltage(0), (0x80, 0x00))
        self.assertEqual(encode_voltage(1), (0x8C, 0xCD))
        self.assertEqual(encode_voltage(10), (0xFF, 0xFF))
        self.assertAlmostEqual(decode_voltage(0x80, 0x00), 0.0)

    def test_confirmed_command_vectors(self) -> None:
        self.assertEqual(set_vout(2, 1), bytes.fromhex("128CCD5E"))
        self.assertEqual(readback_vout(), bytes.fromhex("18000018"))
        self.assertEqual(request_samples(100), bytes.fromhex("380064C8"))
        self.assertEqual(set_hall_gains(10, 100), bytes.fromhex("0809001A"))
        self.assertEqual(set_kerr_gain(MokeTarget.KERR0, 100), bytes.fromhex("4802004C"))
        self.assertEqual(set_kerr_gain(MokeTarget.KERR1, 1000), bytes.fromhex("8803008E"))

    def test_confirmed_hall_polynomial_vectors(self) -> None:
        self.assertAlmostEqual(hall_field_from_voltage(-2), -0.02672380095)
        self.assertAlmostEqual(hall_field_from_voltage(0), -0.0007387072430926411)
        self.assertAlmostEqual(hall_field_from_voltage(2), 0.02526823526)

    def test_decoder_rejects_bad_checksum(self) -> None:
        with self.assertRaises(DeviceError):
            MokeFrame.decode(bytes.fromhex("D28000D3"))

    def test_binary_adapter_reads_all_vouts(self) -> None:
        transport = _BinaryTransport(
            b"".join(
                MokeFrame(3, 2, channel, *encode_voltage(channel - 4)).encode()
                for channel in range(8)
            )
        )
        adapter = MokeBoxAdapter(MokeBoxConfig("127.0.0.1:10001"), transport)
        adapter.connect()

        values = adapter.read_vouts()

        self.assertEqual(transport.sent, [readback_vout()])
        self.assertEqual(set(values), set(range(8)))
        self.assertAlmostEqual(values[4], 0.0)

    def test_binary_adapter_rejects_unqualified_vout_write(self) -> None:
        adapter = MokeBoxAdapter(MokeBoxConfig("127.0.0.1:10001"), _BinaryTransport(b""))
        adapter.connect()

        with self.assertRaises(DeviceError):
            adapter.set_vout(0, 1.0)


class _BinaryTransport:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent: list[bytes] = []

    def connect(self, _endpoint: str, _timeout_s: float) -> None:
        return None

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    def recv_exact(self, count: int) -> bytes:
        if len(self.response) != count:
            raise AssertionError(f"expected {count} bytes, got {len(self.response)}")
        return self.response

    def close(self) -> None:
        return None
