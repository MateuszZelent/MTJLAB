"""Small, reference-trained linear nuisance models with protected signal regions.

This is deliberately not an automatic line remover. Basis functions originate
from reference measurements. Fitting a signal frame additionally requires
independently qualified control regions; rank/conditioning failures fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike
from app.domain.spectrum_interference import SpectrumInterferenceCalibration

from app.domain.spectrum_correction import (
    FloatVector, SpectrumFrameEnvelope, SpectrumFrameRole, immutable_vector,
)
from .streaming_statistics import finite_vector


def calibrated_interference_model(calibration: SpectrumInterferenceCalibration):
    """Recompute the operator from immutable calibration, never deserialize QR."""
    return ReferenceInterferenceModel(
        context_id=calibration.context.context_id, baseline_w=calibration.baseline_w,
        basis_w=calibration.basis_w, coefficient_bounds=calibration.coefficient_bounds,
        control_mask=calibration.control_mask, protected_mask=calibration.protected_mask,
        control_sigma_w=calibration.control_sigma_w,
        signal_control_regions_qualified=calibration.signal_control_regions_qualified,
        maximum_condition_number=calibration.maximum_condition_number,
    )


@dataclass(frozen=True, slots=True, eq=False)
class InterferenceFit:
    background_w: FloatVector
    coefficients: tuple[float, ...]
    control_rms_w: float
    condition_number: float


class ReferenceInterferenceModel:
    """Pre-factorized weighted QR: O(Mr + r² + Fr) per accepted update.

    Columns in basis_w are perturbation shapes in W, so coefficients are
    dimensionless. Bounds are the qualified calibration envelope. Masks and
    weights are fixed during one model's lifetime; changing them requires a
    new factorization and a new model identity.
    """

    def __init__(
        self,
        *,
        context_id: str,
        baseline_w: ArrayLike,
        basis_w: ArrayLike,
        coefficient_bounds: tuple[tuple[float, float], ...],
        control_mask: ArrayLike,
        protected_mask: ArrayLike,
        control_sigma_w: ArrayLike | None = None,
        signal_control_regions_qualified: bool = False,
        maximum_condition_number: float = 1e6,
        maximum_components: int = 8,
    ) -> None:
        if not context_id or not np.isfinite(maximum_condition_number) or maximum_condition_number < 1:
            raise ValueError("Nuisance model requires identity and a valid conditioning limit.")
        self.context_id = context_id
        self.baseline_w = immutable_vector(baseline_w, name="baseline_w", nonnegative=True)
        basis = np.asarray(basis_w, dtype=np.float64)
        if (
            basis.ndim != 2 or basis.shape[0] != self.baseline_w.size
            or not 1 <= basis.shape[1] <= maximum_components or not np.all(np.isfinite(basis))
        ):
            raise ValueError("Nuisance basis must be finite F×r with a bounded component count.")
        controls, protected = np.asarray(control_mask), np.asarray(protected_mask)
        if (
            controls.dtype != np.bool_ or protected.dtype != np.bool_
            or controls.shape != self.baseline_w.shape or protected.shape != controls.shape
        ):
            raise ValueError("Control and protected masks must be boolean frequency vectors.")
        controls = controls & ~protected
        if np.count_nonzero(controls) < basis.shape[1] + 1:
            raise ValueError("Too few unprotected control points identify this nuisance model.")
        if len(coefficient_bounds) != basis.shape[1] or any(
            not np.isfinite(low) or not np.isfinite(high) or low >= high
            for low, high in coefficient_bounds
        ):
            raise ValueError("Nuisance coefficient bounds must match the calibrated basis.")
        sigma = (
            np.ones(self.baseline_w.size)
            if control_sigma_w is None else finite_vector(control_sigma_w, size=self.baseline_w.size)
        )
        if np.any(sigma <= 0):
            raise ValueError("Control uncertainty weights must be strictly positive.")
        # Weight normalization avoids overflow for tiny physical noise powers.
        weights = np.min(sigma[controls]) / sigma[controls]
        design = basis[controls] * weights[:, None]
        column_scales = np.linalg.norm(design, axis=0)
        if np.any(column_scales == 0) or not np.all(np.isfinite(column_scales)):
            raise ValueError("A nuisance component is not identifiable on control regions.")
        normalized = design / column_scales
        singular_values = np.linalg.svd(normalized, compute_uv=False)
        condition = singular_values[0] / singular_values[-1]
        if not np.isfinite(condition) or condition > maximum_condition_number:
            raise ValueError("Nuisance basis is rank deficient or poorly conditioned.")
        q, r = np.linalg.qr(normalized, mode="reduced")
        self._basis = np.frombuffer(basis.tobytes(), dtype=np.float64).reshape(basis.shape)
        self._indices = np.flatnonzero(controls)
        self._weights = weights
        self._column_scales = column_scales
        self._q = q
        self._r = r
        self.coefficient_bounds = coefficient_bounds
        self.signal_control_regions_qualified = signal_control_regions_qualified
        self.condition_number = float(condition)

    def fit(self, envelope: SpectrumFrameEnvelope, powers_w: ArrayLike) -> InterferenceFit:
        if envelope.context_id != self.context_id or not envelope.complete:
            raise ValueError("Nuisance fit requires a complete sweep from the calibrated context.")
        if envelope.role == SpectrumFrameRole.SIGNAL:
            if not self.signal_control_regions_qualified:
                raise ValueError("Fitting from a signal requires independently qualified control regions.")
        elif envelope.role != SpectrumFrameRole.REFERENCE:
            raise ValueError("Transitions and unknown states cannot update nuisance coefficients.")
        values = finite_vector(powers_w, size=self.baseline_w.size)
        if np.any(values <= 0):
            raise ValueError("Nuisance fit input must be positive acquired watts.")
        observations = values[self._indices] - self.baseline_w[self._indices]
        coefficients = np.linalg.solve(
            self._r, self._q.T @ (observations * self._weights)
        ) / self._column_scales
        if any(
            value < low or value > high
            for value, (low, high) in zip(coefficients, self.coefficient_bounds, strict=True)
        ):
            raise ValueError("Nuisance parameters are outside the calibrated envelope.")
        estimated = self.baseline_w + self._basis @ coefficients
        if np.any(estimated < 0) or not np.all(np.isfinite(estimated)):
            raise ValueError("Nuisance model predicts invalid background power.")
        residual = values[self._indices] - estimated[self._indices]
        return InterferenceFit(
            immutable_vector(estimated, name="model background_w", nonnegative=True),
            tuple(float(value) for value in coefficients),
            float(np.sqrt(np.mean(residual**2))), self.condition_number,
        )

    @property
    def working_memory_bytes(self):
        return sum(array.nbytes for array in (
            self.baseline_w, self._basis, self._q, self._r, self._weights, self._indices, self._column_scales,
        ))
