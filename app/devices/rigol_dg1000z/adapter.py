"""Safe, explicit adapter for the Rigol DG1032Z signal generator."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Literal, NoReturn

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
from app.safety.rigol_current import (
    RigolCurrentEstimate,
    RigolSafetyEnvelope,
    validate_rigol_waveform,
)
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
    dut_envelope: RigolSafetyEnvelope | None = None


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


@dataclass(frozen=True, slots=True)
class RigolCounterConfig:
    state: Literal["ON", "OFF", "RUN", "STOP", "SINGLE"] = "ON"
    coupling: Literal["AC", "DC"] = "AC"
    gate_time: Literal["AUTO", "USER1", "USER2", "USER3", "USER4", "USER5", "USER6"] = "AUTO"
    high_frequency_rejection: bool = False
    trigger_level_v: float = 0.0
    sensitivity_percent: float = 25.0


@dataclass(frozen=True, slots=True)
class RigolCounterReading:
    frequency_hz: float
    period_s: float
    duty_percent: float
    positive_width_s: float
    negative_width_s: float


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
        self._last_modulation_config: dict[int, RigolModulationConfig] = {}
        self._sweep_enabled: set[int] = set()
        self._last_sweep_config: dict[int, RigolFrequencySweepConfig] = {}
        self._burst_enabled: set[int] = set()
        self._last_burst_config: dict[int, RigolBurstConfig] = {}
        self._output_states: dict[int, bool] = {1: False, 2: False}

    def _interlock(self) -> OutputInterlock:
        return OutputInterlock()

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Rigol is not connected.")
        return self._session

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            return self._identity_or_raise()
        if not self._settings.enabled:
            raise SafetyViolation("Rigol is disabled in the station profile.")
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("No Rigol VISA resource is configured in settings.yml.")
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
                states = self._read_output_states()
                if any(states.values()):
                    raise DeviceError(
                        "Rigol did not confirm both outputs OFF during connection."
                    )
            else:
                self._read_output_states()
            self._update_aggregate_output_state()
            self._capabilities = self._probe_capabilities(identity)
            return identity
        except Exception as exc:
            hardware_state_may_be_unknown = self._session is session
            close_error: Exception | None = None
            try:
                session.close()
            except Exception as close_exc:
                close_error = close_exc
            finally:
                self._session = None
                self._identity = None
                self._capabilities = None
                self._state = (
                    DeviceState.UNKNOWN
                    if hardware_state_may_be_unknown
                    else DeviceState.DISCONNECTED
                )
            if close_error is not None:
                raise ConnectionError(
                    f"Rigol connection failed ({exc}); VISA close also failed ({close_error})."
                ) from exc
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
                ("counter", ":COUN?"),
                ("harmonics", ":SOUR1:HARM?"),
                ("coupling", ":COUP?"),
                ("tracking", ":SOUR1:TRACK?"),
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
                f"Rigol firmware did not confirm feature {feature!r}; the control remains locked pending qualification."
            )

    def _identity_or_raise(self) -> DeviceIdentity:
        if self._identity is None:
            raise ConnectionError("Rigol has a session without a verified identity.")
        return self._identity

    def disconnect(self) -> None:
        session = self._session
        if session is None:
            self._state = DeviceState.DISCONNECTED
            return
        shutdown_error: Exception | None = None
        close_error: Exception | None = None
        if self._settings.safety.outputs_off_on_disconnect:
            try:
                self._write_all_outputs_off()
                states = self._read_output_states()
                if any(states.values()):
                    raise DeviceError(
                        "Rigol did not confirm both outputs OFF before disconnect."
                    )
            except Exception as exc:
                shutdown_error = exc
        try:
            session.close()
        except Exception as exc:
            close_error = exc
        finally:
            self._session = None
            self._identity = None
            self._capabilities = None
            self._last_config.clear()
            self._last_output_config.clear()
            self._modulation_enabled.clear()
            self._last_modulation_config.clear()
            self._sweep_enabled.clear()
            self._last_sweep_config.clear()
            self._burst_enabled.clear()
            self._last_burst_config.clear()
        if shutdown_error is not None or close_error is not None:
            self._state = DeviceState.UNKNOWN
            details = []
            if shutdown_error is not None:
                details.append(f"OUTPUT OFF was not confirmed: {shutdown_error}")
            if close_error is not None:
                details.append(f"VISA close failed: {close_error}")
            raise DeviceError("Rigol disconnect failed safely: " + "; ".join(details))
        self._output_states = {1: False, 2: False}
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
            states = self._read_output_states()
            if any(states.values()):
                raise DeviceError(
                    "Rigol did not confirm both outputs OFF after emergency shutdown."
                )
        except Exception:
            self._state = DeviceState.UNKNOWN
            raise
        else:
            self._output_states = {1: False, 2: False}
            self._state = DeviceState.OUTPUT_OFF

    def apply_limit_settings(self, station: object) -> None:
        if not isinstance(station, StationSettings):
            raise TypeError("Rigol limit update requires StationSettings.")
        self.assert_limit_only_update(self._settings, station.rigol)
        if self._session is not None:
            try:
                states = self._read_output_states()
            except Exception:
                self.emergency_off()
                raise
            if any(states.values()):
                raise SafetyViolation(
                    "Rigol limits can change without reconnecting only when both "
                    "outputs are confirmed OFF."
                )
        self._station = station
        self._settings = station.rigol
        self._last_config.clear()
        self._last_output_config.clear()
        self._modulation_enabled.clear()
        self._last_modulation_config.clear()
        self._sweep_enabled.clear()
        self._last_sweep_config.clear()
        self._burst_enabled.clear()
        self._last_burst_config.clear()

    def _read_output_states(self) -> dict[int, bool]:
        session = self._require_session()
        states = {
            channel: self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            for channel in (1, 2)
        }
        self._output_states.update(states)
        return states

    def _parse_output_state(self, response: str, *, channel: int) -> bool:
        return self._parse_on_off(
            response, field=f"CH{channel} OUTPUT state"
        )

    def _parse_on_off(self, response: str, *, field: str) -> bool:
        normalized = str(response).strip().upper()
        if normalized in {"1", "ON"}:
            return True
        if normalized in {"0", "OFF"}:
            return False
        self._state = DeviceState.UNKNOWN
        raise DeviceError(f"Rigol returned an invalid {field}: {response!r}.")

    def _update_aggregate_output_state(self) -> None:
        self._state = DeviceState.OUTPUT_ON if any(self._output_states.values()) else DeviceState.OUTPUT_OFF

    def _channel_settings(self, channel: int):
        try:
            return self._settings.safety.channels[str(channel)]
        except KeyError as exc:
            raise SafetyViolation(f"Rigol CH{channel} is not configured.") from exc

    def _check_errors(self) -> None:
        session = self._require_session()
        errors: list[str] = []
        for _ in range(20):
            response = session.query(":SYST:ERR?").strip()
            if response.startswith("0,"):
                break
            errors.append(response)
        if errors:
            raise DeviceError("Rigol reported an error: " + "; ".join(errors))

    def configure_channel(self, config: RigolChannelConfig) -> RigolCurrentEstimate:
        """Safely configure a channel while its output is forced OFF."""

        if config.channel not in (1, 2):
            raise SafetyViolation("Rigol channel number must be 1 or 2.")
        channel = self._channel_settings(config.channel)
        self._assert_independent_channels()
        if not channel.enabled:
            raise SafetyViolation(f"Rigol CH{config.channel} is disabled in the station profile.")
        self._assert_finite("phase", config.phase_deg)
        estimate = self._validate_waveform_config(config)
        session = self._require_session()
        prefix = f":SOUR{config.channel}"
        waveform = config.waveform.upper()
        session.write(f":OUTP{config.channel} OFF")
        self._verify_output_off(config.channel)
        # APPL/FUNC changes and advanced modes interact on the instrument.
        # Start every carrier transaction from one explicit, reproducible
        # state instead of relying on whatever the front panel last selected.
        session.write(f"{prefix}:MOD OFF")
        session.write(f"{prefix}:SWE:STAT OFF")
        session.write(f"{prefix}:BURS OFF")
        self._modulation_enabled.discard(config.channel)
        self._last_modulation_config.pop(config.channel, None)
        self._sweep_enabled.discard(config.channel)
        self._last_sweep_config.pop(config.channel, None)
        self._burst_enabled.discard(config.channel)
        self._last_burst_config.pop(config.channel, None)
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
        self._update_aggregate_output_state()
        return estimate

    def update_frequency(self, channel: int, frequency_hz: float) -> float:
        """Change only carrier frequency while preserving the current OUTPUT state."""

        self._assert_independent_channels()
        config = self._last_config.get(channel)
        if config is None:
            raise SafetyViolation(
                "Configure and validate the Rigol channel before updating frequency."
            )
        if config.waveform.upper() in {"DC", "NOIS"}:
            raise SafetyViolation(
                f"Rigol {config.waveform.upper()} does not support carrier-frequency updates."
            )
        updated = replace(config, frequency_hz=float(frequency_hz))
        self._validate_waveform_config(updated)
        session = self._require_session()
        prefix = f":SOUR{channel}"
        output_before = self._parse_output_state(
            session.query(f":OUTP{channel}?"), channel=channel
        )
        if output_before:
            self._validate_active_quick_update(channel, updated)
        try:
            session.write(f"{prefix}:FREQ {updated.frequency_hz:.12g}")
            self._check_errors()
            try:
                actual = float(session.query(f"{prefix}:FREQ?"))
            except (TypeError, ValueError) as exc:
                raise DeviceError("Rigol returned an invalid frequency readback.") from exc
            output_after = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            if output_after != output_before:
                raise DeviceError("Rigol OUTPUT state changed during a frequency update.")
            if not self._same_number(actual, updated.frequency_hz, absolute=1e-3):
                raise DeviceError(
                    f"Rigol frequency readback {actual:.9g} Hz does not match "
                    f"{updated.frequency_hz:.9g} Hz."
                )
        except Exception as exc:
            self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
        self._last_config[channel] = updated
        self._output_states[channel] = output_after
        self._update_aggregate_output_state()
        return actual

    def update_levels(
        self,
        channel: int,
        *,
        high_level_v: float,
        low_level_v: float,
    ) -> tuple[float, float]:
        """Update HighL/LowL atomically while preserving and verifying OUTPUT."""

        self._assert_independent_channels()
        config = self._last_config.get(channel)
        if config is None:
            raise SafetyViolation(
                "Configure and validate the Rigol channel before updating levels."
            )
        if config.waveform.upper() == "DC":
            raise SafetyViolation(
                "DC has one voltage level. Change Offset / DC level instead of HighL/LowL."
            )
        updated = replace(
            config,
            high_level_v=float(high_level_v),
            low_level_v=float(low_level_v),
        )
        self._validate_waveform_config(updated)
        session = self._require_session()
        prefix = f":SOUR{channel}"
        output_before = self._parse_output_state(
            session.query(f":OUTP{channel}?"), channel=channel
        )
        if output_before:
            self._validate_active_quick_update(channel, updated)
        try:
            current_low = float(session.query(f"{prefix}:VOLT:LOW?"))
            commands = [
                f"{prefix}:VOLT:HIGH {updated.high_level_v:.12g}",
                f"{prefix}:VOLT:LOW {updated.low_level_v:.12g}",
            ]
            if updated.high_level_v <= current_low:
                commands.reverse()
            for command in commands:
                session.write(command)
            self._check_errors()
            try:
                actual_high = float(session.query(f"{prefix}:VOLT:HIGH?"))
                actual_low = float(session.query(f"{prefix}:VOLT:LOW?"))
            except (TypeError, ValueError) as exc:
                raise DeviceError("Rigol returned invalid level readback.") from exc
            output_after = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            if output_after != output_before:
                raise DeviceError("Rigol OUTPUT state changed during a level update.")
            if not self._same_number(
                actual_high, updated.high_level_v, absolute=1e-6
            ) or not self._same_number(
                actual_low, updated.low_level_v, absolute=1e-6
            ):
                raise DeviceError(
                    "Rigol level readback does not match requested HighL/LowL."
                )
        except Exception as exc:
            self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
        self._last_config[channel] = updated
        self._output_states[channel] = output_after
        self._update_aggregate_output_state()
        return actual_high, actual_low

    def update_amplitude_vpp(self, channel: int, amplitude_vpp_v: float) -> float:
        """Update Vpp around the validated current offset without cycling OUTPUT."""

        config = self._last_config.get(channel)
        if config is None:
            raise SafetyViolation("Configure the Rigol channel before quick amplitude control.")
        if not math.isfinite(amplitude_vpp_v) or amplitude_vpp_v < 0:
            raise SafetyViolation("Rigol amplitude Vpp must be finite and non-negative.")
        offset_v = (config.high_level_v + config.low_level_v) / 2.0
        actual_high, actual_low = self.update_levels(
            channel,
            high_level_v=offset_v + amplitude_vpp_v / 2.0,
            low_level_v=offset_v - amplitude_vpp_v / 2.0,
        )
        return actual_high - actual_low

    def update_offset(self, channel: int, offset_v: float) -> float:
        """Update offset while preserving validated Vpp and OUTPUT state."""

        self._assert_independent_channels()
        config = self._last_config.get(channel)
        if config is None:
            raise SafetyViolation("Configure the Rigol channel before quick offset control.")
        if not math.isfinite(offset_v):
            raise SafetyViolation("Rigol offset must be finite.")
        if config.waveform.upper() == "DC":
            updated = replace(
                config,
                high_level_v=float(offset_v),
                low_level_v=float(offset_v),
            )
            self._validate_waveform_config(updated)
            session = self._require_session()
            prefix = f":SOUR{channel}"
            output_before = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            if output_before:
                self._validate_active_quick_update(channel, updated)
            # Do not re-apply the whole DC function while energised.  The
            # dedicated offset command changes only the active DC level.
            try:
                session.write(f"{prefix}:VOLT:OFFS {updated.high_level_v:.12g}")
                self._check_errors()
                try:
                    actual_offset = float(session.query(f"{prefix}:VOLT:OFFS?"))
                except (TypeError, ValueError) as exc:
                    raise DeviceError("Rigol returned an invalid DC-level readback.") from exc
                output_after = self._parse_output_state(
                    session.query(f":OUTP{channel}?"), channel=channel
                )
                if output_after != output_before:
                    raise DeviceError("Rigol OUTPUT state changed during a DC-level update.")
                if not self._same_number(actual_offset, offset_v, absolute=1e-6):
                    raise DeviceError(
                        "Rigol DC-level readback does not match the requested offset."
                    )
            except Exception as exc:
                self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
            self._last_config[channel] = updated
            self._output_states[channel] = output_after
            self._update_aggregate_output_state()
            return actual_offset
        amplitude_vpp_v = config.high_level_v - config.low_level_v
        actual_high, actual_low = self.update_levels(
            channel,
            high_level_v=offset_v + amplitude_vpp_v / 2.0,
            low_level_v=offset_v - amplitude_vpp_v / 2.0,
        )
        return (actual_high + actual_low) / 2.0

    def _fail_live_setpoint_update(
        self, *, output_was_on: bool, cause: Exception
    ) -> NoReturn:
        """Fail closed after a potentially partial update of an energised channel."""

        if output_was_on:
            try:
                self.emergency_off()
            except Exception as shutdown_exc:
                raise DeviceError(
                    "Rigol live setpoint update failed and emergency shutdown could not be confirmed: "
                    f"{shutdown_exc}"
                ) from cause
        raise cause

    def _validate_active_quick_update(
        self, channel: int, updated: RigolChannelConfig
    ) -> None:
        """Validate live edits against the mode actually active in hardware."""

        self._synchronize_advanced_states(channel)
        if channel in self._sweep_enabled:
            raise SafetyViolation(
                "Disable Rigol sweep before changing the live carrier setpoint."
            )
        if channel in self._burst_enabled:
            raise SafetyViolation(
                "Disable Rigol burst before changing the live carrier setpoint."
            )
        if channel in self._modulation_enabled:
            modulation = self._last_modulation_config.get(channel)
            if modulation is None:
                raise SafetyViolation(
                    "Rigol modulation is active but its envelope was not configured and validated by this session."
                )
            self._validate_modulation_envelope(updated, modulation)

    def last_channel_config(self, channel: int) -> RigolChannelConfig:
        """Return the last configuration confirmed by instrument readback."""

        try:
            return self._last_config[channel]
        except KeyError as exc:
            raise SafetyViolation(
                "Configure and validate the Rigol channel before changing its level."
            ) from exc

    def quick_control_snapshot(self) -> dict[str, float]:
        """Return the last configuration verified against instrument readback."""

        snapshot: dict[str, float] = {}
        for channel, config in self._last_config.items():
            snapshot[f"rigol.{channel}.frequency"] = config.frequency_hz
            snapshot[f"rigol.{channel}.high_level"] = config.high_level_v
            snapshot[f"rigol.{channel}.low_level"] = config.low_level_v
            snapshot[f"rigol.{channel}.amplitude"] = (
                config.high_level_v - config.low_level_v
            )
            snapshot[f"rigol.{channel}.offset"] = (
                config.high_level_v + config.low_level_v
            ) / 2.0
        return snapshot

    def _validate_waveform_config(self, config: RigolChannelConfig) -> RigolCurrentEstimate:
        channel = self._channel_settings(config.channel)
        return validate_rigol_waveform(
            channel=channel,
            safety=self._settings.safety,
            waveform=config.waveform,
            frequency=config.frequency_hz,
            high_level=config.high_level_v,
            low_level=config.low_level_v,
            output_load=config.output_load,
            dut_min_impedance=config.dut_min_impedance_ohm,
            dut_envelope=config.dut_envelope,
        )

    def _verify_applied_configuration(self, expected: RigolChannelConfig) -> None:
        """Reject silent quantisation or clamping after every write transaction.

        The DG1032Z may enforce a minimum Vpp and therefore alter LowL/HighL.
        A mismatch is reported while output remains OFF; callers must use a
        physically representable requested configuration instead of silently
        continuing with a different waveform.
        """

        session = self._require_session()
        prefix = f":SOUR{expected.channel}"
        is_dc = expected.waveform.upper() == "DC"
        try:
            actual_waveform = session.query(f"{prefix}:FUNC?").strip().upper()
            actual_frequency = (
                float(session.query(f"{prefix}:FREQ?"))
                if expected.waveform.upper() not in {"DC", "NOIS"}
                else expected.frequency_hz
            )
            actual_dc_level = (
                float(session.query(f"{prefix}:VOLT:OFFS?")) if is_dc else None
            )
            actual_high = (
                expected.high_level_v
                if is_dc
                else float(session.query(f"{prefix}:VOLT:HIGH?"))
            )
            actual_low = (
                expected.low_level_v
                if is_dc
                else float(session.query(f"{prefix}:VOLT:LOW?"))
            )
            actual_load = session.query(
                f":OUTP{expected.channel}:LOAD?"
            ).strip().upper()
            actual_phase = (
                float(session.query(f"{prefix}:PHAS?"))
                if expected.waveform.upper() not in {"DC", "NOIS"}
                else expected.phase_deg
            )
            actual_output = self._parse_output_state(
                session.query(f":OUTP{expected.channel}?"), channel=expected.channel
            )
        except (TypeError, ValueError) as exc:
            raise DeviceError("Rigol returned an invalid configuration readback.") from exc
        mismatches: list[str] = []
        if actual_waveform != expected.waveform.upper():
            mismatches.append(f"FUNC {actual_waveform} ≠ {expected.waveform.upper()}")
        if expected.waveform.upper() not in {"DC", "NOIS"} and not self._same_number(actual_frequency, expected.frequency_hz, absolute=1e-3):
            mismatches.append(f"FREQ {actual_frequency:.9g} ≠ {expected.frequency_hz:.9g} Hz")
        if is_dc and actual_dc_level is not None and not self._same_number(
            actual_dc_level, expected.high_level_v, absolute=1e-6
        ):
            mismatches.append(
                f"DC level {actual_dc_level:.9g} != {expected.high_level_v:.9g} V"
            )
        if not is_dc and not self._same_number(actual_high, expected.high_level_v, absolute=1e-6):
            mismatches.append(f"HighL {actual_high:.9g} ≠ {expected.high_level_v:.9g} V")
        if not is_dc and not self._same_number(actual_low, expected.low_level_v, absolute=1e-6):
            mismatches.append(f"LowL {actual_low:.9g} ≠ {expected.low_level_v:.9g} V")
        if not self._load_response_matches(actual_load, expected.output_load):
            mismatches.append(
                f"LOAD {actual_load} ≠ {self._format_load(expected.output_load)}"
            )
        if not self._same_number(actual_phase, expected.phase_deg, absolute=1e-6):
            mismatches.append(
                f"PHAS {actual_phase:.9g} ≠ {expected.phase_deg:.9g} deg"
            )
        shape_queries = {
            "SQU": ("DCYC", expected.square_duty_percent),
            "RAMP": ("SYMM", expected.ramp_symmetry_percent),
            "PULS": ("WIDT", expected.pulse_width_s),
        }
        shape_suffix, expected_shape = shape_queries.get(
            expected.waveform.upper(), ("", None)
        )
        if expected_shape is not None:
            try:
                actual_shape = float(
                    session.query(
                        f"{prefix}:FUNC:{expected.waveform.upper()}:{shape_suffix}?"
                    )
                )
            except (TypeError, ValueError) as exc:
                raise DeviceError(
                    "Rigol returned an invalid waveform-shape readback."
                ) from exc
            tolerance = 1e-9 if expected.waveform.upper() == "PULS" else 1e-6
            if not self._same_number(
                actual_shape, expected_shape, absolute=tolerance
            ):
                mismatches.append(
                    f"{shape_suffix} {actual_shape:.9g} ≠ {expected_shape:.9g}"
                )
        if expected.waveform.upper() == "PULS":
            for suffix, expected_value in (
                ("TRAN:LEAD", expected.pulse_leading_s),
                ("TRAN:TRA", expected.pulse_trailing_s),
            ):
                if expected_value is None:
                    continue
                try:
                    actual_value = float(
                        session.query(f"{prefix}:FUNC:PULS:{suffix}?")
                    )
                except (TypeError, ValueError) as exc:
                    raise DeviceError(
                        "Rigol returned an invalid pulse-edge readback."
                    ) from exc
                if not self._same_number(
                    actual_value, expected_value, absolute=1e-9
                ):
                    mismatches.append(
                        f"{suffix} {actual_value:.9g} ≠ {expected_value:.9g}"
                    )
        if actual_output:
            mismatches.append("output turned on during a configuration transaction")
        if mismatches:
            raise DeviceError(
                "Rigol applied a different configuration (output remains OFF): " + "; ".join(mismatches)
            )

    @staticmethod
    def _same_number(actual: float, expected: float, *, absolute: float) -> bool:
        return abs(actual - expected) <= max(absolute, abs(expected) * 1e-8)

    @staticmethod
    def _assert_finite(name: str, value: float) -> None:
        if not math.isfinite(value):
            raise SafetyViolation(f"Rigol {name} must be finite.")

    @staticmethod
    def _format_load(value: str | float) -> str:
        if isinstance(value, str) and value.strip().upper() in {"HIGHZ", "INF", "INFINITY"}:
            return "INF"
        parsed = float(value)
        if not 1 <= parsed <= 10_000:
            raise SafetyViolation("Rigol output load must be 1 ohm–10 kohm or HIGHZ.")
        return f"{parsed:.12g}"

    def _write_shape_parameters(self, prefix: str, waveform: str, config: RigolChannelConfig) -> None:
        session = self._require_session()
        if waveform == "SQU" and config.square_duty_percent is not None:
            self._assert_finite("duty cycle", config.square_duty_percent)
            if not 0 < config.square_duty_percent < 100:
                raise SafetyViolation("Duty cycle must be in the range (0, 100)%.")
            session.write(f"{prefix}:FUNC:SQU:DCYC {config.square_duty_percent:.12g}")
        elif waveform == "RAMP" and config.ramp_symmetry_percent is not None:
            self._assert_finite("ramp symmetry", config.ramp_symmetry_percent)
            if not 0 <= config.ramp_symmetry_percent <= 100:
                raise SafetyViolation("Ramp symmetry must be in the range 0–100%.")
            session.write(f"{prefix}:FUNC:RAMP:SYMM {config.ramp_symmetry_percent:.12g}")
        elif waveform == "PULS":
            for suffix, value in (
                ("WIDT", config.pulse_width_s),
                ("TRAN:LEAD", config.pulse_leading_s),
                ("TRAN:TRA", config.pulse_trailing_s),
            ):
                if value is not None:
                    self._assert_finite("pulse parameter", value)
                    if value <= 0:
                        raise SafetyViolation("Pulse width and edge times must be positive.")
                    session.write(f"{prefix}:FUNC:PULS:{suffix} {value:.12g}")

    def set_output(self, channel: int, enabled: bool) -> bool:
        channel_settings = self._channel_settings(channel)
        if enabled:
            self._assert_independent_channels()
            self._synchronize_advanced_states(channel)
            self._interlock().assert_can_enable(
                device_name=f"Rigol CH{channel}",
                device_allows_output=(
                    self._settings.safety.allow_output_enable
                    and channel_settings.enabled
                ),
            )
            config = self._last_config.get(channel)
            if config is None:
                raise SafetyViolation("Configure and validate the Rigol channel before OUTPUT ON.")
            self._validate_waveform_config(config)
            if channel in self._modulation_enabled:
                modulation = self._last_modulation_config.get(channel)
                if modulation is None:
                    raise SafetyViolation("Rigol modulation state is missing its validated envelope.")
                self._validate_modulation_envelope(config, modulation)
            if channel in self._sweep_enabled and channel not in self._last_sweep_config:
                raise SafetyViolation("Rigol sweep is active without a locally validated configuration.")
            if channel in self._burst_enabled and channel not in self._last_burst_config:
                raise SafetyViolation("Rigol burst is active without a locally validated configuration.")
            # Re-read the complete channel immediately before the single
            # energising transition. This also proves OUTPUT is still OFF and
            # detects front-panel or remote changes after configure.
            self._verify_applied_configuration(config)
        session = self._require_session()
        try:
            session.write(f":OUTP{channel} {'ON' if enabled else 'OFF'}")
            self._check_errors()
            active = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            if active != enabled:
                raise DeviceError("Rigol did not confirm the requested output state.")
        except Exception as exc:
            if enabled:
                # The energising command may already have reached the
                # instrument. Never return from a failed ON transaction while
                # relying on an unverified output state.
                try:
                    self.emergency_off()
                except Exception as shutdown_exc:
                    raise DeviceError(
                        "Rigol OUTPUT ON failed and emergency shutdown could not be confirmed: "
                        f"{shutdown_exc}"
                    ) from exc
            else:
                self._state = DeviceState.UNKNOWN
            raise
        self._output_states[channel] = active
        self._update_aggregate_output_state()
        return active

    def _synchronize_advanced_states(self, channel: int) -> None:
        """Read the instrument, never infer advanced mode state from the UI."""

        session = self._require_session()
        source = f":SOUR{channel}"
        feature_for = {
            "modulation": "modulation",
            "sweep": "frequency_sweep",
            "burst": "burst",
            "harmonics": "harmonics",
        }
        query_for = {
            "modulation": f"{source}:MOD?",
            "sweep": f"{source}:SWE:STAT?",
            "burst": f"{source}:BURS:STAT?",
            "harmonics": f"{source}:HARM?",
        }
        states: dict[str, bool] = {}
        for name, feature in feature_for.items():
            if self._capabilities is not None and self._capabilities.supports(feature):
                states[name] = self._parse_on_off(
                    session.query(query_for[name]), field=f"{name} state"
                )
            else:
                states[name] = False
        for active, enabled_set in (
            (states["modulation"], self._modulation_enabled),
            (states["sweep"], self._sweep_enabled),
            (states["burst"], self._burst_enabled),
        ):
            if active:
                enabled_set.add(channel)
            else:
                enabled_set.discard(channel)
        active_names = [name for name, active in states.items() if active]
        if states["harmonics"]:
            raise SafetyViolation(
                "Rigol harmonics are active, but their worst-case output envelope is not controlled by this UI. "
                "Disable harmonics on the instrument before OUTPUT ON."
            )
        if len(active_names) > 1:
            raise SafetyViolation(
                "Rigol reports conflicting advanced modes active: " + ", ".join(active_names) + "."
            )

    def _assert_independent_channels(self) -> None:
        """Prevent one channel edit from silently changing the other channel."""

        session = self._require_session()
        if self._capabilities is not None and self._capabilities.supports("coupling"):
            coupling = session.query(":COUP?").strip().upper()
            if "ON" in coupling:
                raise SafetyViolation(
                    "Rigol channel coupling is active on the instrument. Disable frequency, phase, and amplitude coupling before using independent channel controls."
                )
        if self._capabilities is not None and self._capabilities.supports("tracking"):
            tracking = session.query(":SOUR1:TRACK?").strip().upper()
            if tracking not in {"0", "OFF"}:
                raise SafetyViolation(
                    "Rigol channel tracking is active on the instrument. Disable TRACK before using independent channel controls."
                )

    def configure_output(self, config: RigolOutputConfig) -> None:
        """Configure the output path while proving that the carrier is OFF."""

        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Rigol CH{config.channel} is disabled in the station profile.")
        last = self._last_config.get(config.channel)
        if last is None:
            raise SafetyViolation("Configure the waveform and validate the Rigol current model first.")
        expected_load = self._format_load(config.output_load)
        if self._format_load(last.output_load) != expected_load:
            raise SafetyViolation(
                "Changing output load requires waveform reconfiguration to recalculate DUT current."
            )
        if config.polarity not in {"NORM", "INV"} or config.mode not in {"NORM", "GAT"}:
            raise SafetyViolation("Rigol output polarity or mode is invalid.")
        if config.gate_polarity not in {"NORM", "INV"} or config.sync_polarity not in {"NORM", "INV"}:
            raise SafetyViolation("Rigol gate/sync polarity is invalid.")
        self._assert_finite("SYNC delay", config.sync_delay_s)
        if config.sync_delay_s < 0 or config.sync_delay_s > 10:
            raise SafetyViolation("Rigol SYNC delay must be in the range 0–10 s.")
        session = self._require_session()
        prefix = f":OUTP{config.channel}"
        session.write(f"{prefix} OFF")
        self._verify_output_off(config.channel)
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

    def configure_modulation(self, config: RigolModulationConfig) -> None:
        """Configure modulation with the carrier output forced OFF."""

        self._assert_feature("modulation")
        if not config.enabled:
            self._disable_advanced_mode(
                channel=config.channel,
                command="MOD OFF",
                readback_field="MOD",
                operation="modulation",
            )
            self._modulation_enabled.discard(config.channel)
            self._last_modulation_config.pop(config.channel, None)
            return
        if config.enabled:
            self._assert_advanced_mode_exclusive(config.channel, "modulation")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Rigol CH{config.channel} is disabled in the station profile.")
        if config.modulation_type not in {"AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"}:
            raise SafetyViolation("Rigol modulation type is not allowed.")
        if config.source not in {"INT", "EXT"}:
            raise SafetyViolation("Rigol modulation source must be INT or EXT.")
        if config.source == "INT" and config.internal_shape not in {"SIN", "SQU", "RAMP", "NOIS", "ARB"}:
            raise SafetyViolation("Rigol internal modulation waveform is not allowed.")
        if config.polarity not in {"POS", "NEG"}:
            raise SafetyViolation("Rigol modulation polarity must be POS or NEG.")
        if config.source == "INT":
            self._assert_finite("modulation rate", config.rate_hz)
        self._assert_finite("modulation parameter", config.parameter)
        if config.source == "INT" and config.rate_hz <= 0:
            raise SafetyViolation("Modulation frequency/rate must be positive.")
        rate_limits = channel.lab_limits.modulation_rate
        if config.source == "INT" and rate_limits.enabled:
            minimum = parse_quantity(rate_limits.min, "frequency").si_value
            maximum = parse_quantity(rate_limits.max, "frequency").si_value
            if not minimum <= config.rate_hz <= maximum:
                raise SafetyViolation("Modulation rate is outside the configured Rigol frequency range.")
        if config.parameter < 0:
            raise SafetyViolation("The modulation parameter cannot be negative.")
        if config.modulation_type in {"AM", "PWM"} and config.parameter > 100:
            raise SafetyViolation(f"Rigol {config.modulation_type} percentage must be in the range 0..100%.")
        if config.modulation_type in {"PM", "PSK"} and config.parameter > 360:
            raise SafetyViolation(f"Rigol {config.modulation_type} phase must be in the range 0..360 degrees.")
        self._validate_modulation_envelope(
            self._last_config.get(config.channel), config
        )
        session = self._require_session()
        source = f":SOUR{config.channel}"
        kind = config.modulation_type
        session.write(f":OUTP{config.channel} OFF")
        self._verify_output_off(config.channel)
        session.write(f"{source}:MOD OFF")
        session.write(f"{source}:MOD:TYPE {kind}")
        session.write(f"{source}:{kind}:SOUR {config.source}")
        if config.source == "INT":
            if kind in {"AM", "FM", "PM", "PWM"}:
                session.write(f"{source}:{kind}:INT:FREQ {config.rate_hz:.12g}")
                session.write(f"{source}:{kind}:INT:FUNC {config.internal_shape}")
            else:
                session.write(f"{source}:{kind}:INT:RATE {config.rate_hz:.12g}")
        if kind in {"ASK", "FSK", "PSK"}:
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
        expected_fields: dict[str, str | float | int | bool] = {
            "MOD": config.enabled,
            "MOD:TYPE": kind,
            f"{kind}:SOUR": config.source,
            f"{kind}:{suffix}": config.parameter,
        }
        if config.source == "INT":
            if kind in {"AM", "FM", "PM", "PWM"}:
                expected_fields.update(
                    {
                        f"{kind}:INT:FREQ": config.rate_hz,
                        f"{kind}:INT:FUNC": config.internal_shape,
                    }
                )
            else:
                expected_fields[f"{kind}:INT:RATE"] = config.rate_hz
        if kind in {"ASK", "FSK", "PSK"}:
            expected_fields[f"{kind}:POL"] = config.polarity
        self._verify_advanced_configuration(
            config.channel, "modulation", expected_fields
        )
        self._modulation_enabled.add(config.channel)
        self._last_modulation_config[config.channel] = config

    def _validate_modulation_envelope(
        self,
        carrier: RigolChannelConfig | None,
        modulation: RigolModulationConfig,
    ) -> None:
        """Validate the worst setpoint reached by a modulation before OUTPUT ON."""

        if carrier is None:
            raise SafetyViolation("Configure the Rigol carrier before enabling modulation.")
        kind = modulation.modulation_type
        waveform = carrier.waveform.upper()
        if waveform in {"DC", "NOIS"}:
            raise SafetyViolation(f"Rigol {waveform} cannot be used as a modulation carrier.")
        if kind == "PWM" and waveform != "PULS":
            raise SafetyViolation("Rigol PWM requires a pulse carrier waveform.")
        parameter = float(modulation.parameter)
        if kind in {"AM", "ASK"}:
            base_vpp = carrier.high_level_v - carrier.low_level_v
            candidate_vpp = (
                base_vpp * (1.0 + parameter / 100.0)
                if kind == "AM"
                else max(base_vpp, parameter)
            )
            offset = (carrier.high_level_v + carrier.low_level_v) / 2.0
            self._validate_waveform_config(
                replace(
                    carrier,
                    high_level_v=offset + candidate_vpp / 2.0,
                    low_level_v=offset - candidate_vpp / 2.0,
                )
            )
        elif kind in {"FM", "FSK"}:
            frequencies = (
                (carrier.frequency_hz - parameter, carrier.frequency_hz + parameter)
                if kind == "FM"
                else (carrier.frequency_hz, parameter)
            )
            if min(frequencies) <= 0:
                raise SafetyViolation(f"Rigol {kind} reaches a non-positive frequency.")
            for frequency in frequencies:
                self._validate_waveform_config(replace(carrier, frequency_hz=frequency))

    def configure_frequency_sweep(self, config: RigolFrequencySweepConfig) -> None:
        """Configure the generator's internal frequency sweep at OUTPUT OFF."""

        self._assert_feature("frequency_sweep")
        if not config.enabled:
            self._disable_advanced_mode(
                channel=config.channel,
                command="SWE:STAT OFF",
                readback_field="SWE:STAT",
                operation="frequency sweep",
            )
            self._sweep_enabled.discard(config.channel)
            self._last_sweep_config.pop(config.channel, None)
            return
        if config.enabled:
            self._assert_advanced_mode_exclusive(config.channel, "frequency sweep")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Rigol CH{config.channel} is disabled in the station profile.")
        for name, value in (
            ("start sweep", config.start_hz),
            ("stop sweep", config.stop_hz),
            ("sweep duration", config.duration_s),
            ("start hold sweep", config.start_hold_s),
            ("stop hold sweep", config.stop_hold_s),
            ("return time sweep", config.return_time_s),
        ):
            self._assert_finite(name, value)
        if config.spacing not in {"LIN", "LOG", "STEP"}:
            raise SafetyViolation("Rigol sweep spacing must be LIN, LOG, or STEP.")
        if config.trigger_source not in {"INT", "EXT", "MAN"} or config.trigger_slope not in {"POS", "NEG"}:
            raise SafetyViolation("Rigol sweep trigger contains an unsupported value.")
        if isinstance(config.steps, bool) or not isinstance(config.steps, int):
            raise SafetyViolation("Rigol sweep step count must be an integer.")
        if config.start_hz <= 0 or config.stop_hz <= 0 or config.start_hz == config.stop_hz:
            raise SafetyViolation("Sweep requires positive, different start and stop frequencies.")
        if config.duration_s <= 0 or min(config.start_hold_s, config.stop_hold_s, config.return_time_s) < 0:
            raise SafetyViolation("Sweep time must be positive; hold/return times cannot be negative.")
        advanced = channel.lab_limits
        if advanced.sweep_duration.enabled:
            duration_min = parse_quantity(advanced.sweep_duration.min, "time").si_value
            duration_max = parse_quantity(advanced.sweep_duration.max, "time").si_value
            if not duration_min <= config.duration_s <= duration_max:
                raise SafetyViolation("Sweep time is outside the configured range.")
        if advanced.sweep_steps.enabled and not advanced.sweep_steps.min <= config.steps <= advanced.sweep_steps.max:
            raise SafetyViolation("Sweep step count is outside the configured range.")
        limits = channel.lab_limits.frequency
        if limits.enabled:
            for value, name in ((config.start_hz, "start"), (config.stop_hz, "stop")):
                self._enforce_frequency(value, limits.min, limits.max, f"Sweep {name}")
        session = self._require_session()
        source = f":SOUR{config.channel}"
        session.write(f":OUTP{config.channel} OFF")
        self._verify_output_off(config.channel)
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
        self._verify_advanced_configuration(
            config.channel,
            "frequency sweep",
            {
                "FREQ:STAR": config.start_hz,
                "FREQ:STOP": config.stop_hz,
                "SWE:TIME": config.duration_s,
                "SWE:SPAC": config.spacing,
                "SWE:STEP": config.steps,
                "SWE:HTIM:STAR": config.start_hold_s,
                "SWE:HTIM:STOP": config.stop_hold_s,
                "SWE:RTIM": config.return_time_s,
                "SWE:TRIG:SOUR": config.trigger_source,
                "SWE:TRIG:SLOP": config.trigger_slope,
                "SWE:TRIG:TRIGO": config.trigger_output,
                "SWE:STAT": config.enabled,
            },
        )
        self._sweep_enabled.add(config.channel)
        self._last_sweep_config[config.channel] = config

    def trigger_frequency_sweep(self, channel: int) -> None:
        self._assert_feature("frequency_sweep")
        self._channel_settings(channel)
        self._require_session().write(f":SOUR{channel}:SWE:TRIG")
        self._check_errors()

    def configure_burst(self, config: RigolBurstConfig) -> None:
        """Configure burst/gate parameters while the carrier output is OFF."""

        self._assert_feature("burst")
        if not config.enabled:
            self._disable_advanced_mode(
                channel=config.channel,
                command="BURS OFF",
                readback_field="BURS:STAT",
                operation="burst",
            )
            self._burst_enabled.discard(config.channel)
            self._last_burst_config.pop(config.channel, None)
            return
        if config.enabled:
            self._assert_advanced_mode_exclusive(config.channel, "burst")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Rigol CH{config.channel} is disabled in the station profile.")
        for name, value in (
            ("faza burst", config.phase_deg),
            ("okres burst", config.period_s),
            ("delay burst", config.delay_s),
        ):
            self._assert_finite(name, value)
        if config.mode not in {"TRIG", "GAT"}:
            raise SafetyViolation("Rigol burst mode must be TRIG or GAT.")
        if config.trigger_source not in {"INT", "EXT", "MAN"} or config.trigger_slope not in {"POS", "NEG"}:
            raise SafetyViolation("Rigol burst trigger contains an unsupported value.")
        if config.gate_polarity not in {"POS", "NEG"} or config.idle not in {"FPT", "TOP", "CENT", "BOT"}:
            raise SafetyViolation("Rigol burst gate/idle parameters are invalid.")
        if isinstance(config.cycles, bool) or not isinstance(config.cycles, int):
            raise SafetyViolation("Rigol burst cycle count must be an integer.")
        if config.cycles < 1 or config.period_s <= 0 or config.delay_s < 0:
            raise SafetyViolation("Burst requires cycles >= 1, period > 0, and delay >= 0.")
        limits = channel.lab_limits
        if limits.burst_period.enabled:
            period_min = parse_quantity(limits.burst_period.min, "time").si_value
            period_max = parse_quantity(limits.burst_period.max, "time").si_value
            if not period_min <= config.period_s <= period_max:
                raise SafetyViolation("Burst period is outside the configured range.")
        if limits.burst_cycles.enabled and not limits.burst_cycles.min <= config.cycles <= limits.burst_cycles.max:
            raise SafetyViolation("Burst cycle count is outside the configured range.")
        session = self._require_session()
        source = f":SOUR{config.channel}"
        session.write(f":OUTP{config.channel} OFF")
        self._verify_output_off(config.channel)
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
        self._verify_advanced_configuration(
            config.channel,
            "burst",
            {
                "BURS:MODE": config.mode,
                "BURS:NCYC": config.cycles,
                "BURS:PHAS": config.phase_deg,
                "BURS:INT:PER": config.period_s,
                "BURS:TDEL": config.delay_s,
                "BURS:TRIG:SOUR": config.trigger_source,
                "BURS:TRIG:SLOP": config.trigger_slope,
                "BURS:TRIG:TRIGO": config.trigger_output,
                "BURS:GATE:POL": config.gate_polarity,
                "BURS:IDLE": config.idle,
                "BURS:STAT": config.enabled,
            },
        )
        self._burst_enabled.add(config.channel)
        self._last_burst_config[config.channel] = config

    def _disable_advanced_mode(
        self,
        *,
        channel: int,
        command: str,
        readback_field: str,
        operation: str,
    ) -> None:
        """Always permit a transition toward the non-energising mode state."""

        self._channel_settings(channel)
        session = self._require_session()
        source = f":SOUR{channel}"
        session.write(f":OUTP{channel} OFF")
        self._verify_output_off(channel)
        session.write(f"{source}:{command}")
        self._check_errors()
        self._verify_advanced_configuration(
            channel, operation, {readback_field: False}
        )

    def _assert_advanced_mode_exclusive(self, channel: int, requested: str) -> None:
        """Reject implicit mode switching; the operator must disable it first."""

        session = self._require_session()
        source = f":SOUR{channel}"
        queries = {
            "modulation": f"{source}:MOD?",
            "frequency sweep": f"{source}:SWE:STAT?",
            "burst": f"{source}:BURS:STAT?",
        }
        if self._capabilities is not None and self._capabilities.supports("harmonics"):
            queries["harmonics"] = f"{source}:HARM?"
        active: list[str] = []
        for mode, query in queries.items():
            if mode == requested:
                continue
            try:
                response = session.query(query).strip().upper()
            except Exception as exc:
                raise DeviceError(
                    f"Rigol could not confirm whether {mode} is disabled before enabling {requested}."
                ) from exc
            if self._parse_on_off(response, field=f"{mode} state"):
                active.append(mode)
        if active:
            raise SafetyViolation(
                f"Cannot enable Rigol {requested} while {', '.join(active)} is active. "
                "Disable the active mode first."
            )

    def trigger_burst(self, channel: int) -> None:
        self._assert_feature("burst")
        self._channel_settings(channel)
        self._require_session().write(f":SOUR{channel}:BURS:TRIG")
        self._check_errors()

    def synchronize_phases(self) -> None:
        self._assert_feature("phase_sync")
        self._require_session().write(":SOUR1:PHAS:SYNC")
        self._check_errors()

    def configure_counter(self, config: RigolCounterConfig) -> None:
        """Configure the rear-panel frequency counter with full readback."""

        self._assert_feature("counter")
        if config.state not in {"ON", "OFF", "RUN", "STOP", "SINGLE"}:
            raise SafetyViolation("Rigol counter state is invalid.")
        if config.coupling not in {"AC", "DC"}:
            raise SafetyViolation("Rigol counter coupling must be AC or DC.")
        if config.gate_time not in {"AUTO", "USER1", "USER2", "USER3", "USER4", "USER5", "USER6"}:
            raise SafetyViolation("Rigol counter gate time is invalid.")
        self._assert_finite("counter trigger level", config.trigger_level_v)
        self._assert_finite("counter sensitivity", config.sensitivity_percent)
        if not -2.5 <= config.trigger_level_v <= 2.5:
            raise SafetyViolation("Rigol counter trigger level must be between -2.5 V and 2.5 V.")
        if not 0 <= config.sensitivity_percent <= 100:
            raise SafetyViolation("Rigol counter sensitivity must be between 0% and 100%.")
        session = self._require_session()
        session.write(f":COUN:COUP {config.coupling}")
        if config.gate_time == "AUTO":
            session.write(":COUN:AUTO")
        else:
            session.write(f":COUN:GATE {config.gate_time}")
        session.write(f":COUN:HF {'ON' if config.high_frequency_rejection else 'OFF'}")
        session.write(f":COUN:LEVE {config.trigger_level_v:.12g}")
        session.write(f":COUN:SENS {config.sensitivity_percent:.12g}")
        session.write(f":COUN {config.state}")
        self._check_errors()
        expected = {
            ":COUN:COUP?": config.coupling,
            ":COUN:HF?": "ON" if config.high_frequency_rejection else "OFF",
            ":COUN:LEVE?": config.trigger_level_v,
            ":COUN:SENS?": config.sensitivity_percent,
        }
        for query, wanted in expected.items():
            response = session.query(query).strip().upper()
            if isinstance(wanted, str):
                if response != wanted:
                    raise DeviceError(f"Rigol counter readback {query} returned {response}, expected {wanted}.")
            else:
                try:
                    actual = float(response)
                except ValueError as exc:
                    raise DeviceError(f"Rigol returned invalid counter readback for {query}.") from exc
                if not self._same_number(actual, wanted, absolute=1e-6):
                    raise DeviceError(f"Rigol counter readback {query} returned {actual:.9g}, expected {wanted:.9g}.")
        state_response = session.query(":COUN?").strip().upper()
        accepted_states = {
            "ON": {"ON", "RUN"},
            "RUN": {"RUN", "ON"},
            "SINGLE": {"SINGLE", "STOP"},
            "STOP": {"STOP"},
            "OFF": {"OFF"},
        }[config.state]
        if state_response not in accepted_states:
            raise DeviceError(
                f"Rigol counter state readback returned {state_response}, expected one of {sorted(accepted_states)}."
            )

    def read_counter(self) -> RigolCounterReading:
        """Read the five documented counter results without changing output state."""

        self._assert_feature("counter")
        values = self._require_session().query(":COUN:MEAS?").strip().split(",")
        if len(values) != 5:
            raise DeviceError("Rigol counter returned an incomplete measurement.")
        try:
            parsed = tuple(float(value) for value in values)
        except ValueError as exc:
            raise DeviceError("Rigol counter returned non-numeric measurement data.") from exc
        if not all(math.isfinite(value) for value in parsed):
            raise DeviceError("Rigol counter returned a non-finite measurement.")
        return RigolCounterReading(*parsed)

    def _verify_output_off(self, channel: int) -> None:
        active = self._parse_output_state(
            self._require_session().query(f":OUTP{channel}?"), channel=channel
        )
        if active:
            raise DeviceError("Rigol enabled output during advanced configuration.")
        self._output_states[channel] = False
        self._update_aggregate_output_state()

    def _verify_advanced_configuration(
        self,
        channel: int,
        operation: str,
        fields: dict[str, str | float | int | bool],
    ) -> None:
        """Read back each advanced field after an OUTPUT-OFF transaction."""

        session = self._require_session()
        prefix = f":SOUR{channel}"
        mismatches: list[str] = []
        for suffix, expected in fields.items():
            response = session.query(f"{prefix}:{suffix}?").strip().upper()
            if isinstance(expected, bool):
                actual = self._parse_on_off(response, field=f"{operation} {suffix}")
                if actual != expected:
                    mismatches.append(
                        f"{suffix} {response} != {'ON' if expected else 'OFF'}"
                    )
            elif isinstance(expected, str):
                if response != expected.upper():
                    mismatches.append(
                        f"{suffix} {response} != {expected.upper()}"
                    )
            else:
                try:
                    actual_number = float(response)
                except ValueError as exc:
                    raise DeviceError(
                        f"Rigol returned invalid {operation} readback for "
                        f"{suffix}: {response!r}."
                    ) from exc
                if not self._same_number(
                    actual_number, float(expected), absolute=1e-9
                ):
                    mismatches.append(
                        f"{suffix} {actual_number:.12g} != {float(expected):.12g}"
                    )
        self._verify_output_off(channel)
        if mismatches:
            raise DeviceError(
                f"Rigol {operation} readback failed (output remains OFF): "
                + "; ".join(mismatches)
            )

    def _verify_output_configuration(self, expected: RigolOutputConfig) -> None:
        session = self._require_session()
        prefix = f":OUTP{expected.channel}"
        state = self._parse_output_state(
            session.query(f"{prefix}?"), channel=expected.channel
        )
        load = session.query(f"{prefix}:LOAD?").strip().upper()
        polarity = session.query(f"{prefix}:POL?").strip().upper()
        mode = session.query(f"{prefix}:MODE?").strip().upper()
        gate_polarity = session.query(f"{prefix}:GAT:POL?").strip().upper()
        sync_enabled = session.query(f"{prefix}:SYNC?").strip().upper()
        sync_polarity = session.query(f"{prefix}:SYNC:POL?").strip().upper()
        sync_delay = float(session.query(f"{prefix}:SYNC:DEL?"))
        mismatches: list[str] = []
        if state:
            mismatches.append("OUTPUT is ON")
        if not self._load_response_matches(load, expected.output_load):
            mismatches.append(f"LOAD {load}")
        if polarity != expected.polarity:
            mismatches.append(f"POL {polarity}")
        if mode != expected.mode:
            mismatches.append(f"MODE {mode}")
        if gate_polarity != expected.gate_polarity:
            mismatches.append(f"GAT:POL {gate_polarity}")
        if self._parse_on_off(sync_enabled, field="SYNC state") != expected.sync_enabled:
            mismatches.append(f"SYNC {sync_enabled}")
        if sync_polarity != expected.sync_polarity:
            mismatches.append(f"SYNC:POL {sync_polarity}")
        if not self._same_number(sync_delay, expected.sync_delay_s, absolute=1e-9):
            mismatches.append(f"SYNC:DEL {sync_delay:.9g}")
        if mismatches:
            raise DeviceError("Rigol output configuration readback failed (output remains OFF): " + "; ".join(mismatches))

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
            raise SafetyViolation(f"{name}={value:.9g} Hz is outside the configured {lower:.9g}–{upper:.9g} Hz range.")
