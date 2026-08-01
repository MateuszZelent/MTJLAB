# Keithley Limit Reconciliation and UI Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators accept a complete safe Keithley limit-change package while all device limit editors stage changes without freezing the UI.

**Architecture:** Add a pure Keithley safety-domain reconciler that calculates the edited leaf plus only necessary dependent leaves. Both Qt editors will show a shared Fluent confirmation modal. Replace the full Settings page rebuild with a changed-leaf synchronization path for staged limit-only snapshots.

**Tech Stack:** Python, PySide6, PySide6-Fluent-Widgets, Pydantic, pytest/unittest.

## Global Constraints

- Parse explicit electrical units with `parse_quantity`, calculate finite SI values, and format proposed values through `format_quantity_auto`.
- Preserve the primary edit; stage dependent edits only after accepting the entire package.
- Preserve `StationSettings.model_validate`, compiler, adapter hardware limits, output-off checks, readback, and audit as independent safeguards.
- Opening, accepting, or cancelling the proposal must not write YAML or send an instrument command; only **Save settings** persists or triggers the existing hot-apply path.
- Limit-only staging must not call `SettingsPage._populate()` or `_refresh_diagnostics()`; changed fields must be synchronized in place.
- Do not touch user-owned changes in `app/ui/results/heatmap_tab.py` or `tests/test_fluent_results_theme.py`.

---

### Task 1: Pure Keithley dependent-limit reconciliation

**Files:**
- Create: `app/safety/keithley_limit_reconciliation.py`
- Modify: `app/safety/__init__.py`
- Create: `tests/test_keithley_limit_reconciliation.py`

**Interfaces:**
- `propose_keithley_limit_adjustments(limits: Mapping[str, Any], primary_path: tuple[str, ...], primary_value: str) -> KeithleyLimitProposal`
- `KeithleyLimitAdjustment(path, previous, proposed, reason)` is immutable and paths are relative to `lab_limits`.
- Invalid dimension, non-finite input, reversed range, incompatible configured lower bound, and a value beyond 3 A/40 V raise `ConfigurationError`.

- [ ] **Step 1: Write the failing proposal tests**

Create a Channel B fixture by deep-copying `tests.helpers.SETTINGS_TEMPLATE`. Start with this exact behavioural test:

```python
def test_current_expansion_proposes_synced_trip_and_power() -> None:
    proposal = propose_keithley_limit_adjustments(
        _channel_b_limits(), ("source_current", "max"), "150 mA"
    )
    assert [(item.path, item.proposed) for item in proposal.adjustments] == [
        (("source_current", "max_abs"), "150 mA"),
        (("measured_current_trip", "max"), "150 mA"),
        (("max_abs_power",), "10.05 mW"),
    ]
```

Add one test each for source-voltage expansion, current-compliance expansion, voltage-compliance expansion, no-op valid edit, disabled power limit, power reduction requiring two dependent changes, trip reduction, unit mismatch, NaN, reversed range, 3 A current, and 40 V voltage boundary.

- [ ] **Step 2: Run tests to prove the API is missing**

Run: `python -m pytest tests/test_keithley_limit_reconciliation.py -q`

Expected: collection fails because `app.safety.keithley_limit_reconciliation` does not exist.

- [ ] **Step 3: Implement the domain-only reconciler**

Define these frozen dataclasses in the new module:

```python
@dataclass(frozen=True, slots=True)
class KeithleyLimitAdjustment:
    path: tuple[str, ...]
    previous: str
    proposed: str
    reason: str

@dataclass(frozen=True, slots=True)
class KeithleyLimitProposal:
    primary_path: tuple[str, ...]
    primary_value: str
    adjustments: tuple[KeithleyLimitAdjustment, ...]
```

Copy the supplied mapping, apply the parsed primary leaf, and derive each magnitude as `max(abs(min_si), abs(max_si))`. Keep range `max_abs` aligned with edited source ranges. Use these two constraints:

```python
current_mode_power_w = source_current_abs_a * voltage_compliance_abs_v
voltage_mode_power_w = source_voltage_abs_v * current_compliance_abs_a
required_power_w = max(current_mode_power_w, voltage_mode_power_w)
```

For an expanded source/compliance, expand only the necessary signed current/voltage trip boundary and then increase enabled `max_abs_power` if it is below `required_power_w`. For a power/trip reduction, retain that primary value and reduce the minimum number of source/compliance boundaries needed to satisfy both products and trip containment. Emit stable adjustment ordering: `max_abs`, current trip, voltage trip, power. Import only domain, quantity, settings-error, and Keithley immutable range constants; never import Qt or persistence code. Export the three public names from `app/safety/__init__.py`.

- [ ] **Step 4: Run focused safety regression tests**

Run: `python -m pytest tests/test_keithley_limit_reconciliation.py tests/test_settings_and_safety.py -q`

Expected: all proposal tests pass and existing source-times-compliance rejection remains green.

- [ ] **Step 5: Commit the safety-domain slice**

Run: `git add app/safety/__init__.py app/safety/keithley_limit_reconciliation.py tests/test_keithley_limit_reconciliation.py`

Run: `git commit -m "feat: reconcile dependent Keithley limits"`

### Task 2: Shared Fluent proposal dialog

**Files:**
- Modify: `app/ui/widgets/limit_field.py`
- Modify: `app/ui/widgets/__init__.py`
- Modify: `tests/test_fluent_dialogs.py`

**Interfaces:**
- `KeithleyLimitProposalDialog(proposal: KeithleyLimitProposal, parent: QWidget | None = None)` extends `StationDialog`.
- It returns `Accepted` only from an `acceptKeithleyLimitChanges` primary button; `cancelKeithleyLimitChanges` rejects it.

- [ ] **Step 1: Write failing rendering and cancel tests**

Create a three-change proposal, show the dialog, call `processEvents()`, and assert:

```python
self.assertTrue(dialog.isVisible())
self.assertGreater(dialog.geometry().width(), 430)
self.assertTrue(dialog.findChild(PrimaryPushButton, "acceptKeithleyLimitChanges").isVisible())
self.assertIn("10.05 mW", dialog.adjustments_text.toPlainText())
```

Repeat after sizing the parent to `820 x 560`; assert the dialog remains inside its parent and the list and buttons remain visible. Add a test that `reject()` yields `QDialog.DialogCode.Rejected`.

- [ ] **Step 2: Run the failing dialog test**

Run: `python -m pytest tests/test_fluent_dialogs.py -q`

Expected: import error for `KeithleyLimitProposalDialog`.

- [ ] **Step 3: Implement and export the dialog**

Place the dialog beside `LimitEditDialog`. Use `StrongBodyLabel`, `BodyLabel`, read-only `PlainTextEdit`, `PushButton`, and `PrimaryPushButton`; add a warning that accepting changes only stages the safety envelope and does not affect hardware until an explicit save. Render each row as `<path>: <previous> -> <proposed>\nReason: <reason>`. Use `setMinimumWidth(520)`, a bounded adjustment-list height, and object names specified above. Set the primary button text to `Accept {len(proposal.adjustments)} changes`. Export it in `app/ui/widgets/__init__.py`.

- [ ] **Step 4: Verify the widget slice**

Run: `python -m pytest tests/test_fluent_dialogs.py -q`

Run: `ruff check app/ui/widgets/limit_field.py tests/test_fluent_dialogs.py`

Expected: all dialog tests pass and ruff emits no diagnostics.

- [ ] **Step 5: Commit the modal**

Run: `git add app/ui/widgets/limit_field.py app/ui/widgets/__init__.py tests/test_fluent_dialogs.py`

Run: `git commit -m "feat: show Keithley limit change proposals"`

### Task 3: Fast staging of limit-only external snapshots

**Files:**
- Modify: `app/ui/settings_page.py`
- Modify: `tests/test_fluent_settings_page.py`

**Interfaces:**
- Add `SettingsPage.stage_limit_snapshot(settings: StationSettings, raw: dict[str, Any], changed_paths: set[tuple[str | int, ...]]) -> None`.
- It accepts only changed leaf paths containing `lab_limits`; arbitrary snapshots continue through `stage_external_snapshot`.

- [ ] **Step 1: Write a non-rebuild regression test**

Build a valid Channel B snapshot changing `source_current.max`, `source_current.max_abs`, `measured_current_trip.max`, and `max_abs_power`. Patch `page._populate` and `page._refresh_diagnostics`, call `stage_limit_snapshot`, then assert:

```python
populate.assert_not_called()
refresh_diagnostics.assert_not_called()
self.assertEqual(page._safety_limit_editors[source_max_path].text(), "150 mA")
self.assertEqual(page._safety_limit_editors[power_path].text(), "10.05 mW")
self.assertTrue(page._dirty)
```

Also assert a non-limit path raises `ValueError`.

- [ ] **Step 2: Run the failing focused test**

Run: `python -m pytest tests/test_fluent_settings_page.py -q`

Expected: attribute error for `stage_limit_snapshot`.

- [ ] **Step 3: Implement narrow in-place synchronization**

Set `_settings`, `_raw`, and `_dirty` from the validated snapshot. Under `_changing=True`, for each path update the matching hidden `limits_table` item and matching `_safety_limit_editors[path]`, then restore `_changing`. Call only `_update_subtitle()` and emit the existing unsaved-draft status. Do not call `_populate`, `_populate_forms`, `_populate_limits`, `_populate_roles`, or `_refresh_diagnostics`.

- [ ] **Step 4: Verify normal and narrow settings rendering**

Run: `python -m pytest tests/test_fluent_settings_page.py -q`

Expected: fast staging test plus existing normal desktop and narrow-window tests pass.

- [ ] **Step 5: Commit the latency foundation**

Run: `git add app/ui/settings_page.py tests/test_fluent_settings_page.py`

Run: `git commit -m "perf: stage safety limits without rebuilding settings"`

### Task 4: Integrate proposal acceptance in the device page and Settings editor

**Files:**
- Modify: `app/ui/shell/main_window.py`
- Modify: `app/ui/settings_page.py`
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_fluent_settings_page.py`

**Interfaces:**
- Device-page edits call the reconciler before `StationSettings.model_validate`, display the shared dialog, validate one final raw snapshot, calculate changed leaves with `SettingsPage._changed_leaf_paths`, and call `stage_limit_snapshot`.
- Settings-card edits use the same reconciler and dialog before changing the hidden table item.

- [ ] **Step 1: Write failing device-page accept and cancel tests**

Use `QTimer.singleShot` to accept the existing `LimitEditDialog` with Channel B current maximum `150 mA`, then accept `KeithleyLimitProposalDialog`. Before Save, assert repository YAML still has `10 mA`; assert staged paths are `150 mA`, `150 mA`, and `10.05 mW`; assert visible Keithley fields refresh in place. Patch `_populate` and `_refresh_diagnostics` and assert neither ran. In a second test reject the proposal and assert neither the staged draft nor displayed field changes.

- [ ] **Step 2: Write failing Settings-card accept and cancel tests**

Set the Channel B `source_current.max` safety-card editor to `150 mA`, complete editing, and accept the proposal. Assert the current-trip and power editors show proposal values. In the cancel case, assert the editor returns to the value in `_raw` and `_dirty` remains unchanged.

- [ ] **Step 3: Run the integration tests before implementation**

Run: `python -m pytest tests/test_main_window.py tests/test_fluent_settings_page.py -q`

Expected: new tests fail because no proposal dialog is shown and dependent leaves remain unchanged.

- [ ] **Step 4: Wire the two UI paths atomically**

In `MainWindow._edit_device_limit`, retain the first limit editor. For Keithley paths only, reconcile its candidate before model validation; if adjustments exist, display `KeithleyLimitProposalDialog`; apply all returned dependent leaves to the same raw copy only after acceptance; then validate once. On cancellation or `ConfigurationError`, stage nothing. Refresh every affected visible Keithley `LimitField` from validated settings instead of only the primary field.

In `SettingsPage._commit_safety_limit_editor`, create the Keithley candidate before calling `_sync_limit_from_tree`. Apply accepted primary and dependent leaves through the narrow synchronization helper; on cancellation restore the line edit from `_raw`. Keep existing behavior for Rigol, Anritsu, and unrelated settings rows.

- [ ] **Step 5: Verify UI, safety, and lint integration**

Run: `python -m pytest tests/test_main_window.py tests/test_fluent_settings_page.py tests/test_fluent_dialogs.py tests/test_keithley_limit_reconciliation.py tests/test_settings_and_safety.py -q`

Run: `ruff check app tests`

Expected: acceptance/cancellation, all quantity and safety rules, and Fluent rendering all pass without ruff diagnostics.

- [ ] **Step 6: Commit vertical integration**

Run: `git add app/ui/shell/main_window.py app/ui/settings_page.py tests/test_main_window.py tests/test_fluent_settings_page.py`

Run: `git commit -m "feat: reconcile Keithley limit edits before staging"`

### Task 5: Completion verification

**Files:**
- Test: `tests/test_main_window.py`, `tests/test_fluent_settings_page.py`, `tests/test_fluent_dialogs.py`, `tests/test_keithley_limit_reconciliation.py`, `tests/test_settings_and_safety.py`, `tests/test_adapters_and_runner.py`

- [ ] **Step 1: Add a no-persistence/no-device-effect cancellation test**

Reject the proposal, invoke Save settings, assert affected YAML leaves remain unchanged, and assert Keithley controller mocks receive no `apply_limit_settings` call before Save.

- [ ] **Step 2: Run the complete relevant suite**

Run: `python -m pytest tests/test_main_window.py tests/test_fluent_settings_page.py tests/test_fluent_dialogs.py tests/test_keithley_limit_reconciliation.py tests/test_settings_and_safety.py tests/test_adapters_and_runner.py -q`

Run: `ruff check app tests`

Run: `git diff --check`

Expected: all selected tests pass, ruff reports no diagnostics, and diff check is clean.

- [ ] **Step 3: Inspect scope and commit only feature files**

Run: `git status --short`

Confirm the two user-owned heatmap files are unstaged. Stage and commit only the feature files if a verification-only correction was necessary, using `git commit -m "test: cover Keithley limit reconciliation workflow"`.

