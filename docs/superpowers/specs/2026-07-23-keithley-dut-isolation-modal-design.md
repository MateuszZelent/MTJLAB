# Keithley 2602A DUT isolation modal design

**Date:** 2026-07-23  
**Status:** Approved for implementation  
**Scope:** Manual Keithley page, Keithley adapter, simulator, and focused tests

## Purpose

Normal MTJ measurements use the Keithley `OUTPUT_NORMAL` output-off mode. The
operator nevertheless needs a short, deliberate interval in which the selected
SMU channel is physically isolated so a DUT can be connected or disconnected.

The application will expose that interval as a modal laboratory operation:

1. confirm that the selected channel OUTPUT is OFF;
2. switch only that channel to `OUTPUT_HIGH_Z` and verify it;
3. tell the operator that the DUT may now be physically connected or
   disconnected;
4. when the modal is dismissed, restore `OUTPUT_NORMAL` and verify it.

`HIGH_Z` is temporary operational state. It is not saved to the station
profile. The application default remains `NORMAL`.

## Terminology

The UI uses **DUT** rather than **device** so the action cannot be confused
with connecting or disconnecting the Keithley VISA session.

- `NORMAL`: Keithley output path remains electronically connected while OUTPUT
  is OFF.
- `HIGH-Z`: the selected channel output relay is open and the DUT is
  electrically isolated from that SMU channel.

## User interface

Each channel card has its own compact button next to its existing measurement
button:

- `Disconnect / connect DUT…` on channel A;
- `Disconnect / connect DUT…` on channel B.

The button operates on its card's channel directly; it does not depend on the
channel currently selected in the configuration form. This preserves the
explicit A/B behavior requested by the operator.

The button is enabled only when:

- the Keithley is connected and verified;
- that channel is enabled in the station profile;
- its OUTPUT state is known and confirmed OFF;
- no Keithley configuration, output, measurement, ramp, readback, or DUT
  isolation operation is pending.

When disabled because OUTPUT is ON, its tooltip says to turn that channel
OUTPUT OFF first. The operation never silently turns an energized channel off.

After `HIGH_Z` is confirmed, a modal message identifies the channel and says:

> Channel X is isolated (HIGH-Z). You may now disconnect or connect the DUT.
> Select Done after completing the physical operation; the application will
> reconnect the measurement path in NORMAL mode.

The primary action is `Done · restore NORMAL`. Closing the modal through its
window close control has the same meaning and starts restoration. While the
modal or restoration is active, both isolation buttons and all mutating or
measurement actions on the Keithley page are disabled. This prevents a second
channel operation or a measurement from racing the relay transition.

The modal opens only after `HIGH_Z` readback succeeds. It closes immediately
when requested, but the page continues to show a busy state until `NORMAL`
readback succeeds. A success banner then states that channel X is back in
`NORMAL` and ready for normal work.

## Adapter operations and command order

The adapter gains a channel-specific, typed operation for the two supported
temporary states. Arbitrary TSP strings are not accepted from the UI.

### Enter isolation

For channel X (`smua` or `smub`):

```text
print(smux.source.output)                         -> must be OFF
smux.source.offmode = smux.OUTPUT_HIGH_Z
print(smux.source.offmode == smux.OUTPUT_HIGH_Z) -> must be true
print(smux.source.output)                         -> must still be OFF
```

Only after all readbacks succeed does the adapter return confirmed `HIGH_Z`.

### Restore normal work

```text
print(smux.source.output)                         -> must be OFF
smux.source.offmode = smux.OUTPUT_NORMAL
print(smux.source.offmode == smux.OUTPUT_NORMAL) -> must be true
print(smux.source.output)                         -> must still be OFF
```

Only after all readbacks succeed does the adapter return confirmed `NORMAL`.

The operation is not retried automatically after a write or timeout. A
readback mismatch or transport error makes the off-mode state unknown.

### Normal-mode invariant

The following normal work paths must establish or verify
`OUTPUT_NORMAL` before continuing:

- applying a source or measure-only configuration while OUTPUT is OFF;
- enabling a channel OUTPUT;
- a manual or Live measurement.

If OUTPUT is OFF, the adapter may apply and verify `OUTPUT_NORMAL` as part of
the explicit operation. If OUTPUT is ON, it may only verify that the existing
off mode is `NORMAL`; it must not change relay mode under load. A mismatch
while energized fails closed and invokes the existing confirmed shutdown/fault
path.

This invariant ensures that a prior interrupted isolation workflow cannot
silently leave later measurements in `HIGH_Z`.

## Connection behavior

Keithley `Connect` remains read-only and keeps its existing traffic:

```text
*IDN?
print(smua.source.output)
print(smub.source.output)
```

It does not set OUTPUT, off mode, source settings, or clear the error queue.
The DUT isolation feature must not reintroduce any connection-time mutation.

## UI state flow

The page tracks one isolation workflow at a time:

```text
IDLE
  -> ENTERING_HIGH_Z
  -> OPERATOR_MODAL
  -> RESTORING_NORMAL
  -> IDLE
```

Any adapter failure transitions to `FAULT/UNKNOWN` presentation rather than
claiming that the path is connected or isolated. Pending state is cleared, but
normal measurement and OUTPUT enable remain blocked until `NORMAL` is
successfully re-established and verified.

Live measurement selection for the affected channel is stopped before entering
`HIGH_Z`. If both channels were selected, the unaffected channel is also
paused for the duration of the modal because the Keithley controller is shared;
the UI does not automatically resume Live afterward. This keeps resumption an
explicit operator action.

## Failure and shutdown behavior

- OUTPUT ON or unknown: reject before any off-mode write.
- Failure entering `HIGH_Z`: do not open the modal and do not tell the operator
  that isolation is safe.
- Failure restoring `NORMAL`: keep OUTPUT OFF, show a persistent fault message,
  and do not claim that the DUT is reconnected.
- Transport loss after a mutation: report off mode as unknown; do not retry the
  mutation blindly.
- Application shutdown while the modal is open: existing shutdown still
  commands OUTPUT OFF. Leaving the relay in confirmed or possible `HIGH_Z` is
  safer than closing it during process teardown. The next explicit
  configuration, measurement, or OUTPUT-enable operation must re-establish and
  verify `NORMAL`.
- Compliance and emergency-off behavior remain unchanged and take precedence
  over this workflow.

## Audit and status evidence

Existing VISA logging records the exact TSP writes and readbacks. Page status
messages identify:

- channel;
- requested off mode;
- confirmed result;
- failure or unknown state.

No DUT identifier or free-form operator text is collected by this feature.

## Tests

### Adapter tests

- exact enter-isolation command order and channel isolation;
- exact restore-normal command order;
- reject OUTPUT ON and unknown OUTPUT without an off-mode write;
- reject invalid channel or unsupported mode before VISA mutation;
- readback mismatch and transport failure produce an error and no blind retry;
- channel B operation never writes channel A and vice versa;
- configuration, measurement, and OUTPUT enable establish or verify NORMAL;
- `Connect` remains the exact read-only query sequence.

### Simulator tests

- channel-specific HIGH-Z/NORMAL transitions and equality readback;
- other channel state remains unchanged;
- measurement and OUTPUT enable return the target channel to NORMAL.

### UI tests

- one correctly parented button is rendered beside Measure on each channel
  card after `show()` and event processing;
- each button dispatches its own A/B channel;
- button disabled for OUTPUT ON, unknown state, disconnected state, and pending
  operations;
- modal appears only after confirmed HIGH-Z;
- modal dismissal dispatches NORMAL restoration;
- measurement/output controls stay disabled until restoration completes;
- failure messages never claim successful isolation or reconnection;
- narrow-window geometry keeps both channel actions visible and usable.

## Non-goals

- Persisting HIGH-Z as a profile default.
- Automatically turning OUTPUT OFF to begin DUT isolation.
- Switching both channels together.
- Disconnecting the VISA session.
- Allowing arbitrary output-off modes from the manual UI.
- Automatically resuming Live measurement after physical DUT handling.

