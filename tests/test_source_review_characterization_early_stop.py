"""An early Stop must never produce a transient OUTPUT ON or configure after it."""
import threading

import pytest

from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from app.devices.keithley_2600.characterization.models import CharacterizationSweepConfig
from tests.test_keithley_characterization_runner import _MockKeithleyDevice


@pytest.mark.parametrize("stage", ["before_start", "off_proof", "recovery", "configure"])
def test_stop_before_enable_never_enables_or_ramps(stage):
    device = _MockKeithleyDevice()
    cancelled = threading.Event()
    cfg = CharacterizationSweepConfig(start_level_si=1e-6, stop_level_si=3e-6,
        points_count=3, source_range_si=.01, dwell_time_s=.001)
    if stage == "before_start":
        cancelled.set()
    else:
        operation = {"off_proof": "confirm_output_off", "recovery": "recover_from_compliance",
                     "configure": "configure_source"}[stage]
        original = getattr(device, operation)
        def stop(*args, **kwargs):
            result = original(*args, **kwargs)
            cancelled.set()
            return result
        setattr(device, operation, stop)
    result = KeithleyCharacterizationRunner.run_sweep(device, cfg, cancelled)
    assert result.completion_status == "cancelled" and result.points == ()
    assert "set_output:A:True" not in device.calls
    assert not any(call.startswith("ramp_to_zero") for call in device.calls)
    assert any(call.startswith("configure_source") for call in device.calls) is (stage == "configure")
    assert device.calls[-1] == "assert_output_state:A:False"
    if stage in {"before_start", "off_proof"}:
        assert not any(call.startswith("recover") for call in device.calls)


def test_configure_failure_still_attempts_off_without_enabling_or_ramping():
    device = _MockKeithleyDevice()
    def failed_configure(request):
        device.calls.append("configure_attempted")
        raise RuntimeError("configuration interrupted")
    device.configure_source = failed_configure
    cfg = CharacterizationSweepConfig(start_level_si=1e-6, stop_level_si=3e-6,
        points_count=3, source_range_si=.01, dwell_time_s=.001)
    with pytest.raises(RuntimeError, match="configuration interrupted"):
        KeithleyCharacterizationRunner.run_sweep(device, cfg)
    assert "set_output:A:True" not in device.calls
    assert not any(call.startswith("ramp_to_zero") for call in device.calls)
    assert device.calls[-2:] == ["set_output:A:False", "assert_output_state:A:False"]
