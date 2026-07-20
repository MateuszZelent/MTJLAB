---
name: preserve-measurement-data
description: Implement or review measurement persistence, HDF5 and CSV writing, thaTEC:OS/PyThat compatibility, schema manifests, metadata, timestamps, checkpoints, crash tolerance, run close status, recovery/resume, reference spectra, and result readers in this repository. Use whenever changing MeasurementPoint, SpectrumTrace, storage writers/readers, exports, persisted names or dtypes, or any workflow that must remain durable and scientifically interpretable after interruption.
---

# Preserve Measurement Data

Treat persisted measurement files as scientific records and compatibility contracts. Never trade provenance, crash consistency, or interpretability for a convenient local representation.

Read [references/storage-contract.md](references/storage-contract.md) before changing a writer, reader, schema, checkpoint, or resume path.

## Classify the surface

Identify whether the change affects the public thaTEC/PyThat schema, private recovery state, optional CSV index, immutable provenance, or result interpretation. Do not rename or repurpose a field in place; add a versioned field or explicit migration path.

## Preserve commit semantics

Validate a complete point and trace before mutation. Write a self-contained pending checkpoint, flush it, expose it through the committed link boundary, append the public thaTEC representation, mark completion, and flush again. Advance the in-memory count only after commit succeeds.

Flush safety/operator events immediately. Closing must set a terminal status, clear the public running flag, flush, close, and validate the final contract. A validation failure must not masquerade as success.

## Preserve scientific meaning

- Require finite scalar and array values.
- Require matching spectrum frequency/power lengths and stable grids where required.
- Store UTC timestamps with timezone evidence and required public numeric timestamps.
- Keep unit-bearing names and attributes stable.
- Preserve recipe/settings sources and hashes, device identity/capabilities, simulation status, DUT limits, and processing operation/unit.
- Use deterministic JSON where identity or diffs matter.

## Preserve recovery rules

Resume only after an externally confirmed safe boundary. Verify plan, recipe, and settings identity; reject completed runs; require contiguous complete checkpoints; remove pending work; truncate private and public data to the same checkpoint; rebuild CSV from committed data; record the resume event.

Never infer a safe resume boundary only from row count.

## Change the schema deliberately

When public HDF5 changes:

1. update `app/resources/thatec_manifest_v1.json` only for a real contract change;
2. update writer and reader together;
3. update validator rules;
4. retain or explicitly migrate old readable files;
5. verify qualified PyThat and real round-trip;
6. test nominal, empty, partial, and malformed files.

Do not update the golden hash unless the reference artifact was intentionally replaced and independently verified.

## Verification checklist

- Append and read scalar-only and spectrum points.
- Reject NaN, infinity, mismatched arrays, bad indices, and invalid processed data.
- Simulate interruption around the commit boundary.
- Verify completed, aborted, and faulted close behavior.
- Verify resume identity mismatch and truncation.
- Run HDF5, thaTEC reader/mapper/validator, reference-store, recovery, and affected result UI tests.
- Validate a produced file with `require_pythat=True`.
