"""Safe loading and atomic persistence for the station settings file."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from functools import wraps
import math
import os
from pathlib import Path
import tempfile
from threading import RLock
import time
from typing import Any, Callable

from pydantic import ValidationError
from ruamel.yaml import YAML

from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.settings.models import StationSettings
from app.settings.validation import format_settings_validation_error


_SETTINGS_IO_LOCK = RLock()


def _serialized_settings_io(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize settings transactions across GUI and background writers."""

    @wraps(method)
    def locked(*args: Any, **kwargs: Any) -> Any:
        with _SETTINGS_IO_LOCK:
            return method(*args, **kwargs)

    return locked


@dataclass(slots=True)
class LoadedSettings:
    settings: StationSettings
    raw: dict[str, Any]
    source: Path


@dataclass(frozen=True, slots=True)
class SettingsRepairReport:
    """Describe a deterministic on-disk settings migration."""

    source: Path
    created: bool
    defaults_added: bool
    known_issues_repaired: bool
    safety_limits_narrowed: bool
    backup: Path | None

    @property
    def changed(self) -> bool:
        """Return whether startup created or rewrote the settings file."""

        return (
            self.created
            or self.defaults_added
            or self.known_issues_repaired
            or self.safety_limits_narrowed
        )


class SettingsRepository:
    """Read/write station configuration without silently accepting invalid YAML."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.default_flow_style = False

    @_serialized_settings_io
    def load(self) -> LoadedSettings:
        """Load settings after applying safe, deterministic file repairs."""

        loaded, _report = self._load_and_repair()
        return loaded

    @_serialized_settings_io
    def repair(self) -> SettingsRepairReport:
        """Repair this settings file and return an idempotent migration report."""

        _loaded, report = self._load_and_repair()
        return report

    def _load_and_repair(self) -> tuple[LoadedSettings, SettingsRepairReport]:
        created = self.ensure_exists()
        if not self.path.is_file():
            raise ConfigurationError(f"Configuration file is missing: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = self._yaml.load(stream)
        except Exception as exc:
            raise ConfigurationError(f"Cannot read YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("The root settings.yml element must be a mapping.")
        # Upgrade every older profile from the packaged production template.
        # The recursive merge only fills absent keys: station-specific values,
        # comments and ordering remain intact, while new devices and newly
        # introduced nested fields arrive without per-device migration code.
        defaults = self._template_raw()
        defaults_added = self._merge_missing_defaults(raw, defaults)
        known_issues_repaired = self.repair_known_issues(raw)
        safety_limits_narrowed = self.repair_legacy_keithley_limits(raw, defaults)
        try:
            settings = StationSettings.model_validate(raw)
        except ValidationError as exc:
            raise ConfigurationError(format_settings_validation_error(exc)) from exc
        backup = None
        if defaults_added or known_issues_repaired or safety_limits_narrowed:
            # Persist only after the merged document validates.  This is a
            # schema upgrade, not an operator configuration change.
            # _atomic_dump also keeps a .bak.
            backup = self._atomic_dump(raw)
        loaded = LoadedSettings(settings=settings, raw=raw, source=self.path)
        report = SettingsRepairReport(
            source=self.path,
            created=created,
            defaults_added=defaults_added,
            known_issues_repaired=known_issues_repaired,
            safety_limits_narrowed=safety_limits_narrowed,
            backup=backup,
        )
        return loaded, report

    def _template_raw(self) -> dict[str, Any]:
        try:
            text = files("app").joinpath("resources/settings.template.yml").read_text(
                encoding="utf-8"
            )
            defaults = self._yaml.load(text)
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            raise ConfigurationError("The packaged settings template is missing.") from exc
        if not isinstance(defaults, dict):
            raise ConfigurationError("The packaged settings template root must be a mapping.")
        return defaults

    @classmethod
    def _merge_missing_defaults(
        cls,
        target: dict[str, Any],
        defaults: dict[str, Any],
    ) -> bool:
        """Recursively add absent schema defaults without replacing user data."""

        changed = False
        for key, default in defaults.items():
            if key not in target:
                target[key] = deepcopy(default)
                changed = True
                continue
            current = target[key]
            if isinstance(current, dict) and isinstance(default, dict):
                changed = cls._merge_missing_defaults(current, default) or changed
        return changed

    @_serialized_settings_io
    def ensure_exists(self) -> bool:
        """Create a local station configuration from the packaged template once."""

        if self.path.exists():
            return False
        try:
            template = files("app").joinpath("resources/settings.template.yml").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise ConfigurationError("The packaged settings template is missing.") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(template)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                return False
            self._replace_with_windows_retry(temp_path, self.path)
            return True
        finally:
            temp_path.unlink(missing_ok=True)

    def save(self, settings: StationSettings) -> None:
        """Persist a validated station configuration."""

        self.save_raw(settings.model_dump(mode="python"))

    @_serialized_settings_io
    def save_raw(self, raw: dict[str, Any]) -> StationSettings:
        """Validate then atomically persist a UI-edited round-trip YAML document."""

        payload = deepcopy(raw)
        self.repair_known_issues(payload)
        try:
            settings = StationSettings.model_validate(payload)
        except ValidationError as exc:
            raise ConfigurationError(format_settings_validation_error(exc)) from exc
        self._atomic_dump(payload)
        return settings

    @_serialized_settings_io
    def update_raw(
        self,
        transform: Callable[[dict[str, Any], StationSettings], dict[str, Any]],
    ) -> tuple[StationSettings, dict[str, Any]]:
        """Atomically load, transform, validate and save the latest document."""

        loaded = self.load()
        payload = transform(deepcopy(loaded.raw), loaded.settings)
        persisted = self.save_raw(payload)
        return persisted, payload

    @staticmethod
    def repair_known_issues(raw: dict[str, Any]) -> bool:
        """Repair deterministic legacy/contradictory values without inventing limits.

        An empty expected-input declaration cannot coexist with a request to
        require that declaration.  Disabling the requirement is the schema's
        explicit opt-out; it does not create a fictitious RF power limit and
        does not enable acquisition by itself.

        """

        changed = False
        profile = raw.get("profile")
        if isinstance(profile, dict):
            for obsolete_key in (
                "state",
                "approved_by",
                "approved_at",
                "approval_note",
                "lock_outputs_when_unverified",
                "lock_outputs_when_unapproved",
            ):
                if obsolete_key in profile:
                    del profile[obsolete_key]
                    changed = True

        application = raw.get("application")
        if (
            isinstance(application, dict)
            and "require_operator_confirmation_before_arming" in application
        ):
            del application["require_operator_confirmation_before_arming"]
            changed = True

        devices = raw.get("devices")
        rigol = devices.get("rigol") if isinstance(devices, dict) else None
        rigol_safety = rigol.get("safety") if isinstance(rigol, dict) else None
        if isinstance(rigol_safety, dict):
            # Recipe/DUT impedance declarations were removed.  The remaining
            # Rigol safety checks use only configured output limits and fixed
            # hardware characteristics, so legacy values must not prevent an
            # existing station profile from loading.
            if "require_declared_dut_impedance" in rigol_safety:
                del rigol_safety["require_declared_dut_impedance"]
                changed = True
            channels = rigol_safety.get("channels")
            if isinstance(channels, dict):
                for channel in channels.values():
                    limits = (
                        channel.get("lab_limits")
                        if isinstance(channel, dict)
                        else None
                    )
                    if isinstance(limits, dict) and "declared_dut_impedance" in limits:
                        del limits["declared_dut_impedance"]
                        changed = True
        anritsu = devices.get("anritsu") if isinstance(devices, dict) else None
        generator = (
            anritsu.get("signal_generator") if isinstance(anritsu, dict) else None
        )
        if isinstance(generator, dict) and "arm_ttl" in generator:
            del generator["arm_ttl"]
            changed = True

        try:
            safety = raw["devices"]["anritsu"]["safety"]
        except (KeyError, TypeError):
            return changed
        if not isinstance(safety, dict):
            return changed

        documented_reference = {"min": "-120 dBm", "max": "+50 dBm"}
        if safety.get("reference_level") != documented_reference:
            safety["reference_level"] = documented_reference
            changed = True
        return changed

    @classmethod
    def repair_legacy_keithley_limits(
        cls,
        raw: dict[str, Any],
        defaults: dict[str, Any],
    ) -> bool:
        """Replace incompatible legacy Keithley envelopes with safe template ranges.

        This migration only narrows a source or compliance range when its
        existing trip envelope does not cover it. It strictly narrows rather
        than expands any user envelope.
        """

        try:
            channels = raw["devices"]["keithley"]["safety"]["channels"]
            default_channels = defaults["devices"]["keithley"]["safety"][
                "channels"
            ]
        except (KeyError, TypeError):
            return False
        if not isinstance(channels, dict) or not isinstance(default_channels, dict):
            return False

        changed = False
        source_pairs = (
            ("source_current", "measured_current_trip", DIMENSION_CURRENT),
            ("source_voltage", "measured_voltage_trip", DIMENSION_VOLTAGE),
        )
        compliance_pairs = (
            ("current_compliance", "measured_current_trip", DIMENSION_CURRENT),
            ("voltage_compliance", "measured_voltage_trip", DIMENSION_VOLTAGE),
        )
        for channel_name, channel in channels.items():
            default_channel = default_channels.get(channel_name)
            if not isinstance(channel, dict) or not isinstance(default_channel, dict):
                continue
            limits = channel.get("lab_limits")
            default_limits = default_channel.get("lab_limits")
            if not isinstance(limits, dict) or not isinstance(default_limits, dict):
                continue

            channel_narrowed = False
            for value_name, trip_name, dimension in source_pairs:
                value = limits.get(value_name)
                trip = limits.get(trip_name)
                safe_default = default_limits.get(value_name)
                if not all(isinstance(item, dict) for item in (value, trip, safe_default)):
                    continue
                if value.get("enabled", True) is False or trip.get("enabled", True) is False:
                    continue
                if cls._range_contains(trip, value, dimension):
                    continue
                if not cls._range_contains(trip, safe_default, dimension):
                    continue
                candidate = cls._intersect_ranges(value, safe_default, dimension)
                if candidate is not None and cls._range_contains(trip, candidate, dimension):
                    limits[value_name] = candidate
                    changed = True
                    channel_narrowed = True

            for value_name, trip_name, dimension in compliance_pairs:
                value = limits.get(value_name)
                trip = limits.get(trip_name)
                safe_default = default_limits.get(value_name)
                if not all(isinstance(item, dict) for item in (value, trip, safe_default)):
                    continue
                if value.get("enabled", True) is False or trip.get("enabled", True) is False:
                    continue
                if cls._trip_covers_magnitude(trip, value, dimension):
                    continue
                if not cls._trip_covers_magnitude(trip, safe_default, dimension):
                    continue
                candidate = cls._intersect_ranges(value, safe_default, dimension)
                if candidate is not None and cls._trip_covers_magnitude(trip, candidate, dimension):
                    limits[value_name] = candidate
                    changed = True
                    channel_narrowed = True
            if channel_narrowed and isinstance(default_channel.get("defaults"), dict):
                # Legacy form defaults may now lie outside the narrowed lab
                # envelope.  Reset only this channel's form snapshot; this also
                # guarantees that its persisted default output state is OFF.
                channel["defaults"] = deepcopy(default_channel["defaults"])
        return changed

    @classmethod
    def _intersect_ranges(
        cls,
        current: dict[str, Any],
        safe: dict[str, Any],
        dimension: str,
    ) -> dict[str, Any] | None:
        """Intersect current range with safe default so that neither bound is widened."""
        try:
            c_min_q = parse_quantity(current["min"], dimension)
            c_max_q = parse_quantity(current["max"], dimension)
            s_min_q = parse_quantity(safe["min"], dimension)
            s_max_q = parse_quantity(safe["max"], dimension)
        except (KeyError, TypeError, ValueError):
            return None

        new_min = max(c_min_q.si_value, s_min_q.si_value)
        new_max = min(c_max_q.si_value, s_max_q.si_value)

        if new_min > new_max:
            return None

        result = deepcopy(safe)
        if math.isclose(new_min, s_min_q.si_value, abs_tol=1e-12):
            result["min"] = safe["min"]
        elif math.isclose(new_min, c_min_q.si_value, abs_tol=1e-12):
            result["min"] = current["min"]
        else:
            unit = "A" if dimension == DIMENSION_CURRENT else "V"
            result["min"] = f"{new_min:.12g} {unit}"

        if math.isclose(new_max, s_max_q.si_value, abs_tol=1e-12):
            result["max"] = safe["max"]
        elif math.isclose(new_max, c_max_q.si_value, abs_tol=1e-12):
            result["max"] = current["max"]
        else:
            unit = "A" if dimension == DIMENSION_CURRENT else "V"
            result["max"] = f"{new_max:.12g} {unit}"

        if "max_abs" in current:
            result["max_abs"] = safe.get("max_abs", result["max"])
        return result

    @staticmethod
    def _range_contains(
        outer: dict[str, Any],
        inner: dict[str, Any],
        dimension: str,
    ) -> bool:
        """Compare explicit-unit ranges in SI without guessing malformed values."""

        try:
            outer_min = parse_quantity(outer["min"], dimension).si_value
            outer_max = parse_quantity(outer["max"], dimension).si_value
            inner_min = parse_quantity(inner["min"], dimension).si_value
            inner_max = parse_quantity(inner["max"], dimension).si_value
        except (KeyError, TypeError, ValueError):
            return False
        return (
            outer_min <= outer_max
            and inner_min <= inner_max
            and outer_min <= inner_min
            and inner_max <= outer_max
        )

    @staticmethod
    def _trip_covers_magnitude(
        trip: dict[str, Any],
        value: dict[str, Any],
        dimension: str,
    ) -> bool:
        """Match the model's absolute compliance-versus-trip invariant in SI."""

        try:
            trip_min = parse_quantity(trip["min"], dimension).si_value
            trip_max = parse_quantity(trip["max"], dimension).si_value
            value_min = parse_quantity(value["min"], dimension).si_value
            value_max = parse_quantity(value["max"], dimension).si_value
        except (KeyError, TypeError, ValueError):
            return False
        return (
            trip_min <= trip_max
            and value_min <= value_max
            and max(abs(value_min), abs(value_max))
            <= max(abs(trip_min), abs(trip_max))
        )

    def _atomic_dump(self, payload: dict[str, Any]) -> Path | None:
        """Replace the file only after a complete temporary YAML write succeeds."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, text=True
        )
        temp_path = Path(temp_name)
        backup: Path | None = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                self._yaml.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                backup = self.path.with_suffix(self.path.suffix + ".bak")
                backup.write_bytes(self.path.read_bytes())
            self._replace_with_windows_retry(temp_path, self.path)
            return backup
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _replace_with_windows_retry(source: Path, destination: Path) -> None:
        """Atomically replace a profile despite short-lived Windows file locks."""

        for attempt in range(6):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))


def repair_settings_file(path: str | Path) -> SettingsRepairReport:
    """Create or safely migrate the settings file required at application startup."""

    return SettingsRepository(path).repair()
