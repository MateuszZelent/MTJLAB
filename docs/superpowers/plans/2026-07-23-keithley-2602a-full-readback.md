# Keithley 2602A Full Readback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the compact Keithley configuration readback with a complete, query-only 2602A audit and local JSON/YAML export.

**Architecture:** An explicit Keithley-package registry owns public documented TSP readback probes and parser metadata. The adapter returns an immutable snapshot with per-probe results; the existing Fluent page renders grouped rows and exports that already-acquired snapshot without more VISA traffic.

**Tech Stack:** Python, PySide6, PySide6-Fluent-Widgets, PyVISA/TSP, PyYAML, pytest, ruff.

## Global Constraints

- Audit queries never write to VISA, alter OUTPUT, clear `errorqueue`, or start trigger execution.
- Preserve physical values in SI under unit-bearing keys; format only in UI/export.
- Read `errorqueue.count` but never `errorqueue.next()`.
- Exclude user TSP programs, files, buffers and unbounded tables from the finite configuration audit.
- Render and test the Fluent dialog after `show()` and event processing at desktop and narrow widths.

---

### Task 1: Model the complete snapshot and documented probe registry

**Files:**

- Create: `app/devices/keithley_2600/readback.py`
- Modify: `app/devices/keithley_2600/__init__.py`
- Create: `tests/test_keithley_full_readback.py`

**Interfaces:** `KeithleyReadbackProbe(group, key, label, scope, query_template, parser, unit)`, `KeithleyReadbackValue(group, key, label, scope, value, unit, status, error=None)`, `KeithleyFullReadback(identity, captured_at_utc, values)`, and `KEITHLEY_2602A_READBACK_PROBES`.

- [ ] **Step 1: Write the failing serialization test**

```python
def test_full_readback_serializes_si_values_and_probe_statuses() -> None:
    snapshot = KeithleyFullReadback("KEITHLEY,2602A,123,2.1.6", "2026-07-23T12:00:00+00:00", (
        KeithleyReadbackValue("source", "source.leveli_a", "Source current", "B", 0.001, "A", "ok"),
        KeithleyReadbackValue("trigger", "trigger.count", "Trigger count", "B", None, None, "unsupported", "TSP error"),
    ))
    payload = snapshot.to_dict()
    assert payload["values"][0]["value"] == 0.001
    assert payload["values"][1]["status"] == "unsupported"
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_keithley_full_readback.py::test_full_readback_serializes_si_values_and_probe_statuses -v`

Expected: FAIL because the readback module does not exist.

- [ ] **Step 3: Implement immutable models and the registry**

```python
@dataclass(frozen=True, slots=True)
class KeithleyReadbackProbe:
    group: str
    key: str
    label: str
    scope: Literal["smu", "instrument"]
    query_template: str
    parser: Literal["float", "integer", "boolean", "text", "enum"]
    unit: str | None = None
```

Populate one registry with documented 2602A source, output-off, measurement, trigger, digio, local-node, and diagnostics probes. Use SI keys such as `source.leveli_a`, `source.limitv_v`, `measure.rangei_a`, and `source.delay_s`.

- [ ] **Step 4: Verify GREEN and commit**

Run: `pytest tests/test_keithley_full_readback.py -v`

Expected: PASS.

Run: `git add app/devices/keithley_2600/readback.py app/devices/keithley_2600/__init__.py tests/test_keithley_full_readback.py; git commit -m "feat: model Keithley full configuration readback"`

### Task 2: Execute probes through a query-only adapter operation

**Files:**

- Modify: `app/devices/keithley_2600/adapter.py`
- Modify: `app/devices/keithley_2600/module.py`
- Modify: `app/ui/workers.py`
- Modify: `tests/test_keithley_full_readback.py`

**Interfaces:** `KeithleyAdapter.read_full_configuration() -> KeithleyFullReadback`; device-module operation `read_full_configuration`; each unavailable or failed field is reported without failing the entire audit.

- [ ] **Step 1: Write failing safety and isolation tests**

```python
def test_full_readback_queries_both_smus_without_visa_writes() -> None:
    adapter, session = connected_keithley_adapter({"print(smub.source.leveli)": "0.001"})
    audit = adapter.read_full_configuration()
    assert audit.value("B", "source.leveli_a").value == 0.001
    assert all("source.output =" not in item for item in session.writes)
    assert "print(errorqueue.next())" not in session.writes

def test_full_readback_marks_one_unsupported_probe_and_continues() -> None:
    adapter, _ = connected_keithley_adapter(command_errors={"print(smub.trigger.count)": "TSP error"})
    audit = adapter.read_full_configuration()
    assert audit.value("B", "trigger.count").status == "unsupported"
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_keithley_full_readback.py -v`

Expected: FAIL because `read_full_configuration` is absent.

- [ ] **Step 3: Implement isolated query execution**

```python
def read_full_configuration(self) -> KeithleyFullReadback:
    session = self._require_session()
    values = tuple(self._read_probe(session, probe, scope) for probe in KEITHLEY_2602A_READBACK_PROBES for scope in (("A", "B") if probe.scope == "smu" else (None,)))
    return KeithleyFullReadback(self._identity_or_raise().raw, utc_now_iso(), values)
```

`_read_probe` only calls `session.query`; unknown-property TSP replies become `unsupported`, transport/malformed replies become `read_error`, and it never calls `emergency_off`.

- [ ] **Step 4: Verify GREEN and commit**

Run: `pytest tests/test_keithley_full_readback.py tests/test_adapters_and_runner.py -k "keithley and read" -v`

Expected: PASS; no test sees a VISA write after audit start.

Run: `git add app/devices/keithley_2600/adapter.py app/devices/keithley_2600/module.py app/ui/workers.py tests/test_keithley_full_readback.py; git commit -m "feat: query complete Keithley readback audit"`

### Task 3: Render grouped results, compare OFF mode, and export the snapshot

**Files:**

- Modify: `app/devices/keithley_2600/ui/page.py`
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_keithley_full_readback.py`

**Interfaces:** The page dispatches `read_full_configuration`; `_KeithleyReadbackDialog.export_snapshot(path: Path) -> None` writes its in-memory snapshot; `output.offmode` compares against `station.keithley.safety.output_off_mode` while no-peer values show `Device only`.

- [ ] **Step 1: Write failing dialog/export tests**

```python
def test_full_readback_dialog_marks_high_z_profile_mismatch_and_exports_yaml(tmp_path: Path) -> None:
    dialog = _KeithleyReadbackDialog(full_readback_with_offmode("normal"), snapshots, station, parent)
    dialog.show(); QApplication.processEvents()
    dialog.export_snapshot(tmp_path / "keithley-audit.yaml")
    assert dialog.row_for_key("output.offmode", "B").status_text == "MISMATCH: profile HIGH-Z"
    assert yaml.safe_load((tmp_path / "keithley-audit.yaml").read_text())["schema_version"] == 1
    assert dialog.isVisible() and dialog.width() > 700
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_main_window.py -k "keithley and full_readback" tests/test_keithley_full_readback.py -v`

Expected: FAIL because the full dialog/export path is missing.

- [ ] **Step 3: Implement grouped rows and export**

```python
def export_snapshot(self, path: Path) -> None:
    payload = self._readback.to_dict()
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False) if path.suffix.lower() in {".yaml", ".yml"} else json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
```

Use `StationFileDialog.getSaveFileName` with JSON/YAML filters. Full-audit rows are never assignable and export must not dispatch a controller call.

- [ ] **Step 4: Verify GREEN and commit**

Run: `pytest tests/test_main_window.py -k "keithley and (read or full_readback)" -v`

Expected: PASS with visible, non-zero desktop and narrow geometry; JSON/YAML payloads are valid.

Run: `git add app/devices/keithley_2600/ui/page.py tests/test_main_window.py tests/test_keithley_full_readback.py; git commit -m "feat: display and export Keithley full readback"`

### Task 4: Final safety and quality verification

- [ ] **Step 1: Run static analysis**

Run: `ruff check app tests`

Expected: PASS.

- [ ] **Step 2: Run all focused tests**

Run: `pytest tests/test_keithley_full_readback.py tests/test_adapters_and_runner.py tests/test_main_window.py -k "keithley or full_readback" -v`

Expected: PASS, including explicit proof of query-only audit traffic.

- [ ] **Step 3: Inspect final scope**

Run: `git status --short`

Expected: no unexpected files; commit only Task 4 corrections with `git commit -m "test: verify Keithley readback audit"`.
