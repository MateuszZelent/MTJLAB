# SWEEP Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute a four-device SWEEP in simulation, persist the complete applied state for every spectrum, and expose that state in Results alongside PyThat data.

**Architecture:** A per-run deterministic simulation context supplies stable device-specific random streams. `RecipeRunner` keeps a complete applied-state ledger and stores it with every checkpoint. The HDF5 reader and Results page show that state, while PyThat remains the source for the measurement dataset and spectrum axes.

**Tech Stack:** Python 3.11, PySide6, h5py, PyThat 0.2.14, pytest.

## Global Constraints

- `--simulate` must never open physical VISA, TCP/IP, USB, serial, or alter `.config/settings.yml`.
- Same seed plus same plan must reproduce numerical simulated data; device streams are derived by SHA-256 and may not use Python `hash()`.
- A checkpoint containing a spectrum must retain the state of every device required by that plan.
- Static device configuration is metadata/indicator data, never a synthetic PyThat axis.
- A PyThat failure prevents a run from being reported as completed.
- Lake Shore changes already present in the worktree are outside this implementation.

---

### Task 1: Deterministic simulation context and MOKE transport

**Files:**
- Create: `app/devices/simulation.py`
- Modify: `app/devices/simulators.py`
- Modify: `app/devices/moke_box/module.py`
- Test: `tests/test_simulation_context.py`
- Test: `tests/test_device_modules.py`

**Interfaces:**
- Produces `SimulationContext(seed: int, model_version: str = "1")` with `random_stream(device_key, stream_key)`.
- Produces `SimulatedMokeBoxTransport(context: SimulationContext)` accepted by `MokeBoxAdapter`.
- `SimulatedVisaFactory(..., context: SimulationContext | None = None)` uses its device stream.

- [ ] **Step 1: Write failing tests**

```python
def test_device_streams_are_reproducible_and_independent():
    first = SimulationContext(seed=7)
    second = SimulationContext(seed=7)
    assert first.random_stream("anritsu", "trace").random() == second.random_stream("anritsu", "trace").random()
    assert first.random_stream("anritsu", "trace").random() != first.random_stream("keithley", "measurement").random()

def test_moke_module_connects_without_tcp_in_simulation():
    adapter = built_in_device_registry().get("moke_box").create_adapter(simulated_station_settings(loaded_settings()), simulation=True)
    assert adapter.connect().idn.startswith("MOKE")
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest -q tests/test_simulation_context.py tests/test_device_modules.py -k "simulation or moke"`

Expected: FAIL because `SimulationContext` and the simulated MOKE adapter do not exist.

- [ ] **Step 3: Implement the context and binary MOKE fake**

```python
class SimulationContext:
    def random_stream(self, device_key: str, stream_key: str) -> random.Random:
        material = f"{self.seed}:{self.model_version}:{device_key}:{stream_key}".encode()
        return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:16], "big"))
```

Implement `connect`, `send`, `recv_exact`, and `close` in the MOKE transport. Return eight valid VOUT frames for the identity probe and a valid Hall AD7734 frame for `request_samples(1)`.

- [ ] **Step 4: Run GREEN tests**

Run: `python -m pytest -q tests/test_simulation_context.py tests/test_device_modules.py -k "simulation or moke"`

Expected: PASS.

### Task 2: Simulation provenance in run files

**Files:**
- Modify: `app/storage/hdf5_writer.py`
- Modify: `app/storage/hdf5_reader.py`
- Test: `tests/test_hdf5_writer.py`

**Interfaces:**
- `Hdf5RunWriter(..., simulation_metadata: dict[str, object] | None = None)` writes `/run/simulation_json`.
- `RunDetail.simulation_metadata: dict[str, object]` exposes that dataset.

- [ ] **Step 1: Write a failing writer/reader test**

```python
writer = Hdf5RunWriter(..., simulation_metadata={"enabled": True, "seed": 71, "model_version": "1", "devices": ["rigol"]})
writer.close("completed")
assert Hdf5RunReader.detail(path).simulation_metadata["seed"] == 71
```

- [ ] **Step 2: Run RED test**

Run: `python -m pytest -q tests/test_hdf5_writer.py -k simulation_metadata`

Expected: FAIL because the writer does not accept simulation provenance.

- [ ] **Step 3: Implement immutable run provenance**

Write `simulation_json` with the same serialisation rules as capabilities. On resume, retain the original dataset and reject a conflicting simulation/non-simulation mode.

- [ ] **Step 4: Run GREEN test**

Run: `python -m pytest -q tests/test_hdf5_writer.py -k simulation_metadata`

Expected: PASS.

### Task 3: Complete applied-device state in every checkpoint

**Files:**
- Modify: `app/engine/runner.py`
- Modify: `app/storage/hdf5_writer.py`
- Modify: `app/storage/hdf5_reader.py`
- Test: `tests/test_simulated_run.py`
- Test: `tests/test_hdf5_writer.py`

**Interfaces:**
- `Hdf5RunWriter.append(..., device_states: dict[str, object] | None = None)` atomically writes `device_state_json` below `/points/<index>`.
- `StoredPoint.device_states: dict[str, object]` loads that dataset.
- `RecipeRunner` passes a full snapshot at every spectrum/checkpoint.

- [ ] **Step 1: Write a failing atomic-storage test**

```python
writer.append(point, trace, device_states={"rigol": {"channel_1": {"frequency_hz": 1_000.0, "waveform": "SIN"}}})
assert Hdf5RunReader.points(path)[0].device_states["rigol"]["channel_1"]["waveform"] == "SIN"
```

- [ ] **Step 2: Run RED test**

Run: `python -m pytest -q tests/test_hdf5_writer.py -k device_state`

Expected: FAIL because `append` has no `device_states` argument.

- [ ] **Step 3: Implement state ledger and checkpoint dataset**

Keep `RecipeRunner._applied_device_states`. After every `configure_*`, `update_*`, and output action, store a serialisable full state from the action payload plus confirmed actual values. Store the resulting mapping as `device_state_json` within `_pending/<index>` before its atomic move.

- [ ] **Step 4: Write and run four-device RED integration test**

The test must build a 2 × 3 plan with Rigol frequency updates, Keithley I/V/P, MOKE Hall, and Anritsu spectra; assert every one of six spectra has all four state keys and all fixed Rigol fields.

Run: `python -m pytest -q tests/test_simulated_run.py -k four_device_state`

Expected: FAIL before the runner supplies all state snapshots.

- [ ] **Step 5: Implement missing state updates and verify GREEN**

Run: `python -m pytest -q tests/test_hdf5_writer.py -k device_state; python -m pytest -q tests/test_simulated_run.py -k four_device_state`

Expected: PASS.

### Task 4: Results uses PyThat for data and exposes full device state

**Files:**
- Create: `app/storage/pythat_reader.py`
- Modify: `app/ui/results/page.py`
- Test: `tests/test_results_page.py`

**Interfaces:**
- `PyThatRunData.load(path) -> PyThatRunData` opens `MeasurementTree(index=True, override=True)` and exposes dimensions/data variables.
- Results gains read-only `Data` and `Device state` panes.

- [ ] **Step 1: Write a failing Results test**

```python
page.points.setCurrentItem(page.points.topLevelItem(0))
assert "rigol" in page.device_state.toPlainText()
assert "PyThat dimensions" in page.data.toPlainText()
```

- [ ] **Step 2: Run RED test**

Run: `python -m pytest -q tests/test_results_page.py -k "pythat or device_state"`

Expected: FAIL because the panes and PyThat loader do not exist.

- [ ] **Step 3: Implement loader and panes**

Load PyThat when a run is selected; if it fails, put the explicit compatibility error in the data pane and do not use native scalar data as a silent substitute. On point selection, format `StoredPoint.device_states` in the device-state pane.

- [ ] **Step 4: Run GREEN test**

Run: `python -m pytest -q tests/test_results_page.py -k "pythat or device_state"`

Expected: PASS.

### Task 5: Run-worker provenance and end-to-end acceptance

**Files:**
- Modify: `app/ui/run_worker.py`
- Create: `recipes/simulation_four_device_sweep.yml`
- Modify: `tests/test_simulated_run.py`
- Modify: `tests/test_run_controller.py`

**Interfaces:**
- A simulated run creates `SimulationContext`, passes it to all adapters, and writes its metadata.
- The acceptance recipe is a four-device 10 × 100 plan with `measure_moke_hall` before every acquisition.

- [ ] **Step 1: Write failing end-to-end test**

```python
result_path = run_simulated_recipe("simulation_four_device_sweep.yml", seed=42)
assert all(set(Hdf5RunReader.points(result_path)[i].device_states) == {"rigol", "keithley", "anritsu", "moke_box"} for i in range(1000))
```

- [ ] **Step 2: Run RED test**

Run: `python -m pytest -q tests/test_simulated_run.py -k acceptance_four_device`

Expected: FAIL because no four-device recipe or shared run context exists.

- [ ] **Step 3: Implement run wiring and recipe**

Generate one seed per simulated run, pass it to VISA/MOKE factories, provide `simulation_metadata` to the writer, and add `measure_moke_hall` inside the innermost loop of the acceptance recipe.

- [ ] **Step 4: Verify smoke and acceptance**

Run: `python -m pytest -q tests/test_simulated_run.py -k "four_device or acceptance_four_device"`

Expected: PASS. Run the 1000-point acceptance test separately with a 60-second progress check.

### Task 6: Full verification and manual Results evidence

**Files:**
- Test: `tests/test_simulation_context.py`
- Test: `tests/test_hdf5_writer.py`
- Test: `tests/test_simulated_run.py`
- Test: `tests/test_results_page.py`

- [ ] **Step 1: Run focused suite**

Run: `python -m pytest -q tests/test_simulation_context.py tests/test_hdf5_writer.py tests/test_simulated_run.py tests/test_results_page.py`

Expected: PASS.

- [ ] **Step 2: Run lint and complete suite**

Run: `python -m ruff check app tests; python -m pytest -q`

Expected: PASS without new warnings/errors.

- [ ] **Step 3: Generate an evidence file and inspect it**

Run the four-device acceptance recipe with a fixed seed into `measurements/`. Inspect `/run/simulation_json`, all `/points/*/device_state_json`, PyThat dimensions, and the offscreen Results page test.

- [ ] **Step 4: Commit only implementation files**

```bash
git add app/devices/simulation.py app/devices/simulators.py app/devices/moke_box/module.py app/engine/runner.py app/storage app/ui/results app/ui/run_worker.py recipes/simulation_four_device_sweep.yml tests
git commit -m "feat: add reproducible four-device sweep simulation"
```

