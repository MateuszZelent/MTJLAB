"""Small, fast checks for the dependency boundaries introduced by modularisation."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    return tuple(imported)


class ArchitectureTests(unittest.TestCase):
    def test_generic_device_packages_do_not_exist(self) -> None:
        for package in ("rigol",):
            self.assertFalse(
                (ROOT / "app" / "devices" / package).exists(),
                f"generic device package app.devices.{package} must not exist",
            )

    def test_device_domain_layers_do_not_import_ui(self) -> None:
        """Adapters/models/safety stay usable without Qt or the application shell."""

        device_root = ROOT / "app" / "devices"
        checked: list[Path] = []
        for path in device_root.rglob("*.py"):
            if "ui" in path.relative_to(device_root).parts or path.name == "module.py":
                continue
            checked.append(path)
            self.assertFalse(
                any(name == "app.ui" or name.startswith("app.ui.") for name in _imports(path)),
                path.relative_to(ROOT).as_posix(),
            )
        self.assertTrue(checked)

    def test_legacy_main_window_is_only_a_small_facade(self) -> None:
        facade = ROOT / "app" / "ui" / "main_window.py"
        shell = ROOT / "app" / "ui" / "shell" / "main_window.py"

        self.assertLessEqual(len(facade.read_text(encoding="utf-8").splitlines()), 100)
        self.assertTrue(shell.is_file())
        self.assertIn("class MainWindow", shell.read_text(encoding="utf-8"))

    def test_recipe_page_uses_the_recipe_extension_boundary(self) -> None:
        page = ROOT / "app" / "ui" / "recipes" / "page.py"
        imports = _imports(page)

        self.assertFalse(
            any(name.startswith("app.devices.") for name in imports),
            "device UI belongs behind recipe extensions, not in RecipePage",
        )

    def test_shell_creates_pages_through_device_manifests(self) -> None:
        shell = ROOT / "app" / "ui" / "shell" / "main_window.py"
        imports = _imports(shell)

        self.assertFalse(
            any(name.startswith("app.devices.") and ".ui" in name for name in imports)
        )
        self.assertIn(".create_page(", shell.read_text(encoding="utf-8"))
