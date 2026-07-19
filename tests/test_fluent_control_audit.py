from __future__ import annotations

import ast
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"

FORBIDDEN_LEGACY_CONTROLS = {
    "QCheckBox",
    "QComboBox",
    "QDialogButtonBox",
    "QFrame",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QMenu",
    "QPlainTextEdit",
    "QProgressBar",
    "QPushButton",
    "QScrollArea",
    "QSpinBox",
    "QTableWidget",
    "QTabWidget",
    "QToolButton",
    "QTreeWidget",
}


class FluentControlAuditTests(unittest.TestCase):
    def test_application_does_not_construct_legacy_visual_controls(self) -> None:
        violations: list[str] = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name in FORBIDDEN_LEGACY_CONTROLS:
                    relative = path.relative_to(PROJECT_ROOT)
                    violations.append(f"{relative}:{node.lineno}: {name}")
        self.assertEqual(
            violations,
            [],
            "Use the corresponding qfluentwidgets control:\n" + "\n".join(violations),
        )

    def test_application_does_not_subclass_legacy_shell_or_frame(self) -> None:
        violations: list[str] = []
        forbidden_bases = {"QFrame", "QMainWindow"}
        for path in sorted(APP_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
                if bases & forbidden_bases:
                    relative = path.relative_to(PROJECT_ROOT)
                    violations.append(
                        f"{relative}:{node.lineno}: {node.name} -> {sorted(bases & forbidden_bases)}"
                    )
        self.assertEqual(violations, [], "Legacy visual base class found:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
