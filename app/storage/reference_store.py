"""Portable thaTEC/PyThat-compatible Anritsu reference artefacts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from app.devices.anritsu_ms2830a import ReferenceSpectrum, SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.domain.spectrum_averaging import InstrumentAverageProvenance
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
    def save(
        cls,
        path: str | Path,
        reference: ReferenceSpectrum,
        *,
        simulation_mode: bool | None = None,
    ) -> ReferenceSpectrum:
        if simulation_mode is not None and not isinstance(simulation_mode, bool):
            raise TypeError(
                f"Expected simulation_mode to be a boolean or None, got {simulation_mode!r}."
            )
        target = Path(path)
        metadata = cls.metadata(reference, simulation_mode=simulation_mode)
        if simulation_mode is None:
            simulation_metadata: dict[str, Any] = {
                "enabled": None,
                "mode": "unknown",
                "mode_source": "not_provided",
            }
        else:
            simulation_metadata = {
                "enabled": simulation_mode,
                "mode": "simulation" if simulation_mode else "hardware",
                "mode_source": "application_runtime",
            }
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
            simulation_metadata=simulation_metadata,
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
                    "simulation_enabled": simulation_mode,
                },
            )
            writer.close("completed")
        except Exception as exc:
            try:
                writer.close("faulted")
            except (ExecutionError, OSError, RuntimeError, ValueError, TypeError, KeyError) as close_error:
                exc.add_note(f"Closing the failed reference archive also failed: {close_error}")
            raise
        return replace(reference, source_file=str(target.resolve()), saved_to_file=True)

    @classmethod
    def read_simulation_enabled(cls, path: str | Path) -> bool | None:
        """Read recorded simulation provenance: True, False, or None when unknown."""
        target = Path(path)
        metadata = Hdf5RunReader.single_point(target).metadata
        if metadata.get("reference_schema") != cls.SCHEMA:
            raise ExecutionError("The selected HDF5 file is not a Lab Control reference artefact.")
        run_simulation = Hdf5RunReader.detail(target).simulation_metadata
        return cls._resolve_simulation_enabled(run_simulation, metadata)

    @classmethod
    def load_with_provenance(
        cls,
        path: str | Path,
        *,
        expected_simulation_mode: bool | None = None,
        allow_unknown_simulation: bool = True,
    ) -> tuple[ReferenceSpectrum, bool | None]:
        """Load one reference spectrum along with its recorded simulation provenance."""
        if expected_simulation_mode is not None and not isinstance(expected_simulation_mode, bool):
            raise TypeError(
                "Expected expected_simulation_mode to be a boolean or None, "
                f"got {expected_simulation_mode!r}."
            )
        target = Path(path)
        metadata = Hdf5RunReader.single_point(target).metadata
        if metadata.get("reference_schema") != cls.SCHEMA:
            raise ExecutionError("The selected HDF5 file is not a Lab Control reference artefact.")
        run_simulation = Hdf5RunReader.detail(target).simulation_metadata
        simulation_enabled = cls._resolve_simulation_enabled(run_simulation, metadata)
        if expected_simulation_mode is not None:
            if simulation_enabled is None:
                if not allow_unknown_simulation:
                    raise ExecutionError(
                        "Reference file does not record whether it was acquired in simulation or hardware mode."
                    )
            elif simulation_enabled is not expected_simulation_mode:
                if simulation_enabled:
                    raise ExecutionError(
                        "Cannot load a simulated reference spectrum into a hardware measurement session."
                    )
                raise ExecutionError(
                    "Cannot load a hardware reference spectrum into a simulation session."
                )
        stored = Hdf5RunReader.spectrum(target, 0)
        if stored is None:
            raise ExecutionError("The reference file contains no spectrum data.")
        try:
            evidence = metadata.get("instrument_average")
            timing_context = metadata.get("timing_context")
            if timing_context is not None and not isinstance(timing_context, str):
                raise ValueError("The reference timing context must be text.")
            acquired = datetime.fromisoformat(
                stored.acquired_at_utc or str(metadata["acquired_at_utc"])
            )
            trace = SpectrumTrace(
                frequencies_hz=stored.frequencies_hz,
                powers_dbm=stored.powers_dbm,
                acquired_at_utc=acquired,
                trace_name=stored.trace_name,
                instrument_average=InstrumentAverageProvenance.from_metadata(evidence) if evidence is not None else None,
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
                vbw_filter_mode=metadata.get("vbw_filter_mode"),
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
                timing_context=timing_context,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError(f"Reference metadata is invalid: {exc}") from exc
        return reference, simulation_enabled

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        expected_simulation_mode: bool | None = None,
        allow_unknown_simulation: bool = True,
    ) -> ReferenceSpectrum:
        reference, _simulation_enabled = cls.load_with_provenance(
            path,
            expected_simulation_mode=expected_simulation_mode,
            allow_unknown_simulation=allow_unknown_simulation,
        )
        return reference

    @classmethod
    def metadata(
        cls,
        reference: ReferenceSpectrum,
        *,
        simulation_mode: bool | None = None,
    ) -> dict[str, Any]:
        if simulation_mode is not None and not isinstance(simulation_mode, bool):
            raise TypeError(
                f"Expected simulation_mode to be a boolean or None, got {simulation_mode!r}."
            )
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
            "vbw_filter_mode": reference.vbw_filter_mode,
            "vbw_hz": reference.vbw_hz,
            "detector": reference.detector,
            "attenuation_auto": reference.attenuation_auto,
            "attenuation_db": reference.attenuation_db,
            "preamplifier_enabled": reference.preamplifier_enabled,
            "sweep_time_auto": reference.sweep_time_auto,
            "sweep_time_s": reference.sweep_time_s,
            "notes": reference.notes,
            "grid_hash": reference.grid_hash,
            **({"timing_context": reference.timing_context} if reference.timing_context else {}),
            **({"instrument_average": reference.instrument_average.metadata()} if reference.instrument_average else {}),
            **({"simulation_enabled": simulation_mode} if simulation_mode is not None else {}),
        }

    @classmethod
    def _resolve_simulation_enabled(
        cls,
        run_simulation: object,
        point_metadata: dict[str, Any],
    ) -> bool | None:
        if not isinstance(run_simulation, dict):
            raise ExecutionError("Reference /run/simulation_json metadata must be a JSON object.")
        try:
            run_enabled = cls._optional_bool(run_simulation.get("enabled"))
            point_enabled = cls._optional_bool(point_metadata.get("simulation_enabled"))
        except ValueError as exc:
            raise ExecutionError(f"Reference simulation provenance is invalid: {exc}") from exc
        mode = run_simulation.get("mode")
        if mode == "unknown" and run_enabled is not None:
            raise ExecutionError("Reference simulation provenance disagrees between mode and enabled.")
        if run_enabled is not None and point_enabled is not None and run_enabled is not point_enabled:
            raise ExecutionError(
                "Reference simulation provenance disagrees between /run and checkpoint metadata."
            )
        return run_enabled if run_enabled is not None else point_enabled

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
