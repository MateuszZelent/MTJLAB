"""Fail-closed, read-only Model 475 adapter using Lake Shore's public bridge."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Protocol

from pyvisa.constants import Parity, StopBits

from app.devices.base import DeviceAdapter, InstrumentSession, SessionFactory, parse_identity
from app.devices.lakeshore_475.models import (
    FieldUnit,
    GaussmeterConfig,
    GaussmeterReading,
    GaussmeterSnapshot,
    MeasurementMode,
    field_unit_from_code,
    measurement_mode_from_code,
    parse_measurement_mode_response,
)
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState


_ALLOWED_QUERIES = frozenset({"*IDN?", "UNIT?", "RDGMODE?", "RANGE?", "AUTO?", "TYPE?", "RDGFIELD?", "RDGFRQ?", "RDGPEAK?"})


class _OfficialModelFactory(Protocol):
    def __call__(self, connection: object) -> object: ...


class _ReadOnlyConnection:
    """Small public-API connection for ``Model425(connection=...)``."""

    def __init__(self, session: InstrumentSession) -> None:
        self._session = session
        self._last_command_at: float | None = None

    def _wait_for_slot(self) -> None:
        if self._last_command_at is not None:
            remaining = 0.05 - (time.monotonic() - self._last_command_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_command_at = time.monotonic()

    def query(self, command: str) -> str:
        normalized = command.strip().upper()
        if normalized not in _ALLOWED_QUERIES:
            raise SafetyViolation(f"Lake Shore read-only proxy rejected query {command!r}.")
        self._wait_for_slot()
        return self._session.query(normalized)

    def write(self, command: str) -> None:
        raise SafetyViolation(f"Lake Shore read-only proxy rejected write {command!r}.")

    def clear(self) -> None:
        """Clear only a local transport buffer when the backend exposes it."""

        clear = getattr(self._session, "clear", None)
        if callable(clear):
            clear()


def _default_official_model_factory(connection: object) -> object:
    try:
        from lakeshore.model_425 import Model425
    except ImportError as exc:
        raise ConnectionError("Lake Shore support requires lakeshore==1.10.0.") from exc
    return Model425(connection=connection)


class LakeShore475Adapter(DeviceAdapter):
    """The sole public Lake Shore adapter, restricted to Model 475 queries."""

    def __init__(
        self,
        config: GaussmeterConfig,
        *,
        session_factory: SessionFactory | None = None,
        official_model_factory: _OfficialModelFactory | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._factory = session_factory or PyVisaSessionFactory()
        self._official_model_factory = official_model_factory or _default_official_model_factory
        self._session: InstrumentSession | None = None
        self._connection: _ReadOnlyConnection | None = None
        self._official_model: object | None = None

    def _require_connection(self) -> _ReadOnlyConnection:
        if self._connection is None:
            raise ConnectionError("Lake Shore Model 475 is not connected.")
        return self._connection

    def _identity_or_raise(self) -> DeviceIdentity:
        if self._identity is None:
            raise ConnectionError(
                "Lake Shore Model 475 has a session without a verified identity."
            )
        return self._identity

    @staticmethod
    def _is_asrl(resource: str) -> bool:
        return resource.strip().upper().startswith("ASRL")

    def _configure_session(self, session: InstrumentSession) -> None:
        session.read_termination = "\r\n"
        session.write_termination = "\r\n"
        if self._is_asrl(self._config.resource):
            for name, value in (
                ("baud_rate", self._config.baud_rate),
                ("data_bits", 7),
                ("parity", Parity.odd),
                ("stop_bits", StopBits.one),
            ):
                try:
                    setattr(session, name, value)
                except (AttributeError, ValueError) as exc:
                    raise ConnectionError(f"Could not configure Lake Shore serial {name}: {exc}") from exc

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            return self._identity_or_raise()
        session = self._factory.open(self._config.resource, self._config.visa_backend, self._config.timeout_ms)
        try:
            self._configure_session(session)
            connection = _ReadOnlyConnection(session)
            official_model = self._official_model_factory(connection)
            identity = parse_identity(self._config.resource, connection.query("*IDN?"))
            idn = identity.idn.upper()
            if (identity.manufacturer or "").upper() not in {"LSCI", "LAKE SHORE"} or "MODEL475" not in idn:
                raise ConnectionError(f"Unexpected Lake Shore Model 475 identity: {identity.idn}")
            if self._config.require_serial_match and identity.serial != self._config.expected_serial:
                raise ConnectionError("Instrument serial number differs from the configured value.")
            self._session = session
            self._connection = connection
            self._official_model = official_model
            self.read_snapshot()
        except Exception:
            try:
                session.close()
            finally:
                self._session = None
                self._connection = None
                self._official_model = None
                self._state = DeviceState.DISCONNECTED
                self._identity = None
                self._capabilities = None
            raise
        self._identity = identity
        self._capabilities = DeviceCapabilities(device_name="lakeshore_gaussmeter", model="475", firmware=identity.firmware, features=frozenset({"field_reading", "dc", "rms", "peak", "read_only", "official_driver_bridge"}))
        self._state = DeviceState.VERIFIED
        return identity

    def disconnect(self) -> None:
        session, self._session = self._session, None
        self._connection = None
        self._official_model = None
        if session is not None:
            try:
                session.close()
            finally:
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        if self._session is not None:
            self._state = DeviceState.VERIFIED

    @staticmethod
    def _numeric(response: str, command: str) -> float:
        try:
            value = float(response.strip())
        except (TypeError, ValueError) as exc:
            raise DeviceError(f"Lake Shore returned invalid {command} response: {response!r}") from exc
        if not math.isfinite(value):
            raise DeviceError(f"Lake Shore returned non-finite {command} response.")
        return value

    @staticmethod
    def _tesla(value: float, unit: FieldUnit) -> float:
        if unit is FieldUnit.GAUSS:
            return value * 1e-4
        if unit is FieldUnit.TESLA:
            return value
        name = "Oersted" if unit is FieldUnit.OERSTED else "A/m"
        raise DeviceError(f"Lake Shore {name} readings cannot be safely converted to tesla.")

    def read_snapshot(self) -> GaussmeterSnapshot:
        connection = self._require_connection()
        try:
            mode_response = connection.query("RDGMODE?").strip()
            (
                mode_code,
                dc_resolution_code,
                rms_filter_mode_code,
                peak_mode_code,
                peak_display_code,
            ) = parse_measurement_mode_response(mode_response)
            unit_code = connection.query("UNIT?").strip()
            auto = connection.query("AUTO?").strip()
            if auto not in {"0", "1"}:
                raise ValueError(f"Unknown Lake Shore AUTO? code {auto!r}.")
            snapshot = GaussmeterSnapshot(
                mode_code=mode_code,
                mode=measurement_mode_from_code(mode_code),
                unit_code=unit_code,
                unit=field_unit_from_code(unit_code),
                range_code=connection.query("RANGE?").strip(),
                autorange_enabled=auto == "1",
                probe_type_code=connection.query("TYPE?").strip(),
                timestamp_utc=datetime.now(timezone.utc),
                dc_resolution_code=dc_resolution_code,
                rms_filter_mode_code=rms_filter_mode_code,
                peak_mode_code=peak_mode_code,
                peak_display_code=peak_display_code,
            )
        except (DeviceError, ValueError) as exc:
            self._state = DeviceState.FAULT
            raise DeviceError(f"Lake Shore configuration read failed: {exc}") from exc
        return snapshot

    def read_measurement(self) -> GaussmeterReading:
        connection = self._require_connection()
        try:
            for _attempt in range(2):
                snapshot = self.read_snapshot()
                if snapshot.mode in {MeasurementMode.DC, MeasurementMode.RMS}:
                    field_t = self._tesla(self._numeric(connection.query("RDGFIELD?"), "RDGFIELD?"), snapshot.unit)
                    frequency_hz = self._numeric(connection.query("RDGFRQ?"), "RDGFRQ?") if snapshot.mode is MeasurementMode.RMS else None
                    reading = GaussmeterReading.now(mode=snapshot.mode, unit=snapshot.unit, snapshot=snapshot, field_t=field_t, frequency_hz=frequency_hz)
                else:
                    values = [self._numeric(part, "RDGPEAK?") for part in connection.query("RDGPEAK?").split(",")]
                    if len(values) != 2:
                        raise DeviceError("Lake Shore RDGPEAK? must return negative and positive values.")
                    reading = GaussmeterReading.now(mode=snapshot.mode, unit=snapshot.unit, snapshot=snapshot, negative_peak_t=self._tesla(values[0], snapshot.unit), positive_peak_t=self._tesla(values[1], snapshot.unit))
                end_mode = parse_measurement_mode_response(
                    connection.query("RDGMODE?").strip()
                )[0]
                end_unit = connection.query("UNIT?").strip()
                if end_mode == snapshot.mode_code and end_unit == snapshot.unit_code:
                    self._state = DeviceState.VERIFIED
                    return reading
            raise DeviceError("Lake Shore unit or mode changed during both measurement attempts.")
        except DeviceError:
            self._state = DeviceState.FAULT
            raise


class UnavailableLakeShoreAdapter(DeviceAdapter):
    """Adapter returned for an incomplete disabled profile without opening I/O."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    def connect(self) -> DeviceIdentity:
        raise ConnectionError(self._reason)

    def disconnect(self) -> None:
        self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        return None
