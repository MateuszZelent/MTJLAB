"""Absolute voltage-limit normalization must not mutate or widen limits."""

import pytest
from pydantic import ValidationError

from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import DIMENSION_VOLTAGE, parse_quantity
from app.safety.rigol_current import validate_rigol_waveform
from app.settings.models import StationSettings
from app.settings.repository import SettingsRepository
from tests.helpers import simulation_settings


def raw_with_limit(value):
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["combined_voltage_limit"] = value
    return raw


def test_negative_limit_is_normalized_before_model_freezes():
    settings = StationSettings.model_validate(raw_with_limit("-2 mV"))
    channel = settings.rigol.safety.channels["1"]
    assert parse_quantity(channel.lab_limits.combined_voltage_limit, DIMENSION_VOLTAGE).si_value == .002
    with pytest.raises(ValidationError, match="frozen"):
        channel.lab_limits.combined_voltage_limit = "1 V"
    with pytest.raises(SafetyViolation, match="combined_voltage_limit exceeded"):
        validate_rigol_waveform(channel=channel, safety=settings.rigol.safety,
            waveform="SIN", frequency="1 kHz", high_level="2 mV", low_level="-2 mV", output_load="HIGHZ")


@pytest.mark.parametrize("value", ["0 V", "-0 V", "1 A", "nan V", "inf V"])
def test_invalid_voltage_limits_are_rejected(value):
    with pytest.raises((ConfigurationError, ValidationError)):
        StationSettings.model_validate(raw_with_limit(value))


def test_legacy_migration_takes_narrower_absolute_limit(tmp_path):
    raw = raw_with_limit("-2 V")
    limits = raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]
    limits["high_level"] = {"min": "-10 mV", "max": "10 mV", "enabled": True}
    repository = SettingsRepository(tmp_path / "settings.yml")
    saved = repository.save_raw(raw)
    loaded = repository.load().settings
    for settings in (saved, loaded):
        limit = settings.rigol.safety.channels["1"].lab_limits.combined_voltage_limit
        assert parse_quantity(limit, DIMENSION_VOLTAGE).si_value == .01


@pytest.mark.parametrize("channel_id", ["1", "2"])
@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("waveform", ["SIN", "DC"])
def test_800_mv_shared_budget_survives_save_and_reload(tmp_path, channel_id, sign, waveform):
    raw = simulation_settings().model_dump(mode="python")
    for channel in raw["devices"]["rigol"]["safety"]["channels"].values():
        channel["lab_limits"]["combined_voltage_limit"] = "800 mV"
        # Isolate the voltage budget from independent current/power guards.
        channel["lab_limits"]["estimated_load_current"]["enabled"] = False
        channel["lab_limits"]["estimated_load_power"]["enabled"] = False
    repository = SettingsRepository(tmp_path / "settings.yml")
    repository.save_raw(raw)
    settings = repository.load().settings
    channel = settings.rigol.safety.channels[channel_id]
    assert parse_quantity(channel.lab_limits.combined_voltage_limit, DIMENSION_VOLTAGE).si_value == .8
    amplitude_vpp = .6 if waveform == "SIN" else 0.0
    offset_v = sign * (.2 if waveform == "SIN" else .8)
    for increment_v in (-.0001, 0.0, .0001):
        candidate_offset_v = offset_v + sign * increment_v
        request = dict(
            channel=channel, safety=settings.rigol.safety, waveform=waveform,
            frequency="1 kHz", high_level=candidate_offset_v + amplitude_vpp / 2,
            low_level=candidate_offset_v - amplitude_vpp / 2, output_load="HIGHZ",
        )
        if increment_v > 0:
            with pytest.raises(SafetyViolation, match="combined_voltage_limit exceeded"):
                validate_rigol_waveform(**request)
        else:
            validate_rigol_waveform(**request)
