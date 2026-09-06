"""Thread-owned TCP transport for MOKE Box fixed-size binary records."""

from __future__ import annotations

import socket
import time

from app.domain.errors import ConnectionError


class MokeBoxTcpTransport:
    def __init__(self) -> None:
        self._socket: socket.socket | None = None
        self._timeout_s: float | None = None

    def connect(self, endpoint: str, timeout_s: float) -> None:
        host, separator, port_text = endpoint.rpartition(":")
        if not separator or not host:
            raise ValueError("MOKE endpoint must be formatted as host:port.")
        self.close()
        self._timeout_s = timeout_s
        self._socket = socket.create_connection((host, int(port_text)), timeout=timeout_s)
        self._socket.settimeout(timeout_s)

    def send(self, frame: bytes) -> None:
        if self._socket is None:
            raise ConnectionError("MOKE TCP transport is not connected.")
        self._socket.sendall(frame)

    def recv_exact(self, count: int) -> bytes:
        if self._socket is None:
            raise ConnectionError("MOKE TCP transport is not connected.")
        configured_timeout = self._timeout_s
        if configured_timeout is None:
            raw_timeout = getattr(self._socket, "gettimeout", lambda: None)()
            if raw_timeout is not None:
                configured_timeout = float(raw_timeout)
            else:
                configured_timeout = getattr(self._socket, "timeout", None)

        deadline = (
            (time.monotonic() + configured_timeout)
            if (configured_timeout is not None and configured_timeout > 0)
            else None
        )
        chunks: list[bytes] = []
        remaining = count
        try:
            while remaining:
                if deadline is not None:
                    remaining_s = deadline - time.monotonic()
                    if remaining_s <= 0:
                        raise TimeoutError(
                            f"MOKE TCP transport timed out receiving record ({count} bytes requested)."
                        )
                    if hasattr(self._socket, "settimeout"):
                        self._socket.settimeout(remaining_s)
                try:
                    chunk = self._socket.recv(remaining)
                except (socket.timeout, TimeoutError) as exc:
                    raise TimeoutError(
                        f"MOKE TCP transport timed out receiving record ({count} bytes requested)."
                    ) from exc
                if not chunk:
                    raise ConnectionError("MOKE TCP connection closed during a record.")
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            if (
                self._socket is not None
                and configured_timeout is not None
                and hasattr(self._socket, "settimeout")
            ):
                try:
                    self._socket.settimeout(configured_timeout)
                except Exception:
                    pass
        return b"".join(chunks)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
