from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from app.settings.repository import SettingsRepository
from tests.helpers import SETTINGS_TEMPLATE


class SettingsRepositoryTests(unittest.TestCase):
    def test_legacy_profile_gets_explicit_disabled_lakeshore_section_in_raw_yaml(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["devices"].pop("lakeshore_gaussmeter")
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
