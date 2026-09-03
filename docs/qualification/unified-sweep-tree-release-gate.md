# Unified Sweep Tree Release Gate

This gate is mandatory before a release that changes sweep normalization,
compilation, execution presentation, persistence, recovery, or instrument
output behaviour. Simulation proves software behaviour only; it does not
replace hardware-in-the-loop qualification.

## Automated software gate

Run on the release workstation from the repository root:

```powershell
python -m pytest -q tests/test_recipe_semantic_tree.py tests/test_sweep_provider_contract.py tests/test_recipe_builder.py tests/test_recipe_compiler.py tests/test_adapters_and_runner.py tests/test_fluent_recipe_execution_pages.py tests/test_execution_ui_responsiveness.py tests/test_hdf5_writer.py tests/test_run_recovery.py tests/test_simulated_run.py -m "not qualification and not hil"
python -m pytest -q tests/test_execution_ui_responsiveness.py tests/test_simulated_run.py -m qualification
python -m ruff check app tests
python -m compileall -q app tests
```

The focused lane must cover and pass:

- one sweep axis, nested axes on two devices, and two nested parameters on one
  configured device, including the expected Cartesian point counts;
- one generated semantic `Set ROI value` operation per axis and no technical
  `update_*` rows in the operator tree;
- requested, confirmed applied, and readback values without presenting a
  request as an acknowledgement;
- normal completion, operator stop, compliance trip, watchdog timeout,
  transport loss, safe-finally failure, and independent shutdown failure;
- additive axis provenance round-trip and rejection of recovery after a change
  in axis nesting or plan identity;
- schema-version-1 recipe compatibility and legacy HDF5 reader compatibility.

## Responsiveness qualification

The deterministic hardware-free qualification must complete exactly 1,000
committed measurement points. Every acquired raw spectrum and every configured
processed spectrum must contain exactly 10,001 values. The accepted semantic
tree is created before timing starts and is never rebuilt during the run.

Record these values in the release evidence:

- random seed and immutable plan hash;
- completed points and spectra/value count;
- maximum presentation queue depth and received/coalesced event counts;
- model flush count and maximum tree-update duration;
- preview flush count and maximum preview-update duration;
- longest observed GUI timer gap.

The run is a GO only when the tree visibly advances before completion, runtime
tree rebuilds equal zero, no safety/terminal event is lost, and the longest GUI
timer gap is below 250 ms. Window construction and the first Fluent layout are
completed before starting the probe and are not counted as sweep latency.

## Rendered UI gate

Capture and inspect Sweeps and Execution after `show()` and Qt event processing
in all four combinations:

- 1440×900 light;
- 1440×900 dark;
- 1024×720 light;
- 1024×720 dark.

The measurement tree must have visible, non-zero geometry and reachable
vertical scrolling. Operation, configured/active value, progress, and state
remain distinguishable. The active operation and every active outer/inner axis
must stay visible, keyboard focus must be usable, generated rows must remain
read-only, and the plot/event log must not collapse the tree below its useful
size. Check empty, invalid, disabled, hover, focus, running, applied, failed,
stopping, and reduced-motion states. Store screenshots with the release
artifacts; offscreen geometry tests alone are insufficient visual approval.

## Hardware-in-the-loop gate

Before a physical release, run a separate approved HIL procedure for each
supported instrument and firmware combination. It must verify exact command
order, OUTPUT-OFF configuration boundaries, continuous-output behaviour,
quantization, compliance/readback handling, retries, cancellation, watchdog,
E-STOP, transport loss, and confirmed independent shutdown. Record instrument
serial numbers, firmware, calibration status, station profile hash, raw TX/RX
trace, and resulting HDF5 file.

Simulation passing this document is never evidence that a physical DUT can be
energized safely.

## Release record

Complete for every candidate:

| Item | Evidence |
| --- | --- |
| Commit / build | Working tree on `master`; qualification executed before commit |
| Settings and recipe hashes | Persisted and asserted by Runner/HDF5/recovery tests |
| Focused test lane | 313 passed, 1 deselected, 15 subtests, 2026-09-03 |
| 1,000 × 10,001 qualification | 1 passed in 119.43 s after responsive shell/log optimization; exact point/value assertions |
| Maximum GUI gap | `< 0.250 s`, unchanged deterministic assertion passed |
| Light/dark desktop screenshots | 12 native captures: three scenarios × Sweeps/Execution × light/dark |
| Light/dark narrow screenshots | 12 native captures: three scenarios × Sweeps/Execution × light/dark; global Event log auto-collapses in compact Execution and remains available via the menu |
| HDF5/recovery compatibility | Axis provenance round-trip and changed nesting rejection passed; 44 broader storage tests passed, 3 licensed/golden fixtures unavailable |
| HIL report or explicit simulation-only limitation | **Simulation-only qualification. HIL is still mandatory before physical release.** |
| Reviewer and date | Codex implementation audit, 2026-09-03 |

The repeatable capture command is:

```powershell
$env:QT_QPA_PLATFORM='windows'
python tools/capture_unified_sweep_tree.py
```

The 24 native Windows captures were regenerated on 2026-09-03 and inspected for readable typography,
light/dark contrast, visible active operation and setpoint, connected active
axis spine, scroll reachability, and non-overlapping cards. The three scenarios
are one axis, nested axes on two devices, and nested axes on the same device.
In the compact 1024×720 Execution view the duplicate global Event log drawer is
collapsed automatically so the tree receives the full first viewport; the
Application menu can explicitly restore it. The offscreen
backend was rejected for visual evidence because it rendered missing-glyph
boxes; it remains appropriate for geometry-only automated tests.

The exact operator-reported WAIT form (`duration: 2000 ms`) is also a release
regression: it compiles to 2.0 s, cannot complete before 1.95 s, remains
interruptible, and presents a live countdown before the confirmed boundary.

Measurement checkpoints remain atomically durable per point. A RAM-only batch
was rejected as a GUI-stall fix: the runner/storage path is already outside the
GUI thread, while diagnostics attributed the observed qualification gap to
read-only tree selection repainting. The final model uses one coalesced reveal
timer and semantic highlighting without per-action `currentIndex()` mutation.
Any future asynchronous/batched writer must preserve backpressure, point/event
ordering, safe-boundary recovery identity, and forced drain on pause, stop,
fault, compliance, watchdog, and shutdown.
