# Lake Shore 475 Read-Only Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a production, read-only Lake Shore Model 475 integration with official-driver transport boundary, UI, recipes, HDF5 output, tests, and HIL documentation.

**Architecture:** A Model 475-specific adapter owns a configured PyVISA session and passes a read-only `write/query/clear` proxy to the official `lakeshore.model_425.Model425` class. The module exposes typed snapshot and measurement operations through the existing `DeviceModule`/`DeviceController` contracts, then adds one read-only recipe checkpoint action and a dedicated Qt page.

**Tech Stack:** Python 3.11, PySide6, PyVISA, official `lakeshore==1.10.0`, pytest, ruff, h5py, PyThat.

## Global Constraints

- Support Model 475 only; remove Model 425 as a public device capability.
- Allow only documented read queries: `*IDN?`, `UNIT?`, `RDGMODE?`, `RANGE?`, `AUTO?`, `TYPE?`, `RDGFIELD?`, `RDGFRQ?`, `RDGPEAK?`.
- Never send a device-setting command, `*CLS`, calibration command, output command, raw command, or unbounded raw query.
- Use RS-232 7O1, CR/LF, and configurable 9600/19200/38400/57600 baud for ASRL resources.
- Limit every transport query to 20 commands per second or less.
- Store magnetic induction only in tesla: convert gauss; reject Oe and A/m.
- Default all physical profiles to `enabled: false`; HIL remains mandatory before production enablement.
- Every production change begins with a focused failing test, then the smallest passing implementation.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `app/devices/lakeshore_gaussmeter/models.py` | Model 475 configuration, enums, snapshots and readings. |
| `app/devices/lakeshore_gaussmeter/connection.py` | VISA configuration and official-driver read-only bridge. |
| `app/devices/lakeshore_gaussmeter/adapter.py` | Verified read-only Model 475 lifecycle and parsing. |
| `app/devices/lakeshore_gaussmeter/simulator.py` | Fake Model 475 VISA session scenarios. |
| `app/devices/lakeshore_gaussmeter/module.py` | Adapter factory, dispatcher, manifest and page factory. |
| `app/devices/lakeshore_gaussmeter/ui/page.py` | Manual read and live-monitor UI. |
| `app/settings/models.py`, `.config/settings.yml` | Validated disabled-by-default profile. |
| `app/ui/shell/main_window.py`, `app/ui/dashboard/page.py`, `app/ui/settings_page.py` | Optional-device composition, connection panels and settings surface. |
| `app/recipes/models.py`, `app/engine/compiler.py` | `measure_lakeshore_field` parser and compilation. |
| `app/ui/run_worker.py`, `app/engine/runner.py` | Run-time adapter ownership and scalar measurement mapping. |
| `app/storage/thatec_writer.py` | Human-readable labels and units for Lake Shore scalar rows. |
| `tests/test_lakeshore_gaussmeter.py` | Model, bridge, adapter and simulator tests. |
| `tests/test_recipe_compiler.py`, `tests/test_run_controller.py`, `tests/test_main_window.py`, `tests/test_hdf5_writer.py` | Integration regression coverage. |
| `docs/HIL_QUALIFICATION.md`, `docs/PROCEDURA_OPERATORA.md` | Hardware gate and operator procedure. |

## Task 1: Dependency, settings, domain models, and unavailable adapter

**Files:**

- Modify: `pyproject.toml`, `requirements.txt`, `app/settings/models.py`, `.config/settings.yml`
- Modify: `app/devices/lakeshore_gaussmeter/models.py`, `app/devices/lakeshore_gaussmeter/__init__.py`
- Create: `tests/test_lakeshore_gaussmeter.py`

**Interfaces:**

- Produces `GaussmeterConfig`, `MeasurementMode`, `FieldUnit`, `GaussmeterSnapshot`, `GaussmeterReading`.
- Produces `LakeShoreGaussmeterSettings` with `enabled: bool`, VISA resource, baud rate, serial requirement and live interval.

- [ ] **Step 1: Write failing model and settings tests**

```python
def test_gaussmeter_reading_requires_mode_specific_values() -> None:
    with pytest.raises(ValueError, match="DC reading requires field_t"):
        GaussmeterReading(mode=MeasurementMode.DC, field_t=None, ...)

def test_lakeshore_profile_rejects_live_interval_below_half_second() -> None:
    with pytest.raises(ValidationError, match="live_interval"):
        StationSettings.model_validate(profile_with(live_interval="499 ms"))
```

- [ ] **Step 2: Run the focused tests and confirm red**

Run: `pytest tests/test_lakeshore_gaussmeter.py -q`

Expected: FAIL because the Model 475 types and validated profile fields do not yet exist.

- [ ] **Step 3: Implement the smallest typed model surface**

```python
class MeasurementMode(StrEnum):
    DC = "dc"
    RMS = "rms"
    PEAK = "peak"

@dataclass(frozen=True, slots=True)
class GaussmeterReading:
    mode: MeasurementMode
    unit: FieldUnit
    snapshot: GaussmeterSnapshot
    timestamp_utc: datetime
    field_t: float | None = None
    frequency_hz: float | None = None
    negative_peak_t: float | None = None
    positive_peak_t: float | None = None
```

Make the profile disabled by default and limit baud rates to the four values documented by the manual.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_lakeshore_gaussmeter.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt app/settings/models.py .config/settings.yml app/devices/lakeshore_gaussmeter/models.py app/devices/lakeshore_gaussmeter/__init__.py tests/test_lakeshore_gaussmeter.py
git commit -m "feat(lakeshore): add Model 475 profile and typed readings"
```

## Task 2: Official driver bridge and Model 475 adapter

**Files:**

- Create: `app/devices/lakeshore_gaussmeter/connection.py`
- Modify: `app/devices/lakeshore_gaussmeter/adapter.py`, `app/devices/lakeshore_gaussmeter/simulator.py`, `app/devices/lakeshore_gaussmeter/__init__.py`
- Modify: `tests/test_lakeshore_gaussmeter.py`

**Interfaces:**

- Consumes `GaussmeterConfig` and a VISA `InstrumentSession`.
- Produces `LakeShore475Adapter.read_snapshot() -> GaussmeterSnapshot` and `read_measurement() -> GaussmeterReading`.

- [ ] **Step 1: Add failing bridge and adapter tests**

```python
def test_official_driver_bridge_rejects_every_write() -> None:
    bridge = ReadOnlyLakeShoreConnection(fake_session)
    with pytest.raises(SafetyViolation):
        bridge.write("UNIT 2")

def test_475_adapter_converts_gauss_and_queries_only_read_commands() -> None:
    adapter = LakeShore475Adapter(config, session_factory=factory, driver_factory=fake_driver)
    adapter.connect()
    assert adapter.read_measurement().field_t == pytest.approx(0.0125)
    assert fake_session.writes == ["*IDN?", "UNIT?", "RDGMODE?", "RDGFIELD?", "RDGMODE?", "UNIT?"]
```

Add independent tests for RMS frequency, two peak values, `OL`, Oe/A/m rejection, inconsistent unit retry, IDN/serial rejection, disconnect after failed connect, and actual `Model425(connection=bridge)` over a fake connection.

- [ ] **Step 2: Run and confirm red**

Run: `pytest tests/test_lakeshore_gaussmeter.py -q`

Expected: FAIL because the bridge and public adapter operations are absent.

- [ ] **Step 3: Implement `ReadOnlyLakeShoreConnection` and adapter**

```python
READ_QUERIES = frozenset({"*IDN?", "UNIT?", "RDGMODE?", "RANGE?", "AUTO?", "TYPE?", "RDGFIELD?", "RDGFRQ?", "RDGPEAK?"})

class ReadOnlyLakeShoreConnection:
    def write(self, _command: str) -> None:
        raise SafetyViolation("Lake Shore 475 integration is read-only.")

    def query(self, command: str) -> str:
        if command not in READ_QUERIES:
            raise SafetyViolation(f"Unsupported Lake Shore query {command!r}.")
        self._rate_limit()
        return self._session.query(command)
```

Configure ASRL attributes before constructing `Model425(connection=bridge)`. Parse and validate only the documented Model 475 response formats. Preserve session ownership in the adapter and never expose the bridge publicly.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_lakeshore_gaussmeter.py -q`

Expected: PASS, including the real official package boundary test.

- [ ] **Step 5: Commit**

```bash
git add app/devices/lakeshore_gaussmeter tests/test_lakeshore_gaussmeter.py
git commit -m "feat(lakeshore): add read-only official driver bridge"
```

## Task 3: Module factory, registry, and UI page

**Files:**

- Modify: `app/devices/lakeshore_gaussmeter/module.py`, `app/devices/registry.py`
- Create: `app/devices/lakeshore_gaussmeter/ui/__init__.py`, `app/devices/lakeshore_gaussmeter/ui/page.py`
- Modify: `app/main.py`, `tests/test_device_modules.py`, `tests/test_main_window.py`

**Interfaces:**

- Consumes `DeviceController.call("read_snapshot")` and `call("read_measurement")`.
- Produces a `LakeShore475Page` and module operations with no writable operation.

- [ ] **Step 1: Write failing manifest and offscreen UI tests**

```python
def test_lakeshore_module_creates_a_page_and_dispatches_only_read_operations() -> None:
    module = built_in_device_registry().get("lakeshore_gaussmeter")
    assert module.page_factory is not None
    with pytest.raises(ValueError, match="Unsupported Lake Shore operation"):
        module.dispatch(adapter, "set_unit", "T")

def test_lakeshore_page_stops_live_timer_after_read_error(qtbot) -> None:
    page = LakeShore475Page(controller, settings)
    page._start_live()
    controller.error.emit("read_measurement", "OL")
    assert not page._timer.isActive()
```

- [ ] **Step 2: Run and confirm red**

Run: `pytest tests/test_device_modules.py tests/test_main_window.py -q`

Expected: FAIL because the module has no page factory or page implementation.

- [ ] **Step 3: Implement the page and read-only dispatcher**

Use `QTimer` with a 500 ms floor, an in-flight flag, `Read now`, a live toggle, read-only diagnostic tiles, a timestamp, an error banner, and history plots. Connect only to controller result/error/state signals.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_device_modules.py tests/test_main_window.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/devices/lakeshore_gaussmeter app/devices/registry.py app/main.py tests/test_device_modules.py tests/test_main_window.py
git commit -m "feat(lakeshore): add read-only device page"
```

## Task 4: Application shell, dashboard, settings, and discovery assignment

**Files:**

- Modify: `app/ui/shell/main_window.py`, `app/ui/dashboard/page.py`, `app/ui/settings_page.py`
- Modify: `app/ui/assets/icons/` and `app/main.py`
- Modify: `tests/test_main_window.py`, `tests/test_settings_repository.py`

**Interfaces:**

- Adds `lakeshore_gaussmeter` to the shell device key lists and connection panels.
- Produces a settings tab and dashboard card wired through the existing access policy.

- [ ] **Step 1: Write failing shell and settings tests**

```python
def test_lakeshore_is_present_in_settings_tabs_dashboard_and_toolbar(window) -> None:
    assert "Lake Shore 475" in tab_labels(window)
    assert "lakeshore_gaussmeter" in window.connection_panels
    assert "lakeshore_gaussmeter" in window.dashboard.cards

def test_lakeshore_assignment_persists_only_for_engineer(settings_path, window) -> None:
    window._save_discovered_assignment("lakeshore_gaussmeter", "ASRL3::INSTR")
    assert SettingsRepository(settings_path).load().settings.lakeshore_gaussmeter.resource == "ASRL3::INSTR"
```

- [ ] **Step 2: Run and confirm red**

Run: `pytest tests/test_main_window.py tests/test_settings_repository.py -q`

Expected: FAIL because optional-device shell integration is absent.

- [ ] **Step 3: Implement the generic optional-device wiring**

Add the stable key to all current fixed lists, use the profile display name and resource in dashboard/card code, expose read-only fields in settings, and add an SVG icon following existing icon conventions. Preserve existing access controls and do not enable the device merely by assigning a resource.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_main_window.py tests/test_settings_repository.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/ui/shell/main_window.py app/ui/dashboard/page.py app/ui/settings_page.py app/ui/assets/icons app/main.py tests/test_main_window.py tests/test_settings_repository.py
git commit -m "feat(lakeshore): wire dashboard and settings"
```

## Task 5: Recipe compiler, run worker, and runner measurements

**Files:**

- Modify: `app/recipes/models.py`, `app/engine/compiler.py`, `app/ui/recipes/page.py`
- Modify: `app/ui/run_worker.py`, `app/engine/runner.py`
- Modify: `tests/test_recipe_compiler.py`, `tests/test_run_controller.py`, `tests/test_adapters_and_runner.py`

**Interfaces:**

- Produces recipe type `measure_lakeshore_field` with empty payload.
- Produces scalar keys `lakeshore.field_t`, `lakeshore.frequency_hz`, `lakeshore.negative_peak_t`, `lakeshore.positive_peak_t`, `lakeshore.mode_code`, `lakeshore.unit_code`, `lakeshore.range_code`, `lakeshore.autorange_enabled`, and `lakeshore.probe_type_code`.

- [ ] **Step 1: Write failing compilation and simulated-run tests**

```python
def test_lakeshore_measurement_requires_enabled_resource_and_is_a_checkpoint() -> None:
    plan = RecipeCompiler(enabled_lakeshore_settings()).compile(recipe("measure_lakeshore_field"))
    assert plan.required_devices == frozenset({"lakeshore_gaussmeter"})
    assert plan.total_points == 1

def test_runner_maps_rms_reading_to_stable_scalar_keys() -> None:
    result = run_lakeshore_rms_recipe()
    assert result.point.measurements["lakeshore.field_t"] == pytest.approx(0.1)
    assert result.point.measurements["lakeshore.frequency_hz"] == pytest.approx(60.0)
```

- [ ] **Step 2: Run and confirm red**

Run: `pytest tests/test_recipe_compiler.py tests/test_run_controller.py tests/test_adapters_and_runner.py -q`

Expected: FAIL because parser, compiler, worker and runner do not know the Lake Shore action.

- [ ] **Step 3: Implement the vertical action**

Add the action literal, validate the enabled profile/resource during compilation, include it in required devices and point count, create the adapter only when required, and map the typed reading to finite scalar keys. Do not add Lake Shore to source emergency-off actions; `RunWorker` lifecycle disconnect closes it.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_recipe_compiler.py tests/test_run_controller.py tests/test_adapters_and_runner.py -q`

Expected: PASS for DC, RMS and Peak simulations.

- [ ] **Step 5: Commit**

```bash
git add app/recipes/models.py app/engine/compiler.py app/ui/recipes/page.py app/ui/run_worker.py app/engine/runner.py tests/test_recipe_compiler.py tests/test_run_controller.py tests/test_adapters_and_runner.py
git commit -m "feat(lakeshore): add recipe measurement checkpoint"
```

## Task 6: HDF5 labels, operator documentation, and final qualification

**Files:**

- Modify: `app/storage/thatec_writer.py`, `tests/test_hdf5_writer.py`
- Modify: `docs/HIL_QUALIFICATION.md`, `docs/PROCEDURA_OPERATORA.md`, `docs/NEW_DEVICE_MODULES.md`

**Interfaces:**

- Produces readable thaTEC labels for every Lake Shore scalar key.
- Produces an explicit HIL checklist and operator connection/readout procedure.

- [ ] **Step 1: Write failing storage and documentation-presence tests**

```python
def test_hdf5_exposes_lakeshore_field_as_tesla_indicator(tmp_path) -> None:
    writer = write_point({"lakeshore.field_t": 0.01})
    definition = read_thatec_definition(writer.path, "lakeshore.field_t")
    assert definition["control name"] == "Magnetic field (T)"
```

- [ ] **Step 2: Run and confirm red**

Run: `pytest tests/test_hdf5_writer.py -q`

Expected: FAIL because Lake Shore-specific display metadata is absent.

- [ ] **Step 3: Implement labels and documentation**

Map all nine scalar keys to explicit device, control, and SI unit text. Add the 17-item HIL gate and operator steps for approved RS-232/GPIB connection, Model 475 identity check, read-only limitation, live monitoring, overload response, and recovery.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_hdf5_writer.py -q`

Expected: PASS.

- [ ] **Step 5: Run the full verification suite**

Run: `pytest -q && ruff check app tests`

Expected: all tests pass and ruff reports zero violations.

- [ ] **Step 6: Commit**

```bash
git add app/storage/thatec_writer.py tests/test_hdf5_writer.py docs/HIL_QUALIFICATION.md docs/PROCEDURA_OPERATORA.md docs/NEW_DEVICE_MODULES.md
git commit -m "docs(lakeshore): add HIL and operator qualification"
```

## Plan self-review

- Spec coverage: Tasks 1-2 cover the official bridge, query whitelist, transport, units, identity and all measurement modes; Tasks 3-4 cover module/UI/shell/settings; Task 5 covers recipes and runs; Task 6 covers HDF5, documentation, HIL and full verification.
- Placeholder scan: no deferred implementation markers or unspecified validation remain.
- Interface consistency: `LakeShore475Adapter.read_measurement()` is produced in Task 2, dispatched in Task 3, and consumed in Tasks 5-6.
