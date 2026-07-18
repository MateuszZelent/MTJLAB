# Production hardening — audit items 5–9

**Date:** 2026-07-18

**Status:** approved direction; MOKE hardware mode is read-only

## 1. Goal

Close audit findings 5–9 without weakening safety boundaries:

1. accept correct Unicode SI spellings for resistance and magnetic field;
2. make station readiness cover every registered physical device and every
   energy-producing output action;
3. expose only qualified read-only MOKE operations;
4. finish and freeze the read-only Lake Shore 475 vertical integration;
5. make the qualified Python environment reproducible and keep PyThat off the
   ABI-warning-producing netCDF4 backend.

Physical HIL remains a separate release gate. These changes make the software
candidate internally consistent; they do not claim physical qualification.

## 2. Considered approaches

### A. Patch each visible symptom

Add three aliases to the unit map, append two `if` blocks to readiness, disable
one MOKE button and suppress the NumPy warning.

This is the smallest diff, but it leaves duplicated device knowledge, keeps
write operations reachable through the MOKE dispatcher and hides an
environment defect instead of selecting a qualified storage backend.

### B. Central contracts and fail-closed surfaces — selected

- canonicalise Unicode units before lookup;
- describe readiness devices through one internal descriptor table;
- define one authoritative set of energy-producing action kinds;
- reduce the MOKE module dispatcher, capabilities and recipe extension to
  qualified read-only operations;
- retain the existing Lake Shore 475 read-only design and verify every vertical
  boundary against it;
- route all PyThat opens through one bridge that selects `h5netcdf`;
- pin the qualified Python and package versions in lock artifacts.

This is still a focused change, but it prevents UI, compiler, runner and
readiness from drifting independently.

### C. Replace PyThat/netCDF and redesign device capabilities

Remove the PyThat dependency, create a new public data format and rebuild all
device capability discovery around a dynamic registry.

This would be disproportionate to the audit findings and would invalidate the
already approved thaTEC/PyThat compatibility work.

## 3. Unit canonicalisation

`app/domain/quantities.py` remains dependency-free except for the standard
library. `_unit_definition()` will:

1. trim whitespace;
2. apply Unicode NFKC normalisation;
3. translate both the micro sign (`µ`) and Greek small mu (`μ`) to ASCII `u`;
4. translate omega spellings before case-insensitive lookup:
   - `Ω` and `ω` → `ohm`;
   - `kΩ`, `KΩ`, `kω`, `Kω` → `kohm`;
   - `MΩ`, `Mω` → `Mohm`;
   - `mΩ`, `mω` → a distinct milliohm key;
5. use the existing case-insensitive lookup for the remaining vocabulary.

`mΩ` means `1e-3 ohm`; `MΩ` means `1e6 ohm`. The legacy ASCII spelling
`Mohm` remains accepted as megaohm for compatibility. The corrupted `Âµt`
alias is removed. Both `µT` and `μT` are accepted and formatted as `uT`.

Regression tests cover parsing, conversion and formatting, not internal map
contents.

## 4. Station readiness

Readiness will use one internal descriptor per physical device:

| Key | Settings object | Assignment |
|---|---|---|
| `rigol` | `settings.rigol` | `connection.resource` |
| `keithley` | `settings.keithley` | `connection.resource` |
| `anritsu` | `settings.anritsu` | `connection.resource` |
| `moke_box` | `settings.moke_box` | `endpoint` |
| `lakeshore_gaussmeter` | `settings.lakeshore_gaussmeter` | `resource` |

The existing state, error, required-device and identity-verification rules apply
uniformly to all five descriptors. MOKE and Lake Shore use the same
`verified_resources` contract as VISA devices: the verified value equals their
currently assigned endpoint/resource.

The authoritative energy-producing set is:

```text
set_rigol_output
set_keithley_output
set_anritsu_sg_output
```

Only actions with `payload.enabled == true` make the plan energised. A plan
containing Anritsu RF OUTPUT ON must never receive “Plan contains no OUTPUT ON
action”.

Tests prove required/optional states for MOKE and Lake Shore and the RF message.

## 5. MOKE read-only boundary

The user decision is explicit: leave MOKE read-only for now.

### 5.1. Qualified public operations

The production module dispatcher exposes only:

- `read_signal` for the legacy read-only transport;
- `read_vouts`;
- `read_hall_voltage`.

`read_hall_voltage` remains restricted to the physically observed one-sample,
MainBox channel-0 AD7734 response.

The dispatcher does not expose:

- `acquire_samples`;
- `read_fields`;
- `set_hall_gains`;
- `set_kerr_gain`;
- `set_vout`;
- `ramp_vout`.

Calls to these operation names fail before reaching the adapter.

### 5.2. Capabilities and settings

The module and connected adapter advertise only read-only capabilities:

```text
read_only
vout_readback
hall_voltage_readback
```

The compatibility settings `allow_vout_control` and
`allowed_vout_channels` remain loadable so old profiles do not become
unreadable, but any attempt to enable VOUT control is rejected by settings
validation. The physical adapter is always constructed with output control
disabled.

Low-level reconstructed protocol helpers may remain for protocol research and
isolated tests, but they are not reachable from the production module, UI or
recipe compiler.

### 5.3. UI and Sweeper

The MOKE page contains:

- connection/read-only safety state;
- VOUT readback;
- Hall-1 voltage and derived field readback;
- live Hall-1 readback using the qualified one-sample operation.

The AD7734 multi-stream inspector and `Acquire streams` action are removed from
the production page.

`moke_box.field_target` is removed from the parameter registry and from all
legacy selector ordering. The MOKE module exposes no sweep axis. The
read-only `measure_moke_hall` library block remains available and can be placed
inside another device's sweep.

The recipe model may continue to recognise legacy `configure_moke_box` nodes so
old files produce the existing explicit qualification error rather than a
parser error. No UI path creates such a node.

## 6. Lake Shore 475 freeze criteria

The existing
`docs/superpowers/specs/2026-07-18-lakeshore-475-read-only-design.md` remains the
source of truth. This hardening work does not add write capability or broaden
the model scope.

The integration is considered frozen as a software candidate only when the
same commit proves:

- the read-only query proxy rejects every write;
- the module has adapter, simulator, page factory and read-only capabilities;
- dashboard, toolbar, settings and lifecycle include the stable
  `lakeshore_gaussmeter` key;
- the recipe library exposes `measure_lakeshore_field` only when enabled and
  assigned;
- compiler, required-device discovery, worker and runner execute one
  checkpoint;
- DC, RMS and Peak results are stored with qualified units and reopen through
  HDF5/PyThat;
- readiness includes Lake Shore;
- focused Lake Shore tests, the complete suite and Ruff pass.

The production profile remains disabled until physical HIL is signed.

## 7. PyThat backend and reproducible environment

### 7.1. Root cause

The warning is reproducible after pytest collection when xarray selects
`netCDF4` while PyThat writes its temporary `.nc` representation:

```text
netCDF4._netCDF4
RuntimeWarning: numpy.ndarray size changed, may indicate binary incompatibility.
Expected 16 from C header, got 96 from PyObject
```

Observed environment:

```text
CPython 3.14.6
NumPy 2.5.1
netCDF4 1.7.4 (cp311-abi3 Windows wheel)
xarray 2026.7.0
PyThat 0.2.14
```

The warning is not suppressed. The application avoids that binary path.

### 7.2. Single PyThat bridge

A new private storage bridge owns opening `PyThat.MeasurementTree`. It:

1. verifies the installed PyThat version is exactly `0.2.14`;
2. imports xarray and confirms the `h5netcdf` engine is available;
3. temporarily sets xarray's netCDF engine order to prefer `h5netcdf`;
4. opens `MeasurementTree(index=True, override=True)`;
5. closes any retained HDF5 handle on all paths;
6. removes only the `.nc` sidecar created for the current HDF5 input after the
   dataset has been loaded into memory;
7. converts import/backend/runtime failures into a precise application error.

The compatibility validator and Results reader both use this bridge. No caller
opens PyThat directly in production code.

Tests run the bridge after pytest collection with `RuntimeWarning` promoted to
an error and assert that the public measurement tree still round-trips.

### 7.3. Lock artifacts

The repository gains:

- `.python-version` containing `3.14.6`;
- `requirements.lock.txt` with exact runtime and PyThat-transitive versions;
- `requirements-dev.lock.txt` extending the runtime lock with exact test/lint
  versions;
- `tools/check_locked_environment.py`, which compares the interpreter and
  installed distributions with the lock before release qualification.

`requirements.txt` and `pyproject.toml` remain the human-maintained compatible
ranges. Lock files are the release-install source:

```text
python -m pip install -r requirements.lock.txt
python tools/check_locked_environment.py
```

The lock includes `h5netcdf` explicitly because it is the qualified PyThat
backend. `netCDF4` remains pinned because PyThat declares it as a dependency,
but application validation does not execute its ABI-warning-producing backend.

The release check runs:

```text
python -W error::RuntimeWarning -m pytest ...
```

for the storage/PyThat boundary.

## 8. Error handling

- Unknown or dimensionally wrong units remain fail-closed.
- A required MOKE/Lake Shore assignment is a readiness failure.
- An optional unassigned device is informational or warning-level according to
  the existing policy.
- Any MOKE write or unqualified stream operation is rejected at the dispatcher
  even if a stale UI or caller tries to invoke it.
- A missing `h5netcdf` backend or wrong PyThat version prevents a result from
  being marked compatible.
- A Lake Shore read error produces no partial checkpoint.

## 9. Test strategy

Every behavior change follows RED → GREEN:

1. Unicode unit regression tests;
2. MOKE/Lake Shore readiness tests;
3. Anritsu RF energisation test;
4. MOKE dispatcher and UI surface tests;
5. parameter-registry/Sweeper absence tests;
6. Lake Shore vertical focused suite;
7. PyThat bridge warning-as-error test;
8. lock checker success and mismatch tests;
9. full pytest, Ruff, compileall and diff check on one clean commit.

## 10. Acceptance

The work is complete when:

- `Ω`, `kΩ`, `MΩ`, `mΩ`, `µT` and `μT` parse with correct scales;
- readiness reports all five devices and Anritsu RF OUTPUT ON;
- production MOKE surfaces are read-only and contain no field sweep axis or raw
  multi-stream action;
- Lake Shore 475 passes its vertical software integration tests;
- PyThat round-trips through `h5netcdf` with RuntimeWarning treated as error;
- exact lock artifacts and the environment checker are present;
- the complete test suite and static checks pass on a clean, frozen commit;
- the production audit report reflects the new software state without claiming
  physical HIL.
