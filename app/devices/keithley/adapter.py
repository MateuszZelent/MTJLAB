"""Whitelisted TSP adapter for a dual-channel Keithley 2600-family SMU."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
import time
from typing import Literal

from app.devices.base import (
    DeviceAdapter,
    InstrumentSession,
    OutputInterlock,
    SessionFactory,
    parse_identity,
    validate_identity,
)
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.safety.keithley import KeithleySourceRequest, validate_keithley_measurement, validate_keithley_source
from app.settings.models import KeithleyChannelSettings, KeithleySettings, StationSettings


@dataclass(frozen=True, slots=True)
class KeithleyMeasurement:
    channel: Literal["A", "B"]
    voltage_v: float
    current_a: float
    power_w: float
    compliance_detected: bool = False
    compliance_stop_required: bool = False


@dataclass(frozen=True, slots=True)
class KeithleyRampRequest:
    channel: Literal["A", "B"]
    target_si: float
    max_step_si: float
    settle_time_s: float
    deadline_s: float = 10.0

    def __post_init__(self) -> None:
        values = (self.target_si, self.max_step_si, self.settle_time_s, self.deadline_s)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Keithley ramp values must be finite.")
        if self.max_step_si <= 0 or self.settle_time_s < 0 or self.deadline_s <= 0:
            raise ValueError("Ramp step/deadline must be positive and settling time non-negative.")


@dataclass(frozen=True, slots=True)
class KeithleyRampResult:
    channel: Literal["A", "B"]
    start_si: float
    target_si: float
    levels_si: tuple[float, ...]
    final_measurement: KeithleyMeasurement


def build_keithley_ramp_levels(
    start_si: float,
    target_si: float,
    max_step_si: float,
    *,
    max_points: int,
) -> tuple[float, ...]:
    """Return finite inclusive target levels with no step above ``max_step_si``."""

    if not all(math.isfinite(value) for value in (start_si, target_si, max_step_si)):
        raise SafetyViolation("Keithley ramp boundaries and step must be finite.")
    if max_step_si <= 0 or max_points < 1:
        raise SafetyViolation("Keithley ramp step and point limit must be positive.")
    steps = max(1, math.ceil(abs(target_si - start_si) / max_step_si))
    if steps > max_points:
        raise SafetyViolation(
            f"Keithley ramp requires {steps} points; approved maximum is {max_points}."
        )
    delta = target_si - start_si
    return tuple(target_si if index == steps else start_si + delta * index / steps for index in range(1, steps + 1))


class KeithleyAdapter(DeviceAdapter):
    """Safe subset of TSP; raw Lua and dynamic namespace objects are never exposed."""

    def __init__(self, station: StationSettings, *, session_factory: SessionFactory | None = None) -> None:
        super().__init__()
        self._station = station
        self._settings: KeithleySettings = station.keithley
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None
        self._last_request: dict[str, KeithleySourceRequest] = {}
        self._armed_until: dict[str, float] = {}
        self._output_states: dict[Literal["A", "B"], bool] = {"A": False, "B": False}

    def _interlock(self) -> OutputInterlock:
        return OutputInterlock(
            profile_approved=self._station.profile.state == "approved",
            profile_locks_outputs=True,
        )

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Keithley is not connected.")
        return self._session

    @staticmethod
    def _smu(channel: Literal["A", "B"]) -> str:
        return "smua" if channel == "A" else "smub"

    def _channel_settings(self, channel: Literal["A", "B"]) -> KeithleyChannelSettings:
        return self._settings.safety.channels[channel]

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            return self._identity_or_raise()
        if not self._settings.enabled:
            raise SafetyViolation("Keithley is disabled in the station profile.")
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("No Keithley VISA resource is configured in settings.yml.")
        timeout = int(parse_quantity(self._settings.connection.timeout, DIMENSION_TIME).si_value * 1000)
        session = self._factory.open(resource, self._settings.connection.visa_backend, timeout)
        try:
            if self._settings.connection.read_termination is not None:
                session.read_termination = self._settings.connection.read_termination
            if self._settings.connection.write_termination is not None:
                session.write_termination = self._settings.connection.write_termination
            identity = parse_identity(resource, session.query("*IDN?"))
            validate_identity(
                identity,
                vendor_contains=self._settings.identity.expected_vendor_contains,
                expected_models=self._settings.identity.expected_models,
                expected_serial=self._settings.identity.expected_serial,
                require_serial_match=self._settings.identity.require_serial_match,
            )
            self._session = session
            self._identity = identity
            self._capabilities = DeviceCapabilities(
                device_name="keithley",
                model=identity.model or "2600",
                firmware=identity.firmware,
                features=frozenset(
                    {"source_current", "source_voltage", "measure_iv", "ramp_to_zero", "ramp_to_level"}
                ),
            )
            self._state = DeviceState.VERIFIED
            if self._settings.safety.outputs_off_on_connect:
                self._write_all_outputs_off()
                states = self._read_output_states()
                if any(states.values()):
                    raise DeviceError("Keithley did not confirm OUTPUT OFF after connection.")
            else:
                self._read_output_states()
            self._update_aggregate_output_state()
            self._clear_errors()
            return identity
        except Exception:
            try:
                session.close()
            finally:
                self._session = None
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED
            raise

    def _identity_or_raise(self) -> DeviceIdentity:
        if self._identity is None:
            raise ConnectionError("Keithley has a session without a verified identity.")
        return self._identity

    def disconnect(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            if self._settings.safety.outputs_off_on_disconnect:
                try:
                    session.write("smua.source.output = smua.OUTPUT_OFF")
                    session.write("smub.source.output = smub.OUTPUT_OFF")
                except Exception:
                    pass
            try:
                session.close()
            finally:
                self._identity = None
                self._capabilities = None
                self._last_request.clear()
                self._armed_until.clear()
                self._output_states = {"A": False, "B": False}
                self._state = DeviceState.DISCONNECTED

    def _write_all_outputs_off(self) -> None:
        session = self._require_session()
        session.write("smua.source.output = smua.OUTPUT_OFF")
        session.write("smub.source.output = smub.OUTPUT_OFF")

    def emergency_off(self) -> None:
        if self._session is None:
            return
        try:
            self._write_all_outputs_off()
            states = self._read_output_states()
            if any(states.values()):
                raise DeviceError("Keithley did not confirm OUTPUT OFF during E-STOP.")
        except Exception:
            self._state = DeviceState.UNKNOWN
        else:
            self._armed_until.clear()
            self._output_states = {"A": False, "B": False}
            self._state = DeviceState.OUTPUT_OFF

    def _clear_errors(self) -> None:
        self._require_session().write("errorqueue.clear()")

    def read_errors(self, limit: int = 20) -> list[str]:
        session = self._require_session()
        errors: list[str] = []
        for _ in range(limit):
            try:
                count = int(float(session.query("print(errorqueue.count)")))
            except (TypeError, ValueError) as exc:
                raise DeviceError("Keithley returned an invalid errorqueue.count value.") from exc
            if count <= 0:
                return errors
            errors.append(session.query("print(errorqueue.next())"))
        errors.append("Keithley error queue did not drain.")
        return errors

    def _check_errors(self) -> None:
        errors = self.read_errors()
        if errors:
            raise DeviceError("Keithley reported an error: " + "; ".join(errors))

    def configure_source(self, request: KeithleySourceRequest) -> None:
        """Set function, range-safe level and compliance while output is guaranteed OFF."""

        if request.channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        channel = self._channel_settings(request.channel)
        validate_keithley_source(channel, request)
        smu = self._smu(request.channel)
        session = self._require_session()
        session.write(f"{smu}.source.output = {smu}.OUTPUT_OFF")
        self._output_states[request.channel] = False
        if request.mode == "measure_only":
            self._configure_measurement_ranges_and_sense(session, smu, request)
            session.write(f"{smu}.measure.nplc = {request.nplc:.12g}")
            self._check_errors()
            self._last_request[request.channel] = request
            self._update_aggregate_output_state()
            return
        if request.mode == "current":
            session.write(f"{smu}.source.func = {smu}.OUTPUT_DCAMPS")
            session.write(f"{smu}.source.limitv = {request.compliance_si:.12g}")
            self._configure_ranges_and_sense(session, smu, request)
            session.write(f"{smu}.source.leveli = {request.level_si:.12g}")
        else:
            session.write(f"{smu}.source.func = {smu}.OUTPUT_DCVOLTS")
            session.write(f"{smu}.source.limiti = {request.compliance_si:.12g}")
            self._configure_ranges_and_sense(session, smu, request)
            session.write(f"{smu}.source.levelv = {request.level_si:.12g}")
        session.write(f"{smu}.measure.nplc = {request.nplc:.12g}")
        self._check_errors()
        self._last_request[request.channel] = request
        self._update_aggregate_output_state()

    @staticmethod
    def _configure_ranges_and_sense(
        session: InstrumentSession, smu: str, request: KeithleySourceRequest
    ) -> None:
        """Write only whitelisted range/sense settings with source output OFF."""

        source_suffix = "i" if request.mode == "current" else "v"
        commands = [
            f"{smu}.source.autorange{source_suffix} = {smu}.{'AUTORANGE_ON' if request.source_autorange else 'AUTORANGE_OFF'}",
            *KeithleyAdapter._measurement_range_and_sense_commands(smu, request),
        ]
        if request.source_range_si is not None:
            commands.append(f"{smu}.source.range{source_suffix} = {request.source_range_si:.12g}")
        for command in commands:
            session.write(command)

    def _output_is_enabled(self, channel: Literal["A", "B"]) -> bool:
        smu = self._smu(channel)
        response = self._require_session().query(f"print({smu}.source.output)").strip().upper()
        if response in {"ON", "OUTPUT_ON"}:
            return True
        if response in {"OFF", "OUTPUT_OFF"}:
            return False
        try:
            value = float(response)
        except ValueError as exc:
            raise DeviceError("Keithley returned an invalid source.output state.") from exc
        if value == 1:
            return True
        if value == 0:
            return False
        raise DeviceError(f"Keithley returned unknown source.output state {response!r}.")

    def _read_output_states(self) -> dict[Literal["A", "B"], bool]:
        states = {channel: self._output_is_enabled(channel) for channel in ("A", "B")}
        self._output_states.update(states)
        return states

    def _update_aggregate_output_state(self) -> None:
        self._state = DeviceState.OUTPUT_ON if any(self._output_states.values()) else DeviceState.OUTPUT_OFF

    @staticmethod
    def _configure_measurement_ranges_and_sense(
        session: InstrumentSession, smu: str, request: KeithleySourceRequest
    ) -> None:
        for command in KeithleyAdapter._measurement_range_and_sense_commands(smu, request):
            session.write(command)

    @staticmethod
    def _measurement_range_and_sense_commands(smu: str, request: KeithleySourceRequest) -> list[str]:
        commands = [
            f"{smu}.measure.autorangev = {smu}.{'AUTORANGE_ON' if request.measure_voltage_autorange else 'AUTORANGE_OFF'}",
            f"{smu}.measure.autorangei = {smu}.{'AUTORANGE_ON' if request.measure_current_autorange else 'AUTORANGE_OFF'}",
            f"{smu}.sense = {smu}.{'SENSE_2WIRE' if request.sense_mode == '2wire' else 'SENSE_4WIRE'}",
        ]
        if request.measure_voltage_range_si is not None:
            commands.append(f"{smu}.measure.rangev = {request.measure_voltage_range_si:.12g}")
        if request.measure_current_range_si is not None:
            commands.append(f"{smu}.measure.rangei = {request.measure_current_range_si:.12g}")
        return commands

    def set_output(self, channel: Literal["A", "B"], enabled: bool) -> None:
        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        settings = self._channel_settings(channel)
        if enabled:
            request = self._last_request.get(channel)
            if request is None:
                raise SafetyViolation("Configure a safe Keithley source before enabling OUTPUT.")
            validate_keithley_source(settings, request)
            self._assert_armed(channel, settings.enabled)
        smu = self._smu(channel)
        self._require_session().write(
            f"{smu}.source.output = {smu}.OUTPUT_ON" if enabled else f"{smu}.source.output = {smu}.OUTPUT_OFF"
        )
        self._check_errors()
        active = self._output_is_enabled(channel)
        if active != enabled:
            raise DeviceError("Keithley did not confirm the requested output state.")
        self._output_states[channel] = active
        self._update_aggregate_output_state()
        if not enabled:
            self._armed_until.pop(channel, None)

    def arm_output(self, channel: Literal["A", "B"], *, ttl_s: float = 30.0) -> float:
        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        settings = self._channel_settings(channel)
        self._interlock().assert_can_enable(
            device_name=f"Keithley CH{channel}",
            device_allows_output=self._settings.safety.allow_output_enable and settings.enabled,
        )
        request = self._last_request.get(channel)
        if request is None or request.mode == "measure_only":
            raise SafetyViolation("Configure a safe Keithley source first.")
        validate_keithley_source(settings, request)
        if ttl_s <= 0 or ttl_s > 120:
            raise SafetyViolation("Keithley ARM duration must be in the range (0, 120] s.")
        expires = time.monotonic() + ttl_s
        self._armed_until[channel] = expires
        return expires

    def _assert_armed(self, channel: Literal["A", "B"], channel_enabled: bool) -> None:
        self._interlock().assert_can_enable(
            device_name=f"Keithley CH{channel}",
            device_allows_output=self._settings.safety.allow_output_enable and channel_enabled,
        )
        expiry = self._armed_until.pop(channel, None)
        if expiry is None:
            raise SafetyViolation("ARM the Keithley channel first.")
        if time.monotonic() > expiry:
            raise SafetyViolation("Keithley ARM window expired; ARM the channel again.")

    def measure(self, channel: Literal["A", "B"]) -> KeithleyMeasurement:
        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        smu = self._smu(channel)
        session = self._require_session()
        try:
            # One TSP acquisition keeps I and V from the same measurement
            # instant. Keithley returns current first, then voltage.
            response = session.query(f"print({smu}.measure.iv())").strip()
            values = [item for item in re.split(r"[,;\t\s]+", response) if item]
            if len(values) != 2:
                raise ValueError(f"expected two IV values, received {len(values)}")
            current, voltage = (float(item) for item in values)
            if not (math.isfinite(current) and math.isfinite(voltage)):
                raise ValueError("non-finite IV result")
        except (TypeError, ValueError) as exc:
            raise DeviceError("Keithley returned an invalid I/V measurement.") from exc
        request = self._last_request.get(channel)
        compliance_detected = self._at_compliance_limit(request, voltage=voltage, current=current)
        stop_required = compliance_detected and self._settings.safety.stop_on_compliance
        if stop_required:
            self.emergency_off()
            self._state = DeviceState.COMPLIANCE
        result = KeithleyMeasurement(channel, voltage, current, voltage * current, compliance_detected, stop_required)
        try:
            validate_keithley_measurement(
                self._channel_settings(channel),
                voltage,
                current,
                request.dut_envelope if request is not None else None,
            )
        except SafetyViolation:
            # A manual read must be as fail-safe as a recipe checkpoint: trip
            # limits are laboratory boundaries, so both outputs are disabled.
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise
        self._check_errors()
        return result

    @staticmethod
    def _at_compliance_limit(
        request: KeithleySourceRequest | None, *, voltage: float, current: float
    ) -> bool:
        """Conservatively infer compliance from a measured limiting quantity.

        The exact status attribute varies across 2600 firmware.  Until that
        model-specific probe has been qualified, reaching the programmed limit
        itself is sufficient evidence to enter the safe compliance policy.
        """

        if request is None or request.mode == "measure_only" or request.compliance_si <= 0:
            return False
        limiting_value = voltage if request.mode == "current" else current
        tolerance = max(abs(request.compliance_si) * 1e-4, 1e-9)
        return abs(limiting_value) >= abs(request.compliance_si) - tolerance

    def ramp_to_zero(self, channel: Literal["A", "B"], *, deadline_s: float = 10.0) -> None:
        request = self._last_request.get(channel)
        if request is None or request.mode == "measure_only":
            self.set_output(channel, False)
            return
        limits = self._channel_settings(channel).lab_limits
        step = (
            parse_quantity(limits.ramp_current_step_max, "current").si_value
            if request.mode == "current"
            else parse_quantity(limits.ramp_voltage_step_max, "voltage").si_value
        )
        level = request.level_si
        started = time.monotonic()
        smu = self._smu(channel)
        session = self._require_session()
        while abs(level) > step:
            if time.monotonic() - started > deadline_s:
                self.emergency_off()
                raise DeviceError("Keithley ramp-to-zero timed out.")
            level -= step if level > 0 else -step
            field = "leveli" if request.mode == "current" else "levelv"
            session.write(f"{smu}.source.{field} = {level:.12g}")
            if request.settle_time_s:
                time.sleep(request.settle_time_s)
        field = "leveli" if request.mode == "current" else "levelv"
        session.write(f"{smu}.source.{field} = 0")
        self.set_output(channel, False)

    def ramp_to_level(self, request: KeithleyRampRequest) -> KeithleyRampResult:
        """Ramp an already active source without ever enabling an output.

        The actual starting level is queried from the instrument. Every point
        is checked against the approved source/DUT envelope and followed by an
        atomic I/V measurement. Any transport, compliance or limit failure
        attempts to turn both SMU outputs off.
        """

        channel = request.channel
        current_request = self._last_request.get(channel)
        if current_request is None or current_request.mode == "measure_only":
            raise SafetyViolation("Configure a Current or Voltage source before using a ramp.")
        try:
            output_enabled = self._output_is_enabled(channel)
        except Exception:
            self.emergency_off()
            raise
        if not output_enabled:
            self._output_states[channel] = False
            self._update_aggregate_output_state()
            raise SafetyViolation("Manual ramp requires the selected Keithley OUTPUT to be ON.")
        channel_settings = self._channel_settings(channel)
        limits = channel_settings.lab_limits
        dimension = "current" if current_request.mode == "current" else "voltage"
        approved_step = parse_quantity(
            limits.ramp_current_step_max
            if current_request.mode == "current"
            else limits.ramp_voltage_step_max,
            dimension,
        ).si_value
        step_tolerance = max(abs(approved_step), 1.0) * 1e-12
        if request.max_step_si > approved_step + step_tolerance:
            raise SafetyViolation(
                f"Requested ramp step {request.max_step_si:.12g} SI exceeds approved "
                f"maximum {approved_step:.12g} SI."
            )
        if limits.point_settle_time is not None:
            minimum_settle = parse_quantity(limits.point_settle_time.min, DIMENSION_TIME).si_value
            maximum_settle = parse_quantity(limits.point_settle_time.max, DIMENSION_TIME).si_value
            if not minimum_settle <= request.settle_time_s <= maximum_settle:
                raise SafetyViolation(
                    f"Ramp settling time must be within {minimum_settle:.12g}.."
                    f"{maximum_settle:.12g} s."
                )
        target_request = replace(current_request, level_si=request.target_si)
        validate_keithley_source(channel_settings, target_request)
        smu = self._smu(channel)
        field = "leveli" if current_request.mode == "current" else "levelv"
        session = self._require_session()
        try:
            response = session.query(f"print({smu}.source.{field})")
            start_si = float(response.strip())
        except (TypeError, ValueError) as exc:
            self.emergency_off()
            raise DeviceError("Keithley returned an invalid source level before ramping.") from exc
        if not math.isfinite(start_si):
            self.emergency_off()
            raise DeviceError("Keithley returned a non-finite source level before ramping.")
        start_request = replace(current_request, level_si=start_si)
        try:
            validate_keithley_source(channel_settings, start_request)
        except Exception:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise
        levels = build_keithley_ramp_levels(
            start_si,
            request.target_si,
            request.max_step_si,
            max_points=limits.sweep_points_max,
        )
        predicted_s = len(levels) * request.settle_time_s
        if predicted_s > request.deadline_s:
            raise SafetyViolation(
                f"Ramp dwell alone requires {predicted_s:.3g} s, exceeding the "
                f"{request.deadline_s:.3g} s deadline."
            )
        started = time.monotonic()
        final_measurement: KeithleyMeasurement | None = None
        try:
            for level in levels:
                if time.monotonic() - started > request.deadline_s:
                    raise DeviceError("Keithley manual ramp exceeded its deadline.")
                step_request = replace(current_request, level_si=level)
                validate_keithley_source(channel_settings, step_request)
                session.write(f"{smu}.source.{field} = {level:.12g}")
                self._last_request[channel] = step_request
                if request.settle_time_s:
                    time.sleep(request.settle_time_s)
                if time.monotonic() - started > request.deadline_s:
                    raise DeviceError("Keithley manual ramp exceeded its deadline.")
                final_measurement = self.measure(channel)
                if final_measurement.compliance_stop_required:
                    raise SafetyViolation("Keithley reached compliance during the manual ramp.")
            if final_measurement is None:
                raise DeviceError("Keithley ramp produced no measurement checkpoint.")
            if not self._output_is_enabled(channel):
                raise DeviceError("Keithley OUTPUT switched off unexpectedly during the ramp.")
        except Exception:
            self.emergency_off()
            if self._state not in {DeviceState.UNKNOWN, DeviceState.COMPLIANCE}:
                self._state = DeviceState.FAULT
            raise
        self._last_request[channel] = target_request
        self._output_states[channel] = True
        self._update_aggregate_output_state()
        return KeithleyRampResult(channel, start_si, request.target_si, levels, final_measurement)
