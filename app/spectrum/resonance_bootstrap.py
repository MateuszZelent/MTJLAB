"""Offline two-sample bootstrap of complete, qualified spectral block means."""

from dataclasses import asdict, dataclass
from importlib.metadata import version
import math

import numpy as np

from .resonance_metrics import fit_linear_resonance
from .resonance_studentization import block_parameter_covariance

PARAMETERS = ("amplitude_w", "center_hz", "fwhm_hz", "finite_window_area_w_hz")


@dataclass(frozen=True, slots=True)
class ResonanceBootstrapConfig:
    resamples: int = 1000
    seed: int = 20261004
    confidence_level: float = .95
    minimum_blocks: int = 20
    maximum_blocks: int = 256
    working_memory_limit_bytes: int = 64 * 1024 * 1024
    independent_blocks_qualified: bool = False
    reference_equivalence_qualified: bool = False
    stationary_signal_qualified: bool = False
    qualification_evidence: str = ""
    interval_method: str = "percentile"

    def __post_init__(self):
        if self.interval_method not in {"percentile", "studentized"}:
            raise ValueError("Bootstrap interval method must be percentile or studentized.")
        for name, low, high in (("resamples", 200, 10000), ("minimum_blocks", 8, 256),
                               ("maximum_blocks", 8, 256), ("seed", 0, 2**64 - 1),
                               ("working_memory_limit_bytes", 1024, 1024**3)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Bootstrap {name} must be an integer in {low}..{high}.")
        if self.minimum_blocks > self.maximum_blocks or not math.isfinite(self.confidence_level) or not .5 < self.confidence_level < 1:
            raise ValueError("Invalid bootstrap block limits or confidence level.")
        for name in ("independent_blocks_qualified", "reference_equivalence_qualified", "stationary_signal_qualified"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("Bootstrap qualifications must be explicit booleans.")
        if not isinstance(self.qualification_evidence, str) or any((
            self.independent_blocks_qualified, self.reference_equivalence_qualified, self.stationary_signal_qualified,
        )) and not self.qualification_evidence.strip():
            raise ValueError("Declared bootstrap qualifications require recorded evidence.")

    @property
    def assumptions_qualified(self):
        return self.independent_blocks_qualified and self.reference_equivalence_qualified and self.stationary_signal_qualified


def bootstrap_buffer_bytes(reference_blocks, signal_blocks, points, config):
    estimate = (reference_blocks + signal_blocks + 24) * points * 8 + config.resamples * 8 * 8
    if config.interval_method == "studentized":
        estimate += max(reference_blocks, signal_blocks) * points * 8
    return estimate


def bootstrap_resonance_blocks(frequencies_hz, reference_blocks_w, signal_blocks_w, *,
                                initial_center_hz, initial_fwhm_hz, shape="gaussian",
                                config=ResonanceBootstrapConfig(), cancellation_check=None, progress_callback=None):
    """Resample each whole F-vector, REF once per replicate, independently of SIGNAL.

    Inputs are equal-sized nonoverlapping block means within each source.
    Qualification of block independence, reference equivalence and signal
    stationarity is external. Percentile intervals are marginal, conditional
    on declared assumptions; this function does not validate their coverage.
    """
    frequencies = np.asarray(frequencies_hz)
    references, signals = np.asarray(reference_blocks_w), np.asarray(signal_blocks_w)
    if frequencies.ndim != 1 or frequencies.size < 11 or frequencies.dtype.kind not in "iuf" or not np.all(np.isfinite(frequencies)) or np.any(np.diff(frequencies) <= 0):
        raise ValueError("Bootstrap requires a finite increasing frequency grid.")
    for values in (references, signals):
        if values.ndim != 2 or values.shape[1] != frequencies.size or values.shape[0] < 2 or values.dtype.kind not in "iuf":
            raise ValueError("Bootstrap requires real N by F block matrices with at least two blocks.")
        if values.shape[0] > config.maximum_blocks or not np.all(np.isfinite(values)) or np.any(values <= 0):
            raise ValueError("Block means must be positive finite acquired watts within the block cap.")
    estimated_bytes = bootstrap_buffer_bytes(len(references), len(signals), frequencies.size, config)
    if estimated_bytes > config.working_memory_limit_bytes:
        raise ValueError("Bootstrap buffers exceed the working-memory budget.")
    if cancellation_check:
        cancellation_check()
    corrected = signals.mean(axis=0) - references.mean(axis=0)
    estimate = fit_linear_resonance(frequencies, corrected, initial_center_hz=initial_center_hz,
                                    initial_fwhm_hz=initial_fwhm_hz, shape=shape)
    report = {"algorithm": f"whole-spectral-block-{config.interval_method}-v1", "config": asdict(config),
              "software_versions": {"numpy": np.__version__, "scipy": version("scipy")},
              "reference_blocks": len(references), "signal_blocks": len(signals), "estimate": asdict(estimate),
              "parameter_order": PARAMETERS, "confidence_intervals": None, "parameter_covariance": None,
              "resamples_completed": 0, "failed_fits": 0, "failed_studentizations": 0, "estimated_buffer_bytes": estimated_bytes,
              "status": "unqualified", "coverage_qualified": False,
              "interval_scope": "marginal per parameter; not simultaneous or a peak-detection threshold",
              "units": {"amplitude_w": "W", "center_hz": "Hz", "fwhm_hz": "Hz", "finite_window_area_w_hz": "W*Hz"},
              "limitations": ["Block length and assumptions require separate experimental qualification",
                              "Interval coverage requires independent Monte Carlo/laboratory validation",
                              "Complete REF and SIGNAL vectors preserve frequency dependence; no per-bin IID assumption",
                              "One shared reference mean is resampled once per replicate",
                              "No fitted-interference-model uncertainty or bracketed-reference covariance included"]}
    if not config.assumptions_qualified:
        return report
    if min(len(references), len(signals)) < config.minimum_blocks:
        report["status"] = "insufficient_blocks"
        return report
    random = np.random.default_rng(config.seed)
    samples = np.empty((config.resamples, len(PARAMETERS)))
    standard_errors = np.empty_like(samples) if config.interval_method == "studentized" else None
    estimate_vector = np.array([getattr(estimate, name) for name in PARAMETERS])
    original_error = None
    if standard_errors is not None:
        try:
            original_covariance = block_parameter_covariance(frequencies, references, signals, estimate)
            original_error = np.sqrt(np.diag(original_covariance))
        except ValueError:
            report["status"] = "studentization_failed"
            return report
    accepted = 0
    for _ in range(config.resamples):
        if cancellation_check:
            cancellation_check()
        reference_weights = random.multinomial(len(references), np.full(len(references), 1 / len(references))) / len(references)
        signal_weights = random.multinomial(len(signals), np.full(len(signals), 1 / len(signals))) / len(signals)
        residual = signal_weights @ signals - reference_weights @ references
        try:
            fit = fit_linear_resonance(frequencies, residual, initial_center_hz=estimate.center_hz,
                                       initial_fwhm_hz=estimate.fwhm_hz, shape=shape)
        except ValueError:
            report["failed_fits"] += 1
        else:
            try:
                if standard_errors is not None:
                    covariance = block_parameter_covariance(frequencies, references, signals, fit,
                                                           reference_weights, signal_weights)
                    standard_errors[accepted] = np.sqrt(np.diag(covariance))
            except ValueError:
                report["failed_studentizations"] += 1
            else:
                samples[accepted] = [getattr(fit, name) for name in PARAMETERS]
                accepted += 1
        report["resamples_completed"] += 1
        if progress_callback:
            progress_callback(report["resamples_completed"], config.resamples)
    if report["failed_fits"] or report["failed_studentizations"]:
        report["status"] = "unstable_fit"
        return report  # No CI conditioned on discarding unidentifiable fits.
    tail = (1 - config.confidence_level) / 2
    if standard_errors is None:
        bounds = np.quantile(samples, [tail, 1 - tail], axis=0)
    else:
        pivots = (samples - estimate_vector) / standard_errors
        quantiles = np.quantile(pivots, [tail, 1 - tail], axis=0)
        bounds = estimate_vector - quantiles[::-1] * original_error
        report["studentization"] = {"method": "whole-block-local-linear-sandwich-v1",
                                    "standard_errors": original_error.tolist()}
    covariance = np.cov(samples, rowvar=False, ddof=1)
    if not np.all(np.isfinite(bounds)) or not np.all(np.isfinite(covariance)):
        raise ValueError("Bootstrap parameter intervals or covariance are not finite.")
    report["confidence_intervals"] = {name: bounds[:, index].tolist() for index, name in enumerate(PARAMETERS)}
    report["parameter_covariance"] = covariance.tolist()
    report["status"] = "conditional_interval"
    return report
