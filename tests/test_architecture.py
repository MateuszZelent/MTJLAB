"""Small, fast checks for the dependency boundaries introduced by modularisation."""

from __future__ import annotations

import ast
from importlib import import_module
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
    def test_only_pyside6_fluent_distribution_is_declared(self) -> None:
        root = Path(__file__).resolve().parents[1]
        declarations = (
            (root / "pyproject.toml").read_text(encoding="utf-8")
            + (root / "requirements.txt").read_text(encoding="utf-8")
            + (root / "requirements.lock.txt").read_text(encoding="utf-8")
        ).lower()
        self.assertIn("pyside6-fluent-widgets", declarations)
        self.assertNotIn("pyqt-fluent-widgets", declarations)
        self.assertNotIn("pyqt6-fluent-widgets", declarations)
        self.assertNotIn("pyside2-fluent-widgets", declarations)

    def test_generic_device_packages_do_not_exist(self) -> None:
        for package in ("rigol", "keithley", "anritsu"):
            generic_package = ROOT / "app" / "devices" / package
            self.assertFalse(
                any(generic_package.glob("*.py")),
                f"generic device package app.devices.{package} must not exist",
            )

    def test_registered_manifests_are_owned_by_their_concrete_packages(self) -> None:
        from app.devices.registry import built_in_device_registry

        for module in built_in_device_registry().all_modules():
            owner = import_module(
                f"app.devices.{module.implementation_key}.module"
            )
            self.assertIs(owner.MODULE, module)

    def test_source_does_not_import_removed_generic_device_packages(self) -> None:
        forbidden = (
            "app.devices.rigol",
            "app.devices.keithley",
            "app.devices.anritsu",
        )
        for root_name in ("app", "tests"):
            for path in (ROOT / root_name).rglob("*.py"):
                for imported in _imports(path):
                    self.assertFalse(
                        any(
                            imported == prefix
                            or imported.startswith(prefix + ".")
                            for prefix in forbidden
                        ),
                        f"{path.relative_to(ROOT).as_posix()} imports {imported}",
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

    def test_legacy_main_window_facades_are_removed(self) -> None:
        facade = ROOT / "app" / "ui" / "main_window.py"
        shell = ROOT / "app" / "ui" / "shell" / "main_window.py"
        ui_init = ROOT / "app" / "ui" / "__init__.py"

        self.assertFalse(facade.exists())
        self.assertTrue(shell.is_file())
        self.assertIn("class MainWindow", shell.read_text(encoding="utf-8"))
        self.assertNotIn("MainWindow", ui_init.read_text(encoding="utf-8"))

    def test_legacy_root_tk_shell_is_removed(self) -> None:
        """The production entry point must not retain an unsafe legacy shell."""

        self.assertFalse(
            (ROOT / "gui.py").exists(),
            "the historical Tkinter generator must not remain as a runnable UI shell",
        )

    def test_shell_does_not_alias_device_page_buttons_onto_dashboard_cards(self) -> None:
        shell = (
            ROOT / "app" / "ui" / "shell" / "main_window.py"
        ).read_text(encoding="utf-8")
        for name in ("connect_button", "disconnect_button", "test_button"):
            self.assertNotIn(f"card.{name} =", shell)

    def test_shell_uses_native_navigation_sizing_and_theme_actions(self) -> None:
        shell = (
            ROOT / "app" / "ui" / "shell" / "main_window.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("navigationInterface.setFixedWidth", shell)
        self.assertNotIn("self.theme_action =", shell)

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

    def test_recipe_preview_uses_semantic_plot_tokens(self) -> None:
        source = (ROOT / "app" / "ui" / "recipes" / "sweep_editor.py").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(source, r"#[0-9A-Fa-f]{3,8}")
