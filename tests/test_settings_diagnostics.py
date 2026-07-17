from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.settings.diagnostics import (
    configuration_diagnostics,
    configuration_sha256,
    redacted_settings,
    structural_diff,
)


class SettingsDiagnosticsTests(unittest.TestCase):
    def test_structural_diff_is_stable_and_reports_nested_changes(self) -> None:
        before = {"devices": {"rigol": {"enabled": True, "models": ["A"]}}}
        after = {"devices": {"rigol": {"enabled": False, "models": ["A", "B"]}}}
        self.assertEqual(
            structural_diff(before, after),
            (
                "~ devices.rigol.enabled: True -> False",
                "+ devices.rigol.models[1] = 'B'",
            ),
        )

    def test_redaction_is_recursive_and_does_not_mutate_source(self) -> None:
        raw = {
            "api_token": "secret-value",
            "nested": {"password": "secret-password", "resource": "GPIB0::1::INSTR"},
        }
        redacted = redacted_settings(raw)
        self.assertEqual(redacted["api_token"], "<redacted>")
        self.assertEqual(redacted["nested"]["password"], "<redacted>")
        self.assertEqual(redacted["nested"]["resource"], "GPIB0::1::INSTR")
        self.assertEqual(raw["api_token"], "secret-value")

    def test_hash_and_diagnostics_are_reproducible(self) -> None:
        self.assertEqual(configuration_sha256({"b": 2, "a": 1}), configuration_sha256({"a": 1, "b": 2}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            path.write_text("schema_version: 1\n", encoding="utf-8")
            diagnostics = configuration_diagnostics(path, {"schema_version": 1})
        self.assertTrue(any("SHA-256" in line for line in diagnostics))
        self.assertTrue(any("not created yet" in line for line in diagnostics))


if __name__ == "__main__":
    unittest.main()
