"""One voltage envelope for manual control, recipes and field calibration.

DAC zero confirms only the programming signal. It cannot establish that the
Kepco power stage is disabled, that coil current has decayed, or that B is zero.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from app.domain.errors import SafetyViolation

if TYPE_CHECKING:
    from app.settings.models import StationSettings


def finite_number(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SafetyViolation(f"{name} must be a finite number.")


@dataclass(frozen=True, slots=True)
class MokeControlProfile:
    """Qualified physical binding and station envelope, all quantities in SI."""

    channel: int
    binding_id: str
    minimum_v: float
    maximum_v: float
    safe_v: float
    maximum_step_v: float
    maximum_slew_v_s: float
    step_interval_s: float
    ramp_timeout_s: float
    qualification_reference: str
    simulation: bool = False
    source_endpoint: str = ""
    kepco_model: str = "BOP 72-6M"
    kepco_mode: str = "current"
    minimum_settling_s: float = 2.0

    def __post_init__(self) -> None:
        if type(self.channel) is not int or self.channel not in range(8):
            raise SafetyViolation("MOKE VOUT channel must be an integer in 0..7.")
        if not self.binding_id.strip() or not self.qualification_reference.strip():
            raise SafetyViolation("MOKE control requires a binding and qualification reference.")
        if self.kepco_mode != "current" or not self.kepco_model.strip():
            raise SafetyViolation("This MOKE control profile requires an identified Kepco in current mode.")
        for name in ("minimum_v", "maximum_v", "safe_v", "maximum_step_v",
                     "maximum_slew_v_s", "step_interval_s", "ramp_timeout_s", "minimum_settling_s"):
            finite_number(getattr(self, name), name)
        if not -10 <= self.minimum_v < self.maximum_v <= 10:
            raise SafetyViolation("MOKE station range must be ordered within -10 V..10 V.")
        if self.safe_v != 0 or not self.minimum_v <= self.safe_v <= self.maximum_v:
            raise SafetyViolation("This MOKE implementation requires qualified DAC zero as safe target.")
        if min(self.maximum_step_v, self.maximum_slew_v_s,
               self.step_interval_s, self.ramp_timeout_s) <= 0:
            raise SafetyViolation("MOKE ramp step, slew, interval and timeout must be positive.")
        if self.step_interval_s > 1 or self.ramp_timeout_s > 3600:
            raise SafetyViolation("MOKE interval must be at most 1 s and ramp deadline at most 3600 s.")
        if not 0 <= self.minimum_settling_s < self.ramp_timeout_s:
            raise SafetyViolation("MOKE minimum settling time must be nonnegative and fit the ramp deadline.")
        if self.maximum_step_v < 10 / 32767 or self.maximum_slew_v_s * self.step_interval_s < 10 / 32767:
            raise SafetyViolation("MOKE ramp step must permit at least one DAC code per interval.")

    @property
    def fingerprint(self) -> str:
        source = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(source.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MokeVoltagePlan:
    """Immutable, one-shot trajectory and explicit experiment/operator limits."""

    profile_fingerprint: str
    channel: int
    minimum_v: float
    maximum_v: float
    targets_v: tuple[float, ...]
    settling_s: float = 0.0

    def validate(self, profile: MokeControlProfile) -> None:
        if type(self.channel) is not int or self.profile_fingerprint != profile.fingerprint or self.channel != profile.channel:
            raise SafetyViolation("MOKE plan does not match the qualified physical binding.")
        finite_number(self.minimum_v, "Working minimum")
        finite_number(self.maximum_v, "Working maximum")
        finite_number(self.settling_s, "MOKE settling time")
        if not 0 <= self.settling_s < profile.ramp_timeout_s:
            raise SafetyViolation("MOKE settling time must be nonnegative and fit the ramp deadline.")
        if not profile.minimum_v <= self.minimum_v < self.maximum_v <= profile.maximum_v:
            raise SafetyViolation("MOKE working min/max must fit within the station envelope.")
        if not isinstance(self.targets_v, tuple) or not 1 <= len(self.targets_v) <= 100_000:
            raise SafetyViolation("MOKE plan requires 1..100000 immutable voltage targets.")
        for value in self.targets_v:
            self.applied_voltage(value)

    def applied_voltage(self, value_v: float) -> float:
        """Round inward at a bound so a DAC code never expands the envelope."""
        from app.devices.moke_box.protocol import decode_voltage, encode_voltage

        finite_number(value_v, "MOKE voltage")
        if not self.minimum_v <= value_v <= self.maximum_v:
            raise SafetyViolation("MOKE voltage is outside the operator/experiment min/max.")
        msb, lsb = encode_voltage(value_v)
        code = (msb << 8) | lsb
        actual = decode_voltage(msb, lsb)
        if actual < self.minimum_v:
            code += 1
        elif actual > self.maximum_v:
            code -= 1
        actual = decode_voltage(code >> 8, code & 255)
        if not self.minimum_v <= actual <= self.maximum_v:
            raise SafetyViolation("MOKE working range contains no representable DAC target.")
        return actual


@dataclass(frozen=True, slots=True)
class MokeVoltageResult:
    channel: int
    requested_v: float
    applied_v: float
    actual_v: float
    profile_fingerprint: str
    safe_target_confirmed: bool = False


def control_profile_from_settings(settings: StationSettings, *, simulation: bool = False) -> MokeControlProfile:
    """Single conversion of explicit-unit settings into the runtime envelope."""
    from app.domain.quantities import (
        DIMENSION_TIME,
        DIMENSION_VOLTAGE,
        DIMENSION_VOLTAGE_SLEW,
        parse_quantity,
    )

    device = settings.moke_box
    envelope = device.voltage_control
    if not simulation and not (device.enabled and device.protocol_qualified and device.endpoint
                               and device.allow_vout_control and envelope.approved
                               and device.allowed_vout_channels == (envelope.channel,)):
        raise SafetyViolation("MOKE physical voltage output is not qualified and approved.")
    return MokeControlProfile(
        envelope.channel,
        "SIM::MOKE::COIL" if simulation else envelope.binding_id,
        parse_quantity(envelope.minimum, DIMENSION_VOLTAGE).si_value,
        parse_quantity(envelope.maximum, DIMENSION_VOLTAGE).si_value,
        parse_quantity(envelope.safe_target, DIMENSION_VOLTAGE).si_value,
        parse_quantity(envelope.maximum_step, DIMENSION_VOLTAGE).si_value,
        parse_quantity(envelope.maximum_slew, DIMENSION_VOLTAGE_SLEW).si_value,
        parse_quantity(envelope.step_interval, DIMENSION_TIME).si_value,
        parse_quantity(envelope.ramp_timeout, DIMENSION_TIME).si_value,
        "simulation-only" if simulation else envelope.qualification_reference,
        simulation,
        "SIM::MOKE::INSTR" if simulation else device.endpoint or "",
        envelope.kepco_model,
        envelope.kepco_mode,
        parse_quantity(envelope.minimum_settling_time, DIMENSION_TIME).si_value,
    )
