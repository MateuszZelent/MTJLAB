# Task 3 report

Status: DONE (review fixes applied)

Added the pure `DeviceSweepProvider`/`CompiledAxisSetpoint` contract and registered Keithley, Rigol, and Anritsu providers through `RecipeExtension`. The registry now exposes enabled providers and rejects module-key mismatches. Providers bind legacy parameter actions, validate descriptor dimensions/endpoints and station limits, and compile SI values into existing Runner payload shapes with documented quantization.

Verification:
- RED provider contract tests failed before registration with missing `sweep_provider`/`sweep_providers` APIs.
- `python -m pytest -q tests/test_sweep_provider_contract.py --maxfail=10` — 5 passed.
- `python -m pytest -q tests/test_device_modules.py tests/test_recipe_semantic_tree.py tests/test_sweep_provider_contract.py --maxfail=10` — 31 passed, 6 subtests passed.

Review fixes: Rigol level updates now preserve the non-swept level from context/configuration; Anritsu provider uses local structural payload dataclasses and validates endpoint/target/dimension without importing adapters/VISA. Added regression tests for both.

Fix verification: `python -m pytest -q tests/test_sweep_provider_contract.py --maxfail=10` — 7 passed.

Commits: `4cda617` plus the follow-up provider-hardening checkpoint.
