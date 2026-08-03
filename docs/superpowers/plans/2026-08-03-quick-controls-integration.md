# Quick Controls Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Quick Controls and the Rigol/Keithley device cards render and edit one synchronized setpoint state, expose the same finite safety ranges through Fluent sliders, and modernize the floating window without weakening hardware safety.

**Architecture:** `QuickControlCoordinator` becomes the shared UI state boundary for draft text, confirmed SI readback, status, and bounds. Device pages publish every valid draft edit and consume coordinator-originated updates; Quick Controls renders the same coordinator state, including continuous slider movement. The existing device adapters remain the final validation and readback authority. A single safety resolver supplies effective bounds to both cards and the floating window.

**Tech Stack:** Python 3, PySide6, QFluentWidgets, Pydantic settings, `parse_quantity`, pytest/unittest Qt tests, Ruff.

## Global Constraints

- Preserve the UI Migration Contract: the floating window and its hosting tree remain Fluent-native; standard Qt controls are used only as functional internals.
- Parse human-authored values with `app.domain.quantities.parse_quantity`; use SI values for slider math and safety comparisons; format only at the UI boundary.
- UI validation is guidance; `QuickControlCoordinator`, device safety validators, and adapters remain authoritative.
- Never issue output-affecting commands from a slider without the existing coordinator/adapter path, readback, and failure handling.
- Preserve current dirty worktree changes in Keithley, MainWindow, workers, and tests unless a directly overlapping line must be adapted for this feature.
- No persistence schema or measurement data format changes.
- Every production change follows a red test, green implementation, and fresh focused verification.

---

### Task 1: Add shared coordinator state for draft/readback synchronization

**Files:**
- Modify: `app/ui/quick_controls.py:42-210`
- Test: `tests/test_quick_controls.py`

**Interfaces:**
- Consumes: `QUICK_CONTROLS_BY_TARGET`, `parse_quantity`, existing controller result/error signals.
- Produces: `QuickControlCoordinator.draft_changed = Signal(str, str, str)`, `QuickControlCoordinator.confirmed_changed = Signal(str, float)`, `draft_text(target) -> str | None`, `publish_draft(target, text, source="device_card")`, `publish_draft_snapshot(values, source="device_card")`, and `confirmed_snapshot(target, value_si, *, adopt_draft: bool)`.

- [ ] **Step 1: Write the failing tests.**

Add tests that establish the state contract without constructing the floating window:

```python
def test_device_card_draft_is_available_to_quick_controls_state(self) -> None:
    coordinator = QuickControlCoordinator(
        {"rigol": _FakeController(), "keithley": _FakeController()},
        QWidget(),
    )
    changes = []
    coordinator.draft_changed.connect(lambda *event: changes.append(event))

    coordinator.publish_draft("rigol.1.frequency", "12 kHz")

    self.assertEqual(coordinator.draft_text("rigol.1.frequency"), "12 kHz")
    self.assertEqual(changes[-1], ("rigol.1.frequency", "12 kHz", "device_card"))


def test_readback_does_not_overwrite_newer_device_card_draft(self) -> None:
    coordinator = QuickControlCoordinator(
        {"rigol": _FakeController(), "keithley": _FakeController()},
        QWidget(),
    )
    coordinator.publish_draft("rigol.1.frequency", "12 kHz")

    coordinator.confirmed_snapshot(
        "rigol.1.frequency", 10_000.0, adopt_draft=False
    )

    self.assertEqual(coordinator.draft_text("rigol.1.frequency"), "12 kHz")


def test_confirmed_quick_setpoint_adopts_quantized_readback(self) -> None:
    coordinator = QuickControlCoordinator(
        {"rigol": _FakeController(), "keithley": _FakeController()},
        QWidget(),
    )
    coordinator.publish_draft("rigol.1.frequency", "12 kHz", source="quick_controls")

    coordinator.confirmed_snapshot(
        "rigol.1.frequency", 12_001.0, adopt_draft=True
    )

    self.assertIn("kHz", coordinator.draft_text("rigol.1.frequency"))
    self.assertAlmostEqual(
        parse_quantity(
            coordinator.draft_text("rigol.1.frequency"), DIMENSION_FREQUENCY
        ).si_value,
        12_001.0,
    )
```

- [ ] **Step 2: Run the focused tests and verify the intended failure.**

Run:

```powershell
python -m pytest tests/test_quick_controls.py -k "draft or confirmed" -q
```

Expected: FAIL because the coordinator has no draft/readback state API.

- [ ] **Step 3: Implement the minimum state boundary.**

Initialize `_draft_texts`, `_confirmed_values`, and `_dirty_drafts` in the coordinator. `publish_draft` must parse using the registered descriptor before storing, reject unknown targets, increment no hardware queue, and emit `draft_changed`. `confirmed_snapshot` must always store the confirmed SI value; it may replace the draft only when `adopt_draft=True` or the target is not dirty. Format adopted values with `format_quantity_auto` and the descriptor dimension. Add `draft_text()` and `publish_draft_snapshot()` without importing device page classes.

Update `_result()` so a successful `quick_setpoint` calls `confirmed_snapshot(..., adopt_draft=True)` and then requests `quick_readback`; readback dictionaries call `confirmed_snapshot(..., adopt_draft=False)` for every registered target before emitting the existing `value_read` signal. Preserve the existing per-device queue and error behavior.

- [ ] **Step 4: Run the focused tests and verify green.**

Run:

```powershell
python -m pytest tests/test_quick_controls.py -k "draft or confirmed" -q
```

Expected: PASS with no new warnings.

---

### Task 2: Make effective bounds finite and shared by cards and Quick Controls

**Files:**
- Modify: `app/safety/quick_controls.py:14-95`
- Modify: `app/devices/rigol_dg1000z/ui/page.py:721-751`
- Modify: `app/devices/keithley_2600/ui/page.py:221-249`
- Modify: `app/devices/keithley_2600/ui/page.py:3244-3291`
- Modify: `tests/test_settings_and_safety.py`
- Modify: `tests/test_quick_controls.py`

**Interfaces:**
- Consumes: `StationSettings`, Rigol hardware constants in `app.safety.rigol_current`, Keithley hardware constants in `app.safety.keithley`.
- Produces: a `QuickControlSafetyBound` with finite `minimum_si`/`maximum_si` for every registered slider target and a single `quick_control_bound(settings, target, ...)` path reused by device `LimitField` wrappers.

- [ ] **Step 1: Write failing bounds tests.**

Add a test that disables the editable limits and asserts every target has finite numeric bounds, plus a parity test for the page-facing lookup:

```python
def test_disabled_quick_control_limits_resolve_to_finite_hardware_ranges(self) -> None:
    raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
    for channel in raw["devices"]["rigol"]["safety"]["channels"].values():
        for name in ("frequency", "high_level", "low_level", "amplitude_vpp", "offset"):
            channel["lab_limits"][name]["enabled"] = False
    for channel in raw["devices"]["keithley"]["safety"]["channels"].values():
        for name in ("source_current", "source_voltage"):
            channel["lab_limits"][name]["enabled"] = False

    bounds = quick_control_safety_bounds(StationSettings.model_validate(raw))

    for target, bound in bounds.items():
        with self.subTest(target=target):
            self.assertTrue(math.isfinite(bound.minimum_si))
            self.assertTrue(math.isfinite(bound.maximum_si))
            self.assertLess(bound.minimum_si, bound.maximum_si)
```

Add a page test that compares the visible `LimitField` badge text for active Rigol/Keithley level controls to the same resolver output.

- [ ] **Step 2: Run the bounds tests and verify failure.**

Run:

```powershell
python -m pytest tests/test_settings_and_safety.py -k "quick_control or disabled_quick" -q
```

Expected: the existing `HARDWARE` assertions or missing finite values fail, demonstrating the old split contract.

- [ ] **Step 3: Implement the shared finite resolver.**

Use the existing documented constants rather than new UI constants:

- Keithley current: `±KEITHLEY_2602A_MAX_CURRENT_RANGE_A`.
- Keithley voltage: `±KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V`.
- Rigol frequency: `0`/the documented resolution through `rigol_hardware_frequency_max_hz()`.
- Rigol high/low/offset: the documented open-circuit peak envelope from `rigol_current`.
- Rigol amplitude: the documented minimum Vpp and conservative maximum envelope.

Intersect these hardware bounds with configured `min`, `max`, and `max_abs`. Keep the existing adapter-level coupled validation intact. Add an explicit resolver by target so page mapping is not duplicated; page `_rigol_limit_values()` and both Keithley `level` limit paths must call it.

Update the old disabled-limit test to assert the numeric hardware text and add source metadata if useful for the UI. Preserve non-quick `LimitField` behavior such as `DISABLED`, `N/A`, and range-selector limits.

- [ ] **Step 4: Run all affected safety and quantity tests.**

Run:

```powershell
python -m pytest tests/test_settings_and_safety.py tests/test_quick_controls.py -q
```

Expected: all affected tests pass and no adapter safety test is skipped.

---

### Task 3: Add a Fluent quantity slider with unit-safe linear/log mapping

**Files:**
- Create: `app/ui/widgets/quick_quantity_slider.py`
- Modify: `app/ui/widgets/__init__.py`
- Test: `tests/test_quick_quantity_slider.py`

**Interfaces:**
- Consumes: `QuickControlDescriptor`, `QuickControlSafetyBound`, `parse_quantity`, `format_quantity_auto`, QFluentWidgets `Slider`.
- Produces: `QuickQuantitySlider(QWidget)` with `set_bounds(bound)`, `set_value_si(value_si)`, `value_si()`, `draft_value_changed`, `commit_requested`, `minimum_label`, `maximum_label`, `slider`.

- [ ] **Step 1: Write failing mapping and widget tests.**

Test pure mapping helpers first:

```python
def test_frequency_mapping_is_logarithmic_and_round_trips(self) -> None:
    mapping = QuantitySliderMapping(
        minimum_si=1.0,
        maximum_si=30_000_000.0,
        logarithmic=True,
    )
    midpoint = mapping.from_position(500, 1000)
    self.assertGreater(midpoint, 1_000.0)
    self.assertLess(midpoint, 1_000_000.0)
    self.assertAlmostEqual(mapping.to_position(midpoint, 1000), 500, delta=1)


def test_slider_commit_emits_explicit_unit_value_inside_shared_bounds(self) -> None:
    widget = QuickQuantitySlider(
        target="keithley.A.current",
        descriptor=QUICK_CONTROLS_BY_TARGET["keithley.A.current"],
        parent=QWidget(),
    )
    widget.set_bounds(_bound(-0.001, 0.001, "-1 mA", "1 mA"))
    widget.set_value_si(0.00025)

    widget.slider.sliderReleased.emit()

    self.assertTrue(widget.last_committed_text.endswith("A"))
    self.assertLessEqual(widget.value_si(), 0.001)
```

- [ ] **Step 2: Run the new tests and verify failure.**

Run:

```powershell
python -m pytest tests/test_quick_quantity_slider.py -q
```

Expected: FAIL because the widget and mapping do not exist.

- [ ] **Step 3: Implement the minimum Fluent widget.**

Use QFluentWidgets `Slider`, a `LineEdit` for explicit-unit entry, and `CaptionLabel` MIN/MAX labels. Derive `step_si` from `quantity_step_si(current_text, dimension)` so trailing zeros are meaningful: `0.00100 A` moves by `0.00001 A`, `10.000 kHz` moves by `1 Hz`. Build a dynamic integer slider range from the number of safe steps; use linear mapping for signed voltage/current/levels and `log10` only for positive frequency, then quantize each candidate to `step_si` and render with `render_quantity_si_like`. During slider movement update the line edit and emit `draft_value_changed` immediately; on `sliderReleased` emit `commit_requested`. Block signals while synchronizing from coordinator state. Never convert an invalid/non-finite bound into a fallback number.

Keep visual styling on `stationSurface`, `stationState`, and existing theme tokens. Add accessible names and tooltips. For a disabled slider show the shared bound text and an actionable reason.

- [ ] **Step 4: Run slider tests and lint the new file.**

Run:

```powershell
python -m pytest tests/test_quick_quantity_slider.py -q
python -m ruff check app/ui/widgets/quick_quantity_slider.py tests/test_quick_quantity_slider.py
```

Expected: PASS and no Ruff violations.

---

### Task 4: Bind Rigol and Keithley card drafts to coordinator state

**Files:**
- Modify: `app/devices/rigol_dg1000z/ui/page.py`
- Modify: `app/devices/keithley_2600/ui/page.py`
- Modify: `app/ui/shell/main_window.py`
- Modify: `tests/test_fluent_device_pages.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**
- Consumes: coordinator `publish_draft`, `draft_changed`, and `confirmed_changed` signals.
- Produces: page signals `quick_control_draft_changed = Signal(str, str)` and page methods `quick_control_draft_snapshot() -> dict[str, str]`, `quick_control_draft_changed_from_coordinator(target, text, source)`.

- [ ] **Step 1: Write failing integration tests.**

Add tests that instantiate pages with fake controllers and verify their visible active fields publish drafts; add a MainWindow test that edits a card field and asserts the Quick Controls row changes without a device call.

```python
def test_rigol_card_draft_is_published_without_hardware_call(self) -> None:
    page = RigolPage(_FakeController(), simulation_settings(), QWidget())
    published = []
    page.quick_control_draft_changed.connect(
        lambda target, text: published.append((target, text))
    )

    page.frequency.setText("12 kHz")
    page.frequency.editingFinished.emit()

    self.assertIn(("rigol.1.frequency", "12 kHz"), published)
```

- [ ] **Step 2: Run the integration tests and verify failure.**

Run:

```powershell
python -m pytest tests/test_fluent_device_pages.py -k "quick or draft" -q
```

Expected: FAIL because pages do not publish draft state.

- [ ] **Step 3: Implement page-side publication and projection.**

Rigol must publish the full active channel snapshot after every valid frequency/level text change, after frequency/level synchronization, and on channel changes. Use the existing `configuration_snapshot_for()` and format each target with explicit units. Keithley must publish the current channel/mode source level immediately on valid text change and publish cached per-channel snapshots when the mode/channel changes. Add a guard so coordinator-originated updates do not echo back as a new draft. The coordinator broadcasts draft changes immediately to the opposite surface; `Configure/Apply` remains the point at which the page asks the device to accept a draft when output is OFF.

Connect the page signals in `MainWindow` to `QuickControlCoordinator.publish_draft`. Connect coordinator `draft_changed` to page projection methods, with source filtering so only Quick Controls-originated changes rewrite the page draft. Seed the coordinator once after page construction with both pages' `quick_control_draft_snapshot()` results. Keep current `quick_setpoint_requested` connections unchanged for energized changes.

On successful page configure/readback, publish confirmed values with `adopt_draft=True`; keep the existing `quick_setpoint_value_read` methods for compatibility while making full Rigol snapshots update all coupled fields.

- [ ] **Step 4: Run focused page/MainWindow tests.**

Run:

```powershell
python -m pytest tests/test_fluent_device_pages.py tests/test_main_window.py -k "quick or draft or readback" -q
```

Expected: PASS, including existing output and readback tests.

---

### Task 5: Rebuild Quick Controls rows and window with Fluent cards/sliders

**Files:**
- Modify: `app/ui/quick_controls.py`
- Modify: `app/ui/design_system/station_qss.py` only if an existing semantic state selector is required; do not add per-page colors.
- Modify: `tests/test_quick_controls.py`

**Interfaces:**
- Consumes: coordinator state/bounds signals and `QuickQuantitySlider`.
- Produces: each `QuickControlRow` exposes `slider`, `value`, `limits`, `status`; `QuickControlsWindow` renders device-group `CardWidget`s and keeps `_rows[target]` compatibility for tests/callers.

- [ ] **Step 1: Write failing rendering and synchronization tests.**

Extend the existing window test:

```python
def test_quick_controls_renders_fluent_sliders_and_shared_draft(self) -> None:
    coordinator = QuickControlCoordinator(
        {"rigol": _FakeController(), "keithley": _FakeController()},
        QWidget(),
        settings=simulation_settings(approved=True),
    )
    coordinator.publish_draft("rigol.1.frequency", "12 kHz")
    window = QuickControlsWindow(coordinator, QWidget())
    window.set_targets(("rigol.1.frequency", "keithley.A.current"))
    window.resize(720, 760)
    window.show()
    self.application.processEvents()

    self.assertTrue(window._rows["rigol.1.frequency"].slider.isVisibleTo(window))
    self.assertEqual(window._rows["rigol.1.frequency"].value.text(), "12 kHz")
    self.assertGreater(window._rows["rigol.1.frequency"].minimumWidth(), 0)
    self.assertEqual(window.controls_scroll.horizontalScrollBar().maximum(), 0)
```

Add a narrow 420px render test and a test that `bounds_changed` updates both row labels and slider range. Add precision tests showing `0.00100 A` advances by `0.00001 A` and `10.000 kHz` advances by `1 Hz`, including a slider drag that updates the opposite card before `sliderReleased`.

- [ ] **Step 2: Run the new tests and verify failure.**

Run:

```powershell
python -m pytest tests/test_quick_controls.py -k "slider or shared_draft or narrow" -q
```

Expected: FAIL because current rows have no slider and use independent default values.

- [ ] **Step 3: Implement row and window composition.**

Replace the current numeric-only row body with `CardWidget`-backed rows containing:

- `StrongBodyLabel` title and a compact target/channel caption;
- `QuickQuantitySlider` with shared MIN/MAX labels;
- explicit-unit `QuantityStepEdit` for precision entry;
- Fluent transparent up/down buttons;
- semantic status label for draft/pending/confirmed/rejected/unknown.

Preserve keyboard arrow stepping, text editing, row ordering, saved targets,
the picker, output card, and `_rows` lookup. Group rows under Rigol and
Keithley cards without introducing a second target registry. When the
coordinator emits `draft_changed`, update the corresponding row unless it is
currently being edited locally. On slider commit call `coordinator.submit`;
on slider draft movement update only the shared draft and status.

Use existing station surface/card tokens and QFluent controls. Keep the
floating window non-modal and topmost. Use a header summary for connected/
pending/readback state, but do not infer safety from that summary.

- [ ] **Step 4: Run focused UI tests and inspect all visible geometry.**

Run:

```powershell
python -m pytest tests/test_quick_controls.py -q
```

Expected: all Quick Controls tests pass; normal and narrow window tests prove
the rows, sliders, labels, output card, and scroll host have non-zero visible
geometry and no horizontal scrolling.

---

### Task 6: Verify coupled updates, safety failures, and full regression

**Files:**
- Modify: `tests/test_adapters_and_runner.py` only if a missing readback contract test is needed.
- Modify: `tests/test_quick_controls.py`
- Modify: `tests/test_settings_and_safety.py`
- Modify: `tests/test_fluent_device_pages.py`

**Interfaces:**
- Consumes: the completed coordinator, bounds resolver, page bindings, and Fluent window.
- Produces: evidence that the full user workflow remains safe and synchronized.

- [ ] **Step 1: Add failing fault-path tests.**

Cover at least:

- out-of-range slider/text input dispatches no controller command;
- failed quick setpoint causes readback/rollback and displays rejected state;
- successful Rigol High/Low update refreshes Amplitude and Offset;
- stale readback does not replace a newer card draft;
- a settings update changes card and floating MIN/MAX together;
- `show()` at 720px and 420px leaves all required controls visible.

- [ ] **Step 2: Run the focused safety and UI tests and verify red before fixes.**

Run:

```powershell
python -m pytest tests/test_quick_controls.py tests/test_settings_and_safety.py tests/test_fluent_device_pages.py -q
```

Expected: each new regression test fails before the corresponding production
behavior exists; existing tests identify any changed contracts explicitly.

- [ ] **Step 3: Fix only implementation/test contract issues.**

Do not weaken assertions to match the implementation. Preserve explicit-unit
parsing, finite bounds, adapter validation, command ordering, and safe-state
behavior. For a failure caused by existing dirty Keithley changes, isolate it
and do not revert those changes.

- [ ] **Step 4: Run the complete verification suite.**

Run:

```powershell
python -m ruff check app tests
python -m pytest -q
```

Record exit codes, test counts, skipped safety tests, and any environment-only
limitations. A completion claim requires both commands to exit successfully
and a fresh render test to pass.

- [ ] **Step 5: Review the final diff against the design.**

Run:

```powershell
git diff --check
git status --short
git diff --stat
```

Confirm that no legacy shell, duplicate target mapping, arbitrary slider limit,
or direct hardware command from the UI was introduced.
