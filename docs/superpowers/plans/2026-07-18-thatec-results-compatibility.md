# THATEC Results Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open real and generated THATEC HDF5 results through one lazy public reader and present their complete public tree in Results.

**Architecture:** Add a `ThatecRunReader` that consumes only public THATEC groups and exposes typed hierarchy, row, device, labbook, and post-process records. Replace Results' dependence on the private `Hdf5RunReader` with this reader, retaining private groups solely as optional enrichment.

**Tech Stack:** Python 3, h5py, PySide6, PyThat, unittest.

## Global Constraints

- `/scan_definition`, `/measurement`, `/devices`, `/labbook`, and `/post-process` are the public contract.
- Never require `/run`, `/points`, `/spectra`, or `/events` to open a file.
- Do not load a full two-dimensional measurement matrix until a user selects a checkpoint.
- Results access is read-only.

---

### Task 1: Public THATEC read model

**Files:**
- Create: `app/storage/thatec_reader.py`
- Modify: `app/storage/__init__.py`
- Test: `tests/test_thatec_reader.py`

**Interfaces:**
- Produces `ThatecRunReader.describe(path) -> ThatecRun`, `tree(path) -> tuple[ThatecTreeNode, ...]`, `row(path, row_id) -> ThatecRow`, and `row_slice(path, row_id, checkpoint) -> ThatecRowData`.

- [ ] **Step 1: Write failing tests for the reference THATEC file**

```python
run = ThatecRunReader.describe(REFERENCE_FILE)
assert run.rows["row_07"].shape == (5050, 10001)
assert {device.name for device in run.devices} == {
    "Anritsu MS269Xa MS2830A SpectrumAnalyzer",
    "Keithley 2614B\nSourcemeter",
    "MOKE-Box\nField Control",
}
assert ThatecRunReader.row_slice(REFERENCE_FILE, "row_07", 0).values.shape == (10001,)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest -q tests/test_thatec_reader.py -k reference`

Expected: FAIL because `ThatecRunReader` does not exist.

- [ ] **Step 3: Implement lazy public parsing**

```python
class ThatecRunReader:
    @staticmethod
    def describe(path: str | Path) -> ThatecRun:
        with h5py.File(path, "r") as h5:
            return _describe_public_thatec(h5, Path(path))

    @staticmethod
    def row_slice(path: str | Path, row_id: str, checkpoint: int) -> ThatecRowData:
        with h5py.File(path, "r") as h5:
            data = h5[f"measurement/{row_id}/data"]
            values = data[checkpoint] if data.ndim == 2 else data[:]
            return ThatecRowData(row_id=row_id, checkpoint=checkpoint, values=tuple(float(v) for v in values))
```

Parse two-column string datasets as key/value records, decode `tree_view`, and retain only row descriptors at `describe` time.

- [ ] **Step 4: Run focused reader tests and verify GREEN**

Run: `python -m pytest -q tests/test_thatec_reader.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/storage/thatec_reader.py app/storage/__init__.py tests/test_thatec_reader.py
git commit -m "feat: read public THATEC result trees"
```

### Task 2: Generated files are independently readable through THATEC

**Files:**
- Modify: `app/storage/thatec_writer.py`
- Modify: `app/storage/hdf5_writer.py`
- Modify: `tests/test_hdf5_writer.py`
- Modify: `tests/test_thatec_reader.py`

**Interfaces:**
- Consumes `ThatecRunReader.describe` from Task 1.
- Produces public THATEC rows, device records, and labbook entries sufficient for `ThatecRunReader` without private groups.

- [ ] **Step 1: Write a failing writer compatibility test**

```python
with h5py.File(path, "r+") as h5:
    del h5["run"]
    del h5["points"]
    del h5["spectra"]
run = ThatecRunReader.describe(path)
assert run.rows
assert run.devices
assert ThatecRunReader.row_slice(path, run.spectrum_rows[0].id, 0).values
```

- [ ] **Step 2: Run it and verify RED**

Run: `python -m pytest -q tests/test_hdf5_writer.py -k public_thatec`

Expected: FAIL if public tree/device metadata lacks information required by the reader.

- [ ] **Step 3: Make public writer metadata complete**

Ensure `ThatecHdf5Writer` emits key/value device records, labbook metadata, row definitions, `tree_view`, row timestamps, spectrum scale and spectrum metadata that the public reader consumes. Do not change the semantic meaning of existing THATEC rows; only add missing public data.

- [ ] **Step 4: Verify public-only and PyThat paths**

Run: `python -m pytest -q tests/test_hdf5_writer.py tests/test_thatec_validator.py`

Expected: PASS; the public-only reader test and existing PyThat validation both pass.

- [ ] **Step 5: Commit**

```bash
git add app/storage/thatec_writer.py app/storage/hdf5_writer.py tests/test_hdf5_writer.py tests/test_thatec_reader.py
git commit -m "feat: make generated results self-describing THATEC files"
```

### Task 3: Tree-driven Results Explorer

**Files:**
- Modify: `app/ui/results/page.py`
- Create: `app/ui/results/thatec_model.py`
- Modify: `tests/test_results_page.py`

**Interfaces:**
- Consumes `ThatecRunReader.describe`, `tree`, and `row_slice`.
- Produces a Results page with `experiment_tree`, `data_table`, `inspector`, and existing spectrum plot.

- [ ] **Step 1: Write failing UI navigation tests**

```python
page = ResultsPage(str(reference_directory))
page.select_run(REFERENCE_FILE)
assert page.experiment_tree.topLevelItem(0).text(0) == "Measurements"
page.select_tree_node("row_07")
assert page.spectrum_plot.trace_point_count("Selected THATEC spectrum") == 10001
assert "device name" in page.inspector.toPlainText()
```

- [ ] **Step 2: Run focused UI tests and verify RED**

Run: `python -m pytest -q tests/test_results_page.py -k thatec_tree`

Expected: FAIL because Results has no `experiment_tree` or THATEC-node selection.

- [ ] **Step 3: Implement the three-pane inspector**

```python
def _tree_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
    node = item.data(0, Qt.ItemDataRole.UserRole) if item else None
    if isinstance(node, ThatecRow):
        self._show_row(node)
    elif isinstance(node, ThatecRecord):
        self.inspector.setPlainText(self._format_key_values(node.values))
```

Populate the tree from public `tree_view`; add fixed public sections for Devices, Labbook, and Post-process when present. `_show_row` uses only one scalar vector or one matrix checkpoint at a time, and places row definition, shape, unit, timestamp and metadata in the inspector.

- [ ] **Step 4: Verify Results against real and generated artifacts**

Run: `python -m pytest -q tests/test_results_page.py`

Expected: PASS, including reference-file navigation and current generated-file browsing.

- [ ] **Step 5: Commit**

```bash
git add app/ui/results/page.py app/ui/results/thatec_model.py tests/test_results_page.py
git commit -m "feat: browse THATEC experiment trees in Results"
```

### Task 4: End-to-end compatibility audit

**Files:**
- Modify: `tests/test_simulated_run.py`
- Modify: `docs/HIL_QUALIFICATION.md`

- [ ] **Step 1: Write a failing end-to-end assertion**

```python
run = ThatecRunReader.describe(simulation_h5)
assert run.tree
assert run.spectrum_rows
assert all(row.timestamp_count == 1 for row in run.rows.values())
```

- [ ] **Step 2: Run it and verify RED**

Run: `python -m pytest -q tests/test_simulated_run.py -k thatec_public`

Expected: FAIL until Tasks 1–3 provide the public reader and UI-compatible output.

- [ ] **Step 3: Document the operational check**

Add the exact procedure: generate a simulation, open it in Results, expand every public tree section, select the spectrum row, and confirm device/labbook/parameter records are visible.

- [ ] **Step 4: Run complete verification**

Run: `python -m pytest -q tests/test_thatec_reader.py tests/test_hdf5_writer.py tests/test_thatec_validator.py tests/test_results_page.py tests/test_simulated_run.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_simulated_run.py docs/HIL_QUALIFICATION.md
git commit -m "test: qualify THATEC results interoperability"
```
