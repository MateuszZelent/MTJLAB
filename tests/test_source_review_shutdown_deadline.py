"""Device cleanup shares one deadline, including later fallback attempts."""

from contextlib import contextmanager

import pytest

from app.domain.errors import ConfigurationError
from app.domain.models import DeviceState
from app.engine import runner as runner_module
from app.engine.compiler import PlanAction
from app.engine.policy import ExecutionPolicy
from app.engine.runner import RecipeRunner
from tests.helpers import simulation_settings
from tests.test_adapters_and_runner import MemoryWriter


class Device:
    state = DeviceState.OUTPUT_OFF

    def __init__(self, clock, overrun=0):
        self.clock, self.overrun = clock, overrun
        self.budgets = []
        self.calls = 0

    @contextmanager
    def operation_timeout(self, duration):
        self.budgets.append(duration)
        yield

    def emergency_off(self):
        self.calls += 1
        self.clock[0] += self.budgets[-1] + self.overrun
        return True

    abort_acquisition = emergency_off


@pytest.mark.parametrize("overrun", [0, .1])
def test_shutdown_shares_budget_and_does_not_restart_it(monkeypatch, overrun):
    clock = [100.0]
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: clock[0])
    devices = {name: Device(clock, overrun if name == "keithley" else 0)
               for name in ("rigol", "keithley", "anritsu")}
    runner = RecipeRunner(**devices, writer=MemoryWriter(), policy=ExecutionPolicy(shutdown_timeout_s=60))
    runner._required_devices = frozenset(devices)
    assert runner._safe_shutdown() is (overrun == 0)
    assert all(device.calls == 1 for device in devices.values())
    assert devices["keithley"].budgets == [20]
    assert devices["rigol"].budgets[0] <= 20
    assert clock[0] == pytest.approx(160)
    assert runner._shutdown_deadline == 160
    assert runner._safe_shutdown() is False
    assert all(device.calls == 1 for device in devices.values())
    assert runner._shutdown_deadline == 160


def test_finally_and_fallback_use_same_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: clock[0])
    device = Device(clock)
    runner = RecipeRunner(rigol=device, keithley=device, anritsu=device,
                          writer=MemoryWriter(), policy=ExecutionPolicy(shutdown_timeout_s=20))
    runner._required_devices = frozenset({"rigol"})
    monkeypatch.setattr(runner, "_execute_impl", lambda *args: clock.__setitem__(0, 4))
    runner._execute(PlanAction("off", "set_rigol_output", {"channel": 1, "enabled": False}, {}, True), {})
    assert runner._shutdown_deadline == 20
    assert runner._safe_shutdown()
    assert device.budgets == [10, 16]


def test_shutdown_budget_is_configurable_and_validated():
    settings = simulation_settings()
    settings.execution["shutdown_timeout"] = "120000 ms"
    assert ExecutionPolicy.from_settings(settings).shutdown_timeout_s == 120
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ConfigurationError, match="shutdown_timeout_s"):
            ExecutionPolicy(shutdown_timeout_s=value)
