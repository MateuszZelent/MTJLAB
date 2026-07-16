"""Safe Anritsu MS2830A spectrum and live-trace adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Iterable

from app.devices.base import DeviceAdapter, InstrumentSession, SessionFactory, parse_identity, validate_identity
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.safety.anritsu import (
    assert_anritsu_acquisition_allowed,
    validate_anritsu_spectrum,
    validate_anritsu_trace_name,
)
from app.settings.models import AnritsuSettings, StationSettings


@dataclass(frozen=True, slots=True)
class SpectrumConfig:
    start_hz: float
    stop_hz: float
    reference_level_dbm: float
    points: int
    trace: str = "TRAC1"


@dataclass(frozen=True, slots=True)
class SpectrumTrace:
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    acquired_at_utc: datetime
    trace_name: str


class AnritsuAdapter(DeviceAdapter):
    """Spectrum acquisition is explicit; RF generator output is never auto-enabled."""

    def __init__(self, station: StationSettings, *, session_factory: SessionFactory | None = None) -> None:
        super().__init__()
        self._station = station
        self._settings: AnritsuSettings = station.anritsu
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None
        self._live = False

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Anritsu nie jest połączony.")
        return self._session

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            if self._identity is None:
                raise ConnectionError("Anritsu ma sesję bez zweryfikowanej tożsamości.")
            return self._identity
        if not self._settings.enabled:
            raise SafetyViolation("Anritsu jest wyłączony w profilu stanowiska.")
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("Brak zasobu VISA dla Anritsu w settings.yml.")
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
                device_name="anritsu",
                model=identity.model or "MS2830A",
                firmware=identity.firmware,
                features=frozenset(
                    {"spectrum_trace", "live_trace"}
                    | ({"synchronized_single_sweep"} if self._single_sweep_supported else set())
                ),
            )
            self._state = DeviceState.VERIFIED
            return identity
        except Exception:
            session.close()
            self._state = DeviceState.DISCONNECTED
            raise

    def disconnect(self) -> None:
        session, self._session = self._session, None
        self._live = False
        if session is not None:
            try:
                session.close()
            finally:
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        """Abort acquisition.  RF generator state is not modified because it is separately gated."""

        if self._session is None:
            return
        try:
            self._session.write("ABORT")
        except Exception:
            self._live = False
            self._state = DeviceState.UNKNOWN
        else:
            self._live = False
            self._state = DeviceState.VERIFIED

    @property
    def _single_sweep_supported(self) -> bool:
        return self._settings.acquisition.single_sweep_mode == "standard_scpi_opc"

    def _assert_acquisition_allowed(self) -> None:
        assert_anritsu_acquisition_allowed(self._settings.safety)

    def configure_spectrum(self, config: SpectrumConfig) -> None:
        validate_anritsu_trace_name(config.trace)
        validate_anritsu_spectrum(
            self._settings.safety,
            start_hz=config.start_hz,
            stop_hz=config.stop_hz,
            reference_level_dbm=config.reference_level_dbm,
            points=config.points,
        )
        session = self._require_session()
        session.write("INST SPECT")
        session.write(f"FREQ:START {config.start_hz:.12g}HZ")
        session.write(f"FREQ:STOP {config.stop_hz:.12g}HZ")
        session.write(f"DISP:WIND:TRAC:Y:RLEV {config.reference_level_dbm:.12g}")
        session.write(f"SWE:POIN {config.points}")

    def start_live(self) -> None:
        """Enable application-side live refresh; current instrument sweep mode is preserved."""

        self._assert_acquisition_allowed()
        self._require_session().write("INST SPECT")
        self._live = True

    def stop_live(self) -> None:
        self._live = False

    @property
    def live(self) -> bool:
        return self._live

    def start_single_sweep(self) -> None:
        """Start one qualified SCPI sweep for a recipe checkpoint.

        This command family is intentionally unavailable until the current
        Anritsu firmware has been qualified in the station profile.  It is not
        used for the user-facing Live polling loop.
        """

        self._assert_acquisition_allowed()
        if not self._single_sweep_supported:
            raise SafetyViolation(
                "Receptura wymaga zweryfikowanego trybu Anritsu standard_scpi_opc; "
                "obecny profil dopuszcza jedynie Live/Fetch."
            )
        session = self._require_session()
        session.write("INST SPECT")
        session.write("INIT:CONT OFF")
        session.write("INIT:IMM")
        # DeviceState represents the connection/output safety state, not the
        # transient acquisition state; the analyser has no energy output here.
        self._state = DeviceState.VERIFIED

    def wait_complete(self, *, deadline_s: float | None = None) -> None:
        """Wait for the single sweep's `*OPC?` result with a hard deadline."""

        if not self._single_sweep_supported:
            raise SafetyViolation("Brak zweryfikowanego protokołu single sweep Anritsu.")
        timeout = deadline_s
        if timeout is None:
            timeout = parse_quantity(self._settings.acquisition.operation_complete_timeout, DIMENSION_TIME).si_value
        if timeout <= 0:
            raise SafetyViolation("Deadline oczekiwania Anritsu musi być dodatni.")
        session = self._require_session()
        deadline = time.monotonic() + timeout
        while True:
            response = session.query("*OPC?").strip()
            if response in {"1", "+1"}:
                self._state = DeviceState.VERIFIED
                return
            if time.monotonic() >= deadline:
                self.emergency_off()
                raise DeviceError("Timeout podczas oczekiwania Anritsu na zakończenie single sweep.")
            time.sleep(0.05)

    def acquire_single_sweep(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Synchronise, then fetch one trace that belongs to this checkpoint."""

        trace = validate_anritsu_trace_name(trace)
        self.start_single_sweep()
        self.wait_complete()
        return self.fetch_trace(trace)

    def fetch_trace(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read one complete trace; callers schedule this repeatedly for Live mode."""

        trace = validate_anritsu_trace_name(trace)
        self._assert_acquisition_allowed()
        session = self._require_session()
        session.write("FORM ASC")
        try:
            start = float(session.query("FREQ:START?"))
            stop = float(session.query("FREQ:STOP?"))
            points = int(float(session.query("SWE:POIN?")))
            raw = session.query(f"TRAC? {trace}")
            values = tuple(float(item) for item in raw.split(",") if item.strip())
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu zwrócił nieprawidłową odpowiedź podczas pobierania trace.") from exc
        if len(values) != points:
            raise DeviceError(
                f"Anritsu zwrócił {len(values)} punktów trace, oczekiwano {points}."
            )
        if points < 2:
            raise DeviceError("Anritsu zwrócił mniej niż dwa punkty trace.")
        step = (stop - start) / (points - 1)
        return SpectrumTrace(
            frequencies_hz=tuple(start + index * step for index in range(points)),
            powers_dbm=values,
            acquired_at_utc=datetime.now(timezone.utc),
            trace_name=trace,
        )
