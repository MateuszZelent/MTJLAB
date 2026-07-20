from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from app.settings.repository import SettingsRepository
from tests.helpers import SETTINGS_TEMPLATE


class SettingsRepositoryTests(unittest.TestCase):
    def test_load_repairs_anritsu_rf_requirement_without_inventing_power_limit(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        safety = raw["devices"]["anritsu"]["safety"]
        safety["acquisition_allowed"] = True
        safety["require_rf_input_limit_definition"] = True
        safety["rf_input"]["max_expected_power_at_connector"] = None
        safety["frequency"] = {"min": "10 MHz", "max": "3 GHz"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            repository = SettingsRepository(path)
            repository._atomic_dump(raw)

            loaded = repository.load()

            repaired = loaded.raw["devices"]["anritsu"]["safety"]
            self.assertTrue(repaired["acquisition_allowed"])
            self.assertFalse(repaired["require_rf_input_limit_definition"])
            self.assertIsNone(
                repaired["rf_input"]["max_expected_power_at_connector"]
            )
            self.assertEqual(
                repaired["reference_level"],
                {"min": "-120 dBm", "max": "+50 dBm"},
            )
            persisted = repository._yaml.load(path.read_text(encoding="utf-8"))
            self.assertFalse(
                persisted["devices"]["anritsu"]["safety"]
                ["require_rf_input_limit_definition"]
            )

    def test_legacy_profile_gets_explicit_disabled_moke_box_section(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["devices"].pop("moke_box", None)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            repository = SettingsRepository(path)
            repository._atomic_dump(raw)

            loaded = repository.load()

            profile = loaded.raw["devices"]["moke_box"]
            self.assertFalse(profile["enabled"])
            self.assertIsNone(profile["endpoint"])
            self.assertFalse(profile["protocol_qualified"])
            self.assertFalse(profile["allow_vout_control"])
            self.assertEqual(profile["allowed_vout_channels"], [])
            persisted = repository._yaml.load(path.read_text(encoding="utf-8"))
            self.assertIn("moke_box", persisted["devices"])

    def test_legacy_profile_gets_explicit_disabled_lakeshore_section_in_raw_yaml(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["devices"].pop("lakeshore_gaussmeter")
        raw["devices"]["rigol"]["connection"].pop("timeout")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            repository = SettingsRepository(path)
            repository._atomic_dump(raw)

            loaded = repository.load()

            self.assertIn("lakeshore_gaussmeter", loaded.raw["devices"])
            profile = loaded.raw["devices"]["lakeshore_gaussmeter"]
            self.assertFalse(profile["enabled"])
            self.assertIsNone(profile["resource"])
            self.assertEqual(profile["visa_backend"], "system")
            self.assertEqual(
                loaded.raw["devices"]["rigol"]["connection"]["timeout"],
                "4 s",
            )
            persisted = repository._yaml.load(path.read_text(encoding="utf-8"))
            self.assertIn("lakeshore_gaussmeter", persisted["devices"])
            self.assertEqual(
                persisted["devices"]["rigol"]["connection"]["timeout"],
                "4 s",
            )
            self.assertTrue(path.with_suffix(".yml.bak").exists())

    def test_recursive_upgrade_is_device_agnostic_and_preserves_existing_values(self) -> None:
        current = {"devices": {"future_meter": {"enabled": True}}}
        defaults = {
            "devices": {
                "future_meter": {"enabled": False, "resource": None},
                "future_source": {"enabled": False},
            }
        }

        changed = SettingsRepository._merge_missing_defaults(current, defaults)

        self.assertTrue(changed)
        self.assertTrue(current["devices"]["future_meter"]["enabled"])
        self.assertIsNone(current["devices"]["future_meter"]["resource"])
        self.assertEqual(current["devices"]["future_source"], {"enabled": False})

    def test_load_restores_a_missing_field_for_every_device_from_template(self) -> None:
        template = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw = deepcopy(template)
        removed_paths: dict[str, tuple[str, ...]] = {}

        def remove_first_leaf(mapping: dict[str, object]) -> tuple[str, ...] | None:
            for key, value in list(mapping.items()):
                if isinstance(value, dict) and value:
                    nested = remove_first_leaf(value)
                    if nested is not None:
                        return (key, *nested)
                else:
                    mapping.pop(key)
                    return (key,)
            return None

        def value_at(mapping: dict[str, object], path: tuple[str, ...]) -> object:
            current: object = mapping
            for key in path:
                self.assertIsInstance(current, dict)
                current = current[key]  # type: ignore[index]
            return current

        for device, profile in raw["devices"].items():
            path = remove_first_leaf(profile)
            self.assertIsNotNone(path, f"No removable template field for {device}")
            removed_paths[device] = path or ()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            repository = SettingsRepository(path)
            repository._atomic_dump(raw)

            loaded = repository.load().raw

            for device, removed_path in removed_paths.items():
                expected = value_at(template["devices"][device], removed_path)
                actual = value_at(loaded["devices"][device], removed_path)
                self.assertEqual(actual, expected, f"Migration failed for {device}")

    def test_replace_retries_short_windows_file_lock(self) -> None:
        with patch(
            "app.settings.repository.os.replace",
            side_effect=(PermissionError("locked"), None),
        ) as replace, patch("app.settings.repository.time.sleep") as sleep:
            SettingsRepository._replace_with_windows_retry(
                Path("settings.tmp"), Path("settings.yml")
            )

        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once_with(0.05)


if __name__ == "__main__":
    unittest.main()
