"""Offline global difference test: permute complete independent spectral blocks."""

from dataclasses import asdict, dataclass
import math

import numpy as np


@dataclass(frozen=True, slots=True)
class SpectralDifferenceTestConfig:
    permutations: int = 999
    seed: int = 20261009
    alpha: float = .01
    independent_blocks_qualified: bool = False
    null_exchangeability_qualified: bool = False
    qualification_evidence: str = ""
    working_memory_limit_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for name, low, high in (("permutations", 99, 99999), ("seed", 0, 2**64 - 1),
                               ("working_memory_limit_bytes", 1024, 1024**3)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Difference test {name} must be an integer in {low}..{high}.")
        if (isinstance(self.alpha, bool) or not math.isfinite(self.alpha) or not 0 < self.alpha <= .1
                or 1 / (self.permutations + 1) > self.alpha):
            raise ValueError("Alpha must be positive, at most 0.1, and resolvable by the permutation count.")
        flags = (self.independent_blocks_qualified, self.null_exchangeability_qualified)
        if any(type(value) is not bool for value in flags):
            raise ValueError("Difference test qualifications must be explicit booleans.")
        if not isinstance(self.qualification_evidence, str) or any(flags) and not self.qualification_evidence.strip():
            raise ValueError("Declared difference-test qualifications need recorded evidence.")


def difference_test_buffer_bytes(reference_blocks, signal_blocks, points):
    if points > 10001:
        raise ValueError("Difference test supports at most 10001 frequency bins.")
    # Wide-grid batches keep only 16 permutation rows, never B by F data.
    batch_vectors = 48 if points > 2000 else 0
    return (3 * (reference_blocks + signal_blocks) + 12 + batch_vectors) * points * 8


def spectral_difference_test(frequencies_hz, references_w, signals_w, search_mask, *,
                             config=SpectralDifferenceTestConfig(), cancellation_check=None,
                             progress_callback=None):
    """Test a global REF/SIGNAL difference; do not filter data or identify resonances.

    The maximum absolute standardized mean difference controls one declared
    frequency search. Label permutations preserve each complete F-vector.
    The pooled scale is invariant to labels. Validity requires independent
    blocks exchangeable between REF/SIGNAL under the complete null.
    """
    frequencies = np.asarray(frequencies_hz)
    refs, signals = np.asarray(references_w), np.asarray(signals_w)
    mask = np.asarray(search_mask)
    if (frequencies.ndim != 1 or not 1 <= frequencies.size <= 10001
            or frequencies.dtype.kind not in "iuf" or not np.all(np.isfinite(frequencies))
            or np.any(np.diff(frequencies) <= 0)):
        raise ValueError("Difference test needs a finite increasing bounded frequency grid.")
    if mask.dtype != np.bool_ or mask.shape != frequencies.shape or not mask.any():
        raise ValueError("Difference test needs an explicit nonempty boolean frequency mask.")
    for source in (refs, signals):
        if (source.ndim != 2 or source.shape[1] != frequencies.size or not 2 <= len(source) <= 256
                or source.dtype.kind not in "iuf" or not np.all(np.isfinite(source)) or np.any(source <= 0)):
            raise ValueError("Difference test requires positive finite W in 2..256 complete blocks per source.")
    estimated_bytes = difference_test_buffer_bytes(len(refs), len(signals), frequencies.size)
    if estimated_bytes > config.working_memory_limit_bytes:
        raise ValueError("Difference-test buffers exceed the working-memory budget.")
    report = {"algorithm": "whole-spectral-block-max-permutation-v1", "config": asdict(config),
              "numpy_version": np.__version__, "reference_blocks": len(refs), "signal_blocks": len(signals),
              "search_bin_indices": np.flatnonzero(mask).tolist(), "estimated_buffer_bytes": estimated_bytes,
              "search_frequencies_hz": frequencies[mask].tolist(),
              "status": "unqualified", "p_value": None, "global_difference_detected": None,
              "observed_statistic": None, "permutations_completed": 0, "exceedances": None,
              "analytical_constant_case": False,
              "permutation_batch_size": 16 if frequencies.size > 2000 else 1,
              "false_alarm_rate_qualified": False, "laboratory_qualified": False,
              "limitations": ["Global difference alarm is not identification of a magnetic resonance",
                              "No per-bin p-values or strong control under partial alternatives",
                              "One fixed search; repeated live looks require separate sequential control",
                              "Exchangeability excludes unqualified drift, state-dependent noise and fitted model residuals",
                              "Independent validation required; statistical assumptions are not laboratory qualification",
                              "Input spectra remain untouched, including negative differences"]}
    if not (config.independent_blocks_qualified and config.null_exchangeability_qualified):
        return report
    if min(len(refs), len(signals)) < 20:
        report["status"] = "insufficient_blocks"
        return report
    if cancellation_check:
        cancellation_check()
    pooled = np.concatenate((refs[:, mask], signals[:, mask])).astype(float, copy=False)
    pooled -= pooled.mean(axis=0)
    n_ref, n_signal = len(refs), len(signals)
    scale = pooled.std(axis=0, ddof=1) * math.sqrt(1 / n_ref + 1 / n_signal)
    if not np.all(np.isfinite(scale)):
        raise ValueError("Difference-test pooled scale is not finite.")
    variable = scale > 0
    if np.any(np.any(pooled != pooled[0], axis=0) & ~variable):
        raise ValueError("Difference-test variance is below numerical precision.")
    pooled[:, ~variable] = 0
    if not variable.any():
        report.update(status="conditional_test", p_value=1., global_difference_detected=False,
                      observed_statistic=0., analytical_constant_case=True)
        return report
    # Identical bins contribute zero. A pooled zero scale cannot hide a
    # between-group difference because every value in that bin is identical.
    pooled[:, variable] /= scale[variable]
    total = pooled.sum(axis=0)

    def statistic(reference_sum):
        difference = (total - reference_sum) / n_signal - reference_sum / n_ref
        return float(np.max(np.abs(difference)))

    observed = statistic(pooled[:n_ref].sum(axis=0))
    random = np.random.default_rng(config.seed)
    exceedances = 0
    # Roundoff ties count against rejection, avoiding anti-conservative zero p.
    threshold = observed - 64 * np.finfo(float).eps * max(1., observed)
    batch_size = 16 if frequencies.size > 2000 else 1
    for first in range(0, config.permutations, batch_size):
        if cancellation_check:
            cancellation_check()
        count = min(batch_size, config.permutations - first)
        if batch_size == 1:
            selected = random.permutation(len(pooled))[:n_ref]
            exceedances += statistic(pooled[selected].sum(axis=0)) >= threshold
        else:
            weights = np.zeros((count, len(pooled)))
            for row in weights:
                row[random.permutation(len(pooled))[:n_ref]] = 1
            differences = weights @ pooled
            differences *= -(1 / n_signal + 1 / n_ref)
            differences += total / n_signal
            np.abs(differences, out=differences)
            exceedances += np.count_nonzero(np.max(differences, axis=1) >= threshold)
        if progress_callback:
            progress_callback(first + count, config.permutations)
    p_value = (int(exceedances) + 1) / (config.permutations + 1)
    report.update(status="conditional_test", p_value=p_value,
                  global_difference_detected=bool(p_value <= config.alpha),
                  observed_statistic=observed, exceedances=int(exceedances),
                  permutations_completed=config.permutations)
    return report
