"""Deterministic Model 475 simulator for adapter and recipe tests."""

from __future__ import annotations

from app.devices.visa import FakeVisaSession


def simulated_475_session(*, field: float = 0.0, unit_code: str = "2", mode_code: str = "1") -> FakeVisaSession:
    """Return a simple session implementing the safe read-only command subset."""

    return FakeVisaSession(
        responses={
            "*IDN?": "LSCI,MODEL475,SIM475,sim-1.0",
            "RDGFIELD?": f"{field:.12g}",
            "UNIT?": unit_code,
            "RDGMODE?": f"{mode_code},3,1,1,1",
            "RANGE?": "0",
            "AUTO?": "1",
            "TYPE?": "40",
            "RDGFRQ?": "60",
            "RDGPEAK?": f"{-abs(field):.12g},{abs(field):.12g}",
        }
    )
