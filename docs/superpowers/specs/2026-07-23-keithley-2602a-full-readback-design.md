# Keithley 2602A full readback and export

## Goal

Extend the Keithley device readback into a comprehensive, read-only audit of
the documented 2602A configuration and diagnostics, and let the operator save
the result as JSON or YAML. The feature must never change an SMU output,
clear an error, run a trigger model, or copy a hardware value into the active
station profile automatically.

## Scope

The audit will cover both SMUs and group values into:

- output safety: output state, off mode, off function and off limits;
- source configuration: both current and voltage levels, limits, ranges,
  autorange, delay, compliance and source protection-related status;
- measurement configuration: ranges, autorange, NPLC, filters and sensing;
- triggering and digital I/O;
- instrument/local-node configuration and non-mutating diagnostics.

The driver will use an explicit registry of public, documented properties for
the qualified 2602A protocol. Arbitrary user TSP variables, scripts, data
buffers, files, and unbounded tables are excluded: they are not stable device
configuration and cannot be enumerated safely.

## Behaviour

Each registry item has its own read-only TSP query, parser, group, label and
optional comparison rule. A field unavailable on the installed firmware is
reported as `Unsupported`; a failed query is reported as `Read error`; both
allow the remaining audit to complete. `errorqueue.count` is read, but
`errorqueue.next()` is never called because it consumes state.

The UI presents grouped rows with device A, device B, the relevant application
value where one exists, and a match/mismatch/read-status indicator. The
existing compact source-form comparison remains useful, while fields without
a meaningful application peer are explicitly device-only. Output OFF mode is
compared against the active station safety profile, and is never assignable
from the readback dialog.

Operators may export the exact audit snapshot to JSON or YAML. Exports include
UTC timestamp, verified identity and firmware, per-field values/status, and
comparison results. Export is local only and does not send VISA/TSP commands
beyond the readback queries.

## Verification

Focused simulator/fake-VISA tests will assert the query-only command set,
both-channel field parsing, unsupported and per-field-error handling,
HIGH-Z/profile mismatch presentation, and JSON/YAML export. Existing safety
tests must continue to prove no output mutation occurs during readback.
