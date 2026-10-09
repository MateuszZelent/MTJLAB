"""Compare Live dispatch and recipe I/V traffic with both OUTPUT states."""

from copy import deepcopy
from dataclasses import replace

import pytest

from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.keithley_2600.module import _dispatch
from app.devices.simulators import KeithleySimulator
from app.devices.visa import FakeVisaSessionFactory
from app.engine.compiler import PlanAction
from app.engine.runner import RecipeRunner
from tests.test_adapters_and_runner import MemoryWriter
from tests.test_keithley_coupled_ranges import request_for, settings_for


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode", ["current", "voltage"])
@pytest.mark.parametrize("enabled", [False, True])
def test_live_and_sweep_iv_have_identical_traffic_without_configuration_changes(channel, mode, enabled):
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(channel), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        # Both channels stay ON in the energized cases, detecting cross-channel
        # output changes as well as changes to the measured channel.
        for selected in ("A", "B"):
            request = request_for(selected, mode)
            if mode == "voltage":
                request = replace(request, level_si=.005)
            adapter.configure_source(request)
            if enabled:
                adapter.set_output(selected, True)
        def configuration():
            values = deepcopy({key: value for key, value in vars(session).items()
                               if key not in {"commands", "_measurement_index"}})
            # An enabled hardware autorange may select a measurement range
            # during IV acquisition. Its policy and all source settings stay
            # unchanged; the application sends no range assignment.
            values["programmed"] = {key: value for key, value in values["programmed"].items()
                                    if ".measure.range" not in key}
            return values

        before = configuration()
        start = len(session.commands)
        live = _dispatch(adapter, "measure", channel)
        live_traffic = session.commands[start:]
        runner = RecipeRunner(rigol=None, keithley=adapter, anritsu=None, writer=MemoryWriter())
        measurements = {}
        start = len(session.commands)
        runner._execute_impl(PlanAction("read-iv", "measure_keithley", {"channel": channel}, {}), measurements)
        sweep_traffic = session.commands[start:]
        assert sweep_traffic == live_traffic
        assert f"print(smu{channel.lower()}.measure.iv())" in sweep_traffic
        # No assignments, configuration, arming or output commands.
        assert all(command.startswith("print(") for command in sweep_traffic)
        assert configuration() == before
        assert live.output_enabled is enabled
        assert not live.compliance_detected
        prefix = f"keithley.{channel}"
        assert measurements[f"{prefix}.output_enabled"] == float(enabled)
        assert measurements[f"{prefix}.current_a"] == live.current_a
        assert measurements[f"{prefix}.voltage_v"] == live.voltage_v
        assert measurements[f"{prefix}.power_w"] == live.power_w
        assert adapter.connected
    finally:
        adapter.emergency_off()
        adapter.disconnect()
