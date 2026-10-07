"""Simulation rejects malformed traffic instead of silently certifying it."""

import pytest

from app.devices.simulators import AnritsuSimulator
from app.domain.errors import DeviceError


@pytest.mark.parametrize("command", [
    "FREQ:STRT 100HZ", "BAND:AUTO MAYBE", "FORM BANANA", "FORM REAL,64",
    "FORM:BORD INVALID", "FORM REAL,32 GARBAGE", "TRAC:TYPE 9,WRIT",
    "ABORT", "INIT:MODE:SIGN", "OUTP MAYBE", "TRAC1:TYPE WRIT;OUTP ON",
])
def test_unknown_write_is_explicit_error_without_configuration_mutation(command):
    session = AnritsuSimulator()
    before = {key: value for key, value in vars(session).items() if key not in {"commands", "error_queue"}}
    with pytest.raises(DeviceError, match="unsupported write"):
        session.write(command)
    after = {key: value for key, value in vars(session).items() if key not in {"commands", "error_queue"}}
    assert after == before


@pytest.mark.parametrize("command", ["TRAC:TYPE? garbage", "TRAC:TYPE?;OUTP ON", "TRAC? TRAC2", "TRAC? nonsense", "TRAC? TRAC1 junk"])
def test_trace_queries_must_match_complete_supported_grammar(command):
    session = AnritsuSimulator()
    with pytest.raises(DeviceError, match="unsupported query"):
        session.query(command)
    assert session.trace_frame == 0


def test_trace_format_and_indexed_mode_have_real_readback_state():
    session = AnritsuSimulator()
    session.write("FORM REAL,32")
    assert session.query("FORM?") == "REAL,32"
    session.write("FORM ASC")
    assert session.query("FORM?") == "ASC,0"
    session.write("FORM:BORD NORM")
    assert session.query("FORM:BORD?") == "NORM"
    session.write("FORM:BORD SWAP")
    assert session.query("FORM:BORD?") == "SWAP"
    session.write("TRAC:TYPE 1,VIEW")
    assert session.query("TRAC:TYPE?") == "VIEW"
    session.write("TRAC1:TYPE WRIT")
    assert session.query("TRAC:TYPE?") == "WRIT"


def test_documented_application_control_commands_remain_available():
    session = AnritsuSimulator()
    session.error_queue.append("-100,test error")
    for command in ("*CLS", "ABOR", "INIT:MODE:SING", "*WAI"):
        session.write(command)
    assert session.query("SYST:ERR?") == "0,No error"
    assert session.query("INIT:SWP?") == "0"
    assert session.query("INIT:CONT?") == "0"
    assert len(session.query("TRAC? TRAC1").split(",")) == session.points
