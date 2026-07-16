"""Thin VISA transport that keeps pyvisa out of the safety and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable

import pyvisa

from app.devices.base import InstrumentSession
from app.domain.errors import ConnectionError, DeviceError


class PyVisaSessionFactory:
    """Open a VISA resource using either the system backend or an explicit backend."""

    def open(self, resource: str, backend: str, timeout_ms: int) -> InstrumentSession:
        try:
            manager = pyvisa.ResourceManager() if backend == "system" else pyvisa.ResourceManager(backend)
            session = manager.open_resource(resource, open_timeout=timeout_ms)
            session.timeout = timeout_ms
            return _ManagedVisaSession(session=session, manager=manager)
        except Exception as exc:
            raise ConnectionError(f"Nie można otworzyć zasobu VISA {resource!r}: {exc}") from exc


class _ManagedVisaSession:
    def __init__(self, session: InstrumentSession, manager: object) -> None:
        self._session = session
        self._manager = manager

    @property
    def timeout(self) -> int:
        return self._session.timeout

    @timeout.setter
    def timeout(self, value: int) -> None:
        self._session.timeout = value

    @property
    def read_termination(self) -> str | None:
        return self._session.read_termination

    @read_termination.setter
    def read_termination(self, value: str | None) -> None:
        self._session.read_termination = value

    @property
    def write_termination(self) -> str | None:
        return self._session.write_termination

    @write_termination.setter
    def write_termination(self, value: str | None) -> None:
        self._session.write_termination = value

    def write(self, command: str) -> object:
        try:
            return self._session.write(command)
        except Exception as exc:
            raise DeviceError(f"VISA write {command!r} nie powiódł się: {exc}") from exc

    def query(self, command: str) -> str:
        try:
            return self._session.query(command).strip()
        except Exception as exc:
            raise DeviceError(f"VISA query {command!r} nie powiódł się: {exc}") from exc

    def close(self) -> None:
        errors: list[Exception] = []
        try:
            self._session.close()
        except Exception as exc:  # best effort cleanup
            errors.append(exc)
        try:
            close = getattr(self._manager, "close")
            close()
        except Exception as exc:  # best effort cleanup
            errors.append(exc)
        if errors:
            raise DeviceError(f"Nie można zamknąć VISA: {errors[0]}") from errors[0]


@dataclass
class FakeVisaSession:
    """Deterministic in-memory session used by adapter and engine tests."""

    responses: dict[str, str | Callable[[str], str]] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)
    timeout: int = 1000
    read_termination: str | None = None
    write_termination: str | None = None
    closed: bool = False

    def write(self, command: str) -> None:
        if self.closed:
            raise DeviceError("Sesja testowa jest zamknięta.")
        self.writes.append(command)

    def query(self, command: str) -> str:
        if self.closed:
            raise DeviceError("Sesja testowa jest zamknięta.")
        self.writes.append(command)
        response = self.responses.get(command)
        if response is None:
            output = re.match(r"^print\((smu[ab])\.source\.output\)$", command)
            if output:
                smu = output.group(1)
                for write in reversed(self.writes[:-1]):
                    state = re.match(
                        rf"^{smu}\.source\.output\s*=\s*{smu}\.(OUTPUT_ON|OUTPUT_OFF)$", write
                    )
                    if state:
                        return "1" if state.group(1) == "OUTPUT_ON" else "0"
                return "0"
            raise DeviceError(f"Brak zaprogramowanej odpowiedzi fake VISA dla {command!r}.")
        return response(command) if callable(response) else response

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeVisaSessionFactory:
    session: FakeVisaSession
    opened_resources: list[tuple[str, str, int]] = field(default_factory=list)

    def open(self, resource: str, backend: str, timeout_ms: int) -> FakeVisaSession:
        self.opened_resources.append((resource, backend, timeout_ms))
        return self.session
