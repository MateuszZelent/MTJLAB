"""Portable background profiles retaining the public thaTEC/PyThat raw view."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from .hdf5_writer import Hdf5RunWriter
from .spectrum_correction_codec import read_profile


class BackgroundProfileHdf5Store:
    SCHEMA = "lab-control-spectrum-background-v1"

    @classmethod
    def save(
        cls, path: str | Path, context: SpectrumAcquisitionContext, profile: BackgroundProfile
    ) -> None:
        if profile.context_id != context.context_id or np.any(profile.mean_w <= 0):
            raise ExecutionError("Export profile requires matching context and positive mean power.")
        trace = SpectrumTrace(
            tuple(context.frequencies_hz), tuple(10 * np.log10(profile.mean_w) + 30),
            datetime.fromtimestamp(profile.completed_at_s, timezone.utc), "BACKGROUND_MEAN",
        )
        writer = Hdf5RunWriter(
            path, recipe_source="schema_version: 1\nname: Background profile\nsteps: []\n",
            settings_source=f"background_schema: {cls.SCHEMA}\n",
            plan_hash=profile.content_hash, device_idn={"anritsu": "ANRITSU,UNKNOWN"},
            expected_points=1,
            # A portable aggregate has no source-run backend provenance in
            # this schema. Exporting it is not evidence of hardware acquisition.
            simulation_metadata={"enabled": None, "mode": "unknown",
                                 "mode_source": "profile_export_without_source_archive"},
            run_attributes={"background_schema": cls.SCHEMA, "profile_id": profile.profile_id},
        )
        try:
            writer.store_background_profile(context, profile)
            writer.append(
                MeasurementPoint(0, {}, {}, metadata={"background_profile_id": profile.profile_id}),
                trace,
            )
            writer.close("completed")
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
