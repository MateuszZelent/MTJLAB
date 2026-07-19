"""Portable thaTEC/PyThat-compatible Anritsu reference artefacts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from app.devices.anritsu_ms2830a import ReferenceSpectrum, SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.hdf5_writer import Hdf5RunWriter


class ReferenceHdf5Store:
    """Save one immutable reference as a normal PyThat spectrum checkpoint.

    The public thaTEC view remains readable by the qualified PyThat version.
    Reference-specific provenance is retained in the private checkpoint
    metadata, so loading never has to infer whether a spectrum was a signal or
    a reference.
    """

    SCHEMA = "lab-control-anritsu-reference-v1"

    @classmethod
    def save(cls, path: str | Path, reference: ReferenceSpectrum) -> ReferenceSpectrum:
        target = Path(path)
        metadata = cls._metadata(reference)
        writer = Hdf5RunWriter(
            target,
            recipe_source=(
                "schema_version: 1\n"
                "name: Anritsu reference spectrum\n"
                "steps: []\n"
            ),
            settings_source=f"reference_schema: {cls.SCHEMA}\n",
            plan_hash=reference.grid_hash,
            device_idn={"anritsu": reference.source_device_idn or "ANRITSU,UNKNOWN"},
            device_capabilities={
                "anritsu": {
                    "firmware": reference.firmware,
                    "hardware_options": reference.hardware_options,
                }
            },
            expected_points=1,
        )
        try:
            writer.append(
                MeasurementPoint(
                    index=0,
                    setpoints={},
                    measurements={},
                    metadata=metadata,
                ),
                reference.trace,
            )
            writer.append_event(
                "reference_saved",
                {
                    "kind": reference.kind,
                    "average_count": reference.average_count,
                    "grid_hash": reference.grid_hash,
                },
            )
            writer.close("completed")
        except Exception:
            try:
                writer.close("faulted")
            except Exception:
                pass
            raise
        return replace(reference, source_file=str(target.resolve()), saved_to_file=True)

    @classmethod
    def load(cls, path: str | Path) -> ReferenceSpectrum:
        target = Path(path)
        points = Hdf5RunReader.points(target)
        if len(points) != 1:
            raise ExecutionError("A reference file must contain exactly one checkpoint.")
        metadata = points[0].metadata
        if metadata.get("reference_schema") != cls.SCHEMA:
            raise ExecutionError("The selected HDF5 file is not a Lab Control reference artefact.")
        stored = Hdf5RunReader.spectrum(target, 0)
        if stored is None:
            raise ExecutionError("The reference file contains no spectrum data.")
        try:
            acquired = datetime.fromisoformat(
                stored.acquired_at_utc or str(metadata["acquired_at_utc"])
            )
            trace = SpectrumTrace(
                frequencies_hz=stored.frequencies_hz,
                powers_dbm=stored.powers_dbm,
                acquired_at_utc=acquired,
                trace_name=stored.trace_name,
            )
            reference = ReferenceSpectrum(
                trace=trace,
                kind=str(metadata["kind"]),
                average_count=int(metadata["average_count"]),
                acquired_at_utc=acquired,
                source_device_idn=str(metadata.get("source_device_idn", "")),
                firmware=str(metadata.get("firmware", "")),
                hardware_options=tuple(str(item) for item in metadata.get("hardware_options", ())),
                reference_level_dbm=cls._optional_float(metadata.get("reference_level_dbm")),
                advanced_configuration_known=bool(
                    metadata.get("advanced_configuration_known", False)
                ),
                rbw_auto=cls._optional_bool(metadata.get("rbw_auto")),
                rbw_hz=cls._optional_float(metadata.get("rbw_hz")),
                vbw_mode=str(metadata.get("vbw_mode", "")),
                vbw_hz=cls._optional_float(metadata.get("vbw_hz")),
                detector=str(metadata.get("detector", "")),
                attenuation_auto=cls._optional_bool(metadata.get("attenuation_auto")),
                attenuation_db=cls._optional_float(metadata.get("attenuation_db")),
                preamplifier_enabled=cls._optional_bool(
                    metadata.get("preamplifier_enabled")
                ),
                sweep_time_auto=cls._optional_bool(metadata.get("sweep_time_auto")),
                sweep_time_s=cls._optional_float(metadata.get("sweep_time_s")),
                source_file=str(target.resolve()),
                notes=str(metadata.get("notes", "")),
                saved_to_file=True,
                grid_hash=str(metadata["grid_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError(f"Reference metadata is invalid: {exc}") from exc
        return reference

    @classmethod
    def _metadata(cls, reference: ReferenceSpectrum) -> dict[str, Any]:
        return {
            "reference_schema": cls.SCHEMA,
            "kind": reference.kind,
            "average_count": reference.average_count,
            "acquired_at_utc": reference.acquired_at_utc.isoformat(),
            "source_device_idn": reference.source_device_idn,
            "firmware": reference.firmware,
            "hardware_options": list(reference.hardware_options),
            "reference_level_dbm": reference.reference_level_dbm,
            "advanced_configuration_known": reference.advanced_configuration_known,
            "rbw_auto": reference.rbw_auto,
            "rbw_hz": reference.rbw_hz,
            "vbw_mode": reference.vbw_mode,
            "vbw_hz": reference.vbw_hz,
            "detector": reference.detector,
            "attenuation_auto": reference.attenuation_auto,
            "attenuation_db": reference.attenuation_db,
            "preamplifier_enabled": reference.preamplifier_enabled,
            "sweep_time_auto": reference.sweep_time_auto,
            "sweep_time_s": reference.sweep_time_s,
            "notes": reference.notes,
            "grid_hash": reference.grid_hash,
        }

    @staticmethod
    def _optional_float(value: object) -> float | None:
        return None if value is None else float(value)

    @staticmethod
    def _optional_bool(value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        raise ValueError(f"Expected a boolean metadata value, got {value!r}.")
