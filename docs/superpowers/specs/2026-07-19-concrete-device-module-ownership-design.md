# Concrete Device Module Ownership

## Goal

Make every supported hardware family a complete, self-contained device module
whose package name identifies the concrete instrument family. Remove generic
packages such as `app.devices.rigol`, `app.devices.keithley`, and
`app.devices.anritsu`, which currently own implementation code despite the
registry exposing concrete modules.

The refactor must not invalidate existing station settings, recipes, safety
policies, audit records, or measurement files.

## Current problem

The registry exposes concrete modules:

- `rigol_dg1000z`
- `keithley_2600`
- `anritsu_ms2830a`
- `moke_box`
- `lakeshore_gaussmeter`

However, three concrete packages are only partial facades. Their adapters,
models, errors, simulations, or other implementation details live in generic
packages:

- `app.devices.rigol`
- `app.devices.keithley`
- `app.devices.anritsu`

This creates two competing identities for one device family. A developer can
import either the generic or concrete path, UI labels can drift away from the
registered module, and ownership is unclear.

## Chosen architecture

Each concrete hardware package owns its complete vertical slice:

```text
app/devices/
  rigol_dg1000z/
    adapter.py
    models.py
    module.py
    simulation.py
    ui/
  keithley_2600/
    adapter.py
    models.py
    module.py
    simulation.py
    ui/
  anritsu_ms2830a/
    adapter.py
    models.py
    module.py
    simulation.py
    ui/
  moke_box/
    ...
  lakeshore_475/
    ...
```

The exact filenames may follow the existing contents of each package; the
ownership rule is mandatory, while unnecessary empty files are not.

`app.devices.registry` imports only concrete modules. Application code, tests,
workers, recipe extensions, and UI code import device-specific types only from
their concrete package.

Generic packages are deleted after all consumers have migrated. They are not
kept as compatibility facades because that would preserve the ambiguous
architecture.

## Stable data identities

Package names and persisted device keys serve different purposes.

Concrete package names identify implementation ownership:

- `rigol_dg1000z`
- `keithley_2600`
- `anritsu_ms2830a`
- `moke_box`
- `lakeshore_475`

Existing persisted keys remain stable:

- `rigol`
- `keithley`
- `anritsu`
- `moke_box`
- `lakeshore_gaussmeter`

These keys continue to be used in settings YAML, recipes, safety actions,
audit data, HDF5 content, runtime dictionaries, and public data contracts.
They are compatibility identifiers, not Python module names.

The manifest will express both concepts explicitly. A concrete family identity
must not be inferred from the persisted key or display label.

## Naming

All user-visible device names come from the concrete module manifest or the
corresponding configured display name. Generic labels such as `Rigol` may be
used in prose or a compact control only when they clearly refer to the already
selected concrete module; they must not represent an independently selectable
module.

The target concrete names are:

- Rigol DG1000Z family, currently qualified as Rigol DG1032Z
- Keithley 2600 series
- Anritsu MS2830A
- MOKE Box
- Lake Shore 475

The existing `lakeshore_gaussmeter` package will be renamed to
`lakeshore_475` for the same ownership rule. Its persisted key remains
`lakeshore_gaussmeter`.

## Migration sequence

1. Add architecture tests describing concrete ownership and stable data keys.
2. Extend the device-module contract with an explicit concrete family/package
   identity if the current contract cannot represent it unambiguously.
3. Move generic Rigol implementation into `rigol_dg1000z`.
4. Move generic Keithley implementation into `keithley_2600`.
5. Move generic Anritsu implementation into `anritsu_ms2830a`.
6. Rename the Lake Shore implementation package to `lakeshore_475`.
7. Update every internal import, type reference, worker mapping, recipe
   extension, UI integration, and test.
8. Delete the generic packages and reject imports from them.
9. Verify settings, recipes, safety manifests, and stored-result readers still
   use the stable persisted keys.

Each device migration should be independently testable. Mechanical moves must
not be combined with behavior changes.

## Compatibility and safety

No persisted schema or hardware command behavior changes as part of this
refactor. In particular:

- connection settings retain their existing YAML paths;
- safety-policy action names retain their existing keys;
- recipe device names retain their existing keys;
- HDF5 readers and writers retain their existing field names;
- adapter safety checks and output-off behavior remain unchanged;
- simulation lookup keys remain compatible with existing tests and settings.

Import compatibility for the removed generic Python packages is intentionally
not guaranteed inside this repository. A repository-wide search must prove
that no internal consumer remains before deletion.

If documented external Python imports are discovered during implementation,
they must be reported before removing them; an explicit deprecation release
would then require a separate decision.

## Enforcement

Architecture tests will verify:

- every registered module declares a concrete family/package identity;
- concrete identities are unique;
- persisted keys are unique;
- registry entries originate from concrete packages;
- forbidden generic device packages do not exist;
- application and test sources contain no imports from forbidden paths;
- concrete display names are non-empty and sufficiently specific;
- registry construction and all existing public contracts still work.

Behavioral tests will continue to cover adapter operations, simulation,
workers, UI creation, recipe integration, safe shutdown, HDF5 persistence, and
settings loading.

## UI acceptance criteria

- Navigation, dashboard cards, discovery assignment, settings, recipes, and
  device pages resolve to one concrete module per device.
- No screen presents both a generic and a model-specific device as separate
  modules.
- Device labels are consistent with the concrete manifest.
- Existing configured resources remain assigned after the refactor.
- The currently uncommitted Results and Dashboard UI changes are reviewed and
  tested after the module migration; they are not silently bundled into the
  architectural refactor.

## Out of scope

- Changing supported hardware models or qualification status.
- Migrating persisted keys to model-specific names.
- Changing SCPI commands, device capabilities, or safety limits.
- Redesigning device pages.
- Fixing unrelated uncommitted Results-page functionality in the same change.

## Completion criteria

The work is complete when generic device packages are gone, all implementation
is owned by concrete packages, persisted data remains compatible, architecture
tests enforce the boundary, and the full test suite passes without new
warnings or failures.
