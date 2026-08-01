# Anritsu Manual Spectrum Save Configuration Design

## Problem

The Anritsu manual spectrum archive card currently opens
`ManualSpectrumSaveDialog` from `Save current spectrum…`. This makes archive
configuration and saving one operation. A user who wants to archive several
completed spectra must repeat the destination, file policy, trace variant, and
metadata choices for every spectrum.

## Decision

Separate archive configuration from spectrum saving in the Anritsu page.

- `Configure archive…` opens the existing Fluent dialog and stores its
  validated `ManualSpectrumSaveOptions` in the page.
- `Save current spectrum` writes the latest completed trace using the stored
  options without opening a dialog.
- Saving is disabled until a configuration has been accepted and a completed
  trace is available.
- Configuration may be reopened at any time to replace the stored options.
- The existing `ManualSpectrumArchive` remains responsible for HDF5 creation,
  append-session reuse, frequency-grid validation, checkpointing, and close
  semantics.

The configuration is session-local. It is not added to `settings.yml`, because
the selected output path and metadata policy describe an archive session rather
than station defaults.

## Components and data flow

`AnritsuPage` owns an optional `ManualSpectrumSaveOptions` value. The page
continues to derive available trace variants and confirmed metadata when the
configuration dialog opens. On acceptance, it stores the immutable options and
updates the card status/target display.

The save path then follows:

1. Check for a completed trace and stored options.
2. Resolve the configured trace variant from the current page state.
3. Lazily create `ManualSpectrumArchive` with the existing settings, device
   identity, and operator context providers.
4. Call `ManualSpectrumArchive.save` with the stored destination, mode,
   metadata scope/values, trace variant, and any processed values.
5. Update the status and append-session target while retaining the same saved
   options for the next trace.

When a save fails, the stored configuration remains intact so the user can
retry after correcting the underlying issue. A timestamped save still closes
its writer through the existing archive implementation; an append save still
uses the existing active session and close action.

## UI states

The card exposes three distinct states:

- Unconfigured: `Configure archive…` is enabled; `Save current spectrum` is
  disabled and the card says that configuration is required.
- Configured but no trace: both configuration and save policy are visible, but
  saving is disabled until a completed spectrum exists.
- Configured with a completed trace: `Save current spectrum` is enabled and
  performs the save directly; `Configure archive…` remains available for
  changes.

The existing `Close append session` action remains independent and is enabled
only while an append writer is active. Configuration does not create a file or
open a writer.

## Error handling and safety

No instrument command is sent by either archive action. Saving only consumes
the already completed trace in memory and already confirmed metadata. Existing
archive exceptions are surfaced through the existing banner/status path, with
the configuration retained for retry. Invalid or missing configuration is
handled before archive creation and produces a clear card status instead of a
partial write.

## Tests

Add focused UI regression coverage that verifies:

- the separate configure control is rendered;
- accepting configuration enables saving only when a completed trace exists;
- saving uses the stored options and does not reopen the configuration dialog;
- reopening configuration replaces the options used by the next save;
- a save failure retains the stored configuration for retry.

Keep the existing dialog and `ManualSpectrumArchive` tests. Run the focused
Anritsu UI/manual writer tests, then `ruff check app tests` and the relevant
broader test targets.
