"""Thread-owned TCP transport for MOKE Box fixed-size binary records."""

from __future__ import annotations

import socket

from app.domain.errors import ConnectionError


class MokeBoxTcpTransport:
    def __init__(self) -> None:
        self._socket: socket.socket | None = None

    def connect(self, endpoint: str, timeout_s: float) -> None:
        host, separator, port_text = endpoint.rpartition(":")
        if not separator or not host:
            raise ValueError("MOKE endpoint must be formatted as host:port.")
        self.close()
        self._socket = socket.create_connection((host, int(port_text)), timeout=timeout_s)
        self._socket.settimeout(timeout_s)

    def send(self, frame: bytes) -> None:
        if self._socket is None:
            raise ConnectionError("MOKE TCP transport is not connected.")
        self._socket.sendall(frame)

    def recv_exact(self, count: int) -> bytes:
        if self._socket is None:
            raise ConnectionError("MOKE TCP transport is not connected.")
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = self._socket.recv(remaining)
            if not chunk:
                raise ConnectionError("MOKE TCP connection closed during a record.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
