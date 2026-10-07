"""Invalid TSP arguments cannot become apparently confirmed model state."""

from copy import deepcopy

import pytest

from app.devices.simulators import KeithleySimulator
from app.domain.errors import DeviceError


@pytest.mark.parametrize("smu", ["smua", "smub"])
@pytest.mark.parametrize("field,value", [
    ("source.func", "OUTPUT_BOGUS"), ("source.output", "OUTPUT_MAYBE"),
    ("source.offmode", "OUTPUT_UNKNOWN"), ("source.autorangei", "AUTORANGE_MAYBE"),
    ("measure.autorangev", "2"), ("source.highc", "2"),
    ("source.leveli", "nan"), ("source.levelv", "1e999"),
    ("source.limitv", "-1"), ("source.rangei", "0"),
    ("source.rangev", "100"), ("source.leveli", "4"),
    ("measure.nplc", "0"), ("measure.delayfactor", "-1"),
    ("source.leveli", "1; smub.source.output = smub.OUTPUT_ON"),
])
def test_bad_values_leave_all_configuration_unchanged(smu, field, value):
    session = KeithleySimulator()
    if value.startswith(("OUTPUT_", "AUTORANGE_")):
        value = f"{smu}.{value}"
    before = deepcopy({key: value for key, value in vars(session).items() if key != "commands"})
    with pytest.raises(DeviceError):
        session.write(f"{smu}.{field} = {value}")
    after = {key: value for key, value in vars(session).items() if key != "commands"}
    assert after == before


def test_numeric_function_and_output_update_measurement_and_readback_together():
    session = KeithleySimulator()
    session.write("smua.source.levelv = 0.05")
    session.write("smua.source.func = 1")
    session.write("smua.source.output = 1")
    assert session.query("print(smua.source.func == smua.OUTPUT_DCVOLTS)") == "1"
    assert session.query("print(smua.source.output)") == "1"
    assert float(session.query("print(smua.measure.v())")) == .05
    session.write("smua.source.output = 0")
    assert float(session.query("print(smua.measure.v())")) == 0


def test_multi_range_query_requires_closing_parenthesis():
    session = KeithleySimulator()
    with pytest.raises(DeviceError, match="unsupported query"):
        session.query("print(smua.source.rangei, smua.measure.rangei]")
