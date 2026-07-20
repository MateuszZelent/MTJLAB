"""Safe Anritsu MS2830A spectrum and live-trace adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import time

from app.devices.base import DeviceAdapter, InstrumentSession, SessionFactory, parse_identity, validate_identity
from app.devices.anritsu_ms2830a.hardware import (
    ANRITSU_PREAMPLIFIER_OPTIONS,
    ANRITSU_SIGNAL_GENERATOR_OPTIONS,
    parse_anritsu_option_response,
)
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.safety.anritsu import (
    ANRITSU_SWEEP_POINT_COUNTS,
    assert_anritsu_acquisition_allowed,
    validate_anritsu_advanced_spectrum,
    validate_anritsu_dut_input,
    validate_anritsu_spectrum,
    validate_anritsu_signal_generator,
    validate_anritsu_trace_name,
)
from app.settings.models import AnritsuSettings, StationSettings


@dataclass(frozen=True, slots=True)
class SpectrumConfig:
    start_hz: float
    stop_hz: float
    reference_level_dbm: float
    points: int
    trace: str = "TRAC1"
    dut_max_expected_input_dbm: float | None = None


@dataclass(frozen=True, slots=True)
class AnritsuConfigurationSnapshot:
    """Read-only snapshot of the analyser's current spectrum settings."""

    start_hz: float
    stop_hz: float
    reference_level_dbm: float
    points: int
    instrument_mode: str = ""


@dataclass(frozen=True, slots=True)
class AdvancedSpectrumConfig:
    rbw_auto: bool = True
    rbw_hz: float | None = None
    vbw_mode: str = "auto"
    vbw_hz: float | None = None
    detector: str = "NORM"
    attenuation_auto: bool = True
    attenuation_db: float | None = None
    preamplifier_enabled: bool = False
    sweep_time_auto: bool = True
    sweep_time_s: float | None = None


@dataclass(frozen=True, slots=True)
class AdvancedSpectrumSnapshot:
    rbw_auto: bool
    rbw_hz: float
    vbw_mode: str
    vbw_hz: float | None
    detector: str
    attenuation_auto: bool
    attenuation_db: float
    preamplifier_enabled: bool
    sweep_time_auto: bool
    sweep_time_s: float
    instrument_mode: str


@dataclass(frozen=True, slots=True)
class SignalGeneratorConfig:
    frequency_hz: float
    power_dbm: float


@dataclass(frozen=True, slots=True)
class SignalGeneratorSnapshot:
    frequency_hz: float
    power_dbm: float
    output_enabled: bool
    instrument_mode: str


@dataclass(frozen=True, slots=True)
class SpectrumTrace:
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    acquired_at_utc: datetime
    trace_name: str


@dataclass(frozen=True, slots=True)
class ReferenceSpectrum:
    """A reproducible reference trace with acquisition provenance."""

    trace: SpectrumTrace
    kind: str
    average_count: int
    acquired_at_utc: datetime
    source_device_idn: str = ""
    firmware: str = ""
    hardware_options: tuple[str, ...] = ()
    reference_level_dbm: float | None = None
    advanced_configuration_known: bool = False
    rbw_auto: bool | None = None
    rbw_hz: float | None = None
    vbw_mode: str = ""
    vbw_hz: float | None = None
    detector: str = ""
    attenuation_auto: bool | None = None
    attenuation_db: float | None = None
    preamplifier_enabled: bool | None = None
    sweep_time_auto: bool | None = None
    sweep_time_s: float | None = None
    source_file: str = ""
    notes: str = ""
    saved_to_file: bool = False
    grid_hash: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"single", "averaged", "imported"}:
            raise ValueError(f"Unsupported reference kind {self.kind!r}.")
        if self.average_count < 1:
            raise ValueError("Reference average_count must be positive.")
        if len(self.trace.frequencies_hz) != len(self.trace.powers_dbm) or len(self.trace.frequencies_hz) < 2:
            raise ValueError("Reference trace must contain matching frequency and power arrays.")
        if not all(math.isfinite(value) for value in (*self.trace.frequencies_hz, *self.trace.powers_dbm)):
            raise ValueError("Reference trace contains non-finite values.")
        if any(right <= left for left, right in zip(self.trace.frequencies_hz, self.trace.frequencies_hz[1:])):
            raise ValueError("Reference frequency grid must be strictly increasing.")
        optional_numeric = (
            self.reference_level_dbm,
            self.rbw_hz,
            self.vbw_hz,
            self.attenuation_db,
            self.sweep_time_s,
        )
        if any(value is not None and not math.isfinite(value) for value in optional_numeric):
            raise ValueError("Reference acquisition metadata contains a non-finite value.")
        if self.advanced_configuration_known:
            if self.rbw_auto is None or self.rbw_hz is None or self.rbw_hz <= 0:
                raise ValueError("Known advanced reference metadata requires a valid RBW state.")
            if self.vbw_mode not in {"auto", "manual", "off"}:
                raise ValueError("Known advanced reference metadata requires a valid VBW mode.")
            if self.vbw_mode != "off" and (self.vbw_hz is None or self.vbw_hz <= 0):
                raise ValueError("Known advanced reference metadata requires a valid VBW value.")
            if not self.detector:
                raise ValueError("Known advanced reference metadata requires a detector.")
            if self.attenuation_auto is None or self.attenuation_db is None:
                raise ValueError("Known advanced reference metadata requires attenuation state.")
            if self.attenuation_db < 0:
                raise ValueError("Reference attenuation cannot be negative.")
            if self.preamplifier_enabled is None:
                raise ValueError("Known advanced reference metadata requires preamplifier state.")
            if self.sweep_time_auto is None or self.sweep_time_s is None or self.sweep_time_s <= 0:
                raise ValueError("Known advanced reference metadata requires sweep-time state.")
        expected_hash = self.hash_grid(self.trace.frequencies_hz)
        if self.grid_hash and self.grid_hash != expected_hash:
            raise ValueError("Reference grid hash does not match its frequency data.")
        if not self.grid_hash:
            object.__setattr__(self, "grid_hash", expected_hash)

    @staticmethod
    def hash_grid(frequencies_hz: tuple[float, ...]) -> str:
        payload = ",".join(format(value, ".17g") for value in frequencies_hz).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @property
    def start_hz(self) -> float:
        return self.trace.frequencies_hz[0]

    @property
    def stop_hz(self) -> float:
        return self.trace.frequencies_hz[-1]

    @property
    def points(self) -> int:
        return len(self.trace.frequencies_hz)


class AnritsuAdapter(DeviceAdapter):
    """Spectrum acquisition is explicit; RF generator output is never auto-enabled."""

    def __init__(self, station: StationSettings, *, session_factory: SessionFactory | None = None) -> None:
        super().__init__()
        self._station = station
        self._settings: AnritsuSettings = station.anritsu
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None
        self._live = False
        self._restore_continuous_after_live = False
        self._sg_armed_until = 0.0
        self._last_sg_config: SignalGeneratorConfig | None = None
        self._sg_output_enabled = False

    @staticmethod
    def _read_hardware_options(session: InstrumentSession) -> tuple[str, ...]:
        """Best-effort read of installed options without making connection depend on it."""

        original_timeout = session.timeout
        try:
            # Some old firmware may not implement *OPT?. Do not make an
            # otherwise valid instrument unusable, and do not wait for the
            # normal 30-second acquisition timeout for this optional probe.
            session.timeout = max(1, min(original_timeout, 2000))
            return parse_anritsu_option_response(session.query("*OPT?"))
        except DeviceError:
            return ()
        finally:
            session.timeout = original_timeout

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Anritsu is not connected.")
        return self._session

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            if self._identity is None:
                raise ConnectionError("Anritsu has a session without a verified identity.")
            return self._identity
        if not self._settings.enabled:
            raise SafetyViolation("Anritsu is disabled in the station profile.")
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("No Anritsu VISA resource is configured in settings.yml.")
        timeout = int(parse_quantity(self._settings.connection.timeout, DIMENSION_TIME).si_value * 1000)
        session = self._factory.open(resource, self._settings.connection.visa_backend, timeout)
        try:
            # Empty strings preserve the VISA backend defaults. Assigning an
            # empty terminator can make MS2830A queries time out over GPIB.
            if self._settings.connection.read_termination:
                session.read_termination = self._settings.connection.read_termination
            if self._settings.connection.write_termination:
                session.write_termination = self._settings.connection.write_termination
            identity = parse_identity(resource, session.query("*IDN?"))
            validate_identity(
                identity,
                vendor_contains=self._settings.identity.expected_vendor_contains,
                expected_models=self._settings.identity.expected_models,
                expected_serial=self._settings.identity.expected_serial,
                require_serial_match=self._settings.identity.require_serial_match,
            )
            hardware_options = self._read_hardware_options(session)
            missing_options = set(self._settings.identity.required_options) - set(
                hardware_options
            )
            if missing_options:
                raise ConnectionError(
                    "Anritsu is missing profile-required hardware option(s): "
                    + ", ".join(sorted(missing_options))
                )
            self._session = session
            self._identity = identity
            self._capabilities = DeviceCapabilities(
                device_name="anritsu",
                model=identity.model or "MS2830A",
                firmware=identity.firmware,
                features=frozenset(
                    {"spectrum_trace", "live_trace"}
                    | ({"synchronized_single_sweep"} if self._single_sweep_supported else set())
                    | (
                        {"signal_generator"}
                        if ANRITSU_SIGNAL_GENERATOR_OPTIONS.intersection(hardware_options)
                        else set()
                    )
                ),
                hardware_options=hardware_options,
            )
            if self._capabilities.supports("signal_generator"):
                # A pre-existing front-panel/remote SG state is not trusted.
                # Connection succeeds only after RF OFF has been commanded and
                # read back, then the analyser is returned to Spectrum mode.
                self._enter_spectrum_mode_with_rf_off()
            self._state = DeviceState.VERIFIED
            return identity
        except Exception:
            session.close()
            self._session = None
            self._identity = None
            self._capabilities = None
            self._sg_armed_until = 0.0
            self._last_sg_config = None
            self._sg_output_enabled = False
            self._state = DeviceState.DISCONNECTED
            raise

    def disconnect(self) -> None:
        session = self._session
        if (
            session is not None
            and self._settings.safety.outputs_off_on_disconnect
            and self._capabilities is not None
            and self._capabilities.supports("signal_generator")
        ):
            self.emergency_off()
            if self._state == DeviceState.UNKNOWN:
                raise DeviceError(
                    "Cannot disconnect Anritsu because RF OUTPUT OFF could not be verified. "
                    "Keep the session open and use E-STOP or remove RF power externally."
                )
        session, self._session = self._session, None
        self._live = False
        self._restore_continuous_after_live = False
        self._sg_armed_until = 0.0
        self._last_sg_config = None
        self._sg_output_enabled = False
        if session is not None:
            try:
                session.close()
            finally:
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        """Best-effort RF OFF followed by acquisition abort.

        When the SG option is installed, E-STOP may explicitly change the
        active application because proving the energy source OFF has priority
        over preserving the front-panel mode.
        """

        if self._session is None:
            return
        session = self._session
        errors: list[Exception] = []
        has_generator = bool(
            self._capabilities is not None
            and self._capabilities.supports("signal_generator")
        )
        if has_generator:
            try:
                session.write("INST SG")
                session.write("OUTP 0")
                if self._parse_output_state(session.query("OUTP?")):
                    raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF during E-STOP.")
                self._sg_output_enabled = False
                session.write("INST SPECT")
            except Exception as exc:
                errors.append(exc)
        try:
            session.write("ABORT")
        except Exception as exc:
            errors.append(exc)
        if errors:
            self._live = False
            self._state = DeviceState.UNKNOWN
        else:
            self._live = False
            self._sg_armed_until = 0.0
            self._state = DeviceState.VERIFIED

    def apply_limit_settings(self, station: object) -> None:
        if not isinstance(station, StationSettings):
            raise TypeError("Anritsu limit update requires StationSettings.")
        if (
            self._session is not None
            and self._capabilities is not None
            and self._capabilities.supports("signal_generator")
        ):
            session = self._session
            try:
                session.write("INST SG")
                output_enabled = self._parse_output_state(session.query("OUTP?"))
                session.write("INST SPECT")
            except Exception:
                self.emergency_off()
                raise
            if output_enabled:
                raise SafetyViolation(
                    "Anritsu limits can change without reconnecting only when the RF "
                    "generator output is confirmed OFF."
                )
        self._station = station
        self._settings = station.anritsu
        self._last_sg_config = None
        self._sg_armed_until = 0.0

    def _assert_signal_generator_supported(self) -> None:
        if self._capabilities is None or not self._capabilities.supports("signal_generator"):
            raise SafetyViolation(
                "The connected Anritsu did not report an installed signal-generator option."
            )

    def _enter_spectrum_mode_with_rf_off(self) -> None:
        """Explicitly prove optional SG RF OFF before selecting Spectrum mode."""

        session = self._require_session()
        if self._capabilities is not None and self._capabilities.supports("signal_generator"):
            session.write("INST SG")
            session.write("OUTP 0")
            if self._parse_output_state(session.query("OUTP?")):
                self._state = DeviceState.UNKNOWN
                raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF.")
            self._sg_output_enabled = False
            self._sg_armed_until = 0.0
        session.write("INST SPECT")

    @staticmethod
    def _parse_output_state(response: str) -> bool:
        normalized = response.strip().upper()
        if normalized in {"1", "+1", "ON"}:
            return True
        if normalized in {"0", "+0", "OFF"}:
            return False
        raise DeviceError(f"Anritsu returned invalid SG output state {response!r}.")

    def read_signal_generator_configuration(self) -> SignalGeneratorSnapshot:
        """Read SG state without silently changing the active application."""

        self._assert_signal_generator_supported()
        session = self._require_session()
        mode = session.query("INST?").strip()
        if "SG" not in mode.upper():
            raise DeviceError(
                f"Signal-generator readback requires explicit SG mode; current mode is {mode!r}."
            )
        try:
            frequency_hz = float(session.query("FREQ?"))
            power_dbm = float(session.query("POW?"))
            output_enabled = self._parse_output_state(session.query("OUTP?"))
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned invalid SG configuration data.") from exc
        if not math.isfinite(frequency_hz) or not math.isfinite(power_dbm) or frequency_hz <= 0:
            raise DeviceError("Anritsu returned non-finite or invalid SG configuration data.")
        self._sg_output_enabled = output_enabled
        self._state = DeviceState.OUTPUT_ON if output_enabled else DeviceState.OUTPUT_OFF
        return SignalGeneratorSnapshot(frequency_hz, power_dbm, output_enabled, mode)

    def configure_signal_generator(self, config: SignalGeneratorConfig) -> SignalGeneratorSnapshot:
        """Explicitly enter SG mode, force RF OFF, configure and verify readback."""

        self._assert_signal_generator_supported()
        validate_anritsu_signal_generator(
            self._settings,
            frequency_hz=config.frequency_hz,
            power_dbm=config.power_dbm,
        )
        session = self._require_session()
        session.write("INST SG")
        session.write("OUTP 0")
        if self._parse_output_state(session.query("OUTP?")):
            self._state = DeviceState.UNKNOWN
            raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF before configuration.")
        session.write("UNIT:POW DBM")
        session.write(f"FREQ {config.frequency_hz:.12g}HZ")
        session.write(f"POW {config.power_dbm:.12g}")
        actual = self.read_signal_generator_configuration()
        mismatches: list[str] = []
        if not math.isclose(actual.frequency_hz, config.frequency_hz, rel_tol=1e-9, abs_tol=1.0):
            mismatches.append(
                f"frequency requested={config.frequency_hz:g} Hz actual={actual.frequency_hz:g} Hz"
            )
        if not math.isclose(actual.power_dbm, config.power_dbm, rel_tol=0.0, abs_tol=0.01):
            mismatches.append(
                f"power requested={config.power_dbm:g} dBm actual={actual.power_dbm:g} dBm"
            )
        if actual.output_enabled:
            mismatches.append("RF output is ON")
        if mismatches:
            self._state = DeviceState.UNKNOWN
            raise DeviceError("Anritsu SG configuration readback mismatch: " + "; ".join(mismatches))
        self._last_sg_config = config
        self._sg_armed_until = 0.0
        self._state = DeviceState.OUTPUT_OFF
        return actual

    def arm_signal_generator_output(self, *, ttl_s: float | None = None) -> float:
        """Open a short, one-shot enable window after revalidating the configured setpoint."""

        self._assert_signal_generator_supported()
        if self._station.outputs_locked:
            raise SafetyViolation("Anritsu SG output requires an approved station profile.")
        if not self._settings.safety.signal_generator_output_allowed:
            raise SafetyViolation("Anritsu SG output is disabled in the safety profile.")
        if self._last_sg_config is None:
            raise SafetyViolation("Configure and verify the Anritsu SG while RF is OFF first.")
        validate_anritsu_signal_generator(
            self._settings,
            frequency_hz=self._last_sg_config.frequency_hz,
            power_dbm=self._last_sg_config.power_dbm,
        )
        duration = ttl_s
        if duration is None:
            duration = parse_quantity(
                self._settings.signal_generator.arm_ttl, DIMENSION_TIME
            ).si_value
        if not math.isfinite(duration) or duration <= 0 or duration > 60:
            raise SafetyViolation("Anritsu SG ARM duration must be in the range 0–60 s.")
        snapshot = self.read_signal_generator_configuration()
        if snapshot.output_enabled:
            raise SafetyViolation("Cannot ARM Anritsu SG because RF output is already ON.")
        self._sg_armed_until = time.monotonic() + duration
        return self._sg_armed_until

    def set_signal_generator_output(self, enabled: bool) -> bool:
        """Enable RF only inside the one-shot ARM window; OFF is always available."""

        self._assert_signal_generator_supported()
        session = self._require_session()
        if not enabled:
            session.write("OUTP 0")
            active = self._parse_output_state(session.query("OUTP?"))
            self._sg_armed_until = 0.0
            self._sg_output_enabled = active
            self._state = DeviceState.UNKNOWN if active else DeviceState.OUTPUT_OFF
            if active:
                raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF.")
            return False
        if self._station.outputs_locked or not self._settings.safety.signal_generator_output_allowed:
            raise SafetyViolation("Anritsu SG RF output is locked by the station profile.")
        if self._last_sg_config is None:
            raise SafetyViolation("Configure and verify the Anritsu SG before RF OUTPUT ON.")
        if time.monotonic() > self._sg_armed_until:
            self._sg_armed_until = 0.0
            raise SafetyViolation("Anritsu SG ARM window expired; ARM again.")
        validate_anritsu_signal_generator(
            self._settings,
            frequency_hz=self._last_sg_config.frequency_hz,
            power_dbm=self._last_sg_config.power_dbm,
        )
        self._sg_armed_until = 0.0
        session.write("OUTP 1")
        active = self._parse_output_state(session.query("OUTP?"))
        self._sg_output_enabled = active
        self._state = DeviceState.OUTPUT_ON if active else DeviceState.OUTPUT_OFF
        if not active:
            raise DeviceError("Anritsu SG did not confirm RF OUTPUT ON.")
        return True

    @property
    def _single_sweep_supported(self) -> bool:
        return self._settings.acquisition.single_sweep_mode == "standard_scpi_opc"

    def _assert_acquisition_allowed(self) -> None:
        assert_anritsu_acquisition_allowed(self._settings.safety)

    def read_current_configuration(self) -> AnritsuConfigurationSnapshot:
        """Query current settings without changing the analyser or the safety profile."""

        session = self._require_session()
        try:
            instrument_mode = session.query("INST?").strip()
            start_hz = float(session.query("FREQ:STAR?"))
            stop_hz = float(session.query("FREQ:STOP?"))
            reference_level_dbm = float(session.query("DISP:WIND:TRAC:Y:RLEV?"))
            points = int(float(session.query("SWE:POIN?")))
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned an invalid current-configuration response.") from exc
        if not all(math.isfinite(value) for value in (start_hz, stop_hz, reference_level_dbm)):
            raise DeviceError("Anritsu returned a non-finite current-configuration value.")
        if start_hz <= 0 or stop_hz <= start_hz:
            raise DeviceError("Anritsu returned an invalid current frequency range.")
        if points not in ANRITSU_SWEEP_POINT_COUNTS:
            raise DeviceError(f"Anritsu returned unsupported sweep point count {points}.")
        return AnritsuConfigurationSnapshot(
            start_hz, stop_hz, reference_level_dbm, points, instrument_mode
        )

    @staticmethod
    def _parse_switch(response: str, parameter: str) -> bool:
        normalized = response.strip().upper()
        if normalized in {"1", "+1", "ON"}:
            return True
        if normalized in {"0", "+0", "OFF"}:
            return False
        raise DeviceError(f"Anritsu returned invalid {parameter} state {response!r}.")

    def read_advanced_spectrum_configuration(self) -> AdvancedSpectrumSnapshot:
        """Query advanced Spectrum settings without modifying the instrument."""

        session = self._require_session()
        mode = session.query("INST?").strip()
        if "SPECT" not in mode.upper():
            raise DeviceError(
                f"Advanced Spectrum readback requires Spectrum Analyzer mode; current mode is {mode!r}."
            )
        try:
            rbw_auto = self._parse_switch(session.query("BAND:AUTO?"), "RBW auto")
            rbw_hz = float(session.query("BAND?"))
            vbw_auto = self._parse_switch(session.query("BAND:VID:AUTO?"), "VBW auto")
            vbw_response = session.query("BAND:VID?").strip().upper()
            vbw_hz = None if vbw_response == "OFF" else float(vbw_response)
            detector = session.query("DET?").strip().upper()
            attenuation_auto = self._parse_switch(
                session.query("POW:ATT:AUTO?"), "attenuation auto"
            )
            attenuation_db = float(session.query("POW:ATT?"))
            preamplifier_enabled = (
                self._parse_switch(session.query("POW:GAIN?"), "preamplifier")
                if self._capabilities is not None
                and ANRITSU_PREAMPLIFIER_OPTIONS.intersection(
                    self._capabilities.hardware_options
                )
                else False
            )
            sweep_time_auto = self._parse_switch(
                session.query("SWE:TIME:AUTO?"), "sweep-time auto"
            )
            sweep_time_s = float(session.query("SWE:TIME?"))
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned invalid advanced Spectrum data.") from exc
        numeric = (rbw_hz, attenuation_db, sweep_time_s)
        if vbw_hz is not None:
            numeric += (vbw_hz,)
        if not all(math.isfinite(value) for value in numeric):
            raise DeviceError("Anritsu returned non-finite advanced Spectrum data.")
        detector_aliases = {
            "NORMAL": "NORM",
            "POSITIVE": "POS",
            "SAMPLE": "SAMP",
            "NEGATIVE": "NEG",
            "QPEAK": "QPE",
            "CAVERAGE": "CAV",
        }
        detector = detector_aliases.get(detector, detector)
        vbw_mode = "auto" if vbw_auto else ("off" if vbw_hz is None else "manual")
        return AdvancedSpectrumSnapshot(
            rbw_auto=rbw_auto,
            rbw_hz=rbw_hz,
            vbw_mode=vbw_mode,
            vbw_hz=vbw_hz,
            detector=detector,
            attenuation_auto=attenuation_auto,
            attenuation_db=attenuation_db,
            preamplifier_enabled=preamplifier_enabled,
            sweep_time_auto=sweep_time_auto,
            sweep_time_s=sweep_time_s,
            instrument_mode=mode,
        )

    def _assert_advanced_firmware_qualified(self) -> None:
        protocol = self._settings.advanced_spectrum
        if protocol.control_protocol != "standard_scpi":
            raise SafetyViolation(
                "Anritsu advanced Spectrum Analyzer control is unverified for this firmware."
            )
        firmware = self._identity.firmware.strip() if self._identity is not None else ""
        if firmware not in protocol.qualified_firmware:
            raise SafetyViolation(
                f"Anritsu firmware {firmware or 'unknown'!r} is not in the qualified advanced-control list."
            )

    def configure_advanced_spectrum(
        self, config: AdvancedSpectrumConfig
    ) -> AdvancedSpectrumSnapshot:
        """Apply qualified input-path/bandwidth controls and verify every readback."""

        options = self._capabilities.hardware_options if self._capabilities is not None else ()
        validate_anritsu_advanced_spectrum(
            self._settings,
            rbw_auto=config.rbw_auto,
            rbw_hz=config.rbw_hz,
            vbw_mode=config.vbw_mode,
            vbw_hz=config.vbw_hz,
            detector=config.detector,
            attenuation_auto=config.attenuation_auto,
            attenuation_db=config.attenuation_db,
            preamplifier_enabled=config.preamplifier_enabled,
            sweep_time_auto=config.sweep_time_auto,
            sweep_time_s=config.sweep_time_s,
            hardware_options=options,
        )
        self._assert_advanced_firmware_qualified()
        session = self._require_session()
        has_preamp = bool(ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options))
        detector = config.detector.strip().upper()
        vbw_mode = config.vbw_mode.strip().lower()
        try:
            self._enter_spectrum_mode_with_rf_off()
            if has_preamp:
                session.write("POW:GAIN OFF")
            if config.attenuation_auto:
                session.write("POW:ATT:AUTO ON")
            else:
                session.write("POW:ATT:AUTO OFF")
                session.write(f"POW:ATT {config.attenuation_db:.12g}DB")
            session.write(f"DET {detector}")
            if config.rbw_auto:
                session.write("BAND:AUTO ON")
            else:
                session.write("BAND:AUTO OFF")
                session.write(f"BAND {config.rbw_hz:.12g}HZ")
            if vbw_mode == "auto":
                session.write("BAND:VID:AUTO ON")
            elif vbw_mode == "off":
                session.write("BAND:VID:AUTO OFF")
                session.write("BAND:VID OFF")
            else:
                session.write("BAND:VID:AUTO OFF")
                session.write(f"BAND:VID {config.vbw_hz:.12g}HZ")
            if config.sweep_time_auto:
                session.write("SWE:TIME:AUTO ON")
            else:
                session.write("SWE:TIME:AUTO OFF")
                session.write(f"SWE:TIME {config.sweep_time_s:.12g}S")
            if has_preamp and config.preamplifier_enabled:
                session.write("POW:GAIN ON")
            actual = self.read_advanced_spectrum_configuration()
            self._verify_advanced_spectrum_readback(config, actual)
            return actual
        except Exception:
            # Conservative input-path fallback. Do not hide the original fault.
            try:
                if has_preamp:
                    session.write("POW:GAIN OFF")
                session.write("POW:ATT:AUTO OFF")
                session.write("POW:ATT 60DB")
            except Exception:
                pass
            self._state = DeviceState.UNKNOWN
            raise

    @staticmethod
    def _verify_advanced_spectrum_readback(
        requested: AdvancedSpectrumConfig, actual: AdvancedSpectrumSnapshot
    ) -> None:
        mismatches: list[str] = []
        if actual.rbw_auto != requested.rbw_auto:
            mismatches.append("RBW auto state")
        if not requested.rbw_auto and not math.isclose(
            actual.rbw_hz, float(requested.rbw_hz), rel_tol=1e-6, abs_tol=1.0
        ):
            mismatches.append("RBW")
        requested_vbw_mode = requested.vbw_mode.strip().lower()
        if actual.vbw_mode != requested_vbw_mode:
            mismatches.append("VBW mode")
        if requested_vbw_mode == "manual":
            if actual.vbw_hz is None or not math.isclose(
                actual.vbw_hz,
                float(requested.vbw_hz),
                rel_tol=1e-6,
                abs_tol=1.0,
            ):
                mismatches.append("VBW")
        if actual.detector != requested.detector.strip().upper():
            mismatches.append("detector")
        if actual.attenuation_auto != requested.attenuation_auto:
            mismatches.append("attenuation auto state")
        if not requested.attenuation_auto and not math.isclose(
            actual.attenuation_db,
            float(requested.attenuation_db),
            rel_tol=0.0,
            abs_tol=0.01,
        ):
            mismatches.append("attenuation")
        if actual.preamplifier_enabled != requested.preamplifier_enabled:
            mismatches.append("preamplifier")
        if actual.sweep_time_auto != requested.sweep_time_auto:
            mismatches.append("sweep-time auto state")
        if not requested.sweep_time_auto and not math.isclose(
            actual.sweep_time_s,
            float(requested.sweep_time_s),
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            mismatches.append("sweep time")
        if mismatches:
            raise DeviceError(
                "Anritsu advanced configuration readback mismatch: " + ", ".join(mismatches)
            )

    def configure_spectrum(self, config: SpectrumConfig) -> AnritsuConfigurationSnapshot:
        validate_anritsu_trace_name(config.trace)
        validate_anritsu_spectrum(
            self._settings.safety,
            start_hz=config.start_hz,
            stop_hz=config.stop_hz,
            reference_level_dbm=config.reference_level_dbm,
            points=config.points,
            dut_max_expected_input_dbm=config.dut_max_expected_input_dbm,
        )
        session = self._require_session()
        self._enter_spectrum_mode_with_rf_off()
        session.write(f"FREQ:STAR {config.start_hz:.12g}HZ")
        session.write(f"FREQ:STOP {config.stop_hz:.12g}HZ")
        session.write(f"DISP:WIND:TRAC:Y:RLEV {config.reference_level_dbm:.12g}")
        session.write(f"SWE:POIN {config.points}")
        actual = self.read_current_configuration()
        mismatches: list[str] = []
        if not math.isclose(actual.start_hz, config.start_hz, rel_tol=0.0, abs_tol=1.0):
            mismatches.append(f"start requested={config.start_hz:g} Hz actual={actual.start_hz:g} Hz")
        if not math.isclose(actual.stop_hz, config.stop_hz, rel_tol=0.0, abs_tol=1.0):
            mismatches.append(f"stop requested={config.stop_hz:g} Hz actual={actual.stop_hz:g} Hz")
        if not math.isclose(
            actual.reference_level_dbm,
            config.reference_level_dbm,
            rel_tol=0.0,
            abs_tol=0.01,
        ):
            mismatches.append(
                "reference level requested="
                f"{config.reference_level_dbm:g} dBm actual={actual.reference_level_dbm:g} dBm"
            )
        if actual.points != config.points:
            mismatches.append(f"points requested={config.points} actual={actual.points}")
        if mismatches:
            raise DeviceError("Anritsu configuration readback mismatch: " + "; ".join(mismatches))
        return actual

    def start_live(self, ensure_continuous: bool = False) -> AnritsuConfigurationSnapshot:
        """Start Live polling, optionally enabling continuous sweep temporarily."""

        snapshot = self.read_current_configuration()
        if "SPECT" not in snapshot.instrument_mode.upper():
            raise DeviceError(
                f"Read-only Live requires Spectrum Analyzer mode; current mode is "
                f"{snapshot.instrument_mode!r}. Select Spectrum Analyzer on the instrument."
            )
        data_format = self._require_session().query("FORM?").strip().upper()
        if not data_format.startswith("ASC"):
            raise DeviceError(
                f"Read-only Live requires the current trace format to be ASCII; "
                f"the instrument reports {data_format!r}."
            )
        if ensure_continuous:
            session = self._require_session()
            # Do not probe TRAC:TYPE? here. Although documented for Spectrum
            # Analyzer mode, MS2830A firmware can leave the query unanswered
            # in measurement applications where trace-type control is not
            # available (for example SEM/Spurious contexts). Reading TRAC1
            # does not require this query; repeated identical frames are
            # detected by the UI and reported without breaking Live.
            continuous_response = session.query("INIT:CONT?").strip().upper()
            if continuous_response in {"1", "+1", "ON"}:
                continuous = True
            elif continuous_response in {"0", "+0", "OFF"}:
                continuous = False
            else:
                raise DeviceError(
                    f"Anritsu returned invalid INIT:CONT? response {continuous_response!r}."
                )
            self._restore_continuous_after_live = not continuous
            if not continuous:
                session.write("INIT:CONT ON")
        self._live = True
        return snapshot

    def stop_live(self) -> None:
        if self._restore_continuous_after_live and self._session is not None:
            self._session.write("INIT:CONT OFF")
        self._restore_continuous_after_live = False
        self._live = False

    @property
    def live(self) -> bool:
        return self._live

    def start_single_sweep(self, dut_max_expected_input_dbm: float | None = None) -> None:
        """Start one qualified SCPI sweep for a recipe checkpoint.

        This command family is intentionally unavailable until the current
        Anritsu firmware has been qualified in the station profile.  It is not
        used for the user-facing Live polling loop.
        """

        self._assert_acquisition_allowed()
        validate_anritsu_dut_input(
            self._settings.safety, dut_max_expected_input_dbm
        )
        if not self._single_sweep_supported:
            raise SafetyViolation(
                "The recipe requires qualified Anritsu standard_scpi_opc mode; "
                "the current profile permits Live/Fetch only."
            )
        session = self._require_session()
        self._enter_spectrum_mode_with_rf_off()
        session.write("INIT:CONT OFF")
        session.write("INIT:IMM")
        # DeviceState represents the connection/output safety state, not the
        # transient acquisition state; the analyser has no energy output here.
        self._state = DeviceState.VERIFIED

    def wait_complete(self, *, deadline_s: float | None = None) -> None:
        """Wait for the single sweep's `*OPC?` result with a hard deadline."""

        if not self._single_sweep_supported:
            raise SafetyViolation("No qualified Anritsu single-sweep protocol is configured.")
        timeout = deadline_s
        if timeout is None:
            timeout = parse_quantity(self._settings.acquisition.operation_complete_timeout, DIMENSION_TIME).si_value
        if timeout <= 0:
            raise SafetyViolation("Anritsu acquisition deadline must be positive.")
        session = self._require_session()
        deadline = time.monotonic() + timeout
        original_timeout_ms = session.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError("Timed out waiting for the Anritsu single sweep to complete.")
                # The VISA call itself must not outlive the application-level
                # deadline. Some backends reject a zero-millisecond timeout.
                session.timeout = max(1, min(original_timeout_ms, int(remaining * 1000)))
                response = session.query("*OPC?").strip()
                if response in {"1", "+1"}:
                    self._state = DeviceState.VERIFIED
                    return
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except Exception:
            self.emergency_off()
            raise
        finally:
            session.timeout = original_timeout_ms

    def acquire_single_sweep(
        self,
        trace: str = "TRAC1",
        dut_max_expected_input_dbm: float | None = None,
    ) -> SpectrumTrace:
        """Synchronise, then fetch one trace that belongs to this checkpoint."""

        trace = validate_anritsu_trace_name(trace)
        self.start_single_sweep(dut_max_expected_input_dbm)
        self.wait_complete()
        return self.fetch_trace(trace)

    def fetch_trace(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read one trace for a validated recipe/single-sweep workflow."""

        trace = validate_anritsu_trace_name(trace)
        self._assert_acquisition_allowed()
        session = self._require_session()
        session.write("FORM ASC")
        return self._read_ascii_trace(session, trace)

    def fetch_current_trace(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read the currently displayed trace using query commands only."""

        trace = validate_anritsu_trace_name(trace)
        session = self._require_session()
        data_format = session.query("FORM?").strip().upper()
        if not data_format.startswith("ASC"):
            raise DeviceError(
                f"Cannot read the current trace without changing the instrument: "
                f"FORM? returned {data_format!r}, not ASCII."
            )
        return self._read_ascii_trace(session, trace)

    @staticmethod
    def _read_ascii_trace(session: InstrumentSession, trace: str) -> SpectrumTrace:
        try:
            start = float(session.query("FREQ:STAR?"))
            stop = float(session.query("FREQ:STOP?"))
            points = int(float(session.query("SWE:POIN?")))
            raw = session.query(f"TRAC? {trace}")
            values = tuple(float(item) for item in raw.split(",") if item.strip())
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned an invalid trace response.") from exc
        if len(values) != points:
            raise DeviceError(
                f"Anritsu returned {len(values)} trace points; expected {points}."
            )
        if points < 2:
            raise DeviceError("Anritsu returned fewer than two trace points.")
        if not all(math.isfinite(value) for value in (start, stop, *values)):
            raise DeviceError("Anritsu returned NaN or infinity in the trace.")
        if stop <= start:
            raise DeviceError("Anritsu returned an invalid trace frequency axis.")
        step = (stop - start) / (points - 1)
        return SpectrumTrace(
            frequencies_hz=tuple(start + index * step for index in range(points)),
            powers_dbm=values,
            acquired_at_utc=datetime.now(timezone.utc),
            trace_name=trace,
        )
