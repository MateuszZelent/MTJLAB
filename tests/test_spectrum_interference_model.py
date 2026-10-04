from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from app.domain.spectrum_correction import SpectrumFrameEnvelope, SpectrumFrameRole, SweepEvidence
from app.spectrum.interference_model import ReferenceInterferenceModel


def frame(role=SpectrumFrameRole.REFERENCE):
    return SpectrumFrameEnvelope(1, "calibration", "context", 0, 1, role=role,
                                 evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)


def model_fixture(*, qualified=False):
    x = np.linspace(-10, 10, 1001)
    line = np.exp(-x**2 / 2) * 1e-9
    derivative = -x * line
    baseline = np.full(x.size, 2e-9)
    basis = np.column_stack([line, derivative])
    model = ReferenceInterferenceModel(
        context_id="context", baseline_w=baseline, basis_w=basis,
        coefficient_bounds=((-0.5, 2), (-0.25, 0.25)),
        control_mask=np.ones(x.size, dtype=bool), protected_mask=np.abs(x) < .5,
        signal_control_regions_qualified=qualified,
    )
    return x, baseline, basis, model


def test_qr_reference_fit_recovers_fluctuating_amplitude_and_small_shift():
    _x, baseline, basis, model = model_fixture()
    background = baseline + basis @ [.75, .1]
    fit = model.fit(frame(), background)
    np.testing.assert_allclose(fit.coefficients, [.75, .1], rtol=1e-13)
    np.testing.assert_allclose(fit.background_w, background, rtol=1e-14)


def test_signal_is_protected_at_exactly_the_interference_frequency():
    x, baseline, basis, model = model_fixture(qualified=True)
    background = baseline + basis @ [.75, .1]
    signal = np.where(np.abs(x) < .3, 3e-10, 0)
    fit = model.fit(frame(SpectrumFrameRole.SIGNAL), background + signal)
    recovered = background + signal - fit.background_w
    np.testing.assert_allclose(recovered, signal, atol=2e-24)


def test_signal_cannot_train_model_without_independent_control_qualification():
    _x, baseline, _basis, model = model_fixture()
    with pytest.raises(ValueError, match="independently"):
        model.fit(frame(SpectrumFrameRole.SIGNAL), baseline)


def test_out_of_calibration_range_is_rejected_instead_of_clipped():
    _x, baseline, basis, model = model_fixture()
    with pytest.raises(ValueError, match="outside"):
        model.fit(frame(), baseline + basis @ [3, 0])


def test_no_information_outside_protected_region_is_not_fixed_by_regularization():
    basis = np.zeros((101, 1))
    basis[50, 0] = 1e-9
    with pytest.raises(ValueError, match="identifiable"):
        ReferenceInterferenceModel(
            context_id="context", baseline_w=np.full(101, 1e-9), basis_w=basis,
            coefficient_bounds=((0, 2),), control_mask=np.ones(101, dtype=bool),
            protected_mask=np.arange(101) == 50,
        )


def test_rank_deficient_model_is_rejected():
    basis = np.ones((101, 2)) * 1e-9
    with pytest.raises(ValueError, match="conditioned"):
        ReferenceInterferenceModel(
            context_id="context", baseline_w=np.full(101, 1e-9), basis_w=basis,
            coefficient_bounds=((0, 2), (0, 2)), control_mask=np.ones(101, dtype=bool),
            protected_mask=np.zeros(101, dtype=bool),
        )


def test_unknown_sweep_transition_and_wrong_context_rejected():
    _x, baseline, _basis, model = model_fixture()
    for invalid in (
        replace(frame(), evidence=SweepEvidence.UNKNOWN),
        replace(frame(), role=SpectrumFrameRole.TRANSITION),
        replace(frame(), context_id="different"),
    ):
        with pytest.raises(ValueError):
            model.fit(invalid, baseline)
