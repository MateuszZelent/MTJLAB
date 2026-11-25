"""Verified MS2830A Spectrum receiver settings for Sweep/FFT, Trigger and Gate."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from app.domain.errors import DeviceError, SafetyViolation

SWEEP_RULES = {
    "DRAN": "Dynamic range priority",
    "SPE": "Speed priority",
    "OSW": "Sweep only",
    "PSW": "Sweep priority",
    "PFFT": "FFT priority",
}
TIMING_SOURCES = {
    "IMM": "Free run", "EXT": "External input", "WIF": "Wide IF video",
    "VID": "Video", "FRAM": "Frame period", "SG": "SG marker",
}
TRIGGER_EDGES = {"POS": "Rising", "NEG": "Falling"}
FFT_WIDTHS_HZ = (40_000, 2_000_000)
SPECTRUM_TIMING_FIRMWARE = frozenset({"7.03.00", "sim-1.0"})


@dataclass(frozen=True, slots=True)
class SpectrumTimingSettings:
    sweep_rule: str = "DRAN"
    fft_width_hz: int = 40_000
    rbw_filter: str = "NORM"
    trigger_source: str = "IMM"
    external_edge: str = "POS"
    wide_if_edge: str = "POS"
    wide_if_level_dbm: float = -20
    video_edge: str = "POS"
    video_level_dbm: float = -40
    frame_period_s: float = 0.010
    frame_sync_source: str = "IMM"
    frame_offset_s: float = 0.0
    gate_enabled: bool = False
    gate_source: str = "IMM"
    gate_edge: str = "POS"
    gate_level_dbm: float = -20
    gate_delay_s: float = 0.0
    gate_length_s: float = 0.001

    @property
    def synchronized(self) -> bool:
        return self.gate_enabled or self.trigger_source != "IMM"

    def validate_sources(self) -> None:
        if self.gate_enabled and self.gate_source == "IMM":
            raise SafetyViolation("Choose a signal source for Gate.")
        if self.gate_enabled and self.trigger_source != "IMM":
            raise SafetyViolation("Gate uses its own source. Set the separate Trigger source to Free run.")


@dataclass(frozen=True, slots=True)
class TimingParameter:
    header: str
    choices: tuple = ()
    limits: tuple[float, float] | None = None
    resolution: float = 0.0
    suffix: str = ""

    def validate(self, name: str, value: object) -> None:
        if self.choices:
            valid = type(value) is type(self.choices[0]) and value in self.choices
        else:
            minimum, maximum = self.limits
            valid = type(value) in {int, float} and math.isfinite(value) and minimum <= value <= maximum
            if valid and self.suffix == "DBM":
                valid = float(value).is_integer()
        if not valid:
            raise SafetyViolation(f"Invalid Anritsu {name.replace('_', ' ')} {value!r}.")

    def parse(self, name: str, response: str) -> str | int | float | bool:
        text = response.strip().upper()
        try:
            if self.choices == (False, True):
                value = {"0": False, "+0": False, "OFF": False, "1": True, "+1": True, "ON": True}[text]
            elif self.choices == FFT_WIDTHS_HZ:
                value = int(text)
            elif self.choices:
                value = text
            else:
                value = float(text)
            self.validate(name, value)
        except (KeyError, ValueError, SafetyViolation) as exc:
            raise DeviceError(f"Anritsu returned invalid {name.replace('_', ' ')} {response!r}.") from exc
        return value

    def command(self, value: str | float | bool) -> str:
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        if type(value) in {int, float}:
            value = f"{value:.12g}{self.suffix}"
        return f"{self.header} {value}"

    def matches(self, left: object, right: object) -> bool:
        if self.limits is not None:
            return math.isclose(left, right, rel_tol=0.0, abs_tol=self.resolution / 2 + 1e-14)
        return left == right


# Spectrum Analyzer Remote Control, sections 2.3 and 2.7.
TIMING_PARAMETERS = {
    "sweep_rule": TimingParameter("SWE:RUL", tuple(SWEEP_RULES)),
    "fft_width_hz": TimingParameter("SWE:RUL:FFT:WIDT", FFT_WIDTHS_HZ, suffix="HZ"),
    "rbw_filter": TimingParameter("BAND:MODE", ("NORM", "CISP")),
    "trigger_source": TimingParameter("TRIG:SOUR", tuple(TIMING_SOURCES)),
    "external_edge": TimingParameter("TRIG:EXT:SLOP", tuple(TRIGGER_EDGES)),
    "wide_if_edge": TimingParameter("TRIG:WIF:SLOP", tuple(TRIGGER_EDGES)),
    "wide_if_level_dbm": TimingParameter("TRIG:WIF:LEV:ABS", limits=(-60, 50), resolution=1, suffix="DBM"),
    "video_edge": TimingParameter("TRIG:VID:SLOP", tuple(TRIGGER_EDGES)),
    "video_level_dbm": TimingParameter("TRIG:VID:LEV", limits=(-150, 50), resolution=1, suffix="DBM"),
    "frame_period_s": TimingParameter("TRIG:FRAM:PER", limits=(1e-6, 1), resolution=1e-8, suffix="S"),
    "frame_sync_source": TimingParameter("TRIG:FRAM:SYNC", ("IMM", "EXT", "WIF")),
    "frame_offset_s": TimingParameter("TRIG:FRAM:OFFS", limits=(0, 1), resolution=1e-8, suffix="S"),
    "gate_enabled": TimingParameter("SWE:EGAT", (False, True)),
    "gate_source": TimingParameter("SWE:EGAT:SOUR", ("IMM", "EXT", "WIF", "FRAM", "SG")),
    "gate_edge": TimingParameter("SWE:EGAT:SLOP", tuple(TRIGGER_EDGES)),
    "gate_level_dbm": TimingParameter("SWE:EGAT:WIF:LEV:ABS", limits=(-60, 50), resolution=1, suffix="DBM"),
    "gate_delay_s": TimingParameter("SWE:EGAT:DEL", limits=(0, 1), resolution=2e-8, suffix="S"),
    "gate_length_s": TimingParameter("SWE:EGAT:LENG", limits=(1e-5, 1), resolution=2e-8, suffix="S"),
}
TRIGGER_FIELDS = {
    "EXT": ("external_edge",), "WIF": ("wide_if_edge", "wide_if_level_dbm"),
    "VID": ("video_edge", "video_level_dbm"),
}
FRAME_FIELDS = ("frame_period_s", "frame_sync_source", "frame_offset_s")
GATE_FIELDS = ("gate_source", "gate_edge", "gate_level_dbm", "gate_delay_s", "gate_length_s")


@dataclass(frozen=True, slots=True)
class SpectrumTimingSnapshot:
    settings: SpectrumTimingSettings
    effective_method: str
    actual_fft_width_hz: int | None
    sweep_time_auto: bool
    continuous: bool
    rbw_filter_switchable: bool

    def context(self) -> str:
        """Serialize only settings that affect the acquired spectrum."""
        settings = self.settings
        values = {
            "sweep_rule": settings.sweep_rule,
            "method": self.effective_method,
            "rbw_filter": settings.rbw_filter,
            "trigger": settings.trigger_source,
            "gate": settings.gate_enabled,
        }
        if self.actual_fft_width_hz is not None:
            values["fft_width_hz"] = self.actual_fft_width_hz
        for field in TRIGGER_FIELDS.get(settings.trigger_source, ()):
            values[field] = getattr(settings, field)
        if settings.gate_enabled:
            for field in GATE_FIELDS:
                values[field] = getattr(settings, field)
            if settings.gate_source != "WIF":
                values.pop("gate_level_dbm")
        if settings.trigger_source == "FRAM" or settings.gate_enabled and settings.gate_source == "FRAM":
            for field in FRAME_FIELDS:
                values[field] = getattr(settings, field)
        return json.dumps(values, sort_keys=True, allow_nan=False)


_LEGACY_DEFAULT_CONTEXT = SpectrumTimingSnapshot(
    SpectrumTimingSettings(), "SWE", None, True, True, False,
).context()


def timing_comparison_key(context: str | None) -> str | None:
    """Keep the original acquisition identity for the default free-running setup."""
    return None if context == _LEGACY_DEFAULT_CONTEXT else context
