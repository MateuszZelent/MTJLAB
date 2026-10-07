"""Operator reference-level limits survive persistence and constrain commands."""

import pytest
from pydantic import ValidationError

from app.domain.errors import ConfigurationError, SafetyViolation
from app.safety.anritsu import validate_anritsu_spectrum
from app.settings.models import StationSettings
from app.settings.repository import SettingsRepository
from tests.helpers import simulation_settings


def raw_with_limits(limits):
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["anritsu"]["safety"]["reference_level"] = limits
    return raw


def check(safety, level):
    validate_anritsu_spectrum(safety, start_hz=1e6, stop_hz=2e6, reference_level_dbm=level, points=101)


def test_narrow_reference_limits_survive_save_and_load_and_are_enforced(tmp_path):
    limits = {"min": "-80 dBm", "max": "-10 dBm", "enabled": True}
    repository = SettingsRepository(tmp_path / "settings.yml")
    saved = repository.save_raw(raw_with_limits(limits))
    loaded = repository.load().settings
    for settings in (saved, loaded):
        assert settings.anritsu.safety.reference_level.model_dump() == limits
        for level in (-80., -10., -40.):
            check(settings.anritsu.safety, level)
        for level in (-81., 0.):
            with pytest.raises(SafetyViolation, match="configured range"):
                check(settings.anritsu.safety, level)


@pytest.mark.parametrize("limits", [
    {"min": "1 V", "max": "2 V"}, {"min": "0 dBm", "max": "-10 dBm"},
    {"min": None, "max": "0 dBm"}, {"min": None, "max": None},
])
def test_invalid_enabled_reference_limits_do_not_authorize_acquisition(limits):
    with pytest.raises((ConfigurationError, ValidationError)):
        StationSettings.model_validate(raw_with_limits(limits))


def test_disabled_operator_limit_keeps_hardware_envelope(tmp_path):
    limits = {"min": "-80 dBm", "max": "-10 dBm", "enabled": False}
    repository = SettingsRepository(tmp_path / "settings.yml")
    repository.save_raw(raw_with_limits(limits))
    safety = repository.load().settings.anritsu.safety
    assert not safety.reference_level.enabled
    check(safety, 0.)
    with pytest.raises(SafetyViolation, match="documented MS2830A range"):
        check(safety, 51.)
