"""Candidate index for stable, tolerance-aware checkpoint filter grouping."""

from __future__ import annotations

import math
from collections import defaultdict

FILTER_REL_TOL = 1e-12
FILTER_ABS_TOL = 1e-15
_MIN_SCALE = math.ceil(math.log2(FILTER_ABS_TOL / FILTER_REL_TOL))


def _value_keys(value, *, query=False):
    if value is None:
        return (("none",),)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return (("text", str(value)),)
    if math.isnan(number):
        return ()  # math.isclose(NaN, anything) is false.
    if math.isinf(number):
        return (("infinity", number > 0),)
    # The minimum scale covers absolute tolerance around zero. Above it,
    # bins follow the exponent, so very large/small SI values remain distinct.
    scale = max(_MIN_SCALE, math.frexp(abs(number))[1] - 1) if number else _MIN_SCALE
    scales = range(max(_MIN_SCALE, scale - 1), scale + 2) if query else (scale,)
    keys = []
    for exponent in scales:
        width = max(FILTER_ABS_TOL, math.ldexp(FILTER_REL_TOL, exponent))
        cell = math.floor(number / width)
        # Relative tolerance can span two bins at an exponent boundary.
        cells = range(cell - 3, cell + 4) if query else (cell,)
        keys.extend(("number", exponent, candidate) for candidate in cells)
    return tuple(keys)


def unique_signatures(signatures, equal, *, cancelled=None):
    """Keep the first representative, exactly as stable pairwise grouping.

    Bins only select candidates; ``equal`` still makes the final decision.
    Selecting the sparsest component first avoids scanning all earlier rows
    when a sweep has constant outer axes and a changing inner axis.
    """
    representatives = []
    buckets = defaultdict(set)
    for signature in signatures:
        if cancelled is not None and cancelled():
            raise InterruptedError("Filter indexing cancelled")
        groups = []
        for position, (name, value) in enumerate(signature):
            groups.append([
                buckets[token]
                for key in _value_keys(value, query=True)
                if (token := (len(signature), position, name, key)) in buckets
            ])
        if not signature:
            candidates = buckets.get((0,), ())
        elif any(not group for group in groups):
            candidates = ()
        else:
            smallest = min(groups, key=lambda group: sum(map(len, group)))
            candidates = set().union(*smallest)
        duplicate = any(
            all(any(index in bucket for bucket in group) for group in groups)
            and equal(signature, representatives[index])
            for index in sorted(candidates)
        )
        if duplicate:
            continue
        index = len(representatives)
        representatives.append(signature)
        if not signature:
            buckets[(0,)].add(index)
        for position, (name, value) in enumerate(signature):
            for key in _value_keys(value):
                buckets[(len(signature), position, name, key)].add(index)
    return representatives
