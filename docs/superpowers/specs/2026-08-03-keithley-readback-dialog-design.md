# Keithley readback dialog clarity and responsive layout

## Goal

Make the Keithley 2600 modeless, read-only configuration dialog understandable
and usable at its first opening size while preserving the existing query-only
readback and explicit assignment workflows.

## Scope

This slice changes only the readback dialog in
`app/devices/keithley_2600/ui/page.py` and its focused UI tests. It does not
change the TSP query set, adapter readback model, output state, safety limits,
recipe values, persistence, or assignment semantics.

The comparison remains against the channel-specific configuration currently
held by the form. The dialog will name that source explicitly instead of
calling it “Current form”, because the cell is a comparison result: it shows
`MATCH`, the form value when different, or `Not controlled by form` when no
form value is applicable.

## Range explanation and measurement meaning

The dialog will explain the following distinction in plain language:

- `Source autorange` enables the SMU to select the source range for the active
  source function (`source.rangei` or `source.rangev`). It is not a voltage or
  current measurement range.
- `Active source range` is the range currently reported by the instrument for
  that source function. With source autorange enabled it is a device-selected
  snapshot and may change as the source level changes. With autorange disabled
  it is the fixed source range used by the channel.
- Source range settings can indirectly affect the physical result through
  source resolution, source accuracy, range transitions, settling time, and
  available source headroom. They do not normally select the input range used
  to measure voltage or current; those are represented by the separate
  `Measure V autorange`, `Active measure V range`, `Measure I autorange`, and
  `Active measure I range` fields. If the source and measurement function are
  the same, the instrument may couple their ranges; the active measure rows
  show the actual range returned by the device.

No new claims about hardware limits will be encoded in the UI. Numeric values
continue to be formatted through the existing quantity helpers with explicit
dimension selection.

## Table vocabulary and interaction

The seven columns will use explicit roles:

1. `Parameter`
2. `Hardware value / Channel A`
3. `Form comparison / Channel A`
4. `Action / Channel A`
5. `Hardware value / Channel B`
6. `Form comparison / Channel B`
7. `Action / Channel B`

In the Qt header these labels are displayed on two lines (`Hardware value`
over `Channel A`, for example) so the roles remain readable at the minimum
window width.

Action cells will use `Use hardware value` for assignable rows. Output state
and output-off mode remain non-assignable and display an em dash with an
explanation tooltip. The footer action will be `Use all compatible values` and
will retain the invariant that it never copies OUTPUT state or OUTPUT OFF
mode. The existing per-row source-group behavior remains: using one source
row copies the complete source group needed to keep source mode, level,
compliance, autorange, and range internally consistent.

The dialog will include a compact comparison legend and a clearly separated
read-only/safety note. Mismatch cells will identify the form value explicitly,
while matching cells will remain green and device-only/uncontrolled cells will
not be mistaken for failures.

## Responsive layout

The dialog will have a minimum size of approximately 980×620 px and an
initial size of approximately 1180×760 px, clamped to the available desktop
geometry. The table receives the flexible height, keeps action columns wide
enough for their labels, and remains vertically scrollable. Header sections
will not rely on blank spacer columns or accidental content widths. The
normal desktop test will show the dialog and process events before asserting
non-zero rendered geometry; a narrower test will assert that the dialog and
table remain visible and usable rather than shrinking the footer over the
table.

## Safety and compatibility invariants

- `read_configuration()` remains query-only and no OUTPUT state changes.
- The dialog remains modeless and read-only so other floating station surfaces
  remain interactive while the snapshot is open.
- No assignment is performed unless the operator explicitly presses a row or
  footer action.
- OUTPUT state and OUTPUT OFF mode remain excluded from assignment.
- Existing `MATCH` comparison rules, including autorange-aware range matching,
  remain intact.
- Existing quantity parsing/formatting and channel-specific form snapshots
  remain authoritative.

## Verification

Focused tests will verify the exact new headers, comparison copy and action
labels, the range explanation, the non-assignable output rows, the modeless
geometry after `show()` at normal and narrow sizes, and the existing readback
assignment behavior. A two-floating-window regression also proves that the
readback surface does not block Quick Controls; the relevant Keithley UI tests
plus `ruff check app tests` will be run before completion.
