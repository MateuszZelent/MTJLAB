# Anritsu Fresh Spectrum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every manual Anritsu spectrum request acquire a newly completed hardware sweep.

**Architecture:** The UI selects the existing qualified `single_sweep` worker
operation for manual acquisition.  The adapter continues to own SCPI command
ordering and completion polling.  Live remains Continuous plus passive reads.

**Tech Stack:** Python 3.14, PySide6, PyVISA, unittest/pytest.

## Global Constraints

- Do not change spectrum settings or energize Anritsu RF output.
- Preserve the Fluent-native UI shell and existing user workflows.
- Keep the existing Single sweep qualification gate and timeout.

---

### Task 1: Regress manual spectrum request to an owned single sweep

**Files:**
- Modify: `tests/test_fluent_anritsu_moke_lakeshore_pages.py`
- Modify: `app/devices/anritsu_ms2830a/ui/page.py`

**Interfaces:**
- Consumes: `AnritsuPage._controller.call(operation, trace)`.
- Produces: manual `read_once()` dispatches `single_sweep`, `TRAC1`.

- [ ] **Step 1: Write the failing test**

```python
page.read_once()
controller.call.assert_called_once_with("single_sweep", "TRAC1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fluent_anritsu_moke_lakeshore_pages.py -k manual -v`

- [ ] **Step 3: Write minimal implementation**

```python
self._controller.call("single_sweep", "TRAC1")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fluent_anritsu_moke_lakeshore_pages.py -k manual -v`

### Task 2: Preserve protocol and validate against hardware

**Files:**
- Test: `tests/test_simulators.py`

- [ ] **Step 1: Assert two single-sweep traces differ in the simulator**
- [ ] **Step 2: Run focused adapter/UI tests and `ruff check app tests`**
- [ ] **Step 3: Repeat three single-sweep acquisitions on the qualified MS2830A and record hashes**
