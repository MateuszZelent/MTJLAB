# Task 1 report: Qualify and lock the Fluent dependency

## Status

DONE_WITH_CONCERNS. The scoped dependency and architecture tests pass. The full
test suite could not complete within the available command window.

## Red/green evidence

### Red

Command:

```powershell
python -m pytest tests/test_fluent_shell.py tests/test_architecture.py -q
```

Result: `2 failed, 7 passed in 2.23s` (exit 1).

- `test_required_pyside6_fluent_surface_is_importable` raised
  `ModuleNotFoundError: No module named 'qfluentwidgets'`.
- `test_only_pyside6_fluent_distribution_is_declared` failed because
  `pyside6-fluent-widgets` was not declared.

### Qualification and lock evidence

Commands and results:

```powershell
python -m pip install "PySide6-Fluent-Widgets==1.11.2"
```

The initial sandboxed command failed to access PyPI (`WinError 10013`). The
approved retry installed only the required distribution and its resolver
dependencies: `PySide6-Fluent-Widgets-1.11.2`,
`PySideSix-Frameless-Window-0.8.1`, and `darkdetect-0.8.0`.

```powershell
python -c "from importlib.metadata import version; from qfluentwidgets import FluentWindow, NavigationItemPosition, Theme, setTheme; print(version('PySide6-Fluent-Widgets'))"
```

Result: `1.11.2`.

```powershell
python -c "from importlib.metadata import metadata; print(*metadata('PySide6-Fluent-Widgets').get_all('Requires-Dist', []), sep='\n')"
```

Result: standard requirements are `PySide6`, `PySideSix-Frameless-Window >=0.8.0`,
and `darkdetect`; `scipy`, `pillow`, and `colorthief` are guarded by
`extra == 'full'` and were excluded from the lock.

```powershell
python -m pip install --dry-run -r requirements.lock.txt
```

Result: exit 0. The resolver retained existing PySide6/PyQtGraph pins and
reported `PySide6-Fluent-Widgets==1.11.2`; no PyQt or PySide2 Fluent package was
reported.

### Green

Command:

```powershell
python -m pytest tests/test_fluent_shell.py tests/test_architecture.py -q
```

Result: `9 passed in 2.25s` (exit 0).

## Files changed

- `pyproject.toml`: declares `PySide6-Fluent-Widgets==1.11.2`.
- `requirements.txt`: declares the same runtime dependency.
- `requirements.lock.txt`: pins the Fluent package and its newly required
  standard dependencies, `darkdetect==0.8.0` and
  `PySideSix-Frameless-Window==0.8.1`.
- `tests/test_architecture.py`: rejects sibling PyQt/PySide2 Fluent
  distributions.
- `tests/test_fluent_shell.py`: verifies the required `qfluentwidgets` import
  surface.

## Self-review

- The version is exactly `1.11.2` in all three dependency declarations.
- Only the standard PySide6 distribution is declared; no sibling Fluent package
  or `full` extra was added.
- Existing PySide6 (`6.11.1`) and PyQtGraph (`0.13.7`) lock pins remain
  unchanged.
- `git diff --check` completed with exit 0.

## Concern

The full-suite command `python -m pytest -q` was attempted once and reached
65% without failure output before its 120-second timeout. A subsequent
longer attempt was stopped after 60 seconds per parent-task coordination, so
there is no full-suite completion result. This is the only outstanding concern.
