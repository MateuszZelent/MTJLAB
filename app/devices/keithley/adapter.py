"""Whitelisted TSP adapter for a dual-channel Keithley 2600-family SMU."""

from __future__ import annotations

from dataclasses import dataclass
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
from app.domain.models import DeviceIdentity, DeviceState
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.safety.keithley import KeithleySourceRequest, validate_keithley_measurement, validate_keithley_source
from app.settings.models import KeithleyChannelSettings, KeithleySettings, StationSettings


@dataclass(frozen=True, slots=True)
class KeithleyMeasurement:
    channel: Literal["A", "B"]
    voltage_v: float
    current_a: float
    power_w: float


class KeithleyAdapter(DeviceAdapter):
    """Safe subset of TSP; raw Lua and dynamic namespace objects are never exposed."""

    def __init__(self, station: StationSettings, *, session_factory: SessionFactory | None = None) -> None:
        super().__init__()
        self._station = station
        self._settings: KeithleySettings = station.keithley
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None
        self._last_request: dict[str, KeithleySourceRequest] = {}

    def _interlock(self) -> OutputInterlock:
        return OutputInterlock(
            profile_approved=self._station.profile.state == "approved",
            profile_locks_outputs=True,
        )

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Keithley nie jest połączony.")
        return self._session

    @staticmethod
    def _smu(channel: Literal["A", "B"]) -> str:
        return "smua" if channel == "A" else "smub"

    def _channel_settings(self, channel: Literal["A", "B"]) -> KeithleyChannelSettings:
        return self._settings.safety.channels[channel]

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            return self._identity_or_raise()
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("Brak zasobu VISA dla Keithley w settings.yml.")
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
            self._state = DeviceState.VERIFIED
            self._clear_errors()
            if self._settings.safety.outputs_off_on_connect:
                self._write_all_outputs_off()
            return identity
        except Exception:
            try:
                session.close()
            finally:
                self._session = None
                self._identity = None
                self._state = DeviceState.DISCONNECTED
            raise

    def _identity_or_raise(self) -> DeviceIdentity:
        if self._identity is None:
            raise ConnectionError("Keithley ma sesję bez zweryfikowanej tożsamości.")
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
        finally:
            self._state = DeviceState.OUTPUT_OFF

    def _clear_errors(self) -> None:
        self._require_session().write("errorqueue.clear()")

    def read_errors(self, limit: int = 20) -> list[str]:
        session = self._require_session()
        errors: list[str] = []
        for _ in range(limit):
            count = int(float(session.query("print(errorqueue.count)")))
            if count <= 0:
                return errors
            errors.append(session.query("print(errorqueue.next())"))
        errors.append("Kolejka błędów Keithley nie została opróżniona.")
        return errors

    def _check_errors(self) -> None:
        errors = self.read_errors()
        if errors:
            raise DeviceError("Keithley zgłosił błąd: " + "; ".join(errors))

    def configure_source(self, request: KeithleySourceRequest) -> None:
        """Set function, range-safe level and compliance while output is guaranteed OFF."""

        channel = self._channel_settings(request.channel)
        validate_keithley_source(channel, request)
        smu = self._smu(request.channel)
        session = self._require_session()
        session.write(f"{smu}.source.output = {smu}.OUTPUT_OFF")
        if request.mode == "measure_only":
            self._last_request[request.channel] = request
            self._state = DeviceState.OUTPUT_OFF
            return
        if request.mode == "current":
            session.write(f"{smu}.source.func = {smu}.OUTPUT_DCAMPS")
            session.write(f"{smu}.source.limitv = {request.compliance_si:.12g}")
            session.write(f"{smu}.source.leveli = {request.level_si:.12g}")
        else:
            session.write(f"{smu}.source.func = {smu}.OUTPUT_DCVOLTS")
            session.write(f"{smu}.source.limiti = {request.compliance_si:.12g}")
            session.write(f"{smu}.source.levelv = {request.level_si:.12g}")
        session.write(f"{smu}.measure.nplc = {request.nplc:.12g}")
        self._check_errors()
        self._last_request[request.channel] = request
        self._state = DeviceState.OUTPUT_OFF

    def set_output(self, channel: Literal["A", "B"], enabled: bool) -> None:
        settings = self._channel_settings(channel)
        if enabled:
            self._interlock().assert_can_enable(
                device_name=f"Keithley CH{channel}",
                device_allows_output=self._settings.safety.allow_output_enable and settings.enabled,
            )
            request = self._last_request.get(channel)
            if request is None or request.mode == "measure_only":
                raise SafetyViolation("Najpierw skonfiguruj bezpieczne źródło Keithley.")
        smu = self._smu(channel)
        self._require_session().write(
            f"{smu}.source.output = {smu}.OUTPUT_ON" if enabled else f"{smu}.source.output = {smu}.OUTPUT_OFF"
        )
        self._check_errors()
        self._state = DeviceState.OUTPUT_ON if enabled else DeviceState.OUTPUT_OFF

    def measure(self, channel: Literal["A", "B"]) -> KeithleyMeasurement:
        smu = self._smu(channel)
        session = self._require_session()
        voltage = float(session.query(f"print({smu}.measure.v())"))
        current = float(session.query(f"print({smu}.measure.i())"))
        result = KeithleyMeasurement(channel, voltage, current, voltage * current)
        validate_keithley_measurement(self._channel_settings(channel), voltage, current)
        self._check_errors()
        return result

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
                raise DeviceError("Timeout podczas rampy Keithley do zera.")
            level -= step if level > 0 else -step
            field = "leveli" if request.mode == "current" else "levelv"
            session.write(f"{smu}.source.{field} = {level:.12g}")
            if request.settle_time_s:
                time.sleep(request.settle_time_s)
        field = "leveli" if request.mode == "current" else "levelv"
        session.write(f"{smu}.source.{field} = 0")
        self.set_output(channel, False)
