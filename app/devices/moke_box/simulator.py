"""In-memory binary MOKE Box transport used only by simulation runs and tests."""

from __future__ import annotations

from app.devices.moke_box.protocol import (
    MokeAd7734Frame,
    MokeCommandType,
    MokeFrame,
    MokeResponseType,
    MokeTarget,
    encode_voltage,
)
from app.devices.simulation import SimulationContext
from app.domain.errors import ConnectionError, DeviceError


class SimulatedMokeBoxTransport:
    """Implement the qualified binary subset without opening a socket."""

    def __init__(self, context: SimulationContext) -> None:
        self._random = context.random_stream("moke_box", "hall")
        self._connected = False
        self._pending = b""
        self._vouts = {channel: 0.0 for channel in range(8)}

    def connect(self, endpoint: str, timeout_s: float) -> None:
        if not endpoint.startswith("SIM::MOKE"):
            raise ConnectionError("Simulated MOKE transport accepts only SIM::MOKE endpoints.")
        if timeout_s <= 0:
            raise ConnectionError("MOKE simulation timeout must be positive.")
        self._connected = True

    def send(self, frame: bytes) -> None:
        if not self._connected:
            raise ConnectionError("Simulated MOKE Box is not connected.")
        command = MokeFrame.decode(frame)
        if command.record_type == MokeCommandType.READBACK_VOUT:
            self._pending = b"".join(
                MokeFrame(
                    MokeTarget.MAIN_BOX,
                    MokeResponseType.AD5362,
                    channel,
                    *encode_voltage(self._vouts[channel]),
                ).encode()
                for channel in range(8)
            )
        elif command.record_type == MokeCommandType.SEND_DATA:
            requested = command.value_u16
            if requested != 1:
                raise DeviceError("MOKE simulation supports one Hall sample per request.")
            voltage = self._random.uniform(-0.25, 0.25)
            signed = int(round(max(-1.0, min(1.0, voltage / 10.0)) * 0x7FFFFF))
            self._pending = MokeAd7734Frame(
                MokeTarget.MAIN_BOX, 0, signed + 0x800000
            ).encode()
        elif command.record_type == MokeCommandType.SET_VOUT:
            self._vouts[command.channel] = max(-10.0, min(10.0, command.value_u16 / 3276.7 - 10.0))
        elif command.record_type != MokeCommandType.SET_GAIN:
            raise DeviceError(f"MOKE simulation does not support command {command.record_type!r}.")

    def recv_exact(self, count: int) -> bytes:
        if not self._connected:
            raise ConnectionError("Simulated MOKE Box is not connected.")
        if len(self._pending) != count:
            raise DeviceError(
                f"Simulated MOKE response length {len(self._pending)} differs from requested {count}."
            )
        response, self._pending = self._pending, b""
        return response

    def close(self) -> None:
        self._connected = False
        self._pending = b""
