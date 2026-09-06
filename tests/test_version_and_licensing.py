"""Verify release version metadata, licensing and provenance (REL-04)."""

from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

from app.version import APP_NAME, __version__, get_version_info


class VersionAndLicensingTests(unittest.TestCase):
    def test_version_info_structure(self) -> None:
        info = get_version_info()
        self.assertEqual(info["app_name"], APP_NAME)
        self.assertEqual(info["version"], __version__)
        self.assertTrue(len(info["commit"]) > 0)
        self.assertTrue(info["full_version"].startswith(__version__))

    def test_pyproject_toml_metadata_matches_version(self) -> None:
        pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
        self.assertTrue(pyproject_path.is_file())
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
        project = data.get("project", {})
        self.assertEqual(project.get("version"), __version__)
        license_info = project.get("license", {})
        self.assertEqual(license_info.get("text"), "GPL-3.0-or-later")

    def test_root_license_file_exists(self) -> None:
        license_path = Path(__file__).resolve().parent.parent / "LICENSE"
        self.assertTrue(license_path.is_file())
        text = license_path.read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3", text)

    def test_third_party_notices_file_exists(self) -> None:
        notices_path = Path(__file__).resolve().parent.parent / "THIRD_PARTY_NOTICES.md"
        self.assertTrue(notices_path.is_file())
        text = notices_path.read_text(encoding="utf-8")
        self.assertIn("PySide6", text)
        self.assertIn("PySide6-Fluent-Widgets", text)
        self.assertIn("PyThat", text)
