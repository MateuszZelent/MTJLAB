from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
import tempfile

from app.domain.errors import SafetyViolation
from app.domain.quantities import DIMENSION_CURRENT, QuantityError, parse_quantity
from app.safety.anritsu import validate_anritsu_spectrum
from app.safety.keithley import KeithleySourceRequest, validate_keithley_source
from app.safety.rigol_current import validate_rigol_waveform
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from tests.helpers import ROOT, loaded_settings, simulation_settings


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

    def test_repository_revokes_approval_for_any_configuration_change(self) -> None:
        source = (ROOT / ".config" / "settings.yml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            path.write_text(source, encoding="utf-8")
            repository = SettingsRepository(path)
            approved = deepcopy(repository.load().raw)
            approved["profile"].update(
                {
                    "state": "approved",
                    "approved_by": "Operator Test",
                    "approved_at": "2026-01-01T00:00:00+00:00",
                    "approval_note": "Zatwierdzono testowo.",
                }
            )
            self.assertEqual(repository.save_raw(approved).profile.state, "approved")

            changed = deepcopy(repository.load().raw)
            changed["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["frequency"]["max"] = "900 kHz"
            saved = repository.save_raw(changed)

            self.assertEqual(saved.profile.state, "unverified")
            self.assertIsNone(saved.profile.approved_by)
            self.assertTrue(saved.outputs_locked)
            self.assertEqual(repository.load().settings.profile.state, "unverified")

    def test_safety_boundaries_reject_nan_and_infinity(self) -> None:
        settings = loaded_settings()
        with self.assertRaisesRegex(SafetyViolation, "skończoną"):
            validate_keithley_source(
                settings.keithley.safety.channels["B"],
                KeithleySourceRequest("B", "current", float("nan"), 0.067),
            )
        simulated = simulation_settings()
        with self.assertRaisesRegex(SafetyViolation, "skończonymi"):
            validate_anritsu_spectrum(
                simulated.anritsu.safety,
                start_hz=1e6,
                stop_hz=float("inf"),
                reference_level_dbm=0,
                points=101,
            )


if __name__ == "__main__":
    unittest.main()
