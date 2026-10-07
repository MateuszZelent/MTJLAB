"""Simulated SMUs retain independent voltage/current source registers."""

import pytest

from app.devices.simulators import KeithleySimulator
from app.domain.errors import DeviceError


@pytest.mark.parametrize("smu", ["smua", "smub"])
def test_inactive_source_level_does_not_change_measurement_or_compliance(smu):
    sim = KeithleySimulator()
    sim.write(f"{smu}.source.leveli = 0.001")
    sim.write(f"{smu}.source.output = {smu}.OUTPUT_ON")
    before = sim.query(f"print({smu}.measure.iv())")
    sim.write(f"{smu}.source.levelv = 0.05")
    assert sim.query(f"print({smu}.measure.iv())") == before
    assert sim.query(f"print({smu}.source.compliance)") == "false"
    assert float(sim.query(f"print({smu}.source.leveli)")) == .001
    assert float(sim.query(f"print({smu}.source.levelv)")) == .05
    sim.write(f"{smu}.source.func = {smu}.OUTPUT_DCVOLTS")
    assert tuple(map(float, sim.query(f"print({smu}.measure.iv())").split())) == pytest.approx((.005, .05))
    sim.write(f"{smu}.source.leveli = 0.04")
    assert sim.query(f"print({smu}.source.compliance)") == "false"
    assert float(sim.query(f"print({smu}.measure.v())")) == .05
    sim.write(f"{smu}.source.func = {smu}.OUTPUT_DCAMPS")
    assert sim.query(f"print({smu}.source.compliance)") == "true"
    assert tuple(map(float, sim.query(f"print({smu}.measure.iv())").split())) == pytest.approx((.01, .1))
    other = "smub" if smu == "smua" else "smua"
    assert float(sim.query(f"print({other}.source.leveli)")) == 0


@pytest.mark.parametrize("smu", ["smua", "smub"])
def test_default_readback_matches_enforced_compliance(smu):
    sim = KeithleySimulator()
    for suffix, limits in (("v", sim.limit_voltage), ("i", sim.limit_current)):
        assert float(sim.query(f"print({smu}.source.limit{suffix})")) == limits[smu]


@pytest.mark.parametrize("command", ["smua.source.levell = 1", "smub.measure.unknown = 1", "unknown()"])
def test_unsupported_writes_fail_without_changing_model(command):
    sim = KeithleySimulator()
    before = dict(sim.programmed)
    with pytest.raises(DeviceError, match="unsupported"):
        sim.write(command)
    assert sim.programmed == before
    assert not any(sim.output.values())
