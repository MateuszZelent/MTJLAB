# Station Modal Shell Design

## Problem

Quick Controls has the most complete floating-window treatment in the
application: a quiet raised backdrop, a surfaced card, restrained elevation,
Fluent controls, and responsive content. `StationDialog` currently provides
theme metadata only, so its many modal and floating-window subclasses still
assemble their own outer geometry. The Anritsu spectrum window is a visible
example: it has the correct plot behavior but not the same visual shell.

## Goals

- Make the Quick Controls surface treatment reusable by all station-owned
  dialogs and floating windows.
- Preserve each consumer's existing `QDialog` or `FluentWidget` semantics,
  modal behavior, window flags, safety actions, and content widgets.
- Give `StationDialog` subclasses a shared raised surface even when their
  legacy content layout remains directly attached to the dialog.
- Migrate the Anritsu floating spectrum window to the shared surface as the
  first fully composed example.
- Increase the Quick Controls default size from `520x680` to `550x720` while
  preserving its content-based height fitting, 70% available-screen cap, and
  explicit manual resizing.
- Keep light and dark themes driven by the existing `ThemeTokens` contract.

## Non-goals

- Do not change hardware commands, output validation, readback, or modal
  acceptance/rejection logic.
- Do not replace `QDialog.exec()` with a different interaction model.
- Do not introduce a second application shell or navigation tree inside a
  modal.
- Do not rewrite every specialized form in one pass; the shared base surface
  gives existing dialogs a consistent frame, while new/migrated dialogs can
  use the exposed surface layout directly.

## Design

### Shared surface

`app.ui.dialogs.StationModalShell` is a reusable QWidget composition with:

- a token-aware backdrop using the same restrained vertical accent blend as
  Quick Controls;
- an inner `CardWidget` named `stationModalSurface` with station card
  properties and a drop shadow;
- a public `surface_layout` for title/header/content/footer composition;
- configurable outer and surface margins so a Fluent title bar can reserve
  its top area without duplicating geometry code.

The shell has no instrument knowledge and does not own a dialog result.

### StationDialog integration

`StationDialog` creates one shell as a child underlay and positions it inside
the dialog bounds on resize/show. Existing subclasses may continue to create
their current layout on `self`; the shell is lowered behind that content, so
the migration is non-breaking. New or migrated dialogs use
`self.modal_shell.surface_layout` directly for a complete shared composition.

The base still exposes `stationSurface="page"` on the dialog, keeps palette
refresh behavior, and preserves `QDialog` modality.

### Quick Controls integration

`QuickControlsWindow` uses `StationModalShell` as its central surface while
remaining a `FluentWidget`. Its title bar, resize handles, auto-height logic,
scrolling, settings geometry, output actions, and setpoint synchronization
remain owned by Quick Controls. Existing test-facing `backdrop`, `surface`,
and `surface_layout` references remain aliases to the shared shell parts.

### Anritsu spectrum integration

`_AnritsuSpectrumWindow` keeps its always-on-top, non-modal behavior and
`SpectrumPlotWidget`, but places its heading, plot, and status in the shared
`StationDialog.modal_shell.surface_layout`. No acquisition or adapter path is
changed.

## Visual contract

- outer margins: 10 px by default;
- surface content margins: 16 px horizontal, 14 px vertical by default;
- cards use station `surface`, `surface_raised`, `border`, and text tokens;
- shadow is subtle and identical for all shell users;
- narrow layouts remain scrollable or responsive rather than clipping;
- no raw `QPushButton` styling is introduced where a Fluent control exists.

## Verification

- Render tests show a `StationModalShell` and assert non-zero geometry in both
  themes.
- Existing `StationDialog` tests assert the shared shell is present and still
  preserves the page surface contract.
- Quick Controls tests assert the larger default size, shared shell aliases,
  auto-height cap, and manual resize behavior.
- Anritsu tests show the floating spectrum, assert the shared shell and plot
  geometry, and confirm the window remains non-modal.
- Run focused UI tests, `ruff check app tests`, and `git diff --check`.
