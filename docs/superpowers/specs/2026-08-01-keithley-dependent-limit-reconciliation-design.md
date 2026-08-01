# Keithley dependent limit reconciliation design

## Goal

Allow an operator who edits a Keithley channel safety limit to keep the requested
value and explicitly accept the smallest safe, internally consistent set of
dependent limit changes, instead of receiving a late validation error.

## Scope

This applies to the Keithley 2600 limit editor reached from the device page and
to the Keithley rows in the station Settings safety-limit editor. It applies to
both channels and to these active limit relationships:

- current-source envelope: `source_current` × `voltage_compliance` <=
  `max_abs_power`;
- voltage-source envelope: `source_voltage` × `current_compliance` <=
  `max_abs_power`;
- source current and current compliance must be covered by
  `measured_current_trip` when those limits are enabled;
- source voltage and voltage compliance must be covered by
  `measured_voltage_trip` when those limits are enabled;
- a range's stored `max_abs` mirrors the greatest absolute magnitude of its
  edited `min` and `max` boundaries.

`max_abs_power_enabled=false` and disabled range limits remove their respective
station-profile relationship from reconciliation; they do not remove hardware
validation.

## User interaction

1. The operator edits a minimum, maximum, or maximum-power value using an
   explicit unit.
2. The application parses and validates the edited value and simulates the
   change against the complete current channel configuration.
3. If no dependent value must change, the existing staging flow continues.
4. If reconciliation produces dependent changes, a modal presents a single,
   all-or-nothing proposal. Each row shows the parameter, old value, proposed
   value, and a short reason. The dialog clearly states that these are station
   safety-envelope changes.
5. **Accept changes** stages the requested edit and every displayed dependent
   edit in one in-memory settings snapshot. **Cancel** stages nothing and
   leaves the original editor available for correction.
6. Staged values remain unsaved until the existing **Save settings** action.
   Existing output-off checks, settings validation, adapter validation,
   readback, and audit behavior remain authoritative.

For the Channel B example, changing `source_current.max` from `10 mA` to
`150 mA` while `voltage_compliance.max` is `67 mV` produces this proposal:

- `source_current.max_abs`: `10 mA` -> `150 mA` (synchronise the edited
  source envelope);
- `measured_current_trip.max`: `10.5 mA` -> `150 mA` (cover the source
  envelope);
- `max_abs_power`: `670 uW` -> `10.05 mW` (cover 150 mA × 67 mV).

## Reconciliation policy

The edited leaf is the primary value and is never silently replaced. The
reconciler changes only dependent values needed to make the channel internally
consistent, formatting all proposed values through the canonical quantity
formatter after SI calculations.

For a source or compliance expansion, it expands the corresponding measurement
trip boundary only as far as necessary and raises `max_abs_power` to the greater
of the two enabled source-mode worst-case products. It does not add arbitrary
safety margin.

For a measurement-trip reduction, it reduces the associated source and
compliance boundaries as needed to preserve the newly requested trip. For a
maximum-power reduction, it reduces the relevant source/compliance envelopes as
needed to preserve that power ceiling. If one reduction is insufficient, the
proposal contains the necessary two or more coordinated changes. The algorithm
uses the fewest changed dependent boundaries that preserve the primary edit and
all enabled constraints.

The reconciler rejects a proposal rather than guessing when the primary edit is
invalid, units/dimensions are invalid, a required dependent range would become
reversed, a required compliance value would fall below its configured minimum,
or an immutable Keithley hardware boundary would be exceeded. The UI then shows
the specific ordinary validation error; it never bypasses the model, safety, or
adapter checks.

## Architecture

Add a pure Keithley safety-domain reconciliation component. It consumes a
channel-limit draft and a primary leaf update, normalizes values with
`parse_quantity`, and returns typed, immutable proposed edits with reasons or a
structured non-reconcilable error. It must not import Qt, persist settings, or
communicate with hardware.

The two UI editing paths call this component after the primary edit is parsed
and before the draft is staged. A shared Fluent modal renders returned proposal
rows. The UI applies the returned set atomically only after the operator
accepts, updates every visible affected editor, and stages a single validated
snapshot.

## Safety and data contract

- Human-entered values retain explicit units. Calculations are finite SI values;
  proposed strings use the existing canonical formatter.
- The feature stages configuration only. It must not send Keithley commands,
  enable output, arm an instrument, or weaken the existing output-off rule for
  hot settings updates.
- `StationSettings.model_validate`, compiler preflight, device safety checks,
  adapter hard limits, and output readback remain independent defenses.
- Cancelling the modal has no persisted, staged, or device-side effect.
- Existing YAML field names and data representation remain unchanged.

## Verification

Focused tests must cover:

- current and voltage source expansions, including synchronized `max_abs`, trip
  expansion, and recalculated power;
- current and voltage compliance expansions;
- a power reduction requiring one adjustment and one requiring multiple
  coordinated adjustments;
- trip reduction and disabled-limit behavior;
- wrong dimension, non-finite input, reversed range, configured lower-bound,
  and hardware-limit rejection paths;
- accepting and cancelling the proposal from the Keithley device page;
- the Settings safety editor invoking the same reconciliation behavior;
- rendered Fluent modal geometry and readable controls at a normal desktop size
  and a narrow window size.

