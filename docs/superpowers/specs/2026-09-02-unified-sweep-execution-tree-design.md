# Unified Sweep and Execution Tree Design

## Status

Approved for implementation planning on 2026-09-02. This document refines the
measurement-tree direction from
`docs/raports/SWEEP_MEASUREMENT_TREE_MASTERPLAN_2026-07-16.md` and replaces the
item-based Builder/Execution projection described in
`docs/superpowers/plans/2026-07-21-execute-shared-tree-and-live-monitoring.md`.

## Outcome

Sweeps and Execution shall present one semantic measurement tree. A sweep axis,
its ROI definition, its loop body, and the per-point device set operation must
belong to one hierarchy. Execution shall annotate that same hierarchy with live
progress and confirmed values instead of reconstructing compiler-generated
actions by node-ID prefixes.

The implementation must also remove GUI stalls during active sweeps. Instrument
I/O and durable storage remain on the run worker. The GUI receives bounded,
coalesced presentation state and updates model indexes, not complete widget-item
subtrees.

## Verified current-state problems

The current code has three different representations of the same sweep:

1. `RecipeNode.data["parameter_actions"]` owns the device-local sweep definition.
2. `RecipePage._add_operator_control_rows()` creates informational rows such as
   `Source current` and `ROI 1`; these are not recipe nodes.
3. `RecipePage._populate_recipe_tree()` creates a synthetic
   `For each ROI point` container for executable children.

During Execution, `RunMonitorPage._project_generated_actions()` attaches
compiler-generated actions such as `update_keithley_level` by finding the
longest matching source-node prefix. `RunMonitorPage._install_current_roi_rows()`
then inserts another synthetic live-value row. The result is visually and
semantically ambiguous.

The compiler has the same structural limitation: Keithley, Rigol, Anritsu
spectrum, and Anritsu SG device nodes reject more than one local sweep action.
Consequently, two parameters of one device cannot currently be represented as
two explicit nested axes.

The run path already uses a worker thread and coalesces some telemetry, but the
GUI still performs item mutation, selection, layout, scrolling, table updates,
and plot updates from multiple event paths. The observed pause around action
20/44 followed by a jump to 44/44 is consistent with GUI event-loop starvation;
the exact dominant operation must be measured before and after the refactor.

## Decisions

### D1. Use the Fluent model/view tree

The installed `PySide6-Fluent-Widgets==1.11.2` exposes both:

- `qfluentwidgets.TreeWidget`, which inherits `QTreeWidget` and remains
  item-based;
- `qfluentwidgets.TreeView`, which inherits `QTreeView` and installs the Fluent
  tree delegate, theme styling, selection indicator, and smooth-scroll delegate.

The new tree shall therefore use:

```python
from PySide6.QtCore import QAbstractItemModel
from qfluentwidgets import TreeView

class MeasurementTreeModel(QAbstractItemModel):
    ...

class MeasurementTreeView(TreeView):
    ...
```

This is a Fluent component. Raw `QTreeView` and `QTreeWidgetItem` are not the
target architecture. QFluentWidgets Pro is not required because the installed
standard `TreeView` provides the necessary native model/view boundary.

### D2. Introduce one normalized semantic recipe graph

Existing recipe YAML remains accepted. Before presentation and compilation it
is normalized into a typed graph with explicit axis ownership and loop
semantics. Device-local `parameter_actions` with `mode: sweep` are converted in
memory into the same axis representation as explicit `sweep` nodes.

The original recipe source remains unchanged for provenance. Saving an edited
recipe emits the canonical explicit-axis form; it never writes both a local
sweep action and a second explicit axis for the same parameter.

### D3. Make `Set ROI value` an explicit semantic operation

Every sweep point begins with a non-editable semantic child operation:

```text
Set ROI value · <device endpoint> · <parameter> = <current point>
```

It has a stable semantic ID derived from the axis ID. The device sweep provider
compiles it to the existing validated adapter-level operation. The main tree
does not show technical names such as `update_keithley_level`; those remain in
the technical event trace.

The operation exists for the first point as well. If configuration already
applied the quantized first value, the operation completes as
`APPLIED · unchanged` without an unnecessary physical transition.

### D4. Nesting defines loop order

Nested axes always mean a Cartesian product in tree order. The outer axis is
advanced first; the complete inner axis runs for each outer point.

There is no implicit zip/lockstep mode. A future paired-axis feature would need
an explicit `Zip axes` node and a separate design.

Two axes may bind to different parameters on the same configured device. Two
simultaneously active axes may not bind to the same physical target; preflight
rejects that ambiguity.

### D5. One projection, two interaction modes

Sweeps and Execution consume the same immutable semantic tree snapshot:

- Sweeps enables editing, drag-and-drop, ROI editing, and validation details.
- Execution is read-only and adds runtime state by stable semantic ID.

There is no cloned `QTreeWidgetItem` tree, prefix-based generated-action
placement, or second fallback interpretation during a normal run.

### D6. Runtime state is latest-state projection, not the durable event stream

The Runner and HDF5 event stream remain complete and ordered. GUI state is a
bounded projection:

- safety and terminal events are immediate;
- active operation and axis values are latest-state coalesced;
- tree/model changes are flushed at a bounded cadence;
- spectrum preview is independently rate-limited and decimated;
- the visible event log is bounded while HDF5 retains the complete stream.

## Domain model

The following immutable types define the shared language:

```python
@dataclass(frozen=True, slots=True)
class SweepStageSpec:
    stage_index: int
    start: Quantity | None
    stop: Quantity | None
    value: Quantity | None
    spacing: str
    points: tuple[Quantity, ...]

@dataclass(frozen=True, slots=True)
class SweepAxisBinding:
    axis_id: str
    source_node_id: str
    owner_node_id: str
    device_module: str
    endpoint: str
    parameter_id: str
    target: str
    dimension: str
    stages: tuple[SweepStageSpec, ...]
    points: tuple[Quantity, ...]

@dataclass(frozen=True, slots=True)
class AxisPointContext:
    axis_id: str
    point_index: int
    point_count: int
    stage_index: int
    value_si: float
    active_setpoints_si: Mapping[str, float]
    loop_path: tuple[str, ...]
```

`SweepAxisBinding.target` remains the stable key used by
`app/recipes/parameter_registry.py`. `parameter_id` remains owned by the device
provider. Values cross UI, parser, compiler, runner, and storage boundaries as
typed quantities or explicit SI fields.

Each semantic tree node has:

```text
semantic_id
kind
source_node_id (optional for generated rows)
label data
axis binding (optional)
children
editable
draggable
```

Runtime state is kept separately from structure:

```python
@dataclass(frozen=True, slots=True)
class SemanticOperationState:
    semantic_id: str
    phase: str
    requested_si: float | None
    applied_si: float | None
    readback_si: float | None
    action_index: int
    total_actions: int
    axis_context: AxisPointContext | None
```

`MeasurementTreeModel` updates only the indexes whose state changed.

## Device-provider boundary

`RecipeExtension` gains an optional device-owned `sweep_provider`. A provider:

- resolves a legacy local parameter action into `SweepAxisBinding`;
- validates whether a parameter is sweepable for the current snapshot;
- compiles one axis point into a validated device update request;
- identifies the authoritative applied/readback field;
- supplies operator-facing labels and endpoint identity.

The core Sweeps UI must not contain Keithley/Rigol/Anritsu conditionals for
parameter identity, dimensions, or device limits.

The provider returns a neutral compiled description; it does not communicate
with hardware. Existing adapters remain the only owners of protocol operations
and readback.

## Canonical tree scenarios

### One variable parameter

```text
Measurement sequence
└─ Keithley B · configuration · OUTPUT ON for block
   └─ Sweep axis · Source current
      0 A → 10 mA · 10 points · 1 ROI stage
      └─ For each source-current point · 10
         ├─ Set ROI value · Keithley B · source current = {point}
         ├─ Acquire spectrum · Anritsu · TRAC1
         └─ Wait · 2 s
└─ Finally — safe shutdown
```

Execution at point 4/10:

```text
▶ Sweep axis · Source current                 POINT 4/10
  └─ Set ROI value · Keithley B current      APPLIED
     Requested 3.333333 mA · applied 3.333 mA
```

### Two axes on two devices

```text
Measurement sequence
└─ Keithley B · configuration
   └─ Sweep axis · source current · 3 points
      └─ For each current point
         ├─ Set ROI value · Keithley B current = {I}
         └─ Rigol CH1 · configuration
            └─ Sweep axis · frequency · 4 points
               └─ For each frequency point
                  ├─ Set ROI value · Rigol CH1 frequency = {f}
                  ├─ Wait · 100 ms
                  └─ Acquire spectrum · Anritsu
```

This produces 12 measurement points. During the inner loop the active context
shows both outer and inner values:

```text
Keithley B current = 5 mA · Rigol CH1 frequency = 1.333 GHz
```

Device configuration executes exactly where it appears. Placing Rigol inside
the outer loop intentionally reconfigures it for each outer point. Placing its
configuration before the outer axis configures it once.

### Two parameters on one device

```text
Measurement sequence
└─ Keithley B · configuration
   └─ Sweep axis · source current · 3 points
      └─ For each current point
         ├─ Set ROI value · Keithley B source current = {I}
         └─ Sweep axis · voltage compliance · 2 points
            └─ For each compliance point
               ├─ Set ROI value · Keithley B voltage compliance = {V}
               ├─ Wait · 100 ms
               └─ Acquire spectrum · Anritsu
```

This produces six points. The current source value remains active while the
inner compliance axis advances. Device safety validation runs for every
combination.

## Sweeps interaction design

The page's single job is to make the physical execution order obvious before a
run starts.

The tree uses four columns:

```text
Operation                       Configured / active value       Progress    State
```

At normal desktop width, Operation receives approximately 46% of tree width,
Value 34%, Progress 12%, and State 8%. The Value column must not collapse below
the width required for one formatted setpoint. At narrow widths, Progress and
State remain visible while long details elide with a complete tooltip.

ROI stages are axis metadata, not executable siblings. The axis row summarizes
the full range and point count. Selecting it opens the existing ROI editor. A
compact stage strip may appear in the inspector, for example:

```text
[ROI 1 · 0→5 mA · 6] [ROI 2 · 5→10 mA · 5]
```

Dropping an action on the loop body inserts it after the generated
`Set ROI value` row. The generated row is selectable for explanation but never
editable, draggable, deletable, or a drop target.

Nested axis movement updates a preflight summary such as `3 × 4 = 12 points`
before the source is committed.

## Visual direction

The visual language is a restrained laboratory procedure, not a generic card
dashboard.

- Reference palette: instrument paper `#F7F9FC`, raised surface `#FFFFFF`,
  measurement blue `#0F6CBD`, running blue `#2563EB`, confirmed green
  `#107C10`, caution amber `#C27C0E`. Implementation maps these semantics through
  existing `ThemeTokens`; it does not add per-page hardcoded QSS colours.
- Typography: existing Fluent/application type system. `StrongBodyLabel` weight
  for instrument/axis identity, body weight for values, caption treatment for
  units and provenance. No new bundled font.
- Layout: one dominant measurement tree with a quiet inspector; plots and logs
  must not reduce the tree below its useful minimum size.
- Signature element: an active-loop spine — one narrow accent rail connecting
  the currently active outer and inner axes to the running operation. It encodes
  loop ownership and is the only prominent visual flourish.
- Motion: a restrained, interruptible pulse on the running operation and a
  short state cross-fade. No row movement during execution. Reduced-motion mode
  replaces the pulse with a static high-contrast indicator.

Colour is never the only state signal. Every state also has text and an icon.
Keyboard focus, accessible names, dark/light contrast, empty loops, invalid
axes, disabled nodes, hover, drop acceptance, and drop rejection are required
states.

## Execution presentation

Execution receives the semantic snapshot produced during preflight. It never
reparses a different structure during the run.

The active operation card shows:

```text
Device · endpoint
Parameter
Requested value
Applied/readback value
Verification state
```

The tree shows aggregate loop progress on every active axis. Outer-axis values
remain visible while an inner axis changes. The generated `Set ROI value` row
is the row highlighted while the device update is being applied.

Technical compiler/adapter action names remain available in a secondary
technical trace and the HDF5 event stream. They are not main measurement-tree
rows.

## Performance contract

The implementation shall enforce these rules:

1. Tree structure is created once per accepted preflight snapshot.
2. Runtime events never clear, clone, or rebuild the complete tree.
3. State updates emit `dataChanged` only for affected semantic IDs.
4. No `expandAll()`, column resize, or unconditional `scrollTo()` occurs per
   action event.
5. Active-row following is limited to semantic operation transitions and at
   most 10 times per second.
6. Tree/status presentation flushes at most 30 times per second.
7. Spectrum preview flushes at most 5 times per second and is decimated before
   plotting when the source has more display samples than horizontal pixels.
8. The visible event log is bounded; the durable HDF5 stream remains complete.
9. Safety/fault/shutdown events bypass presentation coalescing.
10. No JSONL audit flush, HDF5 write, VISA operation, spectrum transformation,
    or full device-page refresh runs on the GUI thread.

Instrumentation records maximum queue depth, event counts, coalesced counts,
tree-update duration, preview-update duration, and longest observed GUI timer
gap during qualification.

The acceptance run is a deterministic, hardware-free 1000-point simulation
with 10,001-value spectra. The tree must visibly advance throughout the run;
the GUI timer gap must remain below 250 ms on the qualification workstation,
excluding debugger pauses and initial window creation.

## Safety and output semantics

- Normalization and presentation cannot relax compiler, safety-policy, adapter,
  watchdog, cancellation, compliance, or shutdown checks.
- Configuration remains OUTPUT OFF unless the explicit device output policy
  safely enables or continues output.
- `Set ROI value` does not toggle OUTPUT for each point.
- Continuous-output assertions remain explicit and readback-backed.
- Requested values appear immediately; applied/readback values appear only
  after adapter confirmation or a labelled simulated acknowledgement.
- A failed update leaves the displayed applied state unchanged and marks the
  affected endpoint `UNKNOWN` or `FAULT` according to runner state.
- `Finally — safe shutdown` remains a separate immutable branch.

## Persistence and compatibility

- Existing schema-version-1 recipes remain readable and executable.
- Original recipe source and plan hash remain provenance records.
- Legacy local sweep actions normalize deterministically; ambiguous multiple
  local sweep actions remain rejected until represented as explicit nested axes.
- Canonical saves use one representation per axis and never duplicate meaning.
- Existing HDF5 `setpoints_si` remains compatible.
- Axis provenance is additive: `axis_id`, `stage_index`, `point_index`,
  `loop_path`, requested/applied/readback value, and semantic operation ID.
- Recovery continues to use immutable plan identity and confirmed safe
  boundaries; the UI model is never recovery authority.

## Out of scope

- Hardware-native sweep/list modes.
- Implicit paired/zip axes.
- Relaxing DUT, station-profile, compliance, or hardware limits.
- Replacing the Fluent application shell.
- Redesigning Results beyond reading additive axis provenance.
- Treating simulation as proof of physical HIL qualification.

## Acceptance criteria

1. Sweeps shows one axis hierarchy with a generated per-point set operation.
2. Execution renders the same semantic node IDs and structure.
3. One axis displays its current formatted value next to the device endpoint.
4. Two nested devices display both active setpoints and produce the expected
   Cartesian point count.
5. Two nested parameters on one configured device compile and execute without
   the current one-local-axis rejection.
6. Technical `update_*` action names do not appear as primary tree rows.
7. Requested and applied/readback values are distinguishable and unit-safe.
8. Old recipes retain their execution meaning and source provenance.
9. The 1000-point simulation meets the responsiveness contract.
10. Desktop and narrow rendering tests call `show()`, process Qt events, and
    verify visible non-zero geometry, scroll reachability, focus, and active-row
    visibility.
11. Stop, fault, compliance, watchdog, and E-STOP preserve the independent safe
    shutdown contract.

