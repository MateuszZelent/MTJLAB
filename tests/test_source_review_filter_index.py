"""Preserve tolerance semantics without all-pairs filter comparisons."""
import math
import random
import sys

from app.ui.results.filter_index import unique_signatures
from app.ui.results.spectrum_tab import SpectrumResultsTab


def test_index_matches_stable_pairwise_grouping_at_numeric_boundaries():
    rng = random.Random(813)
    values = [None, True, False, "text", "1", 1, {}, [], math.inf, -math.inf,
              math.nan, math.nan, sys.float_info.max, -sys.float_info.max, 0.]
    for exponent in (-1074, -1000, -50, -10, -9, -8, 0, 10, 500, 1023):
        base = math.ldexp(1., exponent)
        for sign in (-1, 1):
            for factor in (0., .5, .99, 1., 1.01, 1.5, 2.):
                values.append(sign * (base + factor * max(1e-15, base * 1e-12)))
    rng.shuffle(values)
    signatures = [(("outer", 1.), ("inner", value)) for value in values]
    signatures += [(), (), (("other", 1.),), (("inner", 1.),)]
    expected = []
    equal = SpectrumResultsTab._signatures_equal
    for signature in signatures:
        if not any(equal(signature, previous) for previous in expected):
            expected.append(signature)
    actual = unique_signatures(signatures, equal)
    # Identity also verifies the first representative for non-transitive
    # approximate equality, as well as separate NaN records.
    assert [id(item) for item in actual] == [id(item) for item in expected]


def test_unique_inner_axis_does_not_scan_constant_outer_axes():
    signatures = [(("A", .001), ("B", .025), ("MOKE", i * .0001)) for i in range(10000)]
    calls = []
    def equal(left, right):
        calls.append(1)
        return SpectrumResultsTab._signatures_equal(left, right)
    actual = unique_signatures(signatures + signatures, equal)
    assert actual == signatures
    assert len(calls) == len(signatures)  # Only the repeated half needs comparison.


def test_nearby_random_values_keep_original_representatives():
    rng = random.Random(918)
    equal = SpectrumResultsTab._signatures_equal
    signatures = []
    for _ in range(600):
        base = rng.choice([0., 1e-9, -.001, .001, 1., 1e90])
        value = base + rng.uniform(-5, 5) * max(1e-15, abs(base) * 1e-12)
        signatures.append((("value", value),))
    expected = []
    for signature in signatures:
        if not any(equal(signature, other) for other in expected):
            expected.append(signature)
    assert unique_signatures(signatures, equal) == expected
