"""Deterministic VISA simulations for operator training and automated tests."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Literal

from app.devices.base import InstrumentSession
from app.domain.errors import DeviceError
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
            raise DeviceError(f"Symulator: rozłączenie podczas {command!r}.")
        if self._matches(command, self._fault.timeout_prefixes):
            raise DeviceError(f"Symulator: timeout podczas {command!r}.")
        if self._matches(command, self._fault.device_error_prefixes):
            raise DeviceError(f"Symulator: błąd urządzenia podczas {command!r}.")

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

    def _assert_open(self) -> None:
        if self.closed:
            raise DeviceError("Symulowana sesja VISA jest zamknięta.")

    def write(self, command: str) -> None:
        self._assert_open()
        self.commands.append(command)
        self._write(command.strip())

    def query(self, command: str) -> str:
        self._assert_open()
        self.commands.append(command)
        return self._query(command.strip())

    def close(self) -> None:
        self.closed = True

    def _write(self, command: str) -> None:
        del command

    def _query(self, command: str) -> str:
        raise DeviceError(f"Symulator nie obsługuje zapytania {command!r}.")


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

    def _write(self, command: str) -> None:
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
        raise DeviceError(f"Rigol simulator: nieobsługiwane zapytanie {command!r}.")


class KeithleySimulator(_BaseSimulator):
    def __init__(self) -> None:
        super().__init__()
        self.mode = {"smua": "current", "smub": "current"}
        self.level = {"smua": 0.0, "smub": 0.0}
        self.output = {"smua": False, "smub": False}
        self.resistance_ohm = {"smua": 10.0, "smub": 10.0}
        self.limit_voltage = {"smua": float("inf"), "smub": float("inf")}
        self.limit_current = {"smua": float("inf"), "smub": float("inf")}
        self.error_queue: list[str] = []
        self.command_errors: dict[str, str] = {}

    def _write(self, command: str) -> None:
        if command == "errorqueue.clear()":
            self.error_queue.clear()
            return
        for prefix, message in self.command_errors.items():
            if command.upper().startswith(prefix.upper()):
                self.error_queue.append(message)
                break
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
                raise DeviceError("Keithley simulator: errorqueue.next() przy pustej kolejce.")
            return self.error_queue.pop(0)
        output = re.match(r"^print\((smu[ab])\.source\.output\)$", command)
        if output:
            return "1" if self.output[output.group(1)] else "0"
        measure = re.match(r"^print\((smu[ab])\.measure\.([vi])\(\)\)$", command)
        if measure:
            smu, quantity = measure.groups()
            voltage, current = self._measured_iv(smu)
            return f"{voltage if quantity == 'v' else current:.12g}"
        raise DeviceError(f"Keithley simulator: nieobsługiwane zapytanie {command!r}.")

    def _measured_iv(self, smu: str) -> tuple[float, float]:
        """Return a deterministic resistive-DUT measurement with compliance.

        The programmed source is clipped at the appropriate compliance limit,
        matching the observable behaviour that the adapter must react to.
        """

        if not self.output[smu]:
            return 0.0, 0.0
        resistance = self.resistance_ohm[smu]
        if resistance <= 0:
            raise DeviceError("Keithley simulator: rezystancja DUT musi być dodatnia.")
        level = self.level[smu]
        if self.mode[smu] == "current":
            current = level
            voltage = current * resistance
            if abs(voltage) > self.limit_voltage[smu]:
                voltage = math.copysign(self.limit_voltage[smu], voltage)
                current = voltage / resistance
            return voltage, current
        voltage = level
        current = voltage / resistance
        if abs(current) > self.limit_current[smu]:
            current = math.copysign(self.limit_current[smu], current)
            voltage = current * resistance
        return voltage, current


class AnritsuSimulator(_BaseSimulator):
    def __init__(self) -> None:
        super().__init__()
        self.start_hz = 1e6
        self.stop_hz = 10e6
        self.points = 1001
        self.reference_level = 0.0

    def _write(self, command: str) -> None:
        match = re.match(r"^FREQ:(START|STOP)\s+([+-]?[\d.eE]+)HZ$", command, re.IGNORECASE)
        if match:
            value = float(match.group(2))
            if match.group(1).upper() == "START":
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

    def _query(self, command: str) -> str:
        if command == "*IDN?":
            return "ANRITSU,MS2830A,SIM000001,sim-1.0"
        if command == "*OPC?":
            return "1"
        if command == "FREQ:START?":
            return f"{self.start_hz:.12g}"
        if command == "FREQ:STOP?":
            return f"{self.stop_hz:.12g}"
        if command == "SWE:POIN?":
            return str(self.points)
        if command.startswith("TRAC? "):
            center = (self.start_hz + self.stop_hz) / 2
            span = max(self.stop_hz - self.start_hz, 1.0)
            values = []
            for index in range(self.points):
                frequency = self.start_hz + span * index / (self.points - 1)
                peak = 35 * math.exp(-((frequency - center) / (span / 12)) ** 2)
                ripple = 2 * math.sin(index / 25)
                values.append(f"{-80 + peak + ripple:.8g}")
            return ",".join(values)
        raise DeviceError(f"Anritsu simulator: nieobsługiwane zapytanie {command!r}.")


class SimulatedVisaFactory:
    """Create a fresh simulated session for one named instrument family."""

    def __init__(
        self,
        device: Literal["rigol", "keithley", "anritsu"],
        *,
        fault: SimulatorFault | None = None,
        keithley_resistance_ohm: float = 10.0,
        keithley_error_queue: tuple[str, ...] = (),
        keithley_command_errors: dict[str, str] | None = None,
    ) -> None:
        self.device = device
        self.fault = fault
        self.keithley_resistance_ohm = keithley_resistance_ohm
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
            session.error_queue = list(self.keithley_error_queue)
            session.command_errors = dict(self.keithley_command_errors)
        else:
            session = AnritsuSimulator()
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
    raw["profile"]["approval_note"] = "Profil syntetyczny; nie zapisano do settings.yml."
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
    return StationSettings.model_validate(raw)
