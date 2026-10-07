"""Documented MS2830A hardware-option limits and option parsing."""

from __future__ import annotations

from dataclasses import dataclass
import csv
import re


@dataclass(frozen=True, slots=True)
class AnritsuFrequencyOption:
    code: str
    maximum_stop_hz: float
    default_sweep_time_s: float


# MS2830A Spectrum Analyzer Remote Control manual, sections 2.1 and 2.7.
ANRITSU_FREQUENCY_OPTIONS: dict[str, AnritsuFrequencyOption] = {
    "040": AnritsuFrequencyOption("040", 3.7e9, 1e-3),
    "041": AnritsuFrequencyOption("041", 6.1e9, 2e-3),
    "043": AnritsuFrequencyOption("043", 13.6e9, 4e-3),
    "044": AnritsuFrequencyOption("044", 26.6e9, 89e-3),
    "045": AnritsuFrequencyOption("045", 43.1e9, 86e-3),
}

ANRITSU_PREAMPLIFIER_OPTIONS = frozenset({"008", "108", "068", "168"})
# MS2830A remote manual, option notes on pages 272-273 and 698-700.
ANRITSU_SIGNAL_GENERATOR_OPTIONS = frozenset({"020", "120", "021", "121"})


def parse_anritsu_option_response(response: str) -> tuple[str, ...]:
    """Normalize an explicit list of option identifiers (not a device catalogue)."""

    value = response.strip().upper()
    if not value or value in {"0", "NONE", "NO OPTION", "NO OPTIONS"}:
        return ()
    options: list[str] = []
    for token in re.split(r"[,;\s]+", value):
        token = token.strip()
        if not token:
            continue
        match = re.search(r"(?:^|[-_/])(\d{3})(?:$|[-_/])", token)
        normalized = match.group(1) if match else token
        if normalized not in options:
            options.append(normalized)
    return tuple(options)


def parse_anritsu_hardware_catalog(response: str, *, native: bool = False) -> tuple[str, ...]:
    """Parse installed hardware options, excluding catalogue entries marked OFF.

    Mainframe Remote Control 4-116: count followed by number/switch/name triples.
    Native OPTINFO? HARD (6-75) returns triples, optionally preceded by a count.
    """
    columns = next(csv.reader([response], strict=True))
    columns = [column.strip() for column in columns]
    if columns == ["0"]:
        return ()
    if not native or len(columns) % 3 == 1:
        if not columns or not columns[0].isdigit():
            raise ValueError("Anritsu hardware catalogue has no valid entry count.")
        count = int(columns.pop(0))
        if len(columns) != count * 3:
            raise ValueError("Anritsu hardware catalogue count does not match its entries.")
    elif not columns or len(columns) % 3:
        raise ValueError("Anritsu hardware catalogue contains incomplete entries.")
    installed = []
    for offset in range(0, len(columns), 3):
        number, switch, name = columns[offset:offset + 3]
        if not number.isascii() or not number.isdigit() or not 0 <= int(number) <= 999 or not name:
            raise ValueError("Anritsu hardware catalogue contains an invalid option entry.")
        switch = switch.upper()
        if switch not in {"ON", "OFF", "1", "0"}:
            raise ValueError("Anritsu hardware catalogue contains an unknown option switch.")
        code = f"{int(number):03d}"
        if switch in {"ON", "1"} and code not in installed:
            installed.append(code)
    return tuple(installed)


def frequency_option_for(options: tuple[str, ...]) -> AnritsuFrequencyOption | None:
    """Return the installed frequency option, preferring the widest recognized one."""

    installed = [ANRITSU_FREQUENCY_OPTIONS[code] for code in options if code in ANRITSU_FREQUENCY_OPTIONS]
    return max(installed, key=lambda option: option.maximum_stop_hz, default=None)
