"""Advisory DC/resistive-load estimates, separate from hardware safety bounds."""

import math
from dataclasses import dataclass

from app.safety.rigol_current import estimate_rigol_current


@dataclass(frozen=True)
class LoadEnvelope:
    current_min_a: float
    current_max_a: float
    voltage_min_v: float
    voltage_max_v: float

    @property
    def peak_current_a(self) -> float:
        return max(abs(self.current_min_a), abs(self.current_max_a))

    @property
    def peak_voltage_v(self) -> float:
        return max(abs(self.voltage_min_v), abs(self.voltage_max_v))


@dataclass(frozen=True)
class SampleLoadEstimate:
    nominal: LoadEnvelope
    resistance_range: LoadEnvelope


def estimate_sample_load(
    *, high_v: float, low_v: float, output_load: str,
    resistance_ohm: float, minimum_ohm: float, maximum_ohm: float,
    waveform: str,
) -> SampleLoadEstimate:
    """Estimate signed extrema over a cycle and a positive resistance interval.

    HIGHZ describes the displayed voltage convention, not source impedance.
    The source is always 50 ohm. This model assumes a directly connected,
    passive resistor; it cannot certify an MTJ or predict RF/transient behavior.
    """
    if waveform not in {"DC", "SIN", "SQU", "RAMP", "PULS"}:
        raise ValueError("Estimate available for DC, sine, square, ramp and pulse only.")
    if not all(math.isfinite(v) for v in (high_v, low_v)) or high_v < low_v:
        raise ValueError("Enter valid voltage levels: High must be at least Low.")
    if waveform == "DC" and high_v != low_v:
        raise ValueError("DC requires one voltage level.")
    if not all(math.isfinite(r) and r > 0 for r in (
        resistance_ohm, minimum_ohm, maximum_ohm,
    )):
        raise ValueError("Resistance must be finite and greater than zero.")
    if not minimum_ohm <= resistance_ohm <= maximum_ohm:
        raise ValueError("Require minimum resistance <= measured resistance <= maximum resistance.")
    source = estimate_rigol_current(
        high_level=high_v, low_level=low_v, output_load=output_load,
    )

    def envelope(resistances: tuple[float, ...]) -> LoadEnvelope:
        pairs = [
            (v / (r + source.source_resistance_ohm),
             v * (r / (r + source.source_resistance_ohm)))
            for r in resistances
            for v in (source.open_circuit_low_v, source.open_circuit_high_v)
        ]
        if not all(math.isfinite(value) for pair in pairs for value in pair):
            raise ValueError("Estimate exceeds the finite numeric range.")
        currents, voltages = zip(*pairs)
        return LoadEnvelope(min(currents), max(currents), min(voltages), max(voltages))

    return SampleLoadEstimate(
        nominal=envelope((resistance_ohm,)),
        resistance_range=envelope((minimum_ohm, maximum_ohm)),
    )
