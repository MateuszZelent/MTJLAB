# Heatmap Coordinate Explorer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render result heatmaps against selectable physical sweep coordinates instead of a positional checkpoint index.

**Architecture:** Add a read-only coordinate model that aligns one physical coordinate vector to every spectral checkpoint.  The heatmap payload builder consumes this model together with selected X/Y dimensions and exact filters, and returns both a colour matrix and a cell-to-checkpoint matrix.  The Fluent heatmap toolbar chooses axes and filters without changing any HDF5 schema or measurement data.

**Tech Stack:** Python 3.14, PySide6, PySide6-Fluent-Widgets, pyqtgraph, NumPy, thaTEC HDF5 readers, pytest.

## Global Constraints

- Read only persisted result data; never rewrite or infer a measurement value.
- Preserve SI coordinate values internally and existing canonical units at all public boundaries.
- Keep dBm and dB variants distinct; never average, interpolate, or combine duplicate cells.
- Keep the Result page Fluent-native and verify visible geometry after `show()`.
- Leave hardware, adapters, recipes, writer schemas, and user-owned worktree changes untouched.

---

### Task 1: Coordinate model and grid builder

**Files:**
- Create: `app/ui/results/heatmap_coordinates.py`
- Modify: `app/ui/results/heatmap_tab.py`
- Test: `tests/test_heatmap_coordinates.py`

**Interfaces:**
- Consumes: `Path`, `ThatecRun`, `ThatecRow`, `ThatecRunReader.spectrum_slice`, and optional `tuple[StoredPoint, ...]`.
- Produces: immutable `HeatmapDimension`, `HeatmapCoordinates`, `HeatmapRequest`, and `HeatmapMatrix` models, plus `build_heatmap_coordinates()` and `read_heatmap_matrix()`.

- [ ] **Step 1: Write failing tests for one physical sweep and two nested sweeps**

```python
def test_coordinates_prefer_aligned_public_scalar_sweep_axis():
    coordinates = build_heatmap_coordinates(path, run, spectral_row)
    assert coordinates.dimensions[0].id == "frequency"
    assert coordinates.dimensions[1].label == "Keithley B current"
    assert coordinates.dimensions[1].values == (0.0, 0.001, 0.002)

def test_matrix_rejects_duplicate_cells_without_averaging():
    with pytest.raises(ValueError, match="multiple checkpoints"):
        read_heatmap_matrix(path, coordinates, request)
```

- [ ] **Step 2: Run the coordinate tests and verify they fail because the module is absent**

Run: `pytest tests/test_heatmap_coordinates.py -q`

Expected: import/collection failure for `app.ui.results.heatmap_coordinates`.

- [ ] **Step 3: Implement immutable coordinate and matrix models**

```python
@dataclass(frozen=True, slots=True)
class HeatmapDimension:
    id: str
    label: str
    unit: str
    values: tuple[float, ...]
    is_frequency: bool = False

@dataclass(frozen=True, slots=True)
class HeatmapMatrix:
    values: np.ndarray
    x_values: np.ndarray
    y_values: np.ndarray
    cell_checkpoints: np.ndarray
```

Build checkpoint-aligned physical dimensions from matching public scalar-control rows; use private setpoints when provided, then validated `ThatecSchemaMapper` reconstruction, then the labelled checkpoint fallback.  Implement both frequency-on-axis and frequency-filtered planes, returning gaps as NaN and rejecting duplicate cells.

- [ ] **Step 4: Run coordinate tests and verify they pass**

Run: `pytest tests/test_heatmap_coordinates.py -q`

Expected: PASS, including raw/processed unit preservation and duplicate rejection.

### Task 2: Interactive plot mapping and Fluent controls

**Files:**
- Modify: `app/ui/results/heatmap_tab.py`
- Modify: `app/ui/results/page.py`
- Test: `tests/test_results_browser.py`
- Test: `tests/test_fluent_results_theme.py`

**Interfaces:**
- Consumes: `HeatmapCoordinates`, `HeatmapRequest`, and `HeatmapMatrix` from Task 1.
- Produces: a visible X-axis selector, Y-axis selector, exact filter controls, and per-cell checkpoint click routing.

- [ ] **Step 1: Write failing rendered-result tests**

```python
def test_heatmap_uses_current_b_as_default_y_axis_and_clicks_its_checkpoint():
    tab.load(path, run, points)
    assert tab.x_axis_combo.currentData() == "frequency"
    assert tab.y_axis_combo.currentData() == "keithley.B.current"
    assert tab.heatmap.plot.getAxis("left").label.toPlainText().strip() == "Keithley B current (A)"

def test_heatmap_can_swap_frequency_to_y_and_filter_an_outer_sweep():
    tab.x_axis_combo.setCurrentIndex(tab.x_axis_combo.findData("rigol.1.frequency"))
    tab.y_axis_combo.setCurrentIndex(tab.y_axis_combo.findData("frequency"))
    tab.load_heatmap_for_row(str(tab.row_combo.currentData()))
    assert tab.heatmap._cell_checkpoint_indices[0, 0] == expected_checkpoint
```

- [ ] **Step 2: Run the rendered-result tests and verify they fail because axis controls do not exist**

Run: `pytest tests/test_results_browser.py -k heatmap -q`

Expected: FAIL with missing `x_axis_combo` or `y_axis_combo`.

- [ ] **Step 3: Implement controls and request lifecycle**

Add X/Y `ComboBox` controls before the row chooser; keep their choices distinct and rebuild filter `ComboBox` controls from non-axis dimensions. Pass private points from `ResultsPage._on_file_selected()` to `HeatmapResultsTab.load()`. Invalidate background tasks on every axis/filter/variant change and disable the load action while a read is active.

Extend `HeatmapPlotWidget.set_data()` with an optional `cell_checkpoint_indices` matrix. Use the nearest selected cell in `_mouse_clicked()` and emit only its valid source checkpoint.

- [ ] **Step 4: Run rendered-result and theme tests and verify they pass**

Run: `pytest tests/test_results_browser.py tests/test_fluent_results_theme.py -q`

Expected: PASS, with visible controls and non-zero geometry after `show()`.

### Task 3: Compatibility, negative paths, and final verification

**Files:**
- Modify: `tests/test_heatmap_coordinates.py`
- Modify: `tests/test_results_browser.py`

**Interfaces:**
- Consumes: completed coordinate builder and Fluent tab from Tasks 1–2.
- Produces: regression coverage for fallback, incompatible grids, missing filter combinations, raw dBm, and processed dB.

- [ ] **Step 1: Write failing tests for public-only fallback and incomplete Cartesian selections**

```python
def test_unproven_coordinates_expose_checkpoint_fallback_reason():
    coordinates = build_heatmap_coordinates(path, run, spectrum_row)
    assert coordinates.fallback_reason

def test_missing_cartesian_cell_is_a_gap_not_an_aggregate():
    matrix = read_heatmap_matrix(path, coordinates, request)
    assert np.isnan(matrix.values[missing_y, missing_x])
```

- [ ] **Step 2: Run them and verify the intended failing behavior**

Run: `pytest tests/test_heatmap_coordinates.py -q`

Expected: FAIL until explicit fallback reason and gap mapping are implemented.

- [ ] **Step 3: Complete error text and readout unit handling**

Report the missing coordinate/filter condition in the state card, retain all valid cells, and include X/Y/Z units in the live readout. Do not add fallback aggregation.

- [ ] **Step 4: Run final checks**

Run: `pytest tests/test_heatmap_coordinates.py tests/test_results_browser.py tests/test_fluent_results_theme.py -q`

Run: `ruff check app/ui/results/heatmap_coordinates.py app/ui/results/heatmap_tab.py app/ui/results/page.py tests/test_heatmap_coordinates.py tests/test_results_browser.py tests/test_fluent_results_theme.py`

Expected: all selected tests pass and ruff reports no diagnostics.
