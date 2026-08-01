# Sweep Device Readiness Design

## Goal

Allow an operator to start a sweep with every device required by the compiled
plan already connected and identity-verified.  Starting a sweep must never ask
the operator to disconnect those devices first.

## Problem

`MainWindow._start_run()` currently rejects every non-disconnected manual
device session.  The run worker then creates a second, independent set of
adapters and opens its own sessions.  This makes a normal, verified connection
look like a prerequisite failure and forces an unnecessary manual workflow.

The current separation also has a real safety purpose: manual UI operations
must not race with a runner-owned output operation.  Removing the check alone
would leave two owners able to communicate with the same instrument.

## Chosen design

Introduce an explicit, exclusive **sweep session lease** for the devices used
by a compiled plan.

1. A preflight modal lists only `plan.required_devices`, using display names,
   assigned resources, identity evidence, and connection state.
2. A disconnected or unverified required device is marked incomplete.  The
   modal exposes **Connect missing devices**, which performs the existing
   safe connect/identity verification only; it must not configure or enable an
   output.
3. Once every required device is connected and verified, the modal enables
   **Start sweep**.  It must also explain that manual controls become
   read-only for the duration of the run.
4. Starting transfers each required, verified adapter into an exclusive run
   lease.  The runner uses these adapters rather than constructing second
   sessions, so the connection is retained across the transition.
5. While a lease is held, device-page actions cannot issue I/O.  E-STOP
   remains available through its independent, short-lived emergency path.
6. The runner retains the existing output-off, configure, arm, enable,
   readback, cancellation, watchdog, and ordered shutdown contracts.  On
   completion or setup failure it releases every lease only after confirmed
   shutdown/disconnect handling, and the UI refreshes its connection state.

## Modal behaviour

The modal is a Fluent-native dialog with a concise status row per required
device:

| State | Meaning | Available action |
| --- | --- | --- |
| Ready | Connected, assigned resource matches, identity verified | Start sweep when every row is ready |
| Not connected | No active safe session | Connect missing devices |
| Connection failed | Last connection attempt failed | Retry connection after showing the error |
| Unsafe or unknown | Output/readback state cannot be trusted | No start; use normal safe recovery/E-STOP workflow |

The modal never treats an unrelated device as a start prerequisite.  It does
not offer a disconnect action.  Cancelling it changes neither connection nor
output state.

## Safety invariants

- The plan compiler, runner, adapters, and safety policy remain authoritative;
  a ready-looking UI cannot bypass any output safety check.
- Connection preparation is discovery/identity verification only and leaves
  outputs off or fails closed if that cannot be demonstrated by the adapter.
- A device has exactly one active command owner: manual page or runner lease.
- A device whose state is `output_on`, `compliance`, `fault`, or `unknown`
  blocks the modal and cannot be leased for a new run.
- Failed connection, lease transfer, cancellation, storage failure, watchdog,
  or runner failure follows the existing safe-shutdown path and never reports
  `SAFE` without confirmation.
- Run provenance continues to include the verified device identity and
  capabilities actually used by the lease.

## Architecture boundaries

- The UI shell owns only the modal and presentation state.
- Device workers retain all adapter and transport ownership; their API gains a
  typed lease/release operation rather than exposing protocol objects to the
  UI.
- The run controller accepts validated leases and coordinates runner access.
  It must not open replacement adapter sessions for leased devices.
- Device-specific protocol code remains in device adapters.  The domain layer
  owns lease/readiness state and errors shared by the UI and run controller.

## Tests and acceptance criteria

1. A rendered Fluent modal at desktop and narrow window sizes visibly lists
   required devices and has non-zero geometry after event processing.
2. A missing required device shows the Connect action; its safe connection
   makes the row ready without enabling output.
3. Start remains disabled until every required device has successful identity
   evidence, and remains blocked for unsafe or unknown states.
4. A fully connected required set starts the run without a manual-disconnect
   warning and without opening duplicate device sessions.
5. Manual operations are rejected while a run lease is held; E-STOP remains
   reachable.
6. Connection/lease failures keep the run stopped, release any acquired
   leases safely, and preserve an actionable error.
7. Existing runner command-order, output-off, shutdown, audit, persistence,
   and recovery tests remain green.
