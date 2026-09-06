# Quarantined Historical Scripts

This directory contains standalone scripts and dumps from early development:

- `dg1032z.py`: Early direct SCPI script for Rigol DG1032Z without station safety checks.
- `test.py`: Early interactive test script for Rigol DG1032Z communication.
- `tmp_ms2830.txt`: Raw spectrum analyzer diagnostic capture dump.

These scripts are kept for historical reference and must NOT be imported or used in the production station runtime. Production instrument control must always use the qualified adapter modules under `app/devices/` subject to safety interlocks and limits.
