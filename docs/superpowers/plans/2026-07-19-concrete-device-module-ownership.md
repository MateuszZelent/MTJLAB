# Concrete Device Module Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every device implementation into its concrete hardware-family package and remove the ambiguous generic Rigol, Keithley, and Anritsu packages without changing persisted device keys.

**Architecture:** `DeviceModule.key` remains the stable identifier used by settings, recipes, safety policies, runtime dictionaries, and HDF5 data. A new `DeviceModule.implementation_key` identifies the concrete Python package that owns the complete vertical slice. The registry and architecture tests enforce uniqueness and ensure every manifest comes from its declared concrete package.

**Tech Stack:** Python 3.14, PySide6, dataclasses, unittest/pytest, YAML settings, HDF5 storage.

## Global Constraints

- Preserve persisted keys exactly: `rigol`, `keithley`, `anritsu`, `moke_box`, and `lakeshore_gaussmeter`.
- Concrete implementation keys are `rigol_dg1000z`, `keithley_2600`, `anritsu_ms2830a`, `moke_box`, and `lakeshore_475`.
- Do not change supported hardware, SCPI commands, safety limits, output-off behavior, recipe schemas, settings paths, or HDF5 field names.
- Do not retain `app.devices.rigol`, `app.devices.keithley`, or `app.devices.anritsu` as compatibility facades.
- Keep the current uncommitted Results/Dashboard UI work intact and outside migration commits.
- Use test-first development for every contract or behavior change.

---

## File Structure

The final device packages are:

```text
app/devices/
  rigol_dg1000z/
    __init__.py
    adapter.py
    module.py
    ui/
  keithley_2600/
    __init__.py
    adapter.py
    module.py
    ui/
  anritsu_ms2830a/
    __init__.py
    adapter.py
    hardware.py
    module.py
    ui/
  moke_box/
    adapter.py
    models.py
    module.py
    protocol.py
    simulator.py
    transport.py
    ui/
  lakeshore_475/
    __init__.py
    adapter.py
    models.py
    module.py
    simulator.py
    ui/
```

`app/contracts/device_module.py` owns the distinction between persisted
identity and implementation ownership. `app/devices/registry.py` is the only
composition root for built-in modules. `tests/test_architecture.py` prevents
generic packages and imports from returning.

### Task 1: Make concrete ownership an explicit module contract

**Files:**
- Modify: `app/contracts/device_module.py`
- Modify: `app/devices/rigol_dg1000z/module.py`
- Modify: `app/devices/keithley_2600/module.py`
- Modify: `app/devices/anritsu_ms2830a/module.py`
- Modify: `app/devices/moke_box/module.py`
- Modify: `app/devices/lakeshore_gaussmeter/module.py`
- Test: `tests/test_device_modules.py`

**Interfaces:**
- Consumes: existing `DeviceModule.key: str` persisted identity.
- Produces: `DeviceModule.implementation_key: str`, a unique concrete Python package name.

- [ ] **Step 1: Write failing contract tests**

Add these tests to `tests/test_device_modules.py`:

```python
def test_registry_exposes_unique_concrete_implementation_keys(self) -> None:
    modules = built_in_device_registry().all_modules()
    self.assertEqual(
        {module.implementation_key for module in modules},
        {
            "rigol_dg1000z",
            "keithley_2600",
            "anritsu_ms2830a",
            "moke_box",
            "lakeshore_475",
        },
    )
    self.assertEqual(
        {module.key for module in modules},
        {"rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"},
    )

def test_registry_rejects_duplicate_implementation_keys(self) -> None:
    first = replace(
        built_in_device_registry().get("rigol"),
        key="first",
        implementation_key="same_family",
        recipe_extension=None,
    )
    second = replace(
        first,
        key="second",
        implementation_key="same_family",
    )
    with self.assertRaisesRegex(ValueError, "implementation keys must be unique"):
        DeviceModuleRegistry((first, second))
```

Import `replace` from `dataclasses` and `DeviceModuleRegistry` from
`app.contracts` in that test file.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m pytest tests/test_device_modules.py -q
```

Expected: FAIL because `DeviceModule` has no `implementation_key`.

- [ ] **Step 3: Add the minimal contract**

Add the required field after `key` in `DeviceModule`:

```python
implementation_key: str
```

In `DeviceModuleRegistry.__init__`, after validating persisted keys, add:

```python
implementation_keys = [module.implementation_key for module in module_list]
if len(implementation_keys) != len(set(implementation_keys)):
    raise ValueError("Device module implementation keys must be unique.")
if any(not key or not key.isidentifier() for key in implementation_keys):
    raise ValueError("Device module implementation keys must be valid Python identifiers.")
```

Set `implementation_key` in each manifest to the exact values asserted by the
test. Lake Shore temporarily declares `lakeshore_475` even though its package
move occurs in Task 5.

- [ ] **Step 4: Run contract and architecture tests**

Run:

```powershell
python -m pytest tests/test_device_modules.py tests/test_architecture.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the contract**

```powershell
git add app/contracts/device_module.py app/devices/*/module.py tests/test_device_modules.py
git commit -m "refactor: distinguish device data keys from implementations"
```

### Task 2: Consolidate the Rigol DG1000Z module

**Files:**
- Move: `app/devices/rigol/adapter.py` to `app/devices/rigol_dg1000z/adapter.py`
- Modify: `app/devices/rigol_dg1000z/__init__.py`
- Modify imports in all files returned by:
  `rg -l "app\.devices\.rigol" app tests -g "*.py"`
- Delete: `app/devices/rigol/__init__.py`
- Delete directory after it is empty: `app/devices/rigol`
- Test: `tests/test_architecture.py`
- Test: `tests/test_adapters_and_runner.py`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: persisted key `rigol` and existing public symbols from `app.devices.rigol`.
- Produces: the same symbols from `app.devices.rigol_dg1000z`.

- [ ] **Step 1: Add a failing ownership test**

Add this reusable assertion to `tests/test_architecture.py`:

```python
def test_generic_device_packages_do_not_exist(self) -> None:
    for package in ("rigol", "keithley", "anritsu"):
        self.assertFalse(
            (ROOT / "app" / "devices" / package).exists(),
            f"generic device package app.devices.{package} must not exist",
        )
```

For this task, temporarily narrow the tuple to `("rigol",)`; Tasks 3 and 4
extend it after their migrations.

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_architecture.py::ArchitectureTests::test_generic_device_packages_do_not_exist -q
```

Expected: FAIL because `app/devices/rigol` exists.

- [ ] **Step 3: Move the implementation and update exports**

Move `adapter.py` without altering its contents. Replace the wildcard facade in
`app/devices/rigol_dg1000z/__init__.py` with explicit exports copied from the
old `app/devices/rigol/__init__.py`, changing local imports to:

```python
from app.devices.rigol_dg1000z.adapter import (
    RigolAdapter,
    RigolBurstConfig,
    RigolChannelConfig,
    RigolFrequencySweepConfig,
    RigolModulationConfig,
    RigolOutputConfig,
)
from app.devices.rigol_dg1000z.module import MODULE

__all__ = [
    "MODULE",
    "RigolAdapter",
    "RigolBurstConfig",
    "RigolChannelConfig",
    "RigolFrequencySweepConfig",
    "RigolModulationConfig",
    "RigolOutputConfig",
]
```

Update every exact import prefix:

```text
app.devices.rigol -> app.devices.rigol_dg1000z
```

Do not replace persisted string values such as `"rigol"`, mapping keys,
settings attributes, operation names, or safety action names.

- [ ] **Step 4: Prove no old imports remain**

Run:

```powershell
rg -n "app\.devices\.rigol(?:\.| import)" app tests -g "*.py"
```

Expected: no output and exit code 1.

- [ ] **Step 5: Run Rigol and architecture tests**

Run:

```powershell
python -m pytest tests/test_architecture.py tests/test_device_modules.py tests/test_adapters_and_runner.py tests/test_main_window.py -q
```

Expected: PASS. If the full UI file exceeds the command timeout, rerun failed
node IDs individually and then run the complete suite during Task 7.

- [ ] **Step 6: Commit the Rigol migration**

```powershell
git add app tests
git commit -m "refactor: consolidate Rigol DG1000Z module"
```

### Task 3: Consolidate the Keithley 2600 module

**Files:**
- Move: `app/devices/keithley/adapter.py` to `app/devices/keithley_2600/adapter.py`
- Modify: `app/devices/keithley_2600/__init__.py`
- Modify imports in all files returned by:
  `rg -l "app\.devices\.keithley" app tests -g "*.py"`
- Delete: `app/devices/keithley/__init__.py`
- Delete directory after it is empty: `app/devices/keithley`
- Test: `tests/test_architecture.py`
- Test: `tests/test_adapters_and_runner.py`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: persisted key `keithley` and existing public symbols from `app.devices.keithley`.
- Produces: the same symbols from `app.devices.keithley_2600`.

- [ ] **Step 1: Extend the failing package test**

Change the tuple in `test_generic_device_packages_do_not_exist` to:

```python
("rigol", "keithley")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_architecture.py::ArchitectureTests::test_generic_device_packages_do_not_exist -q
```

Expected: FAIL for `app.devices.keithley`.

- [ ] **Step 3: Move code and update imports**

Move the adapter unchanged. Replace the wildcard facade with the complete
explicit export list from the old package and local concrete imports:

```python
from app.devices.keithley_2600.adapter import (
    KeithleyAdapter,
    KeithleyRampRequest,
    KeithleyRampResult,
    build_keithley_ramp_levels,
)
from app.safety.keithley import KeithleySourceRequest
from app.devices.keithley_2600.module import MODULE

__all__ = [
    "MODULE",
    "KeithleyAdapter",
    "KeithleyRampRequest",
    "KeithleyRampResult",
    "KeithleySourceRequest",
    "build_keithley_ramp_levels",
]
```

Replace only Python import prefixes:

```text
app.devices.keithley -> app.devices.keithley_2600
```

Keep every persisted `"keithley"` value unchanged.

- [ ] **Step 4: Prove no old imports remain**

Run:

```powershell
rg -n "app\.devices\.keithley(?:\.| import)" app tests -g "*.py"
```

Expected: no output and exit code 1.

- [ ] **Step 5: Run Keithley and architecture tests**

Run:

```powershell
python -m pytest tests/test_architecture.py tests/test_device_modules.py tests/test_adapters_and_runner.py tests/test_main_window.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the Keithley migration**

```powershell
git add app tests
git commit -m "refactor: consolidate Keithley 2600 module"
```

### Task 4: Consolidate the Anritsu MS2830A module

**Files:**
- Move: `app/devices/anritsu/adapter.py` to `app/devices/anritsu_ms2830a/adapter.py`
- Move: `app/devices/anritsu/hardware.py` to `app/devices/anritsu_ms2830a/hardware.py`
- Modify: `app/devices/anritsu_ms2830a/__init__.py`
- Modify imports in all files returned by:
  `rg -l "app\.devices\.anritsu" app tests -g "*.py"`
- Delete: `app/devices/anritsu/__init__.py`
- Delete directory after it is empty: `app/devices/anritsu`
- Test: `tests/test_architecture.py`
- Test: `tests/test_anritsu_hardware.py`
- Test: `tests/test_reference_store.py`
- Test: `tests/test_adapters_and_runner.py`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: persisted key `anritsu` and existing adapter/hardware exports.
- Produces: unchanged public symbols under `app.devices.anritsu_ms2830a`.

- [ ] **Step 1: Complete the failing generic-package test**

Set the tuple to its final value:

```python
("rigol", "keithley", "anritsu")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_architecture.py::ArchitectureTests::test_generic_device_packages_do_not_exist -q
```

Expected: FAIL for `app.devices.anritsu`.

- [ ] **Step 3: Move adapter and hardware ownership**

Move both files without behavior changes. Recreate the concrete package exports
from the old explicit export list:

```python
from importlib import import_module
from typing import Any

from app.devices.anritsu_ms2830a.module import MODULE

__all__ = [
    "MODULE",
    "ANRITSU_FREQUENCY_OPTIONS",
    "ANRITSU_PREAMPLIFIER_OPTIONS",
    "ANRITSU_SIGNAL_GENERATOR_OPTIONS",
    "AdvancedSpectrumConfig",
    "AdvancedSpectrumSnapshot",
    "AnritsuAdapter",
    "AnritsuConfigurationSnapshot",
    "AnritsuFrequencyOption",
    "ReferenceSpectrum",
    "SignalGeneratorConfig",
    "SignalGeneratorSnapshot",
    "SpectrumConfig",
    "SpectrumTrace",
    "frequency_option_for",
    "parse_anritsu_option_response",
]

_HARDWARE_EXPORTS = frozenset(
    {
        "ANRITSU_FREQUENCY_OPTIONS",
        "ANRITSU_PREAMPLIFIER_OPTIONS",
        "ANRITSU_SIGNAL_GENERATOR_OPTIONS",
        "AnritsuFrequencyOption",
        "frequency_option_for",
        "parse_anritsu_option_response",
    }
)

def __getattr__(name: str) -> Any:
    if name == "MODULE":
        return MODULE
    if name not in __all__:
        raise AttributeError(name)
    module_name = (
        "app.devices.anritsu_ms2830a.hardware"
        if name in _HARDWARE_EXPORTS
        else "app.devices.anritsu_ms2830a.adapter"
    )
    return getattr(import_module(module_name), name)
```

Update internal adapter-to-hardware imports and all repository imports:

```text
app.devices.anritsu -> app.devices.anritsu_ms2830a
```

Keep persisted `"anritsu"` values and safety action strings unchanged.

- [ ] **Step 4: Prove no old imports remain**

Run:

```powershell
rg -n "app\.devices\.anritsu(?:\.| import)" app tests -g "*.py"
```

Expected: no output and exit code 1.

- [ ] **Step 5: Run Anritsu and architecture tests**

Run:

```powershell
python -m pytest tests/test_architecture.py tests/test_device_modules.py tests/test_anritsu_hardware.py tests/test_reference_store.py tests/test_adapters_and_runner.py tests/test_main_window.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the Anritsu migration**

```powershell
git add app tests
git commit -m "refactor: consolidate Anritsu MS2830A module"
```

### Task 5: Rename the Lake Shore package to the concrete model

**Files:**
- Move directory contents: `app/devices/lakeshore_gaussmeter/` to `app/devices/lakeshore_475/`
- Modify: `app/devices/lakeshore_475/module.py`
- Modify: `app/devices/registry.py`
- Modify imports in all files returned by:
  `rg -l "app\.devices\.lakeshore_gaussmeter" app tests -g "*.py"`
- Test: `tests/test_architecture.py`
- Test: `tests/test_device_modules.py`
- Test: `tests/test_simulators.py`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: persisted key and settings key `lakeshore_gaussmeter`.
- Produces: implementation package `app.devices.lakeshore_475`.

- [ ] **Step 1: Add a failing manifest-origin test**

Add to `tests/test_architecture.py`:

```python
def test_registered_manifests_are_owned_by_their_concrete_packages(self) -> None:
    from importlib import import_module

    from app.devices.registry import built_in_device_registry

    for module in built_in_device_registry().all_modules():
        owner = import_module(
            f"app.devices.{module.implementation_key}.module"
        )
        self.assertIs(owner.MODULE, module)
```

The dynamic import fails for `lakeshore_475` before the move.

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/test_architecture.py::ArchitectureTests::test_registered_manifests_are_owned_by_their_concrete_packages -q
```

Expected: FAIL with `ModuleNotFoundError: app.devices.lakeshore_475`.

- [ ] **Step 3: Move the package and update imports**

Move every tracked file to `app/devices/lakeshore_475`, preserving the internal
layout. Replace only Python import prefixes:

```text
app.devices.lakeshore_gaussmeter -> app.devices.lakeshore_475
```

Update `app/devices/registry.py` to import:

```python
from app.devices.lakeshore_475.module import MODULE as LAKESHORE_475_MODULE
```

Rename the local registry constant to `LAKESHORE_475_MODULE`. Keep these
manifest fields unchanged:

```python
key="lakeshore_gaussmeter"
settings_key="lakeshore_gaussmeter"
```

- [ ] **Step 4: Prove the old package path is gone**

Run:

```powershell
rg -n "app\.devices\.lakeshore_gaussmeter(?:\.| import)" app tests -g "*.py"
Test-Path app/devices/lakeshore_gaussmeter
```

Expected: `rg` has no output; `Test-Path` prints `False`.

- [ ] **Step 5: Run Lake Shore and architecture tests**

Run:

```powershell
python -m pytest tests/test_architecture.py tests/test_device_modules.py tests/test_simulators.py tests/test_main_window.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the Lake Shore migration**

```powershell
git add app tests
git commit -m "refactor: name Lake Shore module for model 475"
```

### Task 6: Enforce concrete imports and UI identity

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_main_window.py`
- Modify: `app/ui/dashboard/page.py` only if the failing UI test proves labels are hard-coded.
- Modify: `app/ui/shell/main_window.py` only if the failing UI test proves navigation labels bypass manifests.

**Interfaces:**
- Consumes: `DeviceModule.display_name`, `DeviceModule.implementation_key`.
- Produces: architecture guard against generic imports and UI tests proving one concrete entry per registered device.

- [ ] **Step 1: Add the source-import guard**

Add to `tests/test_architecture.py`:

```python
def test_source_does_not_import_removed_generic_device_packages(self) -> None:
    forbidden = (
        "app.devices.rigol",
        "app.devices.keithley",
        "app.devices.anritsu",
    )
    for root_name in ("app", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            for imported in _imports(path):
                self.assertFalse(
                    any(
                        imported == prefix or imported.startswith(prefix + ".")
                        for prefix in forbidden
                    ),
                    f"{path.relative_to(ROOT).as_posix()} imports {imported}",
                )
```

- [ ] **Step 2: Add the UI identity test**

In `tests/test_main_window.py`, add:

```python
def test_dashboard_uses_one_concrete_label_per_registered_device(self) -> None:
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        registry = window._composition.registry
        for module in registry.enabled_modules():
            if module.key in window.dashboard.cards:
                card = window.dashboard.cards[module.key]
                labels = card.findChildren(QLabel)
                self.assertTrue(
                    any(label.text() == module.display_name for label in labels),
                    module.implementation_key,
                )
    finally:
        window.close()
```

The test deliberately inspects the existing label created by
`DeviceCard.__init__`; no production-only accessor is added for testing.

- [ ] **Step 3: Run tests and verify their signal**

Run:

```powershell
python -m pytest tests/test_architecture.py tests/test_main_window.py::MainWindowTests::test_dashboard_uses_one_concrete_label_per_registered_device -q
```

Expected: architecture test PASS. The UI test either PASSes with existing
manifest wiring or FAILs by identifying the hard-coded label.

- [ ] **Step 4: Apply the minimal UI correction if RED**

If the UI test fails, source dashboard and navigation labels from the manifest
already available through the registry:

```python
module.display_name
```

Do not rename persisted dictionary keys or add a second module entry. If the
test already passes, make no production UI change.

- [ ] **Step 5: Run architecture and UI tests**

Run:

```powershell
python -m pytest tests/test_architecture.py tests/test_device_modules.py tests/test_main_window.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the enforcement**

```powershell
git add tests/test_architecture.py tests/test_main_window.py app/ui/dashboard/page.py app/ui/shell/main_window.py
git commit -m "test: enforce concrete device module ownership"
```

Stage UI files only if Task 6 actually changed them; preserve unrelated
pre-existing hunks when staging.

### Task 7: Verify compatibility and return to the UI review

**Files:**
- Modify only files required by a reproducible failing compatibility test.
- Inspect: `.config/settings.yml`
- Inspect: `recipes/*.yml`
- Inspect: `app/engine/compiler.py`
- Inspect: `app/engine/runner.py`
- Inspect: `app/storage/hdf5_writer.py`
- Inspect: `app/storage/thatec_writer.py`

**Interfaces:**
- Consumes: complete concrete module layout.
- Produces: evidence that persisted formats and runtime behavior are unchanged.

- [ ] **Step 1: Prove stable keys remain present**

Run:

```powershell
rg -n '"(rigol|keithley|anritsu|moke_box|lakeshore_gaussmeter)"' app/engine app/storage app/settings app/safety tests -g "*.py"
rg -n '^(  )?(rigol|keithley|anritsu|moke_box|lakeshore_gaussmeter):' .config recipes -g "*.yml" -g "*.yaml"
```

Expected: persisted keys remain present. No model-specific implementation key
appears as a replacement for a persisted key.

- [ ] **Step 2: Run storage, recipe, and recovery tests**

Run:

```powershell
python -m pytest tests/test_hdf5_writer.py tests/test_recipe_builder.py tests/test_run_recovery.py tests/test_simulated_run.py tests/test_thatec_validator.py -q
```

Expected: PASS.

- [ ] **Step 3: Run the full suite with enough time**

Run:

```powershell
python -m pytest -q
```

Expected: all collected tests PASS. Existing failures caused by the
pre-existing uncommitted UI work must be documented and fixed separately or,
if they directly overlap module identity, repaired with a focused failing test.

- [ ] **Step 4: Validate imports and working-tree scope**

Run:

```powershell
python -m compileall -q app tests
git diff --check
git status --short
```

Expected: compilation succeeds; `git diff --check` reports no whitespace
errors; status shows only intentional migration changes plus the user's
pre-existing uncommitted UI/data changes.

- [ ] **Step 5: Resume the original UI production-readiness review**

Run the previously failing UI tests individually:

```powershell
python -m pytest tests/test_main_window.py::MainWindowTests::test_discovery_marks_persisted_resource_assigned_and_disables_duplicate -q
python -m pytest tests/test_main_window.py -q
python -m pytest tests/test_results_page.py -q
```

Expected: identify module-migration failures separately from the existing
Dashboard cell-wrapper regression and any Results-page regressions.

- [ ] **Step 6: Commit only additional migration fixes**

If Step 3 or 4 required migration-specific corrections:

```powershell
git add app/contracts/device_module.py app/devices app/engine app/safety app/settings app/storage tests
git commit -m "fix: preserve device module compatibility"
```

Before staging, reduce this list to files shown by `git diff --name-only` that
were changed specifically for migration compatibility. If no migration-specific
files changed, do not create an empty commit.
