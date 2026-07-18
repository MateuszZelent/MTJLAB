"""Typed configuration and measurements for Lake Shore gaussmeters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


FieldUnit = Literal["G", "T", "Oe", "A/m"]


@dataclass(frozen=True, slots=True)
class GaussmeterConfig:
    """Connection profile for the ASCII/VISA path used by Model 475.

    The official ``lakeshore`` driver currently declares Model 425 support;
    Model 475 uses its documented ASCII command interface.  Keeping this
    configuration independent of :class:`StationSettings` lets the settings
    schema gain the device without coupling protocol code to the UI schema.
    """

    resource: str
    visa_backend: str = "system"
    timeout_ms: int = 3_000
    read_termination: str | None = "\n"
    write_termination: str | None = "\r\n"
    expected_vendor_contains: str = "LAKE SHORE"
    expected_models: tuple[str, ...] = ("475",)
    require_serial_match: bool = False
    expected_serial: str | None = None
    field_unit: FieldUnit = "T"


@dataclass(frozen=True, slots=True)
class Model425Config:
    """Official-driver connection profile for a Lake Shore Model 425."""

    connection: Literal["tcp", "usb"]
    ip_address: str | None = None
    tcp_port: int = 7777
    com_port: str | None = None
    serial_number: str | None = None
    timeout_s: float = 2.0
    expected_vendor_contains: str = "LAKE SHORE"
    expected_models: tuple[str, ...] = ("425",)
    field_unit: FieldUnit = "T"

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("Model 425 timeout_s must be positive.")
        if self.connection == "tcp" and not self.ip_address:
            raise ValueError("Model 425 TCP connection requires ip_address.")
        if self.connection == "usb" and not (self.com_port or self.serial_number):
            raise ValueError("Model 425 USB connection requires com_port or serial_number.")


@dataclass(frozen=True, slots=True)
class FieldReading:
    value: float
    unit: FieldUnit
    timestamp_utc: datetime
    status: str = "ok"

    @classmethod
    def now(cls, value: float, unit: FieldUnit) -> "FieldReading":
        return cls(value=value, unit=unit, timestamp_utc=datetime.now(timezone.utc))
