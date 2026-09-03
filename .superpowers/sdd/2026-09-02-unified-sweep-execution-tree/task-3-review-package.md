# Task 3 review package

Base: `ad5873f`
Head: `4cda617`
Brief: `.superpowers/sdd/2026-09-02-unified-sweep-execution-tree/task-3-brief.md`
Report: `.superpowers/sdd/2026-09-02-unified-sweep-execution-tree/task-3-report.md`

Inspect:
`git diff -U10 ad5873f 4cda617 -- app/contracts app/devices/keithley_2600/sweep_provider.py app/devices/rigol_dg1000z/sweep_provider.py app/devices/anritsu_ms2830a/sweep_provider.py app/devices/keithley_2600/module.py app/devices/rigol_dg1000z/module.py app/devices/anritsu_ms2830a/module.py tests/test_sweep_provider_contract.py`

Review provider ownership, pure boundaries, module-key validation, dimensions/limits/quantization, and compatibility with existing Runner payloads. Return separate spec-compliance/task-quality verdicts and severity findings.
