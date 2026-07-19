# THATEC ↔ Sweep Device and Parameter Bridge

## Purpose

The application must import a public THATEC HDF5 measurement tree as typed
application Sweep nodes, using the instrument modules already implemented in the
station. It must also write public THATEC files which can be read back without
losing the measurement-tree semantics, device parameters, units, channel
assignments, or ordering.

The bridge must recognize device families rather than require byte-for-byte
identical display names. For example:

- `Keithley 2614B Sourcemeter` maps to the `keithley` module for the Keithley
  2600 family;
- `MOKE-Box Field Control` maps to `moke_box`;
- `Anritsu MS269Xa MS2830A SpectrumAnalyzer` maps to `anritsu`;
- Rigol DG1000Z/DG1032Z aliases map to `rigol`.

Import must never make an unsafe guess executable.

## Scope

This project includes:

- device-family and model alias resolution;
- control/indicator/internal-row parameter resolution;
- THATEC tree-to-typed-Recipe reconstruction;
- reverse mapping used by the public THATEC writer;
- local alias editing in Settings;
- import diagnostics and execution blocking;
- reference-file and round-trip tests.

It does not add drivers for device families that are not already supported by
the application. Such rows remain visible and auditable but unresolved.

## Architecture

### Canonical mapping registry

A single versioned registry is the authoritative source for both import and
export. Each device-family entry defines:

- canonical application module key;
- recognized manufacturer, family, model and display-name aliases;
- optional IDN matchers;
- THATEC control aliases;
- target application parameter or action;
- expected unit/dimension;
- channel extraction rules;
- import and export names;
- supported THATEC function types.

Built-in mappings live with the application and are immutable at runtime.
Station-local device-name aliases are stored in validated settings and edited in
the Settings GUI. Local aliases may select an existing device module/model but
may not redefine parameter units or action semantics.

Matching normalizes case, whitespace, newlines and punctuation. Exact aliases
and explicit IDN matches take precedence. Fuzzy string matching is not used for
executable mappings.

### Device resolver

The device resolver consumes the THATEC device name, the public `/devices`
records and optional IDN data. It returns:

- the canonical module and model;
- match confidence: `exact`, `compatible`, or `unresolved`;
- the matched rule identifier;
- diagnostics describing the evidence used.

Conflicting aliases or multiple compatible results are `unresolved`.

### Parameter resolver

The parameter resolver consumes a resolved device, the `row_*` definition,
measurement metadata and the row's position in `tree_view`. It resolves:

- application parameter/action identifier;
- channel A/B or numerical channel;
- fixed value or sweep definition;
- source unit and canonical application dimension;
- row role: control, indicator, acquisition or internal action;
- import diagnostics.

Initial required mappings include:

- Keithley output enable A/B;
- Keithley source current A/B;
- Keithley resistance A/B indicators;
- MOKE Hall voltage/field measurement;
- Anritsu spectrum acquisition;
- internal wait;
- currently exported Rigol, Keithley, Anritsu and MOKE parameters.

`start`, `stop`, `steps` and `equation` reconstruct fixed values and sweep
segments. Values are converted through the application's quantity model.
Original THATEC names, values and units are retained in provenance metadata.

### Recipe reconstructor

The reconstructor preserves the exact `tree_view` order and nesting derived from
`tree indent level`. It emits normal `RecipeNode` instances for resolved rows
and explicit unsupported/import-review nodes for unresolved rows.

Resolved nodes use the same `device_module`, `parameter_actions`, sweep targets,
acquisition actions and safety actions as nodes created manually in Sweep
builder. Synthetic aliases are not shown as generic historical rows.

Unresolved nodes preserve their complete public definition and metadata. They
remain visible with status `UNRESOLVED` and make the reconstructed recipe
non-executable.

### Reverse mapping

The THATEC writer calls the same canonical registry in reverse. Every exported
typed node receives stable public:

- device name;
- control name;
- function;
- data type and dimensions;
- tree indentation;
- sweep/fixed-value definition;
- application role/key provenance.

The original recipe and settings snapshots remain stored in the public
`labbook/parameter` extension for exact restoration by this application.
Standard THATEC readers can ignore those extension keys and still read the
public tree.

## Import flow

1. Read `/devices`, `/scan_definition/tree_view`, all `row_*` definitions and
   relevant `/measurement` metadata.
2. Resolve each distinct device once.
3. Resolve every row using the resolved device and canonical parameter registry.
4. Reconstruct the ordered and nested typed recipe.
5. Present an import report with mapped, review and unresolved counts.
6. Show each mapping and its original THATEC provenance in the Sweep inspector.
7. Require the normal current-profile preflight before enabling Run.

Imported output-enable actions remain subject to existing ARM, approved-profile,
DUT-limit and output-lock rules. HDF5 contents never grant authorization.

## Settings UI

Settings gains a `THATEC aliases` section listing:

- source THATEC device alias or anchored matcher;
- destination application module;
- optional model;
- validation state and conflict message.

The GUI permits adding, editing and removing station-local device aliases.
It prevents duplicate or ambiguous aliases and cannot redefine built-in
parameter mappings. Changes follow the existing draft/approve settings
workflow.

## Sweep UI

Imported nodes use statuses:

- `MAPPED` for an exact built-in or local alias mapping;
- `REVIEW` for a compatible family/model mapping that is semantically
  unambiguous but should be inspected;
- `UNRESOLVED` when no safe mapping exists.

The inspector shows:

- original device and control names;
- original function, values and units;
- selected application module, parameter/action and channel;
- mapping rule and confidence;
- blocking diagnostic when unresolved.

The import summary lists devices, parameters, sweep axes, acquisition rows and
unresolved rows. Any unresolved row disables validation/run until the user
updates an alias or replaces/removes the node.

## Error handling

- Invalid HDF5/public-schema errors stop import without altering the current
  Sweep.
- A device or parameter ambiguity becomes a visible unresolved node rather than
  an arbitrary choice.
- Unit or dimension mismatch is always unresolved.
- Missing optional metadata produces a diagnostic and uses only explicit
  evidence available in the row.
- The importer builds the new recipe transactionally and replaces the current
  tree only after the complete import succeeds.
- Diagnostics include source H5 path, row ID, original names and rule IDs but no
  instrument commands are sent.

## Acceptance tests

### Real THATEC reference

For `Elec_Det_20260606_RPTU0741_32P5_MTJcurrentSweep.h5`:

- every public `tree_view` row is present once, in identical order and nesting;
- Keithley 2614B is recognized as the implemented Keithley 2600 module;
- channels A and B are preserved;
- output-enable, source-current and resistance rows have correct typed roles;
- MOKE-Box Hall voltage maps to the existing MOKE measurement;
- Anritsu Spectrum maps to the existing acquisition action;
- fixed values and sweep start/stop/steps are preserved;
- every `/devices` parameter remains available in provenance/inspection;
- there are no unresolved rows for devices and controls already supported by
  the application.

### Safety and negative cases

- unknown device, ambiguous alias and incompatible unit each create an
  unresolved blocking node;
- imported output-on cannot run without normal ARM/profile/preflight rules;
- importer does not open device sessions or send instrument commands;
- a failed import leaves the previous Sweep unchanged.

### Round trip

For representative Keithley, Rigol, Anritsu and MOKE recipes:

1. compile a typed Sweep;
2. write the public THATEC H5;
3. remove private application groups;
4. import only the public THATEC schema;
5. compare canonical tree structure, device module, parameter/action, channel,
   fixed/sweep values, units and acquisition roles.

The comparison is semantic; stable public THATEC naming is also asserted
separately.

## Completion criteria

The bridge is complete when the real reference file reconstructs as typed
supported Sweep nodes with no unresolved mappings, all device parameters remain
inspectable, round-trip tests pass from public THATEC data alone, and the full
reconstructed recipe remains blocked until the existing safety preflight
accepts it.
