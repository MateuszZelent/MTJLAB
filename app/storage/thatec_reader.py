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


@dataclass(frozen=True, slots=True)
class ThatecSpectrumTrace:
    """One displayable trace extracted from a public THATEC spectrum row."""

    name: str
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ThatecSpectrum:
    """A read-only spectrum slice with its public coordinate metadata."""

    row_id: str
    checkpoint: int
    x_label: str
    x_unit: str
    y_label: str
    y_unit: str
    x_values: tuple[float, ...]
    traces: tuple[ThatecSpectrumTrace, ...]
    source_shape: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _AxisMetadata:
    name: str
    unit: str
    offset: float
    multiplier: float


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
    def spectrum_slice(
        path: str | Path, row_id: str, checkpoint: int
    ) -> ThatecSpectrum:
        """Read one public multi-trace spectrum without relying on private groups.

        THATEC stores a measurement checkpoint before its declared data axes.
        A VNA can therefore be represented as either ``checkpoint × trace ×
        frequency`` or ``checkpoint × frequency × trace``.  The method uses
        the published axis names and scale metadata when present, and falls
        back to the largest axis only when the file does not identify a
        frequency coordinate explicitly.
        """

        import numpy as np

        row = ThatecRunReader.row(path, row_id)
        data = ThatecRunReader.row_slice(path, row_id, checkpoint)
        try:
            raw_values = np.asarray(data.values)
            if np.iscomplexobj(raw_values):
                raise ExecutionError(
                    f"THATEC row {row_id} contains complex samples; select a real-valued "
                    "component stored by the producer before plotting it."
                )
            values = np.asarray(raw_values, dtype=float)
        except (TypeError, ValueError) as exc:
            raise ExecutionError(
                f"THATEC row {row_id} cannot be interpreted as real-valued spectrum data."
            ) from exc
        if values.ndim not in {1, 2}:
            raise ExecutionError(
                f"THATEC row {row_id} has {values.ndim} data axes per checkpoint; "
                "the spectrum browser supports one signal axis and one optional trace axis."
            )
        if values.size == 0:
            raise ExecutionError(f"THATEC row {row_id} contains an empty spectrum.")

        axes = ThatecRunReader._axis_metadata(row.metadata)
        frequency_axis = ThatecRunReader._spectrum_axis_index(values.shape, axes)
        coordinate = axes[frequency_axis] if frequency_axis < len(axes) else _AxisMetadata(
            "Sample", "", 0.0, 1.0
        )
        x_offset, x_multiplier = ThatecRunReader._scale_pair(
            data.scale, frequency_axis, coordinate
        )
        x_values = x_offset + x_multiplier * np.arange(
            values.shape[frequency_axis], dtype=float
        )
        x_values, x_unit = ThatecRunReader._normalise_frequency_axis(
            x_values, coordinate.unit
        )

        value_axis = axes[values.ndim] if len(axes) > values.ndim else _AxisMetadata(
            "Amplitude", "", 0.0, 1.0
        )
        y_offset, y_multiplier = ThatecRunReader._scale_pair(
            data.scale, values.ndim, value_axis
        )
        values = y_offset + y_multiplier * values

        if values.ndim == 1:
            traces = (
                ThatecSpectrumTrace(
                    row.control_name or row.id,
                    tuple(float(value) for value in values),
                ),
            )
        else:
            trace_axis = 1 - frequency_axis
            trace_metadata = (
                axes[trace_axis]
                if trace_axis < len(axes)
                else _AxisMetadata("Trace", "", 0.0, 1.0)
            )
            labels = ThatecRunReader._trace_labels(
                trace_metadata.name, values.shape[trace_axis]
            )
            traces = tuple(
                ThatecSpectrumTrace(
                    labels[index],
                    tuple(
                        float(value)
                        for value in (
                            values[:, index]
                            if frequency_axis == 0
                            else values[index, :]
                        )
                    ),
                )
                for index in range(values.shape[trace_axis])
            )

        return ThatecSpectrum(
            row_id=row_id,
            checkpoint=checkpoint,
            x_label=coordinate.name or "Sample",
            x_unit=x_unit,
            y_label=value_axis.name or "Amplitude",
            y_unit=ThatecRunReader._display_unit(value_axis.unit),
            x_values=tuple(float(value) for value in x_values),
            traces=traces,
            source_shape=tuple(int(size) for size in values.shape),
        )

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
    def _axis_metadata(
        metadata: tuple[tuple[str, str], ...]
    ) -> tuple[_AxisMetadata, ...]:
        """Group THATEC's repeated ``name/unit/offset/multiplier`` fields."""

        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for key, value in metadata:
            normalized_key = key.strip().casefold()
            if normalized_key == "name" and current:
                records.append(current)
                current = {}
            current[normalized_key] = value
        if current:
            records.append(current)
        return tuple(
            _AxisMetadata(
                name=record.get("name", ""),
                unit=record.get("unit", ""),
                offset=ThatecRunReader._metadata_float(record.get("offset"), 0.0),
                multiplier=ThatecRunReader._metadata_float(
                    record.get("multiplier"), 1.0
                ),
            )
            for record in records
        )

    @staticmethod
    def _spectrum_axis_index(
        shape: tuple[int, ...], axes: tuple[_AxisMetadata, ...]
    ) -> int:
        for index, axis in enumerate(axes[: len(shape)]):
            name = axis.name.casefold()
            if "frequency" in name or name in {"freq", "f"}:
                return index
        return max(range(len(shape)), key=lambda index: shape[index])

    @staticmethod
    def _scale_pair(
        scale: tuple[float, ...], index: int, fallback: _AxisMetadata
    ) -> tuple[float, float]:
        scale_index = 2 * index
        if scale_index + 1 < len(scale):
            return scale[scale_index], scale[scale_index + 1]
        return fallback.offset, fallback.multiplier

    @staticmethod
    def _normalise_frequency_axis(values: Any, unit: str) -> tuple[Any, str]:
        normalized = unit.strip().casefold().replace("μ", "u").replace("µ", "u")
        scales = {
            "hz": 1.0,
            "khz": 1e3,
            "mhz": 1e6,
            "ghz": 1e9,
        }
        scale = scales.get(normalized)
        if scale is None:
            return values, ThatecRunReader._display_unit(unit)
        return values * scale, "Hz"

    @staticmethod
    def _trace_labels(name: str, count: int) -> tuple[str, ...]:
        label = name.strip() or "Trace"
        start, separator, end = label.partition("(")
        candidates = end.removesuffix(")").split(",") if separator else []
        candidates = [candidate.strip() for candidate in candidates]
        if len(candidates) == count and all(candidate and candidate != "..." for candidate in candidates):
            return tuple(candidates)
        return tuple(f"{start.strip() or 'Trace'} {index + 1}" for index in range(count))

    @staticmethod
    def _metadata_float(value: str | None, fallback: float) -> float:
        try:
            return float(value) if value is not None else fallback
        except ValueError:
            return fallback

    @staticmethod
    def _display_unit(unit: str) -> str:
        return "" if unit.strip() in {"", "-"} else unit.strip()

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
