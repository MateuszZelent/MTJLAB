"""Safe, explicit adapter for the Rigol DG1032Z signal generator."""

from __future__ import annotations

from dataclasses import dataclass

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
from app.domain.models import DeviceIdentity, DeviceState
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
            return identity
        except Exception:
            try:
                session.close()
            finally:
                self._session = None
                self._identity = None
                self._state = DeviceState.DISCONNECTED
            raise

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
        finally:
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
        actual_waveform = session.query(f"{prefix}:FUNC?").strip().upper()
        actual_frequency = float(session.query(f"{prefix}:FREQ?")) if expected.waveform.upper() != "NOIS" else expected.frequency_hz
        actual_high = float(session.query(f"{prefix}:VOLT:HIGH?"))
        actual_low = float(session.query(f"{prefix}:VOLT:LOW?"))
        actual_output = session.query(f":OUTP{expected.channel}?").strip().upper()
        mismatches: list[str] = []
        if actual_waveform != expected.waveform.upper():
            mismatches.append(f"FUNC {actual_waveform} ≠ {expected.waveform.upper()}")
        if expected.waveform.upper() != "NOIS" and not self._same_number(actual_frequency, expected.frequency_hz, absolute=1e-3):
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
            if not 0 < config.square_duty_percent < 100:
                raise SafetyViolation("Duty cycle musi być w zakresie (0, 100) %.")
            session.write(f"{prefix}:FUNC:SQU:DCYC {config.square_duty_percent:.12g}")
        elif waveform == "RAMP" and config.ramp_symmetry_percent is not None:
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
                    if value <= 0:
                        raise SafetyViolation("Szerokość i zbocza impulsu muszą być dodatnie.")
                    session.write(f"{prefix}:FUNC:PULS:{suffix} {value:.12g}")

    def set_output(self, channel: int, enabled: bool) -> bool:
        channel_settings = self._channel_settings(channel)
        if enabled:
            self._interlock().assert_can_enable(
                device_name=f"Rigol CH{channel}",
                device_allows_output=self._settings.safety.allow_output_enable and channel_settings.enabled,
            )
            if channel not in self._last_config:
                raise SafetyViolation("Najpierw skonfiguruj i zwaliduj kanał Rigola.")
        session = self._require_session()
        session.write(f":OUTP{channel} {'ON' if enabled else 'OFF'}")
        self._check_errors()
        active = session.query(f":OUTP{channel}?").strip().upper() in {"1", "ON"}
        if active != enabled:
            raise DeviceError("Rigol nie potwierdził żądanego stanu wyjścia.")
        self._state = DeviceState.OUTPUT_ON if active else DeviceState.OUTPUT_OFF
        return active
