"""Safe, explicit adapter for the Rigol DG1032Z signal generator."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Literal

from app.devices.base import (
    DeviceAdapter,
    InstrumentSession,
    OutputInterlock,
    SessionFactory,
    parse_identity,
    validate_identity,
)
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.safety.rigol_current import RigolCurrentEstimate, validate_rigol_waveform
from app.settings.models import RigolSettings, StationSettings


@dataclass(frozen=True, slots=True)
class RigolChannelConfig:
    channel: int
    waveform: str
    frequency_hz: float
    high_level_v: float
    low_level_v: float
    output_load: str | float = "HIGHZ"
    phase_deg: float = 0.0
    square_duty_percent: float | None = None
    ramp_symmetry_percent: float | None = None
    pulse_width_s: float | None = None
    pulse_leading_s: float | None = None
    pulse_trailing_s: float | None = None
    dut_min_impedance_ohm: float | None = None


@dataclass(frozen=True, slots=True)
class RigolOutputConfig:
    """Output-path settings that are safe to change only at OUTPUT OFF."""

    channel: int
    output_load: str | float = "HIGHZ"
    polarity: Literal["NORM", "INV"] = "NORM"
    mode: Literal["NORM", "GAT"] = "NORM"
    gate_polarity: Literal["NORM", "INV"] = "NORM"
    sync_enabled: bool = False
    sync_polarity: Literal["NORM", "INV"] = "NORM"
    sync_delay_s: float = 0.0


@dataclass(frozen=True, slots=True)
class RigolModulationConfig:
    channel: int
    enabled: bool
    modulation_type: Literal["AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"]
    source: Literal["INT", "EXT"] = "INT"
    rate_hz: float = 100.0
    parameter: float = 1.0
    internal_shape: Literal["SIN", "SQU", "RAMP", "NOIS", "ARB"] = "SIN"
    polarity: Literal["POS", "NEG"] = "POS"


@dataclass(frozen=True, slots=True)
class RigolFrequencySweepConfig:
    channel: int
    enabled: bool
    start_hz: float
    stop_hz: float
    duration_s: float
    spacing: Literal["LIN", "LOG", "STEP"] = "LIN"
    steps: int = 2
    start_hold_s: float = 0.0
    stop_hold_s: float = 0.0
    return_time_s: float = 0.0
    trigger_source: Literal["INT", "EXT", "MAN"] = "INT"
    trigger_slope: Literal["POS", "NEG"] = "POS"
    trigger_output: bool = False


@dataclass(frozen=True, slots=True)
class RigolBurstConfig:
    channel: int
    enabled: bool
    mode: Literal["TRIG", "GAT"] = "TRIG"
    cycles: int = 1
    phase_deg: float = 0.0
    period_s: float = 1.0
    delay_s: float = 0.0
    trigger_source: Literal["INT", "EXT", "MAN"] = "INT"
    trigger_slope: Literal["POS", "NEG"] = "POS"
    trigger_output: bool = False
    gate_polarity: Literal["POS", "NEG"] = "POS"
    idle: Literal["FPT", "TOP", "CENT", "BOT"] = "FPT"


class RigolAdapter(DeviceAdapter):
    """Only high-level, validated operations are available to callers."""

    def __init__(
        self,
        station: StationSettings,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        super().__init__()
        self._station = station
        self._settings: RigolSettings = station.rigol
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None
        self._last_config: dict[int, RigolChannelConfig] = {}
        self._last_output_config: dict[int, RigolOutputConfig] = {}
        self._modulation_enabled: set[int] = set()
        self._armed_until: dict[int, float] = {}

    def _interlock(self) -> OutputInterlock:
        return OutputInterlock(
            profile_approved=self._station.profile.state == "approved",
            profile_locks_outputs=True,
        )

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Rigol nie jest połączony.")
        return self._session

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            return self._identity_or_raise()
        if not self._settings.enabled:
            raise SafetyViolation("Rigol jest wyłączony w profilu stanowiska.")
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("Brak zasobu VISA dla Rigola w settings.yml.")
        timeout = int(parse_quantity(self._settings.connection.timeout, DIMENSION_TIME).si_value * 1000)
        session = self._factory.open(resource, self._settings.connection.visa_backend, timeout)
        try:
            if self._settings.connection.read_termination is not None:
                session.read_termination = self._settings.connection.read_termination
            if self._settings.connection.write_termination is not None:
                session.write_termination = self._settings.connection.write_termination
            identity = parse_identity(resource, session.query("*IDN?"))
            validate_identity(
                identity,
                vendor_contains=self._settings.identity.expected_vendor_contains,
                expected_models=self._settings.identity.expected_models,
                expected_serial=self._settings.identity.expected_serial,
                require_serial_match=self._settings.identity.require_serial_match,
            )
            session.write("*CLS")
            self._session = session
            self._identity = identity
            self._state = DeviceState.VERIFIED
            if self._settings.safety.outputs_off_on_connect:
                self._write_all_outputs_off()
            self._capabilities = self._probe_capabilities(identity)
            return identity
        except Exception:
            try:
                session.close()
            finally:
                self._session = None
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED
            raise

    def _probe_capabilities(self, identity: DeviceIdentity) -> DeviceCapabilities:
        """Probe optional DG1000Z controls with read-only queries at connect.

        Feature visibility is conservative.  An optional control is exposed
        only if this firmware answers its query; a timeout/error merely hides
        that control and never prevents basic waveform use.
        """

        features = {"basic_waveform"}
        unsupported: set[str] = set()
        if bool(self._settings.capabilities.get("probe_optional_commands", True)):
            session = self._require_session()
            for feature, query in (
                ("modulation", ":SOUR1:MOD?"),
                ("frequency_sweep", ":SOUR1:SWE:STAT?"),
                ("burst", ":SOUR1:BURS:STAT?"),
                ("phase_sync", ":SOUR1:PHAS?"),
            ):
                try:
                    session.query(query)
                except Exception:
                    unsupported.add(feature)
                else:
                    features.add(feature)
            # Clear query errors so a rejected optional probe cannot leak into
            # the first production transaction.
            session.write("*CLS")
        return DeviceCapabilities(
            device_name="rigol",
            model=identity.model or "DG1032Z",
            firmware=identity.firmware,
            features=frozenset(features),
            unsupported_commands=frozenset(unsupported),
        )

    def _assert_feature(self, feature: str) -> None:
        if not bool(self._settings.capabilities.get("fail_on_unknown_firmware_command", True)):
            return
        if self._capabilities is None or not self._capabilities.supports(feature):
            raise DeviceError(
                f"Rigol firmware nie potwierdził funkcji {feature!r}; kontrolka jest zablokowana do kwalifikacji."
            )

    def _identity_or_raise(self) -> DeviceIdentity:
        if self._identity is None:
            raise ConnectionError("Rigol ma sesję bez zweryfikowanej tożsamości.")
        return self._identity

    def disconnect(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            if self._settings.safety.outputs_off_on_disconnect:
                try:
                    session.write(":OUTP1 OFF")
                    session.write(":OUTP2 OFF")
                except Exception:
                    pass
            try:
                session.close()
            finally:
                self._identity = None
                self._capabilities = None
                self._last_config.clear()
                self._last_output_config.clear()
                self._modulation_enabled.clear()
                self._armed_until.clear()
                self._state = DeviceState.DISCONNECTED

    def _write_all_outputs_off(self) -> None:
        session = self._require_session()
        session.write(":OUTP1 OFF")
        session.write(":OUTP2 OFF")

    def emergency_off(self) -> None:
        if self._session is None:
            return
        try:
            self._write_all_outputs_off()
        except Exception:
            self._state = DeviceState.UNKNOWN
        else:
            self._armed_until.clear()
            self._state = DeviceState.OUTPUT_OFF

    def _channel_settings(self, channel: int):
        try:
            return self._settings.safety.channels[str(channel)]
        except KeyError as exc:
            raise SafetyViolation(f"Kanał Rigola CH{channel} nie jest skonfigurowany.") from exc

    def _check_errors(self) -> None:
        session = self._require_session()
        errors: list[str] = []
        for _ in range(20):
            response = session.query(":SYST:ERR?").strip()
            if response.startswith("0,"):
                break
            errors.append(response)
        if errors:
            raise DeviceError("Rigol zgłosił błąd: " + "; ".join(errors))

    def configure_channel(self, config: RigolChannelConfig) -> RigolCurrentEstimate:
        """Safely configure a channel while its output is forced OFF."""

        if config.channel not in (1, 2):
            raise SafetyViolation("Numer kanału Rigola musi wynosić 1 lub 2.")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Kanał Rigola CH{config.channel} jest wyłączony w profilu.")
        self._assert_finite("phase", config.phase_deg)
        estimate = validate_rigol_waveform(
            channel=channel,
            safety=self._settings.safety,
            waveform=config.waveform,
            frequency=config.frequency_hz,
            high_level=config.high_level_v,
            low_level=config.low_level_v,
            output_load=config.output_load,
            dut_min_impedance=config.dut_min_impedance_ohm,
        )
        session = self._require_session()
        prefix = f":SOUR{config.channel}"
        waveform = config.waveform.upper()
        session.write(f":OUTP{config.channel} OFF")
        session.write(f":OUTP{config.channel}:LOAD {self._format_load(config.output_load)}")
        if waveform == "DC":
            session.write(f"{prefix}:APPL:DC DEF,DEF,{config.high_level_v:.12g}")
        else:
            session.write(f"{prefix}:FUNC {waveform}")
            if waveform != "NOIS":
                session.write(f"{prefix}:FREQ {config.frequency_hz:.12g}")
            current_low = float(session.query(f"{prefix}:VOLT:LOW?"))
            levels = [
                f"{prefix}:VOLT:HIGH {config.high_level_v:.12g}",
                f"{prefix}:VOLT:LOW {config.low_level_v:.12g}",
            ]
            if config.high_level_v <= current_low:
                levels.reverse()
            for command in levels:
                session.write(command)
            if waveform != "NOIS":
                session.write(f"{prefix}:PHAS {config.phase_deg:.12g}")
            self._write_shape_parameters(prefix, waveform, config)
        self._check_errors()
        self._verify_applied_configuration(config)
        self._last_config[config.channel] = config
        self._state = DeviceState.OUTPUT_OFF
        return estimate

    def _verify_applied_configuration(self, expected: RigolChannelConfig) -> None:
        """Reject silent quantisation or clamping after every write transaction.

        The DG1032Z may enforce a minimum Vpp and therefore alter LowL/HighL.
        A mismatch is reported while output remains OFF; callers must use a
        physically representable requested configuration instead of silently
        continuing with a different waveform.
        """

        session = self._require_session()
        prefix = f":SOUR{expected.channel}"
        try:
            actual_waveform = session.query(f"{prefix}:FUNC?").strip().upper()
            actual_frequency = (
                float(session.query(f"{prefix}:FREQ?"))
                if expected.waveform.upper() not in {"DC", "NOIS"}
                else expected.frequency_hz
            )
            actual_high = float(session.query(f"{prefix}:VOLT:HIGH?"))
            actual_low = float(session.query(f"{prefix}:VOLT:LOW?"))
            actual_output = session.query(f":OUTP{expected.channel}?").strip().upper()
        except (TypeError, ValueError) as exc:
            raise DeviceError("Rigol zwrócił nieprawidłową odpowiedź weryfikacji konfiguracji.") from exc
        mismatches: list[str] = []
        if actual_waveform != expected.waveform.upper():
            mismatches.append(f"FUNC {actual_waveform} ≠ {expected.waveform.upper()}")
        if expected.waveform.upper() not in {"DC", "NOIS"} and not self._same_number(actual_frequency, expected.frequency_hz, absolute=1e-3):
            mismatches.append(f"FREQ {actual_frequency:.9g} ≠ {expected.frequency_hz:.9g} Hz")
        if not self._same_number(actual_high, expected.high_level_v, absolute=1e-6):
            mismatches.append(f"HighL {actual_high:.9g} ≠ {expected.high_level_v:.9g} V")
        if not self._same_number(actual_low, expected.low_level_v, absolute=1e-6):
            mismatches.append(f"LowL {actual_low:.9g} ≠ {expected.low_level_v:.9g} V")
        if actual_output in {"1", "ON"}:
            mismatches.append("wyjście zostało włączone mimo transakcji konfiguracji")
        if mismatches:
            raise DeviceError(
                "Rigol zastosował inną konfigurację (wyjście pozostaje OFF): " + "; ".join(mismatches)
            )

    @staticmethod
    def _same_number(actual: float, expected: float, *, absolute: float) -> bool:
        return abs(actual - expected) <= max(absolute, abs(expected) * 1e-8)

    @staticmethod
    def _assert_finite(name: str, value: float) -> None:
        if not math.isfinite(value):
            raise SafetyViolation(f"{name} Rigola musi być skończoną liczbą.")

    @staticmethod
    def _format_load(value: str | float) -> str:
        if isinstance(value, str) and value.strip().upper() in {"HIGHZ", "INF", "INFINITY"}:
            return "INF"
        parsed = float(value)
        if not 1 <= parsed <= 10_000:
            raise SafetyViolation("Obciążenie Rigola musi mieścić się w zakresie 1 Ω–10 kΩ albo HIGHZ.")
        return f"{parsed:.12g}"

    def _write_shape_parameters(self, prefix: str, waveform: str, config: RigolChannelConfig) -> None:
        session = self._require_session()
        if waveform == "SQU" and config.square_duty_percent is not None:
            self._assert_finite("duty cycle", config.square_duty_percent)
            if not 0 < config.square_duty_percent < 100:
                raise SafetyViolation("Duty cycle musi być w zakresie (0, 100) %.")
            session.write(f"{prefix}:FUNC:SQU:DCYC {config.square_duty_percent:.12g}")
        elif waveform == "RAMP" and config.ramp_symmetry_percent is not None:
            self._assert_finite("symetria rampy", config.ramp_symmetry_percent)
            if not 0 <= config.ramp_symmetry_percent <= 100:
                raise SafetyViolation("Symetria rampy musi być w zakresie 0–100 %.")
            session.write(f"{prefix}:FUNC:RAMP:SYMM {config.ramp_symmetry_percent:.12g}")
        elif waveform == "PULS":
            for suffix, value in (
                ("WIDT", config.pulse_width_s),
                ("TRAN:LEAD", config.pulse_leading_s),
                ("TRAN:TRA", config.pulse_trailing_s),
            ):
                if value is not None:
                    self._assert_finite("parametr impulsu", value)
                    if value <= 0:
                        raise SafetyViolation("Szerokość i zbocza impulsu muszą być dodatnie.")
                    session.write(f"{prefix}:FUNC:PULS:{suffix} {value:.12g}")

    def set_output(self, channel: int, enabled: bool) -> bool:
        channel_settings = self._channel_settings(channel)
        if enabled:
            self._assert_armed(channel, channel_settings.enabled)
        session = self._require_session()
        session.write(f":OUTP{channel} {'ON' if enabled else 'OFF'}")
        self._check_errors()
        active = session.query(f":OUTP{channel}?").strip().upper() in {"1", "ON"}
        if active != enabled:
            raise DeviceError("Rigol nie potwierdził żądanego stanu wyjścia.")
        self._state = DeviceState.OUTPUT_ON if active else DeviceState.OUTPUT_OFF
        if not enabled:
            self._armed_until.pop(channel, None)
        return active

    def configure_output(self, config: RigolOutputConfig) -> None:
        """Configure the output path while proving that the carrier is OFF."""

        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Kanał Rigola CH{config.channel} jest wyłączony w profilu.")
        last = self._last_config.get(config.channel)
        if last is None:
            raise SafetyViolation("Najpierw skonfiguruj przebieg i zwaliduj model prądu Rigola.")
        expected_load = self._format_load(config.output_load)
        if self._format_load(last.output_load) != expected_load:
            raise SafetyViolation(
                "Zmiana output load wymaga ponownej konfiguracji przebiegu, aby ponownie obliczyć prąd DUT."
            )
        if config.polarity not in {"NORM", "INV"} or config.mode not in {"NORM", "GAT"}:
            raise SafetyViolation("Polaryzacja i tryb wyjścia Rigola są nieprawidłowe.")
        if config.gate_polarity not in {"NORM", "INV"} or config.sync_polarity not in {"NORM", "INV"}:
            raise SafetyViolation("Polaryzacja gate/sync Rigola jest nieprawidłowa.")
        self._assert_finite("opóźnienie SYNC", config.sync_delay_s)
        if config.sync_delay_s < 0 or config.sync_delay_s > 10:
            raise SafetyViolation("Opóźnienie SYNC Rigola musi mieścić się w zakresie 0–10 s.")
        session = self._require_session()
        prefix = f":OUTP{config.channel}"
        session.write(f"{prefix} OFF")
        for command in (
            f"{prefix}:LOAD {expected_load}",
            f"{prefix}:POL {config.polarity}",
            f"{prefix}:MODE {config.mode}",
            f"{prefix}:GAT:POL {config.gate_polarity}",
            f"{prefix}:SYNC {'ON' if config.sync_enabled else 'OFF'}",
            f"{prefix}:SYNC:POL {config.sync_polarity}",
            f"{prefix}:SYNC:DEL {config.sync_delay_s:.12g}",
        ):
            session.write(command)
        self._check_errors()
        self._verify_output_configuration(config)
        self._last_output_config[config.channel] = config
        self._state = DeviceState.OUTPUT_OFF

    def arm_output(self, channel: int, *, ttl_s: float = 30.0) -> float:
        """Validate and arm one channel for a short, one-shot output-enable window."""

        channel_settings = self._channel_settings(channel)
        self._interlock().assert_can_enable(
            device_name=f"Rigol CH{channel}",
            device_allows_output=self._settings.safety.allow_output_enable and channel_settings.enabled,
        )
        if channel not in self._last_config:
            raise SafetyViolation("Najpierw skonfiguruj i zwaliduj kanał Rigola.")
        if channel in self._modulation_enabled:
            raise SafetyViolation(
                "Nie można uzbroić Rigola z aktywną modulacją: model prądu DUT nie kwalifikuje jeszcze "
                "obwiedni modulacji. Wyłącz modulację albo wykonaj kwalifikację rozszerzoną."
            )
        if ttl_s <= 0 or ttl_s > 120:
            raise SafetyViolation("Czas ARM Rigola musi być w zakresie (0, 120] s.")
        expires = time.monotonic() + ttl_s
        self._armed_until[channel] = expires
        return expires

    def _assert_armed(self, channel: int, channel_enabled: bool) -> None:
        self._interlock().assert_can_enable(
            device_name=f"Rigol CH{channel}",
            device_allows_output=self._settings.safety.allow_output_enable and channel_enabled,
        )
        expiry = self._armed_until.pop(channel, None)
        if expiry is None:
            raise SafetyViolation("Najpierw uzbrój kanał Rigola przez ARM.")
        if time.monotonic() > expiry:
            raise SafetyViolation("Okno ARM Rigola wygasło; uzbrój kanał ponownie.")

    def configure_modulation(self, config: RigolModulationConfig) -> None:
        """Configure modulation with the carrier output forced OFF."""

        self._assert_feature("modulation")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Kanał Rigola CH{config.channel} jest wyłączony w profilu.")
        if config.modulation_type not in {"AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"}:
            raise SafetyViolation("Typ modulacji Rigola nie jest dozwolony.")
        if config.source not in {"INT", "EXT"}:
            raise SafetyViolation("Źródło modulacji Rigola musi wynosić INT albo EXT.")
        if config.internal_shape not in {"SIN", "SQU", "RAMP", "NOIS", "ARB"}:
            raise SafetyViolation("Kształt wewnętrzny modulacji Rigola nie jest dozwolony.")
        if config.polarity not in {"POS", "NEG"}:
            raise SafetyViolation("Polaryzacja modulacji Rigola musi wynosić POS albo NEG.")
        self._assert_finite("rate modulacji", config.rate_hz)
        self._assert_finite("parametr modulacji", config.parameter)
        if config.rate_hz <= 0:
            raise SafetyViolation("Częstotliwość/rate modulacji musi być dodatnia.")
        rate_limits = channel.lab_limits.modulation_rate
        minimum = parse_quantity(rate_limits.min, "frequency").si_value
        maximum = parse_quantity(rate_limits.max, "frequency").si_value
        if not minimum <= config.rate_hz <= maximum:
            raise SafetyViolation("Rate modulacji jest poza zatwierdzonym zakresem częstotliwości Rigola.")
        if config.parameter < 0:
            raise SafetyViolation("Parametr modulacji nie może być ujemny.")
        session = self._require_session()
        source = f":SOUR{config.channel}"
        kind = config.modulation_type
        session.write(f":OUTP{config.channel} OFF")
        session.write(f"{source}:MOD OFF")
        session.write(f"{source}:MOD:TYPE {kind}")
        session.write(f"{source}:{kind}:SOUR {config.source}")
        if kind in {"AM", "FM", "PM", "PWM"}:
            session.write(f"{source}:{kind}:INT:FREQ {config.rate_hz:.12g}")
            session.write(f"{source}:{kind}:INT:FUNC {config.internal_shape}")
        else:
            session.write(f"{source}:{kind}:INT:RATE {config.rate_hz:.12g}")
            session.write(f"{source}:{kind}:POL {config.polarity}")
        suffix = {
            "AM": "DEPT",
            "FM": "DEV",
            "PM": "DEV",
            "ASK": "AMPL",
            "FSK": "FREQ",
            "PSK": "PHAS",
            "PWM": "DEV:DCYC",
        }[kind]
        session.write(f"{source}:{kind}:{suffix} {config.parameter:.12g}")
        session.write(f"{source}:MOD {'ON' if config.enabled else 'OFF'}")
        self._check_errors()
        self._verify_output_off(config.channel)
        if config.enabled:
            self._modulation_enabled.add(config.channel)
        else:
            self._modulation_enabled.discard(config.channel)

    def configure_frequency_sweep(self, config: RigolFrequencySweepConfig) -> None:
        """Configure the generator's internal frequency sweep at OUTPUT OFF."""

        self._assert_feature("frequency_sweep")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Kanał Rigola CH{config.channel} jest wyłączony w profilu.")
        for name, value in (
            ("start sweep", config.start_hz),
            ("stop sweep", config.stop_hz),
            ("czas sweep", config.duration_s),
            ("start hold sweep", config.start_hold_s),
            ("stop hold sweep", config.stop_hold_s),
            ("return time sweep", config.return_time_s),
        ):
            self._assert_finite(name, value)
        if config.spacing not in {"LIN", "LOG", "STEP"}:
            raise SafetyViolation("Odstępy sweep Rigola muszą wynosić LIN, LOG albo STEP.")
        if config.trigger_source not in {"INT", "EXT", "MAN"} or config.trigger_slope not in {"POS", "NEG"}:
            raise SafetyViolation("Trigger sweep Rigola zawiera niedozwoloną wartość.")
        if isinstance(config.steps, bool) or not isinstance(config.steps, int):
            raise SafetyViolation("Liczba kroków sweep Rigola musi być liczbą całkowitą.")
        if config.start_hz <= 0 or config.stop_hz <= 0 or config.start_hz == config.stop_hz:
            raise SafetyViolation("Sweep wymaga dodatniego i różnego start/stop.")
        if config.duration_s <= 0 or min(config.start_hold_s, config.stop_hold_s, config.return_time_s) < 0:
            raise SafetyViolation("Czasy sweep muszą być dodatnie lub równe zero (hold/return).")
        advanced = channel.lab_limits
        duration_min = parse_quantity(advanced.sweep_duration.min, "time").si_value
        duration_max = parse_quantity(advanced.sweep_duration.max, "time").si_value
        if not duration_min <= config.duration_s <= duration_max:
            raise SafetyViolation("Czas sweep jest poza zatwierdzonym zakresem.")
        if not advanced.sweep_steps.min <= config.steps <= advanced.sweep_steps.max:
            raise SafetyViolation("Liczba kroków sweep jest poza zatwierdzonym zakresem.")
        limits = channel.lab_limits.frequency
        for value, name in ((config.start_hz, "start"), (config.stop_hz, "stop")):
            self._enforce_frequency(value, limits.min, limits.max, f"Sweep {name}")
        session = self._require_session()
        source = f":SOUR{config.channel}"
        session.write(f":OUTP{config.channel} OFF")
        session.write(f"{source}:SWE:STAT OFF")
        for command in (
            f"{source}:FREQ:STAR {config.start_hz:.12g}",
            f"{source}:FREQ:STOP {config.stop_hz:.12g}",
            f"{source}:SWE:TIME {config.duration_s:.12g}",
            f"{source}:SWE:SPAC {config.spacing}",
            f"{source}:SWE:STEP {config.steps}",
            f"{source}:SWE:HTIM:STAR {config.start_hold_s:.12g}",
            f"{source}:SWE:HTIM:STOP {config.stop_hold_s:.12g}",
            f"{source}:SWE:RTIM {config.return_time_s:.12g}",
            f"{source}:SWE:TRIG:SOUR {config.trigger_source}",
            f"{source}:SWE:TRIG:SLOP {config.trigger_slope}",
            f"{source}:SWE:TRIG:TRIGO {'ON' if config.trigger_output else 'OFF'}",
            f"{source}:SWE:STAT {'ON' if config.enabled else 'OFF'}",
        ):
            session.write(command)
        self._check_errors()
        self._verify_output_off(config.channel)

    def trigger_frequency_sweep(self, channel: int) -> None:
        self._assert_feature("frequency_sweep")
        self._channel_settings(channel)
        self._require_session().write(f":SOUR{channel}:SWE:TRIG")
        self._check_errors()

    def configure_burst(self, config: RigolBurstConfig) -> None:
        """Configure burst/gate parameters while the carrier output is OFF."""

        self._assert_feature("burst")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Kanał Rigola CH{config.channel} jest wyłączony w profilu.")
        for name, value in (
            ("faza burst", config.phase_deg),
            ("okres burst", config.period_s),
            ("delay burst", config.delay_s),
        ):
            self._assert_finite(name, value)
        if config.mode not in {"TRIG", "GAT"}:
            raise SafetyViolation("Tryb burst Rigola musi wynosić TRIG albo GAT.")
        if config.trigger_source not in {"INT", "EXT", "MAN"} or config.trigger_slope not in {"POS", "NEG"}:
            raise SafetyViolation("Trigger burst Rigola zawiera niedozwoloną wartość.")
        if config.gate_polarity not in {"POS", "NEG"} or config.idle not in {"FPT", "TOP", "CENT", "BOT"}:
            raise SafetyViolation("Parametry gate/idle burst Rigola są nieprawidłowe.")
        if isinstance(config.cycles, bool) or not isinstance(config.cycles, int):
            raise SafetyViolation("Liczba cykli burst Rigola musi być liczbą całkowitą.")
        if config.cycles < 1 or config.period_s <= 0 or config.delay_s < 0:
            raise SafetyViolation("Burst wymaga cycles >= 1, period > 0 i delay >= 0.")
        limits = channel.lab_limits
        period_min = parse_quantity(limits.burst_period.min, "time").si_value
        period_max = parse_quantity(limits.burst_period.max, "time").si_value
        if not period_min <= config.period_s <= period_max:
            raise SafetyViolation("Okres burst jest poza zatwierdzonym zakresem.")
        if not limits.burst_cycles.min <= config.cycles <= limits.burst_cycles.max:
            raise SafetyViolation("Liczba cykli burst jest poza zatwierdzonym zakresem.")
        session = self._require_session()
        source = f":SOUR{config.channel}"
        session.write(f":OUTP{config.channel} OFF")
        session.write(f"{source}:BURS OFF")
        for command in (
            f"{source}:BURS:MODE {config.mode}",
            f"{source}:BURS:NCYC {config.cycles}",
            f"{source}:BURS:PHAS {config.phase_deg:.12g}",
            f"{source}:BURS:INT:PER {config.period_s:.12g}",
            f"{source}:BURS:TDEL {config.delay_s:.12g}",
            f"{source}:BURS:TRIG:SOUR {config.trigger_source}",
            f"{source}:BURS:TRIG:SLOP {config.trigger_slope}",
            f"{source}:BURS:TRIG:TRIGO {'ON' if config.trigger_output else 'OFF'}",
            f"{source}:BURS:GATE:POL {config.gate_polarity}",
            f"{source}:BURS:IDLE {config.idle}",
            f"{source}:BURS {'ON' if config.enabled else 'OFF'}",
        ):
            session.write(command)
        self._check_errors()
        self._verify_output_off(config.channel)

    def trigger_burst(self, channel: int) -> None:
        self._assert_feature("burst")
        self._channel_settings(channel)
        self._require_session().write(f":SOUR{channel}:BURS:TRIG")
        self._check_errors()

    def synchronize_phases(self) -> None:
        self._assert_feature("phase_sync")
        self._require_session().write(":SOUR1:PHAS:SYNC")
        self._check_errors()

    def _verify_output_off(self, channel: int) -> None:
        state = self._require_session().query(f":OUTP{channel}?").strip().upper()
        if state in {"1", "ON"}:
            raise DeviceError("Rigol włączył wyjście podczas konfiguracji zaawansowanej.")
        self._state = DeviceState.OUTPUT_OFF

    def _verify_output_configuration(self, expected: RigolOutputConfig) -> None:
        session = self._require_session()
        prefix = f":OUTP{expected.channel}"
        state = session.query(f"{prefix}?").strip().upper()
        load = session.query(f"{prefix}:LOAD?").strip().upper()
        polarity = session.query(f"{prefix}:POL?").strip().upper()
        mode = session.query(f"{prefix}:MODE?").strip().upper()
        gate_polarity = session.query(f"{prefix}:GAT:POL?").strip().upper()
        sync_enabled = session.query(f"{prefix}:SYNC?").strip().upper()
        sync_polarity = session.query(f"{prefix}:SYNC:POL?").strip().upper()
        sync_delay = float(session.query(f"{prefix}:SYNC:DEL?"))
        mismatches: list[str] = []
        if state in {"1", "ON"}:
            mismatches.append("OUTPUT jest ON")
        if not self._load_response_matches(load, expected.output_load):
            mismatches.append(f"LOAD {load}")
        if polarity != expected.polarity:
            mismatches.append(f"POL {polarity}")
        if mode != expected.mode:
            mismatches.append(f"MODE {mode}")
        if gate_polarity != expected.gate_polarity:
            mismatches.append(f"GAT:POL {gate_polarity}")
        if (sync_enabled in {"1", "ON"}) != expected.sync_enabled:
            mismatches.append(f"SYNC {sync_enabled}")
        if sync_polarity != expected.sync_polarity:
            mismatches.append(f"SYNC:POL {sync_polarity}")
        if not self._same_number(sync_delay, expected.sync_delay_s, absolute=1e-9):
            mismatches.append(f"SYNC:DEL {sync_delay:.9g}")
        if mismatches:
            raise DeviceError("Rigol nie potwierdził konfiguracji output (pozostaje OFF): " + "; ".join(mismatches))

    def _load_response_matches(self, response: str, expected: str | float) -> bool:
        target = self._format_load(expected)
        if target == "INF":
            if response in {"INF", "INFINITY", "HIGHZ"}:
                return True
            try:
                return float(response) >= 1e30
            except ValueError:
                return False
        try:
            return self._same_number(float(response), float(target), absolute=1e-6)
        except ValueError:
            return False

    @staticmethod
    def _enforce_frequency(value: float, lower_text: str, upper_text: str, name: str) -> None:
        lower = parse_quantity(lower_text, "frequency").si_value
        upper = parse_quantity(upper_text, "frequency").si_value
        tolerance = max(abs(lower), abs(upper), 1.0) * 1e-12
        if value < lower - tolerance or value > upper + tolerance:
            raise SafetyViolation(f"{name}={value:.9g} Hz poza zatwierdzonym zakresem {lower:.9g}–{upper:.9g} Hz.")
