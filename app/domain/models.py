"""Shared, serialisable state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ApplicationState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    VERIFIED = "verified"
    ARMED = "armed"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    SAFE = "safe"
    FAULT = "fault"
    UNKNOWN = "unknown"


class DeviceState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    VERIFIED = "verified"
    OUTPUT_OFF = "output_off"
    OUTPUT_ON = "output_on"
    COMPLIANCE = "compliance"
    FAULT = "fault"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    resource: str
    idn: str
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceCapabilities:
    device_name: str
    model: str
    firmware: str | None
    features: frozenset[str] = frozenset()
    unsupported_commands: frozenset[str] = frozenset()
    hardware_options: tuple[str, ...] = ()

    def supports(self, feature: str) -> bool:
        return feature in self.features


@dataclass(frozen=True, slots=True)
class MeasurementPoint:
    index: int
    setpoints: dict[str, float]
    measurements: dict[str, float]
    status: str = "ok"
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
