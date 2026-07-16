"""Prosty test komunikacji z generatorem Rigol DG1032Z."""

from __future__ import annotations

import argparse

import pyvisa


DEFAULT_ADDRESS = "USB0::0x1AB1::0x0642::DG1ZA172902039::INSTR"
WAVEFORMS = ("SIN", "SQU", "RAMP", "PULS")


def read_errors(instrument: pyvisa.resources.MessageBasedResource) -> list[str]:
    """Odczytaj i oproznij kolejke bledow SCPI generatora."""
    errors: list[str] = []
    for _ in range(20):
        response = instrument.query(":SYST:ERR?").strip()
        if response.startswith("0,"):
            return errors
        errors.append(response)
    errors.append("Kolejka bledow nie zostala oprozniona po 20 odczytach")
    return errors


def print_channel_state(
    instrument: pyvisa.resources.MessageBasedResource,
) -> None:
    """Wyswietl ustawienia kanalu 1 odczytane z generatora."""
    function = instrument.query(":SOUR1:FUNC?").strip()
    frequency_hz = float(instrument.query(":SOUR1:FREQ?"))
    high_level_v = float(instrument.query(":SOUR1:VOLT:HIGH?"))
    low_level_v = float(instrument.query(":SOUR1:VOLT:LOW?"))
    output = instrument.query(":OUTP1?").strip()

    amplitude_vpp = high_level_v - low_level_v
    offset_v = (high_level_v + low_level_v) / 2

    print("Kanal 1:")
    print(f"  ksztalt:    {function}")
    print(f"  czestot.:   {frequency_hz:g} Hz")
    print(f"  HighLevel:  {high_level_v:g} V")
    print(f"  LowLevel:   {low_level_v:g} V")
    print(f"  amplituda:  {amplitude_vpp:g} Vpp")
    print(f"  offset:     {offset_v:g} V")
    print(f"  wyjscie:    {output}")


def configure_channel(
    instrument: pyvisa.resources.MessageBasedResource,
    waveform: str,
    frequency_hz: float,
    high_level_v: float,
    low_level_v: float,
    enable_output: bool,
) -> None:
    """Ustaw kanal 1 za pomoca poziomow HighLevel i LowLevel."""
    if high_level_v <= low_level_v:
        raise ValueError("HighLevel musi byc wiekszy od LowLevel")
    if frequency_hz <= 0:
        raise ValueError("Czestotliwosc musi byc dodatnia")

    # Konfiguracja odbywa sie przy wylaczonym wyjsciu.
    instrument.write(":OUTP1 OFF")
    instrument.write(f":SOUR1:FUNC {waveform}")
    instrument.write(f":SOUR1:FREQ:FIX {frequency_hz:.12g}")
    instrument.write(":SOUR1:VOLT:UNIT VPP")
    instrument.write(f":SOUR1:VOLT:HIGH {high_level_v:.12g}")
    instrument.write(f":SOUR1:VOLT:LOW {low_level_v:.12g}")

    errors = read_errors(instrument)
    if errors:
        raise RuntimeError("Bledy SCPI po konfiguracji: " + "; ".join(errors))

    if enable_output:
        instrument.write(":OUTP1 ON")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default=DEFAULT_ADDRESS)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="zastosuj ustawienia; bez tej opcji skrypt tylko odczytuje stan",
    )
    parser.add_argument("--waveform", choices=WAVEFORMS, default="SIN")
    parser.add_argument("--frequency", type=float, default=10_000.0)
    parser.add_argument("--high-level", type=float, default=2.5)
    parser.add_argument("--low-level", type=float, default=-2.5)
    parser.add_argument(
        "--enable-output",
        action="store_true",
        help="wlacz wyjscie CH1 po konfiguracji (wymaga --apply)",
    )
    args = parser.parse_args()
    if args.enable_output and not args.apply:
        parser.error("--enable-output wymaga rowniez --apply")
    return args


def main() -> None:
    args = parse_args()
    resource_manager = pyvisa.ResourceManager()
    instrument = None

    try:
        # Bezposrednie otwarcie dziala nawet wtedy, gdy list_resources() zwraca
        # blad w niektorych instalacjach VISA na Windows.
        instrument = resource_manager.open_resource(args.address, open_timeout=3_000)
        instrument.timeout = 3_000

        identity = instrument.query("*IDN?").strip()
        print(f"Polaczono: {identity}")
        if "DG1032Z" not in identity:
            raise RuntimeError("Pod adresem VISA nie znaleziono Rigol DG1032Z")

        old_errors = read_errors(instrument)
        if old_errors:
            print("Poprzednie bledy SCPI:", "; ".join(old_errors))

        if args.apply:
            configure_channel(
                instrument,
                waveform=args.waveform,
                frequency_hz=args.frequency,
                high_level_v=args.high_level,
                low_level_v=args.low_level,
                enable_output=args.enable_output,
            )
            print("Ustawienia zostaly zastosowane.")

        print_channel_state(instrument)
    finally:
        if instrument is not None:
            instrument.close()
        resource_manager.close()


if __name__ == "__main__":
    main()
