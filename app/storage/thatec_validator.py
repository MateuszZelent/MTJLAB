"""Versioned structural validation for thaTEC:OS/PyThat HDF5 files."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from io import StringIO
import json
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ThatecCompatibilityReport:
    path: Path
    manifest_version: int
    errors: tuple[CompatibilityIssue, ...]
    warnings: tuple[CompatibilityIssue, ...]
    pythat_version: str | None = None
    dimensions: tuple[tuple[str, int], ...] = ()
    data_variables: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> "ThatecCompatibilityReport":
        if self.errors:
            detail = "; ".join(f"{issue.path}: {issue.message}" for issue in self.errors)
            raise ValueError(f"Incompatible thaTEC/PyThat file: {detail}")
        return self


class ThatecCompatibilityValidator:
    """Validate the stable public schema and optional PyThat round-trip."""

    def __init__(self, manifest: dict[str, Any] | None = None) -> None:
        self.manifest = manifest or self.load_packaged_manifest()

    @staticmethod
    def load_packaged_manifest() -> dict[str, Any]:
        resource = files("app.resources").joinpath("thatec_manifest_v1.json")
        return json.loads(resource.read_text(encoding="utf-8"))

    def validate(
        self,
        path: str | Path,
        *,
        require_pythat: bool = False,
    ) -> ThatecCompatibilityReport:
        import h5py

        target = Path(path)
        errors: list[CompatibilityIssue] = []
        warnings: list[CompatibilityIssue] = []
        dimensions: tuple[tuple[str, int], ...] = ()
        data_variables: tuple[str, ...] = ()
        pythat_version: str | None = None
        try:
            handle = h5py.File(target, "r")
        except Exception as exc:
            errors.append(CompatibilityIssue("/", f"cannot open HDF5: {exc}"))
            return self._report(target, errors, warnings)

        with handle as h5:
            self._validate_root(h5, errors)
            self._validate_required_tables(h5, errors)
            self._validate_devices(h5, errors, warnings)
            self._validate_scan_and_measurement(h5, errors, warnings)
            self._validate_private_run_state(h5, errors)

        if require_pythat and not errors:
            try:
                from PyThat import MeasurementTree

                try:
                    pythat_version = version("PyThat")
                except PackageNotFoundError:
                    pythat_version = "unknown"
                qualified = str(self.manifest["qualified_pythat_version"])
                if pythat_version != qualified:
                    errors.append(
                        CompatibilityIssue(
                            "PyThat",
                            f"version {pythat_version!r} is not qualified; expected {qualified!r}",
                        )
                    )
                with redirect_stdout(StringIO()):
                    tree = MeasurementTree(target, index=True, override=True)
                dimensions = tuple((str(name), int(size)) for name, size in tree.dataset.sizes.items())
                data_variables = tuple(str(name) for name in tree.dataset.data_vars)
            except Exception as exc:
                errors.append(CompatibilityIssue("PyThat", f"round-trip failed: {exc}"))
        return self._report(
            target,
            errors,
            warnings,
            pythat_version=pythat_version,
            dimensions=dimensions,
            data_variables=data_variables,
        )

    def verify_golden_reference(self, path: str | Path) -> ThatecCompatibilityReport:
        target = Path(path)
        report = self.validate(target, require_pythat=True)
        errors = list(report.errors)
        golden = self.manifest["golden_reference"]
        if target.name != golden["filename"]:
            errors.append(
                CompatibilityIssue(
                    "/",
                    f"golden filename differs: {target.name!r} != {golden['filename']!r}",
                )
            )
        if target.is_file():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest.lower() != str(golden["sha256"]).lower():
                errors.append(CompatibilityIssue("/", "golden SHA-256 differs from manifest"))
            try:
                import h5py

                with h5py.File(target, "r") as h5:
                    count = sum(
                        bool(re.fullmatch(r"row_[0-9]+", name))
                        for name in h5["scan_definition"]
                    )
                if count < int(golden["minimum_scan_rows"]):
                    errors.append(
                        CompatibilityIssue(
                            "/scan_definition",
                            f"golden has {count} rows; expected at least {golden['minimum_scan_rows']}",
                        )
                    )
            except Exception as exc:
                errors.append(
                    CompatibilityIssue("/scan_definition", f"cannot inventory golden rows: {exc}")
                )
        return ThatecCompatibilityReport(
            path=report.path,
            manifest_version=report.manifest_version,
            errors=tuple(errors),
            warnings=report.warnings,
            pythat_version=report.pythat_version,
            dimensions=report.dimensions,
            data_variables=report.data_variables,
        )

    def _validate_root(self, h5: Any, errors: list[CompatibilityIssue]) -> None:
        import numpy as np

        for name, rule in self.manifest["required_root_attributes"].items():
            if name not in h5.attrs:
                errors.append(CompatibilityIssue("/", f"missing root attribute {name!r}"))
                continue
            value = h5.attrs[name]
            if rule["kind"] == "integer" and not isinstance(value, (int, np.integer)):
                errors.append(CompatibilityIssue("/", f"attribute {name!r} is not integer"))
            if rule["kind"] == "string" and not isinstance(value, (str, bytes)):
                errors.append(CompatibilityIssue("/", f"attribute {name!r} is not text"))
            if "allowed" in rule and int(value) not in rule["allowed"]:
                errors.append(CompatibilityIssue("/", f"attribute {name!r} has invalid value {value!r}"))
        for group in self.manifest["required_groups"]:
            if group not in h5 or not hasattr(h5[group], "keys"):
                errors.append(CompatibilityIssue(f"/{group}", "required group is missing"))

    def _validate_required_tables(self, h5: Any, errors: list[CompatibilityIssue]) -> None:
        for path, rule in self.manifest["required_tables"].items():
            if path not in h5:
                errors.append(CompatibilityIssue(f"/{path}", "required table is missing"))
                continue
            self._validate_table(h5[path], f"/{path}", set(rule["columns"]), errors)

    @staticmethod
    def _validate_table(
        dataset: Any,
        path: str,
        allowed_columns: set[int],
        errors: list[CompatibilityIssue],
    ) -> None:
        if not hasattr(dataset, "shape") or len(dataset.shape) != 2:
            errors.append(CompatibilityIssue(path, "must be a rank-2 table"))
            return
        if int(dataset.shape[1]) not in allowed_columns:
            errors.append(
                CompatibilityIssue(path, f"must have columns in {sorted(allowed_columns)}")
            )
        if dataset.dtype.kind not in {"O", "S", "U"}:
            errors.append(CompatibilityIssue(path, "must use a string-compatible dtype"))

    @staticmethod
    def _table_mapping(dataset: Any) -> dict[str, str]:
        if len(dataset.shape) != 2 or dataset.shape[1] != 2:
            return {}
        return {str(key): str(value) for key, value in dataset.asstr()[()]}

    def _validate_devices(
        self,
        h5: Any,
        errors: list[CompatibilityIssue],
        warnings: list[CompatibilityIssue],
    ) -> None:
        if "devices" not in h5:
            return
        if not h5["devices"]:
            warnings.append(CompatibilityIssue("/devices", "contains no device identity tables"))
        for name, dataset in h5["devices"].items():
            self._validate_table(dataset, f"/devices/{name}", {2}, errors)

    def _validate_scan_and_measurement(
        self,
        h5: Any,
        errors: list[CompatibilityIssue],
        warnings: list[CompatibilityIssue],
    ) -> None:
        if "scan_definition" not in h5 or "measurement" not in h5:
            return
        rule = self.manifest["scan_definition"]
        pattern = re.compile(rule["row_pattern"])
        rows = sorted(name for name in h5["scan_definition"] if pattern.match(name))
        tree = h5["scan_definition"].get("tree_view")
        if tree is not None and tree.shape[0] != len(rows):
            errors.append(
                CompatibilityIssue(
                    "/scan_definition/tree_view",
                    f"contains {tree.shape[0]} entries for {len(rows)} definition rows",
                )
            )
        if not rows:
            warnings.append(CompatibilityIssue("/scan_definition", "contains no scan rows"))
        for row_name in rows:
            definition_path = f"/scan_definition/{row_name}"
            dataset = h5["scan_definition"][row_name]
            self._validate_table(dataset, definition_path, {2}, errors)
            definition = self._table_mapping(dataset)
            for key in rule["base_keys"]:
                if key not in definition:
                    errors.append(CompatibilityIssue(definition_path, f"missing key {key!r}"))
            function = definition.get("function", "")
            internal = function.startswith("internal")
            if not internal:
                for key in rule["device_row_keys"]:
                    if key not in definition:
                        errors.append(
                            CompatibilityIssue(definition_path, f"missing key {key!r}")
                        )
            measurement_path = f"measurement/{row_name}"
            if function == rule["indicator_function"] and measurement_path not in h5:
                errors.append(
                    CompatibilityIssue(f"/{measurement_path}", "indicator data group is missing")
                )
            if measurement_path in h5:
                self._validate_measurement_row(
                    h5[measurement_path],
                    f"/{measurement_path}",
                    definition,
                    errors,
                )

    def _validate_measurement_row(
        self,
        group: Any,
        path: str,
        definition: dict[str, str],
        errors: list[CompatibilityIssue],
    ) -> None:
        rule = self.manifest["measurement_row"]
        for name in rule["required_datasets"]:
            if name not in group:
                errors.append(CompatibilityIssue(f"{path}/{name}", "required dataset is missing"))
        if "data" not in group or "timestamp" not in group:
            return
        data = group["data"]
        timestamp = group["timestamp"]
        if data.dtype.kind not in {"f", "i", "u"}:
            errors.append(CompatibilityIssue(f"{path}/data", "must be numeric"))
        if timestamp.dtype.kind != "f" or len(timestamp.shape) != 1:
            errors.append(CompatibilityIssue(f"{path}/timestamp", "must be a rank-1 float array"))
        if data.shape and timestamp.shape and data.shape[0] != timestamp.shape[0]:
            errors.append(
                CompatibilityIssue(path, "data checkpoint dimension differs from timestamp length")
            )
        for attribute in rule["data_attributes"]:
            if attribute not in data.attrs:
                errors.append(CompatibilityIssue(f"{path}/data", f"missing attribute {attribute!r}"))
        try:
            dimensions = int(data.attrs.get("dim of data", -1))
        except (TypeError, ValueError):
            dimensions = -1
        if dimensions != max(0, len(data.shape) - 1):
            errors.append(
                CompatibilityIssue(
                    f"{path}/data",
                    f"dim of data={dimensions} does not match rank {len(data.shape)}",
                )
            )
        if definition.get("dimensions") not in {None, str(dimensions)}:
            errors.append(CompatibilityIssue(path, "definition and data dimensions differ"))
        if definition.get("data type") not in {None, str(data.attrs.get("data type"))}:
            errors.append(CompatibilityIssue(path, "definition and data type differ"))
        if dimensions > 0:
            for name in rule["multidimensional_datasets"]:
                if name not in group:
                    errors.append(CompatibilityIssue(f"{path}/{name}", "required for array data"))
            if "metadata" in group:
                self._validate_table(group["metadata"], f"{path}/metadata", {2}, errors)
            if "scale" in group:
                expected = data.shape[0] * 2 * (dimensions + 1)
                if len(group["scale"]) != expected:
                    errors.append(
                        CompatibilityIssue(
                            f"{path}/scale",
                            f"contains {len(group['scale'])} values, expected {expected}",
                        )
                    )

    @staticmethod
    def _validate_private_run_state(h5: Any, errors: list[CompatibilityIssue]) -> None:
        if "run" not in h5:
            return
        status = h5["run"].attrs.get("status")
        if isinstance(status, bytes):
            status = status.decode("utf-8", errors="replace")
        if status in {"completed", "aborted", "faulted"} and int(
            h5.attrs.get("measurement running", 1)
        ) != 0:
            errors.append(
                CompatibilityIssue("/run", f"closed run {status!r} is still marked running")
            )

    def _report(
        self,
        path: Path,
        errors: list[CompatibilityIssue],
        warnings: list[CompatibilityIssue],
        *,
        pythat_version: str | None = None,
        dimensions: tuple[tuple[str, int], ...] = (),
        data_variables: tuple[str, ...] = (),
    ) -> ThatecCompatibilityReport:
        return ThatecCompatibilityReport(
            path=path,
            manifest_version=int(self.manifest["manifest_version"]),
            errors=tuple(errors),
            warnings=tuple(warnings),
            pythat_version=pythat_version,
            dimensions=dimensions,
            data_variables=data_variables,
        )
