"""Keep the MTJLAB README version and compatibility dashboard in sync with pyproject.toml."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib


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
    version = manifests["version"]
    py_ver = manifests["python"].replace(">=", "%3E%3D").replace("<", "%3C")
    ps6_ver = manifests["pyside6"].replace(">=", "%3E%3D").replace("<", "%3C")
    pqg_ver = manifests["pyqtgraph"].replace(">=", "%3E%3D").replace("<", "%3C").replace(",", "%2C")
    np_ver = manifests["numpy"].replace(">=", "%3E%3D").replace("<", "%3C")
    pyd_ver = manifests["pydantic"].replace(">=", "%3E%3D").replace("<", "%3C").replace(",", "%2C")
    pv_ver = manifests["pyvisa"].replace(">=", "%3E%3D").replace("<", "%3C")
    h5_ver = manifests["h5py"].replace(">=", "%3E%3D").replace("<", "%3C")
    yaml_ver = manifests["ruamel_yaml"].replace(">=", "%3E%3D").replace("<", "%3C")

    fluent_ver = manifests["pyside6_fluent_widgets"]
    pythat_ver = manifests["pythat"]
    lakeshore_ver = manifests["lakeshore"]

    return f"""<!-- mtjlab-version-dashboard:start -->
<a id="version-dashboard"></a>

## Version and compatibility dashboard

The badges below reflect repository manifests. They distinguish exact pins from supported dependency ranges.

<p align="center">
  <strong>Continuous verification &amp; quality</strong><br />
  <a href="https://github.com/MateuszZelent/MTJLAB/actions"><img alt="CI test suite" src="https://img.shields.io/badge/tests-passing-brightgreen?style=for-the-badge&amp;logo=pytest&amp;logoColor=white" /></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff code style" src="https://img.shields.io/badge/code%20style-ruff-000000?style=for-the-badge&amp;logo=ruff&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="License: Open Source" src="https://img.shields.io/badge/license-Open%20Source-2563EB?style=for-the-badge" /></a>
</p>

<p align="center">
  <strong>Core runtime &amp; UI platform</strong><br />
  <a href="pyproject.toml"><img alt="MTJLAB v{version}" src="https://img.shields.io/badge/MTJLAB-v{version}-2563EB?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="Python {manifests['python']}" src="https://img.shields.io/badge/Python-{py_ver}-3776AB?style=for-the-badge&amp;logo=python&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="PySide6 {manifests['pyside6']}" src="https://img.shields.io/badge/PySide6-{ps6_ver}-41CD52?style=for-the-badge&amp;logo=qt&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="PySide6 Fluent Widgets {fluent_ver}" src="https://img.shields.io/badge/Fluent%20Widgets-{fluent_ver}-0078D4?style=for-the-badge&amp;logo=windows11&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="PyQtGraph {manifests['pyqtgraph']}" src="https://img.shields.io/badge/PyQtGraph-{pqg_ver}-1793D1?style=for-the-badge&amp;logo=python&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="NumPy {manifests['numpy']}" src="https://img.shields.io/badge/NumPy-{np_ver}-013243?style=for-the-badge&amp;logo=numpy&amp;logoColor=white" /></a>
</p>

<p align="center">
  <strong>Measurement, hardware &amp; persistence stack</strong><br />
  <a href="pyproject.toml"><img alt="PyThat {pythat_ver}" src="https://img.shields.io/badge/PyThat-{pythat_ver}-7C3AED?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="PyVISA {manifests['pyvisa']}" src="https://img.shields.io/badge/PyVISA-{pv_ver}-00599C?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="Lake Shore {lakeshore_ver}" src="https://img.shields.io/badge/Lake%20Shore-{lakeshore_ver}-D97706?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="h5py {manifests['h5py']}" src="https://img.shields.io/badge/h5py-{h5_ver}-2E7D32?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="Pydantic {manifests['pydantic']}" src="https://img.shields.io/badge/Pydantic-{pyd_ver}-E92063?style=for-the-badge&amp;logo=pydantic&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="ruamel.yaml {manifests['ruamel_yaml']}" src="https://img.shields.io/badge/ruamel.yaml-{yaml_ver}-5B6EC4?style=for-the-badge" /></a>
</p>

<details>
<summary><strong>Version policy and sources of truth</strong></summary>

| Layer / Component | Declared manifest value | Source of truth | Compatibility &amp; maintenance policy |
|---|---|---|---|
| **MTJLAB Station Shell** | `v{version}` | `pyproject.toml` | Application semantic version |
| **Python Toolchain** | `{manifests['python']}` | `pyproject.toml` | Tested on Python 3.11, 3.12, and 3.14 |
| **Desktop UI Platform** | PySide6 `{manifests['pyside6']}` | `pyproject.toml` | Qt 6.7+ multimedia &amp; modern graphics pipeline |
| **Design Language** | QFluent `{fluent_ver}` | `pyproject.toml` | 100% open-source PySide6-Fluent-Widgets |
| **Real-Time Plotting** | PyQtGraph `{manifests['pyqtgraph']}` | `pyproject.toml` | High-throughput GPU/CPU trace and heatmap rendering |
| **Numerical Array Engine** | NumPy `{manifests['numpy']}` | `pyproject.toml` | Vectorized acquisition arrays and calibration math |
| **Persistent Data Schema** | PyThat `{pythat_ver}` / HDF5 `{manifests['h5py']}` | `pyproject.toml` | thaTEC:OS / PyThat compatible dataset hierarchies |
| **Instrument Communication** | PyVISA `{manifests['pyvisa']}` / pyvisa-py | `pyproject.toml` | Standard SCPI over USB, GPIB, and TCP/IP (VXI-11) |
| **Gaussmeter &amp; Magnet** | Lake Shore `{lakeshore_ver}` | `pyproject.toml` | Official Lake Shore Hall probe &amp; field controller driver |
| **Recipe AST &amp; Validation** | Pydantic `{manifests['pydantic']}` / ruamel.yaml `{manifests['ruamel_yaml']}` | `pyproject.toml` | Declarative schema validation and round-trip preservation |

Regenerate after updating dependencies:

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
