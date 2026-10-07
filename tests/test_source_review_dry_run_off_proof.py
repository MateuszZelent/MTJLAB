"""Dry-run output guards use the same strict physical proof as shutdown."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.domain.errors import ExecutionError
from app.domain.models import DeviceState
from app.engine.runner import ExecutionMode, RecipeRunner
from tests.test_adapters_and_runner import MemoryWriter


@pytest.mark.parametrize("name", ["keithley", "rigol"])
@pytest.mark.parametrize("result,state,accepted", [
    (None, DeviceState.VERIFIED, False),
    (None, DeviceState.OUTPUT_OFF, True),
    (True, DeviceState.COMPLIANCE, True),
    (False, DeviceState.OUTPUT_OFF, False),
    (1, DeviceState.VERIFIED, False),
    (1, DeviceState.OUTPUT_OFF, False),
])
def test_dry_run_requires_physical_off_proof(name, result, state, accepted):
    device = SimpleNamespace(emergency_off=Mock(return_value=result), state=state)
    runner = RecipeRunner(
        rigol=device, keithley=device, anritsu=device, writer=MemoryWriter(),
        execution_mode=ExecutionMode.DRY_RUN,
    )
    runner._required_devices = frozenset({name})
    if accepted:
        runner._confirm_dry_run_outputs_off()
        assert all(value == "off" for key, value in runner._output_status.items() if key.startswith(name + "."))
    else:
        with pytest.raises(ExecutionError, match="could not confirm all outputs OFF"):
            runner._confirm_dry_run_outputs_off()
        assert all(value == "unknown" for key, value in runner._output_status.items() if key.startswith(name + "."))
    device.emergency_off.assert_called_once()
