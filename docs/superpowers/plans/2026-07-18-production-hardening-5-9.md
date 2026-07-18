# Production Hardening Audit Items 5–9 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close audit findings 5–9 with Unicode-correct quantities, complete readiness, a strictly read-only MOKE product surface, a frozen Lake Shore 475 integration, and a reproducible PyThat environment without the netCDF4 ABI warning path.

**Architecture:** Keep each correction at its authoritative boundary: quantity canonicalisation in the domain parser, device assignment descriptors in readiness, MOKE operations in its module manifest/dispatcher, Lake Shore behavior in its existing vertical module, and PyThat backend selection in one storage bridge. Release dependencies are exact pins separate from the human-maintained compatible ranges.

**Tech Stack:** CPython 3.14.6, PySide6 6.11.1, pytest 9.1.1, Ruff 0.15.21, h5py 3.16.0, h5netcdf 1.8.1, xarray 2026.7.0, PyThat 0.2.14.

## Global Constraints

- MOKE hardware behavior is read-only: VOUT readback and one-sample Hall-1 readback only.
- No physical HIL claim is added; Lake Shore and MOKE remain disabled/unqualified by default.
- Existing recipe files containing `configure_moke_box` must fail with an explicit qualification error, not become unparsable.
- `mΩ` means milliohm (`1e-3 Ω`); `MΩ` means megaohm (`1e6 Ω`).
- PyThat must be exactly `0.2.14` and use `h5netcdf` for its temporary netCDF sidecar.
- Every behavior change follows RED → GREEN before production code changes.
- Preserve unrelated user changes and stage only files belonging to the current task.

---

### Task 1: Unicode-correct quantity parser

**Files:**
- Modify: `tests/test_settings_and_safety.py`
- Modify: `app/domain/quantities.py`

**Interfaces:**
- Consumes: `parse_quantity(value, expected_dimension)` and `Quantity.format(unit)`.
- Produces: `_canonical_unit(unit: str) -> str` used only by `_unit_definition`.

- [ ] **Step 1: Add failing Unicode regression tests**

Add independent tests:

```python
def test_unicode_ohm_units_preserve_si_prefix_case(self) -> None:
    self.assertEqual(parse_quantity("50 Ω", DIMENSION_RESISTANCE).si_value, 50.0)
    self.assertEqual(parse_quantity("1 kΩ", DIMENSION_RESISTANCE).si_value, 1_000.0)
    self.assertEqual(parse_quantity("1 MΩ", DIMENSION_RESISTANCE).si_value, 1_000_000.0)
    self.assertEqual(parse_quantity("1 mΩ", DIMENSION_RESISTANCE).si_value, 0.001)

def test_microtesla_accepts_micro_sign_and_greek_mu(self) -> None:
    self.assertEqual(parse_quantity("1 µT", DIMENSION_MAGNETIC_FIELD).si_value, 1e-6)
    self.assertEqual(parse_quantity("1 μT", DIMENSION_MAGNETIC_FIELD).si_value, 1e-6)
```

- [ ] **Step 2: Run RED**

Run:

```text
python -m pytest -q tests/test_settings_and_safety.py -k "unicode_ohm or microtesla"
```

Expected: failures for `Ω`, `kΩ`, `µT`, and incorrect `mΩ` scale.

- [ ] **Step 3: Implement canonicalisation**

Import `unicodedata`. Replace Unicode keys in `_UNITS` with canonical ASCII
keys and add:

```python
def _canonical_unit(unit: str) -> str:
    normalized = unicodedata.normalize("NFKC", unit.strip()).replace("μ", "u").replace("µ", "u")
    omega_aliases = {
        "Ω": "ohm",
        "ω": "ohm",
        "kΩ": "kohm",
        "KΩ": "kohm",
        "kω": "kohm",
        "Kω": "kohm",
        "MΩ": "Mohm",
        "Mω": "Mohm",
        "mΩ": "milliohm",
        "mω": "milliohm",
    }
    return omega_aliases.get(normalized, normalized.lower())
```

Define:

```python
"milliohm": UnitDefinition(DIMENSION_RESISTANCE, 1e-3, "mΩ")
```

Keep `"mohm"` as the case-insensitive legacy spelling for megaohm. Remove the
corrupted `Âµt` key. Make `_unit_definition()` call `_canonical_unit()`.

- [ ] **Step 4: Run GREEN and parser regression**

Run:

```text
python -m pytest -q tests/test_settings_and_safety.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```text
git add app/domain/quantities.py tests/test_settings_and_safety.py
git commit -m "fix: normalize Unicode SI units"
```

### Task 2: Complete five-device readiness and RF output detection

**Files:**
- Modify: `tests/test_station_readiness.py`
- Modify: `app/domain/readiness.py`

**Interfaces:**
- Consumes: `StationSettings`, `ExecutionPlan.required_devices`,
  `device_states`, `verified_resources`, and `device_errors`.
- Produces: `_readiness_devices(settings) -> tuple[tuple[str, str, bool, str | None], ...]`
  and `_ENERGIZING_OUTPUT_ACTIONS`.

- [ ] **Step 1: Add failing readiness tests**

Add tests that build approved settings and assert:

```python
def test_required_moke_and_lakeshore_are_reported(self) -> None:
    # Plan requires both; MOKE has no endpoint and Lake Shore is disabled.
    # Both device.moke_box and device.lakeshore_gaussmeter must block readiness.

def test_anritsu_rf_output_is_reported_as_energized(self) -> None:
    action = PlanAction("rf-on", "set_anritsu_sg_output", {"enabled": True}, {})
    # DUT detail must contain "validated", never "no OUTPUT ON".
```

Also add a positive identity-verification test using a MOKE endpoint and a Lake
Shore VISA resource.

- [ ] **Step 2: Run RED**

Run:

```text
python -m pytest -q tests/test_station_readiness.py
```

Expected: missing readiness items and incorrect RF DUT text.

- [ ] **Step 3: Implement device descriptors**

Add:

```python
_ENERGIZING_OUTPUT_ACTIONS = frozenset(
    {"set_rigol_output", "set_keithley_output", "set_anritsu_sg_output"}
)

def _readiness_devices(settings: StationSettings) -> tuple[tuple[str, str, bool, str | None], ...]:
    return (
        ("rigol", settings.rigol.display_name, settings.rigol.enabled, settings.rigol.connection.resource),
        ("keithley", settings.keithley.display_name, settings.keithley.enabled, settings.keithley.connection.resource),
        ("anritsu", settings.anritsu.display_name, settings.anritsu.enabled, settings.anritsu.connection.resource),
        ("moke_box", settings.moke_box.display_name, settings.moke_box.enabled, settings.moke_box.endpoint),
        (
            "lakeshore_gaussmeter",
            settings.lakeshore_gaussmeter.display_name,
            settings.lakeshore_gaussmeter.enabled,
            settings.lakeshore_gaussmeter.resource,
        ),
    )
```

Refactor the existing loop to consume these values without changing the current
severity rules. Use `_ENERGIZING_OUTPUT_ACTIONS` in the plan check.

- [ ] **Step 4: Run GREEN**

Run:

```text
python -m pytest -q tests/test_station_readiness.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```text
git add app/domain/readiness.py tests/test_station_readiness.py
git commit -m "fix: cover all devices in station readiness"
```

### Task 3: Reduce MOKE to a qualified read-only product surface

**Files:**
- Modify: `tests/test_device_modules.py`
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_recipe_builder.py`
- Modify: `tests/test_recipe_compiler.py`
- Modify: `app/devices/moke_box/adapter.py`
- Modify: `app/devices/moke_box/module.py`
- Modify: `app/devices/moke_box/ui/page.py`
- Modify: `app/recipes/parameter_registry.py`
- Modify: `app/settings/models.py`

**Interfaces:**
- Consumes: `MODULE.dispatch`, `MODULE.capabilities`,
  `parameter_definitions_for_module("moke_box")`, `MokeBoxSettings`.
- Produces: a read-only dispatcher accepting only `read_signal`, `read_vouts`,
  and `read_hall_voltage`.

- [ ] **Step 1: Add failing dispatcher/capability tests**

Extend `test_moke_adapter_exposes_only_measurement_path` to assert:

```python
self.assertEqual(
    registry.get("moke_box").capabilities,
    frozenset({"read_only", "vout_readback", "hall_voltage_readback"}),
)
for operation in (
    "acquire_samples",
    "read_fields",
    "set_hall_gains",
    "set_kerr_gain",
    "set_vout",
    "ramp_vout",
):
    with self.assertRaisesRegex(ValueError, "Unsupported MOKE Box operation"):
        registry.get("moke_box").dispatch(adapter, operation, {})
```

Add a settings test proving `allow_vout_control=True` is rejected even when
`protocol_qualified=True`.

- [ ] **Step 2: Add failing UI and Sweeper tests**

In `tests/test_main_window.py`, assert the MOKE page:

```python
self.assertFalse(hasattr(window.moke_box_page, "acquire_button"))
self.assertFalse(hasattr(window.moke_box_page, "stream_table"))
```

In recipe tests, assert:

```python
self.assertNotIn("moke_box.field_target", PARAMETERS_BY_TARGET)
self.assertEqual(parameter_definitions_for_module("moke_box"), ())
```

Keep the `measure_moke_hall` library-block assertion.

- [ ] **Step 3: Run RED**

Run:

```text
python -m pytest -q tests/test_device_modules.py tests/test_main_window.py tests/test_recipe_builder.py tests/test_recipe_compiler.py -k "moke"
```

Expected: failures showing stream/write operations and field axis are still exposed.

- [ ] **Step 4: Implement dispatcher and capability boundary**

In `module.py`, reduce the whitelist:

```python
if operation not in {"read_signal", "read_vouts", "read_hall_voltage"}:
    raise ValueError(f"Unsupported MOKE Box operation {operation!r}.")
```

Only `read_hall_voltage` accepts a mapping with `count`. Construct the physical
adapter with `allow_vout_control=False` and no allowed VOUT channels. Set module
capabilities to:

```python
frozenset({"read_only", "vout_readback", "hall_voltage_readback"})
```

Make the connected adapter advertise the same three public capabilities.

- [ ] **Step 5: Enforce read-only settings**

Keep compatibility fields but add to `MokeBoxSettings.validate_timeout()`:

```python
if self.allow_vout_control or self.allowed_vout_channels:
    raise ValueError("MOKE Box is read-only until VOUT control completes physical HIL.")
```

- [ ] **Step 6: Remove the unqualified UI and axis**

Remove the stream tab construction, `_build_stream_view`, `_acquire_streams`,
stream-result rendering and stream-only imports/widgets. Remove the
`moke_box.field_target` descriptor and its ordering entry. Do not remove the
read-only Hall recipe block or legacy model recognition for
`configure_moke_box`.

- [ ] **Step 7: Run GREEN**

Run:

```text
python -m pytest -q tests/test_device_modules.py tests/test_main_window.py tests/test_recipe_builder.py tests/test_recipe_compiler.py -k "moke"
```

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

```text
git add app/devices/moke_box app/recipes/parameter_registry.py app/settings/models.py tests/test_device_modules.py tests/test_main_window.py tests/test_recipe_builder.py tests/test_recipe_compiler.py
git commit -m "fix: restrict MOKE to qualified read-only operations"
```

### Task 4: Freeze Lake Shore 475 vertical integration

**Files:**
- Modify: `tests/test_device_modules.py`
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_recipe_builder.py`
- Modify: `tests/test_recipe_compiler.py`
- Modify: `tests/test_simulated_run.py`
- Modify only if a RED test proves a gap:
  `app/devices/lakeshore_gaussmeter/`, `app/ui/run_worker.py`,
  `app/engine/compiler.py`, `app/engine/runner.py`, `app/storage/thatec_writer.py`

**Interfaces:**
- Consumes: existing `LakeShore475Adapter`, `MODULE`, `measure_lakeshore_field`,
  `RecipeRunner`, and HDF5 writer.
- Produces: no new public API; this task proves the approved read-only design on
  one commit.

- [ ] **Step 1: Add missing vertical tests**

Add focused tests for:

```python
def test_lakeshore_page_is_read_only_and_live_is_inflight_guarded(self) -> None:
    # No editable instrument configuration controls.
    # A second timer tick while read_measurement is pending sends no second call.

def test_lakeshore_recipe_compiles_runs_and_round_trips(self) -> None:
    # Enabled simulated profile + measure_lakeshore_field.
    # Required device is lakeshore_gaussmeter.
    # Exactly one checkpoint is stored and PyThat opens it.
```

Assert the traffic log contains only the approved query whitelist and no writes.

- [ ] **Step 2: Run RED or prove existing coverage**

Run:

```text
python -m pytest -q tests/test_device_modules.py tests/test_main_window.py tests/test_recipe_builder.py tests/test_recipe_compiler.py tests/test_simulated_run.py -k "lakeshore"
```

If the new tests pass immediately, record that the behavior already exists and
do not modify production code for that behavior. If a test fails, confirm the
failure is the intended missing vertical contract before editing.

- [ ] **Step 3: Implement only proven gaps**

Use the existing Lake Shore design. Do not add commands, models or output
operations. The only permitted production edits are those directly required by
the failing vertical tests.

- [ ] **Step 4: Run the full Lake Shore focused suite**

Run:

```text
python -m pytest -q tests/test_device_modules.py tests/test_main_window.py tests/test_recipe_builder.py tests/test_recipe_compiler.py tests/test_adapters_and_runner.py tests/test_simulated_run.py -k "lakeshore"
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```text
git add app/devices/lakeshore_gaussmeter app/ui/run_worker.py app/engine/compiler.py app/engine/runner.py app/storage/thatec_writer.py tests
git commit -m "test: freeze Lake Shore 475 read-only integration"
```

### Task 5: Central PyThat bridge using h5netcdf

**Files:**
- Create: `app/storage/pythat_bridge.py`
- Modify: `app/storage/pythat_reader.py`
- Modify: `app/storage/thatec_validator.py`
- Modify: `tests/test_hdf5_writer.py`
- Modify: `tests/test_thatec_validator.py`

**Interfaces:**
- Produces:

```python
def open_measurement_tree(path: str | Path) -> object
```

- Consumes: exact PyThat 0.2.14, xarray `set_options`, and the installed
  `h5netcdf` backend.

- [ ] **Step 1: Add a failing warning-as-error bridge test**

Create a normal HDF5 run, then:

```python
with warnings.catch_warnings():
    warnings.simplefilter("error", RuntimeWarning)
    data = read_pythat_run_data(path)
self.assertEqual(data.dimensions["Checkpoint"], 1)
```

Also assert `netCDF4` is not newly imported by the bridge when removed from
`sys.modules` before the call.

- [ ] **Step 2: Run RED**

Run:

```text
python -W error::RuntimeWarning -m pytest -q tests/test_hdf5_writer.py tests/test_thatec_validator.py
```

Expected: PyThat/netCDF4 ABI warning fails the run.

- [ ] **Step 3: Implement `open_measurement_tree`**

Implement:

```python
def open_measurement_tree(path: str | Path) -> object:
    if version("PyThat") != "0.2.14":
        raise ExecutionError("PyThat 0.2.14 is required by the qualified HDF5 contract.")
    import xarray as xr
    if "h5netcdf" not in xr.backends.list_engines():
        raise ExecutionError("The qualified PyThat bridge requires h5netcdf.")
    from PyThat import MeasurementTree
    target = Path(path)
    sidecar = target.with_suffix(".nc")
    try:
        with xr.set_options(netcdf_engine_order=["h5netcdf", "scipy", "netcdf4"]):
            tree = MeasurementTree(target, index=True, override=True)
        tree.dataset.load()
        return tree
    except Exception as exc:
        raise ExecutionError(f"PyThat cannot open this result file through h5netcdf: {exc}") from exc
    finally:
        handle = getattr(locals().get("tree"), "f", None)
        if handle is not None:
            handle.close()
        sidecar.unlink(missing_ok=True)
```

Keep sidecar deletion scoped to `target.with_suffix(".nc")`.

- [ ] **Step 4: Route both callers through the bridge**

`pythat_reader.py` uses `open_measurement_tree`. The validator catches
`ExecutionError` and records the existing compatibility issue text. Remove
direct `MeasurementTree` imports from production callers.

- [ ] **Step 5: Run GREEN**

Run:

```text
python -W error::RuntimeWarning -m pytest -q tests/test_hdf5_writer.py tests/test_thatec_validator.py tests/test_results_page.py
```

Expected: PASS with no RuntimeWarning and no lingering `.nc` files.

- [ ] **Step 6: Commit Task 5**

```text
git add app/storage/pythat_bridge.py app/storage/pythat_reader.py app/storage/thatec_validator.py tests/test_hdf5_writer.py tests/test_thatec_validator.py
git commit -m "fix: isolate PyThat on qualified h5netcdf backend"
```

### Task 6: Exact runtime locks and environment checker

**Files:**
- Create: `.python-version`
- Create: `requirements.lock.txt`
- Create: `requirements-dev.lock.txt`
- Create: `tools/check_locked_environment.py`
- Create: `tests/test_locked_environment.py`
- Modify: `requirements.txt`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces:

```python
def parse_lock(path: Path) -> tuple[str, dict[str, str]]
def check_environment(lock_path: Path, python_version: str | None = None) -> tuple[str, ...]
```

- [ ] **Step 1: Add failing checker tests**

Tests create temporary locks and assert:

```python
python_version, packages = parse_lock(lock)
self.assertEqual(python_version, "3.14.6")
self.assertEqual(packages["numpy"], "2.5.1")
self.assertEqual(check_environment(lock, python_version="3.14.5"), ("Python 3.14.5 != locked 3.14.6",))
```

Use `unittest.mock.patch("importlib.metadata.version", ...)` only at the
package-metadata boundary.

- [ ] **Step 2: Run RED**

Run:

```text
python -m pytest -q tests/test_locked_environment.py
```

Expected: import failure because the checker does not exist.

- [ ] **Step 3: Implement the checker**

The lock parser accepts:

```text
# python==3.14.6
PackageName==version
```

It ignores blank/comment lines, rejects non-exact requirements, canonicalises
names with `packaging.utils.canonicalize_name`, and returns every mismatch.
CLI exit is 0 for a match and 1 after printing mismatches.

- [ ] **Step 4: Add exact lock files**

`.python-version`:

```text
3.14.6
```

`requirements.lock.txt` pins exactly:

```text
annotated-types==0.6.0
certifi==2026.6.17
cftime==1.6.5
click==8.4.1
cloudpickle==3.1.2
colorama==0.4.6
contourpy==1.3.3
cycler==0.12.1
dask==2026.7.1
fonttools==4.63.0
fsspec==2026.6.0
h5netcdf==1.8.1
h5py==3.16.0
iso8601==2.1.0
kiwisolver==1.5.0
lakeshore==1.10.0
locket==1.0.0
matplotlib==3.11.0
netCDF4==1.7.4
numpy==2.5.1
packaging==26.0
pandas==3.0.3
partd==1.4.2
pillow==12.3.0
pydantic==2.13.4
pydantic_core==2.46.4
pyparsing==3.3.2
pyqtgraph==0.13.7
pyserial==3.5
PySide6==6.11.1
PySide6_Addons==6.11.1
PySide6_Essentials==6.11.1
PyThat==0.2.14
python-dateutil==2.9.0.post0
PyVISA==1.16.2
PyVISA-py==0.8.1
PyYAML==6.0.3
ruamel.yaml==0.18.16
scipy==1.18.0
shiboken6==6.11.1
six==1.17.0
toolz==1.1.0
typing_extensions==4.15.0
typing-inspection==0.4.2
tzdata==2026.3
wakepy==1.0.0
xarray==2026.7.0
```

`requirements-dev.lock.txt` includes `-r requirements.lock.txt` and exact:

```text
iniconfig==2.3.0
pluggy==1.6.0
Pygments==2.20.0
pytest==9.1.1
ruff==0.15.21
```

Add `h5netcdf>=1.8,<2` to compatible ranges in `pyproject.toml` and
`requirements.txt`.

- [ ] **Step 5: Run GREEN and real environment check**

Run:

```text
python -m pytest -q tests/test_locked_environment.py
python tools/check_locked_environment.py requirements-dev.lock.txt
```

Expected: PASS and `Environment matches ...`.

- [ ] **Step 6: Commit Task 6**

```text
git add .python-version requirements.lock.txt requirements-dev.lock.txt requirements.txt pyproject.toml tools/check_locked_environment.py tests/test_locked_environment.py
git commit -m "build: lock qualified production environment"
```

### Task 7: Full verification and audit update

**Files:**
- Modify: `docs/AUDYT_GOTOWOSCI_PRODUKCYJNEJ_2026-07-18.md`

**Interfaces:**
- Consumes: all acceptance evidence from Tasks 1–6.
- Produces: an updated audit that marks findings 5–9 as software-remediated
  while retaining NO-GO until physical HIL and other P0 findings are closed.

- [ ] **Step 1: Run focused safety and storage suites**

Run:

```text
python -m pytest -q tests/test_settings_and_safety.py tests/test_station_readiness.py tests/test_device_modules.py tests/test_recipe_builder.py tests/test_recipe_compiler.py tests/test_hdf5_writer.py tests/test_thatec_validator.py tests/test_locked_environment.py
```

Expected: PASS.

- [ ] **Step 2: Run complete verification**

Run:

```text
python -m ruff check app tests tools
python -m compileall -q app tests tools
python -m pytest -q
git diff --check
python tools/check_locked_environment.py requirements-dev.lock.txt
```

Expected: all PASS. The full pytest warning summary must contain no NumPy ABI
RuntimeWarning.

- [ ] **Step 3: Update the audit**

In the report:

- mark Unicode units fixed with exact accepted spellings;
- state readiness now includes all five devices and Anritsu RF;
- state MOKE product surfaces are read-only;
- record the frozen Lake Shore focused/full test evidence;
- record the `h5netcdf` bridge, lock artifacts and environment-check result;
- keep the overall release verdict NO-GO until HIL and remaining P0 items close.

- [ ] **Step 4: Re-run report and diff checks**

Run:

```text
rg -n "Ω|µT|readiness|MOKE|Lake Shore|h5netcdf|lock" docs/AUDYT_GOTOWOSCI_PRODUKCYJNEJ_2026-07-18.md
git diff --check
git status --short
```

Expected: only intentional task files are changed.

- [ ] **Step 5: Commit Task 7**

```text
git add docs/AUDYT_GOTOWOSCI_PRODUKCYJNEJ_2026-07-18.md
git commit -m "docs: record remediation of audit items 5-9"
```

- [ ] **Step 6: Final clean-commit verification**

Run the complete verification commands once more on the final commit and record
the commit SHA, test count, warning count, Ruff result and lock-check result in
the handoff.
