"""Read public THATEC HDF5 results without relying on application extensions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.errors import ExecutionError


@dataclass(frozen=True, slots=True)
class ThatecDevice:
    name: str
    values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ThatecRecord:
    id: str
    values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ThatecRow:
    id: str
    device_name: str
    control_name: str
    function: str
    dimensions: int
    shape: tuple[int, ...]
    timestamp_count: int
    definition: tuple[tuple[str, str], ...]
    metadata: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ThatecTreeNode:
    id: str
    kind: str
    label: str
    children: tuple["ThatecTreeNode", ...] = ()


@dataclass(frozen=True, slots=True)
class ThatecRun:
    path: Path
    rows: dict[str, ThatecRow]
    devices: tuple[ThatecDevice, ...]
    labbook: tuple[ThatecRecord, ...]
    post_process: tuple[ThatecRecord, ...]
    recipe_source: str = ""
    settings_source: str = ""


@dataclass(frozen=True, slots=True)
class ThatecRowData:
    row_id: str
    checkpoint: int
    values: Any
    scale: tuple[float, ...] = ()


class ThatecRunReader:
    """Read the public `/scan_definition` and `/measurement` contract lazily."""

    @staticmethod
    def describe(path: str | Path) -> ThatecRun:
        target = Path(path)
        with ThatecRunReader._open(target) as file:
            rows = ThatecRunReader._rows_from_open_file(file)
            devices = ThatecRunReader._devices_from_open_file(file)
            labbook = ThatecRunReader._records_from_group(file.get("labbook"))
            post_process = ThatecRunReader._records_from_group(file.get("post-process"))
        recipe_source, settings_source = ThatecRunReader._embedded_sources(labbook)
        return ThatecRun(
            target, rows, devices, labbook, post_process,
            recipe_source=recipe_source, settings_source=settings_source,
        )

    @staticmethod
    def row(path: str | Path, row_id: str) -> ThatecRow:
        rows = ThatecRunReader.describe(path).rows
        try:
            return rows[row_id]
        except KeyError as exc:
            raise ExecutionError(f"THATEC scan row /scan_definition/{row_id} is missing.") from exc

    @staticmethod
    def tree(path: str | Path) -> tuple[ThatecTreeNode, ...]:
        with ThatecRunReader._open(Path(path)) as file:
            scan = ThatecRunReader._require_group(file, "scan_definition")
            tree_view = scan.get("tree_view")
            if tree_view is None:
                raise ExecutionError("THATEC file does not contain /scan_definition/tree_view.")
            rows = ThatecRunReader._rows_from_open_file(file)
            entries = tree_view.asstr()[()]
        return ThatecRunReader._tree_from_entries(entries, rows)

    @staticmethod
    def row_slice(path: str | Path, row_id: str, checkpoint: int) -> ThatecRowData:
        if checkpoint < 0:
            raise ExecutionError("THATEC checkpoint cannot be negative.")
        scale: tuple[float, ...] = ()
        with ThatecRunReader._open(Path(path)) as file:
            try:
                data = file[f"measurement/{row_id}/data"]
            except KeyError as exc:
                raise ExecutionError(f"THATEC data /measurement/{row_id}/data is missing.") from exc
            if data.ndim == 0:
                values = data[()]
            elif data.ndim == 1:
                if checkpoint >= data.shape[0]:
                    raise ExecutionError(f"THATEC checkpoint {checkpoint} is outside row {row_id}.")
                values = data[checkpoint : checkpoint + 1]
            else:
                if checkpoint >= data.shape[0]:
                    raise ExecutionError(f"THATEC checkpoint {checkpoint} is outside row {row_id}.")
                values = data[checkpoint, :]
            scale_dataset = file[f"measurement/{row_id}"].get("scale")
            if scale_dataset is not None and data.ndim >= 2:
                dimensions = data.ndim - 1
                start = checkpoint * 2 * (dimensions + 1)
                scale = tuple(float(value) for value in scale_dataset[start : start + 2 * (dimensions + 1)])
        return ThatecRowData(row_id, checkpoint, values, scale)

    @staticmethod
    def scalar_series(path: str | Path, row_id: str) -> tuple[Any, Any]:
        """Return one scalar row and its timestamps without touching other rows."""
        with ThatecRunReader._open(Path(path)) as file:
            group = file.get(f"measurement/{row_id}")
            if group is None or "data" not in group:
                raise ExecutionError(f"THATEC data /measurement/{row_id}/data is missing.")
            data = group["data"]
            if data.ndim != 1:
                raise ExecutionError(f"THATEC row {row_id} is not scalar.")
            return data[:], group.get("timestamp", ())[:]

    @staticmethod
    def _rows_from_open_file(file: Any) -> dict[str, ThatecRow]:
        scan = ThatecRunReader._require_group(file, "scan_definition")
        measurement = ThatecRunReader._require_group(file, "measurement")
        rows: dict[str, ThatecRow] = {}
        for name in sorted(scan):
            if not name.startswith("row_"):
                continue
            definition = ThatecRunReader._pairs(scan[name])
            values = dict(definition)
            group = measurement.get(name)
            if group is None or "data" not in group:
                shape: tuple[int, ...] = ()
                timestamp_count = 0
                metadata: tuple[tuple[str, str], ...] = ()
            else:
                shape = tuple(int(size) for size in group["data"].shape)
                timestamp_count = int(len(group.get("timestamp", ())))
                metadata = ThatecRunReader._pairs(group["metadata"]) if "metadata" in group else ()
            rows[name] = ThatecRow(
                id=name,
                device_name=values.get("device name", ""),
                control_name=values.get("control name", ""),
                function=values.get("function", ""),
                dimensions=int(values.get("dimensions", "0")),
                shape=shape,
                timestamp_count=timestamp_count,
                definition=definition,
                metadata=metadata,
            )
        return rows

    @staticmethod
    def _devices_from_open_file(file: Any) -> tuple[ThatecDevice, ...]:
        devices = file.get("devices")
        if devices is None:
            return ()
        return tuple(
            ThatecDevice(str(name), ThatecRunReader._pairs(dataset))
            for name, dataset in sorted(devices.items())
        )

    @staticmethod
    def _records_from_group(group: Any) -> tuple[ThatecRecord, ...]:
        if group is None:
            return ()
        records: list[ThatecRecord] = []
        for name, dataset in sorted(group.items()):
            if hasattr(dataset, "items"):
                records.append(
                    ThatecRecord(
                        str(name),
                        (("datasets", ", ".join(str(child) for child in dataset.keys())),),
                    )
                )
                continue
            if getattr(dataset, "ndim", 0) == 2 and dataset.shape[1] == 2:
                records.append(ThatecRecord(str(name), ThatecRunReader._pairs(dataset)))
        return tuple(records)

    @staticmethod
    def _embedded_sources(records: tuple[ThatecRecord, ...]) -> tuple[str, str]:
        """Read optional Lab Control source snapshots from the public labbook.

        The keys live in a normal THATEC two-column ``labbook/parameter`` table,
        so readers which do not know them can safely ignore them.
        """
        for record in records:
            if record.id != "parameter":
                continue
            values = dict(record.values)
            return (
                values.get("Lab Control recipe YAML", ""),
                values.get("Lab Control settings YAML", ""),
            )
        return "", ""

    @staticmethod
    def _tree_from_entries(entries: Any, rows: dict[str, ThatecRow]) -> tuple[ThatecTreeNode, ...]:
        roots: list[dict[str, Any]] = []
        stack: list[tuple[int, dict[str, Any]]] = []
        for entry in entries:
            raw_id, kind, label = (str(value) for value in entry)
            row_number = int(raw_id.replace("row", "").strip())
            row_id = f"row_{row_number:02d}"
            level = int(dict(rows[row_id].definition).get("tree indent level", "0")) if row_id in rows else 0
            node = {"id": row_id, "kind": kind, "label": label, "children": []}
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1]["children"].append(node)
            else:
                roots.append(node)
            stack.append((level, node))

        def freeze(node: dict[str, Any]) -> ThatecTreeNode:
            return ThatecTreeNode(node["id"], node["kind"], node["label"], tuple(freeze(child) for child in node["children"]))

        return tuple(freeze(node) for node in roots)

    @staticmethod
    def _pairs(dataset: Any) -> tuple[tuple[str, str], ...]:
        return tuple((str(key), str(value)) for key, value in dataset.asstr()[()])

    @staticmethod
    def _open(path: Path):
        try:
            import h5py
            return h5py.File(path, "r")
        except OSError as exc:
            raise ExecutionError(f"Cannot read THATEC file {path.name}: {exc}") from exc

    @staticmethod
    def _require_group(file: Any, name: str) -> Any:
        group = file.get(name)
        if group is None:
            raise ExecutionError(f"THATEC file does not contain /{name}.")
        return group
