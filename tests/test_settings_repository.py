from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from app.settings.repository import SettingsRepository


class SettingsRepositoryTests(unittest.TestCase):
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
