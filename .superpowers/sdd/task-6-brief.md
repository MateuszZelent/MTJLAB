### Task 6: Replace top ribbon navigation with a Fluent shell

**Files:**
- Modify: `app/ui/shell/main_window.py`
- Modify: `tests/test_fluent_shell.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**
- Consumes:
  - `FluentWindow`.
  - `FluentPageHost`.
  - `StationSafetyStrip`.
  - existing page widgets and `_tab_indices` compatibility mapping.
- Produces:
  - `MainWindow.navigation_routes: dict[str, QWidget]`.
  - `_navigate_to(route: str) -> None`.
  - the existing `tabs` compatibility facade until Dashboard route extraction
    in Task 8.

- [ ] **Step 1: Write failing Fluent shell tests**

Add to `tests/test_fluent_shell.py`:

```python
from qfluentwidgets import FluentWindow

from app.ui.main_window import MainWindow


class MainWindowFluentShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_main_window_uses_fluent_navigation_and_all_routes_exist(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            self.assertIsInstance(window, FluentWindow)
            self.assertEqual(tuple(window.navigation_routes), (
                "dashboard", "rigol", "keithley", "anritsu",
                "moke_box", "lakeshore_gaussmeter", "sweeps", "execution",
                "results", "settings",
            ))
            self.assertIsNotNone(window.safety_strip)
            self.assertFalse(hasattr(window, "ribbon"))
        finally:
            window.close()

    def test_navigation_changes_current_page_without_recreating_controller(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            controller = window._controllers["keithley"]
            window._navigate_to("keithley")
            self.assertIs(window._controllers["keithley"], controller)
            self.assertIs(window.stackedWidget.currentWidget(), window.navigation_routes["keithley"])
        finally:
            window.close()
```

Delete `test_top_ribbon_replaces_side_tabs_and_status_lives_in_menu_corner`
from `tests/test_main_window.py`; the two new shell tests above and the
`StationSafetyStripTests` cover its intentionally replaced presentation and
retain direct E-STOP verification.

- [ ] **Step 2: Run shell tests to verify current ribbon fails the contract**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::MainWindowFluentShellTests tests/test_main_window.py -q
```

Expected: Fluent shell assertions fail because `MainWindow` is still a
`QMainWindow` with a ribbon.

- [ ] **Step 3: Change the shell base and register route hosts**

Change:

```python
class MainWindow(QMainWindow):
```

to:

```python
class MainWindow(FluentWindow):
```

Import `FluentIcon`, `NavigationItemPosition`, and the new host/strip classes.
In `_build()`, preserve page construction and controller wiring, but replace
top-level scroll/tab insertion with:

```python
self.navigation_routes = {}

def register(route: str, widget: QWidget, title: str, icon: FluentIcon) -> None:
    host = FluentPageHost(widget, self)
    host.setObjectName(f"{route}PageHost")
    self.navigation_routes[route] = host
    self.addSubInterface(host, icon, title)
    widget._scroll_area = host.scroll_area
```

Register routes in the exact order asserted by the test with this built-in icon
mapping:

```python
route_icons = {
    "dashboard": FluentIcon.HOME,
    "rigol": FluentIcon.SPEED_HIGH,
    "keithley": FluentIcon.CALORIES,
    "anritsu": FluentIcon.WIFI,
    "moke_box": FluentIcon.SYNC,
    "lakeshore_gaussmeter": FluentIcon.FLAG,
    "sweeps": FluentIcon.DOCUMENT,
    "execution": FluentIcon.PLAY,
    "results": FluentIcon.FOLDER,
    "settings": FluentIcon.SETTING,
}
```

If a named icon is absent from the qualified 1.11.2 public enum, replace only
that value with `FluentIcon.DEVELOPER_TOOLS` and add the chosen mapping to the
shell test. Do not use Pro icons.

Keep `_tab_indices` and a lightweight compatibility adapter only where an
existing method still addresses a historical top-level label. New code uses
route keys.

- [ ] **Step 4: Install and wire the persistent strip**

Create `self.safety_strip = StationSafetyStrip(self)`, place it in the shell
content area so it remains visible across routes, and connect:

```python
self.safety_strip.estop_requested.connect(self._emergency_off_all)
```

Build `StationSafetySnapshot` from the existing settings, access policy,
simulation flag, readiness evaluation, and device states. Refresh it from the
same state-change locations that currently refresh compact ribbon labels.

- [ ] **Step 5: Remove the ribbon and route compatibility methods**

Delete `_build_top_chrome()` and all `ribbon`, `ribbon_group`,
`ribbon_actions`, and `menu_status_area` construction. Update
`_set_current_tab_widget()` and internal page-open methods to route through
`_navigate_to(route)`.

Keep menu actions, event-log dock, QSettings workspace restore/save, theme
actions, and shortcuts.

- [ ] **Step 6: Run focused shell, theme, E-STOP, and workspace tests**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py tests/test_main_window.py -q
```

Expected: all selected tests pass; no controller, assignment, theme, workspace,
or emergency regression is reported.

- [ ] **Step 7: Commit the Fluent shell**

```powershell
git add app/ui/shell/main_window.py tests/test_fluent_shell.py tests/test_main_window.py
git commit -m "feat: replace application ribbon with Fluent navigation"
```

