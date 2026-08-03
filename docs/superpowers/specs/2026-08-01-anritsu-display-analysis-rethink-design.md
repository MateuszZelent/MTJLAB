# Anritsu Display-Driven Spectrum Analysis Design

## Goal

Make live analysis, overlays, peak markers, peak tracking, manual archive
saves, and floating views refer to one explicit spectrum snapshot. When more
than one trace is visible, the operator chooses which trace is analyzed.

## Observed failure

`AnritsuPage._update_signal_analysis()` currently submits
`_latest_trace.powers_dbm` to the worker. That is always the raw TRAC1 frame.
Reference operations are calculated later in `_refresh_spectrum_display()`, so
the plot can show `Signal - reference` while the peak table and markers still
describe raw data. Cleanup results are also stored as one page-global value and
are not identified by source, unit, or frame generation. The manual archive's
processed choice similarly reads cleanup state separately from the displayed
reference-operation result.

The manual archive error in the supplied screenshot is a second state problem:
an append writer can remain owned by one page/session while another save path
tries to inspect or resume the same HDF5 file. The archive must have one writer
owner and an explicit close/reopen lifecycle.

## Design

### 1. Immutable display snapshot

Add a small, device-local display model with immutable values:

```python
@dataclass(frozen=True, slots=True)
class SpectrumDisplayTrace:
    key: str
    label: str
    frequencies_hz: tuple[float, ...]
    values: tuple[float, ...]
    unit: str
    frame_id: int
    provenance: tuple[str, ...]
```

`SpectrumDisplayState` derives the currently available traces from raw,
averaged, reference, and reference-operation inputs. It also exposes the
currently selected analysis key and the active display trace. Every derived
trace keeps its unit and provenance. The raw `SpectrumTrace` remains immutable
and is never overwritten by cleanup or reference processing.

Available keys are stable (`raw`, `averaged`, `reference`, `processed`, and
`analysis:<source-key>`). A selected key is retained while it remains
available; if it disappears, the model selects the first valid trace and emits
the new selection to the UI.

### 2. Explicit analysis source

The analysis card gets an `Analyze trace` combo. It lists only traces currently
available in the display state. If multiple traces are visible, the operator
must choose one; the default is the primary displayed trace selected by the
existing visibility/operation state. Changing cleanup mode, reference
operation, visibility, reference data, or a live frame refreshes the list and
submits the selected trace.

The analysis request contains the complete selected snapshot, including
`source_key`, `frame_id`, `unit`, `values`, cleanup mode, history for that same
source, and a monotonically increasing generation. The result echoes source
key, frame id, unit, and generation. The page applies a result only when all
identity fields still match current display state; otherwise it discards the
stale result without replacing current markers.

### 3. Unit-safe cleanup and peak analysis

Cleanup operates on generic finite display values while preserving the source
unit. dBm traces retain dBm semantics. Relative dB and linear ratio/mW²
results retain their own units and are never converted as though they were
dBm. Peak results expose the measured amplitude and unit; physical dBm model
fits remain enabled only for dBm traces, while non-dBm traces use unit-safe
location/prominence detection without falsely labeling a dB value as dBm.

Peak table, marker labels, tracking history, status text, and floating mirror
all consume the same selected analysis snapshot/result.

### 4. Live behavior

Every completed live frame updates raw display immediately. The display model
then derives the selected target and submits at most one latest analysis
request. Pending requests are coalesced. A mode/reference/source change forces
a new generation even when the raw frame is unchanged. A completed result for
an older frame or source can never overwrite the current display.

### 5. Manual archive behavior

Manual save options refer to a display-trace key and capture the exact values,
unit, source key, and provenance from the current display snapshot. Saving a
processed/reference-operation trace therefore writes what the operator sees,
not a separately reconstructed cleanup result.

`ManualSpectrumArchive` owns at most one open writer. Reusing the same append
destination reuses that writer; switching destination closes the old writer
before opening another. Reopening an existing append file validates it only
after any owned writer is closed, and close/destruction paths release the
writer. Errors identify whether the file is owned by the current session or
cannot be opened because another process still owns it; no second writer is
created over an uncertain HDF5 state.

## Safety and data contracts

- The analyser is never configured or queried by display analysis.
- Raw acquisition data and reference data remain immutable in memory and in
  persisted files.
- Analysis is display-only and cannot enable output or change instrument state.
- Frequencies remain Hz; dBm, dB, ratio, and mW² remain distinct units.
- Manual HDF5 append preserves contiguous checkpoints, provenance, and close
  status under the existing storage contract.

## Verification

- Pure model tests cover source availability, explicit selection, unit and
  provenance preservation, and fallback when a source disappears.
- Worker/page tests prove that reference-operation and denoise analysis receive
  the selected values rather than raw values, and stale generations are
  ignored.
- Rendering tests show the selector and unit-aware status at normal and narrow
  desktop sizes.
- Manual archive tests cover repeated append saves, switching destinations,
  explicit close, reopen/resume, and failure without a second open writer.
- Existing Anritsu, spectrum-processing, storage, and safety suites remain
  green.
