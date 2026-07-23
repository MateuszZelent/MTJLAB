# Keithley DUT Isolation Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one channel-specific `Disconnect / connect DUT…` action beside each Keithley channel measurement button, opening the relay only for the operator modal and restoring verified `OUTPUT_NORMAL` afterward.

**Architecture:** The Keithley adapter owns the typed, readback-verified off-mode transition and the NORMAL invariant. The device module exposes only the two whitelisted DUT modes through the existing worker dispatcher. The Keithley page implements a single asynchronous state machine: request HIGH-Z, show the modal only after confirmation, then request NORMAL when the modal closes.

**Tech Stack:** Python 3.12, PySide6, PySide6-Fluent-Widgets, unittest/pytest, deterministic VISA simulator

## Global Constraints

- Keithley `Connect` remains exactly read-only: `*IDN?`, output A query, output B query.
- `HIGH_Z` is temporary and is never persisted to station settings.
- The action operates on only the channel card whose button was pressed.
- OUTPUT must be known and confirmed OFF before either off-mode mutation.
- No off-mode write is automatically retried after timeout or uncertain readback.
- Applying configuration, measuring, and enabling OUTPUT establish or verify `OUTPUT_NORMAL`.
- UI rendering tests must call `show()` and process Qt events before checking geometry.

---

### Task 1: Typed channel-specific off-mode transition

**Files:**
- Modify: `app/devices/keithley_2600/adapter.py`
- Modify: `app/devices/keithley_2600/__init__.py`
- Modify: `app/devices/keithley_2600/module.py`
- Modify: `tests/test_adapters_and_runner.py`

**Interfaces:**
- Produces: `KeithleyOutputOffModeResult(channel: Literal["A", "B"], mode: Literal["normal", "high_impedance"], output_enabled: bool)`
- Produces: `KeithleyAdapter.set_dut_output_off_mode(channel, mode) -> KeithleyOutputOffModeResult`
- Produces: dispatcher operation `"set_dut_output_off_mode"` with payload `(channel, mode)`

- [ ] **Step 1: Write failing adapter tests**

Add tests that assert the exact channel-B command traffic:

```python
result = adapter.set_dut_output_off_mode("B", "high_impedance")
self.assertEqual(
    session.writes[traffic_start:],
    [
        "print(smub.source.output)",
        "smub.source.offmode = smub.OUTPUT_HIGH_Z",
        "print(smub.source.offmode == smub.OUTPUT_HIGH_Z)",
        "print(smub.source.output)",
    ],
)
self.assertEqual((result.channel, result.mode, result.output_enabled), ("B", "high_impedance", False))
self.assertFalse(any("smua." in command for command in session.writes[traffic_start:]))
```

Add the symmetric NORMAL assertion and rejection tests for channel OUTPUT ON,
invalid channel, invalid mode, and false equality readback. Rejection before
validation must have no off-mode assignment in the new traffic.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_adapters_and_runner.py -k "dut_output_off_mode" -v
```

Expected: failures because the result type and adapter method do not exist.

- [ ] **Step 3: Implement the typed result and adapter method**

Add:

```python
KeithleyDutOffMode = Literal["normal", "high_impedance"]

@dataclass(frozen=True, slots=True)
class KeithleyOutputOffModeResult:
    channel: Literal["A", "B"]
    mode: KeithleyDutOffMode
    output_enabled: bool
```

Implement `set_dut_output_off_mode()` so it validates channel/mode before VISA,
queries OUTPUT, rejects ON, writes only `OUTPUT_NORMAL` or `OUTPUT_HIGH_Z`,
verifies equality, re-queries OUTPUT, and raises `DeviceError` if OUTPUT changed
or readback did not confirm the requested mode. Update the adapter's cached
output state only from confirmed readback.

- [ ] **Step 4: Export and dispatch the operation**

Export the result from `app/devices/keithley_2600/__init__.py`. Add a strict
dispatcher branch:

```python
if operation == "set_dut_output_off_mode":
    channel, mode = payload
    return adapter.set_dut_output_off_mode(channel, mode)
```

Do not expose raw TSP or accept `zero`.

- [ ] **Step 5: Run adapter tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_adapters_and_runner.py -k "dut_output_off_mode or keithley_connect_is_read_only" -v
```

Expected: all selected tests pass and the connect test still sees only three
queries.

- [ ] **Step 6: Commit the adapter boundary**

```powershell
git add app/devices/keithley_2600/adapter.py app/devices/keithley_2600/__init__.py app/devices/keithley_2600/module.py tests/test_adapters_and_runner.py
git commit -m "feat: add verified Keithley DUT isolation mode"
```

### Task 2: Enforce NORMAL for normal Keithley work

**Files:**
- Modify: `app/devices/keithley_2600/adapter.py`
- Modify: `app/devices/simulators.py`
- Modify: `tests/test_adapters_and_runner.py`
- Modify: `tests/test_simulators.py`

**Interfaces:**
- Consumes: `KeithleyAdapter.set_dut_output_off_mode(...)`
- Produces: `_ensure_normal_output_off_mode(channel) -> None`
- Guarantees: configure, measure, and OUTPUT enable cannot continue with an unverified non-NORMAL off mode

- [ ] **Step 1: Write failing invariant tests**

Add tests for these cases:

```python
# OUTPUT OFF and HIGH-Z: measurement restores NORMAL before measure.iv().
self.assertLess(
    traffic.index("smub.source.offmode = smub.OUTPUT_NORMAL"),
    traffic.index("print(smub.measure.iv())"),
)

# OUTPUT enable: NORMAL is confirmed before OUTPUT_ON.
self.assertLess(
    traffic.index("print(smub.source.offmode == smub.OUTPUT_NORMAL)"),
    traffic.index("smub.source.output = smub.OUTPUT_ON"),
)
```

Also inject HIGH-Z with OUTPUT ON and assert that measurement fails closed,
commands both outputs OFF, and does not write `OUTPUT_NORMAL` under load.

- [ ] **Step 2: Run focused invariant tests and verify RED**

Run:

```powershell
python -m pytest tests/test_adapters_and_runner.py -k "normal_output_off_mode" -v
```

Expected: measurement and enable paths do not yet establish the invariant.

- [ ] **Step 3: Implement `_ensure_normal_output_off_mode`**

The helper must:

```python
active = self._output_is_enabled(channel)
normal = self._query_boolean(f"{smu}.source.offmode == {smu}.OUTPUT_NORMAL")
if normal:
    return
if active:
    self._fail_measurement_output_invariant(
        f"Keithley channel {channel} is energized with a non-NORMAL output-off mode."
    )
self.set_dut_output_off_mode(channel, "normal")
```

Call it:

- after configuration has forced OUTPUT OFF and before the rest of source setup;
- before `_verify_applied_configuration()` on OUTPUT enable;
- after pre-measurement output-state reconciliation and before `measure.iv()`.

Return `measurement_path_connected=True` only after NORMAL verification rather
than deriving it from the profile setting.

- [ ] **Step 4: Extend simulator state behavior**

Ensure `KeithleySimulator` retains channel-specific off-mode assignments and
answers equality queries consistently. Its untouched power-on baseline may
remain HIGH-Z because connect must report hardware state rather than rewrite
it. Add a simulator test that isolate B leaves A unchanged and a later
measurement restores B to NORMAL.

- [ ] **Step 5: Run adapter and simulator suites**

Run:

```powershell
python -m pytest tests/test_adapters_and_runner.py tests/test_simulators.py -v
```

Expected: all tests pass, including compliance, passive measurement, and
read-only connection regressions.

- [ ] **Step 6: Commit the NORMAL invariant**

```powershell
git add app/devices/keithley_2600/adapter.py app/devices/simulators.py tests/test_adapters_and_runner.py tests/test_simulators.py
git commit -m "fix: enforce Keithley NORMAL mode for measurements"
```

### Task 3: Per-channel modal DUT workflow

**Files:**
- Modify: `app/devices/keithley_2600/ui/page.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**
- Consumes: controller operation `"set_dut_output_off_mode"`
- Consumes: `KeithleyOutputOffModeResult`
- Produces: `KeithleyPage.request_dut_isolation(channel: str) -> None`
- Produces: one `dut_isolation` button in each `channel_cards[channel]`

- [ ] **Step 1: Write failing rendering and dispatch tests**

Create page tests that call `show()` and `QApplication.processEvents()`, then
assert:

```python
for channel in ("A", "B"):
    button = page.channel_cards[channel]["dut_isolation"]
    self.assertTrue(button.isVisible())
    self.assertGreater(button.geometry().width(), 0)
    self.assertIn("DUT", button.text())

page.channel_cards["B"]["dut_isolation"].click()
self.assertEqual(controller.calls[-1], ("set_dut_output_off_mode", ("B", "high_impedance")))
```

Cover disconnected, OUTPUT ON, unknown OUTPUT, and pending-operation disabled
states.

- [ ] **Step 2: Run the UI tests and verify RED**

Run:

```powershell
python -m pytest tests/test_main_window.py -k "dut_isolation" -v
```

Expected: missing `dut_isolation` controls and request method.

- [ ] **Step 3: Add buttons and page state**

Add a compact `PushButton("Disconnect / connect DUT…")` immediately after each
`Measure CH X` button. Store it as `channel_cards[channel]["dut_isolation"]`.
Add:

```python
self._dut_isolation_channel: str | None = None
self._dut_isolation_phase = "idle"
```

Implement enablement in `_update_output_readiness()` using connected/verified,
channel enabled, known OFF, and no pending Keithley operations. Stop both Live
checkboxes before dispatch and do not resume them automatically.

- [ ] **Step 4: Implement asynchronous modal state flow**

`request_dut_isolation(channel)` sets phase `entering_high_z`, disables
conflicting controls, and calls:

```python
self._controller.call("set_dut_output_off_mode", (channel, "high_impedance"))
```

In `_result`, a confirmed HIGH-Z result sets phase `operator_modal`, invokes
the Fluent station message box with the approved text, then sets
`restoring_normal` and dispatches:

```python
self._controller.call("set_dut_output_off_mode", (channel, "normal"))
```

A confirmed NORMAL result clears channel/phase and shows the success banner.
The window close control and primary modal action share the same restoration
path.

- [ ] **Step 5: Implement failure presentation**

In `_error`, clear pending state without claiming success. Enter-HIGH-Z failure
must never open the modal. Restore-NORMAL failure must show a persistent
critical banner stating that OUTPUT is OFF but relay mode is unknown and normal
measurement/output operations remain blocked until NORMAL is re-established.

- [ ] **Step 6: Test modal success and failures**

Patch the station modal in tests so it returns immediately. Assert:

- it is invoked only after a `KeithleyOutputOffModeResult(..., "high_impedance", False)`;
- dismissing it queues NORMAL for the same channel;
- NORMAL result re-enables controls;
- errors do not emit success text;
- channel A and B never cross-dispatch.

Run:

```powershell
python -m pytest tests/test_main_window.py -k "dut_isolation" -v
```

Expected: all DUT-isolation UI tests pass.

- [ ] **Step 7: Commit the modal UI**

```powershell
git add app/devices/keithley_2600/ui/page.py tests/test_main_window.py
git commit -m "feat: add Keithley DUT isolation modal"
```

### Task 4: Full regression and visual safety check

**Files:**
- Verify: `app/devices/keithley_2600/adapter.py`
- Verify: `app/devices/keithley_2600/ui/page.py`
- Verify: `app/devices/simulators.py`
- Verify: `tests/test_adapters_and_runner.py`
- Verify: `tests/test_simulators.py`
- Verify: `tests/test_main_window.py`

**Interfaces:**
- Consumes: all preceding tasks
- Produces: verified feature ready for hardware qualification

- [ ] **Step 1: Run formatting and static checks**

```powershell
python -m ruff check app/devices/keithley_2600 app/devices/simulators.py tests/test_adapters_and_runner.py tests/test_simulators.py tests/test_main_window.py
git diff --check
```

Expected: zero Ruff errors and no whitespace errors.

- [ ] **Step 2: Run complete relevant test suites**

```powershell
python -m pytest tests/test_settings_and_safety.py tests/test_adapters_and_runner.py tests/test_simulators.py tests/test_main_window.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Inspect rendered normal and narrow layouts**

Run the existing Qt screenshot/render harness for the Keithley page at normal
desktop width and narrow width. Confirm both A/B buttons are visible beside
their Measure actions, labels are not clipped, OUTPUT state remains prominent,
and the modal clearly names the channel and NORMAL restoration.

- [ ] **Step 4: Review exact safety traffic**

Confirm tests demonstrate:

```text
Connect: queries only
Isolation: OFF query -> HIGH_Z write -> HIGH_Z verify -> OFF query
Restore: OFF query -> NORMAL write -> NORMAL verify -> OFF query
OUTPUT ON: NORMAL established/verified before OUTPUT_ON
Measurement: NORMAL established/verified before measure.iv()
```

- [ ] **Step 5: Record hardware qualification boundary**

In the final handoff, state that automated simulator verification is complete
but relay behavior and front-panel indication still require a controlled
2602A bench check with no DUT or a sacrificial DUT before production use.

