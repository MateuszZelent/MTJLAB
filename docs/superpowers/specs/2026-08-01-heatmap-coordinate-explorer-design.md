# Heatmap Coordinate Explorer Design

## Goal

Replace the positional `Checkpoint` heatmap axis with physical sweep coordinates and let an operator choose any two available dimensions as the heatmap X and Y axes.

## User experience

- The heatmap toolbar exposes `X axis` and `Y axis` selectors.  Both list Frequency and every reconstructed sweep dimension, with units in their labels.
- The initial selection is X = Frequency and Y = the innermost sweep axis.  For the current run this becomes `Frequency (Hz)` × `Keithley B current (A)`.
- A dimension not selected for X or Y becomes a filter.  Its selector chooses one exact stored coordinate value.  When Frequency is a filter, it selects one exact spectral bin.
- Axes must differ.  Changing either axis rebuilds the filter controls and requests a fresh matrix.
- The UI never averages, interpolates, or merges measurements.  Missing coordinate combinations stay NaN/gaps.  Duplicate combinations are rejected with an actionable explanation asking the operator to refine filters.
- Clicking a valid cell opens the source checkpoint spectrum.  The cell-to-checkpoint mapping, rather than a display row number, is authoritative.
- `Checkpoint` remains an explicit fallback only when no complete physical coordinate can be proven from the file.

## Coordinate evidence and reconstruction

Create a result-view coordinate model that produces one immutable coordinate series per spectral checkpoint.

1. Prefer private `StoredPoint` setpoints when they align with the selected spectral row.
2. Otherwise use public thaTEC scalar-control rows that have one finite value per spectral checkpoint.
3. When public scalar rows are absent, reconstruct recipe sweep coordinates from the embedded recipe snapshot with `ThatecSchemaMapper`, validate its Cartesian product and acquisition order against the spectral checkpoint count, and reject mismatches.
4. Fall back to the ordered checkpoint index only after the above evidence sources fail; display why physical coordinates are unavailable.

Every coordinate preserves its persisted SI value and canonical unit.  Display formatting is performed only by the existing quantity formatter; dBm and dB remain distinct value dimensions.

## Matrix construction

Given a spectrum row, two selected dimensions, and exact values for all remaining dimensions:

- If Frequency is one selected axis, read each matching checkpoint spectrum and place every frequency bin against the other selected dimension.
- If Frequency is filtered, read only the requested bin from each matching checkpoint and place its power on the two selected sweep dimensions.
- Build a `cell_checkpoint_indices` matrix alongside the colour matrix.  A valid cell points to one source checkpoint; a gap has no source index.
- Sort physical coordinates monotonically for display while applying the same permutation to values and source indices.

The existing colour matrix remains amplitude in raw dBm or processed dB, selected independently of coordinate axes.

## Boundaries and errors

- Reject an axis absent from the selected row's coordinate model.
- Reject identical X/Y dimensions.
- Reject non-finite coordinates, unmatched scalar rows, incompatible grids, duplicate selected cells, and filters without a matching checkpoint.
- Retain background reads and cancellation.  Each new axis/filter request invalidates earlier reads before rendering.

## Verification

- One-current sweep: default Frequency × Current B labels, units, coordinates, and click mapping.
- Two nested sweeps: both axis orientations plus a filter selecting one outer coordinate.
- Frequency-as-filter plane: two swept controls on X/Y at one selected frequency bin.
- Gaps and duplicate-coordinate rejection.
- Raw dBm and processed dB variants retain their correct colour labels and ranges.
- Rendered geometry is checked after `show()` at desktop and narrow sizes.
