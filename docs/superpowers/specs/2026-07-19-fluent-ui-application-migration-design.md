# Fluent UI Application Migration

## Goal

Migrate the complete Lab Control user interface to
`PySide6-Fluent-Widgets` while preserving the existing instrument-control,
safety, recipe, execution, storage, and plotting behavior.

The migration must produce one coherent desktop design system rather than a
collection of independently styled pages. It must remain suitable for a
measurement station: status and safety information take priority over
decoration, motion is restrained, and every hardware-affecting action keeps its
existing authorization and confirmation path.

## Decisions

- The application is open source and may use the GPLv3 edition of
  `PySide6-Fluent-Widgets`.
- `PySide6-Fluent-Widgets` is the primary widget and theme layer.
- The migration is incremental, beginning with the application shell and
  Dashboard/Find VISA.
- Existing PySide6 pages remain valid during migration through a compatibility
  boundary.
- PyQtGraph remains the plotting implementation.
- Existing device adapters, workers, controllers, safety policy, settings
  repository, recipe compiler, run engine, and storage layers are not rewritten.
- QSS is limited to station-specific semantics that the Fluent library does not
  provide.
- Animation communicates navigation or state change only. It never delays an
  emergency, output, connection, or run-control action.

## Current-state findings

The application currently has a useful separation between the shell,
Dashboard, device pages, recipes, execution, results, settings, controllers,
and device modules. This permits page-by-page migration.

The principal visual debt is centralized in `app/main.py`: two large,
duplicated light and dark QSS strings define both generic widget appearance and
device-specific states. Several pages also contain local hard-coded colors.
The current top ribbon emulates application navigation with a hidden
`QTabWidget`, while nested tabs and plain Qt widgets create inconsistent
hierarchy.

The migration therefore replaces the visual foundation without changing the
domain architecture.

## Dependency policy

Use the PySide6 package, not the PyQt5 or PyQt6 variants, because all variants
export the same `qfluentwidgets` package name and cannot safely coexist.

The first implementation phase will add and lock a tested
`PySide6-Fluent-Widgets` version compatible with the project's PySide6 range.
As of this design, PyPI publishes version `1.11.2`; implementation must verify
the resolved dependency set and import surface before committing the pin.
The optional `full` extra is out of scope. This migration uses only components
available in the standard distribution.

## Target architecture

### Application shell

`MainWindow` becomes a Fluent window with left-side navigation and a central
stack of page widgets. The navigation hierarchy is:

```text
Station
  Overview
  Discovery
Devices
  Rigol DG1000Z
  Keithley 2600
  Anritsu MS2830A
  MOKE Box
  Lake Shore 475
Automation
  Sweeps
  Execution
Data
  Results
System
  Settings
```

The shell owns only navigation, window persistence, theme application, global
status presentation, event-log access, and safe shutdown. It does not absorb
device or workflow logic.

The current bottom event-log dock remains available through a low-emphasis
navigation or title-bar action and preserves its dock/restore behavior.

### Persistent safety strip

A compact persistent strip remains visible independent of the selected page.
It shows:

- overall station readiness;
- active-output count and explicit output-active warning;
- profile approval/lock state;
- simulation state;
- current actor/role summary;
- a visually isolated global E-STOP.

Status uses icon, label, and color together. Color is never the only signal.
The E-STOP remains a direct Qt signal path to the existing emergency handler;
it is not animated, debounced, or routed through page transitions.

### Page contract

Every top-level page is a normal `QWidget` with:

- a stable `objectName`;
- a concise page title and optional subtitle;
- optional page-level primary actions;
- content that can be placed directly in the Fluent stacked navigation area;
- no dependency on the old hidden top-level `QTabWidget`;
- existing public signals preserved until all consumers and tests migrate.

During migration, a `FluentPageHost` adapts legacy pages and their existing
scroll areas without changing page internals. A page leaves the compatibility
host only after its visual and behavioral tests pass.

### Design-system package

`app/ui/design_system/` becomes the single application-facing styling API:

```text
app/ui/design_system/
  __init__.py
  theme.py
  tokens.py
  station_qss.py
  plot_theme.py
  motion.py
  status.py
```

Pages import semantic helpers from this package. They do not import raw palette
constants from Fluent internals and do not define new hard-coded colors.

The package exposes immutable theme tokens for:

- background, surface, raised surface, and border;
- primary and muted text;
- accent and focus;
- success, caution, danger, and neutral status;
- output active, compliance, interlock, and emergency;
- plot background, axes, grid, reference trace, and measurement traces;
- spacing scale, corner radii, typography roles, and supported motion
  durations.

Fluent supplies generic widget appearance. Tokens supply station semantics.
`station_qss.py` generates only the selectors needed for semantic properties
such as `safetyState`, `outputState`, `validationState`, and `deviceState`.

### Theme flow

Theme mode remains `system`, `light`, or `dark` in station settings.

```text
settings / OS color scheme
        |
        v
effective_theme()
        |
        +--> qfluentwidgets setTheme()
        +--> semantic station tokens
        +--> compact station QSS
        +--> PyQtGraph plot palette
        +--> theme_changed signal
```

A theme change updates existing pages and plots without reconstructing device
controllers or interrupting active sessions. The existing system-theme listener
remains.

### Fluent component policy

Prefer Fluent widgets for user-facing controls:

- Fluent window and navigation components for the shell;
- primary, secondary, transparent, and tool buttons;
- line edits, combo boxes, spin boxes, switches, and search fields;
- card, information-bar, badge/status, tab/pivot, progress, dialog, and
  teaching-tip components;
- Fluent table/tree/list variants where they support the required accessibility
  and cell-widget behavior.

Native Qt widgets remain acceptable where replacing them adds risk without
visible benefit, including `QDockWidget`, specialized splitters, PyQtGraph
containers, and compatibility-hosted legacy pages.

No page may depend on a Pro-only component.

## Dashboard and discovery design

Dashboard is the first migrated product page and establishes the pattern for
the rest of the application.

### Overview

The overview uses calm instrument summary cards with consistent status,
connection, identity, and safe-action placement. Cards expose existing
connection/test signals and do not open new VISA sessions themselves.

### Find VISA

Find VISA replaces the current six-column `QTableWidget` presentation with a
responsive result-list model. Each result row/card presents:

- recognized model or a clear `Unknown instrument`/`No response` title;
- VISA resource in copyable monospace text;
- backend as secondary metadata;
- identity summary;
- a semantic status badge;
- a device assignment selector only when assignment is valid;
- one explicit assignment action.

Responding, unavailable, and already-assigned resources are visually distinct.
Timeout details are collapsed to a concise error with an optional detail
expander or tooltip; raw errors are not the dominant page content.

The page header contains scan state, last scan result summary, `Scan VISA`, and
the batch-save action. Scanning shows indeterminate progress and disables only
conflicting actions. Empty, scanning, successful, partial, and failed states
all have explicit presentations.

Discovery behavior remains read-only except for the existing confirmed
assignment flow. It continues to enumerate configured backends and send only
`*IDN?` under the current short timeout policy.

## Remaining page designs

### Device pages

Each device page adopts the same outer structure:

1. device header with connection and identity;
2. safety/status strip;
3. main workspace split between controls and visualization/readout;
4. contextual actions;
5. advanced or diagnostic content behind a low-emphasis expander.

Device-specific controls and plots remain specialized. Similar structure does
not imply forced feature parity.

### Sweeps

The sweep page uses Fluent command actions around the existing tree/editor
model. Selection, drag-and-drop, compilation, safety validation, and snapshot
providers retain their contracts. Errors move to a consistent information bar
and field-level validation system.

### Execution

Execution emphasizes current phase, progress, pause/resume/stop state, active
outputs, and the next safe action. Motion is limited to progress indicators and
short page/state transitions. Control availability continues to come from the
run controller, not presentation heuristics.

### Results

Results keeps its file browser, metadata, spectrum, heatmap, and sweep-tree
components. The surrounding navigation and controls become Fluent. PyQtGraph
uses the semantic plot palette and is otherwise unchanged.

### Settings

Settings uses grouped Fluent setting cards with the existing repository,
authorization, validation, approval-revocation, and atomic-save behavior.
Dangerous or approval-invalidating changes remain explicit and confirmed.

## Motion and responsiveness

Supported motion is intentionally small:

- 120–180 ms navigation/content transitions;
- progress-ring or progress-bar animation for active background work;
- short information-bar appearance/dismissal;
- no looping decorative animation;
- no animation on data traces unless already required for live plotting;
- no delayed hardware command caused by animation.

Motion is disabled or reduced when the OS requests reduced motion, when tests
run in offscreen mode, and for emergency paths.

Pages must remain usable at 1280×720 and scale cleanly at the existing default
1360×880. Navigation may compact; safety status and E-STOP may not disappear.

## Accessibility

- Preserve or improve every existing accessible name and description.
- Maintain keyboard navigation and visible focus.
- Do not encode device or safety state with color alone.
- Keep minimum practical control sizes and readable contrast in both themes.
- Ensure screen readers receive status changes through labels or accessible
  descriptions, not animation alone.
- Preserve F5 scanning and Ctrl+Shift+E emergency shortcuts.

## Error handling

Fluent information bars replace ad hoc banners for transient success, warning,
and failure messages. Modal dialogs remain for confirmation of destructive,
hardware-affecting, or approval-invalidating actions.

Exceptions retain their current logging and audit paths. A presentation failure
must not suppress the underlying event log or audit record. Missing Fluent
assets or invalid theme configuration fail during startup tests rather than
silently reverting to an inconsistent mixed theme.

## Migration stages

### Stage 0 — dependency and compatibility proof

- add and lock the PySide6 Fluent dependency;
- add a minimal import/runtime smoke test;
- establish the design-system package and theme bridge;
- prove PyQtGraph and Fluent coexist in light, dark, and offscreen test modes.

### Stage 1 — shell and Dashboard

- replace the top ribbon with Fluent navigation;
- add the persistent safety strip;
- preserve window, dock, shortcut, and workspace restoration;
- migrate Overview and Find VISA;
- keep all other pages in `FluentPageHost`.

### Stage 2 — shared interaction primitives

- migrate notifications, dialogs, connection panels, limit fields, validation,
  and common page headers;
- remove generic-control rules from the legacy QSS.

### Stage 3 — device pages

- migrate Rigol, Keithley, Anritsu, MOKE Box, and Lake Shore pages one at a
  time;
- verify each device module before beginning the next.

### Stage 4 — workflows and data

- migrate Sweeps, Execution, Results, and Settings;
- apply the semantic PyQtGraph palette across all plots.

### Stage 5 — consolidation

- remove the legacy top-level QSS strings and compatibility host;
- remove obsolete icons/selectors and direct color constants;
- run the full automated and simulated qualification suites;
- perform light/dark visual review at supported resolutions.

Each stage must leave the application runnable and testable. A stage is not
complete merely because the migrated page renders.

## Testing strategy

### Architecture tests

- only the PySide6 Fluent distribution is declared;
- domain/device/storage layers do not import `qfluentwidgets`;
- pages use the design-system facade for station-specific colors;
- legacy top-level QSS shrinks monotonically and is absent after Stage 5.

### Unit and widget tests

- theme resolution and token selection;
- station QSS semantic selectors;
- navigation key-to-page mapping;
- page-host compatibility;
- safety-strip state mapping;
- discovery empty/scanning/success/partial/failure states;
- assignment permissions and payloads;
- keyboard shortcuts and focus behavior;
- reduced-motion behavior.

### Regression tests

Existing tests remain authoritative for:

- connection/test/disconnect flows;
- VISA discovery and assignment;
- authorization and profile approval;
- recipe compilation and execution;
- emergency stop;
- result loading and plot behavior;
- settings validation and persistence.

Tests must be changed only when the presentation contract intentionally changes;
signal payloads and safety behavior are not relaxed to accommodate the new UI.

### Visual verification

For every migrated stage:

- render or launch in offscreen/simulation mode;
- capture light and dark screenshots at 1280×720 and 1360×880;
- inspect focus, clipping, scroll behavior, long VISA resources, long IDN
  strings, errors, disabled states, and active-output warnings;
- verify no mixed legacy/Fluent styling remains on the migrated surface.

## Non-goals

- rewriting adapters, workers, controllers, or the run engine;
- moving the application to QML, Qt WebEngine, React, or another UI runtime;
- replacing PyQtGraph;
- adding new instrument capabilities;
- introducing a Pro Fluent dependency;
- decorative acrylic or transparency where it reduces readability;
- changing safety limits, authorization rules, or hardware command sequences.

## Completion criteria

The migration is complete only when:

- the main window and every top-level page use the Fluent shell and component
  system;
- semantic design tokens drive station states and all PyQtGraph themes;
- the two large legacy QSS strings and page-local hard-coded theme colors are
  removed, except documented device-data colors that encode a fixed physical
  quantity;
- all existing hardware, safety, recipe, execution, storage, and results
  behavior remains covered and passing;
- simulation qualification passes;
- light, dark, and system themes are visually verified at supported sizes;
- reduced motion and accessibility checks pass;
- no compatibility-hosted legacy page remains.
