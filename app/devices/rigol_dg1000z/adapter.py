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
    RIGOL_DG1000Z_FREQUENCY_RESOLUTION_HZ,
    quantize_rigol_frequency,
    quantize_rigol_voltage,
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
    internal_shape: Literal[
        "SIN", "SQU", "TRI", "RAMP", "NRAMP", "NOIS", "USER", "ARB"
    ] = "SIN"
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
    trigger_output: Literal["OFF", "POS", "NEG"] | bool = "OFF"


@dataclass(frozen=True, slots=True)
class RigolBurstConfig:
    channel: int
    enabled: bool
    mode: Literal["TRIG", "INF", "GAT"] = "TRIG"
    cycles: int = 1
    phase_deg: float = 0.0
    period_s: float = 1.0
    delay_s: float = 0.0
    trigger_source: Literal["INT", "EXT", "MAN"] = "INT"
    trigger_slope: Literal["POS", "NEG"] = "POS"
    trigger_output: Literal["OFF", "POS", "NEG"] | bool = "OFF"
    gate_polarity: Literal["NORM", "INV"] = "NORM"
    idle: Literal["FPT", "TOP", "CENTER", "BOTTOM", "CENT", "BOT"] = "FPT"


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
                confirmed_off, shutdown_issues = self._attempt_all_outputs_off()
                if not confirmed_off:
                    raise DeviceError(
                        "Rigol did not confirm both outputs OFF during connection: "
                        + "; ".join(shutdown_issues)
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
        probe_optional = self._settings.capabilities.get(
            "probe_optional_commands_on_connect",
            self._settings.capabilities.get("probe_optional_commands", True),
        )
        if bool(probe_optional):
            session = self._require_session()
            for feature, query in (
                ("modulation", ":SOUR1:MOD?"),
                ("frequency_sweep", ":SOUR1:SWE:STAT?"),
                ("burst", ":SOUR1:BURS:STAT?"),
                ("phase_sync", ":SOUR1:PHAS?"),
                ("counter", ":COUN?"),
                ("harmonics", ":SOUR1:HARM?"),
                ("waveform_sum", ":SOUR1:SUM?"),
                ("coupling", ":COUP?"),
                ("tracking", ":SOUR1:TRACK?"),
            ):
                try:
                    response = session.query(query)
                except Exception:
                    unsupported.add(feature)
                else:
                    if self._capability_response_valid(feature, response):
                        features.add(feature)
                    else:
                        unsupported.add(feature)
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

    @staticmethod
    def _capability_response_valid(feature: str, response: str) -> bool:
        normalized = str(response).strip().upper()
        if feature in {
            "modulation",
            "frequency_sweep",
            "burst",
            "harmonics",
            "waveform_sum",
        }:
            return normalized in {"0", "1", "OFF", "ON"}
        if feature == "phase_sync":
            try:
                return math.isfinite(float(normalized))
            except ValueError:
                return False
        if feature == "counter":
            return normalized in {"0", "1", "OFF", "ON", "RUN", "STOP", "SINGLE"}
        if feature == "tracking":
            return normalized in {"0", "1", "OFF", "ON", "INV", "INVERTED"}
        if feature == "coupling":
            parts = [part.strip() for part in normalized.split(",")]
            if len(parts) != 3:
                return False
            expected_names = ("FREQ", "PHASE", "AMPL")
            for part, name in zip(parts, expected_names, strict=True):
                if ":" not in part:
                    return False
                actual_name, state = (item.strip() for item in part.split(":", 1))
                if actual_name != name or state not in {"0", "1", "OFF", "ON"}:
                    return False
            return True
        return False

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
            confirmed_off, shutdown_issues = self._attempt_all_outputs_off()
            if not confirmed_off:
                shutdown_error = DeviceError(
                    "Rigol did not confirm both outputs OFF before disconnect: "
                    + "; ".join(shutdown_issues)
                )
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
        failures: list[tuple[int, Exception]] = []
        for channel in (1, 2):
            try:
                session.write(f":OUTP{channel} OFF")
            except Exception as exc:
                failures.append((channel, exc))
        if failures:
            self._state = DeviceState.UNKNOWN
            detail = "; ".join(
                f"CH{channel}: {str(exc).strip() or type(exc).__name__}"
                for channel, exc in failures
            )
            raise DeviceError(
                "Rigol OUTPUT OFF command failed after attempting both channels: "
                + detail
            ) from failures[0][1]

    def _attempt_all_outputs_off(self) -> tuple[bool, tuple[str, ...]]:
        """Attempt every shutdown action and confirm both physical outputs OFF."""

        issues: list[str] = []
        try:
            self._write_all_outputs_off()
        except Exception as exc:
            issues.append(str(exc).strip() or type(exc).__name__)

        states: dict[int, bool] | None = None
        try:
            states = self._read_output_states()
        except Exception as exc:
            issues.append(str(exc).strip() or type(exc).__name__)

        if states is not None and set(states) == {1, 2} and not any(states.values()):
            self._output_states = {1: False, 2: False}
            self._state = DeviceState.OUTPUT_OFF
            return True, tuple(issues)

        if states is not None:
            active = ", ".join(
                f"CH{channel}" for channel, enabled in states.items() if enabled
            )
            issues.append(
                f"readback still reports OUTPUT ON for {active or 'an unknown channel'}"
            )
        if not issues:
            issues.append("both OFF readbacks were not available")
        self._state = DeviceState.UNKNOWN
        return False, tuple(issues)

    def emergency_off(self) -> None:
        if self._session is None:
            return
        # DeviceAdapter.emergency_off() is deliberately non-throwing so one
        # failed instrument cannot prevent shutdown attempts for other devices.
        # The aggregate state communicates whether both OFF readbacks succeeded.
        self._attempt_all_outputs_off()

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

    def refresh_station_context(self, station: object) -> None:
        if not isinstance(station, StationSettings):
            raise TypeError("Rigol context refresh requires StationSettings.")
        self._station = station
        self._settings = station.rigol

    def _read_output_states(self) -> dict[int, bool]:
        session = self._require_session()
        states: dict[int, bool] = {}
        failures: list[tuple[int, Exception]] = []
        for channel in (1, 2):
            try:
                states[channel] = self._parse_output_state(
                    session.query(f":OUTP{channel}?"), channel=channel
                )
            except Exception as exc:
                failures.append((channel, exc))
        self._output_states.update(states)
        if failures:
            self._state = DeviceState.UNKNOWN
            detail = "; ".join(
                f"CH{channel}: {str(exc).strip() or type(exc).__name__}"
                for channel, exc in failures
            )
            raise DeviceError(
                "Rigol OUTPUT readback failed after querying both channels: "
                + detail
            ) from failures[0][1]
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

    @staticmethod
    def _trigger_output_token(value: str | bool) -> str:
        # Keep bool compatibility for older recipes/UI snapshots while using
        # the documented POS|NEG|OFF SCPI domain on the wire.
        if isinstance(value, bool):
            return "POS" if value else "OFF"
        normalized = str(value).strip().upper()
        if normalized not in {"OFF", "POS", "NEG"}:
            raise SafetyViolation(
                "Rigol trigger output must be OFF, POS, or NEG."
            )
        return normalized

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
        if not channel.enabled:
            raise SafetyViolation(f"Rigol CH{config.channel} is disabled in the station profile.")
        self._assert_finite("phase", config.phase_deg)
        if not 0 <= config.phase_deg <= 360:
            raise SafetyViolation("Rigol carrier phase must be within 0..360 degrees.")
        self._validate_waveform_config(config)
        self._validate_shape_parameters(config)
        # The recipe/compiler may produce a binary-float interpolation with
        # more digits than the DG1000Z can represent.  Normalize only after
        # validating the raw request, then validate the exact wire state again
        # before any session mutation.
        config = self._quantize_channel_config(config)
        estimate = self._validate_waveform_config(config)
        self._validate_shape_parameters(config)
        # Finish deterministic local validation before querying hardware. A
        # malformed setpoint must fail without any VISA traffic.
        self._assert_independent_channels()
        session = self._require_session()
        prefix = f":SOUR{config.channel}"
        waveform = config.waveform.upper()
        # A previous readback is no longer evidence as soon as this
        # transaction starts.  If any following write/query fails, OUTPUT ON
        # must require a complete configuration again instead of reusing a
        # stale carrier snapshot.
        self._last_config.pop(config.channel, None)
        self._last_output_config.pop(config.channel, None)
        self._state = DeviceState.UNKNOWN
        session.write(f":OUTP{config.channel} OFF")
        self._verify_output_off(config.channel)
        # APPL/FUNC changes and advanced modes interact on the instrument.
        # Start every carrier transaction from one explicit, reproducible
        # state instead of relying on whatever the front panel last selected.
        session.write(f"{prefix}:MOD OFF")
        session.write(f"{prefix}:SWE:STAT OFF")
        session.write(f"{prefix}:BURS OFF")
        # Harmonic generation and waveform summing alter the physical output
        # envelope but are not part of a basic carrier configuration.  Reset
        # them explicitly instead of inheriting a front-panel state.
        session.write(f"{prefix}:HARM OFF")
        session.write(f"{prefix}:SUM OFF")
        self._modulation_enabled.discard(config.channel)
        self._last_modulation_config.pop(config.channel, None)
        self._sweep_enabled.discard(config.channel)
        self._last_sweep_config.pop(config.channel, None)
        self._burst_enabled.discard(config.channel)
        self._last_burst_config.pop(config.channel, None)
        session.write(f":OUTP{config.channel}:LOAD {self._format_load(config.output_load)}")
        if waveform == "DC":
            session.write(f"{prefix}:APPL:DC DEF,DEF,{self._format_wire_number(config.high_level_v)}")
        else:
            session.write(f"{prefix}:FUNC {waveform}")
            if waveform == "USER":
                # USER memory also supports sample-rate playback. This model
                # expresses a repetition frequency, so force FREQ mode instead
                # of inheriting SRATE and changing the physical meaning of
                # frequency_hz.
                session.write(f"{prefix}:FUNC:ARB:MODE FREQ")
            if waveform != "NOIS":
                session.write(f"{prefix}:FREQ {self._format_wire_number(config.frequency_hz)}")
            # HighL and LowL are coupled representations of amplitude and
            # offset on the DG1000Z.  Program the canonical pair while OUTPUT
            # is OFF instead of walking through two conflicting endpoint
            # states that the instrument may clamp.
            amplitude_vpp = config.high_level_v - config.low_level_v
            offset_v = (config.high_level_v + config.low_level_v) / 2.0
            session.write(f"{prefix}:VOLT {self._format_wire_number(amplitude_vpp)}")
            session.write(f"{prefix}:VOLT:OFFS {self._format_wire_number(offset_v)}")
            if waveform != "NOIS":
                session.write(f"{prefix}:PHAS {config.phase_deg:.12g}")
            self._write_shape_parameters(prefix, waveform, config)
        self._check_errors()
        applied = self._verify_applied_configuration(config)
        self._last_config[config.channel] = applied
        self._update_aggregate_output_state()
        return estimate

    def update_frequency(self, channel: int, frequency_hz: float) -> float:
        """Change only carrier frequency while preserving the current OUTPUT state."""

        config = self._last_config.get(channel)
        if config is None:
            raise SafetyViolation(
                "Configure and validate the Rigol channel before updating frequency."
            )
        if config.waveform.upper() in {"DC", "NOIS"}:
            raise SafetyViolation(
                f"Rigol {config.waveform.upper()} does not support carrier-frequency updates."
            )
        raw_updated = replace(config, frequency_hz=float(frequency_hz))
        self._validate_waveform_config(raw_updated)
        # Pulse timing limits depend on the period.  A frequency-only update
        # can therefore make a previously valid width/edge configuration
        # invalid even though the voltage/frequency envelope still passes.
        self._validate_shape_parameters(raw_updated)
        updated = replace(
            raw_updated,
            frequency_hz=quantize_rigol_frequency(raw_updated.frequency_hz),
        )
        self._validate_waveform_config(updated)
        self._validate_shape_parameters(updated)
        self._assert_independent_channels()
        session = self._require_session()
        prefix = f":SOUR{channel}"
        output_before = self._parse_output_state(
            session.query(f":OUTP{channel}?"), channel=channel
        )
        if output_before != self._output_states[channel]:
            self._fail_live_setpoint_update(
                output_was_on=output_before,
                cause=DeviceError(
                    f"Rigol CH{channel} OUTPUT changed before a frequency update."
                ),
            )
        try:
            self._verify_applied_configuration(
                config, expected_output=output_before
            )
        except Exception as exc:
            self._fail_live_setpoint_update(
                output_was_on=output_before, cause=exc
            )
        if output_before:
            self._validate_active_quick_update(channel, updated)
        if self._same_frequency_readback(config.frequency_hz, updated.frequency_hz):
            self._last_config[channel] = config
            self._output_states[channel] = output_before
            self._update_aggregate_output_state()
            return config.frequency_hz
        self._last_config.pop(channel, None)
        try:
            session.write(f"{prefix}:FREQ {self._format_wire_number(updated.frequency_hz)}")
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
            if not self._same_frequency_readback(actual, updated.frequency_hz):
                raise DeviceError(
                    f"Rigol frequency readback {actual:.9g} Hz does not match "
                    f"{updated.frequency_hz:.9g} Hz."
                )
        except Exception as exc:
            self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
        self._last_config[channel] = replace(updated, frequency_hz=actual)
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
        """Update the level pair through canonical amplitude/offset commands."""

        config = self._last_config.get(channel)
        if config is None:
            raise SafetyViolation(
                "Configure and validate the Rigol channel before updating levels."
            )
        if config.waveform.upper() == "DC":
            raise SafetyViolation(
                "DC has one voltage level. Change Offset / DC level instead of HighL/LowL."
            )
        raw_updated = replace(
            config,
            high_level_v=float(high_level_v),
            low_level_v=float(low_level_v),
        )
        self._validate_waveform_config(raw_updated)
        updated = replace(
            raw_updated,
            high_level_v=quantize_rigol_voltage(raw_updated.high_level_v),
            low_level_v=quantize_rigol_voltage(raw_updated.low_level_v),
        )
        self._validate_waveform_config(updated)
        high_changed = not self._same_number(
            updated.high_level_v, config.high_level_v, absolute=1e-12
        )
        low_changed = not self._same_number(
            updated.low_level_v, config.low_level_v, absolute=1e-12
        )
        if high_changed and not low_changed:
            self.update_high_level(channel, updated.high_level_v)
            applied = self.last_channel_config(channel)
            return applied.high_level_v, applied.low_level_v
        if low_changed and not high_changed:
            self.update_low_level(channel, updated.low_level_v)
            applied = self.last_channel_config(channel)
            return applied.high_level_v, applied.low_level_v
        if not high_changed and not low_changed:
            session = self._require_session()
            output_before = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            if output_before != self._output_states[channel]:
                self._fail_live_setpoint_update(
                    output_was_on=output_before,
                    cause=DeviceError(
                        f"Rigol CH{channel} OUTPUT changed before a level update."
                    ),
                )
            try:
                self._verify_applied_configuration(
                    config, expected_output=output_before
                )
            except Exception as exc:
                self._fail_live_setpoint_update(
                    output_was_on=output_before, cause=exc
                )
            self._output_states[channel] = output_before
            self._update_aggregate_output_state()
            return config.high_level_v, config.low_level_v
        old_offset = (config.high_level_v + config.low_level_v) / 2.0
        new_offset = (updated.high_level_v + updated.low_level_v) / 2.0
        old_amplitude = config.high_level_v - config.low_level_v
        new_amplitude = updated.high_level_v - updated.low_level_v
        if self._same_number(old_offset, new_offset, absolute=1e-12):
            self.update_amplitude_vpp(channel, new_amplitude)
            applied = self.last_channel_config(channel)
            return applied.high_level_v, applied.low_level_v
        if self._same_number(old_amplitude, new_amplitude, absolute=1e-12):
            self.update_offset(channel, new_offset)
            applied = self.last_channel_config(channel)
            return applied.high_level_v, applied.low_level_v
        self._assert_independent_channels()
        session = self._require_session()
        prefix = f":SOUR{channel}"
        output_before = self._parse_output_state(
            session.query(f":OUTP{channel}?"), channel=channel
        )
        try:
            self._verify_applied_configuration(
                config, expected_output=output_before
            )
        except Exception as exc:
            self._fail_live_setpoint_update(
                output_was_on=output_before, cause=exc
            )
        if output_before:
            raise SafetyViolation(
                "Changing amplitude and offset together is not atomic while OUTPUT is ON. "
                "Change one representation at a time or switch OUTPUT OFF."
            )
        self._last_config.pop(channel, None)
        try:
            amplitude_vpp = updated.high_level_v - updated.low_level_v
            offset_v = (updated.high_level_v + updated.low_level_v) / 2.0
            session.write(f"{prefix}:VOLT {self._format_wire_number(amplitude_vpp)}")
            session.write(f"{prefix}:VOLT:OFFS {self._format_wire_number(offset_v)}")
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
                actual_high, updated.high_level_v, absolute=1e-4
            ) or not self._same_number(
                actual_low, updated.low_level_v, absolute=1e-4
            ):
                raise DeviceError(
                    "Rigol level readback does not match requested HighL/LowL."
                )
        except Exception as exc:
            self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
        self._last_config[channel] = replace(
            updated,
            high_level_v=actual_high,
            low_level_v=actual_low,
        )
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
        updated = replace(
            config,
            high_level_v=offset_v + amplitude_vpp_v / 2.0,
            low_level_v=offset_v - amplitude_vpp_v / 2.0,
        )
        return self._update_voltage_representation(
            channel, updated, command_suffix="VOLT", requested=amplitude_vpp_v
        )

    def update_offset(self, channel: int, offset_v: float) -> float:
        """Update offset while preserving validated Vpp and OUTPUT state."""

        config = self._last_config.get(channel)
        if config is None:
            raise SafetyViolation("Configure the Rigol channel before quick offset control.")
        if not math.isfinite(offset_v):
            raise SafetyViolation("Rigol offset must be finite.")
        if config.waveform.upper() == "DC":
            raw_updated = replace(
                config,
                high_level_v=float(offset_v),
                low_level_v=float(offset_v),
            )
            self._validate_waveform_config(raw_updated)
            updated = replace(
                raw_updated,
                high_level_v=quantize_rigol_voltage(raw_updated.high_level_v),
                low_level_v=quantize_rigol_voltage(raw_updated.low_level_v),
            )
            self._validate_waveform_config(updated)
            self._assert_independent_channels()
            session = self._require_session()
            prefix = f":SOUR{channel}"
            output_before = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            self._verify_applied_configuration(
                config, expected_output=output_before
            )
            if output_before:
                self._validate_active_quick_update(channel, updated)
            self._last_config.pop(channel, None)
            # Do not re-apply the whole DC function while energised.  The
            # dedicated offset command changes only the active DC level.
            try:
                session.write(
                    f"{prefix}:VOLT:OFFS {self._format_wire_number(updated.high_level_v)}"
                )
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
                if not self._same_number(
                    actual_offset, updated.high_level_v, absolute=1e-6
                ):
                    raise DeviceError(
                        "Rigol DC-level readback does not match the requested offset."
                    )
            except Exception as exc:
                self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
            self._last_config[channel] = replace(
                updated,
                high_level_v=actual_offset,
                low_level_v=actual_offset,
            )
            self._output_states[channel] = output_after
            self._update_aggregate_output_state()
            return actual_offset
        amplitude_vpp_v = config.high_level_v - config.low_level_v
        updated = replace(
            config,
            high_level_v=offset_v + amplitude_vpp_v / 2.0,
            low_level_v=offset_v - amplitude_vpp_v / 2.0,
        )
        return self._update_voltage_representation(
            channel, updated, command_suffix="VOLT:OFFS", requested=offset_v
        )

    def update_high_level(self, channel: int, high_level_v: float) -> float:
        config = self.last_channel_config(channel)
        updated = replace(config, high_level_v=float(high_level_v))
        return self._update_voltage_representation(
            channel, updated, command_suffix="VOLT:HIGH", requested=high_level_v
        )

    def update_low_level(self, channel: int, low_level_v: float) -> float:
        config = self.last_channel_config(channel)
        updated = replace(config, low_level_v=float(low_level_v))
        return self._update_voltage_representation(
            channel, updated, command_suffix="VOLT:LOW", requested=low_level_v
        )

    def _update_voltage_representation(
        self,
        channel: int,
        updated: RigolChannelConfig,
        *,
        command_suffix: str,
        requested: float,
    ) -> float:
        """Write one documented voltage control and verify all coupled views."""

        config = self.last_channel_config(channel)
        if config.waveform.upper() == "DC":
            raise SafetyViolation("Use Offset / DC level for a DC waveform.")
        # Validate the interpolated request first.  The normalized command is
        # then rebuilt from the semantic control being changed so an
        # amplitude update is not accidentally rounded as two endpoint values.
        self._validate_waveform_config(updated)
        normalized_requested = quantize_rigol_voltage(float(requested))
        if command_suffix == "VOLT":
            offset = (config.high_level_v + config.low_level_v) / 2.0
            normalized_updated = replace(
                config,
                high_level_v=offset + normalized_requested / 2.0,
                low_level_v=offset - normalized_requested / 2.0,
            )
        elif command_suffix == "VOLT:OFFS":
            amplitude = config.high_level_v - config.low_level_v
            normalized_updated = replace(
                config,
                high_level_v=normalized_requested + amplitude / 2.0,
                low_level_v=normalized_requested - amplitude / 2.0,
            )
        elif command_suffix == "VOLT:HIGH":
            normalized_updated = replace(
                config, high_level_v=normalized_requested
            )
        elif command_suffix == "VOLT:LOW":
            normalized_updated = replace(
                config, low_level_v=normalized_requested
            )
        else:
            raise SafetyViolation(
                f"Unsupported Rigol voltage control {command_suffix!r}."
            )
        updated = normalized_updated
        self._validate_waveform_config(updated)
        self._assert_independent_channels()
        session = self._require_session()
        prefix = f":SOUR{channel}"
        output_before = self._parse_output_state(
            session.query(f":OUTP{channel}?"), channel=channel
        )
        try:
            self._verify_applied_configuration(config, expected_output=output_before)
        except Exception as exc:
            self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
        if output_before:
            self._validate_active_quick_update(channel, updated)
        self._last_config.pop(channel, None)
        try:
            session.write(
                f"{prefix}:{command_suffix} {self._format_wire_number(normalized_requested)}"
            )
            self._check_errors()
            actual_amplitude = float(session.query(f"{prefix}:VOLT?"))
            actual_offset = float(session.query(f"{prefix}:VOLT:OFFS?"))
            actual_high = float(session.query(f"{prefix}:VOLT:HIGH?"))
            actual_low = float(session.query(f"{prefix}:VOLT:LOW?"))
            output_after = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            expected_amplitude = updated.high_level_v - updated.low_level_v
            expected_offset = (updated.high_level_v + updated.low_level_v) / 2.0
            values_match = all(
                self._same_number(actual, expected, absolute=1e-4)
                for actual, expected in (
                    (actual_amplitude, expected_amplitude),
                    (actual_offset, expected_offset),
                    (actual_high, updated.high_level_v),
                    (actual_low, updated.low_level_v),
                )
            )
            internally_consistent = self._same_number(
                actual_amplitude, actual_high - actual_low, absolute=1e-4
            ) and self._same_number(
                actual_offset, (actual_high + actual_low) / 2.0, absolute=1e-4
            )
            if output_after != output_before:
                raise DeviceError("Rigol OUTPUT state changed during a voltage update.")
            if not values_match or not internally_consistent:
                raise DeviceError(
                    "Rigol voltage readback was clamped or its amplitude/offset and "
                    "HighL/LowL representations disagree."
                )
        except Exception as exc:
            self._fail_live_setpoint_update(output_was_on=output_before, cause=exc)
        self._last_config[channel] = replace(
            updated,
            high_level_v=actual_high,
            low_level_v=actual_low,
        )
        self._output_states[channel] = output_after
        self._update_aggregate_output_state()
        if command_suffix == "VOLT":
            return actual_amplitude
        if command_suffix == "VOLT:OFFS":
            return actual_offset
        return actual_high if command_suffix == "VOLT:HIGH" else actual_low

    def _fail_live_setpoint_update(
        self, *, output_was_on: bool, cause: Exception
    ) -> NoReturn:
        """Fail closed after a potentially partial update of an energised channel."""

        if output_was_on:
            self.emergency_off()
            if self._state != DeviceState.OUTPUT_OFF:
                raise DeviceError(
                    "Rigol live setpoint update failed and emergency shutdown "
                    "could not confirm both outputs OFF."
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
            self._verify_advanced_configuration(
                channel,
                "active modulation",
                self._modulation_expected_fields(modulation),
                require_output_off=False,
            )

    def last_channel_config(self, channel: int) -> RigolChannelConfig:
        """Return the last configuration confirmed by instrument readback."""

        try:
            return self._last_config[channel]
        except KeyError as exc:
            raise SafetyViolation(
                "Configure and validate the Rigol channel before changing its level."
            ) from exc

    def assert_output_state(self, channel: int, *, expected_enabled: bool) -> bool:
        """Confirm the selected output and its complete carrier state."""

        if channel not in {1, 2}:
            raise SafetyViolation("Rigol channel number must be 1 or 2.")
        cached = dict(self._output_states)
        try:
            observed_states = self._read_output_states()
        except Exception:
            self.emergency_off()
            raise
        if observed_states != cached:
            self.emergency_off()
            raise DeviceError(
                "Rigol OUTPUT changed outside the configured control path; "
                "both outputs were forced OFF."
            )
        config = self.last_channel_config(channel)
        try:
            self._verify_applied_configuration(
                config, expected_output=expected_enabled
            )
            if expected_enabled:
                output_config = self._last_output_config.get(channel)
                if output_config is not None:
                    self._verify_output_configuration(output_config)
        except Exception:
            if any(observed_states.values()):
                self.emergency_off()
            raise
        actual = observed_states[channel]
        if actual != expected_enabled:
            if actual:
                self.emergency_off()
            raise DeviceError(
                f"Rigol CH{channel} OUTPUT is {'ON' if actual else 'OFF'}; "
                f"expected {'ON' if expected_enabled else 'OFF'}."
            )
        self._update_aggregate_output_state()
        return actual

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

    @staticmethod
    def _quantize_channel_config(config: RigolChannelConfig) -> RigolChannelConfig:
        return replace(
            config,
            frequency_hz=quantize_rigol_frequency(config.frequency_hz),
            high_level_v=quantize_rigol_voltage(config.high_level_v),
            low_level_v=quantize_rigol_voltage(config.low_level_v),
        )

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
        )

    def _verify_applied_configuration(
        self,
        expected: RigolChannelConfig,
        *,
        expected_output: bool = False,
    ) -> RigolChannelConfig:
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
                actual_dc_level
                if is_dc
                else float(session.query(f"{prefix}:VOLT:HIGH?"))
            )
            actual_low = (
                actual_dc_level
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
            actual_arb_mode = (
                session.query(f"{prefix}:FUNC:ARB:MODE?").strip().upper()
                if expected.waveform.upper() == "USER"
                else None
            )
        except (TypeError, ValueError) as exc:
            raise DeviceError("Rigol returned an invalid configuration readback.") from exc
        mismatches: list[str] = []
        if actual_waveform != expected.waveform.upper():
            mismatches.append(f"FUNC {actual_waveform} ≠ {expected.waveform.upper()}")
        if actual_arb_mode is not None and actual_arb_mode not in {"FREQ", "FERQ"}:
            mismatches.append(f"ARB:MODE {actual_arb_mode} != FREQ")
        if expected.waveform.upper() not in {"DC", "NOIS"} and not self._same_frequency_readback(
            actual_frequency, expected.frequency_hz
        ):
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
        if actual_output != expected_output:
            mismatches.append(
                "OUTPUT "
                f"{'ON' if actual_output else 'OFF'} != expected "
                f"{'ON' if expected_output else 'OFF'}"
            )
        if mismatches:
            description = (
                "Rigol frequency readback differs from the validated state: "
                if any(item.startswith("FREQ ") for item in mismatches)
                else "Rigol configuration readback differs from the validated state: "
            )
            raise DeviceError(
                description + "; ".join(mismatches)
            )
        return replace(
            expected,
            frequency_hz=actual_frequency,
            high_level_v=actual_high,
            low_level_v=actual_low,
            phase_deg=actual_phase,
        )

    @staticmethod
    def _same_number(actual: float, expected: float, *, absolute: float) -> bool:
        return abs(actual - expected) <= max(absolute, abs(expected) * 1e-8)

    @staticmethod
    def _same_frequency_readback(actual: float, expected: float) -> bool:
        """Allow decimal query formatting without hiding a frequency clamp."""

        if not math.isfinite(actual) or not math.isfinite(expected):
            return False
        magnitude = abs(expected)
        if magnitude == 0:
            formatted_step = 10.0**-11
        else:
            # Simulators and some firmware revisions return about 12
            # significant digits even though the programming resolution is
            # 1 uHz. Accept that display-format rounding, but no larger clamp.
            formatted_step = 10.0 ** (math.floor(math.log10(magnitude)) - 11)
        float_rounding_margin = 4.0 * max(math.ulp(actual), math.ulp(expected))
        tolerance = max(
            RIGOL_DG1000Z_FREQUENCY_RESOLUTION_HZ / 2.0,
            formatted_step / 2.0 + float_rounding_margin,
        )
        return abs(actual - expected) <= tolerance

    @staticmethod
    def _format_wire_number(value: float) -> str:
        """Keep documented sub-microvolt/sub-microhertz digits on the wire."""

        return f"{value:.16g}"

    @staticmethod
    def _assert_finite(name: str, value: float) -> None:
        if not math.isfinite(value):
            raise SafetyViolation(f"Rigol {name} must be finite.")

    @staticmethod
    def _format_load(value: str | float) -> str:
        if isinstance(value, str) and value.strip().upper() in {"HIGHZ", "INF", "INFINITY"}:
            return "INF"
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise SafetyViolation(
                "Rigol output load must be a numeric resistance or HIGHZ."
            ) from exc
        if not math.isfinite(parsed) or not 1 <= parsed <= 10_000:
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

    def _validate_shape_parameters(self, config: RigolChannelConfig) -> None:
        """Validate shape-dependent values before the first configuration write."""

        waveform = config.waveform.upper()
        if waveform == "SQU" and config.square_duty_percent is not None:
            self._assert_finite("duty cycle", config.square_duty_percent)
            if not 0 < config.square_duty_percent < 100:
                raise SafetyViolation("Duty cycle must be in the range (0, 100)%.")
        if waveform == "RAMP" and config.ramp_symmetry_percent is not None:
            self._assert_finite("ramp symmetry", config.ramp_symmetry_percent)
            if not 0 <= config.ramp_symmetry_percent <= 100:
                raise SafetyViolation("Ramp symmetry must be in the range 0..100%.")
        if waveform != "PULS":
            return
        width = config.pulse_width_s
        leading = config.pulse_leading_s
        trailing = config.pulse_trailing_s
        for name, value in (
            ("pulse width", width),
            ("pulse leading edge", leading),
            ("pulse trailing edge", trailing),
        ):
            if value is not None:
                self._assert_finite(name, value)
                if value <= 0:
                    raise SafetyViolation(f"Rigol {name} must be positive.")
        if width is not None:
            if width < 16e-9:
                raise SafetyViolation("Rigol pulse width cannot be below 16 ns.")
            period_s = 1.0 / config.frequency_hz
            maximum_width_s = period_s - 2 * 16e-9
            if width >= maximum_width_s:
                raise SafetyViolation(
                    "Rigol pulse width must be below period - 32 ns, as required "
                    "by the documented two-minimum-width timing margin."
                )
        for name, edge in (("leading", leading), ("trailing", trailing)):
            if edge is not None and edge < 10e-9:
                raise SafetyViolation(f"Rigol pulse {name} edge cannot be below 10 ns.")
            if edge is not None and width is not None and edge > 0.625 * width:
                raise SafetyViolation(
                    f"Rigol pulse {name} edge cannot exceed 0.625 × pulse width."
                )

    def set_output(self, channel: int, enabled: bool) -> bool:
        channel_settings = self._channel_settings(channel)
        session = self._require_session()
        try:
            if enabled:
                # Put the channel in a known de-energised state before any
                # preflight query.  Every later failure enters the common
                # two-channel shutdown fallback below.
                session.write(f":OUTP{channel} OFF")
                self._verify_output_off(channel)
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
                    raise SafetyViolation(
                        "Configure and validate the Rigol channel before OUTPUT ON."
                    )
                output_config = self._last_output_config.get(channel)
                if output_config is None:
                    # Direct API/recipe callers may omit the optional output
                    # path action.  Establish deterministic, conservative
                    # defaults rather than inheriting unknown front-panel
                    # polarity/gating/SYNC state.
                    output_config = RigolOutputConfig(
                        channel=channel,
                        output_load=config.output_load,
                    )
                    self.configure_output(output_config)
                self._verify_output_configuration(output_config)
                self._validate_waveform_config(config)
                if channel in self._modulation_enabled:
                    modulation = self._last_modulation_config.get(channel)
                    if modulation is None:
                        raise SafetyViolation(
                            "Rigol modulation state is missing its validated envelope."
                        )
                    self._validate_modulation_envelope(config, modulation)
                    self._verify_advanced_configuration(
                        channel,
                        "modulation before OUTPUT ON",
                        self._modulation_expected_fields(modulation),
                    )
                if channel in self._sweep_enabled:
                    sweep = self._last_sweep_config.get(channel)
                    if sweep is None:
                        raise SafetyViolation(
                            "Rigol sweep is active without a locally validated configuration."
                        )
                    self._validate_sweep_envelope(config, sweep)
                    self._verify_advanced_configuration(
                        channel,
                        "frequency sweep before OUTPUT ON",
                        self._sweep_expected_fields(sweep),
                    )
                if channel in self._burst_enabled:
                    burst = self._last_burst_config.get(channel)
                    if burst is None:
                        raise SafetyViolation(
                            "Rigol burst is active without a locally validated configuration."
                        )
                    self._validate_burst_envelope(config, burst)
                    self._verify_advanced_configuration(
                        channel,
                        "burst before OUTPUT ON",
                        self._burst_expected_fields(burst),
                    )
                # Re-read the complete carrier immediately before the one
                # energising transition; front-panel changes cannot reuse a
                # stale cache entry.
                self._verify_applied_configuration(config)
            session.write(f":OUTP{channel} {'ON' if enabled else 'OFF'}")
            self._check_errors()
            active = self._parse_output_state(
                session.query(f":OUTP{channel}?"), channel=channel
            )
            if active != enabled:
                raise DeviceError("Rigol did not confirm the requested output state.")
        except Exception as exc:
            # Either mutation may have reached the instrument before the
            # transport/error/readback failure.  OFF is idempotent, so always
            # fall back to the complete two-channel shutdown sequence.
            self.emergency_off()
            if self._state != DeviceState.OUTPUT_OFF:
                operation = "ON" if enabled else "OFF"
                raise DeviceError(
                    f"Rigol OUTPUT {operation} failed and emergency shutdown "
                    "could not confirm both outputs OFF."
                ) from exc
            if not enabled:
                reason = str(exc).strip() or type(exc).__name__
                raise DeviceError(
                    "Rigol channel OUTPUT OFF transaction failed, but the "
                    "two-channel fallback confirmed both outputs OFF: " + reason
                ) from exc
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
            "waveform sum": "waveform_sum",
        }
        query_for = {
            "modulation": f"{source}:MOD?",
            "sweep": f"{source}:SWE:STAT?",
            "burst": f"{source}:BURS:STAT?",
            "harmonics": f"{source}:HARM?",
            "waveform sum": f"{source}:SUM?",
        }
        states: dict[str, bool] = {}
        for name, _feature in feature_for.items():
            try:
                states[name] = self._parse_on_off(
                    session.query(query_for[name]), field=f"{name} state"
                )
            except Exception as exc:
                self._state = DeviceState.UNKNOWN
                raise DeviceError(
                    f"Rigol could not confirm {name} state before OUTPUT ON."
                ) from exc
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
        if states["waveform sum"]:
            raise SafetyViolation(
                "Rigol waveform summing is active, but its combined output envelope "
                "has not been validated. Disable SUM on the instrument before OUTPUT ON."
            )
        if len(active_names) > 1:
            raise SafetyViolation(
                "Rigol reports conflicting advanced modes active: " + ", ".join(active_names) + "."
            )

    def _assert_independent_channels(self) -> None:
        """Prevent one channel edit from silently changing the other channel."""

        session = self._require_session()
        try:
            coupling = session.query(":COUP?").strip().upper()
        except Exception as exc:
            self._state = DeviceState.UNKNOWN
            raise DeviceError(
                "Rigol could not confirm channel coupling state."
            ) from exc
        parts = [part.strip() for part in coupling.split(",")]
        names = [part.split(":", 1)[0] if ":" in part else "" for part in parts]
        if len(parts) != 3 or names != ["FREQ", "PHASE", "AMPL"]:
            self._state = DeviceState.UNKNOWN
            raise DeviceError(f"Rigol returned invalid coupling state: {coupling!r}.")
        coupling_states = [
            self._parse_on_off(
                part.split(":", 1)[1], field=f"coupling {part.split(':', 1)[0]} state"
            )
            for part in parts
        ]
        if any(coupling_states):
            raise SafetyViolation(
                "Rigol channel coupling is active on the instrument. Disable frequency, phase, and amplitude coupling before using independent channel controls."
            )
        try:
            tracking = session.query(":SOUR1:TRACK?").strip().upper()
        except Exception as exc:
            self._state = DeviceState.UNKNOWN
            raise DeviceError(
                "Rigol could not confirm channel tracking state."
            ) from exc
        if tracking not in {"0", "OFF", "1", "ON", "INV", "INVERTED"}:
            self._state = DeviceState.UNKNOWN
            raise DeviceError(f"Rigol returned invalid tracking state: {tracking!r}.")
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
        self._last_output_config.pop(config.channel, None)
        self._state = DeviceState.UNKNOWN
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

        if not config.enabled:
            self._modulation_enabled.discard(config.channel)
            self._last_modulation_config.pop(config.channel, None)
            self._disable_advanced_mode(
                channel=config.channel,
                command="MOD OFF",
                readback_field="MOD",
                operation="modulation",
            )
            return
        self._assert_feature("modulation")
        if config.enabled:
            self._assert_advanced_mode_exclusive(config.channel, "modulation")
        channel = self._channel_settings(config.channel)
        if not channel.enabled:
            raise SafetyViolation(f"Rigol CH{config.channel} is disabled in the station profile.")
        if config.modulation_type not in {"AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"}:
            raise SafetyViolation("Rigol modulation type is not allowed.")
        if config.source not in {"INT", "EXT"}:
            raise SafetyViolation("Rigol modulation source must be INT or EXT.")
        if config.source == "INT" and config.internal_shape not in {
            "SIN", "SQU", "TRI", "RAMP", "NRAMP", "NOIS", "USER", "ARB"
        }:
            raise SafetyViolation("Rigol internal modulation waveform is not allowed.")
        if config.polarity not in {"POS", "NEG"}:
            raise SafetyViolation("Rigol modulation polarity must be POS or NEG.")
        if config.source == "INT":
            self._assert_finite("modulation rate", config.rate_hz)
        self._assert_finite("modulation parameter", config.parameter)
        if config.source == "INT" and not 2e-3 <= config.rate_hz <= 1e6:
            raise SafetyViolation(
                "Rigol internal modulation rate must be within the documented "
                "hardware range 2 mHz..1 MHz."
            )
        rate_limits = channel.lab_limits.modulation_rate
        if config.source == "INT" and rate_limits.enabled:
            minimum = parse_quantity(rate_limits.min, "frequency").si_value
            maximum = parse_quantity(rate_limits.max, "frequency").si_value
            if not minimum <= config.rate_hz <= maximum:
                raise SafetyViolation("Modulation rate is outside the configured Rigol frequency range.")
        if config.parameter < 0:
            raise SafetyViolation("The modulation parameter cannot be negative.")
        if config.modulation_type == "AM" and config.parameter > 120:
            raise SafetyViolation("Rigol AM depth must be in the range 0..120%.")
        if config.modulation_type == "PWM" and config.parameter > 100:
            raise SafetyViolation("Rigol PWM deviation must be in the range 0..100%.")
        if config.modulation_type in {"PM", "PSK"} and config.parameter > 360:
            raise SafetyViolation(f"Rigol {config.modulation_type} phase must be in the range 0..360 degrees.")
        self._validate_modulation_envelope(
            self._last_config.get(config.channel), config
        )
        session = self._require_session()
        source = f":SOUR{config.channel}"
        kind = config.modulation_type
        internal_shape = "USER" if config.internal_shape == "ARB" else config.internal_shape
        # Once the first command is sent, the previous advanced-mode snapshot
        # is no longer evidence of the hardware state.  Restore the cache only
        # after every field and OUTPUT-OFF state has been read back.
        self._modulation_enabled.discard(config.channel)
        self._last_modulation_config.pop(config.channel, None)
        self._state = DeviceState.UNKNOWN
        session.write(f":OUTP{config.channel} OFF")
        self._verify_output_off(config.channel)
        session.write(f"{source}:MOD OFF")
        session.write(f"{source}:MOD:TYPE {kind}")
        session.write(f"{source}:{kind}:SOUR {config.source}")
        if kind == "PWM":
            session.write(f"{source}:PULS:HOLD DUTY")
        if config.source == "INT":
            if kind in {"AM", "FM", "PM", "PWM"}:
                session.write(f"{source}:{kind}:INT:FREQ {config.rate_hz:.12g}")
                session.write(f"{source}:{kind}:INT:FUNC {internal_shape}")
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
        self._verify_advanced_configuration(
            config.channel, "modulation", self._modulation_expected_fields(config)
        )
        self._modulation_enabled.add(config.channel)
        self._last_modulation_config[config.channel] = config

    @staticmethod
    def _modulation_expected_fields(
        config: RigolModulationConfig,
    ) -> dict[str, str | float | int | bool]:
        kind = config.modulation_type
        suffix = {
            "AM": "DEPT",
            "FM": "DEV",
            "PM": "DEV",
            "ASK": "AMPL",
            "FSK": "FREQ",
            "PSK": "PHAS",
            "PWM": "DEV:DCYC",
        }[kind]
        fields: dict[str, str | float | int | bool] = {
            "MOD": config.enabled,
            "MOD:TYPE": kind,
            f"{kind}:SOUR": config.source,
            f"{kind}:{suffix}": config.parameter,
        }
        if kind == "PWM":
            fields["PULS:HOLD"] = "DUTY"
        if config.source == "INT":
            shape = "USER" if config.internal_shape == "ARB" else config.internal_shape
            if kind in {"AM", "FM", "PM", "PWM"}:
                fields[f"{kind}:INT:FREQ"] = config.rate_hz
                fields[f"{kind}:INT:FUNC"] = shape
            else:
                fields[f"{kind}:INT:RATE"] = config.rate_hz
        if kind in {"ASK", "FSK", "PSK"}:
            fields[f"{kind}:POL"] = config.polarity
        return fields

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
        if kind != "PWM" and waveform not in {"SIN", "SQU", "RAMP", "USER", "ARB"}:
            raise SafetyViolation(
                f"Rigol {kind} supports SIN, SQU, RAMP, or arbitrary carriers; "
                f"{waveform} is not a documented carrier."
            )
        parameter = float(modulation.parameter)
        if kind == "PWM":
            if carrier.pulse_width_s is None:
                raise SafetyViolation(
                    "Rigol PWM duty deviation requires an explicitly configured pulse width."
                )
            duty_percent = carrier.pulse_width_s * carrier.frequency_hz * 100.0
            maximum_deviation = min(duty_percent, 100.0 - duty_percent)
            if parameter > maximum_deviation:
                raise SafetyViolation(
                    "Rigol PWM duty deviation exceeds the configured pulse duty-cycle envelope."
                )
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

        if not config.enabled:
            self._sweep_enabled.discard(config.channel)
            self._last_sweep_config.pop(config.channel, None)
            self._disable_advanced_mode(
                channel=config.channel,
                command="SWE:STAT OFF",
                readback_field="SWE:STAT",
                operation="frequency sweep",
            )
            return
        self._assert_feature("frequency_sweep")
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
        trigger_output = self._trigger_output_token(config.trigger_output)
        if isinstance(config.steps, bool) or not isinstance(config.steps, int):
            raise SafetyViolation("Rigol sweep step count must be an integer.")
        if config.start_hz <= 0 or config.stop_hz <= 0 or config.start_hz == config.stop_hz:
            raise SafetyViolation("Sweep requires positive, different start and stop frequencies.")
        if not 1e-3 <= config.duration_s <= 500:
            raise SafetyViolation(
                "Rigol sweep time must be within the documented hardware range 1 ms..500 s."
            )
        hold_times = (config.start_hold_s, config.stop_hold_s, config.return_time_s)
        if min(hold_times) < 0 or max(hold_times) > 500:
            raise SafetyViolation(
                "Rigol sweep hold and return times must be within 0..500 s."
            )
        if not 2 <= config.steps <= 1024:
            raise SafetyViolation(
                "Rigol sweep step count must be within the documented hardware range 2..1024."
            )
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
        carrier = self._last_config.get(config.channel)
        if carrier is None:
            raise SafetyViolation("Configure the Rigol carrier before enabling sweep.")
        self._validate_sweep_envelope(carrier, config)
        session = self._require_session()
        source = f":SOUR{config.channel}"
        # Do not retain a previously verified sweep after a partially applied
        # transaction.  It is re-established only after complete readback.
        self._sweep_enabled.discard(config.channel)
        self._last_sweep_config.pop(config.channel, None)
        self._state = DeviceState.UNKNOWN
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
            f"{source}:SWE:TRIG:TRIGO {trigger_output}",
            f"{source}:SWE:STAT {'ON' if config.enabled else 'OFF'}",
        ):
            session.write(command)
        self._check_errors()
        self._verify_output_off(config.channel)
        self._verify_advanced_configuration(
            config.channel,
            "frequency sweep",
            self._sweep_expected_fields(config),
        )
        self._sweep_enabled.add(config.channel)
        self._last_sweep_config[config.channel] = config

    @staticmethod
    def _sweep_expected_fields(
        config: RigolFrequencySweepConfig,
    ) -> dict[str, str | float | int | bool]:
        return {
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
            "SWE:TRIG:TRIGO": RigolAdapter._trigger_output_token(
                config.trigger_output
            ),
            "SWE:STAT": config.enabled,
        }

    def _validate_sweep_envelope(
        self,
        carrier: RigolChannelConfig,
        sweep: RigolFrequencySweepConfig,
    ) -> None:
        waveform = carrier.waveform.upper()
        if waveform not in {"SIN", "SQU", "RAMP", "USER", "ARB"}:
            raise SafetyViolation(
                "Rigol sweep supports SIN, SQU, RAMP, or arbitrary carriers; "
                f"{waveform} is not a documented carrier."
            )
        for frequency_hz in (sweep.start_hz, sweep.stop_hz):
            self._validate_waveform_config(
                replace(carrier, frequency_hz=frequency_hz)
            )

    def trigger_frequency_sweep(self, channel: int) -> None:
        self._assert_feature("frequency_sweep")
        self._channel_settings(channel)
        session = self._require_session()
        source = f":SOUR{channel}:SWE"
        if not self._parse_on_off(
            session.query(f"{source}:STAT?"), field="frequency sweep state"
        ):
            raise SafetyViolation(
                "Rigol frequency sweep must be enabled before a manual trigger."
            )
        trigger_source = session.query(f"{source}:TRIG:SOUR?").strip().upper()
        if trigger_source != "MAN":
            raise SafetyViolation(
                "Rigol frequency sweep trigger source must be MAN before a manual trigger."
            )
        if not self._parse_output_state(
            session.query(f":OUTP{channel}?"), channel=channel
        ):
            raise SafetyViolation(
                "Rigol frequency sweep manual trigger requires the channel OUTPUT to be ON."
            )
        session.write(f"{source}:TRIG")
        self._check_errors()

    def configure_burst(self, config: RigolBurstConfig) -> None:
        """Configure burst/gate parameters while the carrier output is OFF."""

        if not config.enabled:
            self._burst_enabled.discard(config.channel)
            self._last_burst_config.pop(config.channel, None)
            self._disable_advanced_mode(
                channel=config.channel,
                command="BURS OFF",
                readback_field="BURS:STAT",
                operation="burst",
            )
            return
        self._assert_feature("burst")
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
        if config.mode not in {"TRIG", "INF", "GAT"}:
            raise SafetyViolation("Rigol burst mode must be TRIG, INF, or GAT.")
        if config.trigger_source not in {"INT", "EXT", "MAN"} or config.trigger_slope not in {"POS", "NEG"}:
            raise SafetyViolation("Rigol burst trigger contains an unsupported value.")
        trigger_output = self._trigger_output_token(config.trigger_output)
        if config.gate_polarity not in {"NORM", "INV"} or config.idle not in {
            "FPT", "TOP", "CENTER", "BOTTOM", "CENT", "BOT"
        }:
            raise SafetyViolation("Rigol burst gate/idle parameters are invalid.")
        if isinstance(config.cycles, bool) or not isinstance(config.cycles, int):
            raise SafetyViolation("Rigol burst cycle count must be an integer.")
        maximum_cycles = 500_000 if config.trigger_source == "INT" else 1_000_000
        if config.mode == "TRIG" and not 1 <= config.cycles <= maximum_cycles:
            raise SafetyViolation(
                "Rigol burst cycle count must be within 1.."
                f"{maximum_cycles} for the selected trigger source."
            )
        if not 2.0166e-6 <= config.period_s <= 500:
            raise SafetyViolation(
                "Rigol internal burst period must be within 2.0166 us..500 s."
            )
        if not 0 <= config.delay_s <= 100:
            raise SafetyViolation("Rigol burst trigger delay must be within 0..100 s.")
        if not 0 <= config.phase_deg <= 360:
            raise SafetyViolation("Rigol burst phase must be within 0..360 degrees.")
        if config.mode == "GAT" and config.trigger_source != "EXT":
            raise SafetyViolation(
                "Rigol gated burst requires the documented EXT trigger source."
            )
        if config.mode == "INF" and config.trigger_source not in {"EXT", "MAN"}:
            raise SafetyViolation(
                "Rigol infinite burst requires the documented EXT or MAN trigger source."
            )
        limits = channel.lab_limits
        if limits.burst_period.enabled:
            period_min = parse_quantity(limits.burst_period.min, "time").si_value
            period_max = parse_quantity(limits.burst_period.max, "time").si_value
            if not period_min <= config.period_s <= period_max:
                raise SafetyViolation("Burst period is outside the configured range.")
        if config.mode == "TRIG" and limits.burst_cycles.enabled and not limits.burst_cycles.min <= config.cycles <= limits.burst_cycles.max:
            raise SafetyViolation("Burst cycle count is outside the configured range.")
        carrier = self._last_config.get(config.channel)
        if carrier is None:
            raise SafetyViolation("Configure the Rigol carrier before enabling burst.")
        self._validate_burst_envelope(carrier, config)
        session = self._require_session()
        source = f":SOUR{config.channel}"
        idle_token = {"CENT": "CENTER", "BOT": "BOTTOM"}.get(
            config.idle, config.idle
        )
        # A failed mutation leaves the hardware burst state uncertain, so the
        # old snapshot must not be reused by a later OUTPUT-ON validation.
        self._burst_enabled.discard(config.channel)
        self._last_burst_config.pop(config.channel, None)
        self._state = DeviceState.UNKNOWN
        session.write(f":OUTP{config.channel} OFF")
        self._verify_output_off(config.channel)
        session.write(f"{source}:BURS OFF")
        commands = [
            f"{source}:BURS:MODE {config.mode}",
            f"{source}:BURS:PHAS {config.phase_deg:.12g}",
            f"{source}:BURS:INT:PER {config.period_s:.12g}",
            f"{source}:BURS:TDEL {config.delay_s:.12g}",
            f"{source}:BURS:TRIG:SOUR {config.trigger_source}",
            f"{source}:BURS:TRIG:SLOP {config.trigger_slope}",
            f"{source}:BURS:TRIG:TRIGO {trigger_output}",
            f"{source}:BURS:GATE:POL {config.gate_polarity}",
            f"{source}:BURS:IDLE {idle_token}",
            f"{source}:BURS {'ON' if config.enabled else 'OFF'}",
        ]
        if config.mode == "TRIG":
            commands.insert(1, f"{source}:BURS:NCYC {config.cycles}")
        for command in commands:
            session.write(command)
        self._check_errors()
        self._verify_output_off(config.channel)
        self._verify_advanced_configuration(
            config.channel,
            "burst",
            self._burst_expected_fields(config),
        )
        self._burst_enabled.add(config.channel)
        self._last_burst_config[config.channel] = config

    @staticmethod
    def _burst_expected_fields(
        config: RigolBurstConfig,
    ) -> dict[str, str | float | int | bool]:
        idle = {"CENT": "CENTER", "BOT": "BOTTOM"}.get(config.idle, config.idle)
        fields: dict[str, str | float | int | bool] = {
            "BURS:MODE": config.mode,
            "BURS:PHAS": config.phase_deg,
            "BURS:INT:PER": config.period_s,
            "BURS:TDEL": config.delay_s,
            "BURS:TRIG:SOUR": config.trigger_source,
            "BURS:TRIG:SLOP": config.trigger_slope,
            "BURS:TRIG:TRIGO": RigolAdapter._trigger_output_token(
                config.trigger_output
            ),
            "BURS:GATE:POL": config.gate_polarity,
            "BURS:IDLE": idle,
            "BURS:STAT": config.enabled,
        }
        if config.mode == "TRIG":
            fields["BURS:NCYC"] = config.cycles
        return fields

    def _validate_burst_envelope(
        self,
        carrier: RigolChannelConfig,
        _burst: RigolBurstConfig,
    ) -> None:
        waveform = carrier.waveform.upper()
        if waveform == "DC" or (waveform == "NOIS" and _burst.mode != "GAT"):
            raise SafetyViolation(
                "Rigol burst supports NOIS only in gated mode and never supports DC."
            )
        self._validate_waveform_config(carrier)

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
        self._state = DeviceState.UNKNOWN
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
            "harmonics": f"{source}:HARM?",
            "waveform sum": f"{source}:SUM?",
        }
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
        session = self._require_session()
        source = f":SOUR{channel}:BURS"
        if not self._parse_on_off(
            session.query(f"{source}:STAT?"), field="burst state"
        ):
            raise SafetyViolation("Rigol burst must be enabled before a manual trigger.")
        mode = session.query(f"{source}:MODE?").strip().upper()
        if mode not in {"TRIG", "INF"}:
            raise SafetyViolation(
                "Rigol manual burst trigger is available only in N-cycle or infinite mode."
            )
        trigger_source = session.query(f"{source}:TRIG:SOUR?").strip().upper()
        if trigger_source != "MAN":
            raise SafetyViolation(
                "Rigol burst trigger source must be MAN before a manual trigger."
            )
        if not self._parse_output_state(
            session.query(f":OUTP{channel}?"), channel=channel
        ):
            raise SafetyViolation(
                "Rigol burst manual trigger requires the channel OUTPUT to be ON."
            )
        session.write(f"{source}:TRIG")
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
        cached_ch2_output = self._last_output_config.get(2)
        counter_enables_input = config.state != "OFF"
        if counter_enables_input:
            # Enabling the rear-panel counter disables CH2 SYNC on DG1000Z.
            # Invalidate that snapshot before the first mutation and rebuild
            # it from an explicit readback after the counter transaction.
            self._last_output_config.pop(2, None)
        session.write(f":COUN:COUP {config.coupling}")
        if config.gate_time == "AUTO":
            session.write(":COUN:AUTO")
        else:
            session.write(f":COUN:GATE {config.gate_time}")
        session.write(f":COUN:HF {'ON' if config.high_frequency_rejection else 'OFF'}")
        session.write(f":COUN:LEVE {config.trigger_level_v:.12g}")
        session.write(f":COUN:SENS {config.sensitivity_percent:.12g}")
        if counter_enables_input:
            session.write(":COUN ON")
        if config.state in {"RUN", "STOP", "SINGLE"}:
            session.write(f":COUN {config.state}")
        elif config.state == "OFF":
            session.write(":COUN OFF")
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
        state_response = {"1": "ON", "0": "OFF"}.get(
            state_response, state_response
        )
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
        if counter_enables_input:
            sync_response = session.query(":OUTP2:SYNC?").strip().upper()
            sync_enabled = self._parse_on_off(
                sync_response, field="CH2 SYNC state after counter enable"
            )
            if cached_ch2_output is not None:
                self._last_output_config[2] = replace(
                    cached_ch2_output, sync_enabled=sync_enabled
                )

    def read_counter(self) -> RigolCounterReading:
        """Read the five documented counter results without changing output state."""

        self._assert_feature("counter")
        session = self._require_session()
        state = session.query(":COUN?").strip().upper()
        state = {"1": "ON", "0": "OFF"}.get(state, state)
        if state not in {"ON", "RUN", "STOP", "SINGLE"}:
            if state in {"OFF", "0"}:
                raise SafetyViolation(
                    "Enable the Rigol frequency counter before reading it."
                )
            self._state = DeviceState.UNKNOWN
            raise DeviceError(f"Rigol returned invalid counter state: {state!r}.")
        values = session.query(":COUN:MEAS?").strip().split(",")
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
        *,
        require_output_off: bool = True,
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
                expected_response = expected.upper()
                # DG1000Z accepts STEP but documents/returns the abbreviated
                # token STE for the sweep-spacing query.
                if suffix == "SWE:SPAC":
                    response = {"STE": "STEP"}.get(response, response)
                    expected_response = {"STE": "STEP"}.get(
                        expected_response, expected_response
                    )
                if response != expected_response:
                    mismatches.append(
                        f"{suffix} {response} != {expected_response}"
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
        if require_output_off:
            self._verify_output_off(channel)
        if mismatches:
            raise DeviceError(
                f"Rigol {operation} readback failed (output remains OFF): "
                + "; ".join(mismatches)
            )

    def _verify_output_configuration(self, expected: RigolOutputConfig) -> None:
        session = self._require_session()
        prefix = f":OUTP{expected.channel}"
        try:
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
        except Exception as exc:
            # A malformed or incomplete readback cannot serve as evidence for
            # a later OUTPUT ON, even when the failing query itself is
            # read-only.  Normalize transport and conversion failures at the
            # adapter boundary so UI/recipe callers receive an actionable
            # instrument error instead of a raw ValueError/timeout.
            self._state = DeviceState.UNKNOWN
            raise DeviceError(
                "Rigol could not read back the complete output-path "
                f"configuration for CH{expected.channel}."
            ) from exc
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
