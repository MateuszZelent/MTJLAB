from __future__ import annotations

import unittest
from copy import deepcopy

from app.domain.errors import SafetyViolation
from app.domain.quantities import DIMENSION_CURRENT, QuantityError, parse_quantity
from app.safety.rigol_current import validate_rigol_waveform
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from tests.helpers import ROOT, loaded_settings


class QuantityAndSafetyTests(unittest.TestCase):
    def test_quantity_requires_explicit_unit(self) -> None:
        with self.assertRaises(QuantityError):
            parse_quantity("10", DIMENSION_CURRENT)
        self.assertAlmostEqual(parse_quantity("10 mA", DIMENSION_CURRENT).si_value, 0.01)

    def test_station_profile_is_loaded_and_outputs_locked(self) -> None:
        settings = loaded_settings()
        self.assertTrue(settings.outputs_locked)
        self.assertTrue(settings.rigol.identity.require_serial_match)
        self.assertEqual(settings.rigol.identity.expected_serial, "DG1ZA172902039")
        raw = deepcopy(SettingsRepository(ROOT / ".config" / "settings.yml").load().raw)
        raw["profile"]["lock_outputs_when_unverified"] = False
        self.assertTrue(StationSettings.model_validate(raw).outputs_locked)

    def test_rigol_current_estimate_is_limited(self) -> None:
        settings = loaded_settings()
        estimate = validate_rigol_waveform(
            channel=settings.rigol.safety.channels["1"],
            safety=settings.rigol.safety,
            waveform="SQU",
            frequency="1 kHz",
            high_level="1 mV",
            low_level="-1 mV",
            output_load="HIGHZ",
            dut_min_impedance="50 ohm",
        )
        self.assertAlmostEqual(estimate.peak_absolute_current_a, 10e-6)
        raw = deepcopy(SettingsRepository(ROOT / ".config" / "settings.yml").load().raw)
        limits = raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]
        limits["high_level"] = {"min": "-1 V", "max": "1 V"}
        limits["low_level"] = {"min": "-1 V", "max": "1 V"}
        limits["amplitude_vpp"] = {"min": "0 V", "max": "2 V"}
        limits["offset"] = {"min": "-1 V", "max": "1 V"}
        expanded = StationSettings.model_validate(raw)
        with self.assertRaises(SafetyViolation):
            validate_rigol_waveform(
                channel=expanded.rigol.safety.channels["1"],
                safety=expanded.rigol.safety,
                waveform="SQU",
                frequency="1 kHz",
                high_level="1 V",
                low_level="-1 V",
                output_load="HIGHZ",
                dut_min_impedance="1 ohm",
            )


if __name__ == "__main__":
    unittest.main()
