"""Causal holds and explicitly delayed, bracketed reference estimates."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike

from app.domain.spectrum_correction import BackgroundProfile, ReferenceEstimate
from .streaming_statistics import finite_vector


def causal_reference(profile: BackgroundProfile, at_s: float) -> ReferenceEstimate:
    if not math.isfinite(at_s) or at_s < profile.completed_at_s:
        raise ValueError("A causal estimate cannot use a reference completed in the future.")
    return ReferenceEstimate(
        mean_w=profile.mean_w,
        variance_w2=profile.mean_variance_w2,
        profile_weights=((profile.profile_id, 1.0),),
        age_s=at_s - profile.completed_at_s,
        final=False,
    )


def bracketed_reference(
    before: BackgroundProfile,
    after: BackgroundProfile,
    at_s: float,
    *,
    independent_blocks_qualified: bool = False,
    drift_variance_w2: ArrayLike | None = None,
) -> ReferenceEstimate:
    """Interpolate between disjoint reference blocks without pretending to be Live.

    A variance is published only if block independence and an interpolation
    error model are explicitly provided. Independent acquisition alone cannot
    establish that a fluctuating background is linear between observations.
    """
    if before.context_id != after.context_id or before.mean_w.shape != after.mean_w.shape:
        raise ValueError("Bracket references must have identical acquisition contexts.")
    if before.profile_id == after.profile_id or before.completed_at_s >= after.started_at_s:
        raise ValueError("Bracket references must be distinct, nonoverlapping blocks.")
    if not math.isfinite(at_s) or not before.completed_at_s <= at_s <= after.started_at_s:
        raise ValueError("Signal time must lie between the completed reference blocks.")
    weight = (at_s - before.representative_at_s) / (
        after.representative_at_s - before.representative_at_s
    )
    mean = (1 - weight) * before.mean_w + weight * after.mean_w
    drift = None
    if drift_variance_w2 is not None:
        drift = finite_vector(drift_variance_w2, size=mean.size)
        if np.any(drift < 0):
            raise ValueError("Interpolation-error variance must be nonnegative W².")
    variance = None
    if (
        independent_blocks_qualified and drift is not None
        and before.mean_variance_w2 is not None and after.mean_variance_w2 is not None
    ):
        variance = (
            (1 - weight) ** 2 * before.mean_variance_w2
            + weight**2 * after.mean_variance_w2 + drift
        )
    return ReferenceEstimate(
        mean_w=mean,
        variance_w2=variance,
        profile_weights=((before.profile_id, 1 - weight), (after.profile_id, weight)),
        age_s=at_s - before.completed_at_s,
        final=True,
    )
