---
name: verify-instrument-safety
description: Implement or review safety-critical laboratory instrument control in this repository, including output configuration, arming, enabling, ramps, compliance, lab and DUT limits, capability checks, VISA commands, retries, watchdogs, cancellation, emergency stop, shutdown confirmation, recovery, simulation, and audit events. Use whenever a change can energize hardware, alter a physical setpoint, communicate with an instrument, or affect fault and safe-state behavior.
---

# Verify Instrument Safety

Assume hardware can retain its last output after software failure. Safety requires enforced limits, deliberate enablement, observable state, and confirmed shutdown—not merely a disabled button.

Read [references/safety-contract.md](references/safety-contract.md) before changing command execution, device adapters, the compiler, recovery, or output-related UI.

## Build the safety case

For every output-affecting change, identify:

1. lab hardware envelope;
2. experiment/DUT envelope;
3. more restrictive effective limit;
4. required capability and identity evidence;
5. exact configure → arm → enable order;
6. readback after mutation;
7. timeout, transport, compliance, cancellation, and recovery behavior;
8. command that establishes and confirms safe state.

If any item is unknown, fail closed and keep outputs disabled.

## Enforce defense in depth

- Parse explicit-unit values before comparisons.
- Validate at compilation/preflight and again in device safety/adapters.
- Check identity, capabilities, locks, safety-profile approval, and one-shot permission.
- Read back safety-relevant state after mutation when supported.
- Record requested and actual values plus safety context in audit/checkpoints.

UI validation is guidance, never the authority.

## Control outputs deliberately

Configure while output is confirmed off. Separate arming from enabling. Do not retry non-idempotent output commands after state becomes uncertain. Ramp hazardous changes using finite bounded steps, deadlines, and interruptible waits. Treat compliance/trip as a stop condition and preserve diagnostics before shutdown when possible.

## Fail safely

On exception, cancellation, watchdog, or storage failure, attempt all approved shutdown actions even if one fails. Disable sources, abort RF/acquisition, flush checkpoint/event, query confirmation, and report `UNKNOWN` or `FAULT` when safety cannot be confirmed.

Never report `SAFE` solely because OFF was sent. Resume only from a recorded safe boundary after re-verifying identity, capabilities, settings, plan identity, and output-off state.

## Keep discovery read-only

Discovery may enumerate resources and issue a short-timeout identity query. It must not configure or enable outputs. Saving an assignment changes the safety profile and revokes approval.

## Verification checklist

- Test just below, at, and just above effective limits.
- Test wrong dimensions, non-finite values, reversed ranges, missing DUT constraints, and unsupported capabilities.
- Assert exact command order and output-off configuration.
- Inject failure at each mutation and verify remaining shutdown attempts.
- Test watchdog, stop, compliance, transport loss, readback mismatch, and failed shutdown confirmation.
- Verify audit/checkpoints contain requested/actual values and safety context without secrets.
- Mark behavior not verified against qualified hardware documentation or hardware.
