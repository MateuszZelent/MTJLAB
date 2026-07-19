### Task 3: Bridge Fluent, station QSS, reduced motion, and PyQtGraph

**Files:**
- Create: `app/ui/design_system/motion.py`
- Create: `app/ui/design_system/plot_theme.py`
- Create: `app/ui/design_system/fluent_theme.py`
- Modify: `app/ui/design_system/__init__.py`
- Modify: `app/ui/widgets/spectrum_plot.py`
- Modify: `app/main.py`
- Modify: `tests/test_design_system.py`
- Modify: `tests/test_spectrum_plot.py`

**Interfaces:**
- Consumes:
  - `effective_theme(mode: str) -> str`.
  - `tokens_for(theme: str) -> ThemeTokens`.
- Produces:
  - `motion_enabled() -> bool`.
  - `PlotTheme` frozen dataclass and `plot_theme(tokens: ThemeTokens) -> PlotTheme`.
  - `AppliedTheme` frozen dataclass.
  - `apply_application_theme(application: QApplication, mode: str) -> AppliedTheme`.

- [ ] **Step 1: Write failing theme-bridge and plot-palette tests**

Append to `tests/test_design_system.py`:

```python
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from app.ui.design_system import apply_application_theme, motion_enabled, plot_theme


class ThemeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_theme_bridge_applies_fluent_and_semantic_qss(self) -> None:
        with patch("app.ui.design_system.fluent_theme.setTheme") as set_theme:
            applied = apply_application_theme(self.application, "dark")
        set_theme.assert_called_once()
        self.assertEqual(applied.name, "dark")
        self.assertIn('[safetyState="danger"]', self.application.styleSheet())

    def test_offscreen_platform_disables_motion(self) -> None:
        with patch.dict("os.environ", {"QT_QPA_PLATFORM": "offscreen"}):
            self.assertFalse(motion_enabled())

    def test_plot_theme_comes_from_semantic_tokens(self) -> None:
        tokens = tokens_for("light")
        palette = plot_theme(tokens)
        self.assertEqual(palette.background, tokens.plot_background)
        self.assertEqual(palette.measurement, tokens.plot_measurement)
```

Add to `tests/test_spectrum_plot.py`:

```python
def test_apply_theme_uses_design_system_plot_palette(self) -> None:
    widget = SpectrumPlotWidget()
    widget.apply_theme("light")
    self.assertEqual(widget._theme_name, "light")
    self.assertEqual(widget.plot.backgroundBrush().color().name(), "#ffffff")
```

- [ ] **Step 2: Run the new tests to verify missing interfaces**

Run:

```powershell
python -m pytest tests/test_design_system.py tests/test_spectrum_plot.py -q
```

Expected: collection fails because the theme bridge, plot theme, and motion interfaces are absent.

- [ ] **Step 3: Implement motion and plot-theme value objects**

Create `app/ui/design_system/motion.py`:

```python
from __future__ import annotations

import os

from PySide6.QtCore import QSettings


def motion_enabled() -> bool:
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        return False
    return not bool(QSettings().value("accessibility/reduceMotion", False, bool))
```

Create `app/ui/design_system/plot_theme.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from .tokens import ThemeTokens


@dataclass(frozen=True, slots=True)
class PlotTheme:
    background: str
    axes: str
    grid: str
    reference: str
    measurement: str


def plot_theme(tokens: ThemeTokens) -> PlotTheme:
    return PlotTheme(
        background=tokens.plot_background,
        axes=tokens.plot_axes,
        grid=tokens.plot_grid,
        reference=tokens.plot_reference,
        measurement=tokens.plot_measurement,
    )
```

- [ ] **Step 4: Implement the single application theme bridge**

Create `app/ui/design_system/fluent_theme.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme

from .station_qss import station_qss
from .theme import effective_theme
from .tokens import ThemeTokens, tokens_for


@dataclass(frozen=True, slots=True)
class AppliedTheme:
    name: str
    tokens: ThemeTokens


def apply_application_theme(application: QApplication, mode: str) -> AppliedTheme:
    name = effective_theme(mode)
    tokens = tokens_for(name)
    setTheme(Theme.DARK if name == "dark" else Theme.LIGHT)
    application.setStyleSheet(station_qss(tokens))
    return AppliedTheme(name=name, tokens=tokens)
```

Export the new interfaces from `app/ui/design_system/__init__.py`.

- [ ] **Step 5: Route `app/main.py` through the bridge**

Delete `STYLE` and `LIGHT_STYLE` from `app/main.py`. Import
`apply_application_theme` and replace the nested theme function with:

```python
def apply_theme(mode: str) -> None:
    apply_application_theme(app, mode)
```

Keep the existing `theme_changed` signal connection and OS color-scheme
listener.

- [ ] **Step 6: Route `SpectrumPlotWidget.apply_theme()` through `plot_theme()`**

Replace its hard-coded light/dark palette selection with:

```python
self._theme_name = theme
palette = plot_theme(tokens_for(theme))
self.plot.setBackground(palette.background)
```

Apply `palette.axes` to both axis pens and text pens, `palette.grid` to both
crosshair pens, `palette.reference` to the reference/marker pen, and
`palette.measurement` as the default primary-trace pen. Do not call global
Fluent theme APIs from the plot.

- [ ] **Step 7: Run focused theme and plot tests**

Run:

```powershell
python -m pytest tests/test_design_system.py tests/test_spectrum_plot.py tests/test_main_window.py::MainWindowTests::test_theme_switch_supports_light_dark_and_system_persistence -q
```

Expected: all selected tests pass and no legacy global QSS string is required.

- [ ] **Step 8: Commit the theme bridge**

```powershell
git add app/main.py app/ui/design_system app/ui/widgets/spectrum_plot.py tests/test_design_system.py tests/test_spectrum_plot.py
git commit -m "feat: bridge Fluent themes and plot palette"
```

