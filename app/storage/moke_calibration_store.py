"""Immutable MOKE calibration profiles and crash-tolerant raw HDF5 runs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import statistics
import uuid
from dataclasses import asdict
from pathlib import Path

import h5py

from app.devices.moke_box.calibration import (
    CalibrationPoint,
    CalibrationRequest,
    MokeCalibration,
    canonical_json,
)
from app.domain.errors import ConfigurationError
from app.domain.models import MeasurementPoint
from app.storage.hdf5_writer import Hdf5RunWriter

logger = logging.getLogger(__name__)


def atomic_json(path: Path, document: object, *, exclusive: bool = False) -> None:
    """Flush a complete temp file before atomically publishing it on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(document) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)  # exclusive publication; existing versions are immutable
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MokeCalibrationRepository:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()

    def _path(self, calibration_id: str) -> Path:
        if not isinstance(calibration_id, str) or re.fullmatch(r"[0-9a-f]{64}", calibration_id) is None:
            raise ConfigurationError("Invalid MOKE calibration identity.")
        return self.directory / "profiles" / f"{calibration_id}.json"

    def save(self, model: MokeCalibration) -> str:
        path = self._path(model.calibration_id)
        document = asdict(model)
        try:
            atomic_json(path, document, exclusive=True)
        except FileExistsError:
            if self.load(model.calibration_id) != model:
                raise ConfigurationError("Stored MOKE calibration conflicts with its immutable identity.") from None
        return model.calibration_id

    def load(self, calibration_id: str) -> MokeCalibration:
        try:
            document = json.loads(self._path(calibration_id).read_text(encoding="utf-8"))
            model = MokeCalibration.from_document(document)
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigurationError(f"Could not read MOKE calibration: {exc}") from exc
        if model.calibration_id != calibration_id:
            raise ConfigurationError("MOKE calibration content hash does not match its identity.")
        self._validate_raw_run(model)
        return model

    def _validate_raw_run(self, model: MokeCalibration) -> None:
        if not isinstance(model.raw_run_id, str) or re.fullmatch(r"[0-9a-f]{32}", model.raw_run_id) is None:
            raise ConfigurationError("Invalid MOKE calibration raw-run identity.")
        path = self.directory / "runs" / f"{model.raw_run_id}.h5"
        try:
            # Hash and inspect the same open file; never trust a detached model alone.
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != model.raw_sha256:
                    raise ConfigurationError("MOKE calibration raw HDF5 hash does not match its model.")
                stream.seek(0)
                with h5py.File(stream, "r") as raw:
                    run = raw["run"]
                    if (run.attrs.get("status") != "completed"
                            or run.attrs.get("measurement_kind") != "moke_field_calibration"
                            or int(raw.attrs.get("measurement running", 1)) != 0):
                        raise ConfigurationError("MOKE calibration raw run is not completed.")
                    request = json.loads(run["settings_yaml"].asstr()[()])
                    if request["context"] != asdict(model.context):
                        raise ConfigurationError("MOKE calibration raw context does not match its model.")
                    from app.storage.event_log import committed_event_count
                    names = list(raw["events/name"].asstr()[:committed_event_count(raw["events"])])
                    finished = json.loads(raw["events/message"].asstr()[names.index("calibration_finished")])
                    if finished.get("status") != "completed" or finished.get("dac_zero_confirmed") is not True:
                        raise ConfigurationError("MOKE calibration raw run has no confirmed final DAC zero.")
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise ConfigurationError(f"Could not verify MOKE calibration raw HDF5: {exc}") from exc

    def list_ids(self) -> tuple[str, ...]:
        return tuple(sorted(path.stem for path in (self.directory / "profiles").glob("*.json")
                            if re.fullmatch(r"[0-9a-f]{64}", path.stem)))

    def activate(self, calibration_id: str, *, profile_fingerprint: str,
                 simulation: bool, reviewed: bool) -> MokeCalibration:
        if reviewed is not True:
            raise ConfigurationError("Review the calibration before explicitly activating it.")
        model = self.load(calibration_id)
        if model.context.profile_fingerprint != profile_fingerprint or model.context.simulation != simulation:
            raise ConfigurationError("Cannot activate calibration for a different physical/simulation profile.")
        # Separate pointers keep simulated calibration from replacing a hardware model.
        pointer = "active_simulation.json" if simulation else "active_hardware.json"
        atomic_json(self.directory / pointer, {"schema_version": 1, "calibration_id": calibration_id})
        return model

    def active(self, *, profile_fingerprint: str, simulation: bool) -> MokeCalibration | None:
        path = self.directory / ("active_simulation.json" if simulation else "active_hardware.json")
        if not path.exists():
            return None
        try:
            pointer = json.loads(path.read_text(encoding="utf-8"))
            if set(pointer) != {"schema_version", "calibration_id"} or pointer["schema_version"] != 1:
                raise ConfigurationError("Unsupported active MOKE calibration pointer.")
            model = self.load(pointer["calibration_id"])
        except (ValueError, TypeError, OSError) as exc:
            raise ConfigurationError(f"Could not read active MOKE calibration: {exc}") from exc
        if model.context.profile_fingerprint != profile_fingerprint or model.context.simulation != simulation:
            return None
        return model


class MokeCalibrationRunStore:
    """Use the existing scalar checkpoint/PyThat contract; raw samples stay in metadata."""

    def __init__(self, directory: str | Path, request: CalibrationRequest) -> None:
        self.run_id = uuid.uuid4().hex
        self._channel = request.context.channel
        self.path = Path(directory).resolve() / "runs" / f"{self.run_id}.h5"
        document = asdict(request)
        source = canonical_json(document)
        self._writer = Hdf5RunWriter(
            self.path, recipe_source="schema_version: 1\n", settings_source=source,
            plan_hash=hashlib.sha256(source.encode()).hexdigest(),
            device_idn={"lakeshore_gaussmeter": request.context.reference_idn,
                        "moke_box": request.context.binding_id},
            expected_points=2 * len(request.plan.targets_v) * request.repetitions,
            simulation_metadata={"enabled": request.context.simulation},
            run_attributes={"measurement_kind": "moke_field_calibration", "calibration_schema_version": 1},
        )
        try:
            self._writer.append_event("calibration_prepared", {
                "request": document,
                "units": {"requested_v": "V", "applied_v": "V", "actual_v": "V",
                          "reference_samples_t": "T", "hall_samples_v": "V"},
            })
        except Exception:
            try:
                self._writer.close("faulted")
            except Exception:
                logger.exception("Could not close the unstarted MOKE calibration file")
            raise
        self._closed = False

    def event(self, name: str, details: dict[str, object]) -> None:
        self._writer.append_event(name, details)

    def append(self, point: CalibrationPoint) -> None:
        measurements = {
            "moke_box.vout_voltage_v": point.actual_v,
            "lakeshore.field_t": statistics.fmean(point.reference_samples_t),
            "lakeshore.stddev_t": statistics.pstdev(point.reference_samples_t),
        }
        if point.hall_samples_v:
            measurements["moke_box.hall1_voltage_v"] = statistics.fmean(point.hall_samples_v)
        self._writer.append(MeasurementPoint(
            index=point.index,
            setpoints={f"moke_box.vout{self._channel}.voltage": point.requested_v},
            measurements=measurements, metadata={"calibration_point": asdict(point)},
        ))

    def close(self, status: str) -> str:
        if not self._closed:
            self._writer.close(status)
            self._closed = True
        with self.path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
