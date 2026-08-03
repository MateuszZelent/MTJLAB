# Keithley per-channel compliance recovery

## Goal

When one Keithley SMU channel reaches voltage/current compliance, protect that
channel without interrupting an independent measurement on the other channel.
The application must never bypass configured current, voltage, power, DUT, or
immutable instrument limits.

Each channel has an independent `Stop on compliance` policy. The station
safety setting supplies the initial value; the card toggle changes the policy
for the active session without changing any current, voltage, power, DUT, or
hardware limit. With the policy disabled, the Keithley output remains on under
the instrument's limiter, the card shows `COMPLIANCE ACTIVE — OUTPUT CONTINUES
(LIMIT REACHED)`, and same-mode source increases above the triggering level are
blocked until a lower value clears the warning. A compliance warning on one
channel never disables or locks the other channel.

## Approved operator behavior

1. A compliance event records the channel, requested source value, measured I/V,
   limiting value, and the configured safety envelope.
2. Only the affected channel is commanded `OUTPUT OFF` and its readback is
   verified. The other channel is not changed.
3. The affected channel is shown as `COMPLIANCE — OUTPUT OFF`; its output
   control remains locked until recovery. The unaffected channel keeps its
   current state and controls.
4. The page presents two explicit choices for the affected channel:
   - `Restore previous setpoint` (restore configuration only; do not energize),
   - `Keep OFF / edit new setpoint`.
   Neither choice automatically enables an output.
5. After a choice, the operator must use `Apply & verify settings · OUTPUT OFF`
   and then explicitly click `OUTPUT ON`. The normal source, lab-envelope,
   DUT-envelope, power, hardware-range, and output-readback checks run again.
6. A global E-STOP, communication loss, unknown readback, or an unconfirmed
   channel-specific shutdown still uses the existing fail-safe all-output
   shutdown path.

## Architecture

- `KeithleyAdapter` tracks compliance-latched channels and previous source
  requests separately from aggregate output state.
- Compliance handling uses an idempotent `disable_channel_and_verify(channel)`
  path. It never calls the all-channel emergency shutdown unless readback is
  unavailable or inconsistent.
- `recover_from_compliance(channel, choice)` clears only the requested channel
  latch after that channel remains confirmed OFF. `restore` changes the cached
  request/configuration but does not enable output; `keep_off` simply clears the
  latch after the operator has acknowledged the choice.
- The worker/module dispatchers carry the channel and choice explicitly.
- The worker/module dispatchers carry channel-scoped recovery and policy
  operations explicitly.
- The Keithley page stores per-channel UI fault/recovery state. The recovery
  controls are rendered in the affected channel card, while the other card
  remains operational.

## Failure handling

- Any failed or ambiguous channel OFF readback escalates to the existing
  all-output emergency-off path and leaves the device `UNKNOWN`/locked.
- A failed configuration restore leaves the affected channel OFF and presents
  a non-modal error banner with retry/edit actions.
- No recovery path clears the instrument error queue or treats a missing
  compliance query as permission to energize.

## Verification

- Simulator test: compliance on A disables only A, leaves B ON, and rejects a
  direct A re-enable until recovery.
- UI test: A card shows the two choices while B remains enabled; accepting a
  choice does not issue `OUTPUT ON`.
- Simulator/UI tests: continue mode leaves the affected output ON, highlights
  compliance, preserves the other channel, and rejects only same-mode source
  increases above the compliance-triggering level.
- Fault-injection test: failed A readback triggers all-output shutdown and
  `UNKNOWN`.
- Existing limit, output interlock, and global E-STOP tests remain green.
