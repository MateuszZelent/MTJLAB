"""Verified four-byte MOKE Box wire codec reconstructed from LabVIEW."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math

from app.domain.errors import DeviceError


class MokeTarget(IntEnum):
    MAIN_BOX = 0
    KERR0 = 1
    KERR1 = 2
    OPT2 = 3


class MokeCommandType(IntEnum):
    SET_GAIN = 1
    SET_VOUT = 2
    READBACK_VOUT = 3
    SEND_DATA = 7


class MokeResponseType(IntEnum):
    AD7734 = 1
    AD5362 = 2


class MokeGain(IntEnum):
    X1 = 0
    X10 = 1
    X100 = 2
    X1000 = 3


def checksum(header: int, msb: int, lsb: int) -> int:
    return (header + 2 * msb + 4 * lsb) & 0xFF


def make_header(target: int, record_type: int, channel: int) -> int:
    if not 0 <= target <= 3 or not 0 <= record_type <= 7 or not 0 <= channel <= 7:
        raise ValueError("MOKE header fields are outside their bit ranges.")
    return (target << 6) | (record_type << 3) | channel


@dataclass(frozen=True, slots=True)
class MokeFrame:
    origin: int
    record_type: int
    channel: int
    msb: int
    lsb: int

    @property
    def value_u16(self) -> int:
        return (self.msb << 8) | self.lsb

    def encode(self) -> bytes:
        header = make_header(self.origin, self.record_type, self.channel)
        return bytes((header, self.msb, self.lsb, checksum(header, self.msb, self.lsb)))

    @classmethod
    def decode(cls, raw: bytes) -> "MokeFrame":
        if len(raw) != 4:
            raise DeviceError(f"MOKE record must have 4 bytes, received {len(raw)}.")
        header, msb, lsb, received_checksum = raw
        if received_checksum != checksum(header, msb, lsb):
            raise DeviceError("MOKE checksum mismatch; transport stream is unsafe to continue.")
        return cls((header >> 6) & 0x03, (header >> 3) & 0x07, header & 0x07, msb, lsb)


def encode_voltage(voltage_v: float) -> tuple[int, int]:
    if not math.isfinite(voltage_v):
        raise ValueError("MOKE VOUT voltage must be finite.")
    voltage_v = max(-10.0, min(10.0, float(voltage_v)))
    scale = 3276.7 if voltage_v >= 0 else 3276.8
    value = max(0, min(65535, int(round(32768 + scale * voltage_v))))
    return value >> 8, value & 0xFF


def decode_voltage(msb: int, lsb: int) -> float:
    signed = ((msb << 8) | lsb) - 32768
    return 10.0 * signed / (32767.0 if signed >= 0 else 32768.0)


def set_vout(channel: int, voltage_v: float) -> bytes:
    msb, lsb = encode_voltage(voltage_v)
    return MokeFrame(MokeTarget.MAIN_BOX, MokeCommandType.SET_VOUT, channel, msb, lsb).encode()


def set_hall_gains(hall1: MokeGain | int, hall2: MokeGain | int) -> bytes:
    """Build the confirmed packed gain command for the two Hall channels."""

    first, second = _gain_code(hall1), _gain_code(hall2)
    return MokeFrame(
        MokeTarget.MAIN_BOX, MokeCommandType.SET_GAIN, 0, 4 * second + first, 0
    ).encode()


def set_kerr_gain(target: MokeTarget, gain: MokeGain | int) -> bytes:
    """Build a confirmed gain command for either Kerr acquisition module."""

    if target not in {MokeTarget.KERR0, MokeTarget.KERR1}:
        raise ValueError("MOKE Kerr gain target must be KERR0 or KERR1.")
    return MokeFrame(target, MokeCommandType.SET_GAIN, 0, _gain_code(gain), 0).encode()


def readback_vout() -> bytes:
    return MokeFrame(MokeTarget.MAIN_BOX, MokeCommandType.READBACK_VOUT, 0, 0, 0).encode()


def request_samples(count: int) -> bytes:
    if not 1 <= count <= 60_000:
        raise ValueError("MOKE sample count must be in 1..60000.")
    return MokeFrame(MokeTarget.MAIN_BOX, MokeCommandType.SEND_DATA, 0, count >> 8, count & 0xFF).encode()


def _gain_code(gain: MokeGain | int) -> int:
    if isinstance(gain, MokeGain):
        return int(gain)
    try:
        return {1: 0, 10: 1, 100: 2, 1000: 3}[gain]
    except KeyError as exc:
        raise ValueError("MOKE gain must be one of 1, 10, 100, or 1000.") from exc
