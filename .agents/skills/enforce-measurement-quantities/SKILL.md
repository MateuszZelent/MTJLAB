---
name: enforce-measurement-quantities
description: Implement or review physical quantities, units, SI normalization, engineering prefixes, scientific notation, logarithmic dB/dBm values, sweep axes, plot labels, settings, recipes, and instrument protocol conversions in this measurement station. Use whenever numeric measurement or setpoint values cross UI, YAML, compiler, adapter, storage, CSV, HDF5, or plotting boundaries, especially to prevent exponent, prefix, dimension, and implicit-unit errors.
---

# Enforce Measurement Quantities

Preserve a quantity's dimension, unit, scale, sign, and logarithmic meaning across every boundary. A numerically plausible value in the wrong scale is a failure.

Read [references/quantity-contract.md](references/quantity-contract.md) before changing quantity behavior or adding a dimension or unit.

## Follow the boundary rule

1. Accept human-authored settings and recipes as explicit number-plus-unit strings.
2. Parse them with `app.domain.quantities.parse_quantity` and the expected dimension.
3. Normalize once to SI inside typed domain, compiler, safety, and adapter request models.
4. Name SI floats with unit suffixes such as `_hz`, `_v`, `_a`, `_s`, `_t`, or `_dbm`.
5. Convert to instrument wire units only at the protocol/adapter boundary.
6. Persist numeric data with an explicit unit or a stable unit-bearing key; format for humans only at the UI/export boundary.

Never compare differently scaled raw values. Never apply an engineering-prefix multiplier twice.

## Treat dB and dBm specially

- Keep dB, dBm, and linear power as different dimensions.
- Do not apply SI prefix scaling to dB or dBm.
- Do not add or subtract dBm as though it were watts.
- Require an explicit, reviewed conversion formula when moving between logarithmic and linear quantities.

## Add or change a quantity

Update `app/domain/quantities.py`, `app/recipes/parameter_registry.py`, settings/recipe validation, compiler payloads, adapter models, persisted metadata, plot labels, and their tests as applicable. Do not create a second unit table in a page, dialog, or adapter.

## Review exponents and prefixes

For every factor such as `1e-3`, `1e6`, `/ 1000`, or `* 1_000_000`, identify both source and destination units. Replace unexplained magic scaling with a named conversion or quantity parser. Check lowercase `m` versus uppercase `M`, micro aliases, decimal comma input, negative values, zero, and exponent notation.

Use relative or scale-aware comparisons where instrument precision warrants them; use exact comparisons for integer counts and stable serialized strings.

## Fail closed

Reject missing units for human-authored safety values, unknown units, dimension mismatch, NaN, infinity, reversed ranges, and values outside device and DUT limits. Error messages must name the received value, expected dimension/unit, and violated limit without silently correcting the input.

## Verification checklist

- Test small, base-unit, and large-prefixed values.
- Test scientific notation with an explicit unit and signed exponent.
- Test a wrong-dimension value that is numerically plausible.
- Test exact serialized unit metadata or key names.
- Test UI/editor display and parse-back when formatting changes.
- Run affected quantity, recipe, adapter, storage, and plot tests.
