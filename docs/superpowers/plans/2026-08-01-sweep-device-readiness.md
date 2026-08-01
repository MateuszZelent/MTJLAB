# Sweep Device Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start a sweep from a Fluent readiness modal that connects and verifies every required device, then reuses open device sessions rather than requiring a manual disconnect.

**Architecture:** Existing `InstrumentWorker` threads retain adapter/transport ownership. A runner-only proxy marshals synchronous RecipeRunner calls to that owner thread, so a verified session is reused without thread-affinity violations or a second VISA/TCP connection. The Fluent modal only presents readiness; compiler, runner, and adapters remain safety authorities.

**Tech Stack:** Python, PySide6, PySide6-Fluent-Widgets, unittest.

## Global Constraints

- Do not modify the legacy/non-Fluent shell or create a compatibility shell.
- Preflight connection may only connect and verify identity; it must not configure or enable output.
- A runner call executes in the adapter's existing InstrumentWorker thread.
- Never create a duplicate session for a manually connected device.
- Preserve the independent E-STOP path and all runner shutdown/readback/audit contracts.
- Preserve the user's uncommitted Anritsu changes.

---

### Task 1: Add an owner-thread runner adapter proxy

**Files:**

- Modify: `app/ui/workers.py`
- Create: `tests/test_device_run_lease.py`

**Interfaces:**

- `DeviceController.call_for_run(method: str, *args: object, **kwargs: object) -> object`
- `DeviceController.adapter_for_run() -> RunDeviceAdapter`
- `RunDeviceAdapter` exposes `connect()`, `disconnect()`, `emergency_off()`, `state`, `identity`, `capabilities`, and adapter method forwarding.

- [ ] **Step 1: Write the failing test**

```python
def test_run_proxy_executes_adapter_calls_in_its_owner_thread(self) -> None:
    controller = DeviceController(FakeAdapter())
    try:
        controller.call("connect")
        self._wait_for_state(controller, "verified")
        proxy = controller.adapter_for_run()
        self.assertEqual(proxy.connect().idn, "FAKE,MODEL,1,1")
        self.assertEqual(proxy.read_value(), 42)
        self.assertEqual(proxy.last_method_thread_id, controller.worker_thread_id)
    finally:
        controller.close()
```

- [ ] **Step 2: Run RED**

Run: `pytest tests/test_device_run_lease.py::DeviceRunLeaseTests::test_run_proxy_executes_adapter_calls_in_its_owner_thread -q`

Expected: FAIL because `adapter_for_run` does not exist.

- [ ] **Step 3: Implement the minimal bridge**

```python
@dataclass(slots=True)
class _RunCall:
    method: str
    args: tuple[object, ...]
    kwargs: dict[str, object]
    done: Event = field(default_factory=Event)
    result: object = None
    error: BaseException | None = None

@Slot(object)
def invoke_for_run(self, request: _RunCall) -> None:
    try:
        request.result = getattr(self._adapter, request.method)(*request.args, **request.kwargs)
    except BaseException as exc:
        request.error = exc
    finally:
        self.state_changed.emit(self._adapter.state.value)
        request.done.set()
```

Add a queued request signal in `DeviceController`. Its synchronous caller waits for
the event and re-raises the captured exception. Implement proxy properties through
the same bridge and use `__getattr__` only for adapter operations.

- [ ] **Step 4: Run GREEN**

Run: `pytest tests/test_device_run_lease.py -q`

Expected: PASS, including an injected adapter failure that stays an error.

- [ ] **Step 5: Commit**

```bash
git add app/ui/workers.py tests/test_device_run_lease.py
git commit -m "feat: add runner-owned device session proxy"
```

### Task 2: Reuse active sessions in the run worker

**Files:**

- Modify: `app/ui/run_worker.py`
- Modify: `tests/test_run_controller.py`

**Interfaces:**

- `RunController.start` gains the keyword-only argument `device_controllers: Mapping[str, DeviceController] | None = None`.
- `RunWorker` gains the same optional mapping and gets `adapter_for_run()` for each supplied controller, constructing a new adapter only for a disconnected device.

- [ ] **Step 1: Write the failing test**

```python
def test_run_controller_reuses_a_provided_connected_session(self) -> None:
    device_controllers = {"anritsu": self._connected_anritsu_controller()}
    try:
        self.controller.start(self.settings, self.settings_path, self.plan,
                              simulation=True, device_controllers=device_controllers)
        self._wait_for_completion(self.controller)
        self.assertEqual(device_controllers["anritsu"]._worker._adapter.connect_count, 1)
    finally:
        self.controller.close()
        device_controllers["anritsu"].close()
```

- [ ] **Step 2: Run RED**

Run: `pytest tests/test_run_controller.py::RunControllerTests::test_run_controller_reuses_a_provided_connected_session -q`

Expected: FAIL because `start` rejects `device_controllers`.

- [ ] **Step 3: Implement selection of the actual adapter**

```python
active = dict(device_controllers or {})
rigol = active["rigol"].adapter_for_run() if "rigol" in active else RigolAdapter(
    self._settings,
    session_factory=SimulatedVisaFactory("rigol", context=simulation_context)
    if self._simulation else None,
)
keithley = active["keithley"].adapter_for_run() if "keithley" in active else KeithleyAdapter(
    self._settings,
    session_factory=SimulatedVisaFactory("keithley", context=simulation_context)
    if self._simulation else None,
)
anritsu = active["anritsu"].adapter_for_run() if "anritsu" in active else AnritsuAdapter(
    self._settings,
    session_factory=SimulatedVisaFactory("anritsu", context=simulation_context)
    if self._simulation else None,
)
```

Apply the same pattern to required MOKE and Lake Shore devices. Keep the runner's
idempotent `connect()` verification and existing cleanup; do not disconnect before
starting. Run metadata must come from the selected adapter.

- [ ] **Step 4: Run GREEN and regressions**

Run: `pytest tests/test_run_controller.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/ui/run_worker.py tests/test_run_controller.py
git commit -m "feat: reuse connected sessions during sweep runs"
```

### Task 3: Add the Fluent sweep-readiness modal

**Files:**

- Modify: `app/ui/dialogs.py`
- Modify: `tests/test_fluent_dialogs.py`

**Interfaces:**

- `SweepDeviceReadinessDialog(required_devices, display_names, parent)`
- `connect_missing_requested = Signal(tuple)`
- `start_requested = Signal()`
- `update_device(device, state, identity_verified, error=None) -> None`

- [ ] **Step 1: Write the failing rendering/gating test**

```python
def test_sweep_readiness_dialog_gates_start_on_all_required_devices(self) -> None:
    dialog = SweepDeviceReadinessDialog(("anritsu", "keithley"), DISPLAY_NAMES)
    dialog.show(); self.application.processEvents()
    self.assertGreater(dialog.geometry().width(), 0)
    self.assertFalse(dialog.start_button.isEnabled())
    dialog.update_device("anritsu", "verified", True)
    dialog.update_device("keithley", "output_off", True)
    self.assertTrue(dialog.start_button.isEnabled())
```

- [ ] **Step 2: Run RED**

Run: `pytest tests/test_fluent_dialogs.py -k sweep_readiness_dialog -q`

Expected: FAIL because the dialog does not exist.

- [ ] **Step 3: Implement the Fluent-native dialog**

Use `StationDialog`, `SubtitleLabel`, `BodyLabel`, `PrimaryPushButton`, and
`PushButton`. Add one accessible status row per required device. The Connect
button emits only incomplete device identifiers, and the Start button is disabled
for `disconnected`, `fault`, `unknown`, `output_on`, or missing identity.

- [ ] **Step 4: Run GREEN at both sizes**

Run: `pytest tests/test_fluent_dialogs.py -k sweep_readiness_dialog -q`

Expected: PASS after `show()`/event processing at desktop width and 820 px width.

- [ ] **Step 5: Commit**

```bash
git add app/ui/dialogs.py tests/test_fluent_dialogs.py
git commit -m "feat: add sweep device readiness dialog"
```

### Task 4: Wire readiness to sweep start and lock manual I/O

**Files:**

- Modify: `app/ui/shell/main_window.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**

- `_open_sweep_readiness(plan, start_options) -> SweepDeviceReadinessDialog`
- `_connect_sweep_devices(devices: tuple[str, ...]) -> None`
- The final start helper calls `RunController.start` with `device_controllers=active_controllers`.
- `_guard_manual_operation(device, operation, payload)` rejects manual I/O while that device is supplied to a running recipe, while allowing E-STOP.

- [ ] **Step 1: Write failing workflow tests**

```python
def test_readiness_modal_connects_missing_devices_before_start(self) -> None:
    dialog = self.window._open_sweep_readiness(self.plan)
    self.assertFalse(dialog.start_button.isEnabled())
    dialog.connect_missing_button.click()
    self._wait_until(lambda: dialog.start_button.isEnabled())
    dialog.start_button.click()
    self.assertTrue(self.window._run_controller.running)

def test_connected_required_device_starts_without_disconnect_warning(self) -> None:
    self._connect(self.window, "anritsu")
    with patch("app.ui.shell.main_window.QMessageBox.warning") as warning:
        dialog = self.window._open_sweep_readiness(self.plan)
        dialog.start_button.click()
    warning.assert_not_called()
```

- [ ] **Step 2: Run RED**

Run: `pytest tests/test_main_window.py -k "readiness_modal or connected_required_device_starts" -q`

Expected: FAIL because the current start path asks to disconnect manual control.

- [ ] **Step 3: Implement orchestration**

Replace only the blanket connected-session rejection with the modal. Subscribe it to
device state/result/error signals. On Connect, queue only `connect` for missing
required devices. Require safe state plus verified identity for all required rows.
Pass every currently connected controller to the run controller so that an unrelated
manual source also cannot receive a duplicate session. Disable manual page I/O during
the run and retain audit/RBAC/preflight checks.

- [ ] **Step 4: Run GREEN and UI regressions**

Run: `pytest tests/test_main_window.py -k "readiness_modal or connected_required_device_starts" -q`

Expected: PASS.

Run: `pytest tests/test_fluent_shell.py tests/test_fluent_dialogs.py tests/test_main_window.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/ui/shell/main_window.py tests/test_main_window.py
git commit -m "feat: gate sweeps on connected device readiness"
```

### Task 5: Verify safety and final scope

**Files:**

- Modify only if verification identifies a defect in Tasks 1-4.

- [ ] **Step 1: Run safety/run regressions**

Run: `pytest tests/test_adapters_and_runner.py tests/test_station_readiness.py tests/test_run_controller.py -q`

Expected: PASS.

- [ ] **Step 2: Run static checks**

Run: `ruff check app tests`

Expected: PASS.

- [ ] **Step 3: Review scope**

Run: `git diff --check; git status --short`

Expected: only feature files plus the preserved user Anritsu changes.

- [ ] **Step 4: Commit a verified correction only if needed**

```bash
git add app/ui/workers.py app/ui/run_worker.py app/ui/dialogs.py app/ui/shell/main_window.py tests/test_device_run_lease.py tests/test_run_controller.py tests/test_fluent_dialogs.py tests/test_main_window.py
git commit -m "fix: preserve sweep connection safety"
```
