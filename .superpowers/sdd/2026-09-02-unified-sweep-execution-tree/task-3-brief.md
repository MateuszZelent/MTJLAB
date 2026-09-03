# Task 3: Register device-owned sweep providers

Read this brief first; it is the exact task contract. Work on shared `master`, preserve prior commits and unrelated changes, do not spawn agents.

Create `app/contracts/sweep_provider.py`; modify `app/contracts/device_module.py` and `app/contracts/__init__.py`; create provider modules under `app/devices/keithley_2600/sweep_provider.py`, `app/devices/rigol_dg1000z/sweep_provider.py`, and `app/devices/anritsu_ms2830a/sweep_provider.py`; update each device `module.py`; create `tests/test_sweep_provider_contract.py`.

Public contracts:
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
    def compile_point(self, node: RecipeNode, binding: SweepAxisBinding, value: Quantity, context: Mapping[str, Quantity], settings: StationSettings) -> CompiledAxisSetpoint: ...
```

Extend frozen `RecipeExtension` with `sweep_provider: DeviceSweepProvider | None = None` as the only registration path. Add `DeviceModuleRegistry.sweep_providers() -> Mapping[str, DeviceSweepProvider]`; include only enabled extensions with a provider and reject module/provider key mismatch. Providers are pure and must not import QWidget, VISA, or storage.

Keithley owns source level/current/voltage, compliance, settling-time bindings; Rigol owns frequency/high/low level transformations; Anritsu owns spectrum and SG bindings. Reuse existing registry descriptors, safety validators, quantizers, and adapter payload conventions; preserve explicit SI quantity/dimension/limit behavior. `bind_legacy_action` must support current `parameter_actions` sweep forms, including parameter IDs and channel/endpoint information.

Write RED tests first, then GREEN. Required tests include registered provider for `keithley`, `rigol`, `anritsu`; Keithley current and compliance compile independently with `action_kind == "update_keithley_level"` and `"update_keithley_compliance"`; mismatch/disabled providers; wrong dimensions and out-of-range rejection. Run:
`python -m pytest -q tests/test_sweep_provider_contract.py tests/test_recipe_compiler.py tests/test_settings_and_safety.py tests/test_instrument_precision.py`

Commit `refactor: move sweep axes behind device providers` and write `.superpowers/sdd/2026-09-02-unified-sweep-execution-tree/task-3-report.md`.
