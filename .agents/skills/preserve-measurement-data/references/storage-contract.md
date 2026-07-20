# Storage contract

## Source map

- `app/storage/hdf5_writer.py`: private state, checkpoints, events, CSV, close validation.
- `app/storage/thatec_writer.py`: public thaTEC representation.
- `app/storage/hdf5_reader.py`, `thatec_reader.py`, `pythat_reader.py`: readers.
- `app/storage/thatec_schema_mapper.py`: definition-to-data mapping.
- `app/storage/thatec_validator.py`: manifest and PyThat validation.
- `app/resources/thatec_manifest_v1.json`: public contract and golden identity.
- `app/domain/models.py`: `MeasurementPoint` boundary model.

## Representations

| Surface | Purpose | Expectation |
| --- | --- | --- |
| `/measurement`, `/scan_definition`, `/devices`, `/labbook` | public data | Stable external contract |
| `/run` | identity and terminal/recovery state | Stable application contract |
| `/points`, `/spectra` | committed private checkpoints | Contiguous and complete |
| `/_pending` | transaction workspace | Empty after clean commit/recovery |
| `/events` | audit/engine events | Append-only, immediately flushed |
| CSV summary | rebuildable index | Never the authority |

## Non-negotiable invariants

Create new files exclusively; keep indices contiguous across representations; expose only complete validated points; keep public/private final statuses consistent; truncate every representation to one recovery boundary; test readers independently; retain expected ranks, dtypes, dimensions, scales, and timestamps; retain provenance sufficient to explain request, devices, safety envelope, and simulation state.
