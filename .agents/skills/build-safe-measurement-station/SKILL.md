---
name: build-safe-measurement-station
description: Build, modify, review, or test this PySide6 laboratory measurement station across device modules, recipes, execution, storage, settings, and the Fluent application shell. Use for cross-cutting features, new instrument support, workflow changes, architectural refactors, or any change spanning more than one subsystem; route unit-sensitive, persistence-sensitive, and hardware-output work through the specialized project skills.
---

# Build Safe Measurement Station

Treat the repository as a safety-relevant measurement system, not a generic desktop app. Preserve physical meaning, fail-safe behavior, provenance, recoverability, and the Fluent-native shell in every change.

## Route the work

- Use `$enforce-measurement-quantities` whenever values cross recipe, UI, settings, protocol, plotting, or storage boundaries.
- Use `$verify-instrument-safety` whenever code can configure, arm, enable, ramp, stop, retry, or recover hardware outputs.
- Use `$preserve-measurement-data` whenever code changes measurement models, HDF5/CSV persistence, thaTEC/PyThat compatibility, checkpoints, resume, or result readers.
- Use the repository `AGENTS.md` contract and `apple-design` for Fluent shell, interaction, motion, and visual review work.

Load only the specialized skill relevant to the current change. Load all three for an end-to-end measurement workflow or a new device module.

## Establish the change contract

Before editing:

1. Trace the value or action from user input or recipe source to compiled plan, adapter command, acquired result, persisted representation, and result UI.
2. Identify the authoritative registry or model. Extend it instead of adding a parallel mapping.
3. Write down the safety state before, during, and after failure or cancellation.
4. Identify compatibility surfaces: settings YAML, recipe YAML, public HDF5, private recovery state, audit events, and UI labels.

Do not infer safety from UI state. The compiler, safety policy, adapter, runner, and storage boundary must enforce their own invariants.

## Preserve architecture

- Keep device-independent models and errors under `app/domain`.
- Keep device integration behind `app/contracts/device_module.py` and the registered module structure.
- Keep protocol and hardware details inside the device package; do not import device UI from engine or storage code.
- Keep recipe parameter identity centralized in `app/recipes/parameter_registry.py`.
- Keep the Fluent application shell coherent; do not introduce a legacy shell or compatibility facade.
- Prefer immutable typed request/result models at subsystem boundaries.
- Reject unknown enum values, dimensions, commands, and schema variants explicitly; do not silently guess.

## Implement vertically

For a new measurable or controllable quantity, update the smallest complete vertical slice:

1. domain quantity/model;
2. settings or recipe schema;
3. parameter registry and compiler;
4. device safety validation;
5. adapter/protocol conversion;
6. execution state and audit event;
7. persistence schema and reader;
8. UI formatting and validation;
9. focused tests at each boundary.

Do not declare the slice complete when only the UI or adapter works.

## Verify proportionally to risk

Run focused tests first, then the broader suite for cross-cutting changes. Include negative and fault-path tests, not only the nominal path.

- Quantity changes: parser, dimension mismatch, scale/exponent boundaries, finite-value checks, round-trip display.
- Hardware changes: command ordering, limits, readback, compliance, timeout, cancellation, and confirmed safe shutdown.
- Storage changes: interrupted append, close status, resume identity, schema validation, and PyThat round-trip.
- Shell/page changes: show the window, process events, and assert visible non-zero geometry at a normal desktop size and a narrow size.

Run `ruff check app tests` and the relevant `pytest` targets. Treat a skipped safety or compatibility test as unresolved unless the environment limitation is recorded.

## Report completion

State which contracts changed, which invariants were preserved, and exactly which tests ran. Call out any behavior that remains simulator-only or visually unverified.
