# Fluent Foundation, Shell, and Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Stages 0–1 of the approved Fluent migration: a locked PySide6 Fluent dependency, semantic design tokens, a theme bridge, Fluent application navigation, a persistent safety strip, compatibility-hosted legacy pages, and a production-quality Dashboard/Find VISA experience.

**Architecture:** `qfluentwidgets` owns generic widget appearance and application navigation, while `app.ui.design_system` owns stable station semantics and PyQtGraph colors. The existing shell continues to compose controllers and pages, but delegates navigation and status presentation to focused UI classes; all hardware, safety, assignment, recipe, and persistence signals retain their current paths.

**Tech Stack:** Python 3.11+, PySide6 6.7+, PySide6-Fluent-Widgets 1.11.2, PyQtGraph 0.13.7, pytest, unittest/QTest.

## Global Constraints

- Use only the standard `PySide6-Fluent-Widgets` distribution; do not install the `full` extra or any PyQt/PySide sibling Fluent distribution.
- Keep PyQtGraph as the plotting implementation.
- Do not rewrite adapters, workers, controllers, safety policy, settings repository, recipe compiler, run engine, or storage.
- QSS is limited to semantic station states that Fluent does not provide.
- Preserve `system`, `light`, and `dark` theme modes and live OS theme changes.
- Preserve F5 for VISA scan and Ctrl+Shift+E for E-STOP.
- No animation may delay emergency, output, connection, or run-control actions.
- Pages must remain usable at 1280×720 and the existing default 1360×880.
- Preserve accessible names, keyboard focus, audit logging, authorization checks, and confirmation paths.
- Work around unrelated dirty-worktree changes; stage and commit only files named by the current task.

## Planned File Structure

### New files

- `app/ui/design_system/tokens.py` — immutable semantic tokens and spacing/radius/type/motion scales.
- `app/ui/design_system/station_qss.py` — compact QSS generated only from semantic station tokens.
- `app/ui/design_system/plot_theme.py` — conversion from semantic tokens to PyQtGraph colors.
- `app/ui/design_system/motion.py` — reduced-motion detection and supported durations.
- `app/ui/design_system/fluent_theme.py` — single bridge from application theme mode to Fluent, QSS, and plots.
- `app/ui/shell/page_host.py` — compatibility wrapper for legacy scrollable pages.
- `app/ui/shell/safety_strip.py` — persistent, presentation-only station safety/status strip.
- `app/ui/dashboard/visa_results.py` — Fluent VISA result-list widget and per-result rows.
- `tests/test_design_system.py` — token, QSS, plot palette, motion, and Fluent theme tests.
- `tests/test_fluent_shell.py` — dependency, page host, navigation, safety strip, and shortcuts.
- `tests/test_visa_results.py` — result states, assignments, permissions, and accessibility.

### Modified files

- `pyproject.toml` — declare the PySide6 Fluent runtime dependency.
- `requirements.txt` — declare the same direct dependency.
- `requirements.lock.txt` — lock the verified dependency closure.
- `app/main.py` — replace the two global legacy QSS strings with the theme bridge.
- `app/ui/design_system/__init__.py` — export the stable design-system facade.
- `app/ui/widgets/spectrum_plot.py` — consume the semantic plot palette.
- `app/ui/shell/main_window.py` — use Fluent navigation, page hosts, and safety strip.
- `app/ui/shell/__init__.py` — export new shell components used in tests.
- `app/ui/dashboard/page.py` — integrate the VISA result list while preserving discovery behavior.
- `app/ui/dashboard/__init__.py` — export VISA result components.
- `tests/test_architecture.py` — enforce dependency and import boundaries.
- `tests/test_main_window.py` — replace ribbon/table presentation assertions with Fluent contracts.

---

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

### Task 2: Implement semantic theme tokens and station QSS

**Files:**
- Create: `app/ui/design_system/tokens.py`
- Create: `app/ui/design_system/station_qss.py`
- Modify: `app/ui/design_system/__init__.py`
- Create: `tests/test_design_system.py`

**Interfaces:**
- Consumes: theme names `"light"` and `"dark"`.
- Produces:
  - `ThemeTokens` frozen dataclass.
  - `tokens_for(theme: str) -> ThemeTokens`.
  - `station_qss(tokens: ThemeTokens) -> str`.
  - `SPACING`, `RADII`, `TYPOGRAPHY`, and `MOTION` immutable mappings.

- [ ] **Step 1: Write failing token and QSS tests**

Create `tests/test_design_system.py`:

```python
from __future__ import annotations

import unittest

from app.ui.design_system import SPACING, ThemeTokens, station_qss, tokens_for


class DesignSystemTests(unittest.TestCase):
    def test_light_and_dark_tokens_expose_all_station_semantics(self) -> None:
        for mode in ("light", "dark"):
            tokens = tokens_for(mode)
            self.assertIsInstance(tokens, ThemeTokens)
            for name in (
                "background", "surface", "surface_raised", "border",
                "text_primary", "text_muted", "accent", "focus",
                "success", "caution", "danger", "neutral",
                "output_active", "compliance", "interlock", "emergency",
                "plot_background", "plot_axes", "plot_grid",
                "plot_reference", "plot_measurement",
            ):
                self.assertRegex(getattr(tokens, name), r"^#[0-9a-fA-F]{6}$")

    def test_spacing_scale_is_monotonic_and_shared(self) -> None:
        self.assertEqual(tuple(SPACING), ("xs", "sm", "md", "lg", "xl"))
        self.assertEqual(sorted(SPACING.values()), list(SPACING.values()))

    def test_station_qss_contains_only_semantic_property_selectors(self) -> None:
        qss = station_qss(tokens_for("dark"))
        for selector in (
            '[safetyState="danger"]',
            '[outputState="active"]',
            '[validationState="error"]',
            '[deviceState="verified"]',
        ):
            self.assertIn(selector, qss)
        self.assertNotIn("QPushButton {", qss)
        self.assertNotIn("QLineEdit {", qss)

    def test_invalid_theme_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "light or dark"):
            tokens_for("sepia")
```

- [ ] **Step 2: Run tests to verify imports fail**

Run:

```powershell
python -m pytest tests/test_design_system.py -q
```

Expected: collection fails because `ThemeTokens`, `tokens_for`, `SPACING`, and `station_qss` do not exist.

- [ ] **Step 3: Implement immutable tokens**

Create `app/ui/design_system/tokens.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


SPACING = MappingProxyType({"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24})
RADII = MappingProxyType({"sm": 4, "md": 8, "lg": 12})
TYPOGRAPHY = MappingProxyType({
    "caption": 9,
    "body": 10,
    "subtitle": 12,
    "title": 20,
})
MOTION = MappingProxyType({"fast_ms": 120, "normal_ms": 180})


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    background: str
    surface: str
    surface_raised: str
    border: str
    text_primary: str
    text_muted: str
    accent: str
    focus: str
    success: str
    caution: str
    danger: str
    neutral: str
    output_active: str
    compliance: str
    interlock: str
    emergency: str
    plot_background: str
    plot_axes: str
    plot_grid: str
    plot_reference: str
    plot_measurement: str


_LIGHT = ThemeTokens(
    background="#f5f7fa", surface="#ffffff", surface_raised="#f9fafb",
    border="#d6dce5", text_primary="#18202a", text_muted="#5f6b7a",
    accent="#0067c0", focus="#005fb8", success="#0f7b4f",
    caution="#8a5d00", danger="#b4233a", neutral="#667085",
    output_active="#b4233a", compliance="#9a6700", interlock="#9a6700",
    emergency="#a4262c", plot_background="#ffffff", plot_axes="#344054",
    plot_grid="#d0d5dd", plot_reference="#7a5af8",
    plot_measurement="#0067c0",
)

_DARK = ThemeTokens(
    background="#111418", surface="#1b1f24", surface_raised="#22272e",
    border="#343a43", text_primary="#f2f4f7", text_muted="#a6adbb",
    accent="#60a5fa", focus="#7dd3fc", success="#43c58a",
    caution="#f4c152", danger="#ff6b7d", neutral="#98a2b3",
    output_active="#ff6b7d", compliance="#f4c152", interlock="#f4c152",
    emergency="#e5484d", plot_background="#111418", plot_axes="#d0d5dd",
    plot_grid="#343a43", plot_reference="#a78bfa",
    plot_measurement="#60a5fa",
)


def tokens_for(theme: str) -> ThemeTokens:
    normalized = theme.strip().lower()
    if normalized == "light":
        return _LIGHT
    if normalized == "dark":
        return _DARK
    raise ValueError("Theme must be light or dark.")
```

- [ ] **Step 4: Implement semantic-only station QSS and facade exports**

Create `app/ui/design_system/station_qss.py`:

```python
from __future__ import annotations

from .tokens import ThemeTokens


def station_qss(tokens: ThemeTokens) -> str:
    return f"""
QLabel[deviceState="verified"], QLabel[outputState="off"] {{
    color: {tokens.success};
}}
QLabel[deviceState="fault"], QLabel[safetyState="danger"],
QLabel[outputState="active"] {{
    color: {tokens.danger};
}}
QLabel[safetyState="caution"], QLabel[deviceState="compliance"] {{
    color: {tokens.caution};
}}
QLineEdit[validationState="error"], QComboBox[validationState="error"],
QSpinBox[validationState="error"] {{
    border: 2px solid {tokens.danger};
}}
"""
```

Update `app/ui/design_system/__init__.py`:

```python
from .station_qss import station_qss
from .theme import effective_theme
from .tokens import MOTION, RADII, SPACING, TYPOGRAPHY, ThemeTokens, tokens_for

__all__ = [
    "MOTION", "RADII", "SPACING", "TYPOGRAPHY", "ThemeTokens",
    "effective_theme", "station_qss", "tokens_for",
]
```

- [ ] **Step 5: Run the design-system tests**

Run:

```powershell
python -m pytest tests/test_design_system.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit semantic tokens**

```powershell
git add app/ui/design_system tests/test_design_system.py
git commit -m "feat: add semantic station design tokens"
```

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

### Task 4: Add a compatibility host for legacy pages

**Files:**
- Create: `app/ui/shell/page_host.py`
- Modify: `app/ui/shell/__init__.py`
- Modify: `tests/test_fluent_shell.py`

**Interfaces:**
- Consumes: an existing top-level `QWidget`.
- Produces:
  - `FluentPageHost(QWidget)`.
  - `FluentPageHost.content: QWidget`.
  - `FluentPageHost.scroll_area: QScrollArea`.

- [ ] **Step 1: Write the failing page-host test**

Add to `tests/test_fluent_shell.py`:

```python
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from app.ui.shell import FluentPageHost


class FluentPageHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_host_preserves_legacy_widget_and_exposes_scroll_area(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(QLabel("Legacy page"))
        host = FluentPageHost(content)
        self.assertIs(host.content, content)
        self.assertIs(host.scroll_area.widget(), content)
        self.assertTrue(host.scroll_area.widgetResizable())
        self.assertEqual(host.objectName(), "fluentPageHost")
```

- [ ] **Step 2: Run the test to verify import failure**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::FluentPageHostTests -q
```

Expected: collection fails because `FluentPageHost` is absent.

- [ ] **Step 3: Implement the compatibility host**

Create `app/ui/shell/page_host.py`:

```python
from __future__ import annotations

from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget


class FluentPageHost(QWidget):
    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fluentPageHost")
        self.content = content
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)
```

Export it from `app/ui/shell/__init__.py`.

- [ ] **Step 4: Run the focused test**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::FluentPageHostTests -q
```

Expected: pass.

- [ ] **Step 5: Commit the compatibility host**

```powershell
git add app/ui/shell/page_host.py app/ui/shell/__init__.py tests/test_fluent_shell.py
git commit -m "feat: add Fluent legacy page host"
```

### Task 5: Build the persistent station safety strip

**Files:**
- Create: `app/ui/shell/safety_strip.py`
- Modify: `app/ui/shell/__init__.py`
- Modify: `tests/test_fluent_shell.py`

**Interfaces:**
- Consumes:
  - `StationSafetySnapshot` values supplied by `MainWindow`.
- Produces:
  - `StationSafetySnapshot` frozen dataclass.
  - `StationSafetyStrip.estop_requested` signal.
  - `StationSafetyStrip.update_snapshot(snapshot: StationSafetySnapshot) -> None`.

- [ ] **Step 1: Write failing safety-strip state and E-STOP tests**

Add to `tests/test_fluent_shell.py`:

```python
from app.ui.shell import StationSafetySnapshot, StationSafetyStrip


class StationSafetyStripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_snapshot_updates_text_and_semantic_properties(self) -> None:
        strip = StationSafetyStrip()
        strip.update_snapshot(StationSafetySnapshot(
            ready=False,
            active_outputs=2,
            profile_state="LOCKED",
            simulation=True,
            actor="operator",
            roles=("operator",),
        ))
        self.assertIn("2 outputs active", strip.outputs.text())
        self.assertEqual(strip.outputs.property("outputState"), "active")
        self.assertEqual(strip.readiness.property("safetyState"), "danger")
        self.assertIn("SIMULATION", strip.mode.text())

    def test_estop_button_emits_without_animation_or_delay(self) -> None:
        strip = StationSafetyStrip()
        emissions: list[bool] = []
        strip.estop_requested.connect(lambda: emissions.append(True))
        strip.estop.click()
        self.assertEqual(emissions, [True])
```

- [ ] **Step 2: Run tests to verify missing types**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::StationSafetyStripTests -q
```

Expected: collection fails because the strip and snapshot do not exist.

- [ ] **Step 3: Implement snapshot and presentation-only strip**

Create `app/ui/shell/safety_strip.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget
from qfluentwidgets import PrimaryPushButton


@dataclass(frozen=True, slots=True)
class StationSafetySnapshot:
    ready: bool
    active_outputs: int
    profile_state: str
    simulation: bool
    actor: str
    roles: tuple[str, ...]


class StationSafetyStrip(QWidget):
    estop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stationSafetyStrip")
        self.readiness = QLabel()
        self.outputs = QLabel()
        self.profile = QLabel()
        self.mode = QLabel()
        self.actor = QLabel()
        self.estop = PrimaryPushButton("E-STOP — disable all outputs")
        self.estop.setAccessibleName("Emergency stop and disable all outputs")
        self.estop.clicked.connect(self.estop_requested)
        layout = QHBoxLayout(self)
        for widget in (
            self.readiness, self.outputs, self.profile,
            self.mode, self.actor,
        ):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addWidget(self.estop)

    def update_snapshot(self, snapshot: StationSafetySnapshot) -> None:
        self.readiness.setText("Station ready" if snapshot.ready else "Station blocked")
        self.readiness.setProperty("safetyState", "ready" if snapshot.ready else "danger")
        self.outputs.setText(
            "Outputs off" if snapshot.active_outputs == 0
            else f"{snapshot.active_outputs} outputs active"
        )
        self.outputs.setProperty(
            "outputState", "off" if snapshot.active_outputs == 0 else "active"
        )
        self.profile.setText(f"Profile {snapshot.profile_state}")
        self.mode.setText("SIMULATION" if snapshot.simulation else "HARDWARE")
        roles = ", ".join(snapshot.roles) or "no role"
        self.actor.setText(f"{snapshot.actor} · {roles}")
        for widget in (self.readiness, self.outputs):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
```

Export both types from `app/ui/shell/__init__.py`.

- [ ] **Step 4: Run the safety-strip tests**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::StationSafetyStripTests -q
```

Expected: pass.

- [ ] **Step 5: Commit the safety strip**

```powershell
git add app/ui/shell/safety_strip.py app/ui/shell/__init__.py tests/test_fluent_shell.py
git commit -m "feat: add persistent station safety strip"
```

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

### Task 7: Build a testable Fluent VISA result list

**Files:**
- Create: `app/ui/dashboard/visa_results.py`
- Modify: `app/ui/dashboard/__init__.py`
- Create: `tests/test_visa_results.py`

**Interfaces:**
- Consumes:
  - `DiscoveredInstrument`.
  - `assignment_allowed: bool`.
  - `configured_device: str | None`.
- Produces:
  - `VisaResultState` frozen dataclass.
  - `VisaResultRow.assignment_requested(str, str, str)` signal.
  - `VisaResultsView.assignment_requested(object)` signal.
  - `VisaResultsView.set_results(states: tuple[VisaResultState, ...]) -> None`.
  - `VisaResultsView.set_assignment_allowed(allowed: bool) -> None`.

- [ ] **Step 1: Write failing state and row interaction tests**

Create `tests/test_visa_results.py`:

```python
from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.devices.discovery import DiscoveredInstrument
from app.ui.dashboard import VisaResultState, VisaResultsView


class VisaResultsViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_states_distinguish_recognized_unknown_unavailable_and_assigned(self) -> None:
        recognized = VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device=None,
        )
        unknown = VisaResultState.from_result(
            DiscoveredInstrument("TCPIP0::1::INSTR", "system", "VENDOR,MODEL,1,1", None),
            configured_device=None,
        )
        unavailable = VisaResultState.from_result(
            DiscoveredInstrument("ASRL1::INSTR", "system", None, None, "timeout"),
            configured_device=None,
        )
        assigned = VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device="keithley",
        )
        self.assertEqual(
            (recognized.status, unknown.status, unavailable.status, assigned.status),
            ("recognized", "unknown", "unavailable", "assigned"),
        )

    def test_assign_emits_existing_payload_shape(self) -> None:
        view = VisaResultsView()
        state = VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device=None,
        )
        emitted: list[object] = []
        view.assignment_requested.connect(emitted.append)
        view.set_results((state,))
        row = view.rows[0]
        row.assignment.setCurrentIndex(row.assignment.findData("keithley"))
        row.assign_button.click()
        self.assertEqual(emitted, [{
            "keithley": ("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1")
        }])

    def test_unavailable_and_permission_denied_rows_cannot_assign(self) -> None:
        view = VisaResultsView()
        unavailable = VisaResultState.from_result(
            DiscoveredInstrument("ASRL1::INSTR", "system", None, None, "timeout"),
            configured_device=None,
        )
        view.set_results((unavailable,))
        self.assertFalse(view.rows[0].assignment.isEnabled())
        self.assertFalse(view.rows[0].assign_button.isEnabled())
        view.set_assignment_allowed(False)
        self.assertFalse(view.rows[0].assign_button.isEnabled())
```

- [ ] **Step 2: Run tests to verify the new view is absent**

Run:

```powershell
python -m pytest tests/test_visa_results.py -q
```

Expected: collection fails because `VisaResultState` and `VisaResultsView` do
not exist.

- [ ] **Step 3: Implement the result state**

Create `app/ui/dashboard/visa_results.py` with:

```python
@dataclass(frozen=True, slots=True)
class VisaResultState:
    result: DiscoveredInstrument
    status: Literal["recognized", "unknown", "unavailable", "assigned"]
    configured_device: str | None

    @classmethod
    def from_result(
        cls,
        result: DiscoveredInstrument,
        *,
        configured_device: str | None,
    ) -> "VisaResultState":
        if configured_device is not None:
            status = "assigned"
        elif result.idn is None:
            status = "unavailable"
        elif result.device is None:
            status = "unknown"
        else:
            status = "recognized"
        return cls(result=result, status=status, configured_device=configured_device)
```

The file begins with these imports:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.devices.discovery import DiscoveredInstrument
```

- [ ] **Step 4: Implement `VisaResultRow` with Fluent controls**

Use `CardWidget`, `BodyLabel`, `CaptionLabel`, `ComboBox`,
`PrimaryPushButton`, and `PushButton` from `qfluentwidgets`. The row must:

- render identity/model, resource, backend, status, and concise error;
- expose a copyable/selectable resource label;
- populate assignment data keys for every assignable registry device supplied
  by the caller;
- disable controls for unavailable, assigned, or unauthorized states;
- use accessible names containing the VISA resource;
- emit the existing `{device: (resource, backend, idn)}` mapping through its
  parent view.

- [ ] **Step 5: Implement `VisaResultsView` ownership and empty state**

Use a `QScrollArea` plus a content layout; expose:

```python
self.rows: list[VisaResultRow]
```

`set_results()` deletes old rows with `deleteLater()`, creates one row per
state, and shows a Fluent `SubtitleLabel("No VISA scan results")` when empty.
`set_assignment_allowed()` updates existing rows without rebuilding them.

Export public types from `app/ui/dashboard/__init__.py`.

- [ ] **Step 6: Run result-list tests**

Run:

```powershell
python -m pytest tests/test_visa_results.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the result-list component**

```powershell
git add app/ui/dashboard/visa_results.py app/ui/dashboard/__init__.py tests/test_visa_results.py
git commit -m "feat: add Fluent VISA result list"
```

### Task 8: Integrate the Fluent Dashboard and preserve discovery behavior

**Files:**
- Modify: `app/ui/dashboard/page.py`
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_visa_results.py`

**Interfaces:**
- Consumes:
  - `VisaResultState.from_result(...)`.
  - `VisaResultsView.set_results(...)`.
  - existing `_configured_device_for(result) -> str | None`.
- Produces:
  - `DashboardPage.visa_results`.
  - `DashboardPage.navigation_pages: dict[str, QWidget]` containing
    `"overview"` and `"discovery"`.
  - existing `assignments_requested` payload and scan worker behavior unchanged.

- [ ] **Step 1: Write failing Dashboard integration tests**

Replace direct six-column table presentation tests in
`tests/test_main_window.py` with:

```python
def test_find_visa_uses_result_cards_and_preserves_assignment_payload(self) -> None:
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        page = window.dashboard
        emitted: list[object] = []
        page.assignments_requested.disconnect(window._save_discovered_assignments)
        page.assignments_requested.connect(emitted.append)
        result = DiscoveredInstrument(
            "GPIB0::22::INSTR",
            "system",
            "Keithley Instruments Inc., Model 2602A, 1291342, 2.1.6",
            "keithley",
        )
        page._scan_completed((result,))
        self.assertEqual(len(page.visa_results.rows), 1)
        row = page.visa_results.rows[0]
        row.assignment.setCurrentIndex(row.assignment.findData("keithley"))
        row.assign_button.click()
        self.assertEqual(emitted, [{
            "keithley": ("GPIB0::22::INSTR", "system", result.idn)
        }])
    finally:
        window.close()

def test_find_visa_scan_states_are_explicit(self) -> None:
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        page = window.dashboard
        self.assertEqual(page.visa_state, "empty")
        page._scan_completed(())
        self.assertEqual(page.visa_state, "empty")
        page._scan_failed("backend unavailable")
        self.assertEqual(page.visa_state, "failed")
        self.assertIn("backend unavailable", page.discovery_info.text())
    finally:
        window.close()

def test_dashboard_exposes_separate_overview_and_discovery_routes(self) -> None:
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        self.assertEqual(tuple(window.dashboard.navigation_pages), (
            "overview", "discovery",
        ))
        self.assertIs(
            window.navigation_routes["overview"].content,
            window.dashboard.navigation_pages["overview"],
        )
        self.assertIs(
            window.navigation_routes["discovery"].content,
            window.dashboard.navigation_pages["discovery"],
        )
    finally:
        window.close()
```

Update assigned-resource tests to assert `VisaResultRow.status` and disabled
Fluent controls rather than `QTableWidget.cellWidget()`.

- [ ] **Step 2: Run focused Dashboard tests to verify current table fails**

Run:

```powershell
python -m pytest tests/test_visa_results.py tests/test_main_window.py -q
```

Expected: new integration tests fail because `visa_results` and `visa_state`
are absent.

- [ ] **Step 3: Replace only the VISA result table**

Refactor `DashboardPage._build` so the existing Overview surface becomes
`self.overview_page`, while Find VISA, Find TCP/IP, and Saved remain together
inside `self.discovery_page`. Expose:

```python
self.navigation_pages = {
    "overview": self.overview_page,
    "discovery": self.discovery_page,
}
```

The coordinator `DashboardPage` continues to own signals, worker state, cards,
and readiness evaluation but is no longer registered as a top-level route.

In the Find VISA surface construction:

- use Fluent `SubtitleLabel`, `BodyLabel`, `PrimaryPushButton`, `PushButton`,
  `IndeterminateProgressBar`, and `InfoBar`;
- create `self.visa_results = VisaResultsView(...)`;
- retain existing `scan_button`, `save_assignments`, and `discovery_info`
  attributes for integration compatibility;
- remove `self.discovery_table`;
- connect `self.visa_results.assignment_requested` to
  `self.assignments_requested`;
- set `self.visa_state = "empty"` initially.

Keep Find TCP/IP and Saved internals behaviorally unchanged inside the
Discovery route.

- [ ] **Step 4: Route worker states into the Fluent view**

In `_scan_visa()`:

```python
self.visa_state = "scanning"
self.visa_progress.start()
```

In `_scan_completed()`:

```python
states = tuple(
    VisaResultState.from_result(
        result,
        configured_device=self._configured_device_for(result),
    )
    for result in self._discovery_results
)
self.visa_results.set_results(states)
self.visa_state = "success" if states else "empty"
self.visa_progress.stop()
```

Keep the existing usable/assignable counts, card-resource refresh, status
signal, permission logic, and batch-save calculation. Derive batch-save
selection from result rows rather than table cell widgets.

In `_scan_failed()`, set `visa_state = "failed"`, stop progress, show a Fluent
error information bar, and keep the existing status/log message.

Update `MainWindow.navigation_routes`: remove the temporary `"dashboard"`
route and register `dashboard.navigation_pages["overview"]` and
`dashboard.navigation_pages["discovery"]` as the first two routes. Update the
Fluent shell test route tuple to the final route order from the specification.

- [ ] **Step 5: Preserve already-assigned and batch-save behavior**

Replace `_set_row_assigned()` with `VisaResultState(configured_device=...)`
state creation. Update `_collect_discovered_assignments()` to iterate
`self.visa_results.rows`, reading each enabled selector and its associated
result. Preserve the exact mapping shape consumed by
`MainWindow._save_discovered_assignments`.

- [ ] **Step 6: Run Dashboard, assignment, and discovery tests**

Run:

```powershell
python -m pytest tests/test_visa_results.py tests/test_visa_discovery.py tests/test_main_window.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Dashboard and Find VISA migration**

```powershell
git add app/ui/dashboard/page.py tests/test_main_window.py tests/test_visa_results.py
git commit -m "feat: migrate Dashboard VISA discovery to Fluent"
```

### Task 9: Verify Stage 0–1 architecture, behavior, and visual quality

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `docs/superpowers/plans/2026-07-19-fluent-foundation-shell-dashboard.md` only to check completed boxes during execution.

**Interfaces:**
- Consumes: all Stage 0–1 components.
- Produces: verified runnable Fluent shell with compatibility-hosted legacy pages.

- [ ] **Step 1: Add final Stage 0–1 architecture assertions**

Add to `tests/test_architecture.py`:

```python
def test_fluent_imports_are_confined_to_ui(self) -> None:
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "app").rglob("*.py"):
        if "qfluentwidgets" not in path.read_text(encoding="utf-8"):
            continue
        relative = path.relative_to(root).as_posix()
        if not relative.startswith("app/ui/"):
            offenders.append(relative)
    self.assertEqual(offenders, [])

def test_legacy_global_stylesheets_are_removed(self) -> None:
    source = (Path(__file__).resolve().parents[1] / "app/main.py").read_text(
        encoding="utf-8"
    )
    self.assertNotIn("STYLE = ", source)
    self.assertNotIn("LIGHT_STYLE = ", source)
```

- [ ] **Step 2: Run static checks**

Run:

```powershell
python -m ruff check app tests
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 3: Run the complete automated suite**

Run:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest -q
```

Expected: complete test suite passes with no new warnings attributable to the
Fluent migration.

- [ ] **Step 4: Run simulated startup smoke tests**

Run:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -c "from PySide6.QtWidgets import QApplication; from app.ui.main_window import MainWindow; app=QApplication([]); window=MainWindow('.config/settings.yml', simulation=True); window.show(); app.processEvents(); assert window.navigation_routes['overview']; assert window.safety_strip; window.close()"
```

Expected: exit 0.

- [ ] **Step 5: Perform visual verification**

Launch the simulation in light and dark modes at 1280×720 and 1360×880. Capture
screenshots of:

- Overview;
- Find VISA empty state;
- scanning state;
- mixed recognized/unknown/unavailable results;
- assigned result;
- active-output safety strip;
- one compatibility-hosted device page.

Inspect for clipping, mixed legacy/Fluent appearance, focus visibility, long
VISA resources, long IDN strings, disabled controls, dock restoration, and
E-STOP visibility. Record any defect as a failing widget test before fixing it.

- [ ] **Step 6: Run the focused suite after visual fixes**

Run:

```powershell
python -m pytest tests/test_design_system.py tests/test_fluent_shell.py tests/test_visa_results.py tests/test_main_window.py tests/test_spectrum_plot.py tests/test_architecture.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Stage 0–1 verification**

```powershell
git add tests/test_architecture.py docs/superpowers/plans/2026-07-19-fluent-foundation-shell-dashboard.md
git commit -m "test: qualify Fluent shell and Dashboard migration"
```

## Follow-up plans

After this plan is complete and verified, create separate implementation plans
for:

1. shared Fluent interaction primitives and removal of remaining generic QSS;
2. Rigol page migration;
3. Keithley page migration;
4. Anritsu page migration;
5. MOKE Box and Lake Shore page migrations;
6. Sweeps and Execution migrations;
7. Results and Settings migrations;
8. final compatibility/QSS removal and full qualification.

Each follow-up plan must begin from the verified interfaces produced here and
must keep the application runnable at every commit.
