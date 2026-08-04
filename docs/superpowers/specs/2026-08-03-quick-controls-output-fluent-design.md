# Quick Controls A/B Output and Fluent Window Design

## Problem

Quick Controls currently renders one output row per device with a channel selector. The selector starts on the first channel, so an `OUTPUT ON` click targets only Keithley A and gives no direct affordance for Keithley B. The floating window itself still inherits `StationDialog` (`QDialog`), so only its inner controls are QFluentWidgets; the native host surface and title bar remain legacy.

The instrument log also shows both Keithley outputs read `0`, so the UI must distinguish an observed `OFF` state from an unconfirmed state and must never infer B is energized from A.

## Goals and invariants

- Show Keithley A and B as separate, always-visible output controls.
- Keep A and B electrically independent: clicking A never implicitly enables B.
- Offer an explicit `OUTPUT ON A+B` action. It validates and readbacks each channel through the adapter. If the second enable fails, already-enabled channels are disabled and the failure is reported; no UI state is painted `ON` without a successful readback.
- Keep `OUTPUT OFF` available for each channel and for the group, including when the audit lock blocks energizing.
- Publish per-channel `ON`, `OFF`, or `UNKNOWN` state from the device page to Quick Controls. `UNKNOWN` is used after a failed/ambiguous mutation.
- Replace the Quick Controls `QDialog` host with QFluentWidgets `FluentWidget`, preserving non-modal behavior, stay-on-top behavior, geometry persistence, keyboard access, and normal/narrow desktop geometry.
- Keep all existing setpoint synchronization, shared safety bounds, and typed-value slider precision unchanged.

## Design

### Output flow

The UI exposes per-channel `output_requested(device, channel, enabled)` and a separate `output_group_requested(device, enabled)` signal. MainWindow routes both through the owning page. The Keithley page routes the group request as one worker operation, `set_output_group`, to the Keithley adapter.

The adapter validates every requested channel before enabling it, enables in stable A-then-B order, and verifies each hardware state. On failure it invokes the existing emergency-off path and confirms/propagates the safe-state result. Group OFF attempts every requested channel and preserves an unknown/fault state if confirmation is not possible.

The page emits `output_state_changed(channel, state)` after confirmed state changes and after state becomes unknown. MainWindow forwards that state to the floating window, which updates the matching row without changing the device command path.

### Fluent host

`QuickControlsWindow` subclasses the installed `qfluentwidgets.FluentWidget`, which provides the project-compatible frameless Fluent title bar without introducing a navigation shell for a small floating tool. Its central widget uses the existing station theme tokens and QFluent cards/labels/buttons. The output card uses two compact channel rows, a restrained group action, explicit state badges, and responsive wrapping at narrow widths.

### Error behavior

- Invalid, unconfigured, locked, compliance-latched, or out-of-envelope output requests are rejected by the adapter; the page reports the error and marks affected channels `UNKNOWN` only when the hardware state cannot be confirmed.
- A failed group enable never retries a state-changing command blindly.
- A group failure never causes the other channel's confirmed state to be overwritten with `OFF` unless the adapter has read it back.

## Verification

- Adapter tests assert group enable command order, per-channel readback, and rollback/failure handling.
- Page/coordinator tests assert separate A/B requests, group dispatch, and state propagation.
- Quick Controls rendering tests show the window, process events, assert a non-zero normal-size geometry, and verify separate A/B controls plus the Fluent host type at a narrow size.
- Existing quick-control synchronization and quantity precision tests remain green.

