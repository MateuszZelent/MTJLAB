"""Thin VISA transport that keeps pyvisa out of the safety and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Callable

import pyvisa

from app.devices.base import InstrumentSession
from app.domain.errors import ConnectionError, DeviceError


class PyVisaSessionFactory:
    """Open a VISA resource using either the system backend or an explicit backend."""

    def __init__(self) -> None:
        self._traffic_callback: Callable[[str], None] | None = None

    def set_traffic_callback(self, callback: Callable[[str], None] | None) -> None:
        self._traffic_callback = callback

    def open(self, resource: str, backend: str, timeout_ms: int) -> InstrumentSession:
        try:
            manager = pyvisa.ResourceManager() if backend == "system" else pyvisa.ResourceManager(backend)
            session = manager.open_resource(resource, open_timeout=timeout_ms)
            session.timeout = timeout_ms
            if self._traffic_callback is not None:
                self._traffic_callback(
                    f"OPEN OK resource={resource!r}, backend={backend!r}, timeout={timeout_ms} ms, "
                    f"read_termination={session.read_termination!r}, "
                    f"write_termination={session.write_termination!r}"
                )
            return _ManagedVisaSession(session, manager, self._traffic_callback)
        except Exception as exc:
            if self._traffic_callback is not None:
                self._traffic_callback(f"OPEN ERROR {resource!r}: {exc}")
            raise ConnectionError(f"Could not open VISA resource {resource!r}: {exc}") from exc


class _ManagedVisaSession:
    def __init__(
        self,
        session: InstrumentSession,
        manager: object,
        traffic_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._manager = manager
        self._traffic_callback = traffic_callback

    def _emit(self, message: str) -> None:
        if self._traffic_callback is not None:
            self._traffic_callback(message)

    @staticmethod
    def _display_response(response: str) -> str:
        if len(response) <= 1000:
            return repr(response)
        return f"{response[:1000]!r}... <{len(response)} characters total>"

    @staticmethod
    def _is_bulk_trace_query(command: str) -> bool:
        """Return True for trace-data queries whose payload must not enter logs."""

        normalized = " ".join(command.strip().lstrip(":").upper().split())
        return bool(
            re.match(r"^TRAC(?:E)?(?:\d+)?(?:\:DATA)?\?\s+", normalized)
            or re.match(r"^TRAC(?:E)?\?\s+", normalized)
        )

    @staticmethod
    def _trace_response_summary(response: str) -> str:
        if not response:
            return "<spectrum data suppressed; empty response>"
        if response.startswith("#"):
            return f"<binary spectrum data suppressed; {len(response)} characters received>"
        points = response.count(",") + 1
        return (
            f"<spectrum data suppressed; {points} point(s), "
            f"{len(response)} characters received>"
        )

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
        self._emit(f"TX WRITE {command!r}")
        try:
            result = self._session.write(command)
        except Exception as exc:
            self._emit(f"TX ERROR {command!r}: {exc}")
            raise DeviceError(f"VISA write {command!r} failed: {exc}") from exc
        self._emit(f"TX OK {command!r}")
        return result

    def query(self, command: str) -> str:
        started = time.perf_counter()
        self._emit(f"TX QUERY {command!r}")
        try:
            response = self._session.query(command).strip()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._emit(f"RX ERROR {command!r} after {elapsed_ms:.1f} ms: {exc}")
            raise DeviceError(f"VISA query {command!r} failed: {exc}") from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        displayed = (
            self._trace_response_summary(response)
            if self._is_bulk_trace_query(command)
            else self._display_response(response)
        )
        self._emit(f"RX {command!r} after {elapsed_ms:.1f} ms: {displayed}")
        return response

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
            raise DeviceError(f"Could not close VISA: {errors[0]}") from errors[0]


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
            raise DeviceError("The fake VISA session is closed.")
        self.writes.append(command)

    def query(self, command: str) -> str:
        if self.closed:
            raise DeviceError("The fake VISA session is closed.")
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
            keithley_readback = re.match(
                r"^print\((smu[ab]\.(?:(?:source|measure)\.[A-Za-z0-9_]+|sense))\)$",
                command,
            )
            if keithley_readback:
                field = keithley_readback.group(1)
                assignment = re.compile(
                    rf"^{re.escape(field)}\s*=\s*(.+)$", re.IGNORECASE
                )
                for write in reversed(self.writes[:-1]):
                    match = assignment.match(write)
                    if match:
                        return match.group(1)
            rigol_readback = re.match(
                r"^:(OUTP\d+:LOAD|SOUR\d+:PHAS|"
                r"SOUR\d+:FUNC:(?:SQU:DCYC|RAMP:SYMM|PULS:(?:WIDT|TRAN:LEAD|TRAN:TRA)))\?$",
                command,
            )
            if rigol_readback:
                field = rigol_readback.group(1)
                assignment = re.compile(
                    rf"^:{re.escape(field)}\s+(.+)$", re.IGNORECASE
                )
                for write in reversed(self.writes[:-1]):
                    match = assignment.match(write)
                    if match:
                        return match.group(1)
                if field.endswith(":LOAD"):
                    return "INF"
                if field.endswith(":PHAS"):
                    return "0"
                if field.endswith(":DCYC") or field.endswith(":SYMM"):
                    return "50"
                if field.endswith(":WIDT"):
                    return "0.0001"
                return "1e-08"
            scpi_readback = re.match(r"^(:[A-Za-z0-9:]+)\?$", command)
            if scpi_readback:
                field = scpi_readback.group(1)
                assignment = re.compile(
                    rf"^{re.escape(field)}\s+(.+)$", re.IGNORECASE
                )
                for write in reversed(self.writes[:-1]):
                    match = assignment.match(write)
                    if match:
                        return match.group(1)
            raise DeviceError(f"No fake VISA response is configured for {command!r}.")
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
