# Task 6 Report: Native Fluent Application Shell

## Status

Implementation and post-review correction are complete and verified.

The initial Task 6 commit, `12b2db9`, introduced the Fluent routes but was
rejected in review because it nested a `QMainWindow` and retained a tab
compatibility adapter. The corrected production files and tests are currently
uncommitted at the root task's explicit request.

## Corrected architecture

- `MainWindow` is a standard `qfluentwidgets.FluentWindow`.
- `app/main.py` imports `MainWindow` directly from `app.ui.shell`.
- `fluent_content` is a native child `QWidget` in
  `FluentWindow.widgetLayout`; it is not a window and never creates a second
  visible top-level surface.
- The shell contains no nested `QMainWindow`, `QDockWidget`, top-level
  `QTabWidget`, ribbon, `_NavigationTabsAdapter`, or shell compatibility
  facade.
- The former `app.ui.main_window` compatibility module and lazy
  `app.ui.MainWindow` export have been removed. Tests and production callers
  now import concrete modules.
- Dashboard cards no longer expose copied connection-button aliases. The
  shell and tests use the owning connection panels directly.
- Existing controller-backed pages are hosted in exactly ten
  `FluentPageHost` routes: dashboard, Rigol, Keithley, Anritsu, MOKE Box,
  Lakeshore gaussmeter, sweeps, execution, results, and settings.
- Internal navigation uses route keys and native `stackedWidget` semantics.
- Workspace persistence stores the current route, navigation expansion state,
  and the native content and Anritsu splitter states.

## Native shared shell surfaces

- `StationSafetyStrip` remains persistently visible above every route.
- A Fluent `SimpleCardWidget` contains the event log in a vertically resizable
  shell splitter.
- A Fluent caption provides current status messages without a legacy status
  bar.
- A non-selectable Theme item is in the Fluent navigation bottom area,
  immediately above the Settings gear. Its native `RoundMenu` exposes System,
  Light, and Dark actions globally and persists through the existing
  `_set_theme_mode` repository path.
- The Settings route contains no duplicate theme selector, and the title-bar
  application menu contains no duplicate theme submenu.
- Theme consumers use the `theme_actions` mapping directly; the singular
  `theme_action` compatibility alias has been removed.
- The navigation panel defaults to expanded at 248 px on first launch. Users
  can collapse it, and that choice is saved and restored without imposing a
  content-starving minimum width.
- Initial expansion is performed through the native navigation API on the
  first `showEvent`, after the layout is active. Collapse animation then
  physically reduces the navigation interface width and gives the reclaimed
  space to both the Fluent content container and the current page. There is
  no application-level `setFixedWidth` call.
- The title-bar application menu retains event-log visibility and safe
  shutdown actions.
- Ctrl+Shift+E and F5 remain unchanged.

## Safety and responsiveness

- `StationSafetyStrip.estop_requested` remains a direct Qt signal connection
  to `_emergency_off_all`.
- E-STOP remains compact and low emphasis without weakening its behavior.
- Dashboard readiness now emits `readiness_changed` from the single readiness
  refresh path. The shell therefore updates after device state, identity,
  error, audit-health, settings, and preflight mutations instead of showing a
  stale snapshot.
- Safety labels use shrinkable size policies and stretch allocation, so a
  long actor identity cannot force the desktop wider.
- The render regression opens a 1360x880 window and verifies the current
  Fluent page remains visible and wider than 600 px.

## TDD evidence

The initial shell contract and post-review corrections were implemented with
focused red-green cycles:

1. Native content tests failed because `fluent_content` did not exist and the
   tab adapter remained.
2. Readiness propagation failed because the strip stayed "Station ready"
   after dashboard audit health became false.
3. The native event-log test failed because no shell splitter/card existed.
4. Long actor identity failed the safety strip minimum-width bound.
5. The Theme navigation test failed because the `themeMenu` route did not
   exist.
6. Navigation preference restoration failed because the second window always
   expanded.
7. Architecture tests failed while the compatibility module, lazy export, and
   dashboard button aliases still existed.
8. The final P1 tests failed while the application still assigned a fixed
   navigation width and exposed the singular `theme_action` alias.

Each focused test passed after its production correction. Integrated runs
exposed the pre-show navigation lifecycle race: the panel could report
`EXPAND` while its interface container remained at the constructor width of
48 px. Delaying the native expansion until first show fixed the source of the
race without manual width assignment.

## Final verification

```powershell
git diff --check
python -m ruff check app/main.py app/ui/__init__.py app/ui/dashboard/device_card.py app/ui/dashboard/page.py app/ui/shell/main_window.py app/ui/shell/safety_strip.py tests/test_architecture.py tests/test_fluent_shell.py tests/test_main_window.py tests/test_recipe_builder.py tests/test_results_page.py
python -m compileall -q app/main.py app/ui/__init__.py app/ui/dashboard/device_card.py app/ui/dashboard/page.py app/ui/shell/main_window.py app/ui/shell/safety_strip.py tests/test_architecture.py tests/test_fluent_shell.py tests/test_main_window.py tests/test_recipe_builder.py tests/test_results_page.py
python -m pytest tests/test_fluent_shell.py tests/test_main_window.py tests/test_station_readiness.py tests/test_architecture.py tests/test_recipe_builder.py tests/test_results_page.py -q
```

Results:

- `git diff --check`: passed.
- Ruff: `All checks passed!`
- Compileall: passed.
- Pytest: `165 passed in 263.05s` (exit code 0).

The strengthened render regression verifies, after `show()` and event
processing:

- exactly one visible top-level widget;
- that widget is `MainWindow`;
- no visible top-level widget is a `QMainWindow`;
- the current page has adequate rendered width and is visible;
- collapsing the rail makes its rendered width smaller and increases the
  rendered widths of the Fluent content container and current page.

## Corrected files awaiting commit

- `app/main.py`
- `app/ui/__init__.py`
- `app/ui/dashboard/device_card.py`
- `app/ui/dashboard/page.py`
- `app/ui/main_window.py` (deleted)
- `app/ui/shell/main_window.py`
- `app/ui/shell/safety_strip.py`
- `tests/test_architecture.py`
- `tests/test_fluent_shell.py`
- `tests/test_main_window.py`
- `tests/test_recipe_builder.py`
- `tests/test_results_page.py`

Unrelated recovery files and `AGENTS.md` remain unstaged and untouched. This
report is itself an untracked `.superpowers` artifact.

## Concerns

- Legacy page internals remain in `FluentPageHost` under the staged migration
  plan; the application shell itself is native and contains no legacy shell
  embedding.
- QFluentPro was not installed or used. Shell controls use the standard
  PySide6-Fluent-Widgets package.
