"""Acquisition-time scheduling and explicit state evidence, without hardware."""

import pytest
from dataclasses import replace

from app.domain.spectrum_correction import SpectrumFrameEnvelope, SpectrumFrameRole, SweepEvidence
from app.domain.spectrum_interleaved import InterleavedSpectrumConfig, SpectrumOperatorStateConfirmation
from app.spectrum.interleaved_acquisition import InterleavedPhase, InterleavedSpectrumAcquisition
from app.settings.models import SpectrumReferencePolicySettings


def envelope(index, role, completed, *, started=None):
    return SpectrumFrameEnvelope(index, role.value + "-block", "context", 0, completed,
        role=role, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP, started_at_s=started)


def confirmation(role):
    return SpectrumOperatorStateConfirmation(role, "explicit operator state", True)


def test_schedule_uses_completed_acquisition_intervals_and_keeps_ref_signal_separate():
    cycle = InterleavedSpectrumAcquisition(InterleavedSpectrumConfig(1, 2, 3, 2))
    with pytest.raises(ValueError):
        cycle.confirm_state(confirmation(SpectrumFrameRole.SIGNAL))
    cycle.confirm_state(confirmation(SpectrumFrameRole.REFERENCE))
    assert not cycle.record_committed(envelope(0, SpectrumFrameRole.REFERENCE, 101, started=100))
    assert not cycle.record_committed(envelope(1, SpectrumFrameRole.REFERENCE, 102))
    assert cycle.record_committed(envelope(2, SpectrumFrameRole.REFERENCE, 103))
    assert cycle.phase == InterleavedPhase.FINISH_REFERENCE
    with pytest.raises(ValueError):
        cycle.confirm_state(confirmation(SpectrumFrameRole.SIGNAL))
    cycle.reference_committed()
    cycle.confirm_state(confirmation(SpectrumFrameRole.SIGNAL))
    assert not cycle.record_committed(envelope(3, SpectrumFrameRole.SIGNAL, 202, started=201))
    assert cycle.record_committed(envelope(4, SpectrumFrameRole.SIGNAL, 203))
    assert cycle.phase == InterleavedPhase.WAIT_REFERENCE and cycle.block_index == 2
    cycle.confirm_state(confirmation(SpectrumFrameRole.REFERENCE))
    for i in range(3):
        done = cycle.record_committed(envelope(5 + i, SpectrumFrameRole.REFERENCE, 301 + i))
    assert done
    cycle.reference_committed()
    assert cycle.phase == InterleavedPhase.COMPLETE and cycle.reference_blocks == 2


def test_rejected_roles_and_reordered_sweeps_cannot_advance_the_schedule():
    cycle = InterleavedSpectrumAcquisition(InterleavedSpectrumConfig(10, 10))
    with pytest.raises(ValueError):
        cycle.record_committed(envelope(0, SpectrumFrameRole.REFERENCE, 101))
    cycle.confirm_state(confirmation(SpectrumFrameRole.REFERENCE))
    assert not cycle.record_committed(envelope(0, SpectrumFrameRole.REFERENCE, 101))
    for trace in (envelope(1, SpectrumFrameRole.SIGNAL, 102),
                  envelope(1, SpectrumFrameRole.TRANSITION, 102),
                  envelope(0, SpectrumFrameRole.REFERENCE, 102),
                  envelope(1, SpectrumFrameRole.REFERENCE, 101)):
        with pytest.raises(ValueError):
            cycle.record_committed(trace)
    assert cycle.count == 1 and cycle.elapsed_s == 0


def test_context_and_segment_changes_cannot_be_hidden_inside_a_block():
    cycle = InterleavedSpectrumAcquisition(InterleavedSpectrumConfig(10, 10))
    cycle.confirm_state(confirmation(SpectrumFrameRole.REFERENCE))
    cycle.record_committed(envelope(0, SpectrumFrameRole.REFERENCE, 101))
    candidate = envelope(1, SpectrumFrameRole.REFERENCE, 102)
    for changed in (replace(candidate, context_id="different-context"),
                    replace(candidate, configuration_generation=1),
                    replace(candidate, segment_id="different-block")):
        with pytest.raises(ValueError, match="context must remain fixed"):
            cycle.record_committed(changed)
    assert cycle.count == 1 and cycle.elapsed_s == 0


@pytest.mark.parametrize("payload", [
    {"reference_duration_s": 0}, {"signal_duration_s": float("nan")},
    {"signal_duration_s": True}, {"minimum_reference_sweeps": True},
    {"minimum_reference_sweeps": 1}, {"maximum_reference_blocks": 257},
])
def test_invalid_configurations_are_rejected(payload):
    values = {"reference_duration_s": 1, "signal_duration_s": 5} | payload
    with pytest.raises(ValueError):
        InterleavedSpectrumConfig(**values)


@pytest.mark.parametrize("role,description,stable", [
    ("reference", "state", True), (SpectrumFrameRole.TRANSITION, "state", True),
    (SpectrumFrameRole.REFERENCE, "", True), (SpectrumFrameRole.SIGNAL, "state", False),
    (SpectrumFrameRole.REFERENCE, "state", 1),
])
def test_operator_report_does_not_accept_implicit_roles_or_missing_confirmation(role, description, stable):
    with pytest.raises(ValueError):
        SpectrumOperatorStateConfirmation(role, description, stable)


def test_settings_parse_explicit_units_and_reject_dimension_errors():
    policy = SpectrumReferencePolicySettings(block_duration="250 ms", signal_duration="2e1 s", minimum_sweeps=3)
    assert policy.minimum_sweeps == 3
    for payload in ({"block_duration": "1 Hz"}, {"signal_duration": "0 s"},
                    {"minimum_sweeps": True}, {"maximum_reference_blocks": 1}):
        with pytest.raises(ValueError):
            SpectrumReferencePolicySettings(**payload)
