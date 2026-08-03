# Anritsu Display-Driven Analysis and Manual Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Make analysis and manual spectrum saving operate on the exact currently selected/displayed trace while preventing stale live results and HDF5 double-open errors.

**Architecture:** Add a focused immutable display-state module for trace variants and selected-source identity. The Anritsu page renders from that state and submits the same snapshot to the analysis worker; worker outcomes are accepted only for the current source/frame/generation. Manual archive payloads consume the same snapshot and keep a single owned HDF5 writer.

**Tech Stack:** Python 3.11+, PySide6/QFluentWidgets, NumPy, pytest/unittest, h5py, existing HDF5 writer and quantity contracts.

## Global Constraints

- Keep `SpectrumTrace` raw values immutable and preserve Hz/dBm/dB/ratio/mW² units.
- Display analysis is read-only and must not issue VISA commands or affect output safety state.
- Preserve the existing HDF5/thaTEC checkpoint and provenance contract.
- Keep the Fluent shell and existing workflows; add focused rendering tests after `show()` and event processing.
- Use TDD: each production change starts with a focused failing test and ends with a passing focused test.

### Task 1: Display-state model

**Files:**
- Create: `app/devices/anritsu_ms2830a/ui/display_state.py`
- Test: `tests/test_anritsu_display_state.py`

**Interfaces:**
- `SpectrumDisplayTrace(key, label, frequencies_hz, values, unit, frame_id, provenance)`.
- `SpectrumDisplayState(available, selected_key, primary_key)` with `select(key)` and `select_fallback(preferred)`.
- `build_display_state(raw, averaged, reference, operation, visibility, frame_id)` returns immutable trace variants and a valid primary selection.

- [ ] Write failing tests for raw/averaged/reference/processed availability, explicit source selection, and a fallback when a selected trace disappears.
- [ ] Run `python -m pytest tests/test_anritsu_display_state.py -q`; expect missing module/API failures.
- [ ] Implement pure state derivation and provenance/unit preservation; keep reference math delegated to `apply_reference_operation`.
- [ ] Run the focused tests and verify all pass.
- [ ] Run quantity edge tests for dBm difference, ratio, and mW² units; verify no value is relabeled as dBm.

### Task 2: Unit-aware analysis result contract

**Files:**
- Modify: `app/spectrum/analysis.py`
- Modify: `app/devices/anritsu_ms2830a/ui/analysis_worker.py`
- Modify: `app/devices/anritsu_ms2830a/ui/peak_analysis.py`
- Test: `tests/test_spectrum_analysis.py`, `tests/test_anritsu_analysis_worker.py`

**Interfaces:**
- Extend `SpectrumPeak` with an explicit amplitude unit/value contract while retaining compatibility for dBm callers.
- `SpectrumAnalysisRequest` consumes `source_key`, `frame_id`, `unit`, `values`, and `generation`.
- `SpectrumAnalysisOutcome` echoes `source_key`, `frame_id`, `unit`, and `generation`.

- [ ] Write failing tests proving a processed dB input reaches cleanup/detection unchanged and a dBm input still permits physical fit metrics.
- [ ] Run the focused tests and verify failures are caused by the missing source/unit contract.
- [ ] Implement generic finite-value cleanup/detection with dBm-only model fitting and unit-bearing results.
- [ ] Run focused analysis tests and the existing spectrum-processing tests.
- [ ] Verify wrong units, NaN/infinity, and mismatched grids fail closed.

### Task 3: Anritsu page uses one display snapshot

**Files:**
- Modify: `app/devices/anritsu_ms2830a/ui/page.py`
- Modify: `app/devices/anritsu_ms2830a/ui/analysis_worker.py`
- Test: `tests/test_main_window.py` or the focused Anritsu UI test module, plus `tests/test_anritsu_display_state.py`

**Interfaces:**
- Add `self._display_state` and `self._analysis_source` state.
- Add `self.analysis_source` combo to the analysis card.
- Add `_current_display_state()`, `_sync_analysis_source_options()`, `_submit_selected_analysis(force=False)`, and `_analysis_values()` based on the selected snapshot.

- [ ] Write a failing UI test that displays raw plus `Signal − reference`, selects processed, and asserts the analysis request carries processed values/unit/source key.
- [ ] Write a failing live-generation test that submits frame N then frame N+1 and confirms the N result cannot replace N+1.
- [ ] Run both tests and verify the old raw-source behavior fails them.
- [ ] Implement snapshot construction before rendering and route cleanup, peak markers, table, tracking, and floating spectrum through the selected source/result.
- [ ] On every completed frame, reference change, operation change, cleanup change, visibility change, and source selection, rebuild options and submit one newest request.
- [ ] Apply outcomes only when generation, frame, source key, and unit match; otherwise discard them.
- [ ] Render selector/status at 1360×880 and 820×560 with visible geometry assertions.
- [ ] Run focused Anritsu tests and existing live/reference tests.

### Task 4: Manual save consumes the selected display snapshot

**Files:**
- Modify: `app/devices/anritsu_ms2830a/ui/page.py`
- Modify: `app/storage/manual_spectrum_writer.py` only for payload/provenance fields required by the existing schema.
- Test: `tests/test_main_window.py`, `tests/test_manual_spectrum_writer.py`

**Interfaces:**
- `_manual_trace_choices()` derives choices from current display state.
- `_manual_trace_payload(variant)` returns the selected `SpectrumDisplayTrace` values and provenance, not a page-global cleanup result.

- [ ] Write a failing test selecting reference-operation output, saving manually, and reading back the saved processed values/unit/source metadata.
- [ ] Run it and verify the existing implementation saves raw plus unrelated cleanup values.
- [ ] Implement payload selection from the immutable display snapshot without mutating raw/reference data.
- [ ] Run manual writer compatibility and provenance tests.

### Task 5: Single-owner HDF5 append session

**Files:**
- Modify: `app/storage/manual_spectrum_writer.py`
- Modify: `app/devices/anritsu_ms2830a/ui/page.py` for explicit close on page teardown.
- Test: `tests/test_manual_spectrum_writer.py`, `tests/test_main_window.py`

**Interfaces:**
- Keep `ManualSpectrumArchive.save()` and `close()` public signatures stable.
- Add an internal `_close_writer_before_reopen()`/ownership guard and deterministic error classification for a destination already owned by another session.

- [ ] Write failing tests for two archive instances targeting the same append file, switching destinations, and page close releasing the writer.
- [ ] Run them and verify the current implementation attempts a conflicting reopen or leaves an open handle.
- [ ] Implement one-writer ownership, close-before-switch, and safe reopen/resume; never overwrite an uncertain/closed archive.
- [ ] Run all manual archive tests plus HDF5/thaTEC validation tests.

### Task 6: Regression and completion audit

**Files:**
- Modify only focused tests/docs if verification reveals a contract gap.

- [ ] Run `python -m pytest tests/test_anritsu_display_state.py tests/test_anritsu_analysis_worker.py tests/test_manual_spectrum_writer.py -q`.
- [ ] Run focused Anritsu page/main-window tests and existing `tests/test_spectrum_processing.py` targets.
- [ ] Run `ruff check` on all changed files and `git diff --check`.
- [ ] Confirm no VISA calls occur during display analysis or manual save selection.
- [ ] Confirm raw/reference persistence and HDF5 checkpoint/status invariants with readers/validator.
- [ ] Perform a requirement-by-requirement audit against the design spec before claiming completion.
