# Quick Controls Output and Fluent Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Quick Controls explicit, independently synchronized Keithley A/B output controls and a truly Fluent-native floating window without weakening output safety.

**Architecture:** Keep the device adapter as the authority for output validation and readback. Add a Keithley group operation that performs ordered, verified transitions with a safe rollback path; expose only typed worker operations to the page. Replace the Quick Controls `QDialog` host with QFluentWidgets `FluentWidget`, and feed it per-channel state signals from the device pages.

**Tech Stack:** Python 3, PySide6, QFluentWidgets 1.11.2, pytest/unittest Qt tests, existing VISA simulators/fake sessions, Ruff.

## Global Constraints

- The compiler, safety policy, adapter, and page must enforce output invariants independently; UI state is never authority.
- Configure/validate before enabling; every mutation needs hardware readback where supported.
- Never blindly retry a state-changing output command after an uncertain result.
- A failed or unconfirmed shutdown is `UNKNOWN`/`FAULT`, not `OFF`.
- The application shell and floating tool host must use the Fluent-native API; no new `QDialog` compatibility wrapper.
- Preserve existing Quick Controls setpoint synchronization, safety bounds, and typed precision.

---

### Task 1: Add failing adapter tests for grouped Keithley output

**Files:**
- Modify: `tests/test_adapters_and_runner.py`
- Modify: `tests/test_simulators.py`

**Interfaces:**
- Consumes: existing `KeithleyAdapter`, `KeithleySourceRequest`, fake VISA sessions, and simulator settings.
- Produces: executable contract for `KeithleyAdapter.set_output_group(channels, enabled)`.

- [ ] **Step 1: Write the failing tests.**

Add tests that call the wished-for API with `("A", "B")` and assert:

```python
result = adapter.set_output_group(("A", "B"), True)
assert result == {"A": True, "B": True}
assert session.attempted_commands.index("smua.source.output = smua.OUTPUT_ON") < session.attempted_commands.index("smub.source.output = smub.OUTPUT_ON")
```

Add a failure test where B's output readback is wrong or its validation fails; assert A is not left enabled and the error is surfaced. Add an OFF test that attempts both channels and returns both confirmed states.

- [ ] **Step 2: Run the tests to verify they fail for the missing API.**

Run:

```powershell
python -m pytest tests/test_adapters_and_runner.py -k "keithley and group" -q
python -m pytest tests/test_simulators.py -k "keithley and group" -q
```

Expected: FAIL with the missing `set_output_group` attribute or operation contract.

### Task 2: Implement the safe Keithley group operation and worker dispatch

**Files:**
- Modify: `app/devices/keithley_2600/adapter.py`
- Modify: `app/devices/keithley_2600/module.py`
- Modify: `app/ui/workers.py`

**Interfaces:**
- Consumes: existing `set_output`, `emergency_off`, `_output_is_enabled`, and module dispatcher.
- Produces: `set_output_group(channels: tuple[Literal["A", "B"], ...], enabled: bool) -> dict[str, bool]` and worker operation name `set_output_group`.

- [ ] **Step 1: Implement the minimal adapter method.**

Validate non-empty unique channels and channel names before writing. For enable, call existing per-channel validation/readback in deterministic A-then-B order. Track channels successfully enabled. On any exception, call the existing emergency-off path and re-raise without claiming a confirmed group state. For disable, attempt every requested channel and raise the first failure after all attempts; return only readback-confirmed states.

- [ ] **Step 2: Add module and compatibility dispatch.**

Map `set_output_group` in `app/devices/keithley_2600/module.py` and in the Keithley compatibility branch of `InstrumentWorker._dispatch`, passing a tuple of channels and a boolean to the adapter.

- [ ] **Step 3: Run the focused tests to verify green.**

Run the two commands from Task 1 and then:

```powershell
python -m ruff check app/devices/keithley_2600/adapter.py app/devices/keithley_2600/module.py app/ui/workers.py tests/test_adapters_and_runner.py tests/test_simulators.py
```

### Task 3: Add failing page/state propagation tests

**Files:**
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_quick_controls.py`

**Interfaces:**
- Consumes: `KeithleyPage`, `QuickControlsWindow`, and the existing controller test fixture.
- Produces: contracts for separate A/B requests, group dispatch, and `output_state_changed` forwarding.

- [ ] **Step 1: Write failing page tests.**

Assert that `request_channel_output("B", True)` calls `set_output` with B, that `request_output_group(("A", "B"), True)` calls `set_output_group`, and that a successful result updates both `_output_states` and emits state for both channels.

- [ ] **Step 2: Write failing Quick Controls tests.**

Construct the window under the existing Qt test fixture, assert there are separate Keithley A and B rows, click each row's ON button, and assert the emitted channel is the row channel. Assert the group button emits `("keithley", True)` and that `set_output_state("keithley", "B", "on")` updates B only.

- [ ] **Step 3: Run the tests to verify the expected failures.**

Run:

```powershell
python -m pytest tests/test_quick_controls.py -k "output or channel" -q
python -m pytest tests/test_main_window.py -k "keithley.*output or quick.*output" -q
```

### Task 4: Implement independent A/B output controls and state synchronization

**Files:**
- Modify: `app/devices/keithley_2600/ui/page.py`
- Modify: `app/devices/rigol_dg1000z/ui/page.py`
- Modify: `app/ui/quick_controls.py`
- Modify: `app/ui/shell/main_window.py`

**Interfaces:**
- Consumes: adapter `set_output_group`, existing per-channel page actions, and QFluent controls.
- Produces: `output_state_changed`, `request_output_group`, `QuickControlsWindow.output_group_requested`, and `QuickControlsWindow.set_output_state`.

- [ ] **Step 1: Add page output-state signals and safe group request handling.**

Emit `output_state_changed(channel, "on"|"off"|"unknown")` whenever a channel's confirmed state changes. Add `request_output_group` to KeithleyPage, call `set_output_group`, apply only returned confirmed states, and publish `unknown` on an error unless a later readback proves a state. Keep single-channel methods unchanged.

- [ ] **Step 2: Replace the selector row with separate channel rows.**

Create a compact QFluent row per physical channel. Each row has a state badge, `OUTPUT ON`, and `OUTPUT OFF`; Quick Controls emits the exact row channel. For Keithley add a clearly separated `OUTPUT ON A+B` action and a group `OUTPUT OFF` action. Use accessible names and tooltips that state the physical channel.

- [ ] **Step 3: Wire state snapshots and live state events.**

Connect page signals in MainWindow, seed the floating window from the pages' current known/unknown state, and route group requests to the Keithley page. Do not infer B from aggregate device state.

- [ ] **Step 4: Run focused tests and lint.**

Run the tests from Task 3 and:

```powershell
python -m ruff check app/devices/keithley_2600/ui/page.py app/devices/rigol_dg1000z/ui/page.py app/ui/quick_controls.py app/ui/shell/main_window.py tests/test_main_window.py tests/test_quick_controls.py
```

### Task 5: Write failing Fluent host/rendering tests

**Files:**
- Modify: `tests/test_quick_controls.py`

**Interfaces:**
- Consumes: `QuickControlsWindow` and the existing QApplication fixture.
- Produces: rendered-geometry and host-type regression coverage.

- [ ] **Step 1: Assert the failing host contract.**

Add a test asserting `isinstance(window, FluentWidget)`, that the window is visible with non-zero geometry after `show()` and `processEvents()`, that the normal size is at least the desktop-friendly minimum, and that a narrow resize still leaves the scroll area and both channel rows visible.

- [ ] **Step 2: Run the test and verify it fails because the current class is `StationDialog`.**

Run:

```powershell
python -m pytest tests/test_quick_controls.py -k "fluent or render or geometry" -q
```

### Task 6: Migrate Quick Controls to FluentWidget and polish layout

**Files:**
- Modify: `app/ui/quick_controls.py`
- Modify: `app/ui/design_system/station_qss.py`
- Modify: `app/ui/design_system/fluent_theme.py` only if an explicit FluentWidget theme hook is required by the test.

**Interfaces:**
- Consumes: QFluent `FluentWidget`, existing `restore_workspace`, station theme tokens, output row widget behavior.
- Produces: non-modal `QuickControlsWindow(FluentWidget)` with Fluent title bar, preserved geometry persistence, and responsive output cards.

- [ ] **Step 1: Replace the legacy host.**

Remove `QDialog`/`StationDialog` inheritance and use `FluentWidget`. Preserve `WindowStaysOnTopHint`, non-modal show/raise/activate behavior, close-event geometry persistence, and the existing central layout. Do not add a navigation shell to this utility window.

- [ ] **Step 2: Align theme ownership.**

Apply the existing station surface property to the Fluent host/content, ensure the global theme observer repolishes the Fluent host, and remove any selector that makes Quick Controls look like a generic legacy dialog. Keep generic dialog QSS for true dialogs that still need it.

- [ ] **Step 3: Run rendering tests and inspect a normal desktop capture if available.**

Run the Task 5 command at normal and narrow sizes. Confirm title bar, cards, state badges, buttons, focus, and disabled/error states are visually coherent in light and dark themes.

### Task 7: Full verification and safety audit

**Files:**
- Modify: `docs/superpowers/specs/2026-08-03-quick-controls-output-fluent-design.md` only if verification reveals a contract correction.

- [ ] **Step 1: Run focused safety and UI suites.**

```powershell
python -m pytest tests/test_adapters_and_runner.py tests/test_simulators.py tests/test_quick_controls.py tests/test_main_window.py -q
```

- [ ] **Step 2: Run static checks.**

```powershell
python -m ruff check app tests
```

- [ ] **Step 3: Audit the final diff.**

Confirm no output command is emitted by the UI directly, no group action retries a state-changing command, every A/B state shown as ON has a readback source, and no existing quantity/synchronization behavior changed.

