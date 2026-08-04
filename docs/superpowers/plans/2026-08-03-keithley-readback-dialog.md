# Keithley readback dialog clarity and responsive layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Keithley 2600 modeless, read-only configuration comparison clear, responsive, and explicit about the difference between source autorange/source range and measurement ranges.

**Architecture:** Keep the existing `_KeithleyReadbackDialog` as the single owner of readback presentation and preserve its existing `assign_requested` signal. Host it as a modeless floating surface so Quick Controls and other live windows remain usable while the snapshot is open. Improve only its labels, explanatory copy, comparison-cell formatting, action affordances, and geometry; the adapter/model and TSP query path remain unchanged. Add focused Qt regression tests in the existing `MainWindowTests` class, including `show()` and event processing at desktop and narrow sizes.

**Tech Stack:** Python 3, PySide6, PySide6-Fluent-Widgets, Qt `TableWidget`/layouts, unittest/pytest, existing quantity formatting helpers, Ruff.

## Global Constraints

- `read_configuration()` remains read-only: no TSP mutation and no OUTPUT state change.
- The dialog remains modeless and the table remains non-editable.
- OUTPUT state and OUTPUT OFF mode are never assignable from this dialog.
- Existing per-row source-group assignment and autorange-aware comparison rules remain unchanged.
- Use the existing quantity helpers and explicit dimension names; do not add a second unit registry.
- Keep the Fluent-native application shell and reuse existing project design tokens; no legacy shell or ad-hoc theme replacement.
- Preserve unrelated working-tree changes in the Keithley compliance recovery path.

---

### Task 1: Add failing modeless readback-dialog regression tests

**Files:**
- Modify: `tests/test_main_window.py` near `test_keithley_reads_both_channels_and_shows_modeless_configuration`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: `_KeithleyReadbackDialog`, `KeithleyConfigurationReadback`, `KeithleyChannelConfigurationReadback`, and the existing Qt test application fixture.
- Produces: assertions that define the new public UI vocabulary and rendered geometry without changing the dialog API.

- [ ] **Step 1: Add a reusable readback fixture inside the existing test method or nearby helper**

Use the existing two-channel values so the test covers source autorange ON/OFF, source range values, measurement autorange values, one OUTPUT ON state, and assignment widgets. Keep the test data in SI and let the dialog format it.

- [ ] **Step 2: Write the failing semantic-header and copy assertions**

Extend the readback dialog test with assertions equivalent to:

```python
headers = [
    dialog.table.horizontalHeaderItem(column).text()
    for column in range(dialog.table.columnCount())
]
self.assertEqual(
    headers,
    [
        "Parameter",
        "Hardware value\nChannel A",
        "Form comparison\nChannel A",
        "Action\nChannel A",
        "Hardware value\nChannel B",
        "Form comparison\nChannel B",
        "Action\nChannel B",
    ],
)
self.assertIn("Source autorange", dialog.range_guidance.text())
self.assertIn("Active source range", dialog.range_guidance.text())
self.assertIn("measurement range", dialog.range_guidance.text().lower())
self.assertIn("Form:", dialog.table.item(source_level_row, 2).text())
self.assertEqual(dialog.table.item(source_level_row, 5).text(), "MATCH")
```

The exact row lookup must use the parameter text rather than a hard-coded row index so the test documents the table contract.

- [ ] **Step 3: Add failing action and non-assignable-row assertions**

Assert that an assignable source row has a `Use hardware value` button with an accessible name naming the channel and parameter, that the footer action is `Use all compatible values`, and that OUTPUT state/OFF mode have no cell widget and expose a tooltip explaining why they cannot be copied.

- [ ] **Step 4: Add failing normal and narrow geometry assertions**

Show the dialog, call `processEvents()`, and assert:

```python
self.assertGreaterEqual(dialog.width(), 980)
self.assertGreaterEqual(dialog.height(), 620)
self.assertGreater(dialog.table.width(), 850)
self.assertGreater(dialog.table.height(), 300)
```

Then resize to `980×620`, process events, and assert the dialog/table/footer remain visible with positive geometry and the table has a vertical scroll bar or can display all visible rows without the footer covering it. Use the existing application cleanup pattern.

- [ ] **Step 5: Run the focused test and verify it fails for the intended missing UI contract**

Run:

```powershell
pytest tests/test_main_window.py -k "keithley_reads_both_channels_and_shows_modeless_configuration" -q
```

Expected: FAIL because the current dialog still has `Device A`, `Current form A`, blank action headers, `Assign` labels, and the old fixed geometry/copy.

---

### Task 2: Implement explicit table vocabulary and range guidance

**Files:**
- Modify: `app/devices/keithley_2600/ui/page.py` in `_KeithleyReadbackDialog.__init__`, `_assign`, and `_assign_all`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: existing `readback`, `configured`, `_channel_values`, `_snapshot_values`, `_values_match`, and `assign_requested` behavior.
- Produces: `range_guidance` label, semantic table headers, explicit comparison strings, and unchanged assignment signal payloads.

- [ ] **Step 1: Add a small comparison-display helper before editing the constructor**

Add a class/static helper with a concrete signature such as:

```python
@staticmethod
def _comparison_text(matches: bool, configured_value: str | None) -> str:
    if matches:
        return "MATCH"
    if configured_value:
        return f"Form: {configured_value}"
    return "Not controlled by form"
```

Use it for every status cell. Do not change `_values_match`; it already contains the autorange-aware invariant.

- [ ] **Step 2: Add the explanatory copy using Fluent labels**

Add a `BodyLabel` or `CaptionLabel` stored as `self.range_guidance`, set word wrap, and explain in concise English that source autorange chooses the source range, active source range is the current device-reported source range, and neither is normally the input measurement range. State the indirect effects (resolution, accuracy, range transitions, settling), mention that the instrument may couple ranges when source and measurement use the same function, and point the operator to the active measure rows for the actual returned measurement range.

- [ ] **Step 3: Replace ambiguous headers and action text**

Use exactly these headers:

```python
[
    "Parameter",
    "Hardware value\nChannel A",
    "Form comparison\nChannel A",
    "Action\nChannel A",
    "Hardware value\nChannel B",
    "Form comparison\nChannel B",
    "Action\nChannel B",
]
```

Create `PushButton("Use hardware value", ...)` for assignable rows, keep output rows without a cell widget, and set their tooltip to explain that output state/off mode are safety-controlled and must not be copied here. Rename the footer button to `Use all compatible values`; keep its signal as `("ALL", "ALL")` and its exclusion of output rows.

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```powershell
pytest tests/test_main_window.py -k "keithley_reads_both_channels_and_shows_modeless_configuration" -q
```

Expected: PASS, including the existing assignment assertions and the new exact labels/copy assertions.

---

### Task 3: Implement robust initial sizing and table layout

**Files:**
- Modify: `app/devices/keithley_2600/ui/page.py` in `_KeithleyReadbackDialog.__init__`
- Test: `tests/test_main_window.py`

**Interfaces:**
- Consumes: the existing `StationDialog` parent/window behavior and Qt screen geometry.
- Produces: a dialog that opens at a usable desktop size and remains usable at the supported narrow size.

- [ ] **Step 1: Add minimum/start size constants local to the dialog**

Set a minimum around `QSize(980, 620)` and an initial size around `QSize(1180, 760)`. Clamp the initial size to the available screen geometry when a screen is available, preserving margins for the desktop work area. Do not use a fixed maximum that prevents normal resizing.

- [ ] **Step 2: Give the content and footer stable layout ownership**

Keep the table as the stretch item in the main `QVBoxLayout`. Configure action columns to a fixed/interactive width sufficient for `Use hardware value`, parameter/value columns to resize-to-content or stretch as appropriate, and set the table's size policy to expanding. Avoid blank columns and avoid manual row heights that can overlap the footer.

- [ ] **Step 3: Add visible comparison legend and accessible names**

Add a short legend such as `MATCH = hardware equals form / Form: ... = value currently in the form` and set accessible names/tooltips for the table, guidance, footer action, and close button. Keep status colours semantic and readable in the existing light/dark theme.

- [ ] **Step 4: Run the focused geometry test at normal and narrow sizes**

Run:

```powershell
pytest tests/test_main_window.py -k "keithley_reads_both_channels_and_shows_modeless_configuration" -q
```

Expected: PASS with positive rendered geometry after `show()` and after resizing to `980×620`.

---

### Task 4: Verify the complete change and preserve unrelated work

**Files:**
- Inspect: `git diff -- app/devices/keithley_2600/ui/page.py tests/test_main_window.py`
- Test: `tests/test_main_window.py`, relevant adapter tests, full `ruff` target

**Interfaces:**
- Consumes: all changes from Tasks 1–3.
- Produces: evidence that readback remains query-only, assignment behavior remains intact, UI copy is unambiguous, and no unrelated compliance changes were overwritten.

- [ ] **Step 1: Run all focused Keithley readback and safety tests**

Run:

```powershell
pytest tests/test_main_window.py -k "keithley" -q
pytest tests/test_adapters_and_runner.py -k "keithley.*configuration or configuration.*keithley" -q
```

Expected: no failures; readback tests continue to prove query-only behavior and existing source/measurement assignment semantics.

- [ ] **Step 2: Run lint and diff checks**

Run:

```powershell
ruff check app tests
git diff --check
```

Expected: Ruff exits 0 and `git diff --check` reports no whitespace errors. Existing line-ending warnings are not treated as code failures.

- [ ] **Step 3: Inspect final diff against the accepted design**

Confirm that the diff changes only readback-dialog presentation/tests plus the design/plan docs, and that the pre-existing compliance recovery diff remains present and unchanged.

- [ ] **Step 4: Report exact verification evidence**

Report the test commands and counts/output, the new default/minimum geometry, the final column labels, and the preserved safety invariants. Explicitly call out if a real-hardware visual check was not possible and distinguish simulator/UI verification from hardware qualification.
