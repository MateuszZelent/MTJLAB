"""Documented MS2830A hardware-option limits and option parsing."""

from __future__ import annotations

from dataclasses import dataclass
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


def parse_anritsu_option_response(response: str) -> tuple[str, ...]:
    """Normalize option identifiers returned by the IEEE-488.2 ``*OPT?`` query."""

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


def frequency_option_for(options: tuple[str, ...]) -> AnritsuFrequencyOption | None:
    """Return the installed frequency option, preferring the widest recognized one."""

    installed = [ANRITSU_FREQUENCY_OPTIONS[code] for code in options if code in ANRITSU_FREQUENCY_OPTIONS]
    return max(installed, key=lambda option: option.maximum_stop_hz, default=None)

