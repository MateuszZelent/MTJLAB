"""Common session contracts and output interlock enforcement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.errors import ConnectionError, SafetyViolation
from app.domain.models import DeviceIdentity, DeviceState


@runtime_checkable
class InstrumentSession(Protocol):
    """Minimal synchronous VISA-like session owned by exactly one worker thread."""

    timeout: int
    read_termination: str | None
    write_termination: str | None

    def write(self, command: str) -> object: ...

    def query(self, command: str) -> str: ...

    def close(self) -> object: ...


@runtime_checkable
class SessionFactory(Protocol):
    def open(self, resource: str, backend: str, timeout_ms: int) -> InstrumentSession: ...


@dataclass(frozen=True, slots=True)
class OutputInterlock:
    """Central rule that prevents a device adapter from enabling energy output."""

    profile_approved: bool
    profile_locks_outputs: bool

    def assert_can_enable(self, *, device_name: str, device_allows_output: bool) -> None:
        if self.profile_locks_outputs and not self.profile_approved:
            raise SafetyViolation(
                f"Wyjście {device_name} jest zablokowane: profil stanowiska nie jest zatwierdzony."
            )
        if not device_allows_output:
            raise SafetyViolation(
                f"Wyjście {device_name} jest zablokowane w settings.yml (allow_output_enable=false)."
            )


def parse_identity(resource: str, response: str) -> DeviceIdentity:
    parts = [part.strip() for part in response.strip().split(",")]
    if not response.strip():
        raise ConnectionError("Urządzenie zwróciło pustą odpowiedź na *IDN?.")
    return DeviceIdentity(
        resource=resource,
        idn=response.strip(),
        manufacturer=parts[0] if len(parts) > 0 else None,
        model=parts[1] if len(parts) > 1 else None,
        serial=parts[2] if len(parts) > 2 else None,
        firmware=parts[3] if len(parts) > 3 else None,
    )


def validate_identity(
    identity: DeviceIdentity,
    *,
    vendor_contains: str,
    expected_models: tuple[str, ...],
    expected_serial: str | None,
    require_serial_match: bool,
) -> None:
    idn_upper = identity.idn.upper()
    if vendor_contains.upper() not in idn_upper:
        raise ConnectionError(f"Nieoczekiwany producent urządzenia: {identity.idn}")
    if expected_models and not any(model.upper() in idn_upper for model in expected_models):
        raise ConnectionError(
            f"Nieoczekiwany model urządzenia: {identity.idn}; oczekiwano {', '.join(expected_models)}."
        )
    if require_serial_match and identity.serial != expected_serial:
        raise ConnectionError(
            f"Numer seryjny urządzenia różni się od zatwierdzonego: {identity.serial!r}."
        )


class DeviceAdapter(ABC):
    """Base class.  Implementations must never expose a raw command console."""

    def __init__(self) -> None:
        self._state = DeviceState.DISCONNECTED
        self._identity: DeviceIdentity | None = None

    @property
    def state(self) -> DeviceState:
        return self._state

    @property
    def identity(self) -> DeviceIdentity | None:
        return self._identity

    @property
    def connected(self) -> bool:
        return self._state is not DeviceState.DISCONNECTED

    @abstractmethod
    def connect(self) -> DeviceIdentity:
        """Open and verify an instrument session."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close an instrument session."""

    @abstractmethod
    def emergency_off(self) -> None:
        """Best-effort, idempotent output shutdown.  Must not raise on disconnect."""

