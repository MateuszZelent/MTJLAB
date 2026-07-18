from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
import tempfile

from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_FREQUENCY,
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    QuantityError,
    format_quantity_auto,
    parse_quantity,
)
from app.safety.anritsu import validate_anritsu_spectrum
from app.safety.keithley import KeithleySourceRequest, validate_keithley_source
from app.safety.rigol_current import validate_rigol_frequency_sweep, validate_rigol_waveform
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from tests.helpers import SETTINGS_TEMPLATE, loaded_settings, simulation_settings


class QuantityAndSafetyTests(unittest.TestCase):
    def test_missing_local_settings_are_created_from_safe_packaged_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".config" / "settings.yml"
            repository = SettingsRepository(path)

            loaded = repository.load()

            self.assertTrue(path.is_file())
            self.assertEqual(loaded.settings.profile.state, "unverified")
            self.assertIsNone(loaded.settings.rigol.connection.resource)
            self.assertIsNone(loaded.settings.rigol.identity.expected_serial)
            original = path.read_bytes()
            self.assertFalse(repository.ensure_exists())
            self.assertEqual(path.read_bytes(), original)

    def test_scientific_notation_is_scaled_to_si_and_formatted_automatically(self) -> None:
        self.assertEqual(parse_quantity("1e5 kHz", DIMENSION_FREQUENCY).si_value, 1e8)
        self.assertEqual(parse_quantity("1e8", DIMENSION_FREQUENCY, require_unit=False).si_value, 1e8)
        self.assertEqual(format_quantity_auto(1e8, DIMENSION_FREQUENCY), "100 MHz")

    def test_rigol_frequency_sweep_checks_both_endpoints_and_steps(self) -> None:
        channel = loaded_settings().rigol.safety.channels["1"]
        with self.assertRaises(SafetyViolation):
            validate_rigol_frequency_sweep(
                channel=channel, start_hz=1e6, stop_hz=501e6, duration_s=1.0, steps=100
            )
        with self.assertRaises(SafetyViolation):
            validate_rigol_frequency_sweep(
                channel=channel, start_hz=1e6, stop_hz=2e6, duration_s=1.0, steps=10001
            )

    def test_quantity_requires_explicit_unit(self) -> None:
        with self.assertRaises(QuantityError):
            parse_quantity("10", DIMENSION_CURRENT)
        self.assertAlmostEqual(parse_quantity("10 mA", DIMENSION_CURRENT).si_value, 0.01)
        self.assertAlmostEqual(parse_quantity("100 nW", DIMENSION_POWER).si_value, 1e-7)

    def test_unicode_ohm_units_preserve_si_prefix_case(self) -> None:
        self.assertEqual(parse_quantity("50 Ω", DIMENSION_RESISTANCE).si_value, 50.0)
        self.assertEqual(parse_quantity("1 kΩ", DIMENSION_RESISTANCE).si_value, 1_000.0)
        self.assertEqual(parse_quantity("1 MΩ", DIMENSION_RESISTANCE).si_value, 1_000_000.0)
        milliohm = parse_quantity("1 mΩ", DIMENSION_RESISTANCE)
        self.assertEqual(milliohm.si_value, 0.001)
        self.assertEqual(milliohm.format("mΩ"), "1 mΩ")

    def test_microtesla_accepts_micro_sign_and_greek_mu(self) -> None:
        self.assertEqual(
            parse_quantity("1 µT", DIMENSION_MAGNETIC_FIELD).si_value,
            1e-6,
        )
        self.assertEqual(
            parse_quantity("1 μT", DIMENSION_MAGNETIC_FIELD).si_value,
            1e-6,
        )

    def test_station_profile_is_loaded_and_outputs_locked(self) -> None:
        settings = loaded_settings()
        self.assertTrue(settings.outputs_locked)
        self.assertFalse(settings.rigol.identity.require_serial_match)
        self.assertIsNone(settings.rigol.identity.expected_serial)
        self.assertIsNone(settings.rigol.connection.resource)
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
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
        self.assertAlmostEqual(estimate.peak_estimated_dut_power_w, 5e-9)
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
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

    def test_rigol_estimated_dut_power_limit_is_enforced_independently(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        limits = raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]
        limits["estimated_load_power"] = {"min": "0 W", "max": "1 uW", "max_abs": "1 uW"}
        settings = StationSettings.model_validate(raw)
        with self.assertRaisesRegex(SafetyViolation, "Rigol DUT power"):
            validate_rigol_waveform(
                channel=settings.rigol.safety.channels["1"],
                safety=settings.rigol.safety,
                waveform="SQU",
                frequency="1 kHz",
                high_level="100 mV",
                low_level="-100 mV",
                output_load="HIGHZ",
                dut_min_impedance="50 ohm",
            )

    def test_repository_revokes_approval_for_any_configuration_change(self) -> None:
        source = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
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

    def test_theme_change_does_not_revoke_safety_approval(self) -> None:
        source = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
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
                    "approval_note": "Approved for test.",
                }
            )
            repository.save_raw(approved)
            changed = deepcopy(repository.load().raw)
            changed["ui"]["theme"] = "light"
            saved = repository.save_raw(changed)
            self.assertEqual(saved.profile.state, "approved")

    def test_safety_boundaries_reject_nan_and_infinity(self) -> None:
        settings = loaded_settings()
        with self.assertRaisesRegex(SafetyViolation, "must be finite"):
            validate_keithley_source(
                settings.keithley.safety.channels["B"],
                KeithleySourceRequest("B", "current", float("nan"), 0.067),
            )
        simulated = simulation_settings()
        with self.assertRaisesRegex(SafetyViolation, "finite numbers"):
            validate_anritsu_spectrum(
                simulated.anritsu.safety,
                start_hz=1e6,
                stop_hz=float("inf"),
                reference_level_dbm=0,
                points=101,
            )

    def test_anritsu_rejects_point_counts_not_supported_by_hardware(self) -> None:
        settings = simulation_settings()
        with self.assertRaisesRegex(SafetyViolation, "must be one of"):
            validate_anritsu_spectrum(
                settings.anritsu.safety,
                start_hz=1e6,
                stop_hz=2e6,
                reference_level_dbm=0,
                points=999,
            )

    def test_keithley_preflight_rejects_source_compliance_power(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        limits = raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
        limits["source_current"] = {"min": "-1 A", "max": "1 A"}
        limits["voltage_compliance"] = {"min": "1 mV", "max": "10 V"}
        limits["measured_current_trip"] = {"min": "-1 A", "max": "1 A"}
        limits["measured_voltage_trip"] = {"min": "-10 V", "max": "10 V"}
        limits["max_abs_power"] = "10 mW"
        settings = StationSettings.model_validate(raw)

        with self.assertRaisesRegex(SafetyViolation, "source × compliance"):
            validate_keithley_source(
                settings.keithley.safety.channels["B"],
                KeithleySourceRequest("B", "current", 0.1, 1.0),
            )


if __name__ == "__main__":
    unittest.main()
