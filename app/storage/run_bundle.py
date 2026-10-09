"""Numbered, isolated sweep directories and portable run-local artefacts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from app.domain.errors import ExecutionError
from app.storage.naming import sanitize_run_file_stem


SCHEMA = "lab-control-run-bundle-v1"
_NUMBER = re.compile(r"^([1-9][0-9]*)_\d{8}T\d{6}Z_")


def json_value(value):
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((json_value(item) for item in value), key=str)
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, (Path, datetime)):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _next_number(root: Path) -> int:
    number = 0
    if root.exists():
        for child in root.iterdir():
            match = _NUMBER.match(child.name)
            if match and child.is_dir():
                number = max(number, int(match[1]))
        counter = root / ".sweep-sequence"
        if counter.exists():
            value = counter.read_text(encoding="ascii").strip()
            if value:
                number = max(number, int(value))
    return number + 1


@contextmanager
def _sequence_lock(root: Path):
    """OS-owned lock: released even if the worker/process crashes."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".sweep-lock").open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 5
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ExecutionError(f"Cannot reserve a sweep number in {root}: {exc}") from exc
                time.sleep(0.02)
        try:
            yield stream
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _folder(root: Path, number: int, timestamp: datetime, label: str) -> Path:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ExecutionError("Sweep directory timestamps require an explicit timezone.")
    prefix = f"{number}_{timestamp.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_"
    clean = sanitize_run_file_stem(label).strip(".") or "run"
    # Keep room for references/bg0000_<hash>.h5 on Windows installations
    # without long-path support. Never repeat the sample name in each file.
    root_length = len(str(root.resolve()).encode("utf-16-le")) // 2
    budget = min(80, 245 - root_length - 1 - 32)
    remaining = budget - len(prefix)
    if remaining < 10:
        raise ExecutionError(
            f"Result directory is too long for a portable sweep bundle: {root}. "
            "Choose a shorter sample/catalogue directory."
        )
    if len(clean) > remaining:
        digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:8]
        clean = clean[:remaining - 9].rstrip("_.-") + "_" + digest
    return root / (prefix + clean)


def bundle_directory(root: Path, label: str, timestamp: datetime, *, reserve=False) -> Path:
    """Preview is read-only; execution reserves a unique number atomically."""
    if not reserve:
        return _folder(root, _next_number(root), timestamp, label)
    with _sequence_lock(root):
        number = _next_number(root)
        directory = _folder(root, number, timestamp, label)
        directory.mkdir(exist_ok=False)
        _atomic_json(root / ".sweep-sequence", number)
        return directory


def _atomic_json(path: Path, value) -> None:
    encoded = json.dumps(json_value(value), ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".meta-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class RunBundle:
    """Companion files for one run; the main HDF5 remains the commit authority."""

    def __init__(self, data_path: Path):
        self.data_path = Path(data_path)
        self.directory = self.data_path.parent
        self.manifest_path = self.directory / "metadata.json"

    def initialize(self, *, plan, settings_source, operator_context, sample_target,
                   simulation_metadata, display_name) -> None:
        manifest = {
            "schema": SCHEMA,
            "sweep_number": int(self.directory.name.split("_", 1)[0]),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "recipe_name": plan.recipe_name,
            "display_name": display_name,
            "plan_sha256": plan.sha256,
            "recipe_sha256": hashlib.sha256(plan.recipe_source.encode("utf-8")).hexdigest(),
            "settings_sha256": hashlib.sha256(settings_source.encode("utf-8")).hexdigest(),
            "sample_target": json_value(sample_target),
            "operator_context": operator_context,
            "simulation": simulation_metadata,
            "status": "preparing",
            "execution_attempts": 0,
            "provenance_ready": False,
            "data_file": self.data_path.name,
            "references": [],
            "files": {"recipe": "recipe.yml", "settings": "settings.yml", "plan": "plan.json"},
        }
        _atomic_json(self.manifest_path, manifest)
        for name, content in (("recipe.yml", plan.recipe_source), ("settings.yml", settings_source)):
            with (self.directory / name).open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # The exclusive directory reservation makes these new immutable files.
        with (self.directory / "plan.json").open("x", encoding="utf-8") as stream:
            json.dump(json_value(plan), stream, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        manifest["provenance_ready"] = True
        _atomic_json(self.manifest_path, manifest)

    def fail(self, error: str) -> None:
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest.update(status="faulted", error=error,
                            updated_at_utc=datetime.now(timezone.utc).isoformat())
            _atomic_json(self.manifest_path, manifest)

    def verify_identity(self, plan) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("schema") != SCHEMA or manifest.get("data_file") != self.data_path.name
                or manifest.get("plan_sha256") != plan.sha256):
            raise ExecutionError("Run bundle metadata does not match the recovery plan/file.")

    def mark_running(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest.update(status="running", error=None,
                        execution_attempts=int(manifest.get("execution_attempts", 0)) + 1,
                        last_started_at_utc=datetime.now(timezone.utc).isoformat())
        _atomic_json(self.manifest_path, manifest)

    def finalize(self, *, execution_state: str, error: str | None) -> None:
        """Export only committed baselines, after the main writer has closed."""
        import h5py

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        with h5py.File(self.data_path, "r") as source:
            run = source["run"]
            references = []
            for key in sorted(source.get("references", ()), key=int):
                group = source[f"references/{key}"]
                if not bool(group.attrs.get("complete", False)):
                    raise ExecutionError("Bundle export encountered an uncommitted reference.")
                references.append(self._export_reference(source, group, int(key)))
            events = source["events"]
            count = int(events.attrs.get("committed_count", 0))
            # Rebuildable JSONL, streaming one record at a time rather than
            # loading a long sweep's event history into the GUI or memory.
            temporary = self.directory / ".events.tmp"
            with temporary.open("w", encoding="utf-8") as stream:
                for index in range(count):
                    event = {name: events[name].asstr()[index]
                             for name in ("timestamp", "severity", "name", "message")}
                    event["payload"] = json.loads(event.pop("message"))
                    stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.directory / "events.jsonl")
            manifest.update(
                status=str(run.attrs["status"]), execution_state=execution_state, error=error,
                updated_at_utc=datetime.now(timezone.utc).isoformat(), references=references,
                point_count=len(source["points"]),
                device_idn=json.loads(run["device_idn_json"].asstr()[()]),
                device_capabilities=json.loads(run["capabilities_json"].asstr()[()]),
                csv_file=self.data_path.with_suffix(".csv").name if self.data_path.with_suffix(".csv").exists() else None,
            )
        _atomic_json(self.manifest_path, manifest)

    def _export_reference(self, source, group, index):
        from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
        from app.domain.models import MeasurementPoint
        from app.storage.hdf5_writer import Hdf5RunWriter

        purpose = str(group.attrs.get("purpose", "reference"))
        metadata = {
            "artifact_schema": "lab-control-sweep-baseline-v1",
            "artifact_role": purpose,
            "source_data_file": "../" + self.data_path.name,
            "source_reference_index": index,
            "kind": str(group.attrs["kind"]),
            "average_count": int(group.attrs["average_count"]),
            "acquired_at_utc": str(group.attrs["acquired_at_utc"]),
            "acquisition_metadata": json.loads(group.attrs.get("acquisition_metadata_json", "{}")),
            "source_recipe_sweep_indices": group["source_recipe_sweep_indices"][:].tolist()
                if "source_recipe_sweep_indices" in group else [],
        }
        frequencies = group["frequency_hz"][:]
        powers = group["power_dbm"][:]
        digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, allow_nan=False).encode("utf-8"))
        digest.update(frequencies.tobytes())
        digest.update(powers.tobytes())
        identity = digest.hexdigest()
        stem = f"{'bg' if purpose == 'background' else 'ref'}{index:04d}_{identity[:8]}"
        directory = self.directory / "references"
        directory.mkdir(exist_ok=True)
        path = directory / f"{stem}.h5"
        metadata["source_reference_sha256"] = identity
        if not path.exists():
            run = source["run"]
            # A failed export cannot poison the immutable final filename.
            temporary = directory / f".{index}_{secrets.token_hex(3)}.h5"
            writer = None
            try:
                writer = Hdf5RunWriter(
                    temporary, recipe_source=run["recipe_yaml"].asstr()[()],
                    isolate_validation=True,
                    settings_source=run["settings_yaml"].asstr()[()],
                    plan_hash=str(run.attrs["plan_sha256"]),
                    device_idn=json.loads(run["device_idn_json"].asstr()[()]),
                    device_capabilities=json.loads(run["capabilities_json"].asstr()[()]),
                    operator_context=json.loads(run["operator_context_json"].asstr()[()]),
                    simulation_metadata=json.loads(run["simulation_json"].asstr()[()]),
                    run_attributes={**{key: json_value(value) for key, value in run.attrs.items()
                                      if key.startswith("sample_")},
                                    "bundle_artifact_role": purpose, "source_reference_sha256": identity},
                    expected_points=1,
                    validation_memory_budget_bytes=int(run.attrs["validation_memory_budget_bytes"]),
                )
                writer.append(MeasurementPoint(0, {}, {}, metadata=metadata), SpectrumTrace(
                    frequencies, powers, datetime.fromisoformat(metadata["acquired_at_utc"]),
                    str(group.attrs["trace_name"]),
                ))
                writer.close("completed")
                temporary.rename(path)
            except Exception:
                try:
                    writer.close("faulted")
                except Exception:
                    pass
                raise
            finally:
                temporary.unlink(missing_ok=True)
        else:
            # Resume may reuse an immutable export only with identical data.
            import h5py
            import numpy as np
            with h5py.File(path, "r") as stored:
                if (stored["run"].attrs.get("status") != "completed"
                        or stored["run"].attrs.get("source_reference_sha256") != identity
                        or not np.array_equal(stored["spectra/0/frequency_hz"][:], frequencies)
                        or not np.array_equal(stored["spectra/0/power_dbm"][:], powers)):
                    raise ExecutionError(f"Existing baseline export differs from the committed reference: {path}")
        _atomic_json(directory / f"{stem}.json", metadata)
        return {"index": index, "purpose": purpose, "hdf5": f"references/{stem}.h5",
                "metadata": f"references/{stem}.json", "source_reference_sha256": identity}


def is_bundle_companion(path: Path) -> bool:
    """Keep baseline artefacts attached to a sweep, rather than separate runs."""
    if path.parent.name != "references":
        return False
    try:
        return json.loads((path.parent.parent / "metadata.json").read_text(encoding="utf-8")).get("schema") == SCHEMA
    except (OSError, ValueError):
        return False
