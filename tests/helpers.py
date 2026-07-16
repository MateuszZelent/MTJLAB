"""Common in-memory profile builders for test-only adapters."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from app.settings import SettingsRepository
from app.settings.models import StationSettings


ROOT = Path(__file__).resolve().parents[1]


def loaded_settings() -> StationSettings:
    return SettingsRepository(ROOT / ".config" / "settings.yml").load().settings


def simulation_settings(*, approved: bool = False, anritsu_enabled: bool = True) -> StationSettings:
    raw = deepcopy(SettingsRepository(ROOT / ".config" / "settings.yml").load().raw)
    raw["profile"]["state"] = "approved" if approved else "unverified"
    raw["devices"]["keithley"]["connection"]["resource"] = "TCPIP0::fake-keithley::INSTR"
    raw["devices"]["anritsu"]["connection"]["resource"] = "TCPIP0::fake-anritsu::INSTR"
    raw["devices"]["anritsu"]["safety"]["acquisition_allowed"] = anritsu_enabled
    raw["devices"]["anritsu"]["acquisition"]["single_sweep_mode"] = "standard_scpi_opc"
    raw["devices"]["anritsu"]["safety"]["rf_input"]["max_expected_power_at_connector"] = "-10 dBm"
    raw["devices"]["anritsu"]["safety"]["frequency"] = {"min": "1 Hz", "max": "100 GHz"}
    raw["devices"]["anritsu"]["safety"]["reference_level"] = {"min": "-150 dBm", "max": "30 dBm"}
    return StationSettings.model_validate(raw)
