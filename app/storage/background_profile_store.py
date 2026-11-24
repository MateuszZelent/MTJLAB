"""Portable background profiles retaining the public thaTEC/PyThat raw view."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from .hdf5_writer import Hdf5RunWriter
from .spectrum_correction_codec import read_profile


class BackgroundProfileHdf5Store:
    SCHEMA = "lab-control-spectrum-background-v1"

    @classmethod
    def save(
        cls, path: str | Path, context: SpectrumAcquisitionContext, profile: BackgroundProfile, *,
        settings_source: str | None = None, device_idn: str = "",
        simulation_mode: bool | None = None, cancellation_check=None,
    ) -> None:
        if (profile.context_id != context.context_id or profile.mean_w.shape != context.frequencies_hz.shape
                or np.any(profile.mean_w <= 0)):
            raise ExecutionError("Export profile requires matching context and positive mean power.")
        if cancellation_check is not None:
            cancellation_check()
        trace = SpectrumTrace(
            tuple(context.frequencies_hz), tuple(10 * np.log10(profile.mean_w) + 30),
            datetime.fromtimestamp(profile.completed_at_s, timezone.utc), "BACKGROUND_MEAN",
            instrument_average=profile.instrument_average,
        )
        metadata = {"background_profile_id": profile.profile_id, "spectrum_average_count": profile.sweep_count}
        if profile.averaging_source == "instrument":
            metadata.update(background_averaging_source="instrument", background_sweep_count=profile.sweep_count,
                            nominal_sweep_time_s=profile.nominal_sweep_time_s,
                            instrument_average=profile.instrument_average.metadata())
        # An export without a source run cannot infer the original backend.
        if simulation_mode is None:
            simulation_metadata = {"enabled": None, "mode": "unknown",
                                   "mode_source": ("not_provided" if settings_source is not None
                                                   else "profile_export_without_source_archive")}
        else:
            simulation_metadata = {"enabled": simulation_mode,
                                   "mode": "simulation" if simulation_mode else "hardware",
                                   "mode_source": "application_runtime"}
        writer = Hdf5RunWriter(
            path, recipe_source="schema_version: 1\nname: Background profile\nsteps: []\n",
            settings_source=settings_source or f"background_schema: {cls.SCHEMA}\n",
            plan_hash=profile.content_hash, device_idn={"anritsu": device_idn or "ANRITSU,UNKNOWN"},
            expected_points=1, simulation_metadata=simulation_metadata,
            run_attributes={"background_schema": cls.SCHEMA, "profile_id": profile.profile_id},
        )
        try:
            writer.store_background_profile(context, profile)
            writer.append(MeasurementPoint(0, {}, {}, metadata=metadata), trace)
            if cancellation_check is not None:
                cancellation_check()
            writer.close("completed")
        except ProcessingCancelled:
            writer.close("aborted")
            raise
        except Exception as exc:
            try:
                writer.close("faulted")
            except Exception as close_error:
                exc.add_note(f"Closing the failed background export also failed: {close_error}")
            raise

    @classmethod
    def load(cls, path: str | Path) -> tuple[SpectrumAcquisitionContext, BackgroundProfile]:
        try:
            with h5py.File(path, "r") as file:
                run = file["run"].attrs
                if run.get("status") != "completed":
                    raise ExecutionError("File is not a completed background-profile artifact.")
                if run.get("background_schema") == cls.SCHEMA:
                    profile_id = str(run["profile_id"])
                elif (run.get("background_schema") is None
                        and run.get("spectrum_correction_schema") == "spectrum-correction-v1"):
                    profiles = file["spectrum_processing_v1/profiles"]
                    if len(profiles) != 1:
                        raise ExecutionError("Choose an archive with exactly one background, or export the desired profile separately.")
                    profile_id = next(iter(profiles))
                else:
                    raise ExecutionError("File is not a supported background-profile artifact or spectrum correction archive.")
                return read_profile(file[f"spectrum_processing_v1/profiles/{profile_id}"])
        except (OSError, KeyError) as exc:
            raise ExecutionError(f"Cannot read background profile: {exc}") from exc
