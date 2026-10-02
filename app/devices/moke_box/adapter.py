"""Safe adapter boundary for the externally supplied MOKE Box protocol."""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
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
    set_vout,
)
from app.domain.errors import ConnectionError, DeviceError, RunInterrupted, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState
from app.safety.moke_box import MokeVoltagePlan, MokeVoltageResult


class ConfirmedRampError(DeviceError):
    """A complete valid DAC reply permits an approved shutdown attempt."""


# SET_VOUT has no ACK. Keep its confirmation separate from coil/field settling.
VOUT_CONFIRMATION_TIMEOUT_S = 0.25
VOUT_CONFIRMATION_INTERVAL_S = 0.025
VOUT_READBACK_TOLERANCE_V = 0.001


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
    """Readback and qualified, explicitly armed analog programming output."""

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
        self._voltage_plan: MokeVoltagePlan | None = None
        self._armed = False
        self._next_target = 0
        self._stop_requested = threading.Event()
        self._safe_target_confirmed = False
        self._output_changed = False

    @property
    def control_profile(self):
        return self._config.control_profile

    def get_control_profile(self):
        """Read-only typed profile query usable through a controller lease."""
        self._require_binary_transport()
        return self.control_profile

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def safe_target_confirmed(self) -> bool:
        return self._safe_target_confirmed

    @contextmanager
    def io_timeout(self, timeout_s: float):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("MOKE I/O timeout must be finite and positive.")
        self._require_binary_transport()
        scope = getattr(self._transport, "io_timeout", None)
        if callable(scope):
            with scope(timeout_s):
                yield
        else:
            yield  # in-memory test/simulation transport has no blocking socket

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
        features = {"read_only", "vout_readback", "hall_voltage_readback"} if self._binary_transport else {"read_only"}
        if self._binary_transport and self._config.control_profile is not None:
            features.discard("read_only")
            features.update({"voltage_control", "bounded_ramp", "dac_zero_readback"})
        self._capabilities = DeviceCapabilities(
            device_name="moke_box",
            model=self._config.expected_model or "MOKE Box",
            firmware=None,
            features=frozenset(features),
        )
        self._state = DeviceState.VERIFIED
        return self._identity

    def disconnect(self) -> None:
        self._armed = False
        self._voltage_plan = None
        if self._connected and self._output_changed:
            self.emergency_off()
        if self._connected:
            try:
                self._transport.close()
            finally:
                self._connected = False
        self._identity = None
        self._capabilities = None
        self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        """Attempt qualified DAC zero; never claim power-off from DAC readback."""
        self.interrupt_voltage()
        if self._connected and self.control_profile is not None:
            try:
                self.stop_vout()
                return
            except Exception:
                pass
        if self._connected:
            try:
                self._transport.close()
            except Exception:
                pass
        self._connected = False
        self._armed = False
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
        raw = transport.recv_exact(32)
        try:
            frames = self._decode_frames(raw)
        except DeviceError as exc:
            raise DeviceError(f"Invalid MOKE VOUT reply: {raw.hex(' ')}. {exc}") from exc
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
        """Gain writes have no qualified readback and can invalidate calibration."""
        raise SafetyViolation("MOKE Hall gain changes are not qualified for this control workflow.")

    def set_kerr_gain(self, target: MokeTarget, gain: MokeGain | int) -> None:
        """Kerr gain is outside the qualified voltage-control surface."""
        raise SafetyViolation("MOKE Kerr gain changes are not qualified for this control workflow.")

    def set_vout(self, channel: int, voltage_v: float) -> float:
        """Public writes always use the bounded, armed trajectory path."""
        if not self._config.allow_vout_control or channel not in self._config.allowed_vout_channels:
            raise DeviceError("MOKE VOUT control is not qualified for this channel.")
        if isinstance(voltage_v, bool) or not math.isfinite(voltage_v) or not -10.0 <= voltage_v <= 10.0:
            raise DeviceError("MOKE VOUT voltage must be finite and within -10 V..10 V.")
        return self.ramp_vout(channel, voltage_v).actual_v

    def configure_voltage_plan(self, plan: MokeVoltagePlan) -> MokeVoltagePlan:
        """Validate an immutable plan in memory; this never changes a DAC."""
        profile = self.control_profile
        if profile is None or not isinstance(plan, MokeVoltagePlan):
            raise SafetyViolation("MOKE voltage control requires a qualified profile and typed plan.")
        self._require_binary_transport()
        plan.validate(profile)
        with self._lock:
            self._armed = False
            self._voltage_plan = plan
            self._next_target = 0
        return plan

    def arm_voltage_plan(self, plan: MokeVoltagePlan) -> None:
        """Grant one execution of the complete, unchanged prepared trajectory."""
        with self._lock:
            if self.control_profile is None or plan != self._voltage_plan:
                raise SafetyViolation("Prepare the identical MOKE voltage plan before arming.")
            self._require_binary_transport()
            plan.validate(self.control_profile)
            self._next_target = 0
            self._stop_requested.clear()
            self._armed = True

    def interrupt_voltage(self) -> None:
        """Thread-safe cancellation flag; no transport access or queued delay."""
        self._stop_requested.set()

    def disarm_voltage_plan(self) -> None:
        """Revoke the in-memory trajectory without sending a hardware command."""
        self.interrupt_voltage()
        self._armed = False
        self._voltage_plan = None

    def _write_vout(
        self, channel: int, voltage_v: float, *, deadline: float,
        cancel: threading.Event | None, stopping: bool,
    ) -> float:
        transport = self._require_binary_transport()
        self._output_changed = True
        self._safe_target_confirmed = False
        transport.send(set_vout(channel, voltage_v))
        confirmation_deadline = min(deadline, time.monotonic() + VOUT_CONFIRMATION_TIMEOUT_S)
        profile = self.control_profile
        if profile is None:
            raise SafetyViolation("MOKE voltage control is unqualified.")
        # Allow the device to process SET before sending another TCP command.
        # A complete but stale reply permits read-only polling, never a SET retry.
        values: dict[int, float] | None = None
        while True:
            delay = min(
                VOUT_CONFIRMATION_INTERVAL_S,
                max(0, confirmation_deadline - time.monotonic()),
            )
            if not profile.simulation:
                if stopping:
                    time.sleep(delay)
                elif self._stop_requested.wait(delay) or (cancel is not None and cancel.is_set()):
                    raise RunInterrupted("MOKE voltage confirmation was stopped.")
            remaining = confirmation_deadline - time.monotonic()
            if remaining <= 0:
                if values is not None:
                    break
                raise TimeoutError("MOKE VOUT confirmation exceeded its deadline.")
            with self.io_timeout(remaining):
                values = self._read_vouts_from_transport()
            actual = values[channel]
            if abs(actual - voltage_v) <= VOUT_READBACK_TOLERANCE_V:
                return actual
            outside_envelope = not profile.minimum_v <= actual <= profile.maximum_v
            exhausted = time.monotonic() + VOUT_CONFIRMATION_INTERVAL_S >= confirmation_deadline
            if outside_envelope or exhausted:
                break
            if profile.simulation:
                time.sleep(delay)  # Fault-injected stale replies still have a bounded poll rate.
        readings = ", ".join(
            f"VOUT{ch}={value:+.6f} V" for ch, value in sorted(values.items())
        )
        raise ConfirmedRampError(
            f"MOKE VOUT{channel} readback differs from requested value: "
            f"requested {voltage_v:+.6f} V, received {actual:+.6f} V "
            f"(difference {actual - voltage_v:+.6f} V; "
            f"tolerance {VOUT_READBACK_TOLERANCE_V:g} V). {readings}."
        )

    def ramp_vout(
        self, channel: int, voltage_v: float, *, cancel: threading.Event | None = None,
        deadline_s: float | None = None,
    ) -> MokeVoltageResult:
        with self._lock:
            plan, profile = self._voltage_plan, self.control_profile
            if type(channel) is not int or not self._armed or plan is None or profile is None or channel != profile.channel:
                raise SafetyViolation("MOKE voltage trajectory is not armed for this channel.")
            plan.validate(profile)
            if self._next_target >= len(plan.targets_v) or voltage_v != plan.targets_v[self._next_target]:
                raise SafetyViolation("MOKE target is not the next point in the armed trajectory.")
            applied = plan.applied_voltage(voltage_v)
            try:
                actual = self._ramp(applied, cancel=cancel, stopping=False, deadline_s=deadline_s,
                                    settling_s=max(profile.minimum_settling_s, plan.settling_s))
            except RunInterrupted:
                self.stop_vout()
                raise
            except (ConfirmedRampError, TimeoutError) as exc:
                self.stop_vout()
                raise DeviceError(f"{exc}; approved DAC zero was confirmed.") from exc
            except Exception as exc:
                self._fault_and_close(exc, "MOKE voltage ramp failed")
            self._next_target += 1
            if self._next_target == len(plan.targets_v):
                self._armed = False
            self._state = DeviceState.UNKNOWN
            return MokeVoltageResult(channel, voltage_v, applied, actual, profile.fingerprint)

    def _ramp(self, target_v: float, *, cancel: threading.Event | None, stopping: bool,
              deadline_s: float | None = None, settling_s: float = 0.0) -> float:
        profile = self.control_profile
        if profile is None:
            raise SafetyViolation("MOKE voltage control is unqualified.")
        duration = profile.ramp_timeout_s
        if deadline_s is not None:
            if isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float)) or not math.isfinite(deadline_s) or deadline_s <= 0:
                raise SafetyViolation("MOKE ramp deadline must be finite and positive.")
            duration = min(duration, deadline_s)
        deadline = time.monotonic() + duration
        try:
            with self.io_timeout(duration):
                actual = self._read_vouts_from_transport()[profile.channel]
        except Exception as exc:
            self._fault_and_close(exc, "MOKE ramp initial readback failed")
        if not profile.minimum_v <= actual <= profile.maximum_v:
            raise SafetyViolation("Current DAC value is outside the qualified station envelope.")
        # Leave one LSB for rounding so the applied step stays within the bound.
        step = min(profile.maximum_step_v, profile.maximum_slew_v_s * profile.step_interval_s)
        step = max(10 / 32767, step - 10 / 32767)
        envelope = MokeVoltagePlan(profile.fingerprint, profile.channel,
                                   profile.minimum_v, profile.maximum_v, (target_v,))
        while True:
            if not stopping and (self._stop_requested.is_set() or (cancel is not None and cancel.is_set())):
                raise RunInterrupted("MOKE voltage ramp was stopped.")
            if time.monotonic() >= deadline:
                raise TimeoutError("MOKE voltage ramp exceeded its qualified deadline.")
            delta = target_v - actual
            next_v = target_v if abs(delta) <= step else actual + math.copysign(step, delta)
            next_v = envelope.applied_voltage(next_v)
            if next_v == actual and abs(target_v - actual) > 0.001:
                raise DeviceError("MOKE DAC ramp cannot progress within its limits.")
            delay = max(profile.step_interval_s, abs(next_v - actual) / profile.maximum_slew_v_s)
            if not profile.simulation:
                if delay > deadline - time.monotonic():
                    raise TimeoutError("MOKE ramp deadline cannot accommodate the next step.")
                if stopping:
                    time.sleep(delay)
                else:
                    wake = time.monotonic() + delay
                    while time.monotonic() < wake:
                        if self._stop_requested.wait(min(0.02, max(0, wake - time.monotonic()))) or (cancel is not None and cancel.is_set()):
                            raise RunInterrupted("MOKE voltage ramp was stopped.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MOKE voltage ramp exceeded its qualified deadline.")
            try:
                with self.io_timeout(remaining):
                    actual = self._write_vout(profile.channel, next_v, deadline=deadline,
                                              cancel=cancel, stopping=stopping)
            except (ConfirmedRampError, RunInterrupted):
                raise
            except Exception as exc:
                self._fault_and_close(exc, "MOKE ramp transport failed")
            if abs(actual - target_v) <= 10 / 32767:
                if not profile.simulation and settling_s:
                    wake = time.monotonic() + settling_s
                    if wake > deadline:
                        raise TimeoutError("MOKE ramp deadline cannot accommodate settling time.")
                    while time.monotonic() < wake:
                        if self._stop_requested.wait(min(0.02, max(0, wake - time.monotonic()))) or (cancel is not None and cancel.is_set()):
                            raise RunInterrupted("MOKE voltage settling was stopped.")
                    # The hold is a time allowance, not evidence of current or field stability.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("MOKE settling exceeded its qualified deadline.")
                    with self.io_timeout(remaining):
                        confirmed = self._read_vouts_from_transport()[profile.channel]
                    if abs(confirmed - actual) > 0.001:
                        raise ConfirmedRampError("MOKE DAC changed during settling time.")
                    actual = confirmed
                return actual

    def stop_vout(self) -> MokeVoltageResult:
        """Qualified ramp to zero, permitted outside the working min/max."""
        self.interrupt_voltage()
        self._armed = False
        self._voltage_plan = None
        with self._lock:
            profile = self.control_profile
            if profile is None:
                raise SafetyViolation("MOKE DAC shutdown is not qualified.")
            try:
                actual = self._ramp(profile.safe_v, cancel=None, stopping=True)
                self._safe_target_confirmed = abs(actual - profile.safe_v) <= 10 / 32767
                self._output_changed = not self._safe_target_confirmed
                self._state = DeviceState.UNKNOWN  # Kepco power/current are not monitored.
                return MokeVoltageResult(profile.channel, profile.safe_v, profile.safe_v,
                                         actual, profile.fingerprint, self._safe_target_confirmed)
            except Exception as exc:
                self._fault_and_close(exc, "MOKE DAC shutdown failed")

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
        self._armed = False
        self._voltage_plan = None
        self._safe_target_confirmed = False
        try:
            self._transport.close()
        except Exception:
            pass
        self._connected = False
        self._identity = None
        self._capabilities = None
        self._state = DeviceState.UNKNOWN if self._output_changed else DeviceState.FAULT
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
