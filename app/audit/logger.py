"""Append-only JSONL audit logging for safety-relevant station activity."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from app.domain.errors import ConfigurationError


_SEVERITIES = frozenset({"debug", "info", "warning", "error", "critical"})
_SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "authorization")


def _redacted_json_value(value: object, *, key: str = "") -> object:
    """Return a finite, serializable value while redacting secret-bearing fields."""

    normalized_key = key.casefold().replace("-", "_")
    if any(part in normalized_key for part in _SECRET_KEY_PARTS):
        return "<redacted>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {
            str(nested_key): _redacted_json_value(nested, key=str(nested_key))
            for nested_key, nested in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redacted_json_value(nested) for nested in value]
    return str(value)


class AuditLogger:
    """Thread-safe append-only audit file owned by one application session."""

    schema_version = 1

    def __init__(
        self,
        directory: str | Path,
        *,
        profile_id: str,
        simulation: bool,
        application_version: str = "0.1.0",
        actor: str = "",
        actor_roles: tuple[str, ...] = (),
    ) -> None:
        self.directory = Path(directory)
        self.profile_id = profile_id
        self.simulation = simulation
        self.application_version = application_version
        self.actor = actor
        self.actor_roles = tuple(actor_roles)
        self.session_id = uuid4().hex
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.path = self.directory / f"lab-control_{timestamp}_{self.session_id[:8]}.jsonl"
        # Exclusive creation proves that an existing audit can never be overwritten.
        with self.path.open("x", encoding="utf-8", newline="\n"):
            pass
        self._stream = self.path.open("a", encoding="utf-8", newline="\n")
        self.record(
            "Application audit session started",
            category="application",
            event_type="session_started",
            context={
                "application_version": application_version,
                "profile_id": profile_id,
                "simulation": simulation,
                "actor": actor,
                "actor_roles": self.actor_roles,
            },
            critical=True,
        )

    def record(
        self,
        message: str,
        *,
        severity: str = "info",
        category: str = "application",
        event_type: str = "message",
        context: Mapping[str, object] | None = None,
        correlation_id: str | None = None,
        critical: bool = False,
    ) -> dict[str, object]:
        """Append and return one schema-versioned event.

        Critical events are flushed through the operating-system page cache with
        ``fsync`` before this method returns.
        """

        normalized_severity = severity.casefold()
        if normalized_severity not in _SEVERITIES:
            raise ValueError(f"Unsupported audit severity: {severity!r}.")
        if not message.strip():
            raise ValueError("Audit message cannot be empty.")
        with self._lock:
            if self._closed:
                raise RuntimeError("Audit logger is closed.")
            return self._append_unlocked(
                message=message,
                severity=normalized_severity,
                category=category,
                event_type=event_type,
                context=context,
                correlation_id=correlation_id,
                critical=critical,
            )

    def _append_unlocked(
        self,
        *,
        message: str,
        severity: str,
        category: str,
        event_type: str,
        context: Mapping[str, object] | None,
        correlation_id: str | None,
        critical: bool,
    ) -> dict[str, object]:
        event: dict[str, object] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "sequence": self._sequence,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "severity": severity,
            "category": category,
            "event_type": event_type,
            "message": message,
            "profile_id": self.profile_id,
            "simulation": self.simulation,
            "actor": self.actor,
            "actor_roles": self.actor_roles,
            "correlation_id": correlation_id,
            "context": _redacted_json_value(dict(context or {})),
        }
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self._stream.write(encoded + "\n")
        self._stream.flush()
        if critical:
            os.fsync(self._stream.fileno())
        self._sequence += 1
        return event

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._append_unlocked(
                    message="Application audit session closed",
                    severity="info",
                    category="application",
                    event_type="session_closed",
                    context=None,
                    correlation_id=None,
                    critical=True,
                )
            finally:
                self._closed = True
                if hasattr(self, "_stream") and not self._stream.closed:
                    self._stream.close()


class AuditLogReader:
    """Strict reader used by diagnostics, tests and future log export UI."""

    @staticmethod
    def read(path: str | Path) -> tuple[dict[str, Any], ...]:
        return tuple(AuditLogReader.iter_records(path))

    @staticmethod
    def iter_records(path: str | Path) -> Iterator[dict[str, Any]]:
        source = Path(path)
        expected_session: str | None = None
        expected_sequence = 0
        try:
            stream = source.open("r", encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"Cannot open audit log {source}: {exc}") from exc
        with stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ConfigurationError(
                        f"Invalid JSON in audit log {source} at line {line_number}."
                    ) from exc
                if not isinstance(record, dict) or record.get("schema_version") != AuditLogger.schema_version:
                    raise ConfigurationError(
                        f"Unsupported audit record at {source}:{line_number}."
                    )
                session_id = record.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise ConfigurationError(f"Missing session_id at {source}:{line_number}.")
                if expected_session is None:
                    expected_session = session_id
                if session_id != expected_session:
                    raise ConfigurationError(f"Mixed audit sessions at {source}:{line_number}.")
                if record.get("sequence") != expected_sequence:
                    raise ConfigurationError(
                        f"Non-contiguous audit sequence at {source}:{line_number}."
                    )
                expected_sequence += 1
                yield record
