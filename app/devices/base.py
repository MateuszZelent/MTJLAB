"""Common session contracts and output interlock enforcement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
import math
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


class OutputInterlock:
    """Central rule that prevents a device adapter from enabling energy output."""

    def assert_can_enable(self, *, device_name: str, device_allows_output: bool) -> None:
        if not device_allows_output:
            raise SafetyViolation(
                f"{device_name} output is locked in settings.yml (allow_output_enable=false)."
            )


def parse_identity(resource: str, response: str) -> DeviceIdentity:
    parts = [part.strip() for part in response.strip().split(",")]
    if not response.strip():
        raise ConnectionError("The instrument returned an empty response to *IDN?.")
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
        raise ConnectionError(f"Unexpected instrument vendor: {identity.idn}")
    if expected_models and not any(model.upper() in idn_upper for model in expected_models):
        raise ConnectionError(
            f"Unexpected instrument model: {identity.idn}; expected {', '.join(expected_models)}."
        )
    if require_serial_match and identity.serial != expected_serial:
        raise ConnectionError(
            f"Instrument serial number differs from the configured value: {identity.serial!r}."
        )


class DeviceAdapter(ABC):
    """Base class.  Implementations must never expose a raw command console."""

    def __init__(self) -> None:
        self._state = DeviceState.DISCONNECTED
        self._identity: DeviceIdentity | None = None
        self._capabilities = None

    @property
    def state(self) -> DeviceState:
        return self._state

    @property
    def identity(self) -> DeviceIdentity | None:
        return self._identity

    @property
    def capabilities(self):
        """Capabilities established for this session; safe to serialize into a run."""
        return self._capabilities

    @property
    def connected(self) -> bool:
        return self._state is not DeviceState.DISCONNECTED

    def refresh_station_context(self, station: object) -> None:
        """Refresh station-wide interlocks when this device profile is unchanged."""

        if hasattr(self, "_station"):
            self._station = station

    def apply_limit_settings(self, station: object) -> None:
        """Apply changed safety limits without replacing the active adapter."""

        raise SafetyViolation(
            f"{type(self).__name__} does not support live safety-limit updates."
        )

    @classmethod
    def assert_limit_only_update(cls, previous: object, updated: object) -> None:
        """Reject any hot update containing more than range-bound changes."""

        previous_dump = previous.model_dump(mode="python")  # type: ignore[attr-defined]
        updated_dump = updated.model_dump(mode="python")  # type: ignore[attr-defined]
        changes = cls._changed_setting_paths(previous_dump, updated_dump)
        if not changes or any(
            not path or path[-1] not in {"min", "max", "max_abs"}
            for path in changes
        ):
            raise SafetyViolation(
                "Hot settings update accepts only min, max and max_abs limit changes."
            )

    @classmethod
    def _changed_setting_paths(
        cls,
        previous: object,
        updated: object,
        prefix: tuple[str, ...] = (),
    ) -> set[tuple[str, ...]]:
        if isinstance(previous, dict) and isinstance(updated, dict):
            changes: set[tuple[str, ...]] = set()
            for key in previous.keys() | updated.keys():
                path = (*prefix, str(key))
                if key not in previous or key not in updated:
                    changes.add(path)
                else:
                    changes.update(
                        cls._changed_setting_paths(previous[key], updated[key], path)
                    )
            return changes
        return set() if previous == updated else {prefix}

    @contextmanager
    def io_timeout(self, timeout_s: float):
        """Temporarily cap VISA I/O latency for one high-level operation.

        Concrete adapters deliberately keep their sessions private. They all
        use the same ``_session`` ownership convention, allowing the execution
        engine to impose a stricter deadline without exposing raw VISA to UI or
        recipes. A shorter device-profile timeout is never widened.
        """

        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Operation I/O timeout must be finite and positive.")
        session = getattr(self, "_session", None)
        if session is None:
            raise ConnectionError("Cannot set an operation timeout without an active session.")
        previous = int(session.timeout)
        requested = max(1, math.ceil(timeout_s * 1000))
        session.timeout = min(previous, requested)
        try:
            yield
        finally:
            if getattr(self, "_session", None) is session:
                session.timeout = previous

    @abstractmethod
    def connect(self) -> DeviceIdentity:
        """Open and verify an instrument session."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close an instrument session."""

    @abstractmethod
    def emergency_off(self) -> None:
        """Best-effort, idempotent shutdown.

        A transport failure must leave the adapter in ``UNKNOWN`` rather than
        falsely reporting an output-off state.  The method remains
        non-throwing so all other devices still receive their shutdown action.
        """
