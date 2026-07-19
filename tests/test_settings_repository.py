from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from app.settings.repository import SettingsRepository
from tests.helpers import SETTINGS_TEMPLATE


class SettingsRepositoryTests(unittest.TestCase):
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
