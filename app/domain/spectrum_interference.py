"""Immutable calibration identity for a bounded linear interference model."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

import numpy as np

from .spectrum_correction import SpectrumAcquisitionContext, immutable_vector


@dataclass(frozen=True, slots=True, eq=False)
class SpectrumInterferenceCalibration:
    model_id: str
    context: SpectrumAcquisitionContext
    baseline_w: np.ndarray
    basis_w: np.ndarray
    control_mask: np.ndarray
    protected_mask: np.ndarray
    control_sigma_w: np.ndarray
    coefficient_bounds: tuple[tuple[float, float], ...]
    source_profiles: tuple[tuple[str, str], ...]
    signal_control_regions_qualified: bool = False
    qualification_evidence: str = ""
    maximum_condition_number: float = 1e6
    algorithm_version: str = "reference-interference-qr-v1"
    training_provenance_json: str = ""
    content_hash: str = field(init=False)

    def __post_init__(self):
        for name in ("baseline_w", "basis_w", "control_sigma_w"):
            if np.asarray(getattr(self, name)).dtype.kind not in "iuf":
                raise ValueError(f"{name} must contain real numeric power values.")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.model_id) is None:
            raise ValueError("Interference model requires a safe model ID.")
        if self.algorithm_version != "reference-interference-qr-v1":
            raise ValueError("Unknown interference algorithm version.")
        if self.training_provenance_json:
            provenance = json.loads(self.training_provenance_json)
            if not isinstance(provenance, dict):
                raise ValueError("Training provenance must be a JSON object.")
            object.__setattr__(self, "training_provenance_json", json.dumps(provenance, sort_keys=True, allow_nan=False))
        baseline = immutable_vector(self.baseline_w, name="baseline_w", nonnegative=True)
        sigma = immutable_vector(self.control_sigma_w, name="control_sigma_w", nonnegative=True)
        if baseline.shape != self.context.frequencies_hz.shape or sigma.shape != baseline.shape or np.any(sigma <= 0):
            raise ValueError("Interference calibration requires matching axes and positive control sigma.")
        basis = np.asarray(self.basis_w, dtype=np.float64)
        if basis.ndim != 2 or basis.shape[0] != baseline.size or not 1 <= basis.shape[1] <= 8 or not np.all(np.isfinite(basis)):
            raise ValueError("Interference basis must be finite F by r, with 1 to 8 components.")
        basis = np.frombuffer(basis.tobytes(), dtype=np.float64).reshape(basis.shape)
        for name in ("control_mask", "protected_mask"):
            mask = np.asarray(getattr(self, name))
            if mask.dtype != np.bool_ or mask.shape != baseline.shape:
                raise ValueError("Interference masks must be boolean frequency vectors.")
            object.__setattr__(self, name, np.frombuffer(mask.tobytes(), dtype=np.bool_))
        if np.count_nonzero(self.control_mask & ~self.protected_mask) < basis.shape[1] + 1:
            raise ValueError("Insufficient unprotected control points.")
        bounds = tuple(tuple(pair) for pair in self.coefficient_bounds)
        if len(bounds) != basis.shape[1] or any(
            len(pair) != 2 or not np.all(np.isfinite(pair)) or pair[0] >= pair[1] for pair in bounds
        ):
            raise ValueError("Invalid calibrated coefficient bounds.")
        sources = tuple(tuple(pair) for pair in self.source_profiles)
        if not sources or any(
            len(pair) != 2 or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pair[0]) is None
            or re.fullmatch(r"[a-f0-9]{64}", pair[1]) is None for pair in sources
        ) or len({pair[0] for pair in sources}) != len(sources):
            raise ValueError("Calibration requires unique source profile IDs and hashes.")
        if type(self.signal_control_regions_qualified) is not bool or not isinstance(self.qualification_evidence, str):
            raise ValueError("Control qualification must be explicit boolean evidence.")
        if self.signal_control_regions_qualified and not self.qualification_evidence.strip():
            raise ValueError("Qualified signal control regions require recorded evidence.")
        if not np.isfinite(self.maximum_condition_number) or self.maximum_condition_number < 1:
            raise ValueError("Invalid interference conditioning limit.")
        object.__setattr__(self, "baseline_w", baseline)
        object.__setattr__(self, "basis_w", basis)
        object.__setattr__(self, "control_sigma_w", sigma)
        object.__setattr__(self, "coefficient_bounds", bounds)
        object.__setattr__(self, "source_profiles", sources)
        digest = hashlib.sha256(json.dumps(self.metadata(), sort_keys=True, allow_nan=False).encode())
        for array in (self.context.frequencies_hz, baseline, basis, self.control_mask, self.protected_mask, sigma):
            array = array.astype("?" if array.dtype.kind == "b" else "<f8", copy=False)
            digest.update(str(array.shape).encode("ascii"))
            digest.update(array.dtype.str.encode("ascii"))
            digest.update(array.tobytes())
        object.__setattr__(self, "content_hash", digest.hexdigest())

    def metadata(self):
        metadata = {
            "model_id": self.model_id, "context_id": self.context.context_id,
            "configuration_fingerprint": self.context.configuration_fingerprint,
            "configuration_generation": self.context.configuration_generation,
            "settings_verified": self.context.settings_verified,
            "independent_sweeps_qualified": self.context.independent_sweeps_qualified,
            "coefficient_bounds": self.coefficient_bounds, "source_profiles": self.source_profiles,
            "signal_control_regions_qualified": self.signal_control_regions_qualified,
            "qualification_evidence": self.qualification_evidence,
            "maximum_condition_number": self.maximum_condition_number,
            "algorithm_version": self.algorithm_version,
        }
        if self.training_provenance_json:
            metadata["training_provenance_json"] = self.training_provenance_json
        return metadata
