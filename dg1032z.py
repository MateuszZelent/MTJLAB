"""Warstwa komunikacji SCPI z generatorem Rigol DG1032Z."""

from __future__ import annotations

from typing import Any, Iterable

import pyvisa


DEFAULT_ADDRESS = "USB0::0x1AB1::0x0642::DG1ZA172902039::INSTR"
WAVEFORMS = ("SIN", "SQU", "RAMP", "PULS", "NOIS", "USER", "DC")
MODULATION_TYPES = ("AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM")


class InstrumentError(RuntimeError):
    """Blad komunikacji lub blad zgloszony przez generator."""


def _channel(number: int) -> int:
    if number not in (1, 2):
        raise ValueError("Numer kanalu musi wynosic 1 albo 2")
    return number


def _number(value: Any, name: str, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: oczekiwano liczby") from exc
    if minimum is not None and result < minimum:
        raise ValueError(f"{name}: minimalna wartosc to {minimum:g}")
    return result


def _on(value: str) -> bool:
    return value.strip().upper() in {"ON", "1"}


def _short(value: str) -> str:
    """Ujednolic dlugie odpowiedzi SCPI do skrotow uzywanych przez GUI."""
    value = value.strip().upper()
    aliases = {
        "SINE": "SIN",
        "SQUARE": "SQU",
        "PULSE": "PULS",
        "NOISE": "NOIS",
        "ARBITRARY": "USER",
        "INTERNAL": "INT",
        "EXTERNAL": "EXT",
        "MANUAL": "MAN",
        "TRIGGERED": "TRIG",
        "INFINITY": "INF",
        "GATED": "GAT",
        "NORMAL": "NORM",
        "INVERTED": "INV",
        "POSITIVE": "POS",
        "NEGATIVE": "NEG",
        "LINEAR": "LIN",
        "LOGARITHMIC": "LOG",
        "STEP": "STEP",
    }
    return aliases.get(value, value)


class DG1032Z:
    """Sterownik wysokiego poziomu dla dwoch kanalow DG1032Z.

    Obiekt powinien byc uzywany tylko z jednego watku. GUI spelnia ten warunek,
    kierujac wszystkie operacje VISA do dedykowanego watku roboczego.
    """

    def __init__(self) -> None:
        self.resource_manager: pyvisa.ResourceManager | None = None
        self.instrument: pyvisa.resources.MessageBasedResource | None = None
        self.identity = ""

    @property
    def connected(self) -> bool:
        return self.instrument is not None

    def connect(self, address: str = DEFAULT_ADDRESS) -> str:
        self.disconnect()
        try:
            self.resource_manager = pyvisa.ResourceManager()
            self.instrument = self.resource_manager.open_resource(
                address.strip(), open_timeout=3_000
            )
            self.instrument.timeout = 4_000
            self.identity = self.query("*IDN?")
            if "DG1032Z" not in self.identity.upper():
                raise InstrumentError(
                    f"Pod adresem VISA nie znaleziono DG1032Z: {self.identity}"
                )
            self.write("*CLS")
            return self.identity
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        if self.instrument is not None:
            try:
                self.instrument.close()
            finally:
                self.instrument = None
        if self.resource_manager is not None:
            try:
                self.resource_manager.close()
            finally:
                self.resource_manager = None
        self.identity = ""

    def _require_instrument(self) -> pyvisa.resources.MessageBasedResource:
        if self.instrument is None:
            raise InstrumentError("Brak polaczenia z generatorem")
        return self.instrument

    def write(self, command: str) -> None:
        self._require_instrument().write(command)

    def query(self, command: str) -> str:
        return self._require_instrument().query(command).strip()

    def read_errors(self, limit: int = 20) -> list[str]:
        errors: list[str] = []
        for _ in range(limit):
            response = self.query(":SYST:ERR?")
            if response.startswith("0,"):
                return errors
            errors.append(response)
        errors.append(f"Kolejka nie zostala oprozniona po {limit} odczytach")
        return errors

    def execute(self, commands: Iterable[str]) -> None:
        self.write("*CLS")
        for command in commands:
            self.write(command)
        errors = self.read_errors()
        if errors:
            raise InstrumentError("; ".join(errors))

    def raw(self, command: str) -> str | None:
        command = command.strip()
        if not command:
            raise ValueError("Polecenie SCPI jest puste")
        if command.endswith("?"):
            return self.query(command)
        self.write(command)
        return None

    def device_info(self) -> dict[str, str]:
        return {
            "identity": self.query("*IDN?"),
            "version": self.query(":SYST:VERS?"),
            "channels": self.query(":SYST:CHAN:NUM?"),
        }

    def read_channel(self, channel: int) -> dict[str, Any]:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        output = f":OUTP{channel}"
        waveform = _short(self.query(f"{source}:FUNC?"))

        result: dict[str, Any] = {
            "waveform": waveform,
            "frequency": float(self.query(f"{source}:FREQ?")),
            "phase": float(self.query(f"{source}:PHAS?")),
            "output": _on(self.query(f"{output}?")),
            "load": self._read_load(channel),
            "polarity": _short(self.query(f"{output}:POL?")),
            "output_mode": _short(self.query(f"{output}:MODE?")),
            "gate_polarity": _short(self.query(f"{output}:GAT:POL?")),
            "sync": _on(self.query(f"{output}:SYNC?")),
            "sync_polarity": _short(self.query(f"{output}:SYNC:POL?")),
            "sync_delay": float(self.query(f"{output}:SYNC:DEL?")),
            "square_duty": 50.0,
            "ramp_symmetry": 50.0,
            "pulse_width": 0.0005,
            "pulse_leading": 2e-8,
            "pulse_trailing": 2e-8,
        }

        if waveform == "DC":
            dc_level = float(self.query(f"{source}:VOLT:OFFS?"))
            result.update(dc_level=dc_level, high_level=dc_level, low_level=dc_level)
        else:
            result.update(
                dc_level=float(self.query(f"{source}:VOLT:OFFS?")),
                high_level=float(self.query(f"{source}:VOLT:HIGH?")),
                low_level=float(self.query(f"{source}:VOLT:LOW?")),
            )

        if waveform == "SQU":
            result["square_duty"] = float(
                self.query(f"{source}:FUNC:SQU:DCYC?")
            )
        elif waveform == "RAMP":
            result["ramp_symmetry"] = float(
                self.query(f"{source}:FUNC:RAMP:SYMM?")
            )
        elif waveform == "PULS":
            result["pulse_width"] = float(
                self.query(f"{source}:FUNC:PULS:WIDT?")
            )
            result["pulse_leading"] = float(
                self.query(f"{source}:FUNC:PULS:TRAN:LEAD?")
            )
            result["pulse_trailing"] = float(
                self.query(f"{source}:FUNC:PULS:TRAN:TRA?")
            )
        return result

    def _read_load(self, channel: int) -> str:
        value = float(self.query(f":OUTP{channel}:LOAD?"))
        return "HIGHZ" if value > 1e30 else f"{value:g}"

    def apply_waveform(self, channel: int, settings: dict[str, Any]) -> None:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        waveform = str(settings["waveform"]).upper()
        if waveform not in WAVEFORMS:
            raise ValueError(f"Nieobslugiwany ksztalt: {waveform}")

        commands: list[str] = []
        if settings.get("safe_output_off", True):
            commands.append(f":OUTP{channel} OFF")

        if waveform == "DC":
            dc_level = _number(settings["dc_level"], "Poziom DC")
            commands.append(f"{source}:APPL:DC DEF,DEF,{dc_level:.12g}")
        else:
            high = _number(settings["high_level"], "HighLevel")
            low = _number(settings["low_level"], "LowLevel")
            if high <= low:
                raise ValueError("HighLevel musi byc wiekszy od LowLevel")
            commands.append(f"{source}:FUNC {waveform}")
            if waveform != "NOIS":
                frequency = _number(settings["frequency"], "Czestotliwosc", 1e-6)
                commands.append(f"{source}:FREQ {frequency:.12g}")

            # Kolejnosc zabezpiecza przypadek, gdy nowy HighLevel znajduje sie
            # ponizej aktualnego LowLevel.
            current_low = float(self.query(f"{source}:VOLT:LOW?"))
            level_commands = [
                f"{source}:VOLT:HIGH {high:.12g}",
                f"{source}:VOLT:LOW {low:.12g}",
            ]
            if high <= current_low:
                level_commands.reverse()
            commands.extend(level_commands)

            if waveform not in {"NOIS"}:
                phase = _number(settings["phase"], "Faza")
                commands.append(f"{source}:PHAS {phase:.12g}")

            if waveform == "SQU":
                duty = _number(settings["square_duty"], "Duty cycle", 0.01)
                commands.append(f"{source}:FUNC:SQU:DCYC {duty:.12g}")
            elif waveform == "RAMP":
                symmetry = _number(settings["ramp_symmetry"], "Symetria", 0.0)
                commands.append(f"{source}:FUNC:RAMP:SYMM {symmetry:.12g}")
            elif waveform == "PULS":
                width = _number(settings["pulse_width"], "Szerokosc impulsu", 0.0)
                leading = _number(settings["pulse_leading"], "Zbocze narastajace", 0.0)
                trailing = _number(settings["pulse_trailing"], "Zbocze opadajace", 0.0)
                commands.extend(
                    (
                        f"{source}:FUNC:PULS:WIDT {width:.12g}",
                        f"{source}:FUNC:PULS:TRAN:LEAD {leading:.12g}",
                        f"{source}:FUNC:PULS:TRAN:TRA {trailing:.12g}",
                    )
                )
        self.execute(commands)

    def apply_output(self, channel: int, settings: dict[str, Any]) -> None:
        channel = _channel(channel)
        output = f":OUTP{channel}"
        load_text = str(settings["load"]).strip().upper()
        if load_text in {"HIGHZ", "INF", "INFINITY"}:
            load = "INF"
        else:
            load_value = _number(load_text, "Obciazenie", 1.0)
            if load_value > 10_000:
                raise ValueError("Obciazenie moze wynosic maksymalnie 10000 ohm")
            load = f"{load_value:.12g}"

        commands = [
            f"{output} OFF",
            f"{output}:LOAD {load}",
            f"{output}:POL {settings['polarity']}",
            f"{output}:MODE {settings['output_mode']}",
            f"{output}:GAT:POL {settings['gate_polarity']}",
            f"{output}:SYNC:DEL {_number(settings['sync_delay'], 'Opoznienie Sync', 0.0):.12g}",
            f"{output}:SYNC:POL {settings['sync_polarity']}",
            f"{output}:SYNC {'ON' if settings['sync'] else 'OFF'}",
        ]
        if settings["output"]:
            commands.append(f"{output} ON")
        self.execute(commands)

    def set_output(self, channel: int, enabled: bool) -> bool:
        channel = _channel(channel)
        self.execute((f":OUTP{channel} {'ON' if enabled else 'OFF'}",))
        return _on(self.query(f":OUTP{channel}?"))

    def read_modulation(self, channel: int) -> dict[str, Any]:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        kind = _short(self.query(f"{source}:MOD:TYPE?"))
        result: dict[str, Any] = {
            "enabled": _on(self.query(f"{source}:MOD?")),
            "type": kind,
            "source": _short(self.query(f"{source}:{kind}:SOUR?")),
            "internal_shape": "SIN",
            "rate": 100.0,
            "parameter": 1.0,
            "polarity": "POS",
        }
        if kind in {"AM", "FM", "PM", "PWM"}:
            result["rate"] = float(self.query(f"{source}:{kind}:INT:FREQ?"))
            result["internal_shape"] = _short(
                self.query(f"{source}:{kind}:INT:FUNC?")
            )
        else:
            result["rate"] = float(self.query(f"{source}:{kind}:INT:RATE?"))
            result["polarity"] = _short(self.query(f"{source}:{kind}:POL?"))

        parameter_commands = {
            "AM": "DEPT",
            "FM": "DEV",
            "PM": "DEV",
            "ASK": "AMPL",
            "FSK": "FREQ",
            "PSK": "PHAS",
            "PWM": "DEV:DCYC",
        }
        result["parameter"] = float(
            self.query(f"{source}:{kind}:{parameter_commands[kind]}?")
        )
        return result

    def apply_modulation(self, channel: int, settings: dict[str, Any]) -> None:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        kind = str(settings["type"]).upper()
        if kind not in MODULATION_TYPES:
            raise ValueError(f"Nieobslugiwany typ modulacji: {kind}")
        commands = [f"{source}:MOD OFF", f"{source}:MOD:TYPE {kind}"]
        commands.append(f"{source}:{kind}:SOUR {settings['source']}")

        if kind in {"AM", "FM", "PM", "PWM"}:
            commands.extend(
                (
                    f"{source}:{kind}:INT:FREQ {_number(settings['rate'], 'Czestotliwosc modulacji', 0.0):.12g}",
                    f"{source}:{kind}:INT:FUNC {settings['internal_shape']}",
                )
            )
        else:
            commands.extend(
                (
                    f"{source}:{kind}:INT:RATE {_number(settings['rate'], 'Szybkosc kluczowania', 0.0):.12g}",
                    f"{source}:{kind}:POL {settings['polarity']}",
                )
            )

        parameter_commands = {
            "AM": "DEPT",
            "FM": "DEV",
            "PM": "DEV",
            "ASK": "AMPL",
            "FSK": "FREQ",
            "PSK": "PHAS",
            "PWM": "DEV:DCYC",
        }
        parameter = _number(settings["parameter"], "Parametr modulacji", 0.0)
        commands.append(f"{source}:{kind}:{parameter_commands[kind]} {parameter:.12g}")
        commands.append(f"{source}:MOD {'ON' if settings['enabled'] else 'OFF'}")
        self.execute(commands)

    def read_sweep(self, channel: int) -> dict[str, Any]:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        return {
            "enabled": _on(self.query(f"{source}:SWE:STAT?")),
            "start": float(self.query(f"{source}:FREQ:STAR?")),
            "stop": float(self.query(f"{source}:FREQ:STOP?")),
            "time": float(self.query(f"{source}:SWE:TIME?")),
            "spacing": _short(self.query(f"{source}:SWE:SPAC?")),
            "steps": int(float(self.query(f"{source}:SWE:STEP?"))),
            "start_hold": float(self.query(f"{source}:SWE:HTIM:STAR?")),
            "stop_hold": float(self.query(f"{source}:SWE:HTIM:STOP?")),
            "return_time": float(self.query(f"{source}:SWE:RTIM?")),
            "trigger_source": _short(self.query(f"{source}:SWE:TRIG:SOUR?")),
            "trigger_slope": _short(self.query(f"{source}:SWE:TRIG:SLOP?")),
            "trigger_out": _short(self.query(f"{source}:SWE:TRIG:TRIGO?")),
        }

    def apply_sweep(self, channel: int, settings: dict[str, Any]) -> None:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        start = _number(settings["start"], "Czestotliwosc startowa", 1e-6)
        stop = _number(settings["stop"], "Czestotliwosc koncowa", 1e-6)
        if start == stop:
            raise ValueError("Czestotliwosci Start i Stop musza sie roznic")
        steps = int(_number(settings["steps"], "Liczba krokow", 2))
        commands = [
            f"{source}:SWE:STAT OFF",
            f"{source}:FREQ:STAR {start:.12g}",
            f"{source}:FREQ:STOP {stop:.12g}",
            f"{source}:SWE:TIME {_number(settings['time'], 'Czas sweep', 0.001):.12g}",
            f"{source}:SWE:SPAC {settings['spacing']}",
            f"{source}:SWE:STEP {steps}",
            f"{source}:SWE:HTIM:STAR {_number(settings['start_hold'], 'Start hold', 0.0):.12g}",
            f"{source}:SWE:HTIM:STOP {_number(settings['stop_hold'], 'Stop hold', 0.0):.12g}",
            f"{source}:SWE:RTIM {_number(settings['return_time'], 'Return time', 0.0):.12g}",
            f"{source}:SWE:TRIG:SOUR {settings['trigger_source']}",
            f"{source}:SWE:TRIG:SLOP {settings['trigger_slope']}",
            f"{source}:SWE:TRIG:TRIGO {settings['trigger_out']}",
            f"{source}:SWE:STAT {'ON' if settings['enabled'] else 'OFF'}",
        ]
        self.execute(commands)

    def trigger_sweep(self, channel: int) -> None:
        self.execute((f":SOUR{_channel(channel)}:SWE:TRIG",))

    def read_burst(self, channel: int) -> dict[str, Any]:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        return {
            "enabled": _on(self.query(f"{source}:BURS?")),
            "mode": _short(self.query(f"{source}:BURS:MODE?")),
            "cycles": int(float(self.query(f"{source}:BURS:NCYC?"))),
            "phase": float(self.query(f"{source}:BURS:PHAS?")),
            "period": float(self.query(f"{source}:BURS:INT:PER?")),
            "delay": float(self.query(f"{source}:BURS:TDEL?")),
            "trigger_source": _short(self.query(f"{source}:BURS:TRIG:SOUR?")),
            "trigger_slope": _short(self.query(f"{source}:BURS:TRIG:SLOP?")),
            "trigger_out": _short(self.query(f"{source}:BURS:TRIG:TRIGO?")),
            "gate_polarity": _short(self.query(f"{source}:BURS:GATE:POL?")),
            "idle": _short(self.query(f"{source}:BURS:IDLE?")),
        }

    def apply_burst(self, channel: int, settings: dict[str, Any]) -> None:
        channel = _channel(channel)
        source = f":SOUR{channel}"
        commands = [
            f"{source}:BURS OFF",
            f"{source}:BURS:MODE {settings['mode']}",
            f"{source}:BURS:NCYC {int(_number(settings['cycles'], 'Liczba cykli', 1))}",
            f"{source}:BURS:PHAS {_number(settings['phase'], 'Faza burst'):.12g}",
            f"{source}:BURS:INT:PER {_number(settings['period'], 'Okres burst', 0.0):.12g}",
            f"{source}:BURS:TDEL {_number(settings['delay'], 'Opoznienie burst', 0.0):.12g}",
            f"{source}:BURS:TRIG:SOUR {settings['trigger_source']}",
            f"{source}:BURS:TRIG:SLOP {settings['trigger_slope']}",
            f"{source}:BURS:TRIG:TRIGO {settings['trigger_out']}",
            f"{source}:BURS:GATE:POL {settings['gate_polarity']}",
            f"{source}:BURS:IDLE {settings['idle']}",
            f"{source}:BURS {'ON' if settings['enabled'] else 'OFF'}",
        ]
        self.execute(commands)

    def trigger_burst(self, channel: int) -> None:
        self.execute((f":SOUR{_channel(channel)}:BURS:TRIG",))

    def synchronize_phases(self) -> None:
        self.execute((":SOUR1:PHAS:SYNC",))

    def reset(self) -> None:
        self.execute(("*RST",))

