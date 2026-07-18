from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.check_locked_environment import check_environment, parse_lock


class LockedEnvironmentTests(unittest.TestCase):
    def test_parser_reads_python_and_canonical_exact_package_pins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "requirements.lock.txt"
            lock.write_text(
                "# python==3.14.6\nNumPy==2.5.1\nPySide6_Addons==6.11.1\n",
                encoding="utf-8",
            )

            python_version, packages = parse_lock(lock)

        self.assertEqual(python_version, "3.14.6")
        self.assertEqual(packages["numpy"], "2.5.1")
        self.assertEqual(packages["pyside6-addons"], "6.11.1")

    def test_checker_reports_python_and_package_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "requirements.lock.txt"
            lock.write_text(
                "# python==3.14.6\nnumpy==2.5.1\nh5py==3.16.0\n",
                encoding="utf-8",
            )
            with patch(
                "tools.check_locked_environment.metadata.version",
                side_effect=lambda name: {"numpy": "2.5.0", "h5py": "3.16.0"}[name],
            ):
                mismatches = check_environment(lock, python_version="3.14.5")

        self.assertEqual(
            mismatches,
            (
                "Python 3.14.5 != locked 3.14.6",
                "numpy 2.5.0 != locked 2.5.1",
            ),
        )

    def test_parser_resolves_relative_requirement_include(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.lock").write_text(
                "# python==3.14.6\nnumpy==2.5.1\n", encoding="utf-8"
            )
            development = root / "dev.lock"
            development.write_text(
                "-r runtime.lock\npytest==9.1.1\n", encoding="utf-8"
            )

            python_version, packages = parse_lock(development)

        self.assertEqual(python_version, "3.14.6")
        self.assertEqual(packages, {"numpy": "2.5.1", "pytest": "9.1.1"})

    def test_parser_rejects_non_exact_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "bad.lock"
            lock.write_text("# python==3.14.6\nnumpy>=2\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exact"):
                parse_lock(lock)


if __name__ == "__main__":
    unittest.main()
