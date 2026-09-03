# Unified Sweep and Execution Tree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the duplicated item-based Sweeps/Execution projections with one Fluent semantic measurement tree that exposes every per-point ROI set operation, supports nested multi-device and same-device axes, and remains responsive during long simulated or physical sweeps.

**Architecture:** Normalize legacy and explicit sweep syntax into one immutable semantic recipe graph, then render it through `qfluentwidgets.TreeView` backed by a custom `QAbstractItemModel`. Device-owned sweep providers compile axis points into existing validated adapter operations; stable semantic IDs connect the Builder tree, flat execution plan, Runner events, live requested/applied state, and additive HDF5 provenance without node-ID prefix inference.

**Tech Stack:** Python 3.12, PySide6>=6.7, PySide6-Fluent-Widgets==1.11.2, ruamel.yaml, h5py, NumPy, pytest/unittest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-unified-sweep-execution-tree-design.md`

## Global Constraints

- Work on `master` as requested; preserve unrelated and pre-existing uncommitted changes.
- Use `qfluentwidgets.TreeView`, which inherits `QTreeView`; do not implement the new tree with raw `QTreeView`, `TreeWidget`, `QTreeWidget`, or `QTreeWidgetItem`.
- Preserve the Fluent-native shell and reuse `ThemeTokens`; do not add a legacy shell, compatibility facade, or per-page hardcoded colour system.
- Keep device parameter identity in `app/recipes/parameter_registry.py` and device-specific sweep validation/compilation in registered device providers.
- Keep all physical quantities explicit, dimension-checked, and normalized to SI exactly once at the domain/compiler boundary.
- Do not infer applied values or OUTPUT state from requested values or UI state; show applied/readback only after Runner confirmation or labelled simulated acknowledgement.
- Do not change hardware command order, output continuity, compliance handling, watchdogs, cancellation, retry restrictions, or guaranteed shutdown without focused safety tests.
- Existing schema-version-1 recipe source and HDF5 readers remain compatible; new axis provenance is additive.
- Sweeps is migrated before Execution. Execution may not introduce a second semantic tree implementation.
- Runtime GUI updates may update model state only; they may not rebuild, clear, clone, expand, or resize the complete tree per event.
- Safety, fault, and shutdown events bypass presentation coalescing.
- UI tests must call `show()`, process Qt events, and verify geometry at 1440×900 and a narrow 1024×720 layout.
- The deterministic 1000-point simulated qualification must keep the GUI timer gap below 250 ms on the qualification workstation.

---

## File map

### New domain and provider files

- `app/recipes/semantic_tree.py` — immutable axis, point-context, and semantic-tree models plus legacy normalization.
- `app/contracts/sweep_provider.py` — device-independent provider protocol and compiled setpoint description.
- `app/devices/keithley_2600/sweep_provider.py` — Keithley parameter binding, point validation, and update compilation.
- `app/devices/rigol_dg1000z/sweep_provider.py` — Rigol binding and frequency/level point compilation.
- `app/devices/anritsu_ms2830a/sweep_provider.py` — Anritsu spectrum/SG binding and point compilation.
- `app/domain/execution_state.py` — immutable semantic operation and live-axis presentation state.

### New shared Fluent tree files

- `app/ui/measurement_tree/__init__.py` — public exports.
- `app/ui/measurement_tree/model.py` — `MeasurementTreeModel(QAbstractItemModel)` and stable roles.
- `app/ui/measurement_tree/view.py` — `MeasurementTreeView(qfluentwidgets.TreeView)` and bounded active-row following.
- `app/ui/measurement_tree/delegate.py` — restrained active-loop spine and semantic status rendering on top of the Fluent delegate.

### Existing files to modify

- `app/recipes/models.py` — accept and validate canonical explicit-axis binding fields without dropping v1 input.
- `app/recipes/editing.py` — atomic axis extraction/insertion/move operations and canonical serialization.
- `app/recipes/parameter_registry.py` — canonical target aliases only where required by provider binding.
- `app/contracts/device_module.py` — attach the optional `DeviceSweepProvider` to `RecipeExtension`.
- `app/contracts/__init__.py` — export provider contracts.
- `app/devices/*/module.py` — register each device sweep provider.
- `app/engine/compiler.py` — compile normalized explicit axes and emit semantic metadata.
- `app/engine/runner.py` — emit axis-operation lifecycle and confirmed-value events.
- `app/engine/estimation.py` and `app/engine/policy.py` — recognize semantic point operations without changing physical deadlines.
- `app/ui/recipes/common_dialogs.py` — remove item-based recipe tree ownership after migration; retain unrelated dialogs.
- `app/ui/recipes/page.py` — host the shared Fluent tree/model and move recipe editing to semantic IDs.
- `app/ui/execution/page.py` — consume the same semantic snapshot and update model roles only.
- `app/ui/shell/main_window.py` — replace multi-queue widget mutation with one typed presentation-state buffer.
- `app/ui/run_worker.py` — preserve complete Runner events while coalescing presentation state.
- `app/ui/workers.py` — pass the composed device registry into asynchronous preflight compilation.
- `app/storage/hdf5_writer.py` and `app/storage/thatec_writer.py` — add axis provenance without changing existing point/setpoint fields.
- `app/engine/recovery.py` — carry semantic plan identity through resume without making UI state authoritative.

### Tests to create or extend

- Create `tests/test_recipe_semantic_tree.py`.
- Create `tests/test_measurement_tree_model.py`.
- Create `tests/test_sweep_provider_contract.py`.
- Create `tests/test_execution_ui_responsiveness.py`.
- Modify `tests/test_recipe_builder.py`.
- Modify `tests/test_recipe_compiler.py`.
- Modify `tests/test_adapters_and_runner.py`.
- Modify `tests/test_fluent_recipe_execution_pages.py`.
- Modify `tests/test_hdf5_writer.py`.
- Modify `tests/test_run_recovery.py`.
- Modify `tests/test_simulated_run.py`.

---

### Task 1: Characterize current tree semantics and GUI stalls

**Files:**

- Create: `tests/test_execution_ui_responsiveness.py`
- Modify: `tests/test_recipe_builder.py`
- Modify: `tests/test_fluent_recipe_execution_pages.py`

**Interfaces:**

- Consumes: current `RecipePage`, `RunMonitorPage`, compiler plan, and simulated run events.
- Produces: red characterization tests for one axis, two-device nesting, same-device double axis, and GUI event-loop responsiveness.

- [ ] **Step 1: Add a helper that records Qt event-loop gaps**

```python
class GuiGapProbe(QObject):
    def __init__(self, interval_ms: int = 20) -> None:
        super().__init__()
        self._last = time.monotonic()
        self.maximum_gap_s = 0.0
        self.ticks = 0
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        now = time.monotonic()
        self.maximum_gap_s = max(self.maximum_gap_s, now - self._last)
        self._last = now
        self.ticks += 1
```

- [ ] **Step 2: Add RED tests for the required semantic rows**

```python
def test_builder_projects_one_axis_with_one_set_roi_operation(qtbot) -> None:
    page = build_recipe_page(SINGLE_AXIS_SOURCE)
    page.show()
    QApplication.processEvents()
    assert semantic_labels(page) == [
        "Measurement sequence",
        "Keithley B · configuration",
        "Sweep axis · Source current",
        "For each source-current point",
        "Set ROI value · Keithley B · source current",
        "Acquire spectrum · Anritsu",
        "Wait · 2 s",
        "Finally — safe shutdown",
    ]

def test_same_device_nested_axes_are_not_rejected() -> None:
    plan = compile_source(SAME_DEVICE_TWO_AXIS_SOURCE)
    assert plan.total_points == 6
```

- [ ] **Step 3: Add a RED simulated responsiveness test**

```python
@pytest.mark.qualification
def test_execution_tree_keeps_qt_event_loop_live_for_1000_points(qtbot) -> None:
    window = build_simulated_window(SIMULATED_10_BY_100_SOURCE, seed=17)
    probe = GuiGapProbe()
    window.show()
    probe.timer.start()
    start_and_wait_for_run(window, expected_points=1000)
    assert probe.ticks > 20
    assert probe.maximum_gap_s < 0.250
```

- [ ] **Step 4: Run the characterization tests**

Run: `python -m pytest -q tests/test_recipe_builder.py tests/test_fluent_recipe_execution_pages.py tests/test_execution_ui_responsiveness.py -k "semantic or same_device_nested or event_loop_live"`

Expected: semantic and same-device tests fail against the current projection; responsiveness records the current baseline and may fail the 250 ms gate.

- [ ] **Step 5: Commit the characterization contract**

```bash
git add tests/test_recipe_builder.py tests/test_fluent_recipe_execution_pages.py tests/test_execution_ui_responsiveness.py
git commit -m "test: define unified sweep tree contract"
```

### Task 2: Add the immutable semantic recipe graph and legacy normalizer

**Files:**

- Create: `app/recipes/semantic_tree.py`
- Modify: `app/recipes/__init__.py`
- Modify: `app/recipes/models.py`
- Create: `tests/test_recipe_semantic_tree.py`

**Interfaces:**

- Produces: `SweepBindingDraft`, `SweepStageSpec`, `SweepAxisBinding`, `AxisPointContext`, `SemanticNodeKind`, `SemanticTreeNode`, `SemanticMeasurementTree`, `AxisBindingResolver`, and `normalize_recipe_tree(recipe, resolvers)`.
- Consumes: `Recipe`, `RecipeNode`, `Quantity`, central parameter descriptors, and a mapping of structural `AxisBindingResolver` implementations. Task 2 tests use deterministic fake resolvers; the registered device providers from Task 3 satisfy the same protocol.

- [ ] **Step 1: Write RED tests for deterministic normalization**

```python
def test_legacy_device_sweep_normalizes_to_one_axis_and_loop() -> None:
    tree = normalize_recipe_tree(parse_recipe_text(LEGACY_KEITHLEY_SWEEP), providers())
    axis = tree.require("keithley-b.axis.source-level")
    assert axis.kind is SemanticNodeKind.SWEEP_AXIS
    assert axis.axis.target == "keithley.B.current"
    assert [child.kind for child in axis.children] == [SemanticNodeKind.LOOP_BODY]
    assert axis.children[0].children[0].kind is SemanticNodeKind.SET_ROI_VALUE

def test_shared_stage_boundary_is_deduplicated_once() -> None:
    axis = normalize_axis(TWO_STAGE_AXIS)
    assert tuple(point.si_value for point in axis.points).count(0.005) == 1
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_recipe_semantic_tree.py`

Expected: FAIL because the semantic types and normalizer do not exist.

- [ ] **Step 3: Implement the immutable types and index**

```python
class SemanticNodeKind(StrEnum):
    SEQUENCE = "sequence"
    DEVICE = "device"
    SWEEP_AXIS = "sweep_axis"
    LOOP_BODY = "loop_body"
    SET_ROI_VALUE = "set_roi_value"
    ACTION = "action"
    FINALLY = "finally"
    GENERATED_SAFETY = "generated_safety"

class AxisBindingResolver(Protocol):
    module_key: str

    def bind_legacy_action(
        self,
        node: RecipeNode,
        action: Mapping[str, object],
    ) -> SweepBindingDraft:
        ...

@dataclass(frozen=True, slots=True)
class SemanticMeasurementTree:
    roots: tuple[SemanticTreeNode, ...]
    by_id: Mapping[str, SemanticTreeNode]

    def require(self, semantic_id: str) -> SemanticTreeNode:
        try:
            return self.by_id[semantic_id]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown semantic node {semantic_id!r}.") from exc
```

Normalization must reject duplicate semantic IDs, unknown targets, dimension
mismatches, empty stages, duplicate active bindings to one target, and ambiguous
multiple legacy local sweeps. It must retain `Recipe.source_text` unchanged.

- [ ] **Step 4: Add canonical explicit-axis parsing fields**

Allow a `sweep` node to carry:

```yaml
binding:
  owner_node_id: keithley-b
  device_module: keithley
  endpoint: B
  parameter_id: source.level
```

Validate the mapping shape and non-empty strings in `models.py`; target and
dimension authority remain in the registry/provider.

- [ ] **Step 5: Run semantic and existing parser tests**

Run: `python -m pytest -q tests/test_recipe_semantic_tree.py tests/test_recipe_compiler.py tests/test_sweep_points.py`

Expected: PASS; source text and existing schema-version-1 parsing remain unchanged.

- [ ] **Step 6: Commit the semantic graph**

```bash
git add app/recipes/semantic_tree.py app/recipes/__init__.py app/recipes/models.py tests/test_recipe_semantic_tree.py
git commit -m "feat: add semantic sweep tree normalization"
```

### Task 3: Register device-owned sweep providers

**Files:**

- Create: `app/contracts/sweep_provider.py`
- Modify: `app/contracts/device_module.py`
- Modify: `app/contracts/__init__.py`
- Create: `app/devices/keithley_2600/sweep_provider.py`
- Create: `app/devices/rigol_dg1000z/sweep_provider.py`
- Create: `app/devices/anritsu_ms2830a/sweep_provider.py`
- Modify: `app/devices/keithley_2600/module.py`
- Modify: `app/devices/rigol_dg1000z/module.py`
- Modify: `app/devices/anritsu_ms2830a/module.py`
- Create: `tests/test_sweep_provider_contract.py`

**Interfaces:**

- Consumes: `SweepBindingDraft`, `SweepAxisBinding`, and the structural `AxisBindingResolver` protocol from Task 2.
- Produces: `DeviceSweepProvider` and `CompiledAxisSetpoint`; every provider structurally satisfies `AxisBindingResolver`.
- `RecipeExtension.sweep_provider: DeviceSweepProvider | None` is the only registration path.
- Providers validate/compile values but never access QWidget, VISA, or storage.

- [ ] **Step 1: Write RED provider-contract tests**

```python
@pytest.mark.parametrize("module_key", ["keithley", "rigol", "anritsu"])
def test_registered_recipe_module_owns_its_sweep_provider(module_key: str) -> None:
    extension = registry().get(module_key).recipe_extension
    assert extension is not None
    assert extension.sweep_provider is not None

def test_keithley_provider_compiles_current_and_compliance_independently() -> None:
    provider = keithley_provider()
    current = provider.compile_point(current_binding(), parse_quantity("2 mA", "current"), context())
    compliance = provider.compile_point(compliance_binding(), parse_quantity("1 V", "voltage"), context())
    assert current.action_kind == "update_keithley_level"
    assert compliance.action_kind == "update_keithley_compliance"
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_sweep_provider_contract.py`

Expected: FAIL because the provider contract is not registered.

- [ ] **Step 3: Implement the provider types**

```python
@dataclass(frozen=True, slots=True)
class CompiledAxisSetpoint:
    action_kind: str
    payload: Mapping[str, object]
    requested_si: float
    applied_si: float
    verification_field: str

class DeviceSweepProvider(Protocol):
    module_key: str

    def bind_legacy_action(self, node: RecipeNode, action: Mapping[str, object]) -> SweepBindingDraft: ...
    def validate_binding(self, node: RecipeNode, binding: SweepAxisBinding) -> None: ...
    def compile_point(
        self,
        node: RecipeNode,
        binding: SweepAxisBinding,
        value: Quantity,
        context: Mapping[str, Quantity],
        settings: StationSettings,
    ) -> CompiledAxisSetpoint: ...
```

Add `DeviceModuleRegistry.sweep_providers() -> Mapping[str, DeviceSweepProvider]`.
It returns only enabled recipe extensions with a provider and rejects a provider
whose `module_key` differs from its module manifest key.

- [ ] **Step 4: Move device-specific axis maps out of UI/compiler conditionals**

Keithley owns source level, compliance, and settling-time binding. Rigol owns
frequency and level-pair transformations. Anritsu owns spectrum and SG point
configuration. Each provider calls the existing safety/quantization functions
used by current compilation.

- [ ] **Step 5: Run provider, quantity, and safety tests**

Run: `python -m pytest -q tests/test_sweep_provider_contract.py tests/test_recipe_compiler.py tests/test_settings_and_safety.py tests/test_instrument_precision.py`

Expected: PASS; out-of-range and dimension-mismatched points remain rejected.

- [ ] **Step 6: Commit provider ownership**

```bash
git add app/contracts app/devices/keithley_2600/sweep_provider.py app/devices/rigol_dg1000z/sweep_provider.py app/devices/anritsu_ms2830a/sweep_provider.py app/devices/keithley_2600/module.py app/devices/rigol_dg1000z/module.py app/devices/anritsu_ms2830a/module.py tests/test_sweep_provider_contract.py
git commit -m "refactor: move sweep axes behind device providers"
```

### Task 4: Build the shared QFluent model/view tree

**Files:**

- Create: `app/ui/measurement_tree/__init__.py`
- Create: `app/ui/measurement_tree/model.py`
- Create: `app/ui/measurement_tree/view.py`
- Create: `app/ui/measurement_tree/delegate.py`
- Create: `tests/test_measurement_tree_model.py`

**Interfaces:**

- Produces: `MeasurementTreeModel`, `MeasurementTreeView`, `MeasurementTreeDelegate`, `MeasurementTreeRole`, and `TreeInteractionMode`.
- Consumes: immutable `SemanticMeasurementTree` and optional `Mapping[str, SemanticOperationState]`.
- `MeasurementTreeView` inherits `qfluentwidgets.TreeView`; its model is the only mutable presentation surface.

- [ ] **Step 1: Write RED model-index and targeted-update tests**

```python
def test_model_exposes_semantic_hierarchy_without_widget_items(qtbot) -> None:
    model = MeasurementTreeModel(semantic_tree())
    axis = model.index_for_semantic_id("axis-current")
    operation = model.index_for_semantic_id("axis-current.set-roi-value")
    assert axis.isValid()
    assert operation.parent() == model.index_for_semantic_id("axis-current.loop")

def test_runtime_update_emits_data_changed_only_for_affected_row(qtbot) -> None:
    model = MeasurementTreeModel(semantic_tree())
    with qtbot.waitSignal(model.dataChanged) as signal:
        model.apply_state(operation_state("axis-current.set-roi-value"))
    assert signal.args[0].row() == model.index_for_semantic_id("axis-current.set-roi-value").row()
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_measurement_tree_model.py`

Expected: FAIL because the shared model/view package does not exist.

- [ ] **Step 3: Implement the custom model and stable roles**

```python
class MeasurementTreeRole(IntEnum):
    SEMANTIC_ID = int(Qt.ItemDataRole.UserRole) + 1
    NODE_KIND = int(Qt.ItemDataRole.UserRole) + 2
    SOURCE_NODE_ID = int(Qt.ItemDataRole.UserRole) + 3
    AXIS_CONTEXT = int(Qt.ItemDataRole.UserRole) + 4
    EDITABLE = int(Qt.ItemDataRole.UserRole) + 5
    DRAGGABLE = int(Qt.ItemDataRole.UserRole) + 6
    EXECUTION_PHASE = int(Qt.ItemDataRole.UserRole) + 7
```

Implement `index()`, `parent()`, `rowCount()`, `columnCount()`, `data()`,
`flags()`, `headerData()`, `index_for_semantic_id()`, `replace_tree()`, and
`apply_state()`. `replace_tree()` is allowed only at accepted preflight/load
boundaries; `apply_state()` emits targeted `dataChanged`.

- [ ] **Step 4: Implement the Fluent view and delegate**

```python
class MeasurementTreeView(TreeView):
    semantic_activated = Signal(str)
    move_requested = Signal(object)

    def set_interaction_mode(self, mode: TreeInteractionMode) -> None: ...
    def follow_semantic_id(self, semantic_id: str, *, force: bool = False) -> None: ...
```

Call `super().__init__()` so QFluentWidgets installs its `TreeItemDelegate`,
stylesheet, and smooth-scroll delegate. `MeasurementTreeDelegate` subclasses the
installed Fluent delegate and adds only the active-loop spine/status affordance.
Do not replace Fluent hover, focus, theme, or selection behaviour.

- [ ] **Step 5: Verify inheritance and rendering**

```python
def test_measurement_tree_is_fluent_model_view(qtbot) -> None:
    view = MeasurementTreeView()
    assert isinstance(view, qfluentwidgets.TreeView)
    assert not isinstance(view, QTreeWidget)
    view.setModel(MeasurementTreeModel(semantic_tree()))
    view.resize(900, 600)
    view.show()
    QApplication.processEvents()
    assert view.viewport().geometry().width() > 0
```

Run: `python -m pytest -q tests/test_measurement_tree_model.py`

Expected: PASS in light and dark theme parameterizations.

- [ ] **Step 6: Commit the shared Fluent tree**

```bash
git add app/ui/measurement_tree tests/test_measurement_tree_model.py
git commit -m "feat: add fluent semantic measurement tree"
```

### Task 5: Migrate Sweeps to the semantic Fluent tree

**Files:**

- Modify: `app/ui/recipes/page.py`
- Modify: `app/ui/recipes/common_dialogs.py`
- Modify: `app/recipes/editing.py`
- Modify: `tests/test_recipe_builder.py`
- Modify: `tests/test_fluent_recipe_execution_pages.py`

**Interfaces:**

- Consumes: `normalize_recipe_tree()`, `MeasurementTreeModel`, and `MeasurementTreeView`.
- Produces: editable Sweeps tree driven by semantic IDs, canonical axis edits, and model-owned drag/drop requests.
- Removes from the active path: `_add_operator_control_rows()`, `_add_native_sweep_roi_rows()`, synthetic `execution_container`, and `RecipeTreeWidget` item-index arithmetic.

- [ ] **Step 1: Add RED rendered Sweeps tests**

```python
def test_sweeps_tree_keeps_roi_operation_visible_at_desktop_and_narrow_sizes(qtbot) -> None:
    page = recipe_page_with_source(SINGLE_AXIS_SOURCE)
    for size in (QSize(1440, 900), QSize(1024, 720)):
        page.resize(size)
        page.show()
        QApplication.processEvents()
        index = page.tree_model.index_for_semantic_id("axis-current.set-roi-value")
        assert index.isValid()
        assert page.tree.visualRect(index).height() > 0
        assert page.tree.verticalScrollBar().maximum() >= 0

def test_generated_set_roi_row_cannot_be_dragged_or_deleted() -> None:
    index = page.tree_model.index_for_semantic_id("axis-current.set-roi-value")
    assert not page.tree_model.flags(index) & Qt.ItemFlag.ItemIsDragEnabled
    assert not page.tree_model.flags(index) & Qt.ItemFlag.ItemIsDropEnabled
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_recipe_builder.py tests/test_fluent_recipe_execution_pages.py -k "set_roi or semantic_tree or narrow"`

Expected: FAIL while Sweeps still owns `RecipeTreeWidget` and projected rows.

- [ ] **Step 3: Replace the Sweeps tree host**

Instantiate `MeasurementTreeView` and `MeasurementTreeModel` in `RecipePage`.
After successful parse/preflight, normalize once and call `replace_tree()`. Keep
selection and expansion by semantic ID, not `QTreeWidgetItem` identity.

- [ ] **Step 4: Move editing and drag/drop to semantic IDs**

Implement atomic editing helpers:

```python
def extract_device_sweep_axis(source: str, *, owner_node_id: str, parameter_id: str) -> str: ...
def move_recipe_semantic_node(source: str, request: SemanticMoveRequest) -> str: ...
def replace_sweep_axis(source: str, *, axis_id: str, replacement: Mapping[str, object]) -> str: ...
```

The model MIME payload contains only a semantic/source node ID. The controller
validates Finally boundaries, generated rows, target container, and logical
index before changing YAML. Failed moves leave both source and model unchanged.

- [ ] **Step 5: Replace parameter/ROI pseudo-rows with the axis inspector**

The axis row shows formatted range, stage count, point count, and product
preview. Activating it opens the existing ROI editor for the binding. Fixed
device parameters and output policy remain in the device editor/inspector and
do not become sibling execution rows.

- [ ] **Step 6: Run complete Builder tests**

Run: `python -m pytest -q tests/test_recipe_builder.py tests/test_fluent_dialogs.py tests/test_fluent_recipe_execution_pages.py -k "recipe or sweep or tree"`

Expected: PASS; legacy recipes display the new hierarchy and all source edits remain atomic.

- [ ] **Step 7: Commit the Sweeps migration**

```bash
git add app/ui/recipes/page.py app/ui/recipes/common_dialogs.py app/recipes/editing.py tests/test_recipe_builder.py tests/test_fluent_recipe_execution_pages.py
git commit -m "refactor: migrate sweeps to semantic fluent tree"
```

### Task 6: Compile explicit nested axes and semantic point operations

**Files:**

- Modify: `app/engine/compiler.py`
- Modify: `app/engine/estimation.py`
- Modify: `app/engine/policy.py`
- Modify: `app/ui/workers.py`
- Modify: `app/ui/recipes/page.py`
- Modify: `app/ui/shell/main_window.py`
- Modify: `tests/test_recipe_compiler.py`
- Modify: `tests/test_sweep_provider_contract.py`

**Interfaces:**

- Consumes: normalized recipe graph and registered `DeviceSweepProvider` objects.
- Extends `PlanAction` with `semantic_id`, `source_node_id`, and `axis_context`.
- Produces: one provider-validated point-update action for every semantic `Set ROI value`, including a labelled unchanged first point.
- `RecipeCompiler.__init__()` gains a required keyword `device_registry: DeviceModuleRegistry` at application call sites; tests may pass `built_in_device_registry()`.

- [ ] **Step 1: Write RED compiler tests for all three scenarios**

```python
def test_nested_two_device_axes_emit_complete_context() -> None:
    plan = compile_source(TWO_DEVICE_AXIS_SOURCE)
    point_actions = [a for a in plan.actions if a.semantic_id.endswith(".set-roi-value")]
    assert len(point_actions) == 3 + 3 * 4
    inner = [a for a in point_actions if a.axis_context.axis_id == "axis-frequency"]
    assert all(set(a.axis_context.active_setpoints_si) == {
        "keithley.B.current", "rigol.1.frequency"
    } for a in inner)

def test_same_device_two_axes_compile_as_cartesian_product() -> None:
    plan = compile_source(SAME_DEVICE_TWO_AXIS_SOURCE)
    assert plan.total_points == 6
    assert {a.kind for a in plan.actions if a.semantic_id.endswith(".set-roi-value")} >= {
        "update_keithley_level", "update_keithley_compliance"
    }
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_recipe_compiler.py -k "nested_two_device or same_device_two_axes or semantic"`

Expected: FAIL because the current compiler treats generic sweeps as context-only and rejects multiple local axes.

- [ ] **Step 3: Add semantic metadata to PlanAction**

```python
@dataclass(frozen=True, slots=True)
class PlanAction:
    node_id: str
    kind: str
    payload: dict[str, Any]
    setpoints_si: dict[str, float]
    is_finally: bool = False
    semantic_id: str | None = None
    source_node_id: str | None = None
    axis_context: AxisPointContext | None = None
```

Include the new fields in canonical plan hashing and recovery identity.

Update every production compiler construction in `RecipePage`,
`RecipePreflightWorker`, and MainWindow recovery to pass the same composed
registry used by the application shell. Do not instantiate a second registry
inside the compiler.

- [ ] **Step 4: Compile each explicit axis point through its provider**

Before visiting authored loop children, resolve the binding, build the complete
outer-to-inner `active_setpoints_si`, call `provider.compile_point()`, and append
the resulting action with semantic ID `<axis_id>.set-roi-value`.

If the first value is already applied by configuration after quantization,
emit a non-I/O `axis_value_unchanged` action carrying the same semantic ID and
confirmed configured value. Add this kind to estimation/policy with a zero
device deadline and no retry.

- [ ] **Step 5: Preserve safety flow validation**

Update `_validate_device_state_flow()` so every provider point update requires
an earlier compatible device configuration. Reject two active bindings for the
same target, a missing owner, a mismatched endpoint, and an update requiring an
OUTPUT cycle inside a live loop.

- [ ] **Step 6: Run compiler, safety, and estimation tests**

Run: `python -m pytest -q tests/test_recipe_compiler.py tests/test_sweep_provider_contract.py tests/test_execution_policy.py tests/test_plan_estimation.py`

Expected: PASS for one axis, two devices, two same-device parameters, invalid units, limits, quantization, and action-count bounds.

- [ ] **Step 7: Commit semantic compilation**

```bash
git add app/engine/compiler.py app/engine/estimation.py app/engine/policy.py app/ui/workers.py app/ui/recipes/page.py app/ui/shell/main_window.py tests/test_recipe_compiler.py tests/test_sweep_provider_contract.py
git commit -m "feat: compile nested semantic sweep axes"
```

### Task 7: Emit typed runtime axis state and confirmed values

**Files:**

- Create: `app/domain/execution_state.py`
- Modify: `app/engine/runner.py`
- Modify: `app/ui/run_worker.py`
- Modify: `tests/test_adapters_and_runner.py`
- Modify: `tests/test_simulated_run.py`

**Interfaces:**

- Produces: `SemanticOperationState` and Runner events `semantic_operation_started`, `semantic_operation_applied`, and `semantic_operation_failed`.
- Consumes: `PlanAction.semantic_id` and `PlanAction.axis_context`.
- Applied/readback values come only from adapter results or explicit `axis_value_unchanged` configured state.

- [ ] **Step 1: Write RED Runner event tests**

```python
def test_roi_update_emits_requested_then_confirmed_value() -> None:
    events = run_one_axis_with_readback(requested=0.003333333, applied=0.003333)
    started = event(events, "semantic_operation_started")
    applied = event(events, "semantic_operation_applied")
    assert started["requested_si"] == pytest.approx(0.003333333)
    assert "applied_si" not in started
    assert applied["applied_si"] == pytest.approx(0.003333)
    assert applied["verification"] == "readback"

def test_failed_roi_update_does_not_publish_requested_as_applied() -> None:
    events = run_failing_axis_update()
    failed = event(events, "semantic_operation_failed")
    assert "applied_si" not in failed
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_adapters_and_runner.py tests/test_simulated_run.py -k "semantic_operation or roi_update"`

Expected: FAIL because current events expose technical action state only.

- [ ] **Step 3: Add immutable operation state and event serialization**

```python
@dataclass(frozen=True, slots=True)
class SemanticOperationState:
    semantic_id: str
    phase: Literal["waiting", "running", "applied", "failed", "skipped"]
    requested_si: float | None
    applied_si: float | None
    readback_si: float | None
    verification: Literal["readback", "simulated_ack", "configured_unchanged"] | None
    action_index: int
    total_actions: int
    axis_context: AxisPointContext | None
```

- [ ] **Step 4: Emit semantic lifecycle around existing adapter execution**

The Runner emits `started` immediately before the concrete point update,
`applied` only after the adapter returns and device state is recorded, and
`failed` before fault/shutdown processing. Attach the complete loop path and
active setpoint context to every event.

- [ ] **Step 5: Preserve durable ordering while coalescing only presentation**

`RunTelemetryCoalescer` may latest-state coalesce semantic progress for the GUI,
but `Hdf5RunWriter.record_event()` receives every semantic lifecycle event in
order. Terminal and safety events flush pending presentation state first.

- [ ] **Step 6: Run Runner, simulation, stop, and fault tests**

Run: `python -m pytest -q tests/test_adapters_and_runner.py tests/test_simulated_run.py tests/test_run_controller.py -k "semantic or sweep or stop or fault or shutdown"`

Expected: PASS; physical and simulated paths expose the same event shape with distinct verification labels.

- [ ] **Step 7: Commit typed runtime state**

```bash
git add app/domain/execution_state.py app/engine/runner.py app/ui/run_worker.py tests/test_adapters_and_runner.py tests/test_simulated_run.py
git commit -m "feat: emit confirmed semantic sweep state"
```

### Task 8: Migrate Execution to the shared semantic Fluent tree

**Files:**

- Modify: `app/ui/execution/page.py`
- Modify: `app/ui/shell/main_window.py`
- Modify: `tests/test_fluent_recipe_execution_pages.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**

- Consumes: the exact `SemanticMeasurementTree` snapshot accepted by Sweeps preflight and typed semantic Runner state.
- Produces: read-only Execution tree with active-loop progress and requested/applied/readback values.
- Removes: `execution_tree_snapshot()` item cloning, `_project_generated_actions()`, `_install_current_roi_rows()`, prefix ownership inference, and per-action `QTreeWidgetItem` creation.

- [ ] **Step 1: Write RED parity and live-value tests**

```python
def test_execution_uses_same_semantic_tree_identity_as_sweeps(qtbot) -> None:
    snapshot = window.recipe_page.semantic_tree_snapshot()
    window.start_execution(snapshot)
    assert window.run_monitor.tree_model.tree is snapshot

def test_nested_axis_values_remain_visible_while_inner_axis_runs(qtbot) -> None:
    monitor = monitor_with_two_axis_tree()
    monitor.apply_semantic_state(inner_frequency_applied_event())
    assert monitor.value_for("axis-current") == "5 mA"
    assert monitor.value_for("axis-frequency") == "1.333 GHz"
    assert monitor.current_operation_value.text() == "1.333 GHz"
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_fluent_recipe_execution_pages.py tests/test_main_window.py -k "same_semantic_tree or nested_axis_values"`

Expected: FAIL while Execution consumes cloned `QTreeWidgetItem` snapshots.

- [ ] **Step 3: Replace the Execution tree and event mapping**

Use `MeasurementTreeView` in `READ_ONLY` mode. On run start call
`tree_model.replace_tree(preflight.semantic_tree)` once. On semantic Runner
events call `tree_model.apply_state(state)` and update the current-operation
card from the same object.

- [ ] **Step 4: Add axis progress and active-loop spine**

Axis rows show `point N/M`, stage index, and current value. The delegate paints
one connected accent spine through active outer/inner axes and the running
operation. Reduced motion uses a static indicator. A failed branch remains
expanded; completed branches do not collapse automatically.

- [ ] **Step 5: Make technical actions secondary**

Keep concrete action kind/node ID in the event details and copyable technical
log. Do not add technical rows to the main tree. Unknown semantic IDs create one
explicit `Engine-generated operations` diagnostic branch and emit a warning;
they never attach by prefix guessing.

- [ ] **Step 6: Verify default and narrow layout**

At 1440×900, the measurement tree retains at least 520 px width and the active
value column is visible. At 1024×720, the page scrolls vertically and the tree
remains usable. The event log is secondary and cannot force the tree below its
minimum useful height.

Run: `python -m pytest -q tests/test_fluent_recipe_execution_pages.py tests/test_main_window.py -k "execution or semantic or geometry or narrow"`

Expected: PASS after `show()` and event processing in light/dark themes.

- [ ] **Step 7: Commit the Execution migration**

```bash
git add app/ui/execution/page.py app/ui/shell/main_window.py tests/test_fluent_recipe_execution_pages.py tests/test_main_window.py
git commit -m "refactor: share semantic tree with execution"
```

### Task 9: Bound GUI work and prove responsiveness

**Files:**

- Modify: `app/ui/shell/main_window.py`
- Modify: `app/ui/execution/page.py`
- Modify: `app/ui/widgets/spectrum_plot.py`
- Modify: `app/ui/run_worker.py`
- Modify: `tests/test_execution_ui_responsiveness.py`
- Modify: `tests/test_fluent_recipe_execution_pages.py`

**Interfaces:**

- Produces: `ExecutionPresentationBuffer.submit(name, payload)`, `flush_visual_state()`, and diagnostics `ExecutionUiMetrics`.
- Safety/terminal events are immediate; model, device-table, event-log, and plot streams have independent bounded cadences.

- [ ] **Step 1: Add RED coalescing and no-rebuild tests**

```python
def test_1000_semantic_events_coalesce_to_bounded_model_flushes(qtbot) -> None:
    page = monitor_with_tree()
    for index in range(1000):
        page.queue_semantic_state(state_for(index))
    process_events_for(250)
    assert page.ui_metrics.semantic_events_received == 1000
    assert page.ui_metrics.model_flushes <= 8
    assert page.ui_metrics.tree_rebuilds == 0

def test_fault_bypasses_pending_visual_work(qtbot) -> None:
    page.queue_semantic_state(running_state())
    page.append_event("run_fault", fault_payload())
    assert page.state.text() == "FAULT"
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_execution_ui_responsiveness.py -k "coalesce or fault_bypasses"`

Expected: FAIL because current batching is split across several widget-specific queues.

- [ ] **Step 3: Implement one presentation buffer**

```python
@dataclass(slots=True)
class ExecutionPresentationBuffer:
    latest_semantic: dict[str, SemanticOperationState]
    latest_device_snapshot: dict[str, object] | None
    latest_preview: dict[str, object] | None
    log_events: deque[tuple[str, dict[str, object]]]
    metrics: ExecutionUiMetrics
```

Flush semantic/model state at 33 ms, active-row following at 100 ms, preview at
200 ms, and a maximum of eight visible-log rows per GUI tick. Terminal events
stop timers, discard stale presentation frames, and render immediately after
the durable event has already been recorded.

- [ ] **Step 4: Remove expensive per-event operations**

Prohibit `expandAll()`, full column resize, `clear()`, tree cloning, recursive
item lookup, and unconditional scrolling in runtime handlers. Cache model
indexes by semantic ID. Downsample preview data before plot-path construction
and do not refresh unrelated device pages for an Anritsu preview.

- [ ] **Step 5: Run the deterministic 1000-point qualification**

Run: `python -m pytest -q tests/test_execution_ui_responsiveness.py -m qualification`

Expected: exactly 1000 completed points, visible intermediate progress,
`maximum_gap_s < 0.250`, bounded queue depth, zero runtime tree rebuilds, and no
lost safety/terminal event.

- [ ] **Step 6: Run the standard UI lane**

Run: `python -m pytest -q tests/test_fluent_recipe_execution_pages.py tests/test_main_window.py tests/test_execution_ui_responsiveness.py -m "not qualification"`

Expected: PASS with no `application is not responding` interval reproduced.

- [ ] **Step 7: Commit responsiveness work**

```bash
git add app/ui/shell/main_window.py app/ui/execution/page.py app/ui/widgets app/ui/run_worker.py tests/test_execution_ui_responsiveness.py tests/test_fluent_recipe_execution_pages.py
git commit -m "perf: bound execution presentation work"
```

### Task 10: Persist axis provenance and preserve recovery compatibility

**Files:**

- Modify: `app/storage/hdf5_writer.py`
- Modify: `app/storage/thatec_writer.py`
- Modify: `app/engine/recovery.py`
- Modify: `tests/test_hdf5_writer.py`
- Modify: `tests/test_run_recovery.py`
- Modify: `tests/test_thatec_schema_mapper.py`

**Interfaces:**

- Existing point `setpoints_si` and public thaTEC/PyThat mappings remain unchanged.
- Additive private metadata records `semantic_operation_id`, `axis_id`, `stage_index`, `point_index`, `loop_path`, requested/applied/readback values, and verification kind.
- Recovery identity includes semantic plan fields through the existing canonical plan hash.

- [ ] **Step 1: Write RED round-trip tests**

```python
def test_axis_provenance_round_trips_without_changing_setpoint_schema(tmp_path) -> None:
    path = write_axis_point(tmp_path, axis_context=two_axis_context())
    point = Hdf5RunReader(path).read_points()[0]
    assert point.setpoints["keithley.B.current"] == pytest.approx(0.005)
    assert point.metadata["axis_context"]["loop_path"] == ["axis-current", "axis-frequency"]

def test_resume_rejects_changed_axis_nesting(tmp_path) -> None:
    checkpoint = create_checkpoint(tmp_path, OUTER_CURRENT_INNER_FREQUENCY)
    with pytest.raises(RecoveryError, match="plan hash"):
        resume(checkpoint, OUTER_FREQUENCY_INNER_CURRENT)
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q tests/test_hdf5_writer.py tests/test_run_recovery.py tests/test_thatec_schema_mapper.py -k "axis_provenance or axis_nesting"`

Expected: FAIL because axis context is not stored explicitly.

- [ ] **Step 3: Add additive axis-context persistence**

Store one canonical JSON object per point/event with explicit SI field names and
unit manifest references. Do not rename existing datasets or reinterpret legacy
setpoint keys. Readers return an empty axis context for old files.

- [ ] **Step 4: Preserve recovery and terminal-state guarantees**

Resume compares the complete plan hash, including semantic axis order. UI
expansion, selection, and presentation cadence never enter the recovery
contract. Confirmed safe boundaries and shutdown remain authoritative.

- [ ] **Step 5: Run storage, Results, and recovery tests**

Run: `python -m pytest -q tests/test_hdf5_writer.py tests/test_thatec_validator.py tests/test_thatec_schema_mapper.py tests/test_run_recovery.py tests/test_results_page.py`

Expected: PASS for new and legacy files, interrupted append, resume mismatch, and PyThat round-trip.

- [ ] **Step 6: Commit provenance support**

```bash
git add app/storage/hdf5_writer.py app/storage/thatec_writer.py app/engine/recovery.py tests/test_hdf5_writer.py tests/test_run_recovery.py tests/test_thatec_schema_mapper.py
git commit -m "feat: persist semantic sweep axis provenance"
```

### Task 11: Complete cross-cutting qualification and remove the legacy path

**Files:**

- Modify: `app/ui/recipes/page.py`
- Modify: `app/ui/recipes/common_dialogs.py`
- Modify: `app/ui/execution/page.py`
- Modify: `tests/test_recipe_builder.py`
- Modify: `tests/test_fluent_recipe_execution_pages.py`
- Modify: `tests/test_simulated_run.py`
- Create: `docs/qualification/unified-sweep-tree-release-gate.md`

**Interfaces:**

- The shared semantic tree is the only normal Sweeps/Execution projection.
- Legacy YAML compatibility remains at normalization/parser boundaries only.
- No production UI code constructs `QTreeWidgetItem` for the Sweeps or Execution measurement tree.

- [ ] **Step 1: Add architecture enforcement tests**

```python
def test_sweeps_and_execution_use_fluent_tree_view() -> None:
    assert issubclass(MeasurementTreeView, qfluentwidgets.TreeView)
    assert "QTreeWidgetItem" not in sweep_execution_tree_source()

def test_execution_has_no_generated_action_prefix_mapping() -> None:
    source = Path("app/ui/execution/page.py").read_text(encoding="utf-8")
    assert "_project_generated_actions" not in source
    assert "startswith(candidate + \".\")" not in source
```

- [ ] **Step 2: Remove retired tree code after all callers migrate**

Delete `RecipeTreeWidget` only if no unrelated consumer remains. Otherwise keep
the class for that consumer but remove it from Sweeps/Execution imports. Delete
the retired operator-row, native-ROI-row, current-ROI-row, item-clone, and prefix
projection methods.

- [ ] **Step 3: Document the release gate**

The document must require:

- one-axis, two-device-axis, and same-device-two-axis compilation;
- requested/applied/readback event checks;
- normal completion, stop, compliance, timeout, transport loss, and shutdown failure;
- 1000-point deterministic simulation with 10,001-value spectra;
- desktop/narrow light/dark rendered screenshots;
- separate HIL command-order and readback qualification before a physical release.

- [x] **Step 4: Run focused cross-cutting verification**

Run: `python -m pytest -q tests/test_recipe_semantic_tree.py tests/test_sweep_provider_contract.py tests/test_recipe_builder.py tests/test_recipe_compiler.py tests/test_adapters_and_runner.py tests/test_fluent_recipe_execution_pages.py tests/test_execution_ui_responsiveness.py tests/test_hdf5_writer.py tests/test_run_recovery.py tests/test_simulated_run.py -m "not qualification and not hil"`

Expected: PASS with no skipped safety or compatibility test in this focused lane.

- [x] **Step 5: Run static verification**

Run: `python -m ruff check app tests`

Expected: `All checks passed!`

Run: `python -m compileall -q app tests`

Expected: exit code 0.

- [x] **Step 6: Run the heavy simulated qualification**

Run: `python -m pytest -q tests/test_execution_ui_responsiveness.py tests/test_simulated_run.py -m qualification`

Expected: PASS with exactly 1000 committed points, complete spectra, confirmed
safe shutdown, visible intermediate UI progress, and maximum GUI gap below 250 ms.

- [x] **Step 7: Perform visual inspection**

Launch the application in simulation, load each of the three reference recipes,
and capture Sweeps and Execution at 1440×900 and 1024×720 in light and dark
themes. Verify the active-loop spine, scroll reachability, non-truncated current
value, keyboard focus, reduced-motion state, and absence of row movement during
execution.

- [ ] **Step 8: Commit qualification and legacy removal**

```bash
git add app/ui/recipes app/ui/execution tests/test_recipe_builder.py tests/test_fluent_recipe_execution_pages.py tests/test_simulated_run.py docs/qualification/unified-sweep-tree-release-gate.md
git commit -m "test: qualify unified sweep execution tree"
```

## Final GO criteria

The refactor is complete only when all conditions hold simultaneously:

1. Sweeps and Execution use `qfluentwidgets.TreeView` with the shared custom model.
2. The normal runtime path creates no `QTreeWidgetItem` measurement-tree rows.
3. Every axis has exactly one generated `Set ROI value` semantic child.
4. One variable, two devices, and two parameters on one device behave according to the design diagrams.
5. Axis nesting order is visible, deterministic, hashed, persisted, and recovery-checked.
6. Requested values never masquerade as applied/readback values.
7. Old recipe and HDF5 fixtures remain readable and retain their original meaning.
8. No UI event rebuilds the full tree during a run.
9. The 1000-point simulated qualification meets the 250 ms GUI-gap threshold.
10. Stop, fault, compliance, watchdog, E-STOP, and shutdown tests remain green.
11. Desktop/narrow and light/dark visual inspection is recorded.
12. Ruff, compileall, focused cross-cutting tests, and qualification tests pass.

## Self-review

- Spec coverage: Tasks 2–3 cover normalized axes/provider ownership; Tasks 4–5 cover the Fluent Sweeps tree; Tasks 6–7 cover semantic compilation and confirmed runtime state; Tasks 8–9 cover Execution and responsiveness; Task 10 covers provenance/recovery; Task 11 covers removal and qualification.
- Placeholder scan: every task names its interfaces, implementation action, verification command, expected result, and commit boundary.
- Type consistency: `SweepAxisBinding` and `AxisPointContext` originate in `app/recipes/semantic_tree.py`; `SemanticOperationState` originates in `app/domain/execution_state.py`; `MeasurementTreeModel` consumes both through stable semantic IDs; `PlanAction` carries the same semantic identity into Runner and recovery.
- QFluent decision: the plan explicitly uses `qfluentwidgets.TreeView` and preserves its delegate/theme/smooth-scroll initialization while replacing only the item-based data source.
- Safety review: device providers reuse existing validators and quantizers; Runner remains the source of confirmed state; GUI coalescing cannot suppress safety/terminal events; Finally remains independent.
- Scope order: Sweeps reaches a testable semantic Fluent tree in Task 5 before Execution migrates in Task 8.

## WAIT execution/display follow-up (2026-09-02)

The explicit `wait` action was already interruptible and honored its normalized
`duration_s` in the Runner. The defect was in the presentation boundary: the
Execution cadence buffer reconstructed every coalesced semantic event as a
generic `set point`, dropping `kind`, `duration_s`, device/channel and trace
metadata. A real two-second wait therefore looked as if it had been skipped,
even though the engine and HDF5 event stream showed the expected ~2.00 s gap.

The corrective slice keeps the existing safety/runtime behavior and makes the
metadata durable across the UI buffer:

- `SemanticOperationState` now carries the display metadata needed by the
  semantic tree and current-operation card.
- Runner lifecycle events include the wait duration, and wait requested/applied
  values are expressed in seconds instead of reusing the active sweep setpoint.
- The compiler propagates the active ROI axis context into legacy device-module
  child actions, including explicit waits and acquisition steps.
- Execution renders `WAITING`/`MEASURING`/`SETTING`, the duration (`2 s`), SI
  value, active point and loop context from the coalesced state.

Regression coverage:

- compiler test: every per-point legacy-module wait retains `duration_s=2.0`,
  semantic identity and the correct ROI point context;
- Runner test: an 80 ms wait blocks completion until its duration elapses and
  still reaches confirmed safe shutdown;
- UI test: a queued two-second wait remains visible as `Wait · 2 s`,
  `WAITING`, then `CONFIRMED` after the applied event.
- exact runtime integration: the literal recipe spelling `duration: 2000 ms`
  compiles to `2.0 s` and cannot complete before 1.95 s;
- the current-operation card now shows a live WAIT countdown while retaining
  the active ROI point, then changes to an explicit `WAIT completed` boundary.

The WAIT slice is covered independently by the focused regressions above. The
full deterministic GUI gate was rerun on 2026-09-03 with exactly 1,000
spectra of 10,001 source values and passed the unchanged 250 ms maximum-gap
criterion in 119.43 s. Full HDF5 traces remained intact; only the live plot
preview was deterministically decimated before painting.

## Final visual/responsiveness follow-up (2026-09-03)

- The shared Fluent tree now uses semantic icons and accents for sequences,
  devices, axes, loop bodies, ROI updates, waits, acquisitions, outputs and
  guaranteed shutdown.
- Rows have a 34 px minimum height, 18 px icons and 24 px hierarchy indentation.
- Every model install/reset expands the complete hierarchy immediately and
  once more after Qt's deferred geometry pass.
- Deep read-only following uses one coalesced latest-state timer. It scrolls an
  off-screen active row into view but does not mutate Qt selection on every
  action; semantic running/applied state owns the highlight.
- The final 1,000 × 10,001 qualification passed in 119.43 s with the unchanged
  maximum GUI-gap threshold below 250 ms.
- Twenty-four native Windows captures cover the three reference scenarios,
  both pages, both viewport sizes and both themes.
- In the compact 1024×720 Execution layout, the duplicate global Event log is
  auto-collapsed so the semantic tree receives the first viewport. The
  Application menu can explicitly restore it, and the state is covered by a
  shell geometry regression test.

## Final verification record (2026-09-03)

- Focused lane: `313 passed, 1 deselected, 15 subtests passed`.
- Static lane: `ruff check app tests` and `compileall -q app tests` passed.
- Qualification: `1 passed, 20 deselected` in 119.43 s; exactly 1,000 committed
  points and 10,001 values per stored spectrum.
- Rendered evidence: 24 native Windows captures regenerated and inspected.
- No commit was created; the working tree remains on `master` as requested.

The proposed RAM batch for measurement results was not used as a UI workaround.
HDF5 writing already runs outside the GUI thread and currently establishes an
atomic, recoverable commit boundary for every point. Diagnostics isolated the
stall to tree selection repainting. A future storage-process queue is valid only
with bounded memory/backpressure, strict ordering, acknowledged durable batch
boundaries and forced drain on all terminal/safety paths.
