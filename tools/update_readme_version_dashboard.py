"""Keep the MTJLAB README version and compatibility dashboard in sync with pyproject.toml."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_DASHBOARD_BLOCK = re.compile(
    r"<!-- mtjlab-version-dashboard:start -->.*?<!-- mtjlab-version-dashboard:end -->",
    re.DOTALL,
)


def extract_manifest_values(repo_root: Path) -> dict[str, str]:
    """Read version specs from pyproject.toml."""
    pyproject_path = repo_root / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    version = project.get("version", "0.1.0")
    requires_python = project.get("requires-python", ">=3.11")

    deps_list = project.get("dependencies", [])
    deps: dict[str, str] = {}
    for entry in deps_list:
        parts = re.split(r"([><=~^!].*)", entry, maxsplit=1)
        pkg = parts[0].strip()
        spec = parts[1].strip() if len(parts) > 1 else "*"
        deps[pkg.lower()] = spec

    return {
        "version": version,
        "python": requires_python,
        "pyside6": deps.get("pyside6", ">=6.7"),
        "pyside6_fluent_widgets": deps.get("pyside6-fluent-widgets", "==1.11.2").lstrip("="),
        "pyqtgraph": deps.get("pyqtgraph", ">=0.13.7,<0.14"),
        "pythat": deps.get("pythat", "==0.2.14").lstrip("="),
        "pyvisa": deps.get("pyvisa", ">=1.14"),
        "lakeshore": deps.get("lakeshore", "==1.10.0").lstrip("="),
        "h5py": deps.get("h5py", ">=3.11"),
        "numpy": deps.get("numpy", ">=1.26"),
        "pydantic": deps.get("pydantic", ">=2.7,<3"),
        "ruamel_yaml": deps.get("ruamel.yaml", ">=0.18"),
    }


def generate_dashboard(manifests: dict[str, str]) -> str:
    """Render the dashboard block with Shields.io badges and sources-of-truth matrix."""
    return f"""<!-- mtjlab-version-dashboard:start -->
<a id="version-dashboard"></a>

<details>
<summary>Version and compatibility dashboard</summary>

These versions and ranges are declared in [`pyproject.toml`](pyproject.toml).
See [`uv.lock`](uv.lock) for the resolved environment.

| Component | Declared version or range |
| --- | --- |
| MTJLAB | `{manifests['version']}` |
| Python | `{manifests['python']}` |
| PySide6 and Fluent Widgets | `{manifests['pyside6']}` and `{manifests['pyside6_fluent_widgets']}` |
| NumPy and PyQtGraph | `{manifests['numpy']}` and `{manifests['pyqtgraph']}` |
| h5py and PyThat | `{manifests['h5py']}` and `{manifests['pythat']}` |
| PyVISA and Lake Shore | `{manifests['pyvisa']}` and `{manifests['lakeshore']}` |
| Pydantic and ruamel.yaml | `{manifests['pydantic']}` and `{manifests['ruamel_yaml']}` |

Regenerate the table after updating the manifest.

```bash
python tools/update_readme_version_dashboard.py --write
```

</details>
<!-- mtjlab-version-dashboard:end -->"""


def update_readme(repo_root: Path, write: bool = False) -> bool:
    """Update or verify the dashboard block in README.md."""
    readme_path = repo_root / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    manifests = extract_manifest_values(repo_root)
    dashboard = generate_dashboard(manifests)

    if not _DASHBOARD_BLOCK.search(content):
        print(f"Error: Could not find dashboard comment markers in {readme_path}", file=sys.stderr)
        return False

    updated_content = _DASHBOARD_BLOCK.sub(dashboard, content)

    if content == updated_content:
        print("README version dashboard is up-to-date.")
        return True

    if write:
        readme_path.write_text(updated_content, encoding="utf-8")
        print(f"Updated version dashboard in {readme_path}.")
        return True

    print("README version dashboard is OUTDATED. Run with --write to update.", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Update or check README version dashboard")
    parser.add_argument("--write", action="store_true", help="Write changes to README.md")
    parser.add_argument("--check", action="store_true", help="Fail if README.md is outdated")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    success = update_readme(repo_root, write=args.write)
    if args.check and not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
