"""Writer failures must not retry a possibly committed checkpoint."""
from types import SimpleNamespace

import pytest

from app.domain.models import MeasurementPoint
from app.engine.runner import RecipeRunner


def test_internal_type_error_after_write_does_not_repeat_checkpoint():
    recorded = []

    def append(point, trace, **kwargs):
        recorded.append((point, kwargs))
        raise TypeError("device_states serialization failed after mutation")

    runner = object.__new__(RecipeRunner)
    runner._writer = SimpleNamespace(append=append)
    runner._device_states = {"keithley": {"channel_A": {"actual": {"source_level_si": 0.001}}}}
    point = MeasurementPoint(index=0, setpoints={}, measurements={})
    with pytest.raises(TypeError, match="after mutation"):
        runner._append_checkpoint(point)
    assert len(recorded) == 1
    assert recorded[0][1]["device_states"] == runner._device_states
    assert recorded[0][1]["device_states"] is not runner._device_states
