"""An exception after a source write must shut down, never replay the mutation."""
import pytest

from app.devices.keithley_2600.characterization.models import CharacterizationSweepConfig
from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from tests.test_keithley_characterization_runner import _MockKeithleyDevice


@pytest.mark.parametrize("mode", ["current", "voltage"])
def test_type_error_after_write_is_not_retried_and_output_is_disabled(mode):
    device = _MockKeithleyDevice(mode=mode)
    attempts = []

    def update(channel, *args, **kwargs):
        attempts.append((channel, args, kwargs))
        device.current_level = kwargs.get("level_si", args[0] if args else 0)
        if len(attempts) == 1:
            raise TypeError("injected failure after accepted write")

    device.update_source_level = update
    config = CharacterizationSweepConfig(mode=mode, start_level_si=1e-6,
        stop_level_si=3e-6, points_count=3, source_range_si=.01 if mode == "current" else 1.,
        compliance_si=.1 if mode == "current" else .001, dwell_time_s=0)
    with pytest.raises(TypeError, match="after accepted write"):
        KeithleyCharacterizationRunner.run_sweep(device, config)
    assert attempts == [("A", (), {"mode": mode, "level_si": 1e-6})]
    assert not device.output_enabled
    assert device.calls[-2:] == ["set_output:A:False", "assert_output_state:A:False"]
