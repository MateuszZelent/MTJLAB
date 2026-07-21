"""Validation and durable persistence for Keithley manual-form defaults."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
from pathlib import Path
from typing import Any

from app.devices.keithley_2600 import KeithleySourceRequest
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.safety.keithley import validate_keithley_source
from app.settings import SettingsRepository
from app.settings.models import StationSettings


_REQUIRED_SNAPSHOT_FIELDS = {
    "source_mode",
    "source_level",
    "compliance",
    "nplc",
    "settling_time",
    "sense_mode",
    "source_autorange",
    "source_range",
    "measure_voltage_autorange",
    "measure_voltage_range",
    "measure_current_autorange",
    "measure_current_range",
}


def validate_keithley_default_snapshots(
    settings: StationSettings,
    snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Normalize and safety-check a complete A/B form snapshot in memory."""

    if set(snapshots) != {"A", "B"}:
        raise ConfigurationError("Incomplete Keithley A/B form configuration.")

    updates: dict[str, dict[str, Any]] = {}
    for channel in ("A", "B"):
        snapshot = snapshots[channel]
        missing = _REQUIRED_SNAPSHOT_FIELDS - set(snapshot)
        if missing:
            raise ConfigurationError(
                f"Channel {channel}: missing form fields: {', '.join(sorted(missing))}."
            )
        mode = str(snapshot["source_mode"]).strip().lower()
        if mode not in {"measure_only", "current", "voltage"}:
            raise ConfigurationError(
                f"Channel {channel}: invalid source mode {mode!r}."
            )
        level_dimension = (
            DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        )
        compliance_dimension = (
            DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
        )

        def manual_range(
            value: object, dimension: str, autorange: bool, label: str
        ) -> float | None:
            text = str(value).strip()
            if autorange:
                if text.upper() != "AUTO":
                    raise ConfigurationError(
                        f"Channel {channel}: {label} must be AUTO while autorange is ON."
                    )
                return None
            if text.upper() == "AUTO":
                raise ConfigurationError(
                    f"Channel {channel}: {label} is AUTO while autorange is OFF."
                )
            return parse_quantity(text, dimension).si_value

        try:
            nplc = float(str(snapshot["nplc"]).replace(",", "."))
        except ValueError as exc:
            raise ConfigurationError(
                f"Channel {channel}: NPLC must be a finite number."
            ) from exc
        if not math.isfinite(nplc):
            raise ConfigurationError(
                f"Channel {channel}: NPLC must be a finite number."
            )

        source_autorange = bool(snapshot["source_autorange"])
        measure_voltage_autorange = bool(snapshot["measure_voltage_autorange"])
        measure_current_autorange = bool(snapshot["measure_current_autorange"])
        request = KeithleySourceRequest(
            channel=channel,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            level_si=(
                0.0
                if mode == "measure_only"
                else parse_quantity(str(snapshot["source_level"]), level_dimension).si_value
            ),
            compliance_si=(
                0.0
                if mode == "measure_only"
                else parse_quantity(
                    str(snapshot["compliance"]), compliance_dimension
                ).si_value
            ),
            nplc=nplc,
            settle_time_s=parse_quantity(
                str(snapshot["settling_time"]), DIMENSION_TIME
            ).si_value,
            sense_mode=str(snapshot["sense_mode"]),  # type: ignore[arg-type]
            source_autorange=source_autorange,
            source_range_si=(
                None
                if mode == "measure_only"
                else manual_range(
                    snapshot["source_range"],
                    level_dimension,
                    source_autorange,
                    "source range",
                )
            ),
            measure_voltage_autorange=measure_voltage_autorange,
            measure_voltage_range_si=manual_range(
                snapshot["measure_voltage_range"],
                DIMENSION_VOLTAGE,
                measure_voltage_autorange,
                "measure-voltage range",
            ),
            measure_current_autorange=measure_current_autorange,
            measure_current_range_si=manual_range(
                snapshot["measure_current_range"],
                DIMENSION_CURRENT,
                measure_current_autorange,
                "measure-current range",
            ),
        )
        try:
            # Saving a harmless default for a disabled channel must remain
            # possible.  The real configure/OUTPUT paths validate the original
            # channel model again and still reject disabled hardware.
            validation_channel = settings.keithley.safety.channels[
                channel
            ].model_copy(update={"enabled": True})
            validate_keithley_source(
                validation_channel, request
            )
        except SafetyViolation as exc:
            raise SafetyViolation(f"Channel {channel}: {exc}") from exc

        channel_updates: dict[str, Any] = {
            "source_mode": mode,
            "sense_mode": str(snapshot["sense_mode"]),
            "nplc": request.nplc,
            "settling_time": str(snapshot["settling_time"]),
            "source_autorange": source_autorange,
            "source_range": str(snapshot["source_range"]),
            "measure_voltage_autorange": measure_voltage_autorange,
            "measure_voltage_range": str(snapshot["measure_voltage_range"]),
            "measure_current_autorange": measure_current_autorange,
            "measure_current_range": str(snapshot["measure_current_range"]),
        }
        if mode == "current":
            channel_updates["source_current"] = str(snapshot["source_level"])
            channel_updates["voltage_compliance"] = str(snapshot["compliance"])
        elif mode == "voltage":
            channel_updates["source_voltage"] = str(snapshot["source_level"])
            channel_updates["current_compliance"] = str(snapshot["compliance"])
        updates[channel] = channel_updates
    return updates


def persist_keithley_default_snapshots(
    settings_path: str | Path,
    snapshots: Mapping[str, Mapping[str, Any]],
) -> tuple[StationSettings, dict[str, Any]]:
    """Merge validated Keithley defaults into the latest YAML and save atomically."""

    repository = SettingsRepository(settings_path)
    def merge(
        raw: dict[str, Any], settings: StationSettings
    ) -> dict[str, Any]:
        updates = validate_keithley_default_snapshots(settings, snapshots)
        payload = deepcopy(raw)
        channels = payload["devices"]["keithley"]["safety"]["channels"]
        for channel, values in updates.items():
            channels[channel]["defaults"].update(values)
        return payload

    return repository.update_raw(merge)
