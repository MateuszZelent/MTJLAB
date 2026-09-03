# Task 1: Characterize current tree semantics and GUI stalls

Read this brief first; it is the task requirements. Work on the existing `master` workspace and preserve unrelated edits.

Files:
- Create `tests/test_execution_ui_responsiveness.py`.
- Modify `tests/test_recipe_builder.py` and `tests/test_fluent_recipe_execution_pages.py` only as needed for red characterization coverage.

The tests must consume the current `RecipePage`, `RunMonitorPage`, compiler plan, and simulated run events. They are intentionally red against the current projection and define:

1. A `GuiGapProbe(QObject)` with `QTimer` interval 20 ms, `time.monotonic()`, `maximum_gap_s`, and `ticks` exactly as in the plan.
2. A one-axis builder assertion whose semantic labels are exactly:
   - `Measurement sequence`
   - `Keithley B · configuration`
   - `Sweep axis · Source current`
   - `For each source-current point`
   - `Set ROI value · Keithley B · source current`
   - `Acquire spectrum · Anritsu`
   - `Wait · 2 s`
   - `Finally — safe shutdown`
3. A same-device nested-axis compile test asserting `plan.total_points == 6`.
4. A `@pytest.mark.qualification` 1000-point simulation test using `build_simulated_window(..., seed=17)`, the probe, `show()`, `probe.timer.start()`, `start_and_wait_for_run(..., expected_points=1000)`, and assertions `probe.ticks > 20` and `probe.maximum_gap_s < 0.250`.

Run exactly:
`python -m pytest -q tests/test_recipe_builder.py tests/test_fluent_recipe_execution_pages.py tests/test_execution_ui_responsiveness.py -k "semantic or same_device_nested or event_loop_live"`
The semantic and same-device tests should fail because the feature is not implemented yet; ensure failures are meaningful, not collection errors. Commit with message `test: define unified sweep tree contract` after the red characterization is captured.

Do not implement production code in this task. Do not spawn subagents. Write a report to `.superpowers/sdd/2026-09-02-unified-sweep-execution-tree/task-1-report.md` containing files changed, the red command/output, commit hash, and concerns; return only status, commit, one-line tests, concerns.
