"""False/missing OFF evidence cannot become a safe characterization outcome."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.devices.keithley_2600.characterization.output_state import require_output_off
from app.devices.keithley_2600.characterization.runner import CharacterizationWorker, KeithleyCharacterizationRunner
from app.devices.keithley_2600.characterization.models import CharacterizationSweepConfig
from app.devices.keithley_2600.characterization.field_worker import restore_field_policies
from app.domain.errors import DeviceError, SafetyViolation
from tests.helpers import loaded_settings
from tests.test_keithley_characterization_runner import _MockKeithleyDevice


@pytest.mark.parametrize("proof", [False, None, 0, 1, "off"])
def test_confirmation_requires_true_not_truthiness(proof):
    with pytest.raises(DeviceError, match="not confirmed"):
        require_output_off(SimpleNamespace(confirm_output_off=lambda _: proof), "A")


@pytest.mark.parametrize("stage", [0, 1])
def test_initial_off_failure_blocks_configuration_and_enable(stage):
    device = _MockKeithleyDevice()
    proofs = iter(([False] if stage == 0 else [True, False]))
    device.confirm_output_off = lambda _: next(proofs, False)
    cfg = CharacterizationSweepConfig(start_level_si=1e-6, stop_level_si=3e-6,
                                      points_count=3, source_range_si=.01)
    with pytest.raises(DeviceError, match="OUTPUT OFF"):
        KeithleyCharacterizationRunner.run_sweep(device, cfg)
    assert not any(call.startswith(("configure", "set_output")) for call in device.calls)


@pytest.mark.parametrize("proof", [True, False, None])
def test_failed_worker_does_not_release_restoration_on_false_proof(monkeypatch, proof):
    def reject(*args):
        raise SafetyViolation("invalid preflight")
    monkeypatch.setattr(KeithleyCharacterizationRunner, "validate_preflight", reject)
    worker = CharacterizationWorker(SimpleNamespace(confirm_output_off=lambda _: proof),
                                    CharacterizationSweepConfig(), loaded_settings())
    errors, completed = [], []
    worker.failed.connect(errors.append)
    worker.finished_dataset.connect(completed.append)
    worker.run()
    assert worker.output_off_confirmed is (proof is True)
    assert len(errors) == 1 and not completed
    if proof is not True:
        assert "could not be independently confirmed" in errors[0]


def test_final_off_proof_failure_cannot_return_completed_dataset():
    device = _MockKeithleyDevice()
    device.confirm_output_off = lambda _: not any("set_output:A:True" == call for call in device.calls)
    cfg = CharacterizationSweepConfig(start_level_si=1e-6, stop_level_si=3e-6,
        points_count=3, compliance_si=.1, source_range_si=.01, dwell_time_s=.001)
    with pytest.raises(DeviceError, match="after characterization"):
        KeithleyCharacterizationRunner.run_sweep(device, cfg)
    assert "set_output:A:False" in device.calls


def test_field_restore_attempts_both_off_but_neither_policy_without_proof():
    off, confirm, policy = Mock(), Mock(side_effect=[False, True]), Mock()
    device = SimpleNamespace(set_output=off, confirm_output_off=confirm, set_compliance_policy=policy)
    outputs_off, restored, errors = restore_field_policies(device, {"A": "stop", "B": "stop"})
    assert not outputs_off and not restored and len(errors) == 1
    assert [call.args for call in off.call_args_list] == [("A", False), ("B", False)]
    assert [call.args for call in confirm.call_args_list] == [("A",), ("B",)]
    policy.assert_not_called()


def test_field_worker_rejects_false_preflight_and_records_unconfirmed_outcome(tmp_path):
    from tests.test_keithley_field_worker import make_worker

    worker, device = make_worker(tmp_path)
    device.confirm_output_off = Mock(side_effect=lambda channel: channel == "B")
    worker.run()
    assert worker.outcome.status == "fault"
    assert not worker.outcome.outputs_off
    assert not any(call[0] in {"configure", "policy", "output"} for call in device.calls)
    assert device.confirm_output_off.call_args_list[-1].args == ("B",)
