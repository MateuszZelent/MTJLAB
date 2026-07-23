# Anritsu Fresh Spectrum Acquisition Design

## Problem

The manual **Read spectrum** control passively reads `TRAC? TRAC1`.  On the
qualified MS2830A, when `INIT:CONT?` is `0` and no sweep is running, this is
the last completed Trace A buffer, so repeated clicks correctly return the
same payload.

## Decision

Manual acquisition is an owned, qualified operation: start one single sweep,
wait for its completion, then read `TRAC1`.  The existing
`AnritsuAdapter.acquire_single_sweep()` already implements and was verified
against serial `6201514799`, firmware `7.03.00`, producing three distinct
trace hashes.

Live acquisition remains analyser-owned continuous measurement: startup puts
Trace A in `WRIT` and selects continuous sweep; timer ticks only read the
current completed buffer.  Each received trace remains subject to the
existing stale-frame hash diagnostic.

## Safety and failure behaviour

The change neither enables RF output nor changes spectrum configuration.  It
uses the profile-qualified single-sweep protocol and its deadline.  A failed
sweep or transfer retains the last valid display and uses the existing error
path; it must never present the old trace as a newly acquired one.

## Acceptance criteria

1. One manual click dispatches `single_sweep`, not `fetch_current_trace`.
2. The adapter command order remains `INIT:MODE:SING`, completion wait, then
   `FORM ASC`/`TRAC? TRAC1`.
3. Live startup remains Continuous and each button/poll reports completed
   frames without treating a repeated hash as fresh data.
4. Focused UI/controller and adapter tests cover the regression.
