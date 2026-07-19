# Find VISA Layout Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair Find VISA cell-widget regressions and make its result table fill the available vertical workspace.

**Architecture:** Preserve the existing Dashboard API and discovery flow. Remove structural cell wrappers, insert the established widget types directly, and let the result table own all remaining vertical space below the status label.

**Tech Stack:** Python 3.14, PySide6, unittest/pytest.

## Global Constraints

- Do not change VISA scanning, identification, assignment persistence, or permissions.
- Keep direct `QComboBox`, `QPushButton`, and `QLabel` cell widgets.
- Keep the current uncommitted Dashboard work outside the focused repair intact.
- Use test-first development.

---

### Task 1: Lock the responsive layout contract

**Files:**
- Modify: `tests/test_main_window.py`
- Modify: `app/ui/dashboard/page.py`

**Interfaces:**
- Consumes: `DashboardPage.discovery_table`.
- Produces: an uncapped table with positive vertical layout stretch.

- [ ] **Step 1: Write the failing test**

Add to `MainWindowTests`:

```python
def test_find_visa_table_fills_remaining_tab_height(self) -> None:
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        page = window.dashboard
        discovery = page.workspace.widget(1)
        layout = discovery.layout()
        table_index = layout.indexOf(page.discovery_table)
        self.assertGreaterEqual(table_index, 0)
        self.assertGreater(layout.stretch(table_index), 0)
        self.assertEqual(
            page.discovery_table.maximumHeight(),
            QWIDGETSIZE_MAX,
        )
        header = page.discovery_table.horizontalHeader()
        self.assertEqual(
            header.sectionResizeMode(2),
            QHeaderView.ResizeMode.ResizeToContents,
        )
    finally:
        window.close()
```

Import `QWIDGETSIZE_MAX` from `PySide6.QtWidgets` and retain the existing
`QHeaderView` import.

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m pytest tests/test_main_window.py::MainWindowTests::test_find_visa_table_fills_remaining_tab_height -q
```

Expected: FAIL because table stretch is zero and maximum height is 400.

- [ ] **Step 3: Implement the minimal layout correction**

In `DashboardPage.__init__`, configure all resize modes explicitly:

```python
header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
self.discovery_table.setMinimumHeight(190)
discovery_layout.addWidget(self.discovery_table, 1)
```

Remove `setMaximumHeight(400)` and do not add a trailing stretch.

- [ ] **Step 4: Verify GREEN**

Run:

```powershell
python -m pytest tests/test_main_window.py::MainWindowTests::test_find_visa_table_fills_remaining_tab_height -q
```

Expected: PASS.

### Task 2: Restore direct discovery cell widgets

**Files:**
- Modify: `app/ui/dashboard/page.py`
- Verify: `tests/test_main_window.py`

**Interfaces:**
- Consumes: existing `_scan_completed` and `_set_row_assigned`.
- Produces: direct cell widgets compatible with interaction and tests.

- [ ] **Step 1: Verify existing tests are RED**

Run:

```powershell
python -m pytest tests/test_main_window.py -q -k "discovery_row_assign_button_emits_selected_resource_immediately or discovery_marks_persisted_resource_assigned_and_disables_duplicate"
```

Expected: two FAILs because `cellWidget` returns wrapper `QWidget` instances.

- [ ] **Step 2: Remove wrapper usage**

Delete `_padded_cell`. In `_scan_completed`, use:

```python
self.discovery_table.setCellWidget(row, 0, assignment)
self.discovery_table.setCellWidget(row, 1, assign_button)
```

In `_set_row_assigned`, use:

```python
self.discovery_table.setCellWidget(row, 0, badge)
self.discovery_table.setCellWidget(row, 1, button)
```

Keep minimum widths, accessible names, tooltips, enabled states, object names,
button text, and signals unchanged.

- [ ] **Step 3: Verify GREEN**

Run:

```powershell
python -m pytest tests/test_main_window.py -q -k "discovery_row_assign_button_emits_selected_resource_immediately or discovery_marks_persisted_resource_assigned_and_disables_duplicate"
```

Expected: `2 passed`.

- [ ] **Step 4: Run focused Dashboard verification**

Run:

```powershell
python -m pytest tests/test_main_window.py -q -k "discovery or dashboard or visa"
python -m pytest tests/test_architecture.py tests/test_device_modules.py -q
python -m compileall -q app/ui/dashboard tests/test_main_window.py
git diff --check
```

Expected: all selected tests PASS, compilation succeeds, and no whitespace
errors are reported.

- [ ] **Step 5: Commit only the focused repair**

Stage only the relevant hunks in `app/ui/dashboard/page.py` and
`tests/test_main_window.py`, preserving unrelated uncommitted hunks:

```powershell
git diff -- app/ui/dashboard/page.py tests/test_main_window.py
git add -p app/ui/dashboard/page.py tests/test_main_window.py
git commit -m "fix: restore responsive Find VISA workspace"
```
