"""A run zeros every MOKE channel it armed, without touching other outputs."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.domain.errors import ExecutionError
from app.engine.compiler import PlanAction
from app.engine.runner import RecipeRunner
from app.safety.moke_box import MokeVoltageResult
from tests.test_moke_voltage_control import controlled_adapter, plan_for, mutations
from tests.test_adapters_and_runner import MemoryWriter, ShutdownProbe


def runner_for(adapter):
    return RecipeRunner(rigol=ShutdownProbe(), keithley=ShutdownProbe(),
                        anritsu=ShutdownProbe(), moke_box=adapter, writer=MemoryWriter())


@pytest.mark.parametrize("explicit_stop", [False, True])
def test_shutdown_zeros_both_previously_armed_channels(explicit_stop):
    adapter, transport, profile = controlled_adapter(minimum_settling_s=0)
    second = replace(profile, channel=3)
    adapter._config = replace(adapter._config, allowed_vout_channels=(2, 3),
                              additional_control_profiles=(second,))
    runner = runner_for(adapter)
    try:
        for current in (profile, second):
            plan = plan_for(current, targets=(.2,))
            adapter.configure_voltage_plan(plan)
            runner._moke_voltage_plan = plan
            runner._execute(PlanAction("arm", "arm_moke_voltage", {"plan": plan}, {}), {})
            adapter.ramp_vout(current.channel, .2)
        assert runner._moke_owned_channels == {2, 3}
        transport.sent.clear()
        if explicit_stop:
            runner._execute(PlanAction("stop", "stop_moke_voltage", {}, {}), {})
        else:
            runner._shutdown_owned_device("moke_box", adapter)
        written_channels = {frame.channel for frame in mutations(transport)}
        assert written_channels == {2, 3}
        assert adapter.read_vouts()[2] == adapter.read_vouts()[3] == 0
        assert adapter.safe_target_confirmed
        states = runner._device_states["moke_box"]
        assert states["dac_shutdown_2"]["actual"]["safe_target_confirmed"]
        assert states["dac_shutdown_3"]["actual"]["safe_target_confirmed"]
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("first", [RuntimeError("lost connection"),
                                  MokeVoltageResult(0, 0, 0, .1, "profile", False)])
def test_failure_does_not_skip_other_owned_channel(first):
    device = SimpleNamespace(stop_vout=Mock(side_effect=[
        first, MokeVoltageResult(7, 0, 0, 0, "profile", True),
    ]))
    runner = runner_for(device)
    runner._moke_owned_channels = {0, 7}
    with pytest.raises(ExecutionError, match="VOUT 0"):
        runner._shutdown_owned_device("moke_box", device)
    assert [call.args for call in device.stop_vout.call_args_list] == [(0,), (7,)]


def test_failed_later_shutdown_invalidates_previous_zero_metadata():
    device = SimpleNamespace(stop_vout=Mock(side_effect=[
        MokeVoltageResult(0, 0, 0, 0, "profile", True), RuntimeError("readback lost"),
    ]))
    runner = runner_for(device)
    runner._moke_owned_channels = {0}
    runner._shutdown_owned_device("moke_box", device)
    assert runner._device_states["moke_box"]["dac_shutdown_0"]["actual"]["safe_target_confirmed"]
    with pytest.raises(ExecutionError, match="readback lost"):
        runner._shutdown_owned_device("moke_box", device)
    for key in ("dac_shutdown", "dac_shutdown_0"):
        actual = runner._device_states["moke_box"][key]["actual"]
        assert actual["safe_target_confirmed"] is False
        assert actual["actual_v"] is None
