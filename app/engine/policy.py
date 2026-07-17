"""Validated execution deadlines, retries and watchdog timing."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.engine.compiler import PlanAction
from app.settings.models import StationSettings


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    command_timeout_s: float = 5.0
    acquisition_timeout_s: float = 30.0
    retry_count: int = 1
    retry_backoff_s: float = 0.25
    heartbeat_interval_s: float = 1.0
    watchdog_grace_s: float = 0.5

    def __post_init__(self) -> None:
        positive = {
            "command_timeout_s": self.command_timeout_s,
            "acquisition_timeout_s": self.acquisition_timeout_s,
            "heartbeat_interval_s": self.heartbeat_interval_s,
        }
        for name, value in positive.items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name} must be finite and positive.")
        if not math.isfinite(self.retry_backoff_s) or self.retry_backoff_s < 0:
            raise ConfigurationError("retry_backoff_s must be finite and non-negative.")
        if not math.isfinite(self.watchdog_grace_s) or self.watchdog_grace_s < 0:
            raise ConfigurationError("watchdog_grace_s must be finite and non-negative.")
        if not 0 <= self.retry_count <= 5:
            raise ConfigurationError("retry_count must be in the range 0..5.")

    @classmethod
    def from_settings(cls, settings: StationSettings) -> "ExecutionPolicy":
        execution = settings.execution
        return cls(
            command_timeout_s=parse_quantity(
                execution.get("command_timeout", "5 s"), DIMENSION_TIME
            ).si_value,
            acquisition_timeout_s=parse_quantity(
                settings.anritsu.acquisition.operation_complete_timeout,
                DIMENSION_TIME,
            ).si_value,
            retry_count=int(execution.get("retry_count", 1)),
            retry_backoff_s=parse_quantity(
                execution.get("retry_backoff", "250 ms"), DIMENSION_TIME
            ).si_value,
            heartbeat_interval_s=parse_quantity(
                execution.get("heartbeat_interval", "1 s"), DIMENSION_TIME
            ).si_value,
            watchdog_grace_s=parse_quantity(
                execution.get("watchdog_grace", "500 ms"), DIMENSION_TIME
            ).si_value,
        )

    def deadline_for(self, action: PlanAction) -> float:
        """Return the whole-operation deadline, including protocol overhead."""

        if action.kind == "wait":
            return float(action.payload["duration_s"]) + self.watchdog_grace_s
        if action.kind == "ramp_keithley_to_zero":
            return float(action.payload["deadline_s"]) + self.watchdog_grace_s
        if action.kind == "acquire_spectrum":
            return (
                self.acquisition_timeout_s
                + self.command_timeout_s
                + self.watchdog_grace_s
            )
        return self.command_timeout_s + self.watchdog_grace_s
