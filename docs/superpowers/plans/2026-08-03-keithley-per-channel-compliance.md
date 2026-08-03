# Keithley Per-Channel Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline execution is selected for this session). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop only the Keithley channel that reaches compliance when its per-channel policy requests it; otherwise keep that output on with a visible warning and a same-mode directional source-increase block. Preserve an independent channel, and require an explicit operator choice before any stopped channel can be configured and re-enabled.

**Architecture:** Keep the existing all-output emergency path for unknown/communication faults, but add a channel-scoped compliance latch and channel-only OFF/readback path. The adapter exposes an explicit recovery operation with `restore_previous` or `keep_off`, a per-channel `stop_on_compliance` policy, and a source-mode-aware directional block for continue mode. The worker and page carry channel/policy/recovery payloads, and the page renders recovery actions inside the affected channel card.

**Tech Stack:** Python 3, PySide6/QFluentWidgets, Keithley TSP/VISA adapter, simulator transport, pytest/unittest, ruff.

## Current implementation status

- Adapter: per-channel stop/continue policy, verified channel-only shutdown,
  same-mode directional increase block, explicit recovery, and global fallback
  for ambiguous or unrelated safety faults.
- UI: one policy toggle and compliance indicator per card; stop-mode recovery
  choices stay in the affected card, while continue mode leaves both channels
  usable and shows a non-modal warning.
- Verification: simulator, adapter/runner, settings/safety, focused UI, lint,
  and compile checks pass. The broader main-window suite still contains
  unrelated pre-existing failures in profile-limit expectations and a background
  settings-save timing test.

## Global Constraints

- Never widen, bypass, or silently reinterpret configured current, voltage, power, DUT, or immutable hardware limits.
- Recovery may configure a source while its output is confirmed OFF, but it must never enable an output.
- A missing or contradictory output readback escalates to the existing all-output emergency-off path and leaves the adapter UNKNOWN/locked.
- Compliance on A must not write to or disable B; the same rule applies symmetrically.
- Global E-STOP, disconnect, and uncertain hardware state retain the existing all-output safe shutdown behavior.
- Use explicit units and existing quantity/safety validators at every source-configuration boundary.
- Use non-modal banners and in-card actions for compliance recovery; do not block the whole window with a recovery message box.

---

### Task 1: Add channel-scoped compliance state and safe adapter operations

**Files:**
- Modify: `app/devices/keithley_2600/adapter.py` (`KeithleyAdapter.__init__`, connect/disconnect, output aggregation, configure/update methods, `set_output`, `measure`, `ramp_to_level`, and recovery methods)
- Test: `tests/test_simulators.py`

**Interfaces:**
- Produce `KeithleyAdapter.recover_from_compliance(channel: Literal["A", "B"], choice: Literal["restore_previous", "keep_off"]) -> dict[str, object]`.
- Track `self._compliance_channels: set[Literal["A", "B"]]` and `self._last_safe_request: dict[...]`.
- Add an internal `_disable_channel_and_verify(channel) -> None` that writes only the selected SMU `OUTPUT_OFF`, queries only that channel, and escalates to `emergency_off()` on any write/readback failure.

- [ ] **Step 1: Write the failing simulator tests**

Add tests with two outputs enabled and a simulated high-resistance DUT on A:

```python
keithley.configure_source(KeithleySourceRequest("A", "current", 1e-3, 0.067))
keithley.configure_source(KeithleySourceRequest("B", "current", 1e-3, 0.067))
keithley.set_output("A", True)
keithley.set_output("B", True)
measurement = keithley.measure("A")
assert measurement.compliance_stop_required
assert keithley._output_states["A"] is False
assert keithley._output_states["B"] is True
with pytest.raises(SafetyViolation, match="compliance"):
    keithley.set_output("A", True)
assert keithley.set_output("B", True) is True
```

Also test both recovery choices and a fault-injected A output readback. The restore path must return the previous request in its result and leave A OFF; neither path may issue an `OUTPUT_ON` command.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest -q tests/test_simulators.py -k "keithley_per_channel_compliance" --maxfail=1`

Expected: FAIL because the adapter currently shuts down both channels and has no channel-scoped recovery API.

- [ ] **Step 3: Implement the minimal adapter behavior**

1. Initialize/clear `_compliance_channels` and `_last_safe_request` with the session lifecycle.
2. Preserve the last request that completed a non-compliance measurement in `_last_safe_request[channel]`.
3. Replace only the compliance branch in `measure` with `_disable_channel_and_verify(channel)` and add that channel to the latch. Keep all-output shutdown for malformed measurements, measurement safety-trip violations, communication errors, and readback mismatches.
4. Make `_update_aggregate_output_state` report `DeviceState.COMPLIANCE` while any channel is latched, otherwise retain the normal ON/OFF aggregate state.
5. Reject `set_output(channel, True)` only when that channel is latched; never use the global state as a reason to reject the unaffected channel.
6. Implement `recover_from_compliance`: validate channel/choice, repeat channel-only OFF verification, optionally reapply `_last_safe_request[channel]` through the existing OFF-only `configure_source` validator, clear only that latch, and return `{channel, choice, outputs_confirmed_off, restored_request}`. Do not clear instrument errors or enable output.
7. In ramp exception handling, preserve the unaffected channel when the exception is caused by a compliance latch; use all-output shutdown for every other exception.

- [ ] **Step 4: Run the simulator tests and inspect command scope**

Run: `pytest -q tests/test_simulators.py -k "keithley_per_channel_compliance or keithley_compliance" --maxfail=1`

Expected: PASS; the simulated command log must contain only `smua.source.output = smua.OUTPUT_OFF` for an A compliance event, with no B OFF command until an independent global emergency path is invoked.

- [ ] **Step 5: Run lint/compile for the adapter**

Run: `ruff check app/devices/keithley_2600/adapter.py tests/test_simulators.py; python -m compileall -q app/devices/keithley_2600`

Expected: exit 0.

### Task 2: Carry channel and recovery choice through dispatch

**Files:**
- Modify: `app/devices/keithley_2600/module.py`
- Modify: `app/ui/workers.py`
- Test: `tests/test_simulators.py` or a focused dispatcher test if the repository exposes one

**Interfaces:**
- Consume `recover_from_compliance(channel, choice)` from Task 1.
- Produce dispatcher support for operation payload `(channel, choice)` in both the module-specific dispatcher and the compatibility fallback.

- [ ] **Step 1: Add a failing dispatch test**

Call the Keithley module dispatcher with `("A", "keep_off")` and assert it invokes the adapter with exactly that channel and choice.

- [ ] **Step 2: Run the test and verify the unsupported-operation failure**

Run: `pytest -q tests/test_simulators.py -k "keithley_recover_dispatch" --maxfail=1`

Expected: FAIL with an unsupported operation or wrong payload handling.

- [ ] **Step 3: Add exact payload unpacking**

Handle:

```python
if operation == "recover_from_compliance":
    channel, choice = payload
    return adapter.recover_from_compliance(channel, choice)
```

Validate the two-item payload through the adapter’s typed validation; do not default a missing channel or choice.

- [ ] **Step 4: Run dispatch tests and lint**

Run: `pytest -q tests/test_simulators.py -k "keithley_recover_dispatch" --maxfail=1; ruff check app/devices/keithley_2600/module.py app/ui/workers.py`

Expected: PASS and exit 0.

### Task 3: Render per-channel compliance actions without blocking the other channel

**Files:**
- Modify: `app/devices/keithley_2600/ui/page.py` (`_build_channel_card`, page state, readiness, result/error handling, live selection)
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consume `KeithleyMeasurement.compliance_stop_required` and the Task 2 recovery result.
- Produce per-card `restore_previous` and `keep_off` buttons; no global recovery button that claims both outputs are OFF.

- [ ] **Step 1: Write failing UI tests**

Create a simulated page with both channels marked ON, feed a compliance measurement for A, and assert:

```python
assert "COMPLIANCE" in page.channel_cards["A"]["output"].text()
assert page.channel_cards["B"]["output"].text() == "OUTPUT ON"
assert page.channel_cards["A"]["restore_compliance"].isVisible()
assert page.channel_cards["B"]["output_on_action"].isEnabled()
page._controller.call.assert_not_called()  # compliance does not auto-recover
```

Click each recovery choice in separate tests and assert the controller receives `("recover_from_compliance", ("A", choice))`; assert no `set_output(..., True)` is generated.

- [ ] **Step 2: Run the UI tests and verify they fail**

Run: `pytest -q tests/test_main_window.py -k "keithley_per_channel_compliance" --maxfail=1`

Expected: FAIL because the current page has a global recovery button and marks both channels blocked.

- [ ] **Step 3: Implement per-channel UI state and actions**

1. Replace the global recovery flags with per-channel sets/maps and add two compact buttons to each channel card.
2. In `_result("measure", measurement)`, call `_mark_channel_compliance(channel)` only for the affected channel; stop only that channel’s live checkbox if needed, leaving the other channel live.
3. Make `_update_output_readiness` block `OUTPUT ON` only when the selected channel is latched or its recovery is pending. Allow configuration edits with output OFF and keep the unaffected card’s controls enabled.
4. Route `restore_previous` and `keep_off` clicks to `_controller.call("recover_from_compliance", (channel, choice))` with a non-modal pending state.
5. In the recovery result, clear only that channel’s latch, set its card/output state OFF, restore the prior form snapshot when the result includes `restored_request`, and show a warning/success banner explaining that no output was enabled.
6. In `_error`, keep the affected channel OFF/latched and show a persistent banner with retry/edit actions. Do not open a blocking `QMessageBox`.
7. Treat global `DISCONNECTED`, `UNKNOWN`, `FAULT`, and E-STOP states as the existing all-channel lock; do not let a channel-only compliance state overwrite the unaffected channel’s card.

- [ ] **Step 4: Run UI tests and a rendering smoke test**

Run: `pytest -q tests/test_main_window.py -k "keithley_per_channel_compliance or keithley_output" --maxfail=1`

Expected: PASS; A is visibly OFF/COMPLIANCE, B remains ON, and the recovery buttons never send OUTPUT ON.

- [ ] **Step 5: Run lint/compile for the UI**

Run: `ruff check app/devices/keithley_2600/ui/page.py tests/test_main_window.py; python -m compileall -q app/devices/keithley_2600/ui/page.py`

Expected: exit 0.

### Task 4: Preserve global fail-safe behavior and verify end-to-end limits

**Files:**
- Modify: `app/ui/shell/main_window.py` only if the operation guard must classify `recover_from_compliance` as a non-energizing action
- Modify: `docs/superpowers/specs/2026-08-03-keithley-per-channel-compliance-design.md` only for verified implementation notes
- Test: `tests/test_main_window.py`, `tests/test_simulators.py`

**Interfaces:**
- Consume the channel-scoped operation from Tasks 1–3.
- Preserve all existing `emergency_off` and audit interlocks.

- [ ] **Step 1: Add failing global-safety regression tests**

Verify that an audit failure and an active recipe lease still allow `emergency_off`, `recover_from_compliance`, and `OUTPUT OFF`, but block all new energizing operations; verify a failed channel OFF readback forces both channels OFF and `DeviceState.UNKNOWN`.

- [ ] **Step 2: Run the safety tests and verify failures**

Run: `pytest -q tests/test_main_window.py tests/test_simulators.py -k "audit_failure or emergency_off or per_channel_compliance" --maxfail=1`

Expected: FAIL only for the new channel-scoped expectations.

- [ ] **Step 3: Implement the smallest guard/fault changes**

Classify `recover_from_compliance` as non-energizing in `_guard_manual_operation`; never add it to the energizing operation set. Keep the existing all-output fallback on any uncertain channel OFF readback.

- [ ] **Step 4: Run the complete relevant verification**

Run:

```powershell
pytest -q tests/test_simulators.py tests/test_main_window.py --maxfail=1
ruff check app/devices/keithley_2600 app/ui/workers.py app/ui/shell/main_window.py tests/test_simulators.py tests/test_main_window.py
python -m compileall -q app/devices/keithley_2600 app/ui/workers.py app/ui/shell/main_window.py
```

Expected: all targeted tests pass, ruff exits 0, and compileall exits 0. If an unrelated pre-existing test fails, record its exact failure separately and do not weaken the compliance safety behavior to make it pass.

- [ ] **Step 5: Review the diff against the approved design**

Confirm there is no command path that increases a source/compliance/power limit, no automatic `OUTPUT_ON` in recovery, no B shutdown in the A compliance path, and no modal UI that blocks recovery.
