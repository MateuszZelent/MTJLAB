"""Private SI calibration data; unrelated to public thaTEC measurement rows."""

from __future__ import annotations

import json

import numpy as np

from app.domain.errors import ExecutionError
from app.domain.spectrum_correction import SpectrumAcquisitionContext
from app.domain.spectrum_interference import SpectrumInterferenceCalibration
from app.spectrum.interference_model import calibrated_interference_model

SCHEMA = "spectrum-interference-calibration-v1"
ARRAYS = {"frequency_hz": "Hz", "baseline_w": "W", "basis_w": "W",
          "control_sigma_w": "W", "control_mask": "1", "protected_mask": "1"}


def validate_interference_result(result, calibration):
    if (
        calibration.model_id != result.interference_model_id
        or calibration.content_hash != result.interference_model_hash
        or calibration.context.context_id != result.context_id
        or not calibration.signal_control_regions_qualified
        or len(result.interference_last_coefficients) != calibration.basis_w.shape[1]
        or any(value < low or value > high for value, (low, high) in zip(
            result.interference_last_coefficients, calibration.coefficient_bounds, strict=True
        ))
    ):
        raise ExecutionError("Correction interference model identity or coefficients differ from calibration.")


def write_interference_calibration(group, calibration):
    calibrated_interference_model(calibration)  # Reject rank/conditioning before writing.
    group.attrs["schema"] = SCHEMA
    group.attrs["content_hash"] = calibration.content_hash
    group.attrs["metadata_json"] = json.dumps(calibration.metadata(), sort_keys=True, allow_nan=False)
    for name, unit in ARRAYS.items():
        values = calibration.context.frequencies_hz if name == "frequency_hz" else getattr(calibration, name)
        dataset = group.create_dataset(name, data=values, dtype="?" if name.endswith("mask") else "f8")
        dataset.attrs["unit"] = unit


def read_interference_calibration(group):
    if group.attrs.get("schema") != SCHEMA or not bool(group.attrs.get("complete", False)):
        raise ExecutionError("Unknown or uncommitted interference calibration.")
    try:
        metadata = json.loads(group.attrs["metadata_json"])
        arrays = {}
        for name, unit in ARRAYS.items():
            dataset = group[name]
            expected_kind = "b" if name.endswith("mask") else "f"
            if dataset.attrs.get("unit") != unit or dataset.dtype.kind != expected_kind or (
                expected_kind == "f" and dataset.dtype.itemsize != 8
            ):
                raise ExecutionError(f"Invalid interference dataset unit/dtype: {name}.")
            arrays[name] = dataset[:]
        context = SpectrumAcquisitionContext(
            arrays.pop("frequency_hz"), metadata.pop("configuration_fingerprint"),
            configuration_generation=metadata.pop("configuration_generation"),
            settings_verified=metadata.pop("settings_verified"),
            independent_sweeps_qualified=metadata.pop("independent_sweeps_qualified"),
        )
        if context.context_id != metadata.pop("context_id"):
            raise ExecutionError("Interference calibration context identity is corrupted.")
        calibration = SpectrumInterferenceCalibration(context=context, **arrays, **metadata)
        if calibration.content_hash != group.attrs["content_hash"]:
            raise ExecutionError("Interference calibration checksum is corrupted.")
        calibrated_interference_model(calibration)
        return calibration
    except (KeyError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
        raise ExecutionError(f"Malformed interference calibration: {exc}") from exc
