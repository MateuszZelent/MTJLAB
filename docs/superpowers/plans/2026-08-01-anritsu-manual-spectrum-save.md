# Anritsu Manual Spectrum Save Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate manual archive configuration from saving the current Anritsu spectrum so one accepted configuration can be reused for multiple saves.

**Architecture:** Keep `ManualSpectrumSaveDialog` as the Fluent editor for immutable `ManualSpectrumSaveOptions`. Store one optional options value on `AnritsuPage`; the configure action replaces it, while the save action consumes it directly and continues delegating persistence to `ManualSpectrumArchive`. No settings YAML or HDF5 schema changes are needed.

**Tech Stack:** Python 3, PySide6, PySide6-Fluent-Widgets, unittest/pytest, h5py-backed `ManualSpectrumArchive`, Ruff.

## Global Constraints

- Preserve the existing `ManualSpectrumArchive` transaction, append/resume, frequency-grid, and HDF5 validation behavior.
- Configuration must not create a writer, create a file, query/configure the instrument, or change RF state.
- Save must use the last accepted `ManualSpectrumSaveOptions` until the user accepts a replacement configuration.
- Save failures retain the accepted options and leave the user able to retry.
- The Fluent Anritsu page remains one coherent shell/page tree and its rendering regression tests must show the page at desktop and narrow sizes.

---

### Task 1: Add the red UI regression tests

**Files:**
- Modify: `tests/test_fluent_anritsu_moke_lakeshore_pages.py` near the existing Anritsu manual archive tests
- Test: `tests/test_fluent_anritsu_moke_lakeshore_pages.py`

**Interfaces:**
- Consumes the existing `AnritsuPage`, `SpectrumTrace`, `ManualSpectrumSaveOptions`, `ManualSpectrumSaveMode`, and `Hdf5RunReader` APIs.
- Produces executable expectations for the new `configure_manual_spectrum`, `save_manual_spectrum`, and stored-options behavior.

- [ ] **Step 1: Extend imports and add a test for the separate controls and disabled state.**

Add `tempfile`, `ManualSpectrumSaveOptions`, `ManualMetadataValue` if needed by the test, and `Hdf5RunReader` to the existing test imports. Add a test that navigates to Anritsu, processes events, asserts that `page.configure_manual_spectrum` is a visible button, and asserts `page.save_manual_spectrum` is disabled before both configuration and a completed trace. After `_show_trace`, assert that save remains disabled until options are applied.

- [ ] **Step 2: Add a test that applies one configuration and saves two traces without a dialog.**

Use `TemporaryDirectory` and an append `ManualSpectrumSaveOptions` pointing to `manual.h5`, with `metadata_scope="none"` and `trace_variant="raw"`. Apply the options through the page's small state-setting helper, show one trace, process events, and assert that clicking `save_manual_spectrum` creates one point. Replace `_latest_trace` through `_show_trace` with a second trace on the same frequency grid, click save again, and assert `Hdf5RunReader.summary(path).point_count == 2`. This proves the actual writer path is reused and the save action does not require the modal dialog.

- [ ] **Step 3: Add a test that replacement configuration is used and failures retain it.**

Apply a first options value, then apply a second options value with a different destination and assert the next save writes only to the second destination. For the retry contract, use a second configuration pointing at a destination with an incompatible existing append grid, call the save method, assert the failure is reported without clearing `page._manual_save_options`, and then assert the stored destination/mode/variant still equal the accepted second options.

- [ ] **Step 4: Run the new focused tests before implementation.**

Run:

```powershell
pytest tests/test_fluent_anritsu_moke_lakeshore_pages.py -k manual_archive -v
```

Expected: FAIL because the separate configure control and the stored-options save path do not exist yet. Confirm failures are feature failures rather than import or fixture errors.

### Task 2: Separate configuration state and actions on `AnritsuPage`

**Files:**
- Modify: `app/devices/anritsu_ms2830a/ui/page.py: imports, AnritsuPage.__init__, manual archive card construction, signal wiring, `_update_manual_save_controls`, and manual archive methods`
- Modify: `app/devices/anritsu_ms2830a/ui/manual_save.py` only if the dialog contract needs a wording/validation adjustment

**Interfaces:**
- Consumes: existing `ManualSpectrumSaveDialog.options()` and `ManualSpectrumArchive.save(...)` signatures.
- Produces: `AnritsuPage._manual_save_options: ManualSpectrumSaveOptions | None`, `AnritsuPage._apply_manual_save_options(options) -> None`, `AnritsuPage._save_configured_manual_spectrum() -> None`, and a Fluent `configure_manual_spectrum` button.

- [ ] **Step 1: Add the failing production-facing state assertions from the tests.**

Use the new tests to establish that the page has an optional options slot, exposes a separate configure button, and keeps saving disabled until both a trace and options are present.

- [ ] **Step 2: Add the options state and separate Fluent controls.**

Import `ManualSpectrumSaveOptions`. Initialize `self._manual_save_options = None`. In the manual archive card, add a compact `PushButton("Configure archive…")` named `configure_manual_spectrum`; change the existing primary save label to `Save current spectrum` and keep `close_manual_archive` independent. Connect configure to the dialog-opening method and save to the direct save method. Add tooltips explaining that configuration is local to the archive session and save uses the accepted policy without opening a dialog.

- [ ] **Step 3: Make configuration only collect and store options.**

Keep `_show_manual_save_dialog` responsible for building the dialog and collecting current trace choices/confirmed metadata, but after `Accepted` call `_apply_manual_save_options(dialog.options())` and return. `_apply_manual_save_options` stores the immutable options, updates the card target with the configured path/policy, and calls `_update_manual_save_controls`. It must not instantiate `ManualSpectrumArchive` or touch the instrument. Let the dialog be configured before a trace by offering the raw trace variant as the baseline choice; later available averaged/processed variants remain selectable when configuration is reopened.

- [ ] **Step 4: Move the existing archive write body into the direct save method.**

Create `_save_configured_manual_spectrum`. It must guard missing options and missing trace with a card status message, resolve the stored `trace_variant` through `_manual_trace_payload`, lazily create the existing `ManualSpectrumArchive` with the same settings/device/operator providers, and call `archive.save` with every stored option plus processed values. On success retain `_manual_save_options`, update `_manual_archive_last_path`, `_manual_last_mode`, status/banner/target text, and refresh controls. On exception preserve `_manual_save_options`, leave the archive object recoverable, and use the existing error banner/status path.

- [ ] **Step 5: Update enablement and default display without changing persistence.**

Make `save_manual_spectrum` enabled only when `_latest_trace is not None` and `_manual_save_options is not None`. Keep `configure_manual_spectrum` available for archive policy changes and make its tooltip/status clear when no trace is yet available. Prefer the stored destination when reopening configuration; do not use a timestamped output path as the next timestamped base name. Keep `close_manual_archive` enabled only when an append writer has an active path.

- [ ] **Step 6: Run the focused regression tests green.**

Run:

```powershell
pytest tests/test_fluent_anritsu_moke_lakeshore_pages.py -k manual_archive -v
pytest tests/test_manual_spectrum_writer.py -v
```

Expected: all focused UI and writer tests pass, including the existing dialog rendering checks and actual HDF5 append behavior.

### Task 3: Verify the full affected surface and inspect the diff

**Files:**
- Modify: none unless verification finds a focused defect

**Interfaces:**
- Consumes the completed `AnritsuPage` behavior and existing storage contracts.
- Produces fresh evidence for UI rendering, persistence compatibility, lint, and repository cleanliness.

- [ ] **Step 1: Run all Anritsu/device UI regressions.**

```powershell
pytest tests/test_fluent_anritsu_moke_lakeshore_pages.py tests/test_fluent_device_pages.py tests/test_manual_spectrum_writer.py -v
```

- [ ] **Step 2: Run lint on application and tests.**

```powershell
ruff check app tests
```

- [ ] **Step 3: Run the broader relevant suite.**

```powershell
pytest tests/test_hdf5_writer.py tests/test_thatec_validator.py tests/test_thatec_reader.py tests/test_main_window.py -v
```

- [ ] **Step 4: Inspect changed files and final status.**

```powershell
git diff --check
git status --short
git diff -- app/devices/anritsu_ms2830a/ui/page.py tests/test_fluent_anritsu_moke_lakeshore_pages.py
```

Confirm there are no HDF5/schema changes, no instrument commands in archive actions, and no unrelated edits.

- [ ] **Step 5: Commit the implementation.**

```powershell
git add app/devices/anritsu_ms2830a/ui/page.py tests/test_fluent_anritsu_moke_lakeshore_pages.py
git commit -m "fix: separate Anritsu spectrum save configuration"
```
