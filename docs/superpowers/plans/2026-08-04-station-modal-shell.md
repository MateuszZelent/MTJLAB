# Station Modal Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the Quick Controls visual surface into a reusable station modal shell, apply it to `StationDialog` and the Anritsu spectrum window, and make Quick Controls slightly larger by default.

**Architecture:** Add a dependency-free `StationModalShell` composition in `app.ui.dialogs` containing the token-aware backdrop, raised Fluent card, shadow, and content layout. `StationDialog` owns the shell as a lowered underlay for backwards-compatible subclasses and exposes it for migrated dialogs; `QuickControlsWindow` uses the shell as its central layout while keeping its `FluentWidget` host and behavior.

**Tech Stack:** Python 3, PySide6, QFluentWidgets, existing station theme tokens, pytest/unittest Qt rendering tests.

## Global Constraints

- Preserve `QDialog` modal semantics, non-modal floating-window behavior, and existing window flags.
- Do not change instrument commands, output safety validation, readback, or acquisition behavior.
- Use existing `ThemeTokens`; do not add per-dialog hard-coded theme colors.
- Render tests must call `show()` and process events before asserting geometry.
- Keep standard Qt widgets only inside functional modal pages; the shell itself remains Fluent-native.
- Follow TDD: each production change has a failing focused test first.

---

### Task 1: Add failing shared-shell and size contracts

**Files:**
- Modify: `tests/test_fluent_dialogs.py`
- Modify: `tests/test_quick_controls.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**
- Consumes: `StationDialog`, `QuickControlsWindow`, and `_AnritsuSpectrumWindow`.
- Produces: tests requiring `StationModalShell`, the larger `550x720` default, and the Anritsu shared surface.

- [ ] **Step 1: Write the failing shell contract.**

Add assertions equivalent to:

```python
dialog = StationDialog()
assert dialog.modal_shell.objectName() == "stationModalShell"
assert dialog.modal_shell.surface.objectName() == "stationModalSurface"
assert dialog.modal_shell.surface.property("stationSurface") == "card"
```

Show the dialog in light and dark themes, process events, and assert the shell and surface have non-zero geometry and different sampled backgrounds.

- [ ] **Step 2: Write the failing Quick Controls default-size contract.**

Construct `QuickControlsWindow`, assert `window.size().toTuple() == (550, 720)` before any saved geometry is restored, and assert `window.surface is window.modal_shell.surface` plus the existing resize/auto-fit behavior.

- [ ] **Step 3: Write the failing Anritsu shell contract.**

Use the existing simulated Anritsu page fixture to open the floating spectrum, show it, process events, and assert:

```python
assert floating.windowModality() == Qt.WindowModality.NonModal
assert floating.modal_shell.surface.isVisibleTo(floating)
assert floating.spectrum.width() > 0
```

- [ ] **Step 4: Run the focused tests and confirm expected failures.**

Run:

```powershell
python -m pytest tests/test_fluent_dialogs.py -k "shell or station_dialog" -q
python -m pytest tests/test_quick_controls.py -k "default or fluent or render" -q
python -m pytest tests/test_fluent_anritsu_moke_lakeshore_pages.py -k "spectrum or floating" -q
```

Expected failures are missing `modal_shell`, the old `520x680` default, and the Anritsu window not exposing the shared surface.

### Task 2: Implement the reusable Fluent surface shell

**Files:**
- Modify: `app/ui/dialogs.py`
- Modify: `app/ui/design_system/station_qss.py` only for shared shell selectors if the existing station card frame cannot cover them.

**Interfaces:**
- Consumes: `ThemeTokens` through the existing application theme observer and QFluent `CardWidget`.
- Produces: `StationModalShell(parent, *, outer_margins, surface_margins)`, with public `backdrop`, `surface`, `surface_layout`, and `content_layout` aliases.

- [ ] **Step 1: Implement token-aware backdrop and shadow helpers.**

Move the Quick Controls gradient/elevation behavior into private shared helpers in `app.ui.dialogs` without importing Quick Controls. Use the widget palette for colors, antialias the rounded backdrop, and apply a `QGraphicsDropShadowEffect` with the same restrained blur/offset for every shell.

- [ ] **Step 2: Implement `StationModalShell`.**

Create the shell as a `QWidget` with `stationSurface="raised"`, a backdrop child, and a `CardWidget` surface. Set default margins to 10 px outside and `(16, 14, 16, 14)` inside, while allowing callers to override them. Expose the surface's `QVBoxLayout` as `surface_layout` and `content_layout`.

- [ ] **Step 3: Run the shell tests.**

Run `python -m pytest tests/test_fluent_dialogs.py -k "shell or station_dialog" -q` and verify the geometry/theme assertions pass.

### Task 3: Integrate the base `StationDialog`

**Files:**
- Modify: `app/ui/dialogs.py`
- Modify: `tests/test_fluent_dialogs.py`

**Interfaces:**
- Consumes: `StationModalShell`.
- Produces: `StationDialog.modal_shell` positioned within the dialog and lowered behind existing subclass content.

- [ ] **Step 1: Create and position the shell in `StationDialog.__init__`.**

Keep `stationSurface="page"`, `WA_StyledBackground`, and palette refresh. Create `self.modal_shell`, set it as a non-interactive underlay, and position it with `self.rect().adjusted(8, 8, -8, -8)` in `showEvent` and `resizeEvent`; call `lower()` after positioning.

- [ ] **Step 2: Preserve existing subclasses.**

Do not replace their `QVBoxLayout(self)` calls. Confirm `StationAlertDialog`, `StationSettingsGuidanceDialog`, `SweepDeviceReadinessDialog`, `LimitEditDialog`, and `FluentRecipeDialog` still show their controls above the shell and keep their existing acceptance/rejection behavior.

- [ ] **Step 3: Run the complete dialog regression target.**

Run `python -m pytest tests/test_fluent_dialogs.py -q`.

### Task 4: Refactor Quick Controls to consume the shell and enlarge its default

**Files:**
- Modify: `app/ui/quick_controls.py`
- Modify: `tests/test_quick_controls.py`

**Interfaces:**
- Consumes: `StationModalShell`.
- Produces: `QuickControlsWindow.modal_shell`, with existing `backdrop`, `surface`, and `surface_layout` references aliased to shell-owned objects.

- [ ] **Step 1: Replace the private backdrop/surface construction.**

Use `StationModalShell` with outer margins `(10, 38, 10, 10)` to preserve the Fluent title-bar clearance. Keep all existing header, scroll area, output rows, resize handles, geometry persistence, and auto-height code; only route the layout through the shell.

- [ ] **Step 2: Change the default size.**

Set the initial size with `self.resize(550, 720)` before saved geometry restoration. Do not change `setMinimumSize(420, 460)`, the 70% available-screen cap, or manual resize behavior.

- [ ] **Step 3: Run Quick Controls tests.**

Run `python -m pytest tests/test_quick_controls.py -q` and confirm the existing synchronization, precision, output, responsive-layout, and resize tests remain green.

### Task 5: Migrate the Anritsu floating spectrum window

**Files:**
- Modify: `app/devices/anritsu_ms2830a/ui/page.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**
- Consumes: `StationDialog.modal_shell` and its `surface_layout`.
- Produces: the same non-modal, always-on-top `_AnritsuSpectrumWindow` behavior with a shared station surface.

- [ ] **Step 1: Move only the spectrum window's presentation layout.**

Replace its direct `QVBoxLayout(self)` with `self.modal_shell.surface_layout`; keep the `Current spectrum` heading, `SpectrumPlotWidget`, status text, close signal, title, flags, and existing dimensions. Do not modify trace data or acquisition calls.

- [ ] **Step 2: Verify normal and narrow rendering.**

Show the window at its normal size and at `580x400`, process events, and assert the plot remains visible with positive geometry, status remains visible, and `windowModality()` is `NonModal`.

- [ ] **Step 3: Run focused Anritsu tests.**

Run `python -m pytest tests/test_fluent_anritsu_moke_lakeshore_pages.py tests/test_main_window.py -k "anritsu.*spectrum or floating.*spectrum" -q`.

### Task 6: Final visual and regression verification

**Files:**
- Modify: `docs/superpowers/specs/2026-08-04-station-modal-shell-design.md` only if implementation reveals a contract correction.

**Interfaces:**
- Consumes: all prior shell and migration changes.
- Produces: verified cross-modal visual contract with no instrument behavior changes.

- [ ] **Step 1: Run the focused UI suites.**

```powershell
python -m pytest tests/test_fluent_dialogs.py tests/test_quick_controls.py tests/test_fluent_anritsu_moke_lakeshore_pages.py -q
```

- [ ] **Step 2: Run static checks.**

```powershell
python -m ruff check app tests
git diff --check
```

- [ ] **Step 3: Audit the final diff.**

Confirm that all shell changes are visual/layout-only, no output or acquisition command was added, modal/non-modal semantics are unchanged, and the 70% Quick Controls cap plus manual resize behavior remain covered by tests.
