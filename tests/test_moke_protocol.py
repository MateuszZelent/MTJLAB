from __future__ import annotations

import unittest

from app.devices.moke_box.protocol import (
    MokeAd7734Frame, MokeFrame, MokeTarget, decode_ad7734_voltage, decode_voltage,
    encode_voltage, readback_vout, request_samples,
    set_hall_gains, set_kerr_gain, set_vout,
)
from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig, hall_field_from_voltage
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.simulation import SimulationContext
from app.domain.errors import ConnectionError, DeviceError


class MokeProtocolTests(unittest.TestCase):
    def test_confirmed_vout_vectors(self) -> None:
        self.assertEqual(encode_voltage(-10), (0x00, 0x00))
        self.assertEqual(encode_voltage(-5), (0x40, 0x00))
        self.assertEqual(encode_voltage(0), (0x80, 0x00))
        self.assertEqual(encode_voltage(1), (0x8C, 0xCD))
        self.assertEqual(encode_voltage(10), (0xFF, 0xFF))
        self.assertAlmostEqual(decode_voltage(0x80, 0x00), 0.0)
        for invalid in (-10.0001, 10.0001, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                encode_voltage(invalid)

    def test_confirmed_command_vectors(self) -> None:
        self.assertEqual(set_vout(2, 1), bytes.fromhex("128CCD3E"))
        self.assertEqual(set_vout(2, -1), bytes.fromhex("12733338"))
        self.assertEqual(readback_vout(), bytes.fromhex("18000018"))
        self.assertEqual(request_samples(100), bytes.fromhex("380064A8"))
        self.assertEqual(set_hall_gains(10, 100), bytes.fromhex("0809001A"))
        self.assertEqual(set_kerr_gain(MokeTarget.KERR0, 100), bytes.fromhex("4802004C"))
        self.assertEqual(set_kerr_gain(MokeTarget.KERR1, 1000), bytes.fromhex("8803008E"))

    def test_hall_conversion_uses_one_tesla_per_volt_reference_scale(self) -> None:
        self.assertAlmostEqual(hall_field_from_voltage(-0.1), -0.1)
        self.assertAlmostEqual(hall_field_from_voltage(0), 0.0)
        self.assertAlmostEqual(hall_field_from_voltage(0.1), 0.1)

    def test_decoder_rejects_bad_checksum(self) -> None:
        with self.assertRaises(DeviceError):
            MokeFrame.decode(bytes.fromhex("D28000D3"))

    def test_live_nonzero_vout_reply_connects_without_mutating_outputs(self) -> None:
        # Literal bytes from the operator's connection-error screenshot.
        # Do not generate this oracle using the codec under test.
        reply = bytes.fromhex(
            "10 80 00 10 11 80 00 11 12 82 8f 2a 13 80 00 13 "
            "14 80 00 14 15 80 00 15 16 80 00 16 17 80 00 17"
        )
        transport = _BinaryTransport(reply + reply)
        adapter = MokeBoxAdapter(MokeBoxConfig("127.0.0.1:10001"), transport)
        adapter.connect()
        values = adapter.read_vouts()
        self.assertAlmostEqual(values[2], 6550 / 32767)
        self.assertTrue(all(value == 0 for channel, value in values.items() if channel != 2))
        self.assertEqual(transport.sent, [bytes.fromhex("18000018")] * 2)
        self.assertEqual(MokeFrame.decode(reply[8:12]).encode(), bytes.fromhex("12828f2a"))

    def test_additive_checksum_is_rejected_for_nonzero_record(self) -> None:
        with self.assertRaisesRegex(DeviceError, "expected=0x2A, received=0x52"):
            MokeFrame.decode(bytes.fromhex("12828f52"))

    def test_set_vout_encodes_live_nonzero_register_with_parity(self) -> None:
        # Independent oracle from the same physical reply: SET uses this header.
        self.assertEqual(set_vout(2, 0.2), bytes.fromhex("12828f2a"))

    def test_failed_connection_retains_full_reply_and_closes_without_output_write(self) -> None:
        reply = bytes.fromhex("10800011") + _vout_response()[4:]
        transport = _BinaryTransport(reply)
        transport.closed = False
        transport.close = lambda: setattr(transport, "closed", True)
        adapter = MokeBoxAdapter(MokeBoxConfig("127.0.0.1:10001"), transport)
        with self.assertRaises(ConnectionError) as failure:
            adapter.connect()
        message = str(failure.exception)
        self.assertIn(reply.hex(" "), message)
        self.assertIn("expected=0x10, received=0x11", message)
        self.assertEqual(transport.sent, [readback_vout(), readback_vout()])
        self.assertTrue(transport.closed)
        self.assertFalse(adapter.connected)

    def test_live_ad7734_frame_uses_24_bit_payload_not_checksum(self) -> None:
        frame = MokeAd7734Frame.decode(bytes.fromhex("087EBA1C"))

        self.assertEqual(frame.origin, MokeTarget.MAIN_BOX)
        self.assertEqual(frame.channel, 0)
        self.assertEqual(frame.code_u24, 0x7EBA1C)
        self.assertAlmostEqual(decode_ad7734_voltage(frame.code_u24), -0.09945, places=4)

    def test_binary_adapter_reads_all_vouts(self) -> None:
        transport = _BinaryTransport(
            _vout_response() + _vout_response(values=tuple(channel - 4 for channel in range(8)))
        )
        adapter = MokeBoxAdapter(MokeBoxConfig("127.0.0.1:10001"), transport)
        adapter.connect()

        values = adapter.read_vouts()

        self.assertEqual(transport.sent, [readback_vout(), readback_vout()])
        self.assertEqual(set(values), set(range(8)))
        self.assertAlmostEqual(values[4], 0.0)

    def test_binary_adapter_rejects_unqualified_vout_write(self) -> None:
        adapter = MokeBoxAdapter(
            MokeBoxConfig("127.0.0.1:10001"), _BinaryTransport(_vout_response())
        )
        adapter.connect()

        with self.assertRaises(DeviceError):
            adapter.set_vout(0, 1.0)

    def test_binary_adapter_rejects_out_of_range_vout_before_transport(self) -> None:
        transport = _BinaryTransport(_vout_response())
        adapter = MokeBoxAdapter(
            MokeBoxConfig(
                "127.0.0.1:10001",
                allow_vout_control=True,
                allowed_vout_channels=(0,),
            ),
            transport,
        )
        adapter.connect()
        sent_before = list(transport.sent)

        with self.assertRaisesRegex(DeviceError, "within -10 V..10 V"):
            adapter.set_vout(0, 10.1)

        self.assertEqual(transport.sent, sent_before)

    def test_binary_adapter_reads_live_hall1_24_bit_sample(self) -> None:
        sample = MokeAd7734Frame(MokeTarget.MAIN_BOX, 0, 0x7EBA1C).encode()
        transport = _BinaryTransport(_vout_response() + sample)
        adapter = MokeBoxAdapter(MokeBoxConfig("127.0.0.1:10001"), transport)
        adapter.connect()

        reading = adapter.read_hall_voltage(1)

        self.assertEqual(transport.sent, [readback_vout(), request_samples(1)])
        self.assertEqual(reading.samples, 1)
        self.assertAlmostEqual(reading.voltage_v, -0.09945, places=4)

    def test_connected_binary_adapter_advertises_read_only_capabilities(self) -> None:
        adapter = MokeBoxAdapter(
            MokeBoxConfig("SIM::MOKE::INSTR"),
            SimulatedMokeBoxTransport(SimulationContext(seed=0)),
        )

        adapter.connect()

        self.assertEqual(
            adapter.capabilities.features,
            frozenset({"read_only", "vout_readback", "hall_voltage_readback"}),
        )


class _BinaryTransport:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent: list[bytes] = []

    def connect(self, _endpoint: str, _timeout_s: float) -> None:
        return None

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    def recv_exact(self, count: int) -> bytes:
        if len(self.response) < count:
            raise AssertionError(f"expected at least {count} bytes, got {len(self.response)}")
        result, self.response = self.response[:count], self.response[count:]
        return result

    def close(self) -> None:
        return None


def _vout_response(
    *, values: tuple[float, ...] = (0.0,) * 8, origin: MokeTarget = MokeTarget.MAIN_BOX
) -> bytes:
    return b"".join(
        MokeFrame(origin, 2, channel, *encode_voltage(value)).encode()
        for channel, value in enumerate(values)
    )
