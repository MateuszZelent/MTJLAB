# Find VISA Layout Repair

## Goal

Restore the Find VISA tab to a coherent, production-ready layout and repair
the two cell-widget regressions introduced by the current uncommitted UI work.

## Root causes

The result table has a maximum height of 400 pixels and is added without a
stretch factor. In a tall window, the vertical layout distributes unused space
between the information label and the table, leaving the table detached at the
bottom of the tab.

Assignment controls, action buttons, and assigned-state labels are wrapped in
transparent container widgets. This changes the established `cellWidget`
contract: callers and tests receive a generic `QWidget` instead of the
interactive `QComboBox`, `QPushButton`, or `QLabel`.

## Design

The Find VISA tab remains a single vertical workspace:

1. title and scan actions;
2. scan status/instructions;
3. result table filling all remaining space.

The table has no fixed maximum height. It receives vertical stretch in the
layout and keeps a modest minimum height for small windows. Empty space belongs
inside the empty result table rather than between related controls.

Cell widgets are inserted directly into the table. Row height, minimum control
widths, and existing application styles provide spacing without structural
wrappers. Assigned rows expose a `QLabel` in the Assignment column and a
disabled `QPushButton` in the Action column. Unassigned rows expose a
`QComboBox` and an enabled or disabled `QPushButton` according to assignment
permissions and discovery status.

The first two columns size to their contents. Status also sizes to contents.
VISA resource and Identity/error share the remaining width; Backend sizes to
contents.

## Accessibility and interaction

- Existing accessible names and tooltips remain intact.
- Direct widgets preserve keyboard focus and activation.
- The scan button and Save assignments button remain aligned with the title.
- No animation or additional visual decoration is introduced.
- Mouse-wheel tab switching remains disabled as in the current UI.

## Tests

Tests will verify:

- the direct cell-widget types for assignable and already-assigned rows;
- clicking the direct action button emits the selected assignment;
- the result table has no finite maximum-height cap;
- the table receives vertical stretch in the Find VISA layout;
- column resize modes remain responsive;
- existing Dashboard tests continue to pass.

## Out of scope

- Device-module migration.
- Find TCP/IP or Saved-tab redesign.
- Changes to VISA discovery, identification, assignment persistence, or safety
  permissions.
- Broader Dashboard visual redesign.

## Completion criteria

The two known failing discovery tests pass, the Find VISA table begins directly
under its status text and fills the remaining tab height, and the focused
Dashboard/MainWindow test set reports no regression caused by this repair.
