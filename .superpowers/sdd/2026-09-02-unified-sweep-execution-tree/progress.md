# SDD ledger — plan: docs/superpowers/plans/2026-09-02-unified-sweep-execution-tree.md

Baseline: master working tree at start; pre-existing edits in `app/ui/execution/page.py`, `app/ui/shell/main_window.py`, and `tests/test_fluent_recipe_execution_pages.py` are preserved and will be reconciled, not reset.

## Pre-flight plan scan

| Scope | Produces / consumes | Finding and ruling |
|---|---|---|
| Task 1 ↔ Task 2 | characterization tests consume semantic normalizer | Ordered correctly: red projection tests precede graph implementation. |
| Task 2 ↔ Task 3 | `AxisBindingResolver`, `SweepBindingDraft`, `SweepAxisBinding` | Provider contract is the concrete resolver implementation; keep resolver structural and provider registration authoritative. |
| Task 2 ↔ Task 6 | immutable tree consumed by compiler | Semantic IDs and axis context must be stable before plan expansion; no UI-derived IDs. |
| Task 3 ↔ Task 6 | provider compile points consumed by compiler | Compiler delegates validation/quantization to registered provider; no duplicated device maps. |
| Task 4 ↔ Task 5 | model/view consumed by Sweeps | Sweeps migration must be first user of the Fluent model; editing may retain a separate legacy editor only until Task 11. |
| Task 5 ↔ Task 8 | shared semantic snapshot | Execution accepts the exact snapshot; it must not clone item widgets or infer ownership by prefixes. |
| Task 6 ↔ Task 7 | `PlanAction.semantic_id`/`axis_context` consumed by Runner | Compiler emits identity, Runner emits confirmed state; requested values never become applied state. |
| Task 7 ↔ Task 9 | typed events consumed by presentation buffer | Coalescing is presentation-only; durable event ordering remains complete. |
| Task 7 ↔ Task 10 | operation state/provenance consumed by storage | Additive metadata only; existing setpoint schema remains unchanged. |
| Task 8 ↔ Task 9 | Execution model updated by bounded buffer | Runtime flushes are targeted and rate-limited; tree replacement only at run/preflight boundaries. |
| Task 10 ↔ Task 11 | recovery/provenance verified by release gate | Semantic nesting enters plan identity; UI expansion never enters recovery identity. |
| Task 1 | creates responsiveness/tree tests; modifies builder/execution tests | Internally coherent; expected red tests may require fixture helpers. |
| Task 2 | creates semantic graph/tests; modifies parser exports | Internally coherent; canonical binding validation is additive to v1 parsing. |
| Task 3 | creates provider contracts/modules/tests; modifies registry manifests | Internally coherent; provider modules must reuse existing safety/quantity functions. |
| Task 4 | creates shared tree package/tests | Internally coherent; `TreeView` is the Fluent component and custom model owns mutable state. |
| Task 5 | modifies Sweeps page/dialogs/tests | Internally coherent; editing compatibility remains until final cleanup. |
| Task 6 | modifies compiler/estimation/policy and async call sites/tests | Internally coherent; registry injection must reach every compiler construction. |
| Task 7 | modifies Runner/worker/tests and adds execution state | Internally coherent; terminal/fault ordering remains immediate and durable. |
| Task 8 | modifies Execution/main window/tests | Internally coherent; existing temporary batching changes are migration material, not a reason to reset. |
| Task 9 | modifies presentation/plot/worker/tests | Internally coherent; cap plot/log work and prohibit full-tree mutation per event. |
| Task 10 | modifies storage/recovery/tests | Internally coherent; additive private axis context with legacy empty context. |
| Task 11 | removes retired runtime path/docs/tests | Internally coherent; retain `RecipeTreeWidget` only for unrelated consumers if any. |

### Rulings

- Ruling: execute directly on `master` — the user explicitly requested `master`, so the plan's isolation recommendation is overridden; cost if wrong: uncommitted work remains on the shared branch and must be reviewed carefully.
- Ruling: preserve the three pre-existing modified files and adapt them in place — they are user-owned changes; cost if wrong: some temporary batching code may require extra cleanup during the migration.
- Ruling: use `qfluentwidgets.TreeView` plus `QAbstractItemModel` — this satisfies the Fluent shell contract while avoiding item-mutation stalls; cost if wrong: tests or installed Fluent API may require a narrow compatibility adjustment.

## Task status

Task 1: complete (commit 38aa799, review PASS; test-teardown evidence concern parked for final qualification)
Task 2: complete (semantic graph and legacy normalization implemented; focused tests pass)
Task 3: complete (device sweep providers and contracts implemented; focused tests pass)
Task 4: complete (Fluent TreeView/QAbstractItemModel implementation and model tests pass)
Task 5: complete (Sweeps page uses the shared semantic projection; focused UI tests pass)
Task 6: complete (compiler emits semantic IDs, ROI contexts, and unit-safe point actions)
Task 7: complete (Runner emits lifecycle/confirmed semantic state; wait-duration regression passes)
Task 8: complete (Execution consumes the shared tree and coalesced presentation state)
Task 9: complete (bounded semantic/preview/log presentation buffers are in place)
Task 10: complete (additive provenance and HDF5 persistence tests pass)
Task 11: pending

Latest WAIT follow-up: engine timing was verified independently; the UI metadata
loss was fixed and covered by compiler, Runner, and Execution regression tests.
The 1000-point GUI max-gap qualification is still pending and must not be
reported as a release pass.
