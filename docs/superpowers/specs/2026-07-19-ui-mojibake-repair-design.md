# UI Mojibake Repair

## Goal

Restore corrupted UTF-8 characters in production source files and prevent
mojibake from reaching the UI again.

## Confirmed scope

The repository scan found corrupted text in exactly four production files:

- `app/devices/rigol_dg1000z/ui/page.py`
- `app/devices/rigol_dg1000z/ui/recipe_dialog.py`
- `app/engine/compiler.py`
- `app/ui/workers.py`

The visible Rigol problems include plot titles, the page subtitle, the device
status symbol, warnings, degree/minus signs, dialog labels, and ellipses.
Compiler and worker text contain the same encoding defect even though those
strings were not shown in the screenshots.

## Repair

Replace only confirmed mojibake sequences with their intended Unicode text:

- `Â·` → `·`
- `â€”` → `—`
- `â€¦` → `…`
- corrupted black-circle bytes → `●`
- corrupted warning-sign bytes → `⚠`
- `âˆ’` → `−`
- `Â°` → `°`
- `â€“` → `–`
- `faÃ§ade` → `façade`

No labels, behavior, layout, safety limits, device commands, or persisted keys
change.

## Prevention

An architecture test scans Python source files under `app/` and fails when it
finds common mojibake markers: `Â`, `Ã`, `â`, or the replacement character
`�`. The failure identifies the exact repository-relative path.

This deliberately checks source text rather than rendered widgets so it covers
UI labels, exceptions, documentation strings, and dynamically constructed plot
titles.

## Verification

- Run the new architecture test red before repairing source.
- Repair the four files.
- Run architecture, Rigol UI, compiler, and worker-related tests.
- Scan all Python sources under `app/` for mojibake markers.
- Run the full test suite before completion.

## Completion criteria

The repository-wide scan reports no corrupted markers, Rigol labels contain the
intended symbols, focused tests pass, and the full test suite has no failures.
