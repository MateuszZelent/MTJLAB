"""Safe high-level VISA adapter for a Lake Shore Model 475 gaussmeter."""

from __future__ import annotations

import math
from typing import Protocol

from app.devices.base import DeviceAdapter, InstrumentSession, SessionFactory, parse_identity, validate_identity
from app.devices.lakeshore_gaussmeter.models import FieldReading, GaussmeterConfig, Model425Config
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState


class _Model425Driver(Protocol):
    def connect_tcp(self, ip_address: str, tcp_port: int, timeout: float) -> None: ...
    def connect_usb(self, **kwargs: object) -> None: ...
    def disconnect_tcp(self) -> None: ...
    def disconnect_usb(self) -> None: ...
    def query(self, query_string: str) -> str: ...


class LakeShore475Adapter(DeviceAdapter):
    """Read-only, fail-closed adapter for the Model 475 field meter.

    It exposes measurement and configuration queries only.  The device has no
    energy-output API, therefore ``emergency_off`` simply leaves the adapter in
    a safe verified state.  The command spelling follows the Model 475 manual:
    ``RDGFIELD?`` returns the current field reading.
    """

    def __init__(self, config: GaussmeterConfig, *, session_factory: SessionFactory | None = None) -> None:
        super().__init__()
        self._config = config
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Lake Shore gaussmeter is not connected.")
        return self._session

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            return self._identity_or_raise()
        session = self._factory.open(self._config.resource, self._config.visa_backend, self._config.timeout_ms)
        try:
            session.read_termination = self._config.read_termination
            session.write_termination = self._config.write_termination
            identity = parse_identity(self._config.resource, session.query("*IDN?"))
            validate_identity(
                identity,
                vendor_contains=self._config.expected_vendor_contains,
                expected_models=self._config.expected_models,
                expected_serial=self._config.expected_serial,
                require_serial_match=self._config.require_serial_match,
            )
            session.write("*CLS")
            self._session = session
            self._identity = identity
            self._state = DeviceState.VERIFIED
            self._capabilities = DeviceCapabilities(
                device_name="lakeshore_gaussmeter",
                model=identity.model or "475",
                firmware=identity.firmware,
                features=frozenset({"field_reading", "read_only"}),
            )
            return identity
        except Exception:
            try:
                session.close()
            finally:
                self._state = DeviceState.DISCONNECTED
                self._identity = None
                self._capabilities = None
            raise

    def disconnect(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            finally:
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        """The gaussmeter has no controllable output; preserve a safe state."""

        if self._session is not None:
            self._state = DeviceState.VERIFIED

    def read_field(self) -> FieldReading:
        """Acquire one numeric field reading without changing meter settings."""

        response = self._require_session().query("RDGFIELD?").strip()
        try:
            value = float(response.split(",", 1)[0].strip())
        except (TypeError, ValueError) as exc:
            self._state = DeviceState.FAULT
            raise DeviceError(f"Lake Shore returned an invalid RDGFIELD? value: {response!r}") from exc
        if not math.isfinite(value):
            self._state = DeviceState.FAULT
            raise DeviceError("Lake Shore returned a non-finite field reading.")
        self._state = DeviceState.VERIFIED
        return FieldReading.now(value, self._config.field_unit)

    def read_unit_code(self) -> str:
        """Read the instrument unit selector without changing it."""

        return self._require_session().query("UNIT?").strip()

    def set_unit(self, _unit: str) -> None:
        """Explicitly reject configuration until unit mapping is HIL-qualified."""

        raise SafetyViolation(
            "Changing Lake Shore units is disabled until the Model 475 UNIT mapping is HIL-qualified."
        )


class LakeShore425Adapter(DeviceAdapter):
    """Read-only Model 425 adapter using Lake Shore's optional Python driver.

    The official driver is loaded lazily, so an installation that only uses the
    Model 475 VISA path does not gain a mandatory dependency.  The public
    driver API exposes ``Model425``, ``connect_tcp``, ``connect_usb`` and
    ``query``; no direct serial implementation is duplicated here.
    """

    def __init__(
        self,
        config: Model425Config,
        *,
        driver: _Model425Driver | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._driver = driver
        self._connected = False

    def _load_driver(self) -> _Model425Driver:
        if self._driver is not None:
            return self._driver
        try:
            from lakeshore.model_425 import Model425
        except ImportError as exc:
            raise ConnectionError(
                "Lake Shore Model 425 requires the optional 'lakeshore' package. "
                "Install it with: pip install lakeshore"
            ) from exc
        self._driver = Model425(timeout=self._config.timeout_s)
        return self._driver

    def connect(self) -> DeviceIdentity:
        if self._connected:
            return self._identity_or_raise()
        driver = self._load_driver()
        try:
            if self._config.connection == "tcp":
                driver.connect_tcp(self._config.ip_address or "", self._config.tcp_port, self._config.timeout_s)
            else:
                driver.connect_usb(
                    serial_number=self._config.serial_number,
                    com_port=self._config.com_port,
                    timeout=self._config.timeout_s,
                )
            response = driver.query("*IDN?")
            identity = parse_identity(
                self._config.ip_address or self._config.com_port or self._config.serial_number or "Model425",
                response,
            )
            validate_identity(
                identity,
                vendor_contains=self._config.expected_vendor_contains,
                expected_models=self._config.expected_models,
                expected_serial=None,
                require_serial_match=False,
            )
        except Exception as exc:
            self._state = DeviceState.DISCONNECTED
            if isinstance(exc, ConnectionError):
                raise
            raise ConnectionError(f"Could not connect to Lake Shore Model 425: {exc}") from exc
        self._connected = True
        self._identity = identity
        self._capabilities = DeviceCapabilities(
            device_name="lakeshore_gaussmeter",
            model=identity.model or "425",
            firmware=identity.firmware,
            features=frozenset({"field_reading", "read_only", "official_driver"}),
        )
        self._state = DeviceState.VERIFIED
        return identity

    def disconnect(self) -> None:
        if self._connected and self._driver is not None:
            try:
                if self._config.connection == "tcp":
                    self._driver.disconnect_tcp()
                else:
                    self._driver.disconnect_usb()
            finally:
                self._connected = False
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        if self._connected:
            self._state = DeviceState.VERIFIED

    def read_field(self) -> FieldReading:
        if not self._connected or self._driver is None:
            raise ConnectionError("Lake Shore Model 425 is not connected.")
        try:
            value = float(self._driver.query("FIELD?").strip().split(",", 1)[0])
        except Exception as exc:
            self._state = DeviceState.FAULT
            raise DeviceError(f"Lake Shore Model 425 field acquisition failed: {exc}") from exc
        if not math.isfinite(value):
            self._state = DeviceState.FAULT
            raise DeviceError("Lake Shore Model 425 returned a non-finite field reading.")
        self._state = DeviceState.VERIFIED
        return FieldReading.now(value, self._config.field_unit)
