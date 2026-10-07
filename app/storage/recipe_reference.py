"""Read and qualify immutable file references for a compiled sweep plan."""

import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.spectrum.processing import frequency_grids_match

from .background_profile_store import BackgroundProfileHdf5Store
from .reference_store import ReferenceHdf5Store


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_recipe_reference(path, kind):
    if kind == "background":
        context, profile = BackgroundProfileHdf5Store.load(path)
        if not context.settings_verified or np.any(profile.mean_w <= 0):
            raise ExecutionError(
                "Imported background needs verified analyzer settings and positive mean power."
            )
        trace = SpectrumTrace(
            tuple(context.frequencies_hz),
            tuple(10 * np.log10(profile.mean_w) + 30),
            datetime.fromtimestamp(profile.completed_at_s, UTC),
            "TRAC1",
        )
        return trace, profile.sweep_count, (context, profile)
    if kind == "reference":
        reference = ReferenceHdf5Store.load(path)
        if not reference.advanced_configuration_known or not reference.source_device_idn:
            raise ExecutionError(
                "Imported reference lacks verified RF input/bandwidth metadata; acquire a new reference."
            )
        if reference.vbw_filter_mode is None:
            raise ExecutionError("Imported reference lacks verified VID/POW metadata; acquire a new reference.")
        return reference.trace, reference.average_count, reference
    raise ExecutionError("Reference file kind must be reference or background.")


def verify_recipe_reference(evidence, full, advanced, device_idn, fingerprint):
    if isinstance(evidence, tuple):
        context, _profile = evidence
        if context.configuration_fingerprint != fingerprint:
            raise ExecutionError(
                "Imported background analyzer settings or instrument identity differ from readback."
            )
        axis = context.frequencies_hz
    else:
        reference = evidence
        if reference.source_device_idn != device_idn:
            raise ExecutionError("Imported reference belongs to another analyzer.")
        expected = {
            "reference_level_dbm": reference.reference_level_dbm,
            **{
                name: getattr(reference, name)
                for name in (
                    "rbw_auto",
                    "rbw_hz",
                    "vbw_mode",
                    "vbw_filter_mode",
                    "vbw_hz",
                    "detector",
                    "attenuation_auto",
                    "attenuation_db",
                    "preamplifier_enabled",
                    "sweep_time_auto",
                    "sweep_time_s",
                )
            },
        }
        for name, value in expected.items():
            actual = (
                full.reference_level_dbm
                if name == "reference_level_dbm"
                else getattr(advanced, name)
            )
            same = (
                math.isclose(value, actual, rel_tol=1e-9, abs_tol=1e-12)
                if type(value) in {float, int} and type(actual) in {float, int}
                else value == actual
            )
            if not same:
                raise ExecutionError(
                    f"Imported reference {name} differs from verified analyzer settings."
                )
        axis = reference.trace.frequencies_hz
    if not frequency_grids_match(axis, np.linspace(full.start_hz, full.stop_hz, full.points)):
        raise ExecutionError(
            "Imported reference frequency grid differs from the configured analyzer."
        )
