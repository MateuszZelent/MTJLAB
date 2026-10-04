"""Block sandwich errors of a known resonance fit, retaining spectral dependence."""

import numpy as np

from .resonance_metrics import resonance_values


def resonance_influence(frequencies_hz, fit):
    """Return F×4 normalized fit influence and 4×4 SI parameter transform.

    Fit coordinates are amplitude/|A|, center/FWHM, log(FWHM), baseline/|A|.
    The constant baseline is a nuisance coordinate retained in the inverse.
    This local linearization is not a model-independent uncertainty estimator.
    """
    frequencies = np.asarray(frequencies_hz, dtype=float)
    amplitude, center, width = fit.amplitude_w, fit.center_hz, fit.fwhm_hz
    scale = abs(amplitude)
    if scale == 0:
        raise ValueError("Zero amplitude cannot be studentized.")
    z = (frequencies - center) / width
    profile = resonance_values(frequencies, 1, center, width, shape=fit.shape)
    if fit.shape == "gaussian":
        slope = 8 * np.log(2) * z * profile
    else:
        slope = 8 * z * profile**2
    jacobian = np.column_stack((profile, np.sign(amplitude) * slope,
                               np.sign(amplitude) * z * slope, np.ones(frequencies.size)))
    if np.linalg.cond(jacobian) > 1e10:
        raise ValueError("Studentization Jacobian is unidentifiable.")
    influence = np.linalg.pinv(jacobian).T / scale
    transform = np.zeros((4, 4))
    transform[0, 0], transform[1, 1], transform[2, 2] = scale, width, width
    edge = profile[[0, -1]]
    transform[3] = [fit.finite_window_area_w_hz / amplitude * scale,
                    amplitude * (edge[0] - edge[1]) * width,
                    fit.finite_window_area_w_hz + amplitude * width * (z[0] * edge[0] - z[-1] * edge[1]), 0]
    return influence, transform


def block_parameter_covariance(frequencies_hz, references_w, signals_w, fit,
                               reference_weights=None, signal_weights=None):
    """Sandwich covariance of independent source means; one common REF term.

    Weights are bootstrap multiplicities divided by the original source size.
    Covariance uses entire projected block vectors; no F×F matrix is formed.
    """
    influence, transform = resonance_influence(frequencies_hz, fit)
    covariance = np.zeros((4, 4))
    for blocks, weights in ((references_w, reference_weights), (signals_w, signal_weights)):
        count = len(blocks)
        if count < 2:
            raise ValueError("Studentization requires at least two independent blocks per source.")
        weights = np.full(count, 1 / count) if weights is None else np.asarray(weights)
        if weights.shape != (count,) or not np.all(np.isfinite(weights)) or np.any(weights < 0) or not np.isclose(weights.sum(), 1):
            raise ValueError("Studentization weights must be finite normalized multiplicities.")
        # Center in W before projection to avoid cancellation of the large floor.
        projected = (blocks - blocks.mean(axis=0)) @ influence
        projected -= weights @ projected
        covariance += (projected.T * weights) @ projected / (count - 1)
    diagonal = np.diag(covariance)
    if np.any(diagonal <= 256 * np.finfo(float).eps**2 * diagonal.max()):
        raise ValueError("Studentization cannot resolve a parameter variance above numerical precision.")
    covariance = transform @ covariance @ transform.T
    covariance = (covariance + covariance.T) / 2
    if not np.all(np.isfinite(covariance)) or np.any(np.diag(covariance) <= 0):
        raise ValueError("Studentization has nonpositive or nonfinite parameter variance.")
    return covariance
