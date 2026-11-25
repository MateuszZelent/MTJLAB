"""Append-only JSONL audit logging for safety-relevant station activity."""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.errors import ConfigurationError
from app.version import get_full_version

_SEVERITIES = frozenset({"debug", "info", "warning", "error", "critical"})
_SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "authorization")
_IMMEDIATE_FLUSH_CATEGORIES = frozenset({"safety"})
_IMMEDIATE_FLUSH_EVENT_TYPES = frozenset({
    "session_started",
    "session_closed",
    "run_started",
    "run_completed",
    "run_aborted",
    "run_faulted",
    "run_closed",
    "emergency_stop",
    "output_enabled",
    "output_disabled",
})
_BATCH_MAX_ITEMS = 64
_BATCH_FLUSH_INTERVAL_S = 0.05
_WRITER_ERRORS = (OSError, RuntimeError, ValueError, TypeError)

_QueueEntry = tuple[
    tuple[dict[str, object], dict[str, object]] | None,
    bool,
    bool,
    threading.Event | None,
]


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
        application_version: str | None = None,
        actor: str = "",
        actor_roles: tuple[str, ...] = (),
    ) -> None:
        self.directory = Path(directory)
        self.profile_id = profile_id
        self.simulation = simulation
        self.application_version = application_version or get_full_version()
        self.actor = actor
        self.actor_roles = tuple(actor_roles)
        self.session_id = uuid4().hex
        self._lock = threading.RLock()
        self._sequence = 0
        self._closed = False
        self._failure: BaseException | None = None
        self._stop = threading.Event()
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self.path = self.directory / f"lab-control_{timestamp}_{self.session_id[:8]}.jsonl"
        # Exclusive creation proves that an existing audit can never be overwritten.
        with self.path.open("x", encoding="utf-8", newline="\n"):
            pass
        self._stream = self.path.open("a", encoding="utf-8", newline="\n")

        self._queue: queue.Queue[_QueueEntry] = queue.Queue(maxsize=10000)
        self._worker = threading.Thread(
            target=self._writer_loop,
            name=f"AuditWriter-{self.session_id[:8]}",
            daemon=True,
        )
        self._worker.start()

        try:
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
                wait_durable=True,
            )
        except Exception:
            self._stop.set()
            self._worker.join(timeout=5.0)
            raise

    def check_health(self) -> None:
        """Expose asynchronous storage failure to callers before enabling output."""
        with self._lock:
            if self._failure is not None:
                raise RuntimeError(f"Durable audit unavailable: {self._failure}") from self._failure

    def _fail(self, error: BaseException) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error

    def _enqueue(
        self,
        payload: tuple[dict[str, object], dict[str, object]] | None,
        immediate_flush: bool,
        needs_fsync: bool,
        ack: threading.Event | None,
    ) -> None:
        # Called under _lock: sequence assignment and admission are ordered.
        self.check_health()
        try:
            self._queue.put_nowait((payload, immediate_flush, needs_fsync, ack))
        except queue.Full as exc:
            self._fail(RuntimeError("Audit queue is full; record was not accepted."))
            raise RuntimeError("Audit queue is full; record was not accepted.") from exc

    def _wait_durable(self, ack: threading.Event) -> None:
        if not ack.wait(timeout=5.0):
            self._fail(TimeoutError("Audit durability confirmation timed out."))
        self.check_health()

    def _writer_loop(self) -> None:
        """Background thread redacting, serializing, batch-flushing, and fsyncing (GUI-01 / PERF-M3)."""
        dirty = False
        last_flush = time.monotonic()
        try:
            while not self._stop.is_set() or not self._queue.empty() or dirty:
                if dirty:
                    timeout = max(0.0, _BATCH_FLUSH_INTERVAL_S - (time.monotonic() - last_flush))
                else:
                    timeout = 0.1
                try:
                    first = self._queue.get(timeout=timeout)
                except queue.Empty:
                    if dirty:
                        try:
                            self.check_health()
                            self._stream.flush()
                            dirty = False
                            last_flush = time.monotonic()
                        except _WRITER_ERRORS as exc:
                            self._fail(exc)
                            dirty = False
                    continue

                batch: list[_QueueEntry] = [first]
                if first[3] is None and not first[2]:
                    while len(batch) < _BATCH_MAX_ITEMS:
                        try:
                            nxt = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        batch.append(nxt)
                        if nxt[3] is not None or nxt[2]:
                            break

                lines: list[str] = []
                batch_flush = False
                batch_fsync = False
                acks: list[threading.Event] = []
                try:
                    self.check_health()
                    for payload, immediate_flush, needs_fsync, ack in batch:
                        if payload is not None:
                            event_base, raw_context = payload
                            redacted_event = {
                                **event_base,
                                "context": _redacted_json_value(raw_context),
                            }
                            lines.append(
                                json.dumps(
                                    redacted_event,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                )
                                + "\n"
                            )
                        if immediate_flush:
                            batch_flush = True
                        if needs_fsync:
                            batch_fsync = True
                            batch_flush = True
                        if ack is not None:
                            acks.append(ack)
                    if lines:
                        self._stream.write("".join(lines))
                        dirty = True
                    now = time.monotonic()
                    if batch_flush or (
                        dirty
                        and (
                            len(batch) >= _BATCH_MAX_ITEMS
                            or self._stop.is_set()
                            or (now - last_flush) >= _BATCH_FLUSH_INTERVAL_S
                        )
                    ):
                        self._stream.flush()
                        dirty = False
                        last_flush = now
                    if batch_fsync:
                        os.fsync(self._stream.fileno())
                except _WRITER_ERRORS as exc:
                    self._fail(exc)
                    dirty = False
                finally:
                    for ack in acks:
                        ack.set()  # completion only; success requires check_health
                    for _ in batch:
                        self._queue.task_done()
        finally:
            try:
                if dirty and self._failure is None:
                    self._stream.flush()
            except _WRITER_ERRORS as exc:
                self._fail(exc)
            try:
                self._stream.close()
            except _WRITER_ERRORS as exc:
                self._fail(exc)

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
        wait_durable: bool = False,
    ) -> dict[str, object]:
        """Append and return one schema-versioned event.

        Redaction, JSON serialization, disk writes, and batched flushes execute
        asynchronously on the dedicated AuditWriter background thread so the Qt
        main thread is never blocked (GUI-01 / PERF-M3). Critical safety and
        run-lifecycle events flush immediately; ``wait_durable=True`` waits
        until the event has been flushed and fsynced to disk.
        """

        normalized_severity = severity.casefold()
        if normalized_severity not in _SEVERITIES:
            raise ValueError(f"Unsupported audit severity: {severity!r}.")
        if not message.strip():
            raise ValueError("Audit message cannot be empty.")
        raw_context = dict(context) if context else {}
        needs_fsync = critical or wait_durable
        immediate_flush = (
            needs_fsync
            or category in _IMMEDIATE_FLUSH_CATEGORIES
            or event_type in _IMMEDIATE_FLUSH_EVENT_TYPES
            or normalized_severity in {"error", "critical"}
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("Audit logger is closed.")
            self.check_health()
            event_base: dict[str, object] = {
                "schema_version": self.schema_version,
                "session_id": self.session_id,
                "sequence": self._sequence,
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "monotonic_ns": time.monotonic_ns(),
                "severity": normalized_severity,
                "category": category,
                "event_type": event_type,
                "message": message,
                "profile_id": self.profile_id,
                "simulation": self.simulation,
                "actor": self.actor,
                "actor_roles": self.actor_roles,
                "correlation_id": correlation_id,
            }
            ack = threading.Event() if wait_durable else None
            self._enqueue((event_base, raw_context), immediate_flush, needs_fsync, ack)
            self._sequence += 1

        if ack is not None:
            self._wait_durable(ack)

        return {**event_base, "context": raw_context}

    def flush(self) -> None:
        """Wait for all currently queued audit records to be written to disk."""
        ack = threading.Event()
        with self._lock:
            if self._closed:
                self.check_health()
                return
            self._enqueue(None, True, True, ack)
        self._wait_durable(ack)

    def close(self) -> None:
        try:
            with self._lock:
                if not self._closed:
                    try:
                        self.record(
                            "Application audit session closed",
                            category="application", event_type="session_closed", critical=True,
                        )
                    finally:
                        self._closed = True
        finally:
            self._stop.set()
            self._worker.join(timeout=5.0)
        if self._worker.is_alive():
            self._fail(TimeoutError("Audit writer did not finish closing within 5 s."))
        self.check_health()


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
