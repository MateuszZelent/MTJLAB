### Task 1: Qualify and lock the Fluent dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `requirements.lock.txt`
- Modify: `tests/test_architecture.py`
- Create: `tests/test_fluent_shell.py`

**Interfaces:**
- Consumes: the existing PySide6 6.7+ runtime constraint.
- Produces: importable `qfluentwidgets.FluentWindow`, `qfluentwidgets.NavigationItemPosition`, `qfluentwidgets.Theme`, and `qfluentwidgets.setTheme`.

- [ ] **Step 1: Write the failing dependency-boundary tests**

Add to `tests/test_fluent_shell.py`:

```python
from __future__ import annotations

import unittest


class FluentDependencyTests(unittest.TestCase):
    def test_required_pyside6_fluent_surface_is_importable(self) -> None:
        from qfluentwidgets import (
            FluentWindow,
            NavigationItemPosition,
            Theme,
            setTheme,
        )

        self.assertTrue(issubclass(FluentWindow, object))
        self.assertTrue(hasattr(NavigationItemPosition, "BOTTOM"))
        self.assertTrue(hasattr(Theme, "LIGHT"))
        self.assertTrue(callable(setTheme))
```

Add to `tests/test_architecture.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify the dependency test fails**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py tests/test_architecture.py -q
```

Expected: `test_required_pyside6_fluent_surface_is_importable` errors with `ModuleNotFoundError: No module named 'qfluentwidgets'`, and the declaration test fails because the dependency is absent.

- [ ] **Step 3: Declare and resolve the standard distribution**

Add this runtime dependency to `pyproject.toml` and `requirements.txt`:

```text
PySide6-Fluent-Widgets==1.11.2
```

Install only this distribution:

```powershell
python -m pip install "PySide6-Fluent-Widgets==1.11.2"
```

Verify the installed metadata and imports:

```powershell
python -c "from importlib.metadata import version; from qfluentwidgets import FluentWindow, NavigationItemPosition, Theme, setTheme; print(version('PySide6-Fluent-Widgets'))"
```

Expected: output is `1.11.2`.

Inspect the installed dependency metadata:

```powershell
python -c "from importlib.metadata import metadata; print(*metadata('PySide6-Fluent-Widgets').get_all('Requires-Dist', []), sep='\n')"
python -m pip show PySide6-Fluent-Widgets
```

Add `PySide6-Fluent-Widgets==1.11.2` to `requirements.lock.txt`. For every
standard-extra runtime requirement printed by the first command that is not
already locked, add the exact installed version reported by
`python -m pip show <distribution>`. Requirements guarded by `extra == "full"`
must not be added. Confirm the qualified lock installs without changing its
existing pins:

```powershell
python -m pip install --dry-run -r requirements.lock.txt
```

Expected: the resolver reports `PySide6-Fluent-Widgets==1.11.2`, keeps the
existing PySide6/PyQtGraph versions, and reports no PyQt or PySide2 Fluent
distribution.

- [ ] **Step 4: Run dependency and architecture tests**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py tests/test_architecture.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the qualified dependency**

```powershell
git add pyproject.toml requirements.txt requirements.lock.txt tests/test_architecture.py tests/test_fluent_shell.py
git commit -m "build: add qualified PySide6 Fluent dependency"
```

