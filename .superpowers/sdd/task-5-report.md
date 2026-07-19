# Task 5 Report: Persistent Station Safety Strip

## Implementation summary

Added the presentation-only `StationSafetyStrip` widget and frozen, slotted
`StationSafetySnapshot` dataclass. The strip renders readiness, active-output
state, profile, simulation/hardware mode, actor and roles. Its E-STOP button
emits `estop_requested` directly through the Qt click signal; it introduces no
animation, timer, delay, controller call, or hardware action. Both public types
are exported from `app.ui.shell`.

## TDD evidence

1. Added `StationSafetyStripTests` before the production widget/export code.
2. Ran the focused test command with the new test. It failed at collection as
   expected because `StationSafetySnapshot` and `StationSafetyStrip` could not
   yet be imported from `app.ui.shell`.
3. Implemented the minimal widget, snapshot, and exports required by the
   failing tests.
4. Re-ran the focused test command: `2 passed`.

## Verification

Exact command:

```powershell
python -m pytest tests/test_fluent_shell.py -q
```

Result: `4 passed in 2.30s` (exit code 0).

The focused red/green command was:

```powershell
python -m pytest tests/test_fluent_shell.py::StationSafetyStripTests -q
```

Red result: collection `ImportError` for the missing snapshot export.
Green result: `2 passed in 9.77s`.

## Files

- `app/ui/shell/safety_strip.py` (created)
- `app/ui/shell/__init__.py` (modified)
- `tests/test_fluent_shell.py` (modified)

## Self-review

- Snapshot is immutable (`frozen=True`) and uses slots.
- Semantic properties are set for readiness (`safetyState`) and outputs
  (`outputState`) and refreshed for style application.
- E-STOP is an immediate signal connection, with an accessible label.
- The component remains display-only and is not wired into `MainWindow`.
- `git diff --check` passed before commit.

## Concerns

None for Task 5. The strip is intentionally not integrated into the main
window; that integration belongs to Task 6.
