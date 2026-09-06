"""Safe adapter boundary for the externally supplied MOKE Box protocol."""

from __future__ import annotations

import math
import threading
import time
from typing import Protocol

from app.devices.base import DeviceAdapter
from app.devices.moke_box.models import (
    MokeBoxConfig,
    MokeFieldReading,
    MokeHallVoltageReading,
    MokeReading,
    MokeSampleBatch,
)
from app.devices.moke_box.protocol import (
    MokeAd7734Frame,
    MokeFrame,
    MokeGain,
    MokeResponseType,
    MokeTarget,
    decode_voltage,
    readback_vout,
    request_samples,
    set_hall_gains,
    set_kerr_gain,
    set_vout,
)
from app.domain.errors import ConnectionError, DeviceError
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState


class MokeBoxTransport(Protocol):
    """Minimal protocol to be implemented once the MOKE Box wire API is qualified."""

    def connect(self, endpoint: str, timeout_s: float) -> None: ...
    def identify(self) -> str: ...
    def read_signal(self) -> float: ...
    def close(self) -> None: ...


class MokeBoxBinaryTransport(Protocol):
    """Confirmed TCP record transport; no undocumented identity command exists."""

    def connect(self, endpoint: str, timeout_s: float) -> None: ...
    def send(self, frame: bytes) -> None: ...
    def recv_exact(self, count: int) -> bytes: ...
    def close(self) -> None: ...


class UnavailableMokeBoxAdapter(DeviceAdapter):
    """Fail-closed placeholder used while the MOKE profile is incomplete."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    def connect(self) -> DeviceIdentity:
        self._state = DeviceState.DISCONNECTED
        raise ConnectionError(self._reason)

    def disconnect(self) -> None:
        self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        # No session exists and therefore no protocol command can be sent.
        self._state = DeviceState.DISCONNECTED


class MokeBoxAdapter(DeviceAdapter):
    """Read-only high-level adapter; no unqualified remote control is exposed."""

    def __init__(
        self, config: MokeBoxConfig, transport: MokeBoxTransport | MokeBoxBinaryTransport
    ) -> None:
        super().__init__()
        self._config = config
        self._transport = transport
        self._connected = False
        self._binary_transport = all(
            callable(getattr(transport, name, None)) for name in ("send", "recv_exact")
        )
        self._lock = threading.RLock()

    def _identity_or_raise(self) -> DeviceIdentity:
        if self._identity is None:
            raise ConnectionError("MOKE Box is connected without a verified identity.")
        return self._identity

    def connect(self) -> DeviceIdentity:
        if self._connected:
            return self._identity_or_raise()
        try:
            self._transport.connect(self._config.endpoint, self._config.timeout_s)
            self._connected = True
            if self._binary_transport:
                # Raw TCP has no IDN command. A complete, checksum-valid VOUT
                # response is therefore the non-destructive identity probe.
                self._read_vouts_from_transport()
            identifier = (
                (self._config.expected_model or "MOKE Box binary protocol")
                if self._binary_transport
                else self._legacy_transport().identify().strip()
            )
        except Exception as exc:
            self._connected = False
            try:
                self._transport.close()
            except Exception:
                pass
            self._state = DeviceState.DISCONNECTED
            raise ConnectionError(f"Could not connect to MOKE Box: {exc}") from exc
        if not identifier:
            self._transport.close()
            raise ConnectionError("MOKE Box returned an empty identity.")
        if (
            not self._binary_transport
            and self._config.expected_model
            and self._config.expected_model.casefold() not in identifier.casefold()
        ):
            self._transport.close()
            raise ConnectionError(f"Unexpected MOKE Box identity: {identifier!r}")
        self._identity = DeviceIdentity(resource=self._config.endpoint, idn=identifier, model=self._config.expected_model)
        self._capabilities = DeviceCapabilities(
            device_name="moke_box",
            model=self._config.expected_model or "MOKE Box",
            firmware=None,
            features=frozenset(
                {"read_only", "vout_readback", "hall_voltage_readback"}
                if self._binary_transport
                else {"read_only"}
            ),
        )
        self._state = DeviceState.VERIFIED
        return self._identity

    def disconnect(self) -> None:
        if self._connected:
            try:
                self._transport.close()
            finally:
                self._connected = False
        self._identity = None
        self._capabilities = None
        self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        """Close the session and report UNKNOWN; field-off is not yet qualified."""

        if self._connected:
            try:
                self._transport.close()
            finally:
                self._connected = False
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.UNKNOWN

    def read_signal(self) -> MokeReading:
        if not self._connected:
            raise ConnectionError("MOKE Box is not connected.")
        try:
            value = float(self._legacy_transport().read_signal())
        except Exception as exc:
            self._state = DeviceState.FAULT
            raise DeviceError(f"MOKE Box signal acquisition failed: {exc}") from exc
        if not math.isfinite(value):
            self._state = DeviceState.FAULT
            raise DeviceError("MOKE Box returned a non-finite signal.")
        return MokeReading.now(value)

    def read_vouts(self) -> dict[int, float]:
        """Read all eight DAC values using the confirmed 32-byte response."""

        self._require_binary_transport()
        with self._lock:
            try:
                return self._read_vouts_from_transport()
            except Exception as exc:
                self._fault_and_close(exc, "MOKE VOUT readback failed")

    def _read_vouts_from_transport(self) -> dict[int, float]:
        transport = self._require_binary_transport()
        transport.send(readback_vout())
        frames = self._decode_frames(transport.recv_exact(32))
        values: dict[int, float] = {}
        for frame in frames:
            if (
                frame.origin not in {MokeTarget.MAIN_BOX, MokeTarget.OPT2}
                or frame.record_type != MokeResponseType.AD5362
                or frame.channel in values
            ):
                raise DeviceError("Unexpected MOKE VOUT readback record.")
            values[frame.channel] = decode_voltage(frame.msb, frame.lsb)
        if set(values) != set(range(8)):
            raise DeviceError("MOKE VOUT readback did not contain channels 0..7 exactly once.")
        return values

    def acquire_samples(self, count: int, *, active_streams: int = 4) -> MokeSampleBatch:
        """Acquire the documented AD7734 stream batch without stream resynchronisation."""

        if active_streams not in {4, 7, 10}:
            raise ValueError("MOKE active_streams must be one of 4, 7, or 10.")
        transport = self._require_binary_transport()
        with self._lock:
            try:
                time.sleep(0.005)
                transport.send(request_samples(count))
                frames = self._decode_frames(transport.recv_exact(4 * (count * active_streams + 10)))
                streams: dict[str, list[int]] = {}
                for frame in frames:
                    if frame.record_type != MokeResponseType.AD7734:
                        continue
                    stream = self._stream_name(frame)
                    if stream is None:
                        continue
                    bucket = streams.setdefault(stream, [])
                    if len(bucket) < count:
                        bucket.append(frame.value_u16)
                if len(streams) != active_streams or any(len(values) != count for values in streams.values()):
                    raise DeviceError("MOKE sample response is incomplete or has an unexpected stream layout.")
                return MokeSampleBatch.now(
                    {name: tuple(values) for name, values in streams.items()}, count
                )
            except Exception as exc:
                self._fault_and_close(exc, "MOKE sample acquisition failed")

    def read_fields(self, count: int) -> MokeFieldReading:
        """Acquire and average Hall1/Hall2 using the documented base polynomial."""

        batch = self.acquire_samples(count, active_streams=4)
        try:
            hall1 = tuple(decode_voltage(sample >> 8, sample & 0xFF) for sample in batch.samples_by_stream["main_box.0"])
            hall2 = tuple(decode_voltage(sample >> 8, sample & 0xFF) for sample in batch.samples_by_stream["main_box.2"])
            return MokeFieldReading.from_hall_voltages(hall1, hall2)
        except (KeyError, ValueError) as exc:
            self._fault_and_close(exc, "MOKE Hall field calculation failed")

    def read_hall_voltage(self, count: int = 1) -> MokeHallVoltageReading:
        """Read Hall-1 directly using the physical AD7734 24-bit response layout.

        The live unit returns one MainBox/channel-0 record per requested sample.
        Multi-channel batch framing is deliberately not used here because the
        reconstructed ``N * streams + 10`` layout was not observed on hardware.
        """

        if count != 1:
            raise ValueError(
                "The physical MOKE Box currently qualifies one Hall sample per request only."
            )
        transport = self._require_binary_transport()
        with self._lock:
            try:
                transport.send(request_samples(count))
                frame = MokeAd7734Frame.decode(transport.recv_exact(4))
                if frame.origin != MokeTarget.MAIN_BOX or frame.channel != 0:
                    raise DeviceError("Unexpected MOKE Hall response; expected MainBox channel 0.")
                return MokeHallVoltageReading.from_ad7734_codes(
                    (frame.code_u24,)
                )
            except Exception as exc:
                self._fault_and_close(exc, "MOKE Hall-voltage read failed")

    def set_hall_gains(self, hall1: MokeGain | int, hall2: MokeGain | int) -> None:
        """Set the two documented Hall gains; the command has no firmware ACK."""

        self._send_control(set_hall_gains(hall1, hall2), "MOKE Hall gain update failed")

    def set_kerr_gain(self, target: MokeTarget, gain: MokeGain | int) -> None:
        """Set one documented Kerr gain; only Kerr0/Kerr1 targets are accepted."""

        self._send_control(set_kerr_gain(target, gain), "MOKE Kerr gain update failed")

    def set_vout(self, channel: int, voltage_v: float) -> float:
        """Guarded write with mandatory readback; disabled by the default profile."""

        if not self._config.allow_vout_control or channel not in self._config.allowed_vout_channels:
            raise DeviceError("MOKE VOUT control is not qualified for this channel.")
        if not math.isfinite(voltage_v) or not -10.0 <= voltage_v <= 10.0:
            raise DeviceError("MOKE VOUT voltage must be finite and within -10 V..10 V.")
        transport = self._require_binary_transport()
        with self._lock:
            try:
                transport.send(set_vout(channel, voltage_v))
                actual = self.read_vouts()[channel]
                if not math.isclose(actual, voltage_v, abs_tol=0.001):
                    raise DeviceError("MOKE VOUT readback differs from requested value.")
                return actual
            except Exception as exc:
                self._fault_and_close(exc, "MOKE VOUT write failed")

    def ramp_vout(self, channel: int, voltage_v: float) -> float:
        """Use the documented 50 mV / 25 ms ramp, then verify with readback."""

        if not self._config.allow_vout_control or channel not in self._config.allowed_vout_channels:
            raise DeviceError("MOKE VOUT control is not qualified for this channel.")
        if not math.isfinite(voltage_v) or not -10.0 <= voltage_v <= 10.0:
            raise DeviceError("MOKE VOUT ramp target must be finite and within -10 V..10 V.")
        start = self.read_vouts()[channel]
        target = voltage_v
        direction = 1.0 if target >= start else -1.0
        value = start
        while abs(target - value) > 0.05:
            value += direction * 0.05
            self.set_vout(channel, value)
            time.sleep(0.025)
        return self.set_vout(channel, target)

    def _legacy_transport(self) -> MokeBoxTransport:
        if self._binary_transport:
            raise DeviceError("The binary MOKE protocol does not expose a generic signal command.")
        return self._transport  # type: ignore[return-value]

    def _require_binary_transport(self) -> MokeBoxBinaryTransport:
        if not self._connected:
            raise ConnectionError("MOKE Box is not connected.")
        if not self._binary_transport:
            raise DeviceError("The selected MOKE transport does not support binary records.")
        return self._transport  # type: ignore[return-value]

    @staticmethod
    def _decode_frames(raw: bytes) -> tuple[MokeFrame, ...]:
        if len(raw) % 4:
            raise DeviceError("MOKE response length is not aligned to four-byte records.")
        return tuple(MokeFrame.decode(raw[index:index + 4]) for index in range(0, len(raw), 4))

    @staticmethod
    def _stream_name(frame: MokeFrame) -> str | None:
        if frame.origin == MokeTarget.MAIN_BOX and frame.channel in range(4):
            return f"main_box.{frame.channel}"
        if frame.origin == MokeTarget.KERR0 and frame.channel in range(3):
            return f"kerr0.{frame.channel}"
        if frame.origin == MokeTarget.KERR1 and frame.channel in range(3):
            return f"kerr1.{frame.channel}"
        return None

    def _fault_and_close(self, exc: Exception, message: str) -> None:
        self.disconnect()
        self._state = DeviceState.FAULT
        if isinstance(exc, DeviceError):
            raise exc
        raise DeviceError(f"{message}: {exc}") from exc

    def _send_control(self, frame: bytes, message: str) -> None:
        transport = self._require_binary_transport()
        with self._lock:
            try:
                transport.send(frame)
            except Exception as exc:
                self._fault_and_close(exc, message)
