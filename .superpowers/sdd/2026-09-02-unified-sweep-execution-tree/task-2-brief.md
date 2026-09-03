# Task 2: Add the immutable semantic recipe graph and legacy normalizer

Read this brief first; it is the task requirements with the exact interfaces. Work on existing `master`; preserve unrelated edits and Task 1 characterization changes. Do not spawn agents.

Create `app/recipes/semantic_tree.py`, modify `app/recipes/__init__.py` and `app/recipes/models.py`, and create `tests/test_recipe_semantic_tree.py`.

Public interfaces required:
- `SweepBindingDraft`, `SweepStageSpec`, `SweepAxisBinding`, `AxisPointContext`.
- `SemanticNodeKind` values exactly: `sequence`, `device`, `sweep_axis`, `loop_body`, `set_roi_value`, `action`, `finally`, `generated_safety`.
- frozen/slotted `SemanticTreeNode` and `SemanticMeasurementTree` with `roots`, `by_id`, and `require(semantic_id)` raising `ConfigurationError` for unknown IDs.
- structural `AxisBindingResolver` protocol with `module_key` and `bind_legacy_action(node: RecipeNode, action: Mapping[str, object]) -> SweepBindingDraft`.
- `normalize_recipe_tree(recipe, resolvers)` returning one immutable semantic snapshot.

Normalization must convert legacy device `parameter_actions` sweep entries and canonical explicit `sweep.binding` entries into one axis node whose children are one loop body and exactly one generated `Set ROI value` semantic child. Preserve `Recipe.source_text` unchanged. Generate deterministic stable IDs, axis point contexts, and parent/child indexes. Reject duplicate semantic IDs, unknown targets, dimension mismatches, empty stages, duplicate active bindings to one target, ambiguous multiple legacy local sweeps, and malformed bindings.

Modify recipe parsing so a `sweep` may carry:
```yaml
binding:
  owner_node_id: keithley-b
  device_module: keithley
  endpoint: B
  parameter_id: source.level
```
Validate mapping shape and all four non-empty strings in `models.py`; registry/provider remains authority for target and dimension. Keep schema-version-1 recipe compatibility and existing parser behavior.

Write focused tests first and run RED (`python -m pytest -q tests/test_recipe_semantic_tree.py`) before implementation, then run GREEN:
`python -m pytest -q tests/test_recipe_semantic_tree.py tests/test_recipe_compiler.py tests/test_sweep_points.py`.
Include tests for legacy Keithley normalization, nested axes/Cartesian product context, shared stage-boundary deduplication (0.005 appears once), duplicate target rejection, malformed binding, and source preservation. Commit with `feat: add semantic sweep tree normalization` and write report to `.superpowers/sdd/2026-09-02-unified-sweep-execution-tree/task-2-report.md`.
