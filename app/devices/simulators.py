"""Deterministic VISA simulations for operator training and automated tests."""

from __future__ import annotations

from dataclasses import dataclass
import math
from random import Random
import re
from typing import Literal

from app.devices.base import InstrumentSession
from app.devices.simulation import SimulationContext
from app.domain.errors import ConnectionError, DeviceError
from app.settings.models import StationSettings


@dataclass(frozen=True, slots=True)
class SimulatorFault:
    """Deterministic fault injection for adapter and run-engine tests.

    Prefixes are matched case-insensitively against complete VISA commands.
    The simulator never introduces random faults, keeping a failed test fully
    reproducible.
    """

    timeout_prefixes: frozenset[str] = frozenset()
    disconnect_prefixes: frozenset[str] = frozenset()
    device_error_prefixes: frozenset[str] = frozenset()
    malformed_response_prefixes: frozenset[str] = frozenset()


class _FaultInjectingSession:
    def __init__(self, session: InstrumentSession, fault: SimulatorFault) -> None:
        self._session = session
        self._fault = fault

    @property
    def timeout(self) -> int:
        return self._session.timeout

    @timeout.setter
    def timeout(self, value: int) -> None:
        self._session.timeout = value

    @property
    def read_termination(self) -> str | None:
        return self._session.read_termination

    @read_termination.setter
    def read_termination(self, value: str | None) -> None:
        self._session.read_termination = value

    @property
    def write_termination(self) -> str | None:
        return self._session.write_termination

    @write_termination.setter
    def write_termination(self, value: str | None) -> None:
        self._session.write_termination = value

    @staticmethod
    def _matches(command: str, prefixes: frozenset[str]) -> bool:
        normalized = command.strip().upper()
        return any(normalized.startswith(prefix.upper()) for prefix in prefixes)

    def _inject(self, command: str) -> None:
        if self._matches(command, self._fault.disconnect_prefixes):
            self._session.close()
            raise ConnectionError(f"Simulator disconnected while executing {command!r}.")
        if self._matches(command, self._fault.timeout_prefixes):
            raise DeviceError(f"Simulator timeout while executing {command!r}.")
        if self._matches(command, self._fault.device_error_prefixes):
            raise DeviceError(f"Simulator device error while executing {command!r}.")

    def write(self, command: str) -> object:
        self._inject(command)
        return self._session.write(command)

    def query(self, command: str) -> str:
        self._inject(command)
        if self._matches(command, self._fault.malformed_response_prefixes):
            return "MALFORMED_RESPONSE"
        return self._session.query(command)

    def close(self) -> object:
        return self._session.close()


class _BaseSimulator:
    timeout = 3_000
    read_termination: str | None = "\n"
    write_termination: str | None = "\n"

    def __init__(self) -> None:
        self.closed = False
        self.commands: list[str] = []
        self.error_queue: list[str] = []
        self.command_errors: dict[str, str] = {}

    def _assert_open(self) -> None:
        if self.closed:
            raise DeviceError("The simulated VISA session is closed.")

    def write(self, command: str) -> None:
        self._assert_open()
        self.commands.append(command)
        normalized = command.strip()
        if normalized.upper() == "*CLS":
            self.error_queue.clear()
        else:
            for prefix, message in self.command_errors.items():
                if normalized.upper().startswith(prefix.upper()):
                    self.error_queue.append(message)
                    break
        self._write(normalized)

    def query(self, command: str) -> str:
        self._assert_open()
        self.commands.append(command)
        normalized = command.strip()
        if normalized.upper().lstrip(":") == "SYST:ERR?":
            return self.error_queue.pop(0) if self.error_queue else "0,No error"
        return self._query(normalized)

    def close(self) -> None:
        self.closed = True

    def _write(self, command: str) -> None:
        del command

    def _query(self, command: str) -> str:
        raise DeviceError(f"The simulator does not support query {command!r}.")


class RigolSimulator(_BaseSimulator):
    def __init__(self) -> None:
        super().__init__()
        self.waveform = {1: "SIN", 2: "SIN"}
        self.frequency = {1: 1_000.0, 2: 1_000.0}
        self.high = {1: 0.001, 2: 0.001}
        self.low = {1: -0.001, 2: -0.001}
        self.output = {1: False, 2: False}
        self.load = {1: "INF", 2: "INF"}
        self.polarity = {1: "NORM", 2: "NORM"}
        self.output_mode = {1: "NORM", 2: "NORM"}
        self.gate_polarity = {1: "NORM", 2: "NORM"}
        self.sync = {1: False, 2: False}
        self.sync_polarity = {1: "NORM", 2: "NORM"}
        self.sync_delay = {1: 0.0, 2: 0.0}
        self.modulation = {1: False, 2: False}
        self.sweep = {1: False, 2: False}
        self.burst = {1: False, 2: False}
        self.programmed_scpi: dict[str, str] = {}

    def _write(self, command: str) -> None:
        assignment = re.match(r"^(:[A-Za-z0-9:]+)\s+(.+)$", command)
        if assignment:
            self.programmed_scpi[assignment.group(1).upper()] = assignment.group(2)
        match = re.match(r"^:OUTP([12])\s+(ON|OFF)$", command, re.IGNORECASE)
        if match:
            self.output[int(match.group(1))] = match.group(2).upper() == "ON"
            return
        match = re.match(r"^:OUTP([12]):LOAD\s+(\S+)$", command, re.IGNORECASE)
        if match:
            self.load[int(match.group(1))] = match.group(2).upper()
            return
        match = re.match(r"^:OUTP([12]):POL\s+(NORM|INV)$", command, re.IGNORECASE)
        if match:
            self.polarity[int(match.group(1))] = match.group(2).upper()
            return
        match = re.match(r"^:OUTP([12]):MODE\s+(NORM|GAT)$", command, re.IGNORECASE)
        if match:
            self.output_mode[int(match.group(1))] = match.group(2).upper()
            return
        match = re.match(r"^:OUTP([12]):GAT:POL\s+(NORM|INV)$", command, re.IGNORECASE)
        if match:
            self.gate_polarity[int(match.group(1))] = match.group(2).upper()
            return
        match = re.match(r"^:OUTP([12]):SYNC\s+(ON|OFF)$", command, re.IGNORECASE)
        if match:
            self.sync[int(match.group(1))] = match.group(2).upper() == "ON"
            return
        match = re.match(r"^:OUTP([12]):SYNC:POL\s+(NORM|INV)$", command, re.IGNORECASE)
        if match:
            self.sync_polarity[int(match.group(1))] = match.group(2).upper()
            return
        match = re.match(r"^:OUTP([12]):SYNC:DEL\s+([+-]?[\d.eE]+)$", command, re.IGNORECASE)
        if match:
            self.sync_delay[int(match.group(1))] = float(match.group(2))
            return
        match = re.match(r"^:SOUR([12]):MOD\s+(ON|OFF)$", command, re.IGNORECASE)
        if match:
            self.modulation[int(match.group(1))] = match.group(2).upper() == "ON"
            return
        match = re.match(r"^:SOUR([12]):SWE:STAT\s+(ON|OFF)$", command, re.IGNORECASE)
        if match:
            self.sweep[int(match.group(1))] = match.group(2).upper() == "ON"
            return
        match = re.match(r"^:SOUR([12]):BURS\s+(ON|OFF)$", command, re.IGNORECASE)
        if match:
            self.burst[int(match.group(1))] = match.group(2).upper() == "ON"
            return
        match = re.match(r"^:SOUR([12]):FUNC\s+(\w+)$", command, re.IGNORECASE)
        if match:
            self.waveform[int(match.group(1))] = match.group(2).upper()
            return
        match = re.match(
            r"^:SOUR([12]):APPL:DC\s+DEF,DEF,([+-]?[\d.eE]+)$",
            command,
            re.IGNORECASE,
        )
        if match:
            channel, value = int(match.group(1)), float(match.group(2))
            self.waveform[channel] = "DC"
            self.high[channel] = value
            self.low[channel] = value
            return
        match = re.match(r"^:SOUR([12]):FREQ\s+([+-]?[\d.eE]+)$", command, re.IGNORECASE)
        if match:
            self.frequency[int(match.group(1))] = float(match.group(2))
            return
        match = re.match(r"^:SOUR([12]):VOLT:(HIGH|LOW)\s+([+-]?[\d.eE]+)$", command, re.IGNORECASE)
        if match:
            channel, level, value = int(match.group(1)), match.group(2).upper(), float(match.group(3))
            if level == "HIGH":
                self.high[channel] = value
            else:
                self.low[channel] = value

    def _query(self, command: str) -> str:
        if command == "*IDN?":
            return "Rigol Technologies,DG1032Z,SIM000001,sim-1.0"
        if command == ":SYST:VERS?":
            return "1999.0"
        if command == ":SYST:CHAN:NUM?":
            return "2"
        if command == ":SYST:ERR?":
            return "0,No error"
        match = re.match(r"^:OUTP([12])\?$", command, re.IGNORECASE)
        if match:
            return "ON" if self.output[int(match.group(1))] else "OFF"
        match = re.match(r"^:OUTP([12]):LOAD\?$", command, re.IGNORECASE)
        if match:
            return "9.9E37" if self.load[int(match.group(1))] == "INF" else self.load[int(match.group(1))]
        match = re.match(r"^:OUTP([12]):POL\?$", command, re.IGNORECASE)
        if match:
            return self.polarity[int(match.group(1))]
        match = re.match(r"^:OUTP([12]):MODE\?$", command, re.IGNORECASE)
        if match:
            return self.output_mode[int(match.group(1))]
        match = re.match(r"^:OUTP([12]):GAT:POL\?$", command, re.IGNORECASE)
        if match:
            return self.gate_polarity[int(match.group(1))]
        match = re.match(r"^:OUTP([12]):SYNC\?$", command, re.IGNORECASE)
        if match:
            return "ON" if self.sync[int(match.group(1))] else "OFF"
        match = re.match(r"^:OUTP([12]):SYNC:POL\?$", command, re.IGNORECASE)
        if match:
            return self.sync_polarity[int(match.group(1))]
        match = re.match(r"^:OUTP([12]):SYNC:DEL\?$", command, re.IGNORECASE)
        if match:
            return f"{self.sync_delay[int(match.group(1))]:.12g}"
        match = re.match(r"^:SOUR([12]):MOD\?$", command, re.IGNORECASE)
        if match:
            return "ON" if self.modulation[int(match.group(1))] else "OFF"
        match = re.match(r"^:SOUR([12]):SWE:STAT\?$", command, re.IGNORECASE)
        if match:
            return "ON" if self.sweep[int(match.group(1))] else "OFF"
        match = re.match(r"^:SOUR([12]):BURS:STAT\?$", command, re.IGNORECASE)
        if match:
            return "ON" if self.burst[int(match.group(1))] else "OFF"
        match = re.match(r"^:SOUR([12]):PHAS\?$", command, re.IGNORECASE)
        if match:
            return "0"
        match = re.match(r"^:SOUR([12]):FUNC\?$", command, re.IGNORECASE)
        if match:
            return self.waveform[int(match.group(1))]
        match = re.match(r"^:SOUR([12]):FREQ\?$", command, re.IGNORECASE)
        if match:
            return f"{self.frequency[int(match.group(1))]:.12g}"
        match = re.match(r"^:SOUR([12]):VOLT:(HIGH|LOW)\?$", command, re.IGNORECASE)
        if match:
            values = self.high if match.group(2).upper() == "HIGH" else self.low
            return f"{values[int(match.group(1))]:.12g}"
        readback = re.match(r"^(:[A-Za-z0-9:]+)\?$", command)
        if readback and readback.group(1).upper() in self.programmed_scpi:
            return self.programmed_scpi[readback.group(1).upper()]
        raise DeviceError(f"Rigol simulator: unsupported query {command!r}.")


class KeithleySimulator(_BaseSimulator):
    def __init__(self) -> None:
        super().__init__()
        self.mode = {"smua": "current", "smub": "current"}
        self.level = {"smua": 0.0, "smub": 0.0}
        self.output = {"smua": False, "smub": False}
        self.resistance_ohm = {"smua": 10.0, "smub": 10.0}
        self.limit_voltage = {"smua": float("inf"), "smub": float("inf")}
        self.limit_current = {"smua": float("inf"), "smub": float("inf")}
        self.noise_fraction = 0.0
        self._measurement_index = {"smua": 0, "smub": 0}
        self.programmed: dict[str, str] = {}

    def _write(self, command: str) -> None:
        if command == "errorqueue.clear()":
            self.error_queue.clear()
            return
        assignment = re.match(
            r"^(smu[ab]\.(?:(?:source|measure)\.[A-Za-z0-9_]+|sense))\s*=\s*(.+)$",
            command,
        )
        if assignment:
            self.programmed[assignment.group(1)] = assignment.group(2)
        mode = re.match(r"^(smu[ab])\.source\.func\s*=\s*\1\.(OUTPUT_DCAMPS|OUTPUT_DCVOLTS)$", command)
        if mode:
            self.mode[mode.group(1)] = "current" if mode.group(2) == "OUTPUT_DCAMPS" else "voltage"
            return
        level = re.match(r"^(smu[ab])\.source\.level[iv]\s*=\s*([+-]?[\d.eE]+)$", command)
        if level:
            self.level[level.group(1)] = float(level.group(2))
            return
        limit = re.match(r"^(smu[ab])\.source\.limit([vi])\s*=\s*([+-]?[\d.eE]+)$", command)
        if limit:
            target, quantity, value = limit.group(1), limit.group(2), float(limit.group(3))
            if quantity == "v":
                self.limit_voltage[target] = abs(value)
            else:
                self.limit_current[target] = abs(value)
            return
        output = re.match(r"^(smu[ab])\.source\.output\s*=\s*\1\.(OUTPUT_ON|OUTPUT_OFF)$", command)
        if output:
            self.output[output.group(1)] = output.group(2) == "OUTPUT_ON"

    def _query(self, command: str) -> str:
        if command == "*IDN?":
            return "KEITHLEY INSTRUMENTS,2602A,SIM000001,sim-1.0"
        if command == "print(errorqueue.count)":
            return str(len(self.error_queue))
        if command == "print(errorqueue.next())":
            if not self.error_queue:
                raise DeviceError("Keithley simulator: errorqueue.next() called on an empty queue.")
            return self.error_queue.pop(0)
        output = re.match(r"^print\((smu[ab])\.source\.output\)$", command)
        if output:
            return "1" if self.output[output.group(1)] else "0"
        level = re.match(r"^print\((smu[ab])\.source\.level[iv]\)$", command)
        if level:
            return f"{self.level[level.group(1)]:.12g}"
        readback = re.match(
            r"^print\((smu[ab]\.(?:(?:source|measure)\.[A-Za-z0-9_]+|sense))\)$",
            command,
        )
        if readback and readback.group(1) in self.programmed:
            return self.programmed[readback.group(1)]
        measure_iv = re.match(r"^print\((smu[ab])\.measure\.iv\(\)\)$", command)
        if measure_iv:
            voltage, current = self._measured_iv(measure_iv.group(1))
            return f"{current:.12g}\t{voltage:.12g}"
        measure = re.match(r"^print\((smu[ab])\.measure\.([vi])\(\)\)$", command)
        if measure:
            smu, quantity = measure.groups()
            voltage, current = self._measured_iv(smu)
            return f"{voltage if quantity == 'v' else current:.12g}"
        raise DeviceError(f"Keithley simulator: unsupported query {command!r}.")

    def _measured_iv(self, smu: str) -> tuple[float, float]:
        """Return a deterministic resistive-DUT measurement with compliance.

        The programmed source is clipped at the appropriate compliance limit,
        matching the observable behaviour that the adapter must react to.
        """

        if not self.output[smu]:
            return 0.0, 0.0
        resistance = self.resistance_ohm[smu]
        if resistance <= 0:
            raise DeviceError("Keithley simulator: DUT resistance must be positive.")
        level = self.level[smu]
        if self.mode[smu] == "current":
            current = level
            voltage = current * resistance
            if abs(voltage) > self.limit_voltage[smu]:
                voltage = math.copysign(self.limit_voltage[smu], voltage)
                current = voltage / resistance
        else:
            voltage = level
            current = voltage / resistance
            if abs(current) > self.limit_current[smu]:
                current = math.copysign(self.limit_current[smu], current)
                voltage = current * resistance

        # Optional repeatable readback noise makes long-running UI and recipe
        # tests realistic without introducing randomness into failures. The
        # independent phases make derived resistance vary like a real reading.
        index = self._measurement_index[smu]
        self._measurement_index[smu] += 1
        if self.noise_fraction:
            voltage *= 1.0 + self.noise_fraction * math.sin(index * 0.73 + 0.31)
            current *= 1.0 + self.noise_fraction * math.sin(index * 0.91 + 1.17)
        return voltage, current


class AnritsuSimulator(_BaseSimulator):
    def __init__(self, *, random_source: Random | None = None) -> None:
        super().__init__()
        self._random = random_source
        self.start_hz = 1e6
        self.stop_hz = 10e6
        self.points = 1001
        self.reference_level = 0.0
        self.continuous_sweep = True
        self.trace_frame = 0
        self.instrument_mode = "SPECT"
        self.sg_frequency_hz = 1e9
        self.sg_power_dbm = -30.0
        self.sg_output = False
        self.rbw_auto = True
        self.rbw_hz = 1e3
        self.vbw_auto = True
        self.vbw_hz: float | None = 1e3
        self.detector = "NORM"
        self.attenuation_auto = True
        self.attenuation_db = 10.0
        self.preamplifier_enabled = False
        self.sweep_time_auto = True
        self.sweep_time_s = 0.1

    def _write(self, command: str) -> None:
        mode = re.match(r"^INST\s+(SPECT|SG)$", command, re.IGNORECASE)
        if mode:
            self.instrument_mode = mode.group(1).upper()
            return
        output = re.match(r"^OUTP\s+(ON|OFF|1|0)$", command, re.IGNORECASE)
        if output:
            if self.instrument_mode != "SG":
                raise DeviceError("Anritsu simulator: OUTP is available only in SG mode.")
            self.sg_output = output.group(1).upper() in {"ON", "1"}
            return
        sg_frequency = re.match(r"^FREQ\s+([+-]?[\d.eE]+)HZ$", command, re.IGNORECASE)
        if sg_frequency:
            if self.instrument_mode != "SG":
                raise DeviceError("Anritsu simulator: SG FREQ requires SG mode.")
            self.sg_frequency_hz = float(sg_frequency.group(1))
            return
        sg_power = re.match(r"^POW\s+([+-]?[\d.eE]+)$", command, re.IGNORECASE)
        if sg_power:
            if self.instrument_mode != "SG":
                raise DeviceError("Anritsu simulator: SG POW requires SG mode.")
            self.sg_power_dbm = float(sg_power.group(1))
            return
        if command.upper() == "UNIT:POW DBM":
            if self.instrument_mode != "SG":
                raise DeviceError("Anritsu simulator: UNIT:POW requires SG mode.")
            return
        switch_commands = {
            "BAND:AUTO": "rbw_auto",
            "BAND:VID:AUTO": "vbw_auto",
            "POW:ATT:AUTO": "attenuation_auto",
            "POW:GAIN": "preamplifier_enabled",
            "SWE:TIME:AUTO": "sweep_time_auto",
        }
        switch = re.match(
            r"^(BAND:AUTO|BAND:VID:AUTO|POW:ATT:AUTO|POW:GAIN|SWE:TIME:AUTO)\s+"
            r"(ON|OFF|1|0)$",
            command,
            re.IGNORECASE,
        )
        if switch:
            setattr(
                self,
                switch_commands[switch.group(1).upper()],
                switch.group(2).upper() in {"ON", "1"},
            )
            return
        rbw = re.match(r"^BAND\s+([+-]?[\d.eE]+)HZ$", command, re.IGNORECASE)
        if rbw:
            self.rbw_auto = False
            self.rbw_hz = float(rbw.group(1))
            return
        vbw = re.match(r"^BAND:VID\s+([+-]?[\d.eE]+)HZ$", command, re.IGNORECASE)
        if vbw:
            self.vbw_auto = False
            self.vbw_hz = float(vbw.group(1))
            return
        if command.upper() == "BAND:VID OFF":
            self.vbw_auto = False
            self.vbw_hz = None
            return
        detector = re.match(
            r"^DET\s+(NORM|POS|SAMP|NEG|RMS|QPE|CAV|CRMS)$",
            command,
            re.IGNORECASE,
        )
        if detector:
            self.detector = detector.group(1).upper()
            return
        attenuation = re.match(
            r"^POW:ATT\s+([+-]?[\d.eE]+)(?:DB)?$", command, re.IGNORECASE
        )
        if attenuation:
            self.attenuation_auto = False
            self.attenuation_db = float(attenuation.group(1))
            return
        sweep_time = re.match(
            r"^SWE:TIME\s+([+-]?[\d.eE]+)S$", command, re.IGNORECASE
        )
        if sweep_time:
            self.sweep_time_auto = False
            self.sweep_time_s = float(sweep_time.group(1))
            return
        match = re.match(r"^FREQ:(STAR|STOP)\s+([+-]?[\d.eE]+)HZ$", command, re.IGNORECASE)
        if match:
            value = float(match.group(2))
            if match.group(1).upper() == "STAR":
                self.start_hz = value
            else:
                self.stop_hz = value
            return
        match = re.match(r"^SWE:POIN\s+(\d+)$", command, re.IGNORECASE)
        if match:
            self.points = int(match.group(1))
            return
        match = re.match(r"^DISP:WIND:TRAC:Y:RLEV\s+([+-]?[\d.eE]+)$", command, re.IGNORECASE)
        if match:
            self.reference_level = float(match.group(1))
            return
        match = re.match(r"^INIT:CONT\s+(ON|OFF|1|0)$", command, re.IGNORECASE)
        if match:
            self.continuous_sweep = match.group(1).upper() in {"ON", "1"}

    def _query(self, command: str) -> str:
        if command == "*IDN?":
            return "ANRITSU,MS2830A,SIM000001,sim-1.0"
        if command == "*OPT?":
            return "041,008,020"
        if command == "*OPC?":
            return "1"
        if command == "FORM?":
            return "ASC,0"
        if command == "INST?":
            return self.instrument_mode
        if command == "FREQ?":
            if self.instrument_mode != "SG":
                raise DeviceError("Anritsu simulator: SG FREQ? requires SG mode.")
            return f"{self.sg_frequency_hz:.12g}"
        if command == "POW?":
            if self.instrument_mode != "SG":
                raise DeviceError("Anritsu simulator: SG POW? requires SG mode.")
            return f"{self.sg_power_dbm:.12g}"
        if command == "OUTP?":
            if self.instrument_mode != "SG":
                raise DeviceError("Anritsu simulator: OUTP? requires SG mode.")
            return "1" if self.sg_output else "0"
        switch_queries = {
            "BAND:AUTO?": self.rbw_auto,
            "BAND:VID:AUTO?": self.vbw_auto,
            "POW:ATT:AUTO?": self.attenuation_auto,
            "POW:GAIN?": self.preamplifier_enabled,
            "SWE:TIME:AUTO?": self.sweep_time_auto,
        }
        if command in switch_queries:
            return "1" if switch_queries[command] else "0"
        if command == "BAND?":
            return f"{self.rbw_hz:.12g}"
        if command == "BAND:VID?":
            return "OFF" if self.vbw_hz is None else f"{self.vbw_hz:.12g}"
        if command == "DET?":
            return self.detector
        if command == "POW:ATT?":
            return f"{self.attenuation_db:.12g}"
        if command == "SWE:TIME?":
            return f"{self.sweep_time_s:.12g}"
        if command == "TRAC:TYPE?":
            return "WRIT"
        if command == "INIT:CONT?":
            return "1" if self.continuous_sweep else "0"
        if command == "FREQ:STAR?":
            return f"{self.start_hz:.12g}"
        if command == "FREQ:STOP?":
            return f"{self.stop_hz:.12g}"
        if command == "SWE:POIN?":
            return str(self.points)
        if command == "DISP:WIND:TRAC:Y:RLEV?":
            return f"{self.reference_level:.12g}"
        if command.startswith("TRAC? "):
            if self.continuous_sweep:
                self.trace_frame += 1
            center = (self.start_hz + self.stop_hz) / 2
            span = max(self.stop_hz - self.start_hz, 1.0)
            values = []
            for index in range(self.points):
                frequency = self.start_hz + span * index / (self.points - 1)
                peak = 35 * math.exp(-((frequency - center) / (span / 12)) ** 2)
                ripple = 2 * math.sin(index / 25 + self.trace_frame / 7)
                noise = self._random.uniform(-0.15, 0.15) if self._random else 0.0
                values.append(f"{-80 + peak + ripple + noise:.8g}")
            return ",".join(values)
        raise DeviceError(f"Anritsu simulator: unsupported query {command!r}.")


class SimulatedVisaFactory:
    """Create a fresh simulated session for one named instrument family."""

    def __init__(
        self,
        device: Literal["rigol", "keithley", "anritsu"],
        *,
        context: SimulationContext | None = None,
        fault: SimulatorFault | None = None,
        keithley_resistance_ohm: float = 10.0,
        keithley_noise_fraction: float = 0.0,
        queued_errors: tuple[str, ...] = (),
        command_errors: dict[str, str] | None = None,
        keithley_error_queue: tuple[str, ...] = (),
        keithley_command_errors: dict[str, str] | None = None,
    ) -> None:
        self.device = device
        self.context = context
        self.fault = fault
        self.keithley_resistance_ohm = keithley_resistance_ohm
        if not math.isfinite(keithley_noise_fraction) or keithley_noise_fraction < 0:
            raise ValueError("keithley_noise_fraction must be finite and non-negative.")
        self.keithley_noise_fraction = keithley_noise_fraction
        self.queued_errors = queued_errors
        self.command_errors = command_errors or {}
        self.keithley_error_queue = keithley_error_queue
        self.keithley_command_errors = keithley_command_errors or {}

    def open(self, resource: str, backend: str, timeout_ms: int) -> InstrumentSession:
        del resource, backend
        session: InstrumentSession
        if self.device == "rigol":
            session = RigolSimulator()
        elif self.device == "keithley":
            session = KeithleySimulator()
            session.resistance_ohm = {"smua": self.keithley_resistance_ohm, "smub": self.keithley_resistance_ohm}
            session.noise_fraction = self.keithley_noise_fraction
        else:
            session = AnritsuSimulator(
                random_source=(
                    self.context.random_stream("anritsu", "trace")
                    if self.context is not None
                    else None
                )
            )
        session.error_queue = list(self.queued_errors)
        session.error_queue.extend(self.keithley_error_queue if self.device == "keithley" else ())
        session.command_errors = dict(self.command_errors)
        if self.device == "keithley":
            session.command_errors.update(self.keithley_command_errors)
        session.timeout = timeout_ms
        return _FaultInjectingSession(session, self.fault) if self.fault is not None else session


def simulated_station_settings(settings: StationSettings) -> StationSettings:
    """Create an in-memory profile that routes every device to a simulator.

    The persisted lab profile is never modified.  RF acquisition is enabled
    only inside this synthetic runtime profile, while the profile still starts
    unverified and therefore keeps energy outputs locked.
    """

    raw = settings.model_dump(mode="python")
    raw["profile"]["state"] = "approved"
    raw["profile"]["approved_by"] = "SIMULATION"
    raw["profile"]["approved_at"] = "in-memory"
    raw["profile"]["approval_note"] = "Synthetic profile; not persisted to settings.yml."
    raw["devices"]["rigol"]["connection"]["resource"] = "SIM::RIGOL::INSTR"
    raw["devices"]["rigol"]["identity"]["require_serial_match"] = False
    raw["devices"]["rigol"]["identity"]["expected_serial"] = None
    raw["devices"]["keithley"]["connection"]["resource"] = "SIM::KEITHLEY::INSTR"
    raw["devices"]["anritsu"]["connection"]["resource"] = "SIM::ANRITSU::INSTR"
    raw["devices"]["anritsu"]["acquisition"]["single_sweep_mode"] = "standard_scpi_opc"
    raw["devices"]["anritsu"]["safety"]["acquisition_allowed"] = True
    raw["devices"]["anritsu"]["safety"]["rf_input"]["max_expected_power_at_connector"] = "-10 dBm"
    raw["devices"]["anritsu"]["safety"]["frequency"] = {"min": "1 Hz", "max": "100 GHz"}
    raw["devices"]["anritsu"]["safety"]["reference_level"] = {"min": "-150 dBm", "max": "30 dBm"}
    raw["devices"]["moke_box"].update(
        {
            "enabled": True,
            "endpoint": "SIM::MOKE::INSTR",
            "expected_model": "MOKE SIM",
            "protocol_qualified": True,
        }
    )
    return StationSettings.model_validate(raw)
